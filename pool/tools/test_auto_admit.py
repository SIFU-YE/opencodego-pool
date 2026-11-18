#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动准入（auto_admit）单元测试。

运行（不联网、不依赖 /opt/litellm）：
    python3 pool/tools/test_auto_admit.py

覆盖：
  A. decide()：健康 → 准入；任一窗口 rate-limited → 拒；monthly 超上限 → 拒；
     空/非法 usage → 拒；
  B. run()：只处理 enabled=false；auto_admit=false 锁定不被放行；无密钥跳过；
     403（订阅失效）跳过且不写盘；healthy → 写 enabled=true + 审计；
  C. dry-run 零写盘；
  D. pool_control.set_account_enabled()：原子翻转、只动目标账号、审计带 reason；
  E. update_accounts()：整表回写时保留 auto_admit=false（不会误放开）；
  F. reconciler 集成：run_once(auto_admit_run=True) 在读取期望集合前完成准入。
"""

import json
import os
import sys
import tempfile
import unittest

# Windows 控制台默认 cp936，中文测试名/断言会 UnicodeEncodeError → 强制 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_DIR = os.path.dirname(HERE)
if POOL_DIR not in sys.path:
    sys.path.insert(0, POOL_DIR)

import auto_admit            # noqa: E402
import pool_control          # noqa: E402

WATCHED_ENV = (
    "POOL_HOME", "POOL_CONFIG_PATH", "POOL_AUDIT_PATH", "POOL_CONFIG_TTL", "POOL_STORE",
    "POOL_AUTO_ADMIT", "POOL_AUTO_ADMIT_MAX_MONTHLY", "POOL_USAGE_URL",
    "POOL_UPDATED_BY", "POOL_ACTOR", "POOL_AFFINITY_MIRROR",
)


def usage(monthly=10.0, rolling=0.0, weekly=0.0, monthly_status="ok"):
    return {"usage": {
        "rolling": {"status": "ok", "percent": rolling, "resetsAt": "2026-11-01T00:00:00Z"},
        "weekly": {"status": "ok", "percent": weekly, "resetsAt": "2026-11-02T00:00:00Z"},
        "monthly": {"status": monthly_status, "percent": monthly, "resetsAt": "2026-11-03T00:00:00Z"},
    }}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="autoadmit-")
        self._saved = {k: os.environ.get(k) for k in WATCHED_ENV}
        for k in WATCHED_ENV:
            os.environ.pop(k, None)
        os.environ["POOL_HOME"] = self.tmp
        os.environ["POOL_STORE"] = "json"   # 文件事实源，便于断言落盘内容
        pool_control.invalidate_cache()
        self.cfg_path = os.path.join(self.tmp, "pool-config.json")
        self.env_path = os.path.join(self.tmp, ".env")
        self.audit_path = os.path.join(self.tmp, "admin-audit.jsonl")

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        pool_control.invalidate_cache()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 夹具 ----------
    def write_config(self, accounts, allow=("deepseek-v4.1-flash",)):
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "models": {"allow": list(allow)},
                       "accounts": accounts, "affinity_mode": "full"},
                      fh, ensure_ascii=False)
        pool_control.invalidate_cache()

    def write_env(self, mapping):
        with open(self.env_path, "w", encoding="utf-8") as fh:
            for k, v in mapping.items():
                fh.write("%s=%s\n" % (k, v))

    def read_config(self):
        with open(self.cfg_path, encoding="utf-8") as fh:
            return json.load(fh)

    def read_audit(self):
        if not os.path.exists(self.audit_path):
            return []
        with open(self.audit_path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def accounts(self, slots_enabled):
        """slots_enabled: {slot: enabled}"""
        return [{"slot": s, "key_var": "OCG_KEY_%d" % s, "email_var": "OCG_EMAIL_%d" % s,
                 "enabled": bool(e)} for s, e in slots_enabled.items()]


class DecideTest(unittest.TestCase):
    def test_healthy_admits(self):
        admit, reason = auto_admit.decide(usage(monthly=12.0))
        self.assertTrue(admit, reason)
        self.assertIn("quota-ok", reason)

    def test_rate_limited_rejects(self):
        admit, reason = auto_admit.decide(usage(monthly=100.0, monthly_status="rate-limited"))
        self.assertFalse(admit)
        self.assertIn("rate-limited", reason)
        self.assertIn("monthly", reason)

    def test_weekly_rate_limited_rejects(self):
        doc = usage()
        doc["usage"]["weekly"]["status"] = "rate-limited"
        admit, reason = auto_admit.decide(doc)
        self.assertFalse(admit)
        self.assertIn("weekly", reason)

    def test_monthly_cap_rejects(self):
        os.environ["POOL_AUTO_ADMIT_MAX_MONTHLY"] = "90"
        try:
            admit, reason = auto_admit.decide(usage(monthly=95.0))
            self.assertFalse(admit)
            self.assertIn("monthly-percent", reason)
        finally:
            os.environ.pop("POOL_AUTO_ADMIT_MAX_MONTHLY", None)

    def test_empty_or_invalid_usage_rejects(self):
        for bad in (None, {}, {"usage": {}}, {"usage": "x"}):
            admit, _ = auto_admit.decide(bad)
            self.assertFalse(admit, bad)


class RunTest(Base):
    def test_admits_healthy_disabled_account(self):
        self.write_config(self.accounts({1: False, 2: False}))
        self.write_env({"OCG_KEY_1": "k1", "OCG_KEY_2": "k2"})
        calls = []

        def fake_fetch(key):
            calls.append(key)
            return {"ok": True, "usage": usage(monthly=5.0)}

        result = auto_admit.run(fetch=fake_fetch)
        self.assertEqual(sorted(calls), ["k1", "k2"])
        self.assertEqual([a["slot"] for a in result["admitted"]], [1, 2])
        cfg = self.read_config()
        self.assertTrue(all(a["enabled"] for a in cfg["accounts"]))
        actions = [r["action"] for r in self.read_audit()]
        self.assertIn("accounts.set_enabled", actions)
        entry = [r for r in self.read_audit() if r["action"] == "accounts.set_enabled"][0]
        self.assertEqual(entry["actor"], "auto-admit")
        self.assertIn("auto-admit:quota-ok", json.dumps(entry, ensure_ascii=False))

    def test_skips_enabled_locked_and_keys_missing(self):
        self.write_config([
            {"slot": 1, "key_var": "OCG_KEY_1", "enabled": True},                  # 已在池
            {"slot": 2, "key_var": "OCG_KEY_2", "enabled": False, "auto_admit": False},  # 人工锁定
            {"slot": 3, "key_var": "OCG_KEY_3", "enabled": False},                 # 无密钥
        ])
        self.write_env({"OCG_KEY_1": "k1", "OCG_KEY_2": "k2"})

        result = auto_admit.run(fetch=lambda k: {"ok": True, "usage": usage()})
        self.assertEqual(result["checked"], 0)           # 只有 disabled 且未锁定的才检查
        self.assertEqual(result["admitted"], [])
        reasons = {s["slot"]: s["reason"] for s in result["skipped"]}
        self.assertIn("locked", reasons[2])
        self.assertIn("no-key", reasons[3])
        cfg = {a["slot"]: a["enabled"] for a in self.read_config()["accounts"]}
        self.assertEqual(cfg, {1: True, 2: False, 3: False})

    def test_usage_403_stays_disabled(self):
        self.write_config(self.accounts({5: False}))
        self.write_env({"OCG_KEY_5": "k5"})
        result = auto_admit.run(fetch=lambda k: {"ok": False, "status": 403, "error": "http-403"})
        self.assertEqual(result["admitted"], [])
        self.assertIn("usage-error:http-403", result["skipped"][0]["reason"])
        self.assertFalse(self.read_config()["accounts"][0]["enabled"])

    def test_rate_limited_stays_disabled(self):
        self.write_config(self.accounts({3: False}))
        self.write_env({"OCG_KEY_3": "k3"})
        result = auto_admit.run(
            fetch=lambda k: {"ok": True, "usage": usage(monthly=100.0, monthly_status="rate-limited")})
        self.assertEqual(result["admitted"], [])
        self.assertIn("rate-limited", result["skipped"][0]["reason"])
        self.assertFalse(self.read_config()["accounts"][0]["enabled"])

    def test_dry_run_writes_nothing(self):
        self.write_config(self.accounts({1: False}))
        self.write_env({"OCG_KEY_1": "k1"})
        result = auto_admit.run(dry_run=True,
                                fetch=lambda k: {"ok": True, "usage": usage()})
        self.assertEqual(len(result["admitted"]), 1)
        self.assertTrue(result["admitted"][0]["dry_run"])
        self.assertFalse(self.read_config()["accounts"][0]["enabled"])
        self.assertEqual(self.read_audit(), [])

    def test_global_switch_off(self):
        os.environ["POOL_AUTO_ADMIT"] = "0"
        self.write_config(self.accounts({1: False}))
        self.write_env({"OCG_KEY_1": "k1"})
        result = auto_admit.run(fetch=lambda k: {"ok": True, "usage": usage()})
        self.assertEqual(result["admitted"], [])
        self.assertEqual(result.get("skipped_all"), "disabled(POOL_AUTO_ADMIT=0)")
        self.assertFalse(self.read_config()["accounts"][0]["enabled"])


class ControlTest(Base):
    def test_set_account_enabled_atomic(self):
        self.write_config([
            {"slot": 1, "key_var": "OCG_KEY_1", "enabled": True, "auto_admit": False},
            {"slot": 2, "key_var": "OCG_KEY_2", "enabled": False},
        ])
        pool_control.set_account_enabled(2, True, actor="tester", reason="unit")
        cfg = {a["slot"]: a for a in self.read_config()["accounts"]}
        self.assertTrue(cfg[2]["enabled"])
        # 其它账号逐字段不变（含 auto_admit 锁定）
        self.assertEqual(cfg[1]["enabled"], True)
        self.assertEqual(cfg[1]["auto_admit"], False)
        err = [r for r in self.read_audit() if r["action"] == "accounts.set_enabled"][-1]
        self.assertEqual(err["target"], "slot:2")
        self.assertEqual(err["detail"]["reason"], "unit")

    def test_set_account_enabled_unknown_slot(self):
        self.write_config(self.accounts({1: True}))
        with self.assertRaises(ValueError):
            pool_control.set_account_enabled(9, True)

    def test_update_accounts_preserves_auto_admit_lock(self):
        self.write_config([{"slot": 1, "key_var": "OCG_KEY_1", "enabled": False,
                            "auto_admit": False}])
        # 旧客户端/看板整表回写（不带 auto_admit）不得解锁
        pool_control.update_accounts([{"slot": 1, "key_var": "OCG_KEY_1", "enabled": True}])
        cfg = self.read_config()["accounts"][0]
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg.get("auto_admit"), False)

    def test_validate_rejects_non_bool_auto_admit(self):
        with self.assertRaises(ValueError):
            pool_control._validate_accounts(
                [{"slot": 1, "key_var": "OCG_KEY_1", "auto_admit": "yes"}])


class ReconcilerIntegrationTest(Base):
    def test_run_once_admits_before_building_desired(self):
        """准入发生在读取期望集合之前：本轮就要为刚放回的账号构造 deployment。"""
        import pool_reconciler

        self.write_config(self.accounts({1: False}))
        self.write_env({"OCG_KEY_1": "secret-key-1"})

        orig = auto_admit.fetch_usage
        auto_admit.fetch_usage = lambda key, timeout=12.0: {"ok": True, "usage": usage()}
        try:
            # dry-run：只预览，不写盘
            preview = pool_reconciler._run_auto_admit(dry_run=True)
            self.assertEqual([a["slot"] for a in preview["admitted"]], [1])
            self.assertFalse(self.read_config()["accounts"][0]["enabled"])

            # 真跑：写盘后 build_desired 必须已经包含该账号的 deployment
            report = pool_reconciler._run_auto_admit(dry_run=False)
            self.assertEqual([a["slot"] for a in report["admitted"]], [1])
            self.assertTrue(self.read_config()["accounts"][0]["enabled"])

            cfg = pool_control.load_config(force=True)
            env = pool_control.read_env()
            desired = pool_reconciler.build_desired(cfg, env)
            slots = {dep["litellm_params"]["extra_headers"]["x-opencode-session"]
                     for dep in desired.values()}
            self.assertTrue(any(s.startswith("dsh-acct-1") for s in slots), slots)
        finally:
            auto_admit.fetch_usage = orig

    def test_auto_admit_step_failure_does_not_break_reconciler(self):
        """自动准入内部抛错时 _run_auto_admit 只返回错误，不向上抛。"""
        import pool_reconciler

        self.write_config(self.accounts({1: False}))
        orig = auto_admit.run

        def boom(**kwargs):
            raise RuntimeError("simulated")

        auto_admit.run = boom
        try:
            report = pool_reconciler._run_auto_admit(dry_run=False)
            self.assertFalse(report["ok"])
            self.assertIn("simulated", report["error"])
        finally:
            auto_admit.run = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
