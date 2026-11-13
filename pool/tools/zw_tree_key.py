#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「会话树键」编码成 sidecar 能识别的零宽载荷（与 dsh-plugin/index.js 的 v2 编码同构）。

约定（见 dsh-plugin/index.js encodeTreeKey）：
  载荷 = MAGIC + flag + body + 校验和(4 符号) + 终止哨兵
  MAGIC = U+2060 U+200D U+200B U+200C；字母表 = [U+200B, U+200C, U+200D, U+2060]
  flag 0 = 裸 uuid（32 hex，去横线）→ 每字节 4 符号（MSB 在前）
  flag 1 = session-<uuid>
  校验和 = sum(flag+body 各位) mod 256
JSON 编码后这些字符会变成 \\uXXXX 转义，sidecar 的 _zw_take_escaped 正是按转义形态解码。
"""
MAGIC = "\u2060\u200d\u200b\u200c"
ZW = ["\u200b", "\u200c", "\u200d", "\u2060"]
TERM = "\u2060"


def _digit_byte(byte):
    return [(byte >> 6) & 3, (byte >> 4) & 3, (byte >> 2) & 3, byte & 3]


def encode_tree_key(key):
    """key: 32 位 hex（裸 uuid 去横线）或 session-<uuid>；返回零宽载荷字符串。"""
    k = key.strip()
    flag = 0
    if k.startswith("session-"):
        flag = 1
        k = k[len("session-"):]
    k = k.replace("-", "")
    if len(k) != 32:
        raise ValueError("需要 32 位 hex uuid")
    digits = [flag]
    for i in range(0, 32, 2):
        digits.extend(_digit_byte(int(k[i:i + 2], 16)))
    chk = sum(digits) & 0xFF
    out = MAGIC + "".join(ZW[d] for d in digits)
    out += "".join(ZW[(chk >> s) & 3] for s in (6, 4, 2, 0))
    return out + TERM


if __name__ == "__main__":
    import json
    import sys
    sys.path.insert(0, "/opt/litellm")
    import importlib.util
    spec = importlib.util.spec_from_file_location("sc", "/opt/litellm/session-sidecar.py")
    sc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sc)
    key = "0123456789abcdef0123456789abcdef"
    for k in (key, "session-" + key):
        payload = encode_tree_key(k)
        body = json.dumps({"messages": [{"role": "user", "content": "hi" + payload}]}).encode()
        got = sc.tree_key(body)
        want = k if k.startswith("session-") else key
        print("encode(%s) → 解码=%s  %s" % (k, got, "OK" if got == want else "FAIL"))
