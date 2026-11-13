#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply-mimo-pin.py —— mimo 钉号改造（方案 A'，自执行/自校验/自回滚，无需模型监察）

背景：2026-09-28 实测 opencode zen 上 MiMo 前缀缓存**按账号隔离**（同号 cached≈99%，
跨号=0）。least-busy 轮询会让同一会话的长前缀反复击穿。故将 mimo-v2.6-flash /
mimo-v2.6-pro 的全部流量钉在 KEY_1（带 [1,4] 优先级回退），其余 28 个模型维持均衡。

校验：mimo 两模型各只剩 1 个 deployment 且 key=OCG_KEY_1；其余模型仍 4 个；
健康/请求 200；经号池同前缀两次请求第二次 cached>0 且两次同一账号。
失败：还原 gen_config.py → 重跑生成器（自动重启）→ 再验可用性。
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
CFG = os.path.join(ROOT, "config.yaml")
STATUS = os.path.join(ROOT, "apply-mimo-pin.status.json")
TS = time.strftime("%Y%m%d-%H%M%S")
BAK = GEN + ".bak-mimopin-" + TS
POOL = "http://127.0.0.1:4000"
P = "缓存前缀测试。" + "山间清晨的雾气从谷底升起，松针上的露水闪烁。" * 500

steps = []


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def step(n, ok, d=""):
    steps.append({"name": n, "ok": bool(ok), "detail": str(d)[:300]})
    log(("  OK   " if ok else "  FAIL ") + n + ((" — " + str(d)[:160]) if d else ""))
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
        rc, out = sh("curl -s -o /dev/null -w '%%{http_code}' -m 5 %s/health/liveliness" % POOL, t=10)
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


def chat():
    body = {"model": "deepseek-v4-flash", "stream": False, "max_tokens": 8,
            "messages": [{"role": "user", "content": "1+1=? 只回答数字"}]}
    req = urllib.request.Request(POOL + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json",
                 "Authorization": "Bearer " + env_val("LITELLM_MASTER_KEY")})
    d = json.loads(urllib.request.urlopen(req, timeout=90).read())
    return "choices" in d


def cache_call(model):
    body = {"model": model, "stream": False, "max_tokens": 1,
            "messages": [{"role": "user", "content": P}]}
    req = urllib.request.Request(POOL + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json",
                 "Authorization": "Bearer " + env_val("LITELLM_MASTER_KEY")})
    d = json.loads(urllib.request.urlopen(req, timeout=180).read())
    u = d.get("usage") or {}
    return (u.get("prompt_tokens") or 0,
            (u.get("prompt_tokens_details") or {}).get("cached_tokens") or u.get("cacheRead") or 0)


def last_keys(n=2):
    rc, out = sh("tail -%d %s/pool-requests.jsonl" % (n, ROOT), t=30)
    keys = []
    for line in out.strip().splitlines()[-n:]:
        try:
            keys.append(json.loads(line).get("keyRef", "")[-8:])
        except Exception:
            pass
    return keys


OLD = (
    "for m in models:\n"
    "    for kid in healthy:\n"
    "        out += dep_block(m, healthy_idx[kid])\n"
)
NEW = (
    "# ---- mimo 缓存钉号（2026-09-28 实测：上游前缀缓存按账号隔离，跨号即击穿）----\n"
    "# key=模型名, value=账号优先级列表；取第一个健康账号，全不健康则回退任意健康账号。\n"
    "# flash/pro 作为 leader/teammate 角色承载任务，全部流量钉在 KEY_1，前缀缓存恒命中。\n"
    "PIN_MODEL_ACCT = {\n"
    "    \"mimo-v2.6-flash\": [1, 4],\n"
    "    \"mimo-v2.6-pro\": [1, 4],\n"
    "}\n"
    "for m in models:\n"
    "    pin = PIN_MODEL_ACCT.get(m)\n"
    "    if pin:\n"
    "        pick = next((i for i in pin if i in healthy_idx.values()), None)\n"
    "        if pick is None:\n"
    "            pick = next(iter(healthy_idx.values()))\n"
    "            print(f\"WARN: {m} 钉定账号 {pin} 均不可用，回退账号 {pick}（缓存冷启动一次）\")\n"
    "        out += dep_block(m, pick)\n"
    "    else:\n"
    "        for kid in healthy:\n"
    "            out += dep_block(m, healthy_idx[kid])\n"
)


def rollback(reason):
    log("==== 回滚：%s" % reason)
    if os.path.exists(BAK):
        shutil.copy2(BAK, GEN)
        log("    已还原 gen_config.py ← %s" % os.path.basename(BAK))
    rc, out = sh("python3 %s" % GEN)
    log("    重跑生成器 rc=%s" % rc)
    ok, d = wait_health(300)
    log("    回滚后健康：%s %s" % (ok, d))
    try:
        ok2 = chat()
    except Exception as e:
        ok2 = str(e)[:120]
    step("回滚后可用", bool(ok2), str(ok2))
    status("done", ok=False, reason="rolled_back: " + reason, rollback=True)


def main():
    status("running")
    log("==== mimo 钉号改造 开始")

    src = open(GEN, encoding="utf-8").read()
    if NEW in src:
        log("    已是目标态，跳过文件改动")
        changed = False
    elif OLD in src:
        shutil.copy2(GEN, BAK)
        open(GEN, "w", encoding="utf-8").write(src.replace(OLD, NEW, 1))
        changed = True
        step("改 gen_config.py（钉 KEY_1）", True, "备份 %s" % os.path.basename(BAK))
    else:
        step("改 gen_config.py", False, "锚点未命中")
        status("done", ok=False, reason="anchor-mismatch")
        return 3

    rc, out = sh("python3 %s" % GEN)
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    step("生成 config + 按需重启", rc == 0, tail[:120])
    if rc != 0:
        rollback("生成器执行失败")
        return 1

    ok, d = wait_health(300)
    step("litellm 健康", ok, d)
    if not ok:
        rollback("健康检查失败: " + d)
        return 1

    cfg = open(CFG, encoding="utf-8").read()
    blocks = cfg.split("  - model_name: ")
    def count(model):
        return sum(1 for b in blocks if b.splitlines()[0].strip() == model)
    n_flash, n_pro, n_ref = count("mimo-v2.6-flash"), count("mimo-v2.6-pro"), count("deepseek-v4-flash")
    def key_of(model):
        for b in blocks:
            if b.splitlines()[0].strip() == model:
                for line in b.splitlines():
                    if "api_key:" in line:
                        return line.strip().split("OCG_KEY_")[-1]
        return "?"
    pinned = (n_flash == 1 and n_pro == 1 and key_of("mimo-v2.6-flash") == "1" and key_of("mimo-v2.6-pro") == "1")
    step("mimo 两模型各 1 个 deployment 且钉 KEY_1", pinned,
         "flash=%d(key%s) pro=%d(key%s) 参照模型=%d" % (n_flash, key_of("mimo-v2.6-flash"), n_pro, key_of("mimo-v2.6-pro"), n_ref))
    if n_ref != 4:
        step("其余模型仍 4 号均衡", False, "deepseek-v4-flash deployment=%d" % n_ref)
        rollback("非 mimo 模型均衡被破坏")
        return 1

    try:
        ok2 = chat()
    except Exception as e:
        ok2 = str(e)[:120]
    step("请求可用", bool(ok2), str(ok2))

    # 缓存实证：同前缀两次，第二次应命中，且两次同一账号
    try:
        pt1, c1 = cache_call("mimo-v2.6-flash")
        pt2, c2 = cache_call("mimo-v2.6-flash")
        keys = last_keys(2)
        hit = (c2 or 0) > 0
        same = len(keys) == 2 and keys[0] == keys[1]
        step("缓存命中实证（flash）", hit and same,
             "1st pt=%s cached=%s | 2nd pt=%s cached=%s | 账号=%s" % (pt1, c1, pt2, c2, keys))
    except Exception as e:
        step("缓存命中实证（flash）", False, str(e)[:160])
        hit = False

    if not (pinned and ok2 and hit):
        rollback("校验未全过（pinned=%s ok=%s hit=%s）" % (pinned, ok2, hit))
        return 1

    log("==== 完成：mimo 钉号 KEY_1 生效，缓存命中实证通过")
    status("done", ok=True, reason="all-checks-passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        log("执行器异常：%s" % e)
        status("done", ok=False, reason="executor-exception: " + str(e)[:160])
        code = 1
    log("退出码 %s" % code)
    sys.exit(code)
