#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""树键解码（零宽载荷 + 旧可见字面量）的服务端单元测试 —— 不依赖网络、不依赖号池运行时文件。

覆盖 treepin-s5 的三组请求体形态（同一棵树必须还原出同一个 tree key）：
  1. 原始 UTF-8 字节形态：body 里就是零宽字符的 UTF-8 字节（E2 80 8B …）
  2. JSON \\uXXXX 转义形态：body 里是 ASCII 的 "\\u200b" 之类（上游 ensure_ascii=True 时）
  3. 旧的可见字面量：[pool-tree: <id>]（向后兼容，老会话/回滚期间照旧生效）

用法：
  python3 pool/tools/test_session_tree_key.py
  SIDECAR_PATH=/opt/litellm/session-sidecar.py python3 pool/tools/test_session_tree_key.py
"""
import importlib.util
import json
import os
import sys

# 可移植定位：默认用仓库内的 sidecar（tools/ 的上一级）；服务器上可用 SIDECAR_PATH 覆盖
HERE = os.path.dirname(os.path.abspath(__file__))
SIDECAR = os.path.abspath(os.environ.get("SIDECAR_PATH") or os.path.join(HERE, "..", "session-sidecar.py"))
if not os.path.exists(SIDECAR) and os.path.exists("/opt/litellm/session-sidecar.py"):
    SIDECAR = "/opt/litellm/session-sidecar.py"
sys.path.insert(0, os.path.dirname(SIDECAR))     # sidecar 依赖同目录的 toolrepair
spec = importlib.util.spec_from_file_location("sc", SIDECAR)
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

passed = failed = 0


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} {extra}")


# ---------------------------------------------------------------- 编码器（按 spec 独立实现）
# 刻意不复用 sidecar 里的解码代码：这是「另一个实现」，用来交叉验证格式约定。
ZW = ["\u200b", "\u200c", "\u200d", "\u2060"]
MAGIC = "\u2060\u200d\u200b\u200c"
UUID_SHAPE = "-"


def _digits(byte):
    """一个字节 → 4 个符号值（MSB 在前），与 JS 侧 digitByte() 一致。"""
    return [(byte >> 6) & 3, (byte >> 4) & 3, (byte >> 2) & 3, byte & 3]


def encode_tree_key(key):
    """key → 零宽载荷（与 DSH 插件 index.js 的 encodeTreeKey 同格式）。"""
    body = key
    if key.startswith("session-") and len(key) == 44 and key[8:].count("-") == 4:
        flag, body = 1, key[8:].replace("-", "")
    elif len(key) == 36 and key.count("-") == 4:
        flag, body = 0, key.replace("-", "")
    else:
        flag = 2
    digits = [flag]
    if flag == 2:
        raw = body.encode("utf-8")
        digits += _digits(len(raw))
        for byte in raw:
            digits += _digits(byte)
    else:
        for i in range(0, len(body), 2):
            digits += _digits(int(body[i:i + 2], 16))
    out = MAGIC + "".join(ZW[d] for d in digits)
    return out + "".join(ZW[d] for d in _digits(sum(digits) & 0xFF)) + ZW[3]   # 尾部终止哨兵 U+2060


def body_raw(key, text="ping"):
    """形态 1：原始 UTF-8 字节（DSH 默认走这条：JSON.stringify 不转义零宽字符）。"""
    return json.dumps({"model": "deepseek-v4.1-flash",
                       "messages": [{"role": "user", "content": text + encode_tree_key(key)}]},
                      ensure_ascii=False).encode("utf-8")


def body_escaped(key, text="ping"):
    """形态 2：\\uXXXX 转义（上游序列化器 ensure_ascii=True）。"""
    return json.dumps({"model": "deepseek-v4.1-flash",
                       "messages": [{"role": "user", "content": text + encode_tree_key(key)}]},
                      ensure_ascii=True).encode("ascii")


def body_escaped_upper(key, text="ping"):
    """形态 2b：大写十六进制转义（部分序列化器把 \\u200b 写成 \\u200B）。"""
    import re as _re
    body = body_escaped(key, text)
    return _re.sub(rb"\\u([0-9a-f]{4})", lambda m: b"\\u" + m.group(1).upper(), body)


def body_legacy(key, text="ping"):
    """形态 3：旧的可见字面量（v1 插件写的；也已存在于老会话历史里）。"""
    return json.dumps({"model": "deepseek-v4.1-flash",
                       "messages": [{"role": "user", "content": f"{text} [pool-tree: {key}]"}]},
                      ensure_ascii=False).encode("utf-8")


LEADER = "session-4e7011c7-2246-4e22-8f91-3ca4f93e6295"
BARE = "58822bbf-3aa3-463a-8933-91506896885d"
OTHER = "unit-tree-9"

print("— 三组形态必须还原出同一个 tree key —")
for key in (LEADER, BARE, OTHER):
    for name, make in (("原始 UTF-8 字节", body_raw),
                       ("\\uXXXX 转义", body_escaped),
                       ("旧可见字面量", body_legacy)):
        body = make(key)
        got = sc.tree_key(body)
        ok(f"{name} → {key[:22]}…", got == key, f"got={got!r}")

print("— 同一棵树，三种形态结果一致 —")
keys = [sc.tree_key(make(LEADER)) for make in (body_raw, body_escaped, body_legacy)]
ok("三种形态 tree key 完全一致", keys == [LEADER, LEADER, LEADER], str(keys))

print("— 载荷必须真的不可见（body 里没有可读标记）—")
raw = body_raw(LEADER)
esc = body_escaped(LEADER)
ok("原始形态 body 里没有 pool-tree 字样", b"pool-tree" not in raw)
ok("转义形态 body 里没有 pool-tree 字样", b"pool-tree" not in esc)
ok("原始形态 body 里有零宽 MAGIC 的 UTF-8 字节",
   sc._ZW_MAGIC_BYTES in raw, raw[raw.find(b"ping"):raw.find(b"ping") + 24].hex())
ok("转义形态 body 里是 ASCII 的 \\u2060 序列", b"\\u2060\\u200d\\u200b\\u200c" in esc.lower())

print("— 边界与 fail-open —")
ok("无标记 body → None", sc.tree_key(b'{"messages":[{"role":"user","content":"hi"}]}') is None)
ok("空 body → None", sc.tree_key(b"") is None)
ok("None → None", sc.tree_key(None) is None)
ok("坏 JSON 但含载荷 → 仍能解出（纯字节级）", sc.tree_key(b"{not json" + body_raw(LEADER)[1:]) == LEADER)
ok("截断的载荷 → None", sc.tree_key(b'{"content":"' + encode_tree_key(LEADER)[:-8].encode("utf-8") + b'"}') is None)
ok("旧字面量少于 4 字符 → None", sc.tree_key(b"[pool-tree: abc]") is None)
ok("旧字面量非法字符 → None", sc.tree_key(b"[pool-tree: bad!char]") is None)
ok("大写十六进制转义也能解", sc.tree_key(body_escaped_upper(LEADER)) == LEADER,
   str(sc.tree_key(body_escaped_upper(LEADER))))
_bad = encode_tree_key(LEADER)
_bad_flag3 = (_bad[:4] + ZW[3] + _bad[5:])          # flag 换成保留位 3
ok("flag=3（保留位）→ 回退到 None",
   sc.tree_key(json.dumps({"c": _bad_flag3}, ensure_ascii=False).encode("utf-8")) is None)
ok("只有零宽字符但没有 MAGIC → None",
   sc.tree_key(json.dumps({"c": ZW[0] * 80}, ensure_ascii=False).encode("utf-8")) is None)
_both = json.dumps({"model": "deepseek-v4.1-flash",
                    "messages": [{"role": "user",
                                  "content": "ping [pool-tree: stale-old-9] " + encode_tree_key(LEADER)}]},
                   ensure_ascii=False).encode("utf-8")
ok("旧字面量与零宽载荷同时存在时，取新的零宽载荷", sc.tree_key(_both) == LEADER,
   str(sc.tree_key(_both)))

print("— 回归：载荷内部碰巧出现 MAGIC（≈0.4%/窗口）也不能解错 —")
_tricky = None
for _i in range(5000):
    _k = "unit-tree-%d" % _i
    _e = encode_tree_key(_k)
    if _e.find(MAGIC, 1) >= 0:
        _tricky = (_k, _e)
        break
ok("构造出「载荷内部含 MAGIC」的样本", _tricky is not None)
if _tricky:
    _k, _e = _tricky
    ok("内部含 MAGIC 时仍解回同一键",
       sc.tree_key(json.dumps({"c": "正文" + _e}, ensure_ascii=False).encode("utf-8")) == _k)
    ok("内部含 MAGIC 时（转义形态）仍解回同一键",
       sc.tree_key(json.dumps({"c": "正文" + _e}, ensure_ascii=True).encode("ascii")) == _k)
_chk = encode_tree_key(LEADER)
ok("校验和：翻掉一个符号 → None（不产生假树键）",
   sc.tree_key(json.dumps({"c": _chk[:10] + (ZW[1] if _chk[10] == ZW[0] else ZW[0]) + _chk[11:]},
                          ensure_ascii=False).encode("utf-8")) is None)
ok("校验和：截断尾部 → None",
   sc.tree_key(json.dumps({"c": _chk[:-4]}, ensure_ascii=False).encode("utf-8")) is None)
ok("尾部终止哨兵缺失也能解（解码器容忍，便于前后兼容）",
   sc.tree_key(json.dumps({"c": encode_tree_key(LEADER)[:-1]}, ensure_ascii=False).encode("utf-8")) == LEADER)
ok("载荷末尾字符是 U+2060（非空白，trim 吃不掉）", encode_tree_key(LEADER).endswith("\u2060"))

print("— 与 DSH 侧行为一致：载荷后面还有 JSON 尾巴也能读（自描述长度）—")
p = encode_tree_key(OTHER)
ok("raw 形态尾巴不干扰", sc.tree_key(('{"c":"' + p + '"}]}').encode("utf-8")) == OTHER)
ok("escaped 形态尾巴不干扰",
   sc.tree_key(('{"c":"' + json.dumps(p)[1:-1] + '"}]}').encode("ascii")) == OTHER)

print("— 会话归一链路仍以树键优先（不漏到 nginx 随机号）—")
parsed, sess = sc.baseline_session(body_raw(LEADER), inbound_sid="ext-" + "a" * 32)
ok("baseline_session 用新编码得到树键", sess == LEADER, str(sess))
ok("metadata.session_id 已注入", (parsed or {}).get("metadata", {}).get("session_id") == LEADER)
out, sess2 = sc.process_request(body_escaped(LEADER), inbound_sid="ext-" + "b" * 32)
ok("process_request 用转义形态得到树键", sess2 == LEADER, str(sess2))
ok("硬约束：messages 语义未被改动（转义形态 → 输出为原始 UTF-8）",
   json.loads(out)["messages"] == json.loads(body_escaped(LEADER))["messages"],
   f"{json.loads(body_escaped(LEADER))['messages']} vs {json.loads(out)['messages'] if out else None}")
out1, sess1 = sc.process_request(body_legacy(LEADER), inbound_sid="ext-" + "d" * 32)
ok("process_request 用旧字面量形态得到同一树键", sess1 == LEADER, str(sess1))
ok("硬约束：messages 语义未被改动（旧可见字面量形态）",
   json.loads(out1)["messages"] == json.loads(body_legacy(LEADER))["messages"])
ok("旧可见字面量在输出里逐字保留（解码器不改动 messages）",
   b"[pool-tree: " + LEADER.encode("ascii") + b"]" in (out1 or b""))
out2, sess3 = sc.process_request(body_raw(LEADER), inbound_sid="ext-" + "c" * 32)
ok("process_request 用零宽形态得到树键", sess3 == LEADER, str(sess3))
ok("硬约束：messages 语义未被改动（零宽形态，零宽字符原样保留）",
   json.loads(out2)["messages"] == json.loads(body_raw(LEADER))["messages"])

print(f"\ntree-key: pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
