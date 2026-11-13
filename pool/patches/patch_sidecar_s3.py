#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S3 补丁：会话树键/会话锚同时覆盖 Chat Completions 与 Responses 两种协议。

背景（实测）
    生产号池的 session_id 注入与本次新增的树键逻辑，都被写在
        if method == "POST" and body and "chat/completions" in self.path:
    的守卫里。但 DSH 的 llm-pi-ai provider 配置是 api: openai-responses，
    其流量全部走 POST /v1/responses —— 于是：
      · 既有：DSH 流量一直没有会话亲和（每次请求都是新锚）
      · 本次：树键标记不会被读取，钉号不生效
    本补丁把守卫扩展到两种协议，并让「首条内容哈希」锚支持 Responses 的 input 字段。

回滚
    cp -p /opt/litellm/session-sidecar.py.bak-treepin-s3-* /opt/litellm/session-sidecar.py
    systemctl restart litellm-session-sidecar
"""
import shutil
import sys
import time

PATH = "/opt/litellm/session-sidecar.py"
MARK = "treepin-s3"

OLD_GUARD = '''            if method == "POST" and body and "chat/completions" in self.path:'''
NEW_GUARD = '''            # treepin-s3：DSH 走 /v1/responses（provider api=openai-responses），
            # 会话锚与树键两类逻辑都必须覆盖两种协议，否则对 DSH 流量完全失效。
            if method == "POST" and body and ("chat/completions" in self.path or "/responses" in self.path):'''

OLD_HASH = '''        h = _first_text_hash(d.get("messages"))'''
NEW_HASH = '''        h = _first_text_hash_any(d)                  # treepin-s3：兼容 Responses 的 input'''

OLD_ANCHOR = '''def baseline_session(body_bytes, inbound_sid=None):'''
NEW_ANCHOR = '''def _first_text_hash_any(d):
    """首条内容哈希：优先 messages（Chat Completions），其次 input（Responses，可为字符串或 item 列表）。"""
    msgs = d.get("messages")
    if msgs:
        return _first_text_hash(msgs)
    inp = d.get("input")
    if isinstance(inp, str):
        return hashlib.sha256(inp[:4096].encode("utf-8", "ignore")).hexdigest()
    if isinstance(inp, list):
        return _first_text_hash(inp)
    return None


def baseline_session(body_bytes, inbound_sid=None):'''


def main():
    src = open(PATH, encoding="utf-8").read()
    if MARK in src:
        print("ALREADY-PATCHED (%s)" % MARK)
        return 0
    for name, anchor in (("guard", OLD_GUARD), ("hash", OLD_HASH), ("anchor", OLD_ANCHOR)):
        n = src.count(anchor)
        if n != 1:
            print("ABORT: anchor %s found %d times (expected 1)" % (name, n))
            return 2
    bak = "%s.bak-treepin-s3-%s" % (PATH, time.strftime("%Y%m%d-%H%M"))
    shutil.copy2(PATH, bak)
    out = src.replace(OLD_GUARD, NEW_GUARD, 1).replace(OLD_HASH, NEW_HASH, 1).replace(OLD_ANCHOR, NEW_ANCHOR, 1)
    with open(PATH, "w", encoding="utf-8") as fh:
        fh.write(out)
    import py_compile
    try:
        py_compile.compile(PATH, doraise=True)
    except Exception as e:
        shutil.copy2(bak, PATH)
        print("py_compile FAILED -> rolled back: %s" % e)
        return 4
    print("PATCHED %s" % PATH)
    print("backup  %s" % bak)
    print("py_compile OK | mark count %d" % out.count(MARK))
    return 0


if __name__ == "__main__":
    sys.exit(main())
