#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cache-ttl-test2.py —— TTL 分拣测试：定位「retention 24h 未生效」是 litellm 吞了还是网关不认
（模型固定 mimo-v2.6-flash；三组各用独立前缀，组内消息逐字节相同 → 同 sha → 同账号）

t0 写入 → sleep 390s(>5min) 复查：
  C 直连上游 + retention+key            → C2>0 = 网关认 retention ⇒ 失败点在 litellm 透传
  D 直连上游 + retention+key+cc 断点    → D2>0 = 完整配方直连有效（社区配方是否成立）
  F 经 sidecar(:4001) + retention+key+cc → F2>0 = 我们全链路(P1+P2)有效
"""
import hashlib
import json
import os
import sys
import time
import urllib.request

ROOT = "/opt/litellm"
STATUS = os.path.join(ROOT, "cache-ttl-test2.status.json")
MODEL = "mimo-v2.6-flash"
GAP = 390
FILLER = "山间清晨的雾气从谷底升起，松针上的露水在第一缕阳光下闪烁。" * 40
TS = time.strftime("%Y%m%d-%H%M%S")


def log(m):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def status(state, ok=None, reason="", data=None):
    tmp = STATUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"state": state, "ok": ok, "reason": reason, "data": data or {},
                   "started": TS, "updated": time.strftime("%F %T")},
                  f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATUS)


def env_val(k):
    for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        if line.strip().startswith(k + "="):
            return line.strip().split("=", 1)[1]
    return ""


def mk_body(prompt, tag, retention, cc):
    msgs = None
    if cc:
        msgs = [{"role": "system", "content": [{"type": "text",
                 "text": "你是简洁的技术助手。", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt,
                     "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}]
    else:
        msgs = [{"role": "user", "content": prompt}]
    body = {"model": MODEL, "stream": False, "max_tokens": 30, "messages": msgs}
    if retention:
        body["prompt_cache_key"] = hashlib.sha256(("t2-" + tag).encode()).hexdigest()[:40]
        body["prompt_cache_retention"] = "24h"
    return body


def post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers)
    d = json.loads(urllib.request.urlopen(req, timeout=180).read())
    if "choices" not in d:
        raise RuntimeError(json.dumps(d)[:150])
    u = d.get("usage") or {}
    return int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")
                or u.get("cacheRead") or 0)), u.get("prompt_tokens")


def call(tag, prompt, mode, retention, cc, phase):
    body = mk_body(prompt, tag, retention, cc)
    if mode == "direct":
        url = "https://opencode.ai/inference/go/openai/v1/chat/completions"   # 2026-10-16 规范路径
        hdr = {"content-type": "application/json",
               "Authorization": "Bearer " + env_val("OCG_KEY_1"),
               "User-Agent": "dsh-opencodego-pool/1.0",
               "x-opencode-session": "litellm-ocg-1"}
    else:
        url = "http://127.0.0.1:4001/v1/chat/completions"
        hdr = {"content-type": "application/json",
               "Authorization": "Bearer " + env_val("LITELLM_MASTER_KEY")}
    cached, pt = post(url, body, hdr)
    log("%s %s: cached=%d pt=%s" % (phase, tag, cached, pt))
    return {"cached": cached, "pt": pt}


def main():
    status("running")
    pc = "C-%s-直连retention-" % TS + FILLER
    pd = "D-%s-直连retention+cc-" % TS + FILLER
    pf = "F-%s-池retention+cc-" % TS + FILLER
    data = {"gap_s": GAP, "model": MODEL}
    log("==== TTL 分拣测试（mimo-v2.6-flash, gap=%ds）" % GAP)

    data["C1"] = call("C", pc, "direct", True, False, "t0")
    data["D1"] = call("D", pd, "direct", True, True, "t0")
    data["F1"] = call("F", pf, "pool", True, True, "t0")

    log("sleep %ds ..." % GAP)
    time.sleep(GAP)

    data["C2"] = call("C", pc, "direct", True, False, "+6.5m")
    data["D2"] = call("D", pd, "direct", True, True, "+6.5m")
    data["F2"] = call("F", pf, "pool", True, True, "+6.5m")

    c, d, f = data["C2"]["cached"] > 0, data["D2"]["cached"] > 0, data["F2"]["cached"] > 0
    data["verdict"] = {"C_direct_retention_hit": c,
                       "D_direct_full_recipe_hit": d,
                       "F_pool_full_recipe_hit": f}
    if c:
        reason = "网关认 retention ⇒ 失败点=litellm 透传（需打通参数透传）"
        ok = False
    elif d:
        reason = "retention 被忽略但 cache_control 断点有效 ⇒ P2 断点是真正杠杆（直连有效）"
        ok = f  # 池链路也要有效才算我方落地成功
        if not f:
            reason += "；但经池被剥离 ⇒ 需修 litellm 透传 cache_control"
    else:
        reason = "网关对该路由不执行 retention 也不因断点延长 TTL ⇒ 超 5min 缓存不可得，只剩粘性+5min 自动缓存"
        ok = True  # 分拣完成即为有效结论
    status("done", ok=ok, reason=reason, data=data)
    log("==== 结论: %s" % reason)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        log("测试异常: %s" % e)
        status("done", ok=False, reason="exception: " + str(e)[:200])
        code = 1
    log("退出码 %s" % code)
    sys.exit(code)
