#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""treepin-s6b 粘性钉号回归：同树同账号（rebalance 不漂移）。

背景：share-engine 每轮 rebalance 会改写各账号权重；weighted_pick 对同一棵树会算出
不同账号 → 同一会话中途换号。上游 thinking 模型的 encrypted reasoning 与签发 key 绑定，
换号后回放旧 key 签发的内容直接 400（not issued to this caller），且此后每轮都 400。
修复：树一旦钉住某账号就不再漂移（sticky）；只有钉住的号不可用（被剔除/禁用/别名缺失）
时才允许重选。新树仍按当前权重分布（负载均衡不受影响）。

可任意机器运行：SIDECAR 环境变量指向被测的 session-sidecar.py（默认 tools/ 上一级）。
"""
import importlib.util
import os
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
SIDECAR = os.environ.get("SIDECAR", os.path.join(HERE, "..", "session-sidecar.py"))
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
spec = importlib.util.spec_from_file_location("sc_sticky", SIDECAR)
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

print("sidecar: %s" % SIDECAR)

A = "OCG_KEY_4"
B = "OCG_KEY_6"
T = "session-sticky-test-tree-001"


def fresh_state(pin_file=None):
    """隔离 _PIN 单例；affinity state 用可控桩。"""
    sc._PIN["loaded"] = True
    sc._PIN["map"] = {}
    if pin_file is None:
        fd, pin_file = tempfile.mkstemp(prefix="pins-", suffix=".json")
        os.close(fd)
        os.remove(pin_file)
    sc.PIN_FILE = pin_file
    return pin_file


_ORIG = (sc.affinity_state, sc.disabled_slots, sc.alias_set)


def with_affinity(accounts, disabled=None, known=None, ok=True):
    sc.affinity_state = lambda: {"mode": "full", "reason": "t", "accounts": list(accounts)}
    sc.disabled_slots = lambda: set(disabled or [])
    sc.alias_set = lambda: {"ok": ok, "set": set(known or []), "reason": "t"}
    return _ORIG


def restore_affinity(saved):
    sc.affinity_state, sc.disabled_slots, sc.alias_set = saved[0], saved[1], saved[2]


def alias_for(acct, model="deepseek-v4.1-flash"):
    return "%s@a%ss%d" % (model, acct.lstrip("OCG_KEY_"), sc.session_bucket(T))


# 1) 新树无钉选 → sticky None；resolve 走加权
pf = fresh_state()
saved = with_affinity([(A, 1), (B, 100)], known={alias_for("4"), alias_for("6")})
try:
    assert sc.sticky_account(T, "deepseek-v4.1-flash") is None
    ok("新树无钉选", True)
    k, a, s = sc.resolve_pin(T, "deepseek-v4.1-flash")
    ok("无钉选时回退加权且有别名", k in (A, B) and (a or "").startswith("deepseek-v4.1-flash@a"), "%s %s" % (k, a))
    assert s is False
    ok("回退标记 sticky=False", True)
finally:
    restore_affinity(saved)

# 2) 钉选后即使加权会选别人，也沿用旧钉（核心：rebalance 不漂移）
pf = fresh_state()
saved = with_affinity([(A, 1), (B, 100)], known={alias_for("4"), alias_for("6")})
try:
    w = sc.weighted_pick(T, [(A, 1), (B, 100)])
    other = B if w == A else A
    sc._pins_remember(T, other)
    got = sc.sticky_account(T, "deepseek-v4.1-flash")
    ok("钉选优先于加权（rebalance 不漂移）", got == other, "pinned=%s weighted=%s" % (other, w))
    k, a, s = sc.resolve_pin(T, "deepseek-v4.1-flash")
    exp = "deepseek-v4.1-flash@a%ss%d" % (other.lstrip("OCG_KEY_"), sc.session_bucket(T))
    ok("resolve_pin 返回钉选别名 + sticky 标记", k == other and a == exp and s is True, "%s %s" % (k, a))
finally:
    restore_affinity(saved)

# 3) 钉住的号被剔除（不在健康集合）→ 放行重选
pf = fresh_state()
saved = with_affinity([(B, 100)], known={alias_for("6")})  # A 已剔除
try:
    sc._pins_remember(T, A)
    ok("被剔除则不沿用", sc.sticky_account(T, "deepseek-v4.1-flash") is None)
    k, a, s = sc.resolve_pin(T, "deepseek-v4.1-flash")
    ok("被剔除后回退加权", k == B and a == alias_for("6") and s is False, "%s %s" % (k, a))
finally:
    restore_affinity(saved)

# 4) 钉住的槽位被禁用 → 放行重选
pf = fresh_state()
saved = with_affinity([(A, 1), (B, 100)], disabled={4}, known={alias_for("4"), alias_for("6")})
try:
    sc._pins_remember(T, A)
    ok("被禁用则不沿用", sc.sticky_account(T, "deepseek-v4.1-flash") is None)
finally:
    restore_affinity(saved)

# 5) 别名缺失（该 model 在该号上无别名）→ 放行重选，不指到不存在的 deployment
pf = fresh_state()
saved = with_affinity([(A, 1)], known=set())  # 空集合：无任何可用别名
try:
    sc._pins_remember(T, A)
    ok("别名缺失不沿用（防指到不存在的 deployment）", sc.sticky_account(T, "deepseek-v4.1-flash") is None)
finally:
    restore_affinity(saved)

# 6) 持久化：重置内存后仍能从文件读回
pf = fresh_state()
saved = with_affinity([(A, 1)], known={alias_for("4")})
try:
    sc._pins_remember(T, A)
    assert os.path.isfile(pf)
    sc._PIN["loaded"] = False
    sc._PIN["map"] = {}
    got = sc.sticky_account(T, "deepseek-v4.1-flash")
    ok("重启/重载后钉选仍在（文件持久化）", got == A, got)
finally:
    restore_affinity(saved)

# 7) 损坏的归属文件 → 不抛错，当无钉选处理
import json as _json
bad = pf + ".bad"
with open(bad, "w", encoding="utf-8") as fh:
    fh.write("{not json")
_saved_pin, sc.PIN_FILE = sc.PIN_FILE, bad
sc._PIN["loaded"] = False
sc._PIN["map"] = {}
saved = with_affinity([(A, 1)], known={alias_for("4")})
try:
    ok("损坏的归属文件不抛错", sc.sticky_account(T, "deepseek-v4.1-flash") is None)
finally:
    restore_affinity(saved)
    sc.PIN_FILE = _saved_pin
    sc._PIN["loaded"] = False
    sc._PIN["map"] = {}
    try:
        os.remove(bad)
    except OSError:
        pass

# 8) seed：从最近决策日志预填（部署瞬间不产生全量重选）
pf = fresh_state()
dec = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
dec.write(_json.dumps({"tree": "session-seed-1", "model": "m1", "key_want": "OCG_KEY_7", "mode": "full"}) + "\n")
dec.write(_json.dumps({"tree": "session-seed-1", "model": "m1", "key_want": "OCG_KEY_8", "mode": "full"}) + "\n")
dec.write(_json.dumps({"tree": "other", "model": "m1", "key_want": "OCG_KEY_9", "mode": "shadow"}) + "\n")
dec.close()
_saved_dec = sc.AFFINITY_DECISIONS_FILE
sc.AFFINITY_DECISIONS_FILE = dec.name
sc._PIN["loaded"] = False
sc._PIN["map"] = {}
seed_bucket = sc.session_bucket("session-seed-1")
saved = with_affinity([("OCG_KEY_8", 1)], known={"m1@a8s%d" % seed_bucket})
try:
    got = sc.sticky_account("session-seed-1", "m1")
    ok("seed 取最后一次 full 决策", got == "OCG_KEY_8", got)
    got2 = sc.sticky_account("other", "m1")
    ok("shadow 模式决策不参与预填", got2 is None, got2)
finally:
    restore_affinity(saved)
    sc.AFFINITY_DECISIONS_FILE = _saved_dec
    sc._PIN["loaded"] = False
    sc._PIN["map"] = {}
    os.remove(dec.name)

# 9) 别名格式护栏：已是别名的 model / 非法 key 不改写不记录
pf = fresh_state()
saved = with_affinity([(A, 1)], known={alias_for("4")})
try:
    ok("已是别名不改写", sc.pick_alias(T, "deepseek-v4.1-flash@a4s1") is None)
    ok("非法 key 不改写", sc.pick_alias(T, "deepseek-v4.1-flash", "nope") is None)
    sc._pins_remember(T, None)
    sc._pins_remember(None, A)
    ok("空值不写文件", not os.path.exists(pf))
finally:
    restore_affinity(saved)

# handler 视角：body 里的 model 已被改写为别名 → 剥后缀后再判定（否则决策日志断流）
ok("strip_alias 去掉 @aNsK", sc.strip_alias("deepseek-v4.1-flash@a6s3") == "deepseek-v4.1-flash", sc.strip_alias("deepseek-v4.1-flash@a6s3"))
ok("strip_alias 去掉旧式 @aN", sc.strip_alias("mimo-v2.6-flash@a4") == "mimo-v2.6-flash", sc.strip_alias("mimo-v2.6-flash@a4"))
ok("strip_alias 不动基础模型名", sc.strip_alias("deepseek-v4.1-flash") == "deepseek-v4.1-flash")
pf = fresh_state()
saved = with_affinity([(A, 100)], known={alias_for("4")})
try:
    sc._pins_remember(T, A)
    # 模拟 handler：传入已被改写的别名，必须先剥后缀才能走到粘性分支
    base = sc.strip_alias("deepseek-v4.1-flash@a4s9")
    k, a, s = sc.resolve_pin(T, base)
    ok("handler 视角剥后缀后仍命中粘性钉选", k == A and a == alias_for("4") and s is True, "%s %s" % (k, a))
finally:
    restore_affinity(saved)

print()
if fails:
    print("FAILED %d: %s" % (len(fails), fails))
    sys.exit(1)
print("ALL PASS")
