#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐账号测延迟：用池内别名 @a2/@a4/@a5/@a6 把请求钉到指定账号，各测 2 次。
目的：判断「mimo 流静默」是否与具体账号相关（树键钉号会把会话钉在一个账号上）。
"""
import json
import sys
import time
import urllib.request

master = ""
for line in open("/opt/litellm/.env", encoding="utf-8"):
    if line.startswith("LITELLM_MASTER_KEY="):
        master = line.split("=", 1)[1].strip().strip('"')

tools = [{
    "type": "function",
    "function": {"name": "get_weather", "description": "查天气",
                 "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
}]

def one(alias):
    body = {
        "model": alias, "stream": True, "max_tokens": 512,
        "messages": [{"role": "user", "content": "查北京天气，必须调用工具。"}],
        "tools": tools,
    }
    req = urllib.request.Request("http://127.0.0.1:4001/v1/chat/completions",
                                data=json.dumps(body).encode(),
                                headers={"Authorization": "Bearer " + master,
                                         "Content-Type": "application/json",
                                         "Accept": "text/event-stream"}, method="POST")
    t0 = time.time()
    frames = 0
    last = 0.0
    maxgap = 0.0
    done = False
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            for raw in r:
                now = time.time() - t0
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data: "):
                    frames += 1
                    gap = now - last
                    if gap > maxgap:
                        maxgap = gap
                    last = now
                    if line == "data: [DONE]":
                        done = True
                        break
                if now > 115:
                    break
    except Exception as e:
        return f"{alias:<24} 异常@{time.time()-t0:6.1f}s {type(e).__name__} {str(e)[:60]}"
    return f"{alias:<24} 耗时={time.time()-t0:6.1f}s 帧={frames:<3} 最大空档={maxgap:5.1f}s [DONE]={'Y' if done else 'N'}"

for alias in ["mimo-v2.6-flash@a2", "mimo-v2.6-flash@a4", "mimo-v2.6-flash@a5", "mimo-v2.6-flash@a6"]:
    for i in (1, 2):
        print(f"  [{i}] {one(alias)}")
