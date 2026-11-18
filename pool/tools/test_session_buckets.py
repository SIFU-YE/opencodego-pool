#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""treepin-s6 回归测试：会话桶（session bucket）与别名降级。

可在任意机器运行（默认在 tools/ 的上一级找 session-sidecar.py）：
    python3 pool/tools/test_session_buckets.py
覆盖：
  1) session_bucket 确定性、值域 1..SESSION_BUCKETS、跨树分散；
  2) _ALIAS_RE 同时匹配旧式 <model>@aN 与桶式 <model>@aNsK；
  3) pick_alias 降级链：
       有桶别名 → 桶别名
       只有旧别名 → 旧别名（服务端未重跑 gen_config）
       config 可读但无该别名 → None（不改写，避免指向不存在的 deployment）
       config 读不到 → 旧别名（保持改动前行为）
  4) 同树可重放、已是别名不改写、无树键不改写。
"""
import importlib.util
import os
import sys
import tempfile

# Windows 控制台默认 cp936，中文断言名会 UnicodeEncodeError → 强制 UTF-8 输出
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
SIDECAR = os.environ.get("SIDECAR_PATH", os.path.join(HERE, "..", "session-sidecar.py"))
SIDECAR = os.path.abspath(SIDECAR)

fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  :: " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


if not os.path.exists(SIDECAR):
    print("SKIP: sidecar not found at %s" % SIDECAR)
    sys.exit(0)

sys.path.insert(0, os.path.dirname(SIDECAR))
spec = importlib.util.spec_from_file_location("sc", SIDECAR)
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

print("sidecar: %s" % SIDECAR)
print("SESSION_BUCKETS = %d" % sc.SESSION_BUCKETS)

K = sc.SESSION_BUCKETS
trees = ["session-%04d" % i for i in range(200)]
buckets = [sc.session_bucket(t) for t in trees]

ok("session_bucket 值域 1..K", all(1 <= b <= K for b in buckets), "min=%d max=%d" % (min(buckets), max(buckets)))
ok("session_bucket 确定性", all(sc.session_bucket(t) == b for t, b in zip(trees, buckets)))
ok("桶有分散（非恒定）", len(set(buckets)) > 1, "distinct=%d" % len(set(buckets)))
if K >= 4:
    ok("桶覆盖接近均匀", len(set(buckets)) >= max(2, K // 2), "distinct=%d/%d" % (len(set(buckets)), K))

# ---- 别名正则：新旧两种形态都要认 ----
cfg_both = "  - model_name: mimo-v2.6-flash@a4s3\n  - model_name: deepseek-v4.1-flash@a5\n"
ok("_ALIAS_RE 两种形态都认",
   set(sc._ALIAS_RE.findall(cfg_both)) == {"mimo-v2.6-flash@a4s3", "deepseek-v4.1-flash@a5"},
   sc._ALIAS_RE.findall(cfg_both))

# ---- pick_alias 降级链 ----
_alias_calls = []


def fake_choose_tag(tree):
    _alias_calls.append(tree)
    return "OCG_KEY_4", "acct:OCG_KEY_4"


_real_choose = sc.choose_tag
sc.choose_tag = fake_choose_tag

t = "session-bucket-test-tree"
b = sc.session_bucket(t)
print("test tree=%s -> bucket=%d" % (t, b))

_tmpfiles = []


def with_config(text, missing=False):
    if missing:
        path = os.path.join(tempfile.gettempdir(), "definitely-missing-%d.yaml" % os.getpid())
        if os.path.exists(path):
            os.remove(path)
    else:
        fh = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        fh.write(text)
        fh.close()
        path = fh.name
        _tmpfiles.append(path)
    sc.AFFINITY_CONFIG_FILE = path
    sc._ALIAS_CACHE["checked"] = 0.0      # 绕过 30s TTL
    sc._ALIAS_CACHE["set"] = set()
    sc._ALIAS_CACHE["ok"] = False
    return path


cfg_bucketed = "  - model_name: mimo-v2.6-flash@a4s%d\n" % b
with_config(cfg_bucketed)
got_bucketed = sc.pick_alias(t, "mimo-v2.6-flash")
ok("有桶别名 → 返回桶别名", got_bucketed == "mimo-v2.6-flash@a4s%d" % b, got_bucketed)

with_config("  - model_name: mimo-v2.6-flash@a4\n")
got_legacy = sc.pick_alias(t, "mimo-v2.6-flash")
ok("只有旧别名 → 退旧别名（安全降级）", got_legacy == "mimo-v2.6-flash@a4", got_legacy)

with_config("  - model_name: other-model@a9s1\n")
got_none = sc.pick_alias(t, "mimo-v2.6-flash")
ok("可读但无该别名 → None（不改写）", got_none is None, got_none)

with_config("", missing=True)
got_unreadable = sc.pick_alias(t, "mimo-v2.6-flash")
ok("config 读不到 → 旧别名（保持改动前行为）", got_unreadable == "mimo-v2.6-flash@a4", got_unreadable)

with_config("")
got_empty = sc.pick_alias(t, "mimo-v2.6-flash")
ok("可读但无任何别名 → None", got_empty is None, got_empty)

with_config(cfg_bucketed)
ok("已是别名则不改写", sc.pick_alias(t, "mimo-v2.6-flash@a4s1") is None)
ok("无树键不改写", sc.pick_alias(None, "mimo-v2.6-flash") is None)
ok("同树可重放（两次同值）", sc.pick_alias(t, "mimo-v2.6-flash") == got_bucketed)
ok("选号被调用（树键参与）", _alias_calls.count(t) >= 1, len(_alias_calls))

sc.choose_tag = _real_choose
for p in _tmpfiles:
    try:
        os.remove(p)
    except OSError:
        pass

print()
if fails:
    print("FAILED %d: %s" % (len(fails), fails))
    sys.exit(1)
print("ALL PASS")
