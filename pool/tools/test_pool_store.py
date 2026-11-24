#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pool_store 回归：pool-config 更新不得清空 deployments 表（2026-11-18）。

线上事故：管理控制台启用账号 → pool_control.update_accounts() 写 pool-config 文档
→ pool_store._sync_deployments() 把“文档没有 deployments 键”误当空清单，DELETE 全表
→ 看板 account_rows() 对所有已启用账号算 deps=0，全部显示「无部署」。
deployments 表由 gen_config.py 的 write_deployments() 独立维护，pool-config 不携带该键。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
POOL_DIR = HERE.parent
if str(POOL_DIR) not in sys.path:
    sys.path.insert(0, str(POOL_DIR))

import pool_control  # noqa: E402
import pool_reconciler  # noqa: E402
import pool_store  # noqa: E402

WATCHED = ("POOL_HOME", "POOL_DB", "POOL_CONFIG_PATH", "POOL_STORE")


class DeploymentTablePreservationTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in WATCHED}
        self.tmp = tempfile.mkdtemp(prefix="pool-store-test-")
        os.environ["POOL_HOME"] = self.tmp
        os.environ["POOL_DB"] = os.path.join(self.tmp, "pool.db")
        os.environ["POOL_CONFIG_PATH"] = os.path.join(self.tmp, "pool-config.json")
        os.environ["POOL_STORE"] = "sqlite"
        self.config = {
            "version": 1,
            "models": {"allow": ["model-a", "model-b"]},
            "accounts": [
                {"slot": 1, "key_var": "OCG_KEY_1", "email_var": "OCG_EMAIL_1", "enabled": True},
                {"slot": 2, "key_var": "OCG_KEY_2", "email_var": "OCG_EMAIL_2", "enabled": False},
            ],
            "affinity_mode": "full",
        }
        with open(os.environ["POOL_CONFIG_PATH"], "w", encoding="utf-8") as fh:
            json.dump(self.config, fh, ensure_ascii=False)
        pool_store.write_doc("pool-config", self.config, mirror_path=None)
        pool_control.invalidate_cache()

    def tearDown(self):
        pool_control.invalidate_cache()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def deployment_count(self):
        conn = sqlite3.connect(os.environ["POOL_DB"])
        try:
            return conn.execute("SELECT COUNT(*) FROM deployments").fetchone()[0]
        finally:
            conn.close()

    def test_update_accounts_does_not_clear_deployments(self):
        pool_store.write_deployments([
            {"identity": "model-a|s1", "model_name": "model-a", "model": "model-a", "slot": 1},
            {"identity": "model-b|s1", "model_name": "model-b", "model": "model-b", "slot": 1},
        ])
        self.assertEqual(self.deployment_count(), 2)

        pool_control.update_accounts([
            {"slot": 1, "key_var": "OCG_KEY_1", "email_var": "OCG_EMAIL_1", "enabled": True},
            {"slot": 2, "key_var": "OCG_KEY_2", "email_var": "OCG_EMAIL_2", "enabled": True},
        ])

        self.assertEqual(self.deployment_count(), 2, "启用账号不应清空 deployments 表")
        conn = sqlite3.connect(os.environ["POOL_DB"])
        try:
            deps = dict(conn.execute("SELECT slot, COUNT(*) FROM deployments GROUP BY slot").fetchall())
        finally:
            conn.close()
        self.assertEqual(deps.get(1), 2)
        self.assertEqual(deps.get(2, 0), 0)

    def test_explicit_empty_deployments_still_clears(self):
        pool_store.write_deployments([
            {"identity": "model-a|s1", "model_name": "model-a", "model": "model-a", "slot": 1},
        ])
        doc = dict(self.config)
        doc["deployments"] = []
        pool_store.write_doc("pool-config", doc, mirror_path=None)
        self.assertEqual(self.deployment_count(), 0, "显式 deployments:[] 仍应清空")

    def test_reconciler_store_rows_have_no_secret_and_parse_slot(self):
        desired = {
            "model-a": {
                "model_name": "model-a",
                "litellm_params": {
                    "model": "openai/model-a",
                    "api_key": "sk-MUST-NOT-LEAK",
                    "extra_headers": {"x-opencode-session": "dsh-acct-2"},
                },
            },
            "model-a@a3s1": {
                "model_name": "model-a@a3s1",
                "litellm_params": {"model": "openai/model-a", "api_key": "sk-MUST-NOT-LEAK-2"},
            },
        }
        rows = pool_reconciler._deployment_rows_for_store(desired)
        text = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("sk-MUST-NOT-LEAK", text)
        by_name = {r["model_name"]: r for r in rows}
        self.assertEqual(by_name["model-a"]["slot"], 2)
        self.assertIsNone(by_name["model-a"]["bucket"])
        self.assertEqual(by_name["model-a@a3s1"]["slot"], 3)
        self.assertEqual(by_name["model-a@a3s1"]["bucket"], 1)

        self.assertEqual(pool_reconciler._sync_store_deployments(desired), 2)
        conn = sqlite3.connect(os.environ["POOL_DB"])
        try:
            deps = dict(conn.execute("SELECT slot, COUNT(*) FROM deployments GROUP BY slot").fetchall())
        finally:
            conn.close()
        self.assertEqual(deps.get(2), 1)
        self.assertEqual(deps.get(3), 1)
        # 表里 params 是 SQLite 文本，确认没有密钥
        conn = sqlite3.connect(os.environ["POOL_DB"])
        try:
            params = [row[0] for row in conn.execute("SELECT params FROM deployments")]
        finally:
            conn.close()
        self.assertNotIn("sk-MUST-NOT-LEAK", "".join(params))


if __name__ == "__main__":
    unittest.main()
