#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""de-risk：换 x-opencode-session 后，回放上一轮的 reasoning_content 会不会被上游 400？

做法：第 1 轮用 session A 生成（收集 reasoning_content + content）；
     第 2 轮用 **session B** 把上一轮 assistant 消息（含 reasoning_content）原样回放。
判据：200 = 思考内容与 session 无关（可以安全轮换）；400 = 绑 session（只能每树一串）。
"""
import json
import os
import sys
import time

import sse_probe as P

KEY = os.environ["OCG_KEY_6"]
MODEL = "deepseek-v4.1-flash"


def call(session, messages, max_tokens=400, effort="max"):
    conn = P.TlsConn(P.tunnel(P.UP_HOST, 443), 200)
    body = {
        "model": MODEL,
        "stream": True,
        "max_tokens": max_tokens,
        "messages": messages,
        "reasoning_effort": effort,
    }
    hdrs = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": "Bearer " + KEY,
        "User-Agent": "dsh-opencodego-pool/1.0",
        "x-opencode-session": session,
    }
    conn.request("POST", P.UP_PATH, body=json.dumps(body).encode(), headers=hdrs)
    resp = conn.getresponse()
    status = resp.status
    content, reasoning = [], []
    buf = b""
    while True:
        c = resp.read1(65536)
        if not c:
            break
        buf += c
        while True:
            i = buf.find(b"\n\n")
            if i < 0:
                break
            raw, buf = buf[:i], buf[i + 2:]
            for line in raw.split(b"\n"):
                if not line.startswith(b"data:"):
                    continue
                p = line[5:].strip()
                if p == b"[DONE]" or not p:
                    continue
                try:
                    d = json.loads(p)
                except Exception:
                    continue
                ch = (d.get("choices") or [{}])[0]
                dl = ch.get("delta") or {}
                if dl.get("content"):
                    content.append(dl["content"])
                if dl.get("reasoning_content"):
                    reasoning.append(dl["reasoning_content"])
    err = b"" if status == 200 else resp.read()[:400]
    conn.close()
    return status, "".join(content), "".join(reasoning), err


def main():
    a = "probe-rs-A-" + time.strftime("%H%M%S")
    b = "probe-rs-B-" + time.strftime("%H%M%S")
    q1 = "用一句话说明 TCP 与 UDP 的核心区别。"
    st1, c1, r1, e1 = call(a, [{"role": "user", "content": q1}], max_tokens=300)
    print("round1 session=%s status=%s content=%dch reasoning=%dch" % (a, st1, len(c1), len(r1)))
    if st1 != 200:
        print("round1 失败:", e1[:300]); return
    if not r1:
        print("WARN: 本轮没有 reasoning_content（模型未思考），结论只能部分成立")

    asst = {"role": "assistant", "content": c1 or None, "reasoning_content": r1}
    msgs = [{"role": "user", "content": q1}, asst,
            {"role": "user", "content": "把它压缩成 20 字以内。"}]
    st2, c2, r2, e2 = call(b, msgs, max_tokens=300)
    print("round2 session=%s（已换）status=%s content=%dch" % (b, st2, len(c2)))
    if st2 != 200:
        print("round2 400/错误体:", e2[:500])
        print(">>> 结论：思考内容回放**与 session 绑定**，不能跨 session 轮换")
    else:
        print("round2 ok 正文前 60 字:", c2[:60])
        print(">>> 结论：换 session 后回放 reasonng 正常，轮换安全（至少对 deepseek-v4.1-flash）")


if __name__ == "__main__":
    main()
