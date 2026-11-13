#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cache-ttl-test.py —— 网关缓存 TTL「偏移」对照测试（模型固定 mimo-v2.6-flash）

对照设计（全部经 sidecar :4001，两条 prompt 前缀彼此不同 → 缓存条目互不干扰；
          同一 pair 内消息完全相同 → sidecar sha 相同 → 同一会话同一账号）：
  A 组（对照）：不带缓存字段，t0 写入 → t0+390s(>5min 默认 TTL) 复查 → 期望 cached=0（过期）
  B 组（实验）：带 prompt_cache_key + prompt_cache_retention:"24h" → 同间隔复查 → 期望 cached>0（retention 生效）
耗时 ~7 分钟，独立进程 + status.json 落盘，无需盯守。
"""
import hashlib
import json
import os
import sys
import time
import urllib.request

ROOT = "/opt/litellm"
STATUS = os.path.join(ROOT, "cache-ttl-test.status.json")
MODEL = "mimo-v2.6-flash"
GAP = 390  # 6.5 分钟 > 5 分钟默认 TTL
FILLER = "山间清晨的雾气从谷底升起，松针上的露水在第一缕阳光下闪烁。" * 40  # ~900+ token
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


def call(prompt, retention, tag):
    body = {"model": MODEL, "stream": False, "max_tokens": 30,
            "messages": [{"role": "user", "content": prompt}]}
    if retention:
        body["prompt_cache_key"] = hashlib.sha256(("ttl-" + tag).encode()).hexdigest()[:40]
        body["prompt_cache_retention"] = "24h"
    req = urllib.request.Request(
        "http://127.0.0.1:4001/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json",
                 "Authorization": "Bearer " + env_val("LITELLM_MASTER_KEY")})
    d = json.loads(urllib.request.urlopen(req, timeout=180).read())
    if "choices" not in d:
        raise RuntimeError(json.dumps(d)[:150])
    u = d.get("usage") or {}
    cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")
              or u.get("cacheRead") or 0)
    return {"cached": int(cached), "pt": u.get("prompt_tokens"),
            "ct": u.get("completion_tokens")}


def sess_of(prompt):
    key = hashlib.sha256(prompt[:4096].encode("utf-8", "ignore")).hexdigest()[:16]
    return key


def acct_of(sess):
    """从 pool-requests.jsonl 找该 sess 最近一条的账号尾号"""
    try:
        rows = [json.loads(l) for l in open(os.path.join(ROOT, "pool-requests.jsonl"))
                if '"sess": "%s"' % sess in l]
        return str(rows[-1].get("keyRef"))[-8:] if rows else "?"
    except Exception:
        return "?"


def main():
    status("running")
    pa = "ctrl-%s-前缀A，与B不同。" % TS + FILLER
    pb = "ret-%s-前缀B，与A不同。" % TS + FILLER
    data = {"gap_s": GAP, "model": MODEL}
    log("==== TTL 偏移对照测试（mimo-v2.6-flash, gap=%ds）" % GAP)

    sa, sb = sess_of(pa), sess_of(pb)
    log("A(对照,无retention) sess=%s  B(24h) sess=%s" % (sa, sb))

    data["A1"] = call(pa, False, "A")
    log("A1 写入: cached=%(cached)s pt=%(pt)s" % data["A1"])
    data["B1"] = call(pb, True, "B")
    log("B1 写入: cached=%(cached)s pt=%(pt)s" % data["B1"])

    log("sleep %ds（超过 5 分钟默认 TTL）..." % GAP)
    time.sleep(GAP)

    data["A2"] = call(pa, False, "A")
    log("A2 复查: cached=%(cached)s pt=%(pt)s  ← 期望 0（默认 TTL 已过期）" % data["A2"])
    time.sleep(30)   # 避免 A2 的重写影响判读（不同前缀本就不相干，保险起见）
    data["B2"] = call(pb, True, "B")
    log("B2 复查: cached=%(cached)s pt=%(pt)s  ← 期望 >0（retention 24h 生效）" % data["B2"])

    data["A_sess"], data["B_sess"] = sa, sb
    data["A_acct"], data["B_acct"] = acct_of(sa), acct_of(sb)
    same_pair = (data["A_acct"] == data["A_acct"])  # 同 pair 同账号由 sha 相同保证
    expired = data["A2"]["cached"] == 0
    retained = data["B2"]["cached"] > 0
    hitratio = (data["B2"]["cached"] / data["B2"]["pt"]) if data["B2"].get("pt") else 0
    data["verdict"] = {"A_expired_as_expected": expired,
                       "B_retention_honored": retained,
                       "B2_hit_ratio": round(hitratio, 3)}

    ok = expired and retained
    verdict = ("retention 24h 经 sidecar→litellm→网关 全链路生效"
               if ok else
               ("对照组未过期(TTL>5min?) A2=%d；" % data["A2"]["cached"]) +
               ("实验组未命中 → retention 未被网关执行/被中间层剥离 B2=%d" % data["B2"]["cached"]
                if not retained else ""))
    status("done", ok=ok, reason=verdict, data=data)
    log("==== 结论: %s" % verdict)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        log("测试异常: %s" % e)
        status("done", ok=False, reason="exception: " + str(e)[:200])
        code = 1
    log("退出码 %s" % code)
    sys.exit(code)
