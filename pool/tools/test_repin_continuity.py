#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""软淘汰（额度到 ε 线）下的会话迁移与连续性保护测试（2026-10-31）。

运行（离线，不依赖 /opt/litellm、不联网）：
    python3 pool/tools/test_repin_continuity.py

覆盖：
  A. 已钉住 + 账号「软低额度」（share 被 ε 归零、窗口仍 ok）：
     - 活跃树（距上次请求 < REPIN_IDLE_S）→ 继续沿用旧号（repin=soft-defer，保缓存/推理连续性）
     - 空闲树（≥ REPIN_IDLE_S）        → 允许迁移（repin=soft-idle）
     - 活跃过久（defer > LOW_MAX_S）   → 强制迁移（repin=soft-max）
     - POOL_SOFT_REPIN=0               → 回退旧行为（repin=soft-off）
  B. 硬淘汰（rate-limited/hard=true/别名缺失）→ 立即迁移（repin=hard）
  C. 健康号仍沿用（repin=None），不因再平衡漂移
  D. pins v2 持久化：ts/defer 往返一致、兼容 v1
"""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

# Windows 控制台默认 cp936 → 强制 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_DIR = os.path.dirname(HERE)
if POOL_DIR not in sys.path:
    sys.path.insert(0, POOL_DIR)

MODEL = "deepseek-v4.1-flash"
TREES = ("tree-alpha", "tree-bravo", "tree-charlie", "tree-delta")


class SoftRepinTest(unittest.TestCase):
    home = None
    m = None

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="softrepin-")
        os.environ["POOL_HOME"] = cls.home
        os.environ.pop("POOL_CONFIG_PATH", None)
        os.environ.pop("POOL_STORE", None)
        spec = importlib.util.spec_from_file_location(
            "sidecar_softrepin", os.path.join(POOL_DIR, "session-sidecar.py"))
        cls.m = importlib.util.module_from_spec(spec)
        sys.modules["sidecar_softrepin"] = cls.m
        spec.loader.exec_module(cls.m)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    # ---------------- 夹具 ----------------
    def write_json(self, name, obj):
        with open(os.path.join(self.home, name), "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)

    def write_aliases(self, slots=(4, 6, 9, 10)):
        aliases = []
        for s in slots:
            for b in range(1, 9):
                aliases.append("%s@a%ds%d" % (MODEL, s, b))
        self.write_json("pool-aliases.json",
                        {"version": 1, "updated_at": "2026-10-31T00:00:00Z", "aliases": aliases})

    def write_share(self, accounts):
        self.write_json("routing-share.json",
                        {"generated_at": "2026-10-31T00:00:00Z", "mode": "shadow",
                         "status": "ok", "accounts": accounts})

    def acct(self, key, share, low=False, hard=False, status="ok", rem=50.0):
        return {"key_ref": key, "share": share, "low": low, "hard": hard,
                "gated": hard, "available": not hard, "rem_pct": rem,
                "windows": {"monthly": {"status": status, "percent": 100 - rem,
                                        "resetsAt": "2026-11-19T00:00:00Z"}}}

    def write_pins(self, entries):
        """entries: {tree: (key, ts|None, defer|None)}"""
        pins = {}
        for t, v in entries.items():
            k = v[0]
            ts = v[1] if len(v) > 1 else None
            df = v[2] if len(v) > 2 else None
            rec = {"key": k}
            if ts:
                rec["ts"] = ts
            if df:
                rec["defer"] = df
            pins[t] = rec
        self.write_json("pool-tree-pins.json", {"v": 2, "pins": pins})

    def reset_caches(self):
        m = self.m
        m._AFF_CACHE["checked"] = 0.0
        m._AFF_CACHE["accounts"] = []
        m._AFF_CACHE["states"] = {}
        m._ALIAS_CACHE["checked"] = 0.0
        m._ALIAS_CACHE["ok"] = False
        m._ALIAS_CACHE["set"] = set()
        m._PIN.update(loaded=False, map={}, ts={}, defer={}, saved=0.0)
        m._CTRL_FN = None
        m._CTRL_NEXT_TRY = 0.0

    def setUp(self):
        self.write_json("pool-affinity.json", {"mode": "full"})
        self.write_json("pool-config.json",
                        {"version": 1, "models": {"allow": [MODEL]},
                         "accounts": [{"slot": s, "key_var": "OCG_KEY_%d" % s, "enabled": True}
                                      for s in (4, 6, 9, 10)]})
        with io.open(os.path.join(self.home, "config.yaml"), "w", encoding="utf-8") as fh:
            fh.write("")
        self.write_aliases()
        self.write_share([
            self.acct("OCG_KEY_4", 0.0, low=True, rem=1.0),
            self.acct("OCG_KEY_6", 0.34),
            self.acct("OCG_KEY_9", 0.33),
            self.acct("OCG_KEY_10", 0.33),
        ])
        self.m.SOFT_REPIN_ENABLE = True
        self.m.REPIN_IDLE_S = 600.0
        self.m.LOW_MAX_S = 1800.0
        self.reset_caches()

    # ---------------- A. 软低额度 ----------------
    def test_active_tree_keeps_low_account(self):
        now = time.time()
        self.write_pins({TREES[0]: ("OCG_KEY_4", now - 60)})
        self.reset_caches()
        key, alias, sticky, repin = self.m.resolve_pin_ex(TREES[0], MODEL)
        self.assertEqual(key, "OCG_KEY_4")           # 活跃会话不被硬切
        self.assertTrue(sticky)
        self.assertIsNotNone(alias)
        self.assertEqual(repin, "soft-defer")
        self.assertGreater(self.m._pins_defer_get(TREES[0]), 0)

    def test_idle_tree_migrates(self):
        now = time.time()
        self.write_pins({TREES[1]: ("OCG_KEY_4", now - 3600)})
        self.reset_caches()
        key, alias, sticky, repin = self.m.resolve_pin_ex(TREES[1], MODEL)
        self.assertNotEqual(key, "OCG_KEY_4")        # 空闲树允许迁移
        self.assertFalse(sticky)
        self.assertIsNotNone(alias)
        self.assertEqual(repin, "soft-idle")

    def test_active_too_long_forced_migrate(self):
        now = time.time()
        self.write_pins({TREES[2]: ("OCG_KEY_4", now - 60, now - 2000)})
        self.reset_caches()
        key, _alias, sticky, repin = self.m.resolve_pin_ex(TREES[2], MODEL)
        self.assertNotEqual(key, "OCG_KEY_4")
        self.assertFalse(sticky)
        self.assertEqual(repin, "soft-max")

    def test_soft_switch_off_falls_back_to_immediate(self):
        now = time.time()
        self.write_pins({TREES[3]: ("OCG_KEY_4", now - 10)})
        self.reset_caches()
        self.m.SOFT_REPIN_ENABLE = False
        try:
            key, _alias, _sticky, repin = self.m.resolve_pin_ex(TREES[3], MODEL)
        finally:
            self.m.SOFT_REPIN_ENABLE = True
        self.assertNotEqual(key, "OCG_KEY_4")        # 回退到旧行为：立即迁移
        self.assertEqual(repin, "soft-off")

    # ---------------- B. 硬淘汰 ----------------
    def test_hard_limited_migrates_immediately(self):
        self.write_share([
            self.acct("OCG_KEY_4", 0.0, low=False, hard=True, status="rate-limited", rem=0.0),
            self.acct("OCG_KEY_6", 0.34),
            self.acct("OCG_KEY_9", 0.33),
            self.acct("OCG_KEY_10", 0.33),
        ])
        self.write_pins({TREES[0]: ("OCG_KEY_4", time.time() - 5)})
        self.reset_caches()
        key, _alias, sticky, repin = self.m.resolve_pin_ex(TREES[0], MODEL)
        self.assertNotEqual(key, "OCG_KEY_4")
        self.assertFalse(sticky)
        self.assertEqual(repin, "hard")

    def test_missing_alias_is_hard(self):
        self.write_aliases(slots=(6, 9, 10))          # 去掉 slot4 的别名
        self.write_pins({TREES[0]: ("OCG_KEY_4", time.time() - 5)})
        self.reset_caches()
        key, _alias, _sticky, repin = self.m.resolve_pin_ex(TREES[0], MODEL)
        self.assertNotEqual(key, "OCG_KEY_4")
        self.assertEqual(repin, "hard")

    # ---------------- C. 健康号沿用 ----------------
    def test_healthy_pin_kept(self):
        self.write_pins({TREES[0]: ("OCG_KEY_9", time.time() - 7200)})
        self.reset_caches()
        key, _alias, sticky, repin = self.m.resolve_pin_ex(TREES[0], MODEL)
        self.assertEqual(key, "OCG_KEY_9")           # 健康号即使空闲也沿用（不因 rebalance 漂移）
        self.assertTrue(sticky)
        self.assertIsNone(repin)

    # ---------------- D. pins v2 持久化 ----------------
    def test_pins_v2_roundtrip(self):
        now = time.time()
        self.write_pins({TREES[0]: ("OCG_KEY_6", now - 100, now - 50)})
        mp, ts, defer = self.m._pins_load_from(os.path.join(self.home, "pool-tree-pins.json"))
        self.assertEqual(mp.get(TREES[0]), "OCG_KEY_6")
        self.assertAlmostEqual(ts.get(TREES[0]), now - 100, places=2)
        self.assertAlmostEqual(defer.get(TREES[0]), now - 50, places=2)

    def test_pins_v1_compatible(self):
        self.write_json("pool-tree-pins.json",
                        {"v": 1, "pins": {TREES[0]: {"key": "OCG_KEY_10"}}})
        mp, ts, defer = self.m._pins_load_from(os.path.join(self.home, "pool-tree-pins.json"))
        self.assertEqual(mp.get(TREES[0]), "OCG_KEY_10")
        self.assertEqual(ts, {})
        self.assertEqual(defer, {})

    def test_touch_updates_last_active(self):
        self.write_pins({TREES[0]: ("OCG_KEY_6", time.time() - 100)})
        self.reset_caches()
        t0 = self.m._pins_last_active(TREES[0])
        self.m._pins_touch(TREES[0], now=time.time())
        self.assertGreater(self.m._pins_last_active(TREES[0]), t0)

    def test_v1_pins_backfill_ts_from_decisions(self):
        """v1 pins 无 ts：从决策日志回填，避免重启后活跃树被误判为空闲。"""
        now = int(time.time())
        self.write_json("pool-tree-pins.json",
                        {"v": 1, "pins": {TREES[0]: {"key": "OCG_KEY_4"}}})
        with io.open(os.path.join(self.home, "pool-affinity-decisions.jsonl"),
                     "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now - 120, "tree": TREES[0], "model": MODEL,
                                 "key_want": "OCG_KEY_4", "tag": "acct:OCG_KEY_4",
                                 "mode": "full", "injected": True}) + "\n")
        self.reset_caches()
        self.m._pins_ensure_loaded()
        got = self.m._pins_last_active(TREES[0])
        self.assertAlmostEqual(got, now - 120, delta=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
