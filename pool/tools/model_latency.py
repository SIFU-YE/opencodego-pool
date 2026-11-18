#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型延迟对比：mimo-flash / mimo-pro / dsv4.1-flash / muse，各 2 次（流式 + tools）。"""
import json
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

def one(model, path="/v1/chat/completions"):
    if path.endswith("responses"):
        body = {"model": model, "stream": True, "max_output_tokens": 512,
                "input": "查北京天气，必须调用工具。",
                "tools": [{"type": "function", "name": "get_weather", "description": "查天气",
                           "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]}
    else:
        body = {"model": model, "stream": True, "max_tokens": 512,
                "messages": [{"role": "user", "content": "查北京天气，必须调用工具。"}], "tools": tools}
    req = urllib.request.Request("http://127.0.0.1:4001" + path, data=json.dumps(body).encode(),
                                headers={"Authorization": "Bearer " + master,
                                         "Content-Type": "application/json",
                                         "Accept": "text/event-stream"}, method="POST")
    t0 = time.time()
    frames = 0
    last = 0.0
    maxgap = 0.0
    done = False
    try:
        with urllib.request.urlopen(req, timeout=150) as r:
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
                if now > 145:
                    break
    except Exception as e:
        return f"{model:<28} 异常@{time.time()-t0:6.1f}s {type(e).__name__} {str(e)[:50]}"
    return f"{model:<28} 耗时={time.time()-t0:6.1f}s 帧={frames:<3} 最大空档={maxgap:5.1f}s [DONE]={'Y' if done else 'N'}"

for m, p in [("mimo-v2.6-flash", "/v1/chat/completions"),
             ("mimo-v2.6-pro", "/v1/chat/completions"),
             ("deepseek-v4.1-flash", "/v1/chat/completions"),
             ("muse-spark-1.3-contributor", "/v1/responses")]:
    for i in (1, 2):
        print(f"  [{i}] {one(m, p)}")
