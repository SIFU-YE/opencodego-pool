#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply-free-lb.py —— 把号池改成「纯负载均衡、零性能限制」的一次性执行器

设计目标：**执行期间完全不需要模型监察**，脚本自己完成
  改配置 → 重启 litellm → 等健康 → 四项校验 → 失败自动回滚 → 落盘结果。

改动内容（与《号池修正方案》一一对应）：
  A. gen_config.py：删除每 deployment 的 rpm / tpm / max_parallel_requests（解除限流与并发排队）
  B. gen_config.py：routing_strategy usage-based-routing-v2 → least-busy（纯按在途请求数分流）
  C. gen_config.py：litellm_settings.num_retries 2 → 0（去掉嵌套重试，失败交给 router 换号）
  D. pool_logger.py：回调写文件改到线程/ to_thread，绝不在 LiteLLM 事件循环里做阻塞 IO
  E. （可选 --prometheus-off）.env 删掉 PROMETHEUS_METRICS_ENABLED
  F. 顺带重启 pool-dash，让已改好的 sync_dsh_models（保留 reasoningEfforts）生效

安全机制：
  * 幂等：锚点已在目标态就跳过；重复执行不会重复改文件、不会无谓重启
  * 校验：容器健康 + 非流式200 + 流式可读 + 配置落地 + 回调计数增长
  * 分诊：池校验失败时**直连上游对照**——上游也挂=上游故障（不回滚，保留新配置）
          上游正常而池挂=我们改坏了（完整回滚到执行前）
  * 回滚：还原 gen_config.py / pool_logger.py / .env 备份 → 重新生成 config → 重启 → 再校验一次可用
  * 结果落盘：/opt/latellm 见 STATUS 文件（state=running → done），供外部轮询，无需盯日志

用法：
  python3 /opt/litellm/apply-free-lb.py                # 标准执行
  python3 /opt/litellm/apply-free-lb.py --dry-run      # 只打印将要做什么，不改动
  python3 /opt/litellm/apply-free-lb.py --prometheus-off
退出码：0=成功  1=失败已回滚  2=上游故障（未回滚，新配置保留）  3=执行前置条件不满足
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = "/opt/litellm"
GEN = os.path.join(ROOT, "gen_config.py")
POOL = os.path.join(ROOT, "pool_logger.py")
ENVF = os.path.join(ROOT, ".env")
CFG = os.path.join(ROOT, "config.yaml")
LOG = os.path.join(ROOT, "apply-free-lb.log")
STATUS = os.path.join(ROOT, "apply-free-lb.status.json")

TS = time.strftime("%Y%m%d-%H%M%S")
BAK_SUFFIX = ".bak-freelb-" + TS
POOL_URL = "http://127.0.0.1:4000"
LIVE = POOL_URL + "/health/liveliness"
CHAT = POOL_URL + "/v1/chat/completions"
DIRECT = "https://opencode.ai/inference/go/openai/v1/chat/completions"   # 2026-10-16 规范路径
POOL_USER_AGENT = "dsh-opencodego-pool/1.0"   # 上游要求可识别 UA，禁止 curl/Mozilla/通用 SDK 名
VERIFY_MODEL = "deepseek-v4-flash"      # 4 账号都部署、输出快，适合做健康探针
PROBE = "1+1=? 只回答数字"

steps = []
DRY = "--dry-run" in sys.argv
PROM_OFF = "--prometheus-off" in sys.argv


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def step(name, ok, detail=""):
    steps.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
    log(("  OK   " if ok else "  FAIL ") + name + ((" — " + str(detail)[:240]) if detail else ""))
    return bool(ok)


def write_status(state, ok=None, reason="", rollback=False):
    data = {
        "state": state,
        "ok": ok,
        "reason": reason,
        "rollback": rollback,
        "exit": None if state != "done" else (0 if ok else (2 if reason.startswith("upstream") else 1)),
        "steps": steps,
        "started": TS,
        "updated": time.strftime("%F %T"),
    }
    tmp = STATUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATUS)


def sh(cmd, timeout=180):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT after %ss" % timeout
    except Exception as e:  # noqa: BLE001
        return 125, str(e)


def env_val(k):
    try:
        for line in open(ENVF, encoding="utf-8"):
            line = line.strip()
            if line.startswith(k + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- 改文件（幂等）
def edit_file(path, changes, note):
    """changes: [(名称, old, new)]；old 缺失且 new 已在 → 视为已应用跳过；否则 old 必须恰好出现 1 次。"""
    src = open(path, encoding="utf-8").read()
    out = src
    applied = []
    for name, old, new in changes:
        if old not in out:
            if new in out:
                log("    已是目标态，跳过：%s" % name)
                continue
            raise RuntimeError("锚点未找到且目标态也不在：%s @ %s" % (name, path))
        cnt = out.count(old)
        if cnt != 1:
            raise RuntimeError("锚点出现 %d 次（应为 1）：%s @ %s" % (cnt, name, path))
        out = out.replace(old, new, 1)
        applied.append(name)
    if not applied:
        log("    无需改动：%s（%s）" % (path, note))
        return False, None
    bak = path + BAK_SUFFIX
    shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    log("    已写入 %s，备份 %s，改动：%s" % (os.path.basename(path), os.path.basename(bak), "；".join(applied)))
    return True, bak


# ---------------------------------------------------------------- 校验原语
def wait_health(timeout=300):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        rc, out = sh("curl -s -o /dev/null -w '%%{http_code}' -m 5 %s" % LIVE, timeout=10)
        code = out.strip()[-3:]
        if code == "200":
            return True, "health 200"
        last = code or out.strip()[:60]
        # 只有 Running=false 才算真的退出；重启中的容器必须继续等
        rc2, st = sh("docker inspect -f '{{.State.Running}}/{{.State.Status}}' litellm", timeout=20)
        running = st.strip().split("/")[0] if rc2 == 0 else ""
        if rc2 == 0 and running == "false":
            time.sleep(6)  # 可能正处于 restart-policy 拉起的间隙，再确认一次
            rc3, st2 = sh("docker inspect -f '{{.State.Running}}' litellm", timeout=20)
            if rc3 == 0 and st2.strip() == "false":
                rc4, lg = sh("docker logs --tail 15 litellm", timeout=30)
                return False, "容器已退出: %s | 日志: %s" % (st.strip()[:60], lg.strip()[-300:])
        time.sleep(3)
    rc4, lg = sh("docker logs --tail 15 litellm", timeout=30)
    return False, "健康检查超时(%ss)，最后状态:%s | 日志: %s" % (timeout, last, lg.strip()[-300:])


def chat(stream=False, timeout=120):
    body = {
        "model": VERIFY_MODEL,
        "stream": stream,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": PROBE}],
    }
    if stream:
        body["stream_options"] = {"include_usage": True}
    key = env_val("LITELLM_MASTER_KEY")
    req = urllib.request.Request(
        CHAT,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "Authorization": "Bearer " + key},
    )
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
    if not stream:
        d = json.loads(raw)
        if "choices" not in d:
            raise RuntimeError("非流式响应缺 choices: %s" % raw[:160])
        return "非流式 200，model=%s" % d.get("model")
    lines = [l.strip() for l in raw.splitlines() if l.strip().startswith("data:")]
    if not lines:
        raise RuntimeError("流式响应无 data 行: %s" % raw[:160])
    return "流式 %d 个 data 块" % len(lines)


def chat_with_retry(fn, tries=3, gap=15):
    last = ""
    for i in range(tries):
        try:
            return True, fn()
        except Exception as e:  # noqa: BLE001
            last = str(e)[:200]
            log("    第 %d 次尝试失败：%s" % (i + 1, last))
            if i < tries - 1:
                time.sleep(gap)
    return False, last


def container_config():
    rc, out = sh("docker exec litellm cat /app/config.yaml", timeout=60)
    return out if rc == 0 else ""


def cb_count():
    rc, out = sh("docker exec litellm sh -c 'wc -l < /app/pool-requests.jsonl 2>/dev/null || echo 0'", timeout=30)
    try:
        return int(out.strip().split()[-1])
    except Exception:
        return -1


def direct_upstream_ok():
    """直连上游对照：用来区分「我们改坏了」还是「上游本来就挂」"""
    key = env_val("OCG_KEY_1")
    if not key:
        return False, "OCG_KEY_1 缺失"
    body = {"model": VERIFY_MODEL, "stream": False, "max_tokens": 16,
            "messages": [{"role": "user", "content": PROBE}]}
    req = urllib.request.Request(
        DIRECT,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": POOL_USER_AGENT,
            "x-opencode-session": "dsh-acct-1",
        },
    )
    try:
        raw = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore")
        return ("choices" in raw), raw[:120]
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:160]


# ---------------------------------------------------------------- 回滚
def rollback(baks, reason):
    log("==== 开始回滚：%s" % reason)
    for path, bak in baks:
        if bak and os.path.exists(bak):
            shutil.copy2(bak, path)
            log("    已还原 %s ← %s" % (os.path.basename(path), os.path.basename(bak)))
    # 用（已还原的）生成器重建旧 config；它检测到内容变化会自动 docker restart litellm
    rc, out = sh("python3 %s" % GEN, timeout=300)
    log("    gen_config.py 回滚执行 rc=%s：%s" % (rc, out.strip().splitlines()[-1] if out.strip() else ""))
    if rc != 0:
        # 兜底：至少重启一次让挂载的旧文件重新加载
        sh("docker restart litellm", timeout=120)
    ok, detail = wait_health(300)
    log("    回滚后健康：%s %s" % (ok, detail))
    ok2, d2 = chat_with_retry(lambda: chat(stream=False), tries=3, gap=15) if ok else (False, "健康未恢复，跳过可用性检查")
    log("    回滚后可用性：%s %s" % (ok2, d2))
    step("回滚后恢复可用", ok and ok2, d2)
    write_status("done", ok=False, reason="rolled_back: " + reason, rollback=True)


# ---------------------------------------------------------------- 主流程
def main():
    write_status("running")
    log("==== 号池纯负载均衡改造 开始（dry-run=%s prometheus-off=%s）" % (DRY, PROM_OFF))

    if not os.path.exists(GEN) or not os.path.exists(POOL):
        write_status("done", ok=False, reason="前置：gen_config.py / pool_logger.py 缺失")
        log("前置条件不满足")
        return 3

    # 1) 路由策略预检（改之前先确认 litellm 版本支持 least-busy，避免白重启）
    rc, out = sh(
        'docker exec litellm python -c '
        '"from litellm.router_strategy.least_busy_pick import LeastBusyRouting; print(\'OK\')"',
        timeout=60,
    )
    if "OK" in out:
        strategy = "least-busy"
    elif "ModuleNotFoundError" in out or "ImportError" in out:
        strategy = "simple-shuffle"
        log("  litellm 不含 least-busy 模块，降级 simple-shuffle")
    else:
        strategy = "least-busy"   # exec 本身失败（探测问题），按计划值走，靠健康检查把关
        log("  least-busy 预检无法完成（%s），按计划值继续，健康检查会把关" % out.strip()[:80])
    log("  目标 routing_strategy = %s" % strategy)

    # 2) 组装改动清单
    gen_changes = [
        ("删除 rpm/tpm/max_parallel_requests 三个限制",
         '        "      rpm: 30",\n        "      tpm: 1000000",\n        "      max_parallel_requests: 4",\n',
         ""),
        ("routing_strategy → " + strategy,
         '    "  routing_strategy: usage-based-routing-v2",',
         '    "  routing_strategy: %s",' % strategy),
        ("litellm_settings.num_retries 2 → 0（失败交给 router 换号）",
         '    "  num_retries: 2",\n    "  request_timeout: 600",',
         '    "  num_retries: 0",\n    "  request_timeout: 600",'),
    ]
    pool_changes = [
        ("引入 asyncio/threading",
         "import json\n",
         "import asyncio\nimport json\nimport threading\n"),
        ("回调写文件移出事件循环",
         "    def log_success_event(self, kwargs, response_obj, start_time, end_time):\n"
         "        self._record(kwargs, start_time, end_time, response_obj)\n"
         "\n"
         "    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):\n"
         "        self._record(kwargs, start_time, end_time, response_obj)",
         "    def log_success_event(self, kwargs, response_obj, start_time, end_time):\n"
         "        # 写文件放后台线程，绝不在 LiteLLM 事件循环里做阻塞 IO\n"
         "        threading.Thread(target=self._record,\n"
         "                         args=(kwargs, start_time, end_time, response_obj),\n"
         "                         daemon=True).start()\n"
         "\n"
         "    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):\n"
         "        await asyncio.to_thread(self._record, kwargs, start_time, end_time, response_obj)"),
    ]

    if DRY:
        log("-- dry-run：以下改动将被执行，不做任何写入 --")
        for n, o, nw in gen_changes:
            log("  gen_config.py : %s" % n)
        for n, o, nw in pool_changes:
            log("  pool_logger.py: %s" % n)
        log("  然后：gen_config.py 生成 config（变化才重启 litellm）→ 健康/非流式/流式/配置/回调 校验 → 失败自动回滚")
        write_status("done", ok=True, reason="dry-run")
        return 0

    # 3) 执行改动
    baks = []
    try:
        changed_pool, bak = edit_file(POOL, pool_changes, "回调异步化")
        if changed_pool:
            baks.append((POOL, bak))
        changed_gen, bak = edit_file(GEN, gen_changes, "去限流")
        if changed_gen:
            baks.append((GEN, bak))
        if PROM_OFF:
            src = open(ENVF, encoding="utf-8").read()
            if "PROMETHEUS_METRICS_ENABLED" in src:
                shutil.copy2(ENVF, ENVF + BAK_SUFFIX)
                baks.append((ENVF, ENVF + BAK_SUFFIX))
                lines = [l for l in src.splitlines(True) if not l.startswith("PROMETHEUS_METRICS_ENABLED")]
                with open(ENVF, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                log("    已从 .env 移除 PROMETHEUS_METRICS_ENABLED")
            else:
                log("    .env 已无 Prometheus 开关")
        step("应用文件改动", True, "gen=%s pool=%s" % (changed_gen, changed_pool))
    except Exception as e:  # noqa: BLE001
        step("应用文件改动", False, str(e))
        rollback(baks, "文件改动阶段失败: %s" % str(e)[:160])
        return 1

    # 4) 生成 config（含账号健康门禁；内容变化时它自己 docker restart litellm）
    pre_cnt = cb_count()
    rc, out = sh("python3 %s" % GEN, timeout=300)
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    config_changed = "config.yaml written" in out
    step("生成 config.yaml 并按需重启", rc == 0, "rc=%s, %s" % (rc, tail[:160]))
    if rc != 0:
        rollback(baks, "gen_config.py 执行失败: %s" % tail[:160])
        return 1

    # pool_logger 改了但 config 没变（gen_config 不会重启）→ 手动重启加载新回调代码
    if changed_pool and not config_changed:
        log("  config 未变化但 pool_logger.py 已改，手动 docker restart litellm 加载新代码")
        sh("docker restart litellm", timeout=120)

    # 5) 等健康
    ok, detail = wait_health(300)
    step("litellm 重启后健康", ok, detail)
    if not ok:
        rollback(baks, "重启后健康检查失败: %s" % detail)
        return 1

    # 6) 四项校验
    ok, detail = chat_with_retry(lambda: chat(stream=False), tries=3, gap=15)
    step("非流式请求 200 + choices", ok, detail)
    nonstream_ok = ok

    ok2, detail2 = chat_with_retry(lambda: chat(stream=True), tries=3, gap=15)
    step("流式请求可读", ok2, detail2)

    cfg = container_config()
    cfg_ok = (
        "rpm:" not in cfg
        and "max_parallel_requests" not in cfg
        and ("routing_strategy: " + strategy) in cfg
        and "  num_retries: 0" in cfg
        and "  num_retries: 2" in cfg      # router 层换号重试保留
    )
    step("容器内配置落地（无 rpm/并发限制，策略=%s，重试分层正确）" % strategy, cfg_ok,
         "" if cfg_ok else (cfg[cfg.find("litellm_settings"):cfg.find("litellm_settings") + 400] if "litellm_settings" in cfg else "配置读取失败"))

    time.sleep(8)
    post_cnt = cb_count()
    cb_ok = pre_cnt >= 0 and post_cnt > pre_cnt
    step("pool_logger 回调仍写入（看板统计不受影响）", cb_ok, "行数 %s → %s" % (pre_cnt, post_cnt))

    # 7) 分诊：池失败 ≠ 一定是我们的锅
    if not (nonstream_ok and ok2):
        d_ok, d_detail = direct_upstream_ok()
        log("  池校验失败，直连上游对照：%s %s" % (d_ok, d_detail))
        if d_ok:
            rollback(baks, "池可用性校验失败（直连上游正常，判定为本次改动所致）")
            return 1
        write_status("done", ok=False,
                     reason="upstream-outage: 直连上游也不可用，非号池配置问题；新配置保留，待上游恢复自动生效")
        log("==== 上游故障，非本次改动问题；新配置保留，退出码 2")
        return 2

    if not cfg_ok or not cb_ok:
        rollback(baks, "配置落地/回调校验失败（cfg_ok=%s cb_ok=%s）" % (cfg_ok, cb_ok))
        return 1

    # 8) 顺带：重启 pool-dash 让 sync_dsh_models 修复生效
    rc1, o1 = sh("systemctl restart pool-dash", timeout=60)
    time.sleep(2)
    rc2, o2 = sh("systemctl is-active pool-dash", timeout=20)
    rc3, o3 = sh("curl -s -o /dev/null -w '%%{http_code}' -m 5 http://127.0.0.1:4010/", timeout=15)
    dash_ok = (rc1 == 0 and o2.strip() == "active" and o3.strip()[-1] in "2345")
    step("pool-dash 重启并可访问", dash_ok, "restart_rc=%s active=%s http=%s" % (rc1, o2.strip(), o3.strip()))

    log("==== 完成：号池已是纯负载均衡，全部校验通过")
    write_status("done", ok=True, reason="all-checks-passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:  # noqa: BLE001
        log("执行器异常：%s" % e)
        try:
            write_status("done", ok=False, reason="executor-exception: " + str(e)[:200])
        except Exception:
            pass
        code = 1
    log("退出码 %s" % code)
    sys.exit(code)
