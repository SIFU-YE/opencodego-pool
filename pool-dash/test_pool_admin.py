#!/usr/bin/env python3
"""pool_admin 单测：本地可跑、不联网、不碰生产路径。

运行方式（在 pool-dash/ 目录）：

    python3 test_pool_admin.py -v

覆盖维护者要求的 6 项：
1. Host / Content-Type / 体积上限的允许与拒绝（Host 硬校验只管 `/api/admin/*`；
   `/admin` 独立页面已于 2026-10-17 下线，并入看板页签，路由返回 404）；
2. 模型 allowlist 预览 diff 正确（added/removed/影响面）；
3. 确认串不匹配 → 不执行（token 不被消费，可重试）；
4. 密钥写进临时 POOL_HOME/.env 后，API 响应与审计里都不含完整值，只有指纹；
5. pool_control 导入失败时管理面降级为只读（`/api/admin/status` 报 readonly+原因；
   页签片段仍渲染「只读模式」横幅），写接口 503；
6. HTML 转义生效（恶意降级原因不会以裸 HTML 出现在页签片段里），非法 model id 被白名单拒绝。

所有落盘都在 ``tempfile.mkdtemp()`` 的临时 POOL_HOME 下；reconciler 子进程调用被
monkeypatch 掉（``AdminApp.run_reconciler``），因此不会真的重启/改动任何环境。
"""
import hashlib
import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import MappingProxyType

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pool_admin      # noqa: E402  （必须在 sys.path 处理之后）
import pool_admin_ui   # noqa: E402


# --------------------------------------------------------------------------- #
# pool_control 替身（落盘到临时 POOL_HOME；签名覆盖两种契约）
# --------------------------------------------------------------------------- #
class FakeControl:
    def __init__(self, home):
        self.home = home
        self.cfg = {
            "version": 1,
            "models": {"allow": []},
            "accounts": [],
            "affinity_mode": "shadow",
        }
        self.calls = []
        self.last_error_value = ""

    # -- 读 ---------------------------------------------------------------- #
    def load_config(self):
        self.calls.append("load_config")
        if self.cfg is None:
            return None
        return MappingProxyType(json.loads(json.dumps(self.cfg)))

    def last_error(self):
        return self.last_error_value

    def _env_doc(self):
        out = {}
        path = os.path.join(self.home, ".env")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.rstrip("\n")
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    name, _, value = line.partition("=")
                    out[name.strip()] = value.strip()
        except FileNotFoundError:
            pass
        return out

    def _fp(self, value):
        return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]

    def read_env_fingerprints(self, vars=None):
        doc = self._env_doc()
        names = list(vars) if vars is not None else list(doc)
        return {n: self._fp(doc[n]) for n in names if n in doc}

    # -- 写 ---------------------------------------------------------------- #
    def _save(self):
        path = os.path.join(self.home, "pool-config.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.cfg, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def atomic_write_json(self, path, data, mode=0o600):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        return path

    def update_models(self, models, actor="dashboard"):
        self.calls.append(("update_models", list(models), actor))
        for model in models:
            if not isinstance(model, str) or not re.fullmatch(r"^[A-Za-z0-9._:-]{1,64}$", model):
                raise ValueError("非法模型 id: %r" % (model,))
        self.cfg["models"] = {"allow": list(models)}
        self._save()
        return {"ok": True, "models": list(models)}

    def update_accounts(self, accounts, actor="dashboard"):
        self.calls.append(("update_accounts", [dict(a) for a in accounts], actor))
        clean = []
        for account in accounts:
            allowed = {"slot", "key_var", "email_var", "enabled"}
            if set(account) - allowed:
                raise ValueError("账号含未知字段: %s" % sorted(set(account) - allowed))
            slot = account.get("slot")
            if not isinstance(slot, int) or slot < 1:
                raise ValueError("slot 必须为正整数")
            if account.get("key_var") != "OCG_KEY_%d" % slot:
                raise ValueError("key_var 必须等于 OCG_KEY_<slot>")
            email_var = account.get("email_var")
            if email_var is not None and email_var != "OCG_EMAIL_%d" % slot:
                raise ValueError("email_var 必须等于 OCG_EMAIL_<slot>")
            if not isinstance(account.get("enabled"), bool):
                raise ValueError("enabled 必须是 bool")
            item = {"slot": slot, "key_var": account["key_var"],
                    "enabled": account["enabled"]}
            if email_var is not None:
                item["email_var"] = email_var
            clean.append(item)
        self.cfg["accounts"] = clean
        self._save()
        return {"ok": True, "accounts": clean}

    def set_affinity_mode(self, mode, actor="dashboard"):
        self.calls.append(("set_affinity_mode", mode, actor))
        if mode not in ("shadow", "full"):
            raise ValueError("mode 必须是 shadow 或 full")
        self.cfg["affinity_mode"] = mode
        self._save()
        mirror = os.path.join(self.home, "pool-affinity.json")
        self.atomic_write_json(mirror, {"mode": mode})
        return {"ok": True, "mode": mode}

    def write_env_value(self, name, value):
        self.calls.append(("write_env_value", name))
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", name):
            raise ValueError("非法环境变量名")
        path = os.path.join(self.home, ".env")
        lines = []
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        backup = path + ".bak-test"
        if os.path.isfile(path):
            shutil.copy2(path, backup)
        else:
            backup = ""
        found = False
        out = []
        for line in lines:
            if line.startswith(name + "="):
                out.append(name + "=" + value)
                found = True
            else:
                out.append(line)
        if not found:
            out.append(name + "=" + value)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return {"ok": True, "name": name, "changed": True,
                "fingerprint": self._fp(value), "backup": backup}

    def remove_env_value(self, name):
        self.calls.append(("remove_env_value", name))
        path = os.path.join(self.home, ".env")
        if not os.path.isfile(path):
            return {"ok": True, "name": name, "removed": False}
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        out = [ln for ln in lines if not ln.startswith(name + "=")]
        removed = len(out) != len(lines)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + ("\n" if out else ""))
        return {"ok": True, "name": name, "removed": removed}

    def append_audit(self, entry):
        self.calls.append(("append_audit", dict(entry)))
        path = os.path.join(self.home, "admin-audit.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# HTTP harness
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, *args):  # 静默
        pass

    def do_GET(self):
        if not self.app.dispatch(self, "GET"):
            self.send_error(404)

    def do_POST(self):
        if not self.app.dispatch(self, "POST"):
            self.send_error(404)


class AdminHTTPTest(unittest.TestCase):
    maxDiff = None

    # -- 环境 -------------------------------------------------------------- #
    ENV_KEYS = ("POOL_HOME", "POOL_CONFIG_PATH", "POOL_AUDIT_PATH",
                "POOL_CONFIG_YAML", "POOL_RECONCILER", "POOL_ADMIN_HOSTS",
                "POOL_DB_PATH")

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        self.tmp = tempfile.mkdtemp(prefix="pool-admin-test-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        os.environ["POOL_HOME"] = self.home

        # 假 reconciler 文件（不会被执行：run_reconciler 被替换）
        self.reconciler = os.path.join(self.home, "pool_reconciler.py")
        with open(self.reconciler, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env python3\nprint('{}')\n")

        # 当前已部署模型（config.yaml，作为 "已部署/可用" 一侧）
        with open(os.path.join(self.home, "config.yaml"), "w", encoding="utf-8") as fh:
            fh.write("model_list:\n"
                     "  - model_name: alpha\n"
                     "    litellm_params:\n"
                     "      model: openai/alpha\n"
                     "  - model_name: beta\n"
                     "  - model_name: gamma@a1s1\n")

        self.control = FakeControl(self.home)
        self.control.cfg = {
            "version": 1,
            "models": {"allow": ["alpha", "beta"]},
            "accounts": [{"slot": 1, "key_var": "OCG_KEY_1",
                          "email_var": "OCG_EMAIL_1", "enabled": True}],
            "affinity_mode": "shadow",
        }
        self.app = pool_admin.AdminApp(control=self.control, home=self.home,
                                       reconciler=self.reconciler)
        self.reconciler_modes = []
        self.app.run_reconciler = self._fake_reconciler

        _Handler.app = self.app
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # -- 工具 -------------------------------------------------------------- #
    def _fake_reconciler(self, mode, timeout=30):
        self.reconciler_modes.append(mode)
        if mode == "--check":
            return {"ok": True, "rc": 0, "json": {"ok": True, "hot_path": "available"},
                    "stdout": "{\"ok\":true,\"hot_path\":\"available\"}", "stderr": "",
                    "error": "", "cmd": "fake --check"}
        if mode == "--dry-run":
            doc = {"ok": True, "dry_run": True,
                   "actions": [{"op": "add", "model_name": "gamma", "reason": ["missing"]}],
                   "summary": {"add": 1, "update": 0, "delete": 0, "unchanged": 2}}
            return {"ok": True, "rc": 0, "json": doc, "stdout": json.dumps(doc),
                    "stderr": "", "error": "", "cmd": "fake --dry-run"}
        return {"ok": True, "rc": 0, "json": {"ok": True, "actions": [], "summary": {}},
                "stdout": "{\"ok\":true}", "stderr": "", "error": "", "cmd": "fake --once"}

    def request(self, method, path, body=None, host="127.0.0.1",
                ctype="application/json", raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Host": host}
        payload = None
        if raw is not None:
            payload = raw
        elif body is not None:
            payload = json.dumps(body).encode("utf-8")
        if payload is not None:
            headers["Content-Type"] = ctype
            headers["Content-Length"] = str(len(payload))
            conn.request(method, path, body=payload, headers=headers)
        else:
            conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def request_oversize(self, path, declared=70000):
        """只声明超长 Content-Length，不真正发 body —— 服务端应在读取前 413。"""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.putrequest("POST", path, skip_host=True)
        conn.putheader("Host", "127.0.0.1")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(declared))
        conn.endheaders()
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def json_data(self, status, payload):
        self.assertEqual(status, 200, payload[:400])
        doc = json.loads(payload.decode("utf-8"))
        self.assertTrue(doc.get("ok"), doc)
        return doc.get("data") or {}

    def json_error(self, status, payload):
        doc = json.loads(payload.decode("utf-8"))
        self.assertFalse(doc.get("ok"))
        return doc.get("error") or {}

    def wait_job(self, job_id, timeout=10.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            status, payload = self.request("GET", "/api/admin/jobs/" + job_id)
            self.assertEqual(status, 200, payload[:300])
            last = self.json_data(status, payload)
            if last.get("state") in ("done", "error"):
                return last
            time.sleep(0.02)
        self.fail("job 超时未完成: %r" % (last,))

    def preview(self, path, body):
        status, payload = self.request("POST", path, body)
        return self.json_data(status, payload)

    def apply(self, path, token, confirm, **extra):
        body = {"token": token, "confirm": confirm}
        body.update(extra)
        status, payload = self.request("POST", path, body)
        return status, payload

    # ------------------------------------------------------------------ #
    # 1) Host / Content-Type / 体积上限
    # ------------------------------------------------------------------ #
    def test_01_host_and_content_type_and_body_limit(self):
        # 2026-10-17：/admin 独立页面已下线（管理控制台并入看板页签）→ 404，与本机/远端无关
        status, _ = self.request("GET", "/admin", host="evil.example")
        self.assertEqual(status, 404)
        status, _ = self.request("GET", "/admin", host="localhost")
        self.assertEqual(status, 404)

        # 并入页签后，写接口的 Host 硬校验一分不减
        status, _ = self.request("GET", "/api/admin/status", host="evil.example")
        self.assertEqual(status, 403)
        status, payload = self.request("GET", "/api/admin/status", host="localhost:4010")
        self.assertEqual(status, 200)
        self.assertEqual(self.json_data(status, payload).get("hotPath"), "available")

        # 显式白名单
        os.environ["POOL_ADMIN_HOSTS"] = "pool.example"
        status, _ = self.request("GET", "/api/admin/status", host="pool.example")
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/api/admin/status", host="evil.example")
        self.assertEqual(status, 403)
        os.environ.pop("POOL_ADMIN_HOSTS", None)

        # Content-Type：只接受 application/json
        status, payload = self.request(
            "POST", "/api/admin/models/preview", raw=b"{}", ctype="text/plain")
        self.assertEqual(status, 415)
        self.assertEqual(self.json_error(status, payload).get("code"), "unsupported_media_type")

        # 非法/缺失 JSON body
        status, _ = self.request(
            "POST", "/api/admin/models/preview", raw=b"not-json",
            ctype="application/json")
        self.assertEqual(status, 400)

        # 64KB 上限
        status, payload = self.request_oversize("/api/admin/models/preview")
        self.assertEqual(status, 413)
        self.assertEqual(self.json_error(status, payload).get("code"), "payload_too_large")

        # GET 合法路径不会被误伤
        status, payload = self.request("GET", "/api/admin/models")
        self.assertEqual(status, 200)
        self.assertIn("alpha", self.json_data(status, payload).get("allow"))

    # ------------------------------------------------------------------ #
    # 2) allowlist 预览 diff 正确
    # ------------------------------------------------------------------ #
    def test_02_models_preview_diff(self):
        status, payload = self.request("GET", "/api/admin/models")
        data = self.json_data(status, payload)
        self.assertEqual(data["allow"], ["alpha", "beta"])
        self.assertIn("gamma", data["available"])     # 来自已部署 config.yaml
        self.assertEqual(data["deployed"], ["alpha", "beta", "gamma"])

        # 历史部署备份里的模型也留在"可重新勾选"的候选集合里（下线后仍可恢复）
        with open(os.path.join(self.home, "config.yaml.bak-guard"), "w",
                  encoding="utf-8") as fh:
            fh.write("model_list:\n  - model_name: delta@a1\n")
        status, payload = self.request("GET", "/api/admin/models")
        self.assertIn("delta", self.json_data(status, payload)["available"])

        pv = self.preview("/api/admin/models/preview", {"models": ["alpha", "gamma"]})
        self.assertTrue(pv.get("token"))
        self.assertEqual(pv["diff"]["added"], ["gamma"])
        self.assertEqual(pv["diff"]["removed"], ["beta"])
        self.assertEqual(pv["confirmExpected"], "beta")
        self.assertIn("beta", pv["confirmHint"])
        joined = "\n".join(pv["impact"])
        self.assertIn("下线", joined)
        self.assertIn("+1", joined)                   # 新增 1 个 deployment
        self.assertIn("-1", joined)                   # 下线 1 个 deployment
        # --dry-run 被调用，且结构化输出被展示
        self.assertIn("--dry-run", self.reconciler_modes)
        self.assertTrue(any("reconciler --dry-run" in line for line in pv["details"]))
        # 预览不写入任何配置
        self.assertNotIn("update_models", [c[0] for c in self.control.calls if isinstance(c, tuple)])

        # 无变化 → 400（幂等）
        status, payload = self.request(
            "POST", "/api/admin/models/preview", {"models": ["alpha", "beta"]})
        self.assertEqual(status, 400)
        self.assertIn("无变化", self.json_error(status, payload).get("message", ""))

        # 非法 model id 被白名单正则拒绝（不是拼接进配置）
        status, payload = self.request(
            "POST", "/api/admin/models/preview",
            {"models": ["<script>alert(1)</script>"]})
        self.assertEqual(status, 400)

    # ------------------------------------------------------------------ #
    # 3) 确认串不匹配不执行
    # ------------------------------------------------------------------ #
    def test_03_confirm_mismatch_does_not_execute(self):
        pv = self.preview("/api/admin/models/preview", {"models": ["alpha", "beta", "gamma"]})
        token = pv["token"]
        self.assertEqual(pv["confirmExpected"], "gamma")

        status, payload = self.apply("/api/admin/models/apply", token, "wrong-model")
        self.assertEqual(status, 409)
        self.assertEqual(self.json_error(status, payload).get("code"), "confirm_mismatch")
        self.assertFalse([c for c in self.control.calls
                          if isinstance(c, tuple) and c[0] == "update_models"])
        self.assertEqual(len(self.app._jobs), 0)

        # token 未被消费：用正确确认串可继续执行
        status, payload = self.apply("/api/admin/models/apply", token, "gamma")
        self.assertEqual(status, 200)
        job_id = self.json_data(status, payload)["jobId"]
        job = self.wait_job(job_id)
        self.assertEqual(job["state"], "done", job)
        updates = [c for c in self.control.calls
                   if isinstance(c, tuple) and c[0] == "update_models"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][1], ["alpha", "beta", "gamma"])
        # 四段式最后一步：reconciler --once 热应用
        self.assertIn("--once", self.reconciler_modes)

        # token 单次有效
        status, payload = self.apply("/api/admin/models/apply", token, "gamma")
        self.assertEqual(status, 409)

    # ------------------------------------------------------------------ #
    # 4) 密钥只落 .env，API/审计只回指纹
    # ------------------------------------------------------------------ #
    def test_04_secret_never_echoed(self):
        upstream_key = "sk-upstream-SuperSecretKey-0123456789"
        email = "ops@example.com"
        pv = self.preview("/api/admin/accounts/preview",
                          {"action": "upsert", "slot": 2, "key": upstream_key, "email": email})
        self.assertEqual(pv["confirmExpected"], "2")
        self.assertTrue(pv["diff"]["fingerprintCandidate"].startswith("sha256:"))
        self.assertNotIn(upstream_key, json.dumps(pv))

        status, payload = self.apply("/api/admin/accounts/apply", pv["token"], "2")
        self.assertEqual(status, 200)
        job = self.wait_job(self.json_data(status, payload)["jobId"])
        self.assertEqual(job["state"], "done", job)

        # 密钥确实写进了临时 .env（真实值只在服务端落盘）
        with open(os.path.join(self.home, ".env"), encoding="utf-8") as fh:
            env_text = fh.read()
        self.assertIn(upstream_key, env_text)
        self.assertIn("OCG_EMAIL_2=" + email, env_text)

        # 列表 API 只回指纹，不回完整值
        status, payload = self.request("GET", "/api/admin/accounts")
        raw = payload.decode("utf-8")
        self.assertNotIn(upstream_key, raw)
        accounts = self.json_data(status, payload)["accounts"]
        row = [a for a in accounts if a["slot"] == 2][0]
        self.assertTrue(row["fingerprint"].startswith("sha256:"))
        self.assertNotIn(upstream_key, row["fingerprint"])
        self.assertEqual(row["email"], email)

        # 审计文件也不含完整值
        with open(os.path.join(self.home, "admin-audit.jsonl"), encoding="utf-8") as fh:
            audit_text = fh.read()
        self.assertNotIn(upstream_key, audit_text)
        self.assertIn("sha256:", audit_text)

        # 实例键：完整值只在签发响应出现一次，列表只给 label+尾4
        pv = self.preview("/api/admin/instance-keys/preview",
                          {"action": "issue", "label": "laptop"})
        status, payload = self.apply("/api/admin/instance-keys/apply", pv["token"], "laptop")
        issued = self.json_data(status, payload)
        full_key = issued["key"]
        self.assertTrue(full_key.startswith("sk-pool-"))
        self.assertEqual(len(full_key), len("sk-pool-") + 32)
        status, payload = self.request("GET", "/api/admin/instance-keys")
        raw = payload.decode("utf-8")
        self.assertNotIn(full_key, raw)
        keys = self.json_data(status, payload)["keys"]
        self.assertEqual(keys[0]["label"], "laptop")
        self.assertEqual(keys[0]["tail"], full_key[-4:])
        # sidecar 需要明文键：文件里必须是完整键 -> label
        with open(os.path.join(self.home, "dsh-instance-keys.json"),
                  encoding="utf-8") as fh:
            keys_doc = json.load(fh)
        self.assertEqual(keys_doc[full_key], "laptop")

    # ------------------------------------------------------------------ #
    # 5) 降级：pool_control 导入失败 → 只读状态 + 只读页签片段 + 写接口 503
    # ------------------------------------------------------------------ #
    def test_05_degraded_readonly_page(self):
        from unittest import mock
        with mock.patch.object(pool_admin, "_import_control",
                               side_effect=ImportError("模拟 pool_control 缺失")):
            app = pool_admin.AdminApp(control=None, home=self.home, reconciler="")
        _Handler.app = app
        # 只读状态经 /api/admin/status 暴露（页面已不是独立路由）
        status, payload = self.request("GET", "/api/admin/status")
        data = self.json_data(status, payload)
        self.assertTrue(data.get("readonly"))
        self.assertIn("pool_control", data.get("reason") or "")

        # 页签片段仍由服务端渲染降级横幅，并带上看板需要的挂载点
        body = pool_admin_ui.build_panel(app.state())
        self.assertIn("只读模式", body)
        self.assertIn("管理控制台", body)
        self.assertIn("pool_control", body)
        self.assertIn('id="adminBoot"', body)
        self.assertIn("window.adminInit", body)

        status, payload = self.request("POST", "/api/admin/models/preview",
                                       {"models": ["alpha"]})
        self.assertEqual(status, 503)
        self.assertIn(self.json_error(status, payload).get("code"),
                      ("readonly", "control_missing"))
        # GET 列表尽力而为（不 500）
        status, payload = self.request("GET", "/api/admin/models")
        self.assertEqual(status, 200)
        data = self.json_data(status, payload)
        self.assertTrue(data.get("degraded"))
        status, payload = self.request("GET", "/api/admin/instance-keys")
        self.assertEqual(status, 200)
        _Handler.app = self.app

    # ------------------------------------------------------------------ #
    # 6) HTML 转义 + 非法输入拒绝
    # ------------------------------------------------------------------ #
    def test_06_html_escaping(self):
        from unittest import mock
        evil = '<img src=x onerror="alert(1)"></script><script>alert(2)</script>'
        with mock.patch.object(pool_admin, "_import_control",
                               side_effect=ImportError(evil)):
            app = pool_admin.AdminApp(control=None, home=self.home, reconciler="")
        _Handler.app = app
        status, payload = self.request("GET", "/api/admin/status")
        self.assertEqual(status, 200)
        # JSON 侧是原值（JSON 不是 HTML 上下文）；转义责任在页签渲染那一步
        self.assertIn("alert(1)", self.json_data(status, payload).get("reason") or "")

        body = pool_admin_ui.build_panel(app.state())
        # 服务端横幅转义（含引号）
        self.assertIn("&lt;img src=x", body)
        self.assertNotIn("<img src=x", body)
        # bootstrap JSON 把 < 转义成 \u003c，避免 </script> 提前闭合
        self.assertNotIn("</script><script>alert(2)", body)
        self.assertIn("\\u003c", body)

        # 恶意 model id / label 一律被白名单拒绝
        _Handler.app = self.app
        status, payload = self.request("POST", "/api/admin/models/preview",
                                       {"models": ["ok-model", "<b>x</b>"]})
        self.assertEqual(status, 400)
        status, payload = self.request("POST", "/api/admin/instance-keys/preview",
                                       {"action": "issue", "label": "<script>"})
        self.assertEqual(status, 400)


# --------------------------------------------------------------------------- #
# 7/8) 2026-10-19（task-5）管理控制台「已部署 / 估算 deployment」对齐 pool.db 实况：
#      LiteLLM DB 化后 config.yaml 已无 model_list，旧实现恒返回 [] → 页面「已部署 0 个」，
#      而实况 135 / 看板 KPI「部署」135，两者自相矛盾。这里用临时 pool.db 锁死新口径。
# --------------------------------------------------------------------------- #
class PoolDbDeploymentTest(unittest.TestCase):
    ENV_KEYS = ("POOL_HOME", "POOL_CONFIG_PATH", "POOL_AUDIT_PATH",
                "POOL_CONFIG_YAML", "POOL_RECONCILER", "POOL_ADMIN_HOSTS",
                "POOL_DB_PATH")

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        self.tmp = tempfile.mkdtemp(prefix="pool-admin-db-test-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        os.environ["POOL_HOME"] = self.home

        # 空 config.yaml：模拟 DB 化后的现状（没有 model_list）
        with open(os.path.join(self.home, "config.yaml"), "w", encoding="utf-8") as fh:
            fh.write("general_settings:\n  master_key: os.environ/LITELLM_MASTER_KEY\n")

        self.control = FakeControl(self.home)
        self.control.cfg = {
            "version": 1,
            "models": {"allow": ["alpha", "beta"]},
            "accounts": [{"slot": 4, "key_var": "OCG_KEY_4",
                          "email_var": "OCG_EMAIL_4", "enabled": True}],
            "affinity_mode": "shadow",
        }
        self.app = pool_admin.AdminApp(control=self.control, home=self.home, reconciler="")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write_pool_db(self, model_names):
        """造一个最小 deployments 表（列与线上一致），返回行数。"""
        import sqlite3
        path = os.path.join(self.home, "pool.db")
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE deployments (identity TEXT PRIMARY KEY, model_name TEXT, "
                "model TEXT, slot INTEGER, bucket TEXT, params TEXT, key_fp TEXT, updated_at TEXT)")
            conn.executemany(
                "INSERT INTO deployments (identity, model_name, model, slot, bucket, params, "
                "key_fp, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                [("id-%d" % i, name, "openai/" + name, 4, "b%d" % i, "{}", "", "")
                 for i, name in enumerate(model_names)])
            conn.commit()
        finally:
            conn.close()
        return len(model_names)

    # -- 8) pool.db 实况优先 ------------------------------------------------- #
    def test_07_deployed_count_comes_from_pool_db(self):
        rows = self._write_pool_db([
            "alpha",                    # 主名
            "alpha@a4s1",               # 分片别名（应归一到 alpha）
            "alpha@a4s2",
            "beta",
            "gamma@a6s3",               # 未在 allowlist 里的主名也应被识别
        ])
        data = self.app._get_models()
        # 逐模型标签用的主模型名集合（去别名、去重、排序）
        self.assertEqual(data["deployed"], ["alpha", "beta", "gamma"])
        self.assertEqual(data["deployedModels"], ["alpha", "beta", "gamma"])
        # 「已部署」= pool.db 实况行数（与 /api/summary.deployments、LiteLLM /model/info 同源）
        self.assertEqual(data["deployedRows"], rows)
        self.assertEqual(data["deployments"], rows)
        self.assertEqual(data["deploymentsSource"], "pool.db")
        # 旧估算公式只作为「仅供参考」的对照值保留
        self.assertEqual(data["deploymentsEstimated"], 2 * 1)
        # DB 优先：空 config.yaml 也有结果（旧实现这里恒为 []）
        self.assertNotEqual(data["deployed"], [])

        # 面板文案已切到实况字段，且不再出现旧的「预计 deployment」矛盾表述
        body = pool_admin_ui.build_panel(self.app.state())
        self.assertIn("deployedRows", body)
        self.assertIn("deploymentsEstimated", body)
        self.assertNotIn("预计 deployment", body)
        self.assertIn("仅供参考", body)

    # -- 9) pool.db 不可读 → 回退 config.yaml，不抛异常/不 500 ---------------- #
    def test_08_pool_db_unreadable_falls_back_to_yaml(self):
        with open(os.path.join(self.home, "config.yaml"), "w", encoding="utf-8") as fh:
            fh.write("model_list:\n"
                     "  - model_name: delta\n"
                     "  - model_name: epsilon@a5s2\n")
        # 写坏文件：sqlite 能 open 但查询必报 DatabaseError → 必须被兜住
        with open(os.path.join(self.home, "pool.db"), "wb") as fh:
            fh.write(b"not a sqlite database, on purpose")

        self.assertIsNone(self.app._deployment_stats()["rows"])     # fail-soft
        deployed = self.app._deployed_models()                      # 不抛异常
        self.assertEqual(deployed, ["delta", "epsilon"])
        data = self.app._get_models()
        self.assertEqual(data["deployed"], ["delta", "epsilon"])
        self.assertEqual(data["deploymentsEstimated"], 2 * 1)
        self.assertEqual(data["deployments"], data["deploymentsEstimated"])
        self.assertIn("estimated", data["deploymentsSource"])

        # 页面/接口不 500：起一个临时 server 打 /api/admin/models
        _Handler.app = self.app
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            conn.request("GET", "/api/admin/models", headers={"Host": "127.0.0.1"})
            resp = conn.getresponse()
            payload = resp.read()
            conn.close()
            self.assertEqual(resp.status, 200, payload[:400])
            doc = json.loads(payload.decode("utf-8"))
            self.assertTrue(doc.get("ok"), doc)
            self.assertEqual(doc["data"]["deployedRows"], doc["data"]["deploymentsEstimated"])
        finally:
            server.shutdown()
            server.server_close()
            _Handler.app = None

    # -- 10) 无 pool.db 且无 config.yaml → 两个口径都安全归零，不抛 ----------- #
    def test_09_no_db_no_yaml_is_fail_soft(self):
        os.unlink(os.path.join(self.home, "config.yaml"))
        self.assertIsNone(self.app._deployment_stats()["rows"])
        self.assertEqual(self.app._deployed_models(), [])
        data = self.app._get_models()
        self.assertEqual(data["deployed"], [])
        self.assertEqual(data["deployments"], data["deploymentsEstimated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
