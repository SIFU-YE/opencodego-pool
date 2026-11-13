#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sidecar 新代码的服务端单元测试：树键解析、选号、会话归一与「messages 字节不动」硬约束。

用法：python3 /tmp/test_sidecar_units.py     （直接导入 /opt/litellm/session-sidecar.py）
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


print("— 树键解析 tree_key —")
cases = [
    (b'{"messages":[{"role":"user","content":"hi [pool-tree: abc12345]"}]}', "abc12345"),
    (b'{"input":"[pool-tree: session-4e7011c7-2246-4e22-8f91-3ca4f93e6295]"}',
     "session-4e7011c7-2246-4e22-8f91-3ca4f93e6295"),
    (b'[pool-tree:   spaced-id-1]', "spaced-id-1"),
    (b'[pool-tree: abc]', None),            # 少于 4 字符
    (b'[pool-tree: bad!char]', None),       # 非法字符
    (b'[pool-tree: ' + b'a' * 80 + b']', None),  # 超过 64 字符
    (b'{"messages":[{"role":"user","content":"no marker here"}]}', None),
    ('{"messages":[{"role":"user","content":"中文 [pool-tree: uni-1234]"}]}'.encode("utf-8"), "uni-1234"),
]
for body, want in cases:
    got = sc.tree_key(body)
    ok(f"tree_key({body[:44]!r}) == {want!r}", got == want, f"got={got!r}")

print("— 确定性加权选号 weighted_pick —")
acc = [("A", 0.4), ("B", 0.35), ("C", 0.25)]
picks = [sc.weighted_pick("tree-x", acc) for _ in range(5)]
ok("同一树键反复选号结果一致", len(set(picks)) == 1, str(picks))
spread = {sc.weighted_pick(f"tree-{i}", acc) for i in range(60)}
ok("不同树键能分散到多个账号", len(spread) >= 2, str(sorted(spread)))
ok("空账号表返回 None", sc.weighted_pick("t", []) is None)
ok("权重全 0 返回 None", sc.weighted_pick("t", [("A", 0.0)]) is None)
ok("单账号必中该账号", sc.weighted_pick("t", [("ONLY", 1.0)]) == "ONLY")
ok("无树键返回 None", sc.weighted_pick(None, acc) is None)
ok("选号结果必在候选内", all(p in {"A", "B", "C"} for p in spread))

print("— 份额与控制文件 affinity_state —")
st = sc.affinity_state()
ok("返回 mode/accounts/reason", set(["mode", "accounts", "reason"]).issubset(st.keys()), str(st.keys()))
ok("mode 取值合法", st["mode"] in ("shadow", "full"), st["mode"])
ok("账号只含健康且在配置内的 OCG_KEY_N",
   all(k.startswith("OCG_KEY_") for k, _ in st["accounts"]), str(st["accounts"]))
ok("reason 可判读（别名覆盖度或交集信息）",
   ("alias-groups:" in st["reason"]) or ("config-intersect" in st["reason"])
   or st["reason"].startswith("ok/")
   # 非号池环境（无 routing-share.json / pool-affinity.json / config.yaml）时，代码应
   # fail-open 并给出可判读的降级原因——这也算"可判读"，不能因此判失败。
   or ("control-missing" in st["reason"]) or ("share-error" in st["reason"])
   or st["reason"].startswith("err:"), st["reason"])

print("— 别名机制（替代标签路由）—")
try:
    cfg_path = os.environ.get("POOL_CONFIG") or os.path.join(os.path.dirname(SIDECAR), "config.yaml")
    if not os.path.exists(cfg_path):
        cfg_path = "/opt/litellm/config.yaml"
    cfg = open(cfg_path, encoding="utf-8").read()
    # sidecar 模块内部固定读 /opt/litellm/config.yaml；可移植运行时把它指到解析出的路径
    sc.AFFINITY_CONFIG_FILE = cfg_path
    sc._ALIAS_CACHE["checked"] = 0.0
    sc._ALIAS_CACHE["set"] = set()
    sc._ALIAS_CACHE["ok"] = False
    ok("已移除 enable_tag_filtering（标签机制废弃）", "enable_tag_filtering" not in cfg)
    ok("已移除 acct: 标签", "acct:OCG_KEY_" not in cfg)
    aliases = sc.alias_set()
    ok("别名集合非空且形如 <model>@aN", len(aliases["set"]) > 0
       and all("@" in a for a in aliases["set"]), aliases["reason"])
    _t = "unit-tree-alias"
    # 选号依赖运行时文件（routing-share.json / pool-affinity.json）；单测里以确定性桩替代，
    # 只验证「树键 → 池内别名」这段纯逻辑，避免测试依赖服务器运行时状态。
    _real_choose = sc.choose_tag
    sc.choose_tag = lambda tree: ("OCG_KEY_5", "acct:OCG_KEY_5")
    try:
        _a = sc.pick_alias(_t, "mimo-v2.6-flash")
        ok("pick_alias 返回池内别名", bool(_a) and _a.startswith("mimo-v2.6-flash@a"), str(_a))
        ok("pick_alias 幂等（已是别名则不改写）", sc.pick_alias(_t, "mimo-v2.6-flash@a2") is None)
        ok("pick_alias 对未知模型返回 None", sc.pick_alias(_t, "no-such-model") is None)
    finally:
        sc.choose_tag = _real_choose
except FileNotFoundError:
    print("  SKIP  非号池环境（无 config.yaml）")

print("— 会话归一 baseline_session / process_request —")
body = json.dumps({
    "model": "mimo-v2.6-flash",
    "messages": [{"role": "user", "content": "你好 [pool-tree: unit-tree-9]"}],
}).encode("utf-8")
parsed, sess = sc.baseline_session(body, inbound_sid="ext-" + "a" * 32)
ok("树键优先于 nginx 随机号", sess == "unit-tree-9", str(sess))
ok("metadata.pool_src 已注入", (parsed or {}).get("metadata", {}).get("pool_src") == "sidecar")
ok("metadata.session_id 已注入", (parsed or {}).get("metadata", {}).get("session_id") == "unit-tree-9")

out, sess2 = sc.process_request(body, inbound_sid="ext-" + "b" * 32)
before = json.loads(body)["messages"]
after = json.loads(out)["messages"]
ok("硬约束：messages 字节未被改动", json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True))
ok("process_request 返回树键", sess2 == "unit-tree-9", str(sess2))

# Responses 形态（input 而非 messages）
rbody = json.dumps({"model": "deepseek-v4.1-flash", "input": "ping"}).encode("utf-8")
p2, s2 = sc.baseline_session(rbody, inbound_sid="ext-" + "c" * 32)
ok("Responses 形态也能得到稳定锚（hash）", bool(s2) and s2.startswith("ext-"), str(s2))
ok("Responses 形态不报错", p2 is not None)

# 坏 JSON → 原样透传
o3, s3 = sc.process_request(b"{not json", inbound_sid="x")
ok("坏 JSON：fail-open 原样透传", o3 is None and s3 is None)

print(f"\nunits: pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
