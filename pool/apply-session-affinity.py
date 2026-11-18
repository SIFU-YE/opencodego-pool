#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply-session-affinity.py —— 方案 B：会话粘性 + 负载均衡（自执行/自校验/自回滚）

背景：A' 钉号把 mimo 全量压到 KEY_1，负载均衡失效；而上游前缀缓存按账号隔离
（同号 cached≈99%，跨号=0）。本方案用 litellm 原生 session_affinity：

  sidecar(:4001) 注入 metadata.session_id(=sha256(首条user消息)[:16])
       → litellm model_group_affinity_config.session_affinity
       → 同会话恒钉首跳 deployment（缓存 100%），无 session 的请求照常 least-busy（均衡）

阶段：
  A sidecar 安装 + 冒烟（无中断）
  B gen_config：撤销 mimo 钉号（恢复每模型 4 deployment）+ 注入 affinity 配置；
    pool_logger 加 sess 观测字段；跑 gen_config → litellm 重启
  C 验证：配置落地 / 同会话两次同账号+二次 cached>0 / 多会话铺开(WARN 级)
  D DSH litellm provider baseURL :4000→:4001（HMR 热切，不重启 DSH）
  失败 → 还原三处备份 + 重跑 gen_config + 重启 litellm
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = "/opt/litellm"
GEN = os.path.join(ROOT, "gen_config.py")
POOL_LOGGER = os.path.join(ROOT, "pool_logger.py")
CFG = os.path.join(ROOT, "config.yaml")
SIDECAR = os.path.join(ROOT, "session-sidecar.py")
UNIT = "/etc/systemd/system/litellm-session-sidecar.service"
CORDIS = "/root/.dsh/profiles/desktop/cordis.patch.yml"
STATUS = os.path.join(ROOT, "apply-session-affinity.status.json")
TS = time.strftime("%Y%m%d-%H%M%S")
B = lambda p: p + ".bak-affinity-" + TS

MASTER = ""
steps = []


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def step(name, ok, d="", hard=True):
    steps.append({"name": name, "ok": bool(ok), "detail": str(d)[:300],
                  "hard": hard})
    log(("  OK   " if ok else ("  FAIL " if hard else "  WARN ")) + name +
        ((" — " + str(d)[:200]) if d else ""))
    return bool(ok)


def status(state, ok=None, reason="", rollback=False):
    data = {"state": state, "ok": ok, "reason": reason, "rollback": rollback,
            "steps": steps, "started": TS, "updated": time.strftime("%F %T")}
    tmp = STATUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATUS)


def sh(cmd, t=300):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except Exception as e:
        return 125, str(e)


def env_val(k):
    for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        if line.strip().startswith(k + "="):
            return line.strip().split("=", 1)[1]
    return ""


def wait_health(timeout=300):
    end = time.time() + timeout
    while time.time() < end:
        rc, out = sh("curl -s -o /dev/null -w '%{http_code}' -m 5 "
                     "http://127.0.0.1:4000/health/liveliness", t=10)
        if out.strip()[-3:] == "200":
            return True, "health 200"
        rc2, st = sh("docker inspect -f '{{.State.Running}}' litellm", t=20)
        if rc2 == 0 and st.strip() == "false":
            time.sleep(6)
            rc3, st2 = sh("docker inspect -f '{{.State.Running}}' litellm", t=20)
            if rc3 == 0 and st2.strip() == "false":
                return False, "容器已退出"
        time.sleep(3)
    return False, "健康检查超时(%ss)" % timeout


def chat(port, prompt, model="mimo-v2.6-flash", max_tokens=16, timeout=180):
    body = {"model": model, "stream": False, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % port,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json",
                 "Authorization": "Bearer " + MASTER})
    d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    if "choices" not in d:
        raise RuntimeError("no choices: " + json.dumps(d)[:150])
    return d


def tail_rows(n=8):
    rc, out = sh("tail -%d %s/pool-requests.jsonl" % (n, ROOT), t=30)
    rows = []
    for line in out.strip().splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def edit_file(path, changes):
    """changes: [(名称, old, new)]；old 缺失且 new 在 → 跳过。"""
    src = open(path, encoding="utf-8").read()
    out, applied = src, []
    for name, old, new in changes:
        # 先看目标态：new 已在（即使 old 也作为其子串存在）→ 跳过，避免重复注入
        if new in out:
            log("    已是目标态，跳过：%s" % name)
            continue
        if old not in out:
            raise RuntimeError("锚点未命中：%s @ %s" % (name, path))
        if out.count(old) != 1:
            raise RuntimeError("锚点出现 %d 次：%s" % (out.count(old), name))
        out = out.replace(old, new, 1)
        applied.append(name)
    if applied:
        shutil.copy2(path, B(path))
        open(path, "w", encoding="utf-8").write(out)
        log("    已写入 %s（备份 %s）：%s" % (os.path.basename(path),
                                            os.path.basename(B(path)),
                                            "；".join(applied)))
    return bool(applied)


# ---------------- 锚点定义 ----------------
PIN_OLD = """# ---- mimo 缓存钉号（2026-09-28 实测：上游前缀缓存按账号隔离，跨号即击穿）----
# key=模型名, value=账号优先级列表；取第一个健康账号，全不健康则回退任意健康账号。
# flash/pro 作为 leader/teammate 角色承载任务，全部流量钉在 KEY_1，前缀缓存恒命中。
PIN_MODEL_ACCT = {
    "mimo-v2.6-flash": [1, 4],
    "mimo-v2.6-pro": [1, 4],
}
for m in models:
    pin = PIN_MODEL_ACCT.get(m)
    if pin:
        pick = next((i for i in pin if i in healthy_idx.values()), None)
        if pick is None:
            pick = next(iter(healthy_idx.values()))
            print(f"WARN: {m} 钉定账号 {pin} 均不可用，回退账号 {pick}（缓存冷启动一次）")
        out += dep_block(m, pick)
    else:
        for kid in healthy:
            out += dep_block(m, healthy_idx[kid])
"""
PIN_NEW = """for m in models:
    for kid in healthy:
        out += dep_block(m, healthy_idx[kid])
"""

OUT_ANCHOR = 'out = ["# 由 gen_config.py 自动生成，请勿手改；增删账号/模型后重跑: python3 /opt/litellm/gen_config.py",'
OUT_NEW = """# ---- 会话粘性（方案B，2026-09-28）：带 session_id 的请求钉住首跳 deployment，
# 无 session_id 的照常均衡 —— 同会话前缀缓存恒命中 + 会话间负载均衡。
affinity = "".join(
    f"    {m}: [session_affinity]" + ("\\n" if i + 1 < len(models) else "")
    for i, m in enumerate(models)
)
out = ["# 由 gen_config.py 自动生成，请勿手改；增删账号/模型后重跑: python3 /opt/litellm/gen_config.py","""

ROUTER_OLD = '''    "router_settings:",
    "  routing_strategy: least-busy",
    "  allowed_fails: 3",
    "  cooldown_time: 60",
    "  num_retries: 2",
    "",
    "general_settings:",'''
ROUTER_NEW = '''    "router_settings:",
    "  routing_strategy: least-busy",
    "  allowed_fails: 3",
    "  cooldown_time: 60",
    "  num_retries: 2",
    "  deployment_affinity_ttl_seconds: 21600",
    "  model_group_affinity_config:",
    affinity,
    "",
    "general_settings:",'''

PLG_OLD = """            pt, ct, tt = _usage_of(kwargs, response_obj)
            rec = {
                "ts": int(end_time.timestamp() * 1000) if end_time else 0,"""
PLG_NEW = """            pt, ct, tt = _usage_of(kwargs, response_obj)
            try:
                _md = kwargs.get("metadata") or (kwargs.get("litellm_params") or {}).get("metadata") or {}
                sess = str(_md.get("session_id") or "")[:16]
            except Exception:
                sess = ""
            rec = {
                "sess": sess,
                "ts": int(end_time.timestamp() * 1000) if end_time else 0,"""

CORDIS_OLD = """      litellm:
        api: openai-completions
        apiKeyEnv: LITELLM_API_KEY
        baseURL: http://127.0.0.1:4000/v1"""
CORDIS_NEW = """      litellm:
        api: openai-completions
        apiKeyEnv: LITELLM_API_KEY
        baseURL: http://127.0.0.1:4001/v1"""


def rollback(reason, took):
    log("==== 回滚：%s" % reason)
    for path in took + [GEN, POOL_LOGGER, CORDIS]:
        bak = B(path)
        if not os.path.exists(bak):
            # 续跑场景：回退到该文件最新一份 affinity 备份
            rc, out = sh("ls -t %s.bak-affinity-* 2>/dev/null | head -1" % path, t=20)
            cand = out.strip().splitlines()
            bak = cand[-1] if cand else ""
        if bak and os.path.exists(bak):
            shutil.copy2(bak, path)
            log("    还原 %s ← %s" % (os.path.basename(path), os.path.basename(bak)))
    rc, out = sh("python3 %s" % GEN, t=300)
    log("    重跑生成器 rc=%s" % rc)
    sh("systemctl restart litellm-session-sidecar", t=60)
    ok, d = wait_health(300)
    try:
        ok2 = bool(chat(4000, "1+1=? 只回答数字", max_tokens=8))
    except Exception as e:
        ok2 = str(e)[:120]
    step("回滚后可用", bool(ok2), str(ok2))
    status("done", ok=False, reason="rolled_back: " + reason, rollback=True)


def main():
    global MASTER
    status("running")
    MASTER = env_val("LITELLM_MASTER_KEY")
    log("==== 方案B：会话粘性 + 负载均衡 开始")
    took = []

    # ---------- A. sidecar ----------
    log("-- A. sidecar 安装/冒烟（无中断）--")
    rc, out = sh("systemctl daemon-reload")
    rc2, out2 = sh("systemctl enable --now litellm-session-sidecar", t=60)
    rc3, out3 = sh("systemctl restart litellm-session-sidecar", t=60)
    time.sleep(2)
    act = sh("systemctl is-active litellm-session-sidecar", t=20)[1].strip()
    step("sidecar 服务 active", act == "active", act)
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:4001/v1/models",
            headers={"Authorization": "Bearer " + MASTER}), timeout=15)
        step("sidecar 透传 /v1/models", r.status == 200, "HTTP %s" % r.status)
    except Exception as e:
        step("sidecar 透传 /v1/models", False, str(e)[:120])
        rollback("sidecar 不可用", took)
        return 1
    try:
        d = chat(4001, "sidecar 冒烟 1+1=? 只回答数字", model="deepseek-v4-flash")
        step("sidecar 冒烟 chat", True, "model=%s" % d.get("model"))
    except Exception as e:
        step("sidecar 冒烟 chat", False, str(e)[:120])
        rollback("sidecar 冒烟失败", took)
        return 1

    # ---------- B. 配置改动 ----------
    log("-- B. gen_config 撤钉号 + affinity；pool_logger 加 sess --")
    try:
        if edit_file(GEN, [("撤销 mimo 钉号（恢复每模型×4账号）", PIN_OLD, PIN_NEW),
                           ("注入 affinity 构建", OUT_ANCHOR, OUT_NEW),
                           ("router_settings 加 session_affinity", ROUTER_OLD, ROUTER_NEW)]):
            took.append(GEN)
        if edit_file(POOL_LOGGER, [("pool_logger 记录 sess", PLG_OLD, PLG_NEW)]):
            took.append(POOL_LOGGER)
        step("应用文件改动", True, "gen/pool_logger")
    except Exception as e:
        step("应用文件改动", False, str(e)[:200])
        rollback("文件改动失败", took)
        return 1

    pre_rows = len(tail_rows(1))
    rc, out = sh("python3 %s" % GEN, t=300)
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    step("生成 config + 按需重启", rc == 0, tail[:140])
    if rc != 0:
        rollback("生成器执行失败", took)
        return 1
    ok, d = wait_health(300)
    step("litellm 健康", ok, d)
    if not ok:
        rollback("重启后健康失败: " + d, took)
        return 1

    cfg = open(CFG, encoding="utf-8").read()
    blocks = cfg.split("  - model_name: ")
    n_flash = sum(1 for b in blocks if b.splitlines()[0].strip() == "mimo-v2.6-flash")
    n_pro = sum(1 for b in blocks if b.splitlines()[0].strip() == "mimo-v2.6-pro")
    conf_ok = ("model_group_affinity_config:" in cfg
               and "mimo-v2.6-flash: [session_affinity]" in cfg
               and "deployment_affinity_ttl_seconds: 21600" in cfg
               and n_flash == 4 and n_pro == 4)
    step("配置落地（mimo 恢复4部署 + affinity 开启）", conf_ok,
         "flash=%d pro=%d affinity=%s" % (n_flash, n_pro,
                                          "model_group_affinity_config:" in cfg))
    plg = open(POOL_LOGGER, encoding="utf-8").read()
    step("pool_logger sess 字段就位", '"sess": sess' in plg)

    if not conf_ok:
        rollback("配置落地校验失败", took)
        return 1

    # ---------- C. 粘性 + 缓存 + 铺开验证 ----------
    log("-- C. 粘性/缓存/铺开验证 --")
    marker = "affinity-mark-%s-" % TS
    filler = "山间清晨的雾气从谷底升起，松针上的露水在第一缕阳光下闪烁。" * 40  # ~400+ token
    prompt = marker + filler
    try:
        d1 = chat(4001, prompt)
        time.sleep(1)
        d2 = chat(4001, prompt)
        c1 = ((d1.get("usage") or {}).get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        c2 = ((d2.get("usage") or {}).get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        rows = [r for r in tail_rows(6) if str(r.get("sess") or "")]
        last2 = rows[-2:] if len(rows) >= 2 else []
        same_sess = len(last2) == 2 and last2[0].get("sess") == last2[1].get("sess")
        same_acct = len(last2) == 2 and last2[0].get("keyRef") == last2[1].get("keyRef")
        step("同会话两次 → 同一账号（粘性生效）", same_sess and same_acct,
             "sess=%s keyRef尾=%s" % ([r.get('sess') for r in last2],
                                      [str(r.get('keyRef'))[-8:] for r in last2]))
        step("二次请求命中前缀缓存", int(c2) > 0,
             "1st cached=%s → 2nd cached=%s (pt=%s)" %
             (c1, c2, (d2.get("usage") or {}).get("prompt_tokens")))
    except Exception as e:
        step("粘性+缓存验证", False, str(e)[:200])
        rollback("粘性/缓存验证异常", took)
        return 1

    # 多会话铺开（staggered 利用 least-busy 的在途数差）
    try:
        results = []
        def fire(i):
            try:
                chat(4001, "spread-%d-%s 随机铺开测试，回答 ok" % (i, TS), max_tokens=8)
                results.append(True)
            except Exception:
                results.append(False)
        t0mark = time.time()
        ths = []
        for i in range(6):
            th = threading.Thread(target=fire, args=(i,))
            th.start(); ths.append(th); time.sleep(0.7)
        for th in ths:
            th.join()
        time.sleep(2)
        fresh = [r for r in tail_rows(24)
                 if (r.get("t0") or 0) / 1000 >= t0mark - 1 and r.get("sess")]
        accts = {str(r.get("keyRef"))[-8:] for r in fresh}
        step("多会话铺开（≥2 个账号）", len(accts) >= 2,
             "本次 %d 条 session 请求落在 %d 个账号: %s" %
             (len(fresh), len(accts), sorted(accts)), hard=False)
    except Exception as e:
        step("多会话铺开", False, str(e)[:160], hard=False)

    # ---------- D. DSH 热切 baseURL ----------
    log("-- D. DSH litellm provider 切到 :4001（HMR 热切）--")
    try:
        if edit_file(CORDIS, [("DSH litellm→sidecar", CORDIS_OLD, CORDIS_NEW)]):
            took.append(CORDIS)
        step("cordis.patch.yml baseURL 已切换", True, ":4000 → :4001")
    except Exception as e:
        step("cordis.patch.yml 切换", False, str(e)[:200])
        rollback("DSH 配置切换失败", took)
        return 1

    # 切换后两侧可达性
    for port, label in ((4000, "直连 litellm"), (4001, "sidecar")):
        try:
            d = chat(port, "切换后 %s 可用性 1+1=? 只回答数字" % label,
                     model="deepseek-v4-flash", max_tokens=8)
            step("%s 切换后可用" % label, True, "200")
        except Exception as e:
            step("%s 切换后可用" % label, False, str(e)[:120])

    log("==== 完成")
    status("done", ok=True, reason="all-checks-passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        log("执行器异常：%s" % e)
        status("done", ok=False, reason="executor-exception: " + str(e)[:200])
        code = 1
    log("退出码 %s" % code)
    sys.exit(code)
