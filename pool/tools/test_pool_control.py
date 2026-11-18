#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""号池热更新控制面单元测试（pool_control + pool_reconciler）。

运行（不依赖真实 LiteLLM、不依赖 /opt/litellm、不联网）：
    python3 pool/tools/test_pool_control.py

覆盖：
  A. 控制文件原子写 / 0600 / 临时文件清理 / 失败回滚；
  B. 读侧 TTL 缓存（默认 2s、POOL_CONFIG_TTL 覆盖、写侧立即失效、异常 fail-open 返回 None）；
  C. 审计脱敏（只出现变量名 + sha256 指纹，绝不出现密钥明文；非法 JSON 字段安全化）；
  D. .env 原子写：保持其它行逐字节不变、行序不变、0600、写前备份、
     值未变不动盘、行注入被拒、删除变量；
  E. 写侧 RMW：update_models / update_accounts / set_affinity_mode 校验 + 锁串行化；
  F. reconciler：期望 deployment 命名与 gen_config.py 一致、差异计算、幂等、
     密钥轮换（明文与脱敏两种远端）、删除仅限号池标记、dry-run 零写入、
     管理 API 不可用时的降级报错（404/401/连不上）、重试上限、文件锁重入跳过。

设计说明：管理 API 用本地临时 HTTP server 桩（stdlib http.server），
既验证业务逻辑也验证 HTTP 路径/鉴权头/请求体，不 mock 掉 urllib 层。
"""

import contextlib
import copy
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

import pool_control          # noqa: E402
import pool_reconciler       # noqa: E402

IS_WINDOWS = os.name == "nt"

# 每个用例前清空、用例后恢复，避免不同用例互相污染
WATCHED_ENV = (
    "POOL_HOME", "POOL_CONFIG_PATH", "POOL_AUDIT_PATH", "POOL_CONFIG_TTL",
    "POOL_SESSION_BUCKETS", "POOL_LITELLM_URL", "LITELLM_BASE_URL",
    "LITELLM_MASTER_KEY", "POOL_RECONCILE_RETRIES", "POOL_RECONCILE_BACKOFF",
    "POOL_RECONCILE_TIMEOUT", "POOL_RECONCILE_INTERVAL", "POOL_UPDATED_BY",
    "POOL_AFFINITY_MIRROR", "POOL_ACTOR", "POOL_STORE",
)

MASTER = "sk-master-TEST-0123456789"
UPSTREAM_KEY = "sk-upstream-key-AAAA-old"
UPSTREAM_KEY2 = "sk-upstream-key-BBBB-new"


def run_reconciler(argv):
    """进程内跑 reconciler CLI：返回 (rc, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = pool_reconciler.main(argv)
    return rc, out.getvalue(), err.getvalue()


def last_json(text):
    """从 stdout 取最后一行 JSON（watch 模式会多行，普通模式只有一行）。"""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise AssertionError("stdout 没有 JSON 输出")
    return json.loads(lines[-1])


# ================================================================ 本地管理 API 桩
class FakeLiteLLMState:
    """伪 LiteLLM 管理 API 的状态：deployments + 调用记录 + 故障注入。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.deployments = {}          # id → {model_name, litellm_params, model_info}
        self.next_id = 1
        self.calls = []                # [{"method","path","payload","auth"}]
        self.expect_master = MASTER
        self.fail_counts = {}          # path → 剩余注入失败次数
        self.fail_status = 500
        self.info_404 = False
        self.mask_keys = False

    # -- 桩数据操作 --------------------------------------------------
    def seed(self, model_name, params, deployment_id=None):
        with self.lock:
            dep_id = deployment_id or "seed-%d" % self.next_id
            if deployment_id is None:
                self.next_id += 1
            self.deployments[dep_id] = {
                "model_name": model_name,
                "litellm_params": copy.deepcopy(params),
                "model_info": {"id": dep_id},
            }
            return dep_id

    def create(self, payload):
        with self.lock:
            for dep in self.deployments.values():
                if dep["model_name"] == payload.get("model_name"):
                    return dep
            dep_id = "dep-%d" % self.next_id
            self.next_id += 1
            dep = {
                "model_name": payload["model_name"],
                "litellm_params": copy.deepcopy(payload.get("litellm_params") or {}),
                # 真实 LiteLLM 会把请求里的 model_info 一并存下（除 id 由服务端给），
                # 桩也必须回传，否则「能力字段是否已收敛」这类断言永远判成 update。
                "model_info": dict(payload.get("model_info") or {}, id=dep_id),
            }
            self.deployments[dep_id] = dep
            return dep

    def update(self, payload):
        with self.lock:
            info = payload.get("model_info") or {}
            dep = self.deployments.get(info.get("id"))
            if dep is None:
                for candidate in self.deployments.values():
                    if candidate["model_name"] == payload.get("model_name"):
                        dep = candidate
                        break
            if dep is None:
                return False
            if payload.get("litellm_params"):
                dep["litellm_params"] = copy.deepcopy(payload["litellm_params"])
            if payload.get("model_name"):
                dep["model_name"] = payload["model_name"]
            if payload.get("model_info"):
                merged = dict(dep.get("model_info") or {})
                merged.update(payload["model_info"])
                dep["model_info"] = merged
            return True

    def delete(self, deployment_id):
        with self.lock:
            return self.deployments.pop(deployment_id, None) is not None

    def patch(self, model_id, payload):
        """PATCH /model/{id}/update：补丁语义，只更新给出的字段（真实 LiteLLM 行为）。"""
        with self.lock:
            dep = self.deployments.get(model_id)
            if dep is None:
                for candidate in self.deployments.values():
                    if (candidate.get("model_info") or {}).get("id") == model_id:
                        dep = candidate
                        break
            if dep is None:
                return False
            if payload.get("litellm_params"):
                dep["litellm_params"] = copy.deepcopy(payload["litellm_params"])
            if payload.get("model_name"):
                dep["model_name"] = payload["model_name"]
            if payload.get("model_info"):
                merged = dict(dep.get("model_info") or {})
                merged.update(payload["model_info"])
                dep["model_info"] = merged
            return True

    def info(self):
        with self.lock:
            rows = []
            for dep in self.deployments.values():
                params = copy.deepcopy(dep["litellm_params"])
                if self.mask_keys and "api_key" in params:
                    params["api_key"] = "sk-****"
                rows.append({
                    "model_name": dep["model_name"],
                    "litellm_params": params,
                    # 与真实 /model/info 一致：model_info 原样回传（含能力字段）。
                    "model_info": copy.deepcopy(dep["model_info"]),
                })
            return rows

    def record(self, method, path, payload, auth):
        with self.lock:
            self.calls.append({"method": method, "path": path,
                               "payload": copy.deepcopy(payload), "auth": auth})

    def post_calls(self, path=None):
        with self.lock:
            calls = list(self.calls)
        return [c for c in calls if c["method"] == "POST" and (path is None or c["path"] == path)]

    def get_calls(self):
        with self.lock:
            return [c for c in self.calls if c["method"] == "GET"]

    def names(self):
        with self.lock:
            return {dep["model_name"] for dep in self.deployments.values()}


class _FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {"_raw": raw.decode("utf-8", "replace")}

    def _send(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        fake = self.server.fake
        payload = self._read_body() if method in ("POST", "PATCH") else None
        auth = self.headers.get("Authorization")
        fake.record(method, self.path, payload, auth)
        if auth != "Bearer " + fake.expect_master:
            self._send(401, {"error": "unauthorized"})
            return
        if fake.info_404 and self.path == "/model/info":
            self._send(404, {"error": "not found"})
            return
        remaining = fake.fail_counts.get(self.path, 0)
        if remaining > 0:
            with fake.lock:
                fake.fail_counts[self.path] = remaining - 1
            self._send(fake.fail_status, {"error": "injected failure"})
            return
        if method == "GET" and self.path == "/model/info":
            self._send(200, {"data": fake.info()})
            return
        if method == "POST" and self.path == "/model/new":
            dep = fake.create(payload)
            self._send(200, {"model_name": dep["model_name"],
                             "model_info": dep["model_info"]})
            return
        if method == "POST" and self.path == "/model/update":
            ok = fake.update(payload)
            self._send(200 if ok else 400, {"ok": ok})
            return
        if method == "PATCH" and self.path.startswith("/model/") and self.path.endswith("/update"):
            model_id = self.path[len("/model/"):-len("/update")]
            ok = fake.patch(model_id, payload)
            self._send(200 if ok else 404, {"ok": ok})
            return
        if method == "POST" and self.path == "/model/delete":
            ok = fake.delete(payload.get("id"))
            self._send(200 if ok else 400, {"ok": ok})
            return
        self._send(404, {"error": "not found"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        # 2026-10-21：reconciler 更新通道改走 PATCH /model/{id}/update（旧 POST 不落 model_info）
        self._handle("PATCH")


class FakeAdminServer:
    """把 FakeLiteLLMState 挂到 127.0.0.1 的临时端口上（stdlib，无外部依赖）。"""

    def __init__(self):
        self.fake = FakeLiteLLMState()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
        self.httpd.fake = self.fake
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    @property
    def url(self):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    def close(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        self.thread.join(timeout=3)


# ================================================================ 基础用例
class PoolEnvCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="poolctl-")
        self._saved_env = {name: os.environ.get(name) for name in WATCHED_ENV}
        for name in WATCHED_ENV:
            os.environ.pop(name, None)
        os.environ["POOL_HOME"] = self.tmp
        # 本组用例验证**文件事实源**（load_config 的 TTL/force/fail-open 语义）。
        # 新版 pool_control 默认走 sqlite（pool_store.enabled() 默认 True），
        # 此处显式退回 json —— 与生产应急开关 POOL_STORE=json 是同一条路径。
        os.environ["POOL_STORE"] = "json"
        pool_control.invalidate_cache()
        self._servers = []
        # 默认 reconciler 环境：2 个会话桶，重试 2 次但退避 0（测试快）
        os.environ["POOL_SESSION_BUCKETS"] = "2"
        os.environ["POOL_RECONCILE_RETRIES"] = "2"
        os.environ["POOL_RECONCILE_BACKOFF"] = "0"
        os.environ["POOL_RECONCILE_TIMEOUT"] = "3"

    def tearDown(self):
        for server in self._servers:
            server.close()
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        pool_control.invalidate_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 路径/文件助手 -------------------------------------------------
    def path(self, name):
        return os.path.join(self.tmp, name)

    def write_text(self, name, text):
        with open(self.path(name), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return self.path(name)

    def read_text(self, name):
        with open(self.path(name), "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def write_control(self, doc):
        pool_control.atomic_write_json(self.path("pool-config.json"), doc)
        pool_control.invalidate_cache()

    def write_control_raw(self, doc):
        """只写盘、不动缓存（TTL 缓存断言用）。"""
        pool_control.atomic_write_json(self.path("pool-config.json"), doc)

    def default_control(self, allow=("mimo-v2.6-flash",), accounts=None, mode="full"):
        if accounts is None:
            accounts = [{"slot": 2, "key_var": "OCG_KEY_2",
                         "email_var": "OCG_EMAIL_2", "enabled": True}]
        return {
            "version": 1,
            "models": {"allow": list(allow)},
            "accounts": accounts,
            "affinity_mode": mode,
            "updated_at": None,
            "updated_by": None,
        }

    def write_control_default(self, **kwargs):
        pool_control.invalidate_cache()
        self.write_control(self.default_control(**kwargs))

    def write_default_env(self, key=UPSTREAM_KEY):
        self.write_text(".env", "LITELLM_MASTER_KEY=%s\n"
                                "OCG_KEY_2=%s\n"
                                "OCG_EMAIL_2=a2@example.com\n" % (MASTER, key))

    def start_server(self):
        server = FakeAdminServer().start()
        self._servers.append(server)
        os.environ["POOL_LITELLM_URL"] = server.url
        return server

    def audit_lines(self):
        path = pool_control.audit_path()
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def audit_text(self):
        path = pool_control.audit_path()
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def backups(self, stem=".env.bak-"):
        return [name for name in os.listdir(self.tmp) if name.startswith(stem)]

    def assert_mode_0600(self, path):
        """POSIX 严格断言 0600；Windows 的 chmod 只有只读位，无法断言，跳过该断言。"""
        if IS_WINDOWS:
            return
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600, "权限应为 0600，实际 %o: %s" % (mode, path))


# ================================================================ A/B. 控制文件 + 缓存
class ControlFileTests(PoolEnvCase):
    def test_atomic_write_json_and_permissions(self):
        target = self.path("pool-config.json")
        pool_control.atomic_write_json(target, {"version": 1, "models": {"allow": ["m"]}})
        with open(target, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"version": 1, "models": {"allow": ["m"]}})
        self.assert_mode_0600(target)
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".pool-tmp-")]
        self.assertEqual(leftovers, [], "原子写不应残留临时文件: %s" % leftovers)

    def test_atomic_write_cleanup_on_serialization_error(self):
        target = self.path("pool-config.json")
        pool_control.atomic_write_json(target, {"ok": 1})
        with self.assertRaises(TypeError):
            pool_control.atomic_write_json(target, {"bad": object()})
        with open(target, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"ok": 1}, "失败写入不应破坏旧文件")
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".pool-tmp-")]
        self.assertEqual(leftovers, [], "序列化失败也应清理临时文件")

    def test_ttl_cache_expiry_and_force(self):
        clock = {"now": 1000.0}
        original_now = pool_control._now
        pool_control._now = lambda: clock["now"]
        try:
            self.write_control_raw({"version": 1, "models": {"allow": ["a"]}})
            pool_control.invalidate_cache()
            self.assertEqual(tuple(pool_control.load_config(force=True)["models"]["allow"]), ("a",))
            # TTL 内直接改盘：读侧仍返回缓存值（默认 2s）
            self.write_control_raw({"version": 1, "models": {"allow": ["b"]}})
            self.assertEqual(tuple(pool_control.load_config()["models"]["allow"]), ("a",))
            # 超过默认 2s：首个调用重新读盘
            clock["now"] += 2.01
            self.assertEqual(tuple(pool_control.load_config()["models"]["allow"]), ("b",))
            # force=True 立即重读
            self.write_control_raw({"version": 1, "models": {"allow": ["c"]}})
            self.assertEqual(tuple(pool_control.load_config()["models"]["allow"]), ("b",))
            self.assertEqual(tuple(pool_control.load_config(force=True)["models"]["allow"]), ("c",))
        finally:
            pool_control._now = original_now
            pool_control.invalidate_cache()

    def test_ttl_env_override(self):
        os.environ["POOL_CONFIG_TTL"] = "10"
        clock = {"now": 2000.0}
        original_now = pool_control._now
        pool_control._now = lambda: clock["now"]
        try:
            self.write_control_raw({"version": 1, "models": {"allow": ["a"]}})
            pool_control.invalidate_cache()
            pool_control.load_config(force=True)
            self.write_control_raw({"version": 1, "models": {"allow": ["b"]}})
            clock["now"] += 5.0
            self.assertEqual(tuple(pool_control.load_config()["models"]["allow"]), ("a",),
                             "POOL_CONFIG_TTL=10 时 5s 内应命中缓存")
            clock["now"] += 5.5
            self.assertEqual(tuple(pool_control.load_config()["models"]["allow"]), ("b",))
        finally:
            pool_control._now = original_now
            pool_control.invalidate_cache()

    def test_write_side_invalidates_cache_immediately(self):
        self.write_control_default()
        first = pool_control.load_config(force=True)
        self.assertEqual(tuple(first["models"]["allow"]), ("mimo-v2.6-flash",))
        pool_control.update_models(["mimo-v2.6-pro"], actor="tester")
        self.assertEqual(tuple(pool_control.load_config()["models"]["allow"]), ("mimo-v2.6-pro",),
                         "写侧应让本进程缓存立即失效")

    def test_fail_open_none_on_missing_or_corrupt(self):
        self.assertIsNone(pool_control.load_config(force=True), "文件缺失应返回 None")
        self.assertTrue(pool_control.last_error())
        self.write_text("pool-config.json", "{not json")
        self.assertIsNone(pool_control.load_config(force=True), "坏 JSON 应返回 None")
        self.assertIn("JSONDecodeError", pool_control.last_error() or "")
        self.write_text("pool-config.json", "[1,2,3]")
        self.assertIsNone(pool_control.load_config(force=True), "顶层非 object 应返回 None")
        self.write_control({"version": 1, "models": {"allow": []}})
        cfg = pool_control.load_config(force=True)
        self.assertIsNotNone(cfg, "合法但空 allowlist 的文件应可读")
        self.assertEqual(tuple(cfg["models"]["allow"]), ())

    def test_snapshot_is_immutable(self):
        self.write_control_default()
        cfg = pool_control.load_config(force=True)
        self.assertIsInstance(cfg, type(pool_control.freeze({})))
        self.assertIsInstance(cfg["models"]["allow"], tuple)
        with self.assertRaises(TypeError):
            cfg["version"] = 2
        with self.assertRaises(TypeError):
            cfg["models"]["allow"] = ("x",)
        with self.assertRaises(AttributeError):
            cfg["models"]["allow"].append("x")
        thawed = pool_control.thaw(cfg)
        thawed["models"]["allow"].append("x")  # thaw 后可改
        self.assertEqual(len(cfg["models"]["allow"]), 1)

    def test_poolless_path_override(self):
        override = self.path("custom-dir/control.json")
        os.environ["POOL_CONFIG_PATH"] = override
        pool_control.update_models(["mimo-v2.6-flash"], actor="tester")
        self.assertTrue(os.path.exists(override), "POOL_CONFIG_PATH 应生效")
        self.assertFalse(os.path.exists(self.path("pool-config.json")))
        os.environ.pop("POOL_CONFIG_PATH")
        pool_control.invalidate_cache()
        self.assertIsNone(pool_control.load_config(force=True))


# ================================================================ C. 写侧 + 审计
class WriteApiTests(PoolEnvCase):
    def test_update_models_and_accounts(self):
        created = pool_control.update_models(["mimo-v2.6-flash", "deepseek-v4.1-flash"], actor="tester")
        self.assertEqual(created["updated_by"], "tester")
        self.assertTrue(created["updated_at"])
        cfg = pool_control.load_config(force=True)
        self.assertEqual(tuple(cfg["models"]["allow"]), ("mimo-v2.6-flash", "deepseek-v4.1-flash"))

        accounts = [
            {"slot": 2, "key_var": "OCG_KEY_2", "email_var": "OCG_EMAIL_2", "enabled": True},
            {"slot": 5, "key_var": "OCG_KEY_5", "enabled": False},
        ]
        pool_control.update_accounts(accounts, actor="tester")
        cfg = pool_control.load_config(force=True)
        self.assertEqual(tuple(a["slot"] for a in cfg["accounts"]), (2, 5))
        self.assertNotIn("email_var", cfg["accounts"][1], "未提供的 email_var 不应凭空出现")
        actions = [line["action"] for line in self.audit_lines()]
        self.assertIn("models.update", actions)
        self.assertIn("accounts.update", actions)

    def test_email_alias_accepted_but_real_email_rejected(self):
        result = pool_control.update_accounts(
            [{"slot": 1, "key_var": "OCG_KEY_1", "email": "OCG_EMAIL_1", "enabled": True}],
            actor="tester")
        self.assertEqual(result["accounts"][0]["email_var"], "OCG_EMAIL_1")
        with self.assertRaises(ValueError):
            pool_control.update_accounts(
                [{"slot": 1, "key_var": "OCG_KEY_1", "email": "a1@example.com", "enabled": True}],
                actor="tester")

    def test_validation_rejects_bad_inputs(self):
        self.write_control_default()
        before = self.read_text("pool-config.json")
        with self.assertRaises(ValueError):
            pool_control.update_models(["bad model with space"], actor="tester")
        with self.assertRaises(ValueError):
            pool_control.update_models([123], actor="tester")
        with self.assertRaises(ValueError):
            pool_control.update_accounts([{"slot": 0, "key_var": "OCG_KEY_0"}], actor="tester")
        with self.assertRaises(ValueError):
            pool_control.update_accounts(
                [{"slot": 2, "key_var": "OCG_KEY_3", "enabled": True}], actor="tester")
        with self.assertRaises(ValueError):
            # 未知字段：防止看板把密钥值误写进控制文件
            pool_control.update_accounts(
                [{"slot": 2, "key_var": "OCG_KEY_2", "enabled": True,
                  "api_key": "sk-should-never-be-written"}], actor="tester")
        with self.assertRaises(ValueError):
            pool_control.update_accounts(
                [{"slot": 2, "key_var": "OCG_KEY_2", "enabled": "yes"}], actor="tester")
        with self.assertRaises(ValueError):
            pool_control.set_affinity_mode("aggressive", actor="tester")
        self.assertEqual(self.read_text("pool-config.json"), before, "非法输入不应改动控制文件")
        self.assertFalse(os.path.exists(self.path("routing-share.json")))

    def test_set_affinity_mode_writes_control_and_compat_mirror(self):
        self.write_control_default(mode="full")
        pool_control.set_affinity_mode("shadow", actor="tester")
        cfg = pool_control.load_config(force=True)
        self.assertEqual(cfg["affinity_mode"], "shadow")
        mirror = self.path("pool-affinity.json")
        self.assertTrue(os.path.exists(mirror), "旧 sidecar 兼容镜像应存在")
        with open(mirror, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"mode": "shadow"})
        self.assertFalse(os.path.exists(self.path("routing-share.json")),
                         "不得写份额引擎产物 routing-share.json")
        self.assert_mode_0600(mirror)

    def test_audit_redaction_never_leaks_secret(self):
        self.write_control_default()
        pool_control.update_models(["mimo-v2.6-flash"], actor="tester")
        result = pool_control.write_env_value("OCG_KEY_2", UPSTREAM_KEY2, actor="tester")
        self.assertNotIn(UPSTREAM_KEY2, json.dumps(result, ensure_ascii=False), "返回值不得含明文密钥")
        pool_control.append_audit("admin.login", {"password": "p@ss-word", "ok": True}, actor="tester")
        text = self.audit_text()
        self.assertNotIn(UPSTREAM_KEY2, text, "审计不得出现明文密钥")
        self.assertNotIn("p@ss-word", text, "敏感字段名应自动转指纹")
        self.assertIn("OCG_KEY_2", text, "审计应记录变量名")
        self.assertIn(pool_control.fingerprint(UPSTREAM_KEY2), text, "审计应记录 sha256 指纹")
        self.assertIn(pool_control.fingerprint("p@ss-word"), text)
        for line in self.audit_lines():
            self.assertIn("ts", line)
            self.assertIn("action", line)
        self.assert_mode_0600(pool_control.audit_path())

    def test_concurrent_updates_serialized_by_lock(self):
        self.write_control_default()
        errors = []

        def worker(index):
            try:
                for _ in range(3):
                    pool_control.update_models(["model-%d" % index], actor="w%d" % index)
            except Exception as exc:  # pragma: no cover - 失败时给出信息
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        cfg = pool_control.load_config(force=True)
        self.assertEqual(len(cfg["models"]["allow"]), 1, "RMW 不应丢字段/写坏 JSON")
        self.assertTrue(cfg["models"]["allow"][0].startswith("model-"))
        lines = self.audit_lines()
        self.assertEqual(len(lines), 15)
        for line in lines:
            self.assertEqual(line["action"], "models.update")

    def test_lock_excludes_reentry(self):
        lock_path = self.path("demo.lock")
        with pool_control.file_lock(lock_path):
            with self.assertRaises(TimeoutError):
                with pool_control.file_lock(lock_path, blocking=False):
                    pass


# ================================================================ D. .env 原子写
class EnvWriteTests(PoolEnvCase):
    def test_replace_preserves_other_lines_and_order(self):
        original = ("# 注释行\n"
                    "OTHER_VAR=keep-me\n"
                    "OCG_KEY_2=\"%s\"\n"
                    "\n"
                    "EMPTY=\n"
                    "OCG_KEY_3=three\n" % UPSTREAM_KEY)
        self.write_text(".env", original)
        result = pool_control.write_env_value("OCG_KEY_2", UPSTREAM_KEY2, actor="tester")
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertNotIn(UPSTREAM_KEY2, json.dumps(result, ensure_ascii=False))
        text = self.read_text(".env")
        self.assertEqual(text, ("# 注释行\n"
                                "OTHER_VAR=keep-me\n"
                                "OCG_KEY_2=\"%s\"\n"
                                "\n"
                                "EMPTY=\n"
                                "OCG_KEY_3=three\n" % UPSTREAM_KEY2),
                         ".env 只应改目标行，其它行/行序必须逐字节不变")
        self.assert_mode_0600(self.path(".env"))
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        with open(self.path(backups[0]), "r", encoding="utf-8", newline="") as fh:
            self.assertEqual(fh.read(), original, "备份必须是写前完整内容")

    def test_append_missing_var_and_missing_trailing_newline(self):
        self.write_text(".env", "A=1")  # 故意无结尾换行
        pool_control.write_env_value("B", "two")
        self.assertEqual(self.read_text(".env"), "A=1\nB=two\n")
        # 文件不存在也能创建（先清掉上一段产生的备份，避免误判）
        os.remove(self.path(".env"))
        for name in self.backups():
            os.remove(self.path(name))
        pool_control.write_env_value("C", "three")
        self.assertEqual(self.read_text(".env"), "C=three\n")
        self.assertEqual(self.backups(), [], "新建文件不应产生备份")

    def test_unchanged_value_does_not_touch_disk(self):
        self.write_text(".env", "OCG_KEY_2=%s\n" % UPSTREAM_KEY)
        before = self.read_text(".env")
        backups_before = self.backups()
        result = pool_control.write_env_value("OCG_KEY_2", UPSTREAM_KEY, actor="tester")
        self.assertFalse(result["changed"])
        self.assertEqual(self.read_text(".env"), before)
        self.assertEqual(self.backups(), backups_before)
        self.assertEqual(self.audit_lines(), [], "值未变不应产生审计")

    def test_invalid_values_rejected(self):
        with self.assertRaises(ValueError):
            pool_control.write_env_value("OCG_KEY_2", "line1\nline2")
        with self.assertRaises(ValueError):
            pool_control.write_env_value("OCG_KEY_2", "line1\rline2")
        with self.assertRaises(ValueError):
            pool_control.write_env_value("OCG_KEY_2", "a\x00b")
        with self.assertRaises(ValueError):
            pool_control.write_env_value("bad name", "x")
        with self.assertRaises(ValueError):
            pool_control.write_env_value("OCG_KEY_2", 123)

    def test_write_prints_no_secret(self):
        self.write_text(".env", "OCG_KEY_2=old\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            pool_control.write_env_value("OCG_KEY_2", UPSTREAM_KEY2, actor="tester")
        self.assertNotIn(UPSTREAM_KEY2, out.getvalue() + err.getvalue())

    def test_remove_env_value(self):
        original = "A=1\nOCG_KEY_9=sk-old\nB=2\n"
        self.write_text(".env", original)
        result = pool_control.remove_env_value("OCG_KEY_9", actor="tester")
        self.assertTrue(result["removed"])
        self.assertNotIn("sk-old", json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.read_text(".env"), "A=1\nB=2\n")
        self.assertEqual(len(self.backups()), 1)
        again = pool_control.remove_env_value("OCG_KEY_9", actor="tester")
        self.assertFalse(again["removed"])
        self.assertEqual(len(self.backups()), 1, "未命中不应再写盘/备份")

    def test_read_env_and_fingerprints(self):
        self.write_text(".env", "LITELLM_MASTER_KEY=%s\nOCG_KEY_2=\"%s\"\nOCG_EMAIL_2=a2@x\n"
                                 "OTHER=1\n" % (MASTER, UPSTREAM_KEY))
        env = pool_control.read_env()
        self.assertEqual(env["OCG_KEY_2"], UPSTREAM_KEY, "应去掉引号（与 gen_config 解析一致）")
        fps = pool_control.read_env_fingerprints(["OCG_KEY_2", "MISSING"])
        self.assertEqual(fps["OCG_KEY_2"], pool_control.mask_fingerprint(UPSTREAM_KEY))
        self.assertNotIn("MISSING", fps)
        self.assertNotIn(UPSTREAM_KEY, json.dumps(fps))
        all_fps = pool_control.read_env_fingerprints()
        self.assertEqual(set(all_fps), {"OCG_KEY_2"})


# ================================================================ E. reconciler 纯逻辑
class ReconcilerLogicTests(PoolEnvCase):
    def test_desired_deployments_match_gen_config_conventions(self):
        os.environ["POOL_SESSION_BUCKETS"] = "2"
        config = pool_control.freeze(self.default_control())
        env = {"OCG_KEY_2": UPSTREAM_KEY, "OCG_EMAIL_2": "a2@x"}
        desired = pool_reconciler.build_desired(config, env)
        # 键是池内唯一标识：兜底 `<model>|<session>`；别名 `<model>@a<N>s<K>` 名字天然唯一
        self.assertEqual(set(desired), {
            "mimo-v2.6-flash|dsh-acct-2",
            "mimo-v2.6-flash@a2s1",
            "mimo-v2.6-flash@a2s2",
        })
        base = desired["mimo-v2.6-flash|dsh-acct-2"]["litellm_params"]
        # 2026-10-28 出口拆分：非 muse 走直连反代，api_base/协议前缀对齐路由函数即可
        expected_prefix, expected_base = pool_reconciler._route_of("mimo-v2.6-flash")
        self.assertEqual(base["model"], "%s/mimo-v2.6-flash" % expected_prefix)
        self.assertEqual(base["api_base"], expected_base)
        self.assertEqual(base["api_key"], UPSTREAM_KEY, "热路径必须用字面量 key（容器 env 固化）")
        self.assertEqual(base["extra_headers"]["user-agent"], pool_reconciler.POOL_USER_AGENT)
        self.assertEqual(base["extra_headers"]["x-opencode-session"], "dsh-acct-2")
        alias = desired["mimo-v2.6-flash@a2s2"]["litellm_params"]
        self.assertEqual(alias["extra_headers"]["x-opencode-session"], "dsh-acct-2-s2")

    def test_same_named_fallbacks_are_not_collapsed(self):
        """gen_config 为**每个账号**生成一条同名兜底 <model>；identity 键必须保留全部。

        这是 2026-10-17 对齐修掉的真实缺陷：用 model_name 当键会把 4 条同名兜底折叠成 1 条，
        期望态 165 ≠ 现网 180，dry-run 还会误判 update。
        """
        config = pool_control.freeze({
            "version": 1, "models": {"allow": ["m1"]},
            "accounts": [{"slot": s, "key_var": "OCG_KEY_%d" % s, "enabled": True}
                         for s in (2, 4, 5, 6)],
        })
        env = {"OCG_KEY_%d" % s: UPSTREAM_KEY for s in (2, 4, 5, 6)}
        desired = pool_reconciler.build_desired(config, env, buckets=8)
        self.assertEqual(len(desired), 4 * (1 + 8), "4 账号 × (1 兜底 + 8 桶)")
        fallbacks = [d for d in desired.values() if "@" not in d["model_name"]]
        self.assertEqual(len(fallbacks), 4, "同名兜底被折叠了")
        self.assertEqual(
            {d["litellm_params"]["extra_headers"]["x-opencode-session"] for d in fallbacks},
            {"dsh-acct-2", "dsh-acct-4", "dsh-acct-5", "dsh-acct-6"})

    def test_capability_probe_requires_write_path(self):
        """只探读是不够的：LiteLLM 未接 DB 时读正常、写 500 `No DB Connected`。

        真实踩过：旧 --check 只探 GET /model/info，于是把"热路径可用"报成 true，
        但真正 apply 时 /model/update 直接 500，热更新其实是假的。
        """
        class _Stub:
            def __init__(self, list_exc=None, update_exc=None, entries=180):
                self.list_exc, self.update_exc, self.entries = list_exc, update_exc, entries

            def list_deployments(self):
                if self.list_exc:
                    raise self.list_exc
                return [{"model_name": "m%d" % i} for i in range(self.entries)]

            def update_model(self, payload):  # noqa: ARG002 - 探针不关心载荷
                if self.update_exc:
                    raise self.update_exc
                return {}

        ok, reason = pool_reconciler.probe_capability(_Stub())
        self.assertTrue(ok)
        self.assertIn("写接口可用", reason)

        no_db = pool_reconciler.AdminApiError(
            "管理 API POST /model/update 返回 HTTP 500: "
            '{"error":{"message":"No DB Connected. Here\'s how to do it - .../virtual_keys"}}')
        ok, reason = pool_reconciler.probe_capability(_Stub(update_exc=no_db))
        self.assertFalse(ok, "未接 DB 时必须报不可用（否则热更新是假的）")
        self.assertIn("DATABASE_URL", reason)

        not_found = pool_reconciler.AdminApiError(
            "管理 API POST /model/update 返回 HTTP 400: model not found")
        ok, reason = pool_reconciler.probe_capability(_Stub(update_exc=not_found))
        self.assertTrue(ok, "非 DB 类写错误说明写接口本身可响应")

        down = pool_reconciler.AdminApiUnavailable("连不上 127.0.0.1:4000")
        ok, reason = pool_reconciler.probe_capability(_Stub(update_exc=down))
        self.assertFalse(ok)
        ok, reason = pool_reconciler.probe_capability(_Stub(list_exc=down))
        self.assertFalse(ok)

    def test_anthropic_route_matches_gen_config(self):
        config = pool_control.freeze({
            "version": 1, "models": {"allow": ["claude-haiku-5-5"]},
            "accounts": [{"slot": 5, "key_var": "OCG_KEY_5", "enabled": True}],
        })
        desired = pool_reconciler.build_desired(config, {"OCG_KEY_5": "k5"}, buckets=1)
        params = desired["claude-haiku-5-5|dsh-acct-5"]["litellm_params"]
        # 2026-10-28 出口拆分后 api_base 不再恒为规范 Go 端点：非 muse 走直连反代
        # （DIRECT_ANTHROPIC），muse 才走 UPSTREAM_ANTHROPIC。直接对齐路由函数，
        # 避免测试再次写死而落后于 gen_config.py。
        expected_prefix, expected_base = pool_reconciler._route_of("claude-haiku-5-5")
        self.assertEqual(params["model"], "%s/claude-haiku-5-5" % expected_prefix)
        self.assertEqual(params["api_base"], expected_base)

    def test_gen_config_source_still_matches_our_constants(self):
        """静态守护：命名/UA/路由常量必须与 gen_config.py 保持同步（只读校验，不改它）。"""
        gen_config_path = os.path.join(POOL_DIR, "gen_config.py")
        if not os.path.exists(gen_config_path):
            self.skipTest("gen_config.py 不在仓库内")
        with open(gen_config_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("@a{acct_idx}s{bucket}", source, "别名命名规则变了，需要同步 reconciler")
        self.assertIn("dsh-acct-{acct_idx}-s{bucket}", source, "会话桶 session id 规则变了")
        self.assertIn("dsh-opencodego-pool/1.0", source, "UA 变了，需要同步 reconciler")
        self.assertIn("https://opencode.ai/inference/go/anthropic", source)
        self.assertIn("claude-haiku-5-5", source)

    def test_desired_safety_gates(self):
        env = {"OCG_KEY_2": UPSTREAM_KEY}
        with self.assertRaises(pool_reconciler.ConfigError):
            pool_reconciler.build_desired(None, env)
        with self.assertRaises(pool_reconciler.ConfigError):
            pool_reconciler.build_desired(
                pool_control.freeze(self.default_control(allow=())), env)
        with self.assertRaises(pool_reconciler.ConfigError):
            pool_reconciler.build_desired(
                pool_control.freeze(self.default_control(accounts=[])), env)
        disabled = [{"slot": 2, "key_var": "OCG_KEY_2", "enabled": False}]
        with self.assertRaises(pool_reconciler.ConfigError):
            pool_reconciler.build_desired(
                pool_control.freeze(self.default_control(accounts=disabled)), env)
        with self.assertRaises(pool_reconciler.ConfigError) as ctx:
            pool_reconciler.build_desired(
                pool_control.freeze(self.default_control()), {"OCG_KEY_2": "REPLACE_ME"})
        self.assertIn("OCG_KEY_2", str(ctx.exception))
        # 缺少密钥变量：明确点名变量，拒绝半池
        with self.assertRaises(pool_reconciler.ConfigError) as ctx:
            pool_reconciler.build_desired(pool_control.freeze(self.default_control()), {})
        self.assertIn("OCG_KEY_2", str(ctx.exception))

    def test_plan_diff_add_update_delete_and_idempotence(self):
        os.environ["POOL_SESSION_BUCKETS"] = "1"
        config = pool_control.freeze(self.default_control(allow=("m1",)))
        desired = pool_reconciler.build_desired(config, {"OCG_KEY_2": UPSTREAM_KEY}, buckets=1)
        # 现状为空 → 全部新增
        plan = pool_reconciler.plan_actions(desired, [])
        self.assertEqual(plan_summary_actions(plan), {"add": 2, "update": 0, "delete": 0})
        keys = list(desired)                      # identity 键

        def current_from(desired_map):
            """构造与期望一致的"远端现状"（LiteLLM /model/info 真实形态：不含 api_key）。"""
            out = []
            for key, dep in desired_map.items():
                params = copy.deepcopy(dep["litellm_params"])
                params.pop("api_key", None)
                out.append({"model_name": dep["model_name"],
                            "model_info": {"id": "id-" + key},
                            "litellm_params": params})
            return out

        # 与期望一致的现状 → 无动作（幂等）
        current = current_from(desired)
        plan = pool_reconciler.plan_actions(desired, current)
        self.assertTrue(pool_reconciler.plan_is_empty(plan), plan)
        self.assertEqual(plan["unchanged"], 2)
        # 改一个受管字段 → update（identity 仍匹配）
        current[0]["litellm_params"]["api_base"] = "https://wrong.example"
        plan = pool_reconciler.plan_actions(desired, current)
        self.assertEqual(len(plan["updates"]), 1)
        self.assertIn("params.api_base", plan["updates"][0]["reasons"])
        self.assertEqual(plan["updates"][0]["identity"], keys[0])
        # 远端明文 key 与期望不同 → update
        current = current_from(desired)
        current[0]["litellm_params"]["api_key"] = "sk-OLD"
        plan = pool_reconciler.plan_actions(desired, current)
        self.assertIn("api_key.rotated", plan["updates"][0]["reasons"])
        # 远端脱敏（真实形态：字段直接不返回）+ 状态指纹匹配 → 不变
        masked_current = current_from(desired)
        state = {k: {"key_fp": pool_control.fingerprint(UPSTREAM_KEY)} for k in desired}
        plan = pool_reconciler.plan_actions(desired, masked_current, state=state)
        self.assertEqual(len(plan["updates"]), 0)
        # 状态指纹陈旧（说明 .env 换过 key）→ 判定已轮换
        stale = {k: {"key_fp": pool_control.fingerprint("sk-OLD-KEY")} for k in desired}
        plan = pool_reconciler.plan_actions(desired, masked_current, state=stale)
        self.assertEqual(len(plan["updates"]), 2)
        # 首轮无状态：无法证明轮换 → 不误判（否则首跑会把整池重写一遍）
        plan = pool_reconciler.plan_actions(desired, masked_current, state={})
        self.assertEqual(len(plan["updates"]), 0)
        # force_keys：显式强制下发
        plan = pool_reconciler.plan_actions(desired, masked_current, state=state, force_keys=True)
        self.assertEqual(len(plan["updates"]), 2)
        current = current_from(desired)
        # 多出来的号池标记 deployment → delete；非号池 → ignored
        managed_extra = {
            "model_name": "m1@a2s9", "model_info": {"id": "extra"},
            "litellm_params": {"model": "openai/m1", "api_base": pool_reconciler.UPSTREAM_OPENAI,
                               "api_key": "sk-x",
                               "extra_headers": {"user-agent": pool_reconciler.POOL_USER_AGENT,
                                                 "x-opencode-session": "dsh-acct-2-s9"}}}
        unmanaged = {"model_name": "other-client-model", "model_info": {"id": "foreign"},
                     "litellm_params": {"model": "openai/x", "api_base": "https://example.com"}}
        plan = pool_reconciler.plan_actions(desired, current + [managed_extra, unmanaged])
        self.assertEqual([d["model_name"] for d in plan["deletes"]], ["m1@a2s9"])
        self.assertEqual(plan["ignored"], 1)

    def test_plan_actions_json_has_no_secret(self):
        desired = pool_reconciler.build_desired(
            pool_control.freeze(self.default_control()), {"OCG_KEY_2": UPSTREAM_KEY}, buckets=1)
        plan = pool_reconciler.plan_actions(desired, [])
        rendered = json.dumps(pool_reconciler.plan_actions_json(plan))
        self.assertNotIn(UPSTREAM_KEY, rendered)
        self.assertIn(pool_control.fingerprint(UPSTREAM_KEY), rendered)


def plan_summary_actions(plan):
    summary = pool_reconciler.plan_summary(plan)
    return {"add": summary["add"], "update": summary["update"], "delete": summary["delete"]}


# ================================================================ F. reconciler 端到端（本地桩）
class ReconcilerFakeApiTests(PoolEnvCase):
    def setUp(self):
        super().setUp()
        self.server = self.start_server()
        self.write_control_default()
        self.write_default_env()

    def run_once(self, argv=("--once",)):
        return run_reconciler(list(argv))

    def test_check_reports_hot_path_available(self):
        rc, out, err = run_reconciler(["--check"])
        self.assertEqual(rc, 0, "可用时应 rc=0")
        payload = last_json(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["hot_path"], "available")
        self.assertIn("热路径可用", err)

    def test_check_reports_404_unavailable_with_fallback(self):
        self.server.fake.info_404 = True
        rc, out, err = run_reconciler(["--check"])
        self.assertNotEqual(rc, 0, "404 必须非 0，不能静默假装成功")
        payload = last_json(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["hot_path"], "unavailable")
        self.assertIn("gen_config.py", payload["fallback"])
        self.assertIn("force-recreate", payload["fallback"])
        self.assertIn("热路径不可用", err)
        self.assertIn("gen_config.py", err)
        self.assertNotIn("热路径可用", err)

    def test_check_reports_no_permission(self):
        self.write_text(".env", "LITELLM_MASTER_KEY=sk-master-WRONG\nOCG_KEY_2=%s\n" % UPSTREAM_KEY)
        rc, out, err = run_reconciler(["--check"])
        self.assertNotEqual(rc, 0)
        payload = last_json(out)
        self.assertEqual(payload["hot_path"], "unavailable")
        self.assertIn("无权限", payload["reason"])
        self.assertIn("force-recreate", err)

    def test_once_adds_all_deployments_then_idempotent(self):
        rc, out, err = self.run_once()
        self.assertEqual(rc, 0, err)
        payload = last_json(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["add"], 3, "1 模型 × 1 账号 × (1+2 桶)")
        self.assertEqual(self.server.fake.names(), {
            "mimo-v2.6-flash", "mimo-v2.6-flash@a2s1", "mimo-v2.6-flash@a2s2"})
        for dep in self.server.fake.deployments.values():
            self.assertEqual(dep["litellm_params"]["api_key"], UPSTREAM_KEY,
                             "热路径应把 .env 的字面量 key 推给 LiteLLM")
        # 鉴权头正确
        for call in self.server.fake.calls:
            self.assertEqual(call["auth"], "Bearer " + MASTER)
        # 状态文件：0600、只有指纹、无明文
        state_text = self.read_text(pool_reconciler.STATE_FILE)
        self.assertNotIn(UPSTREAM_KEY, state_text)
        self.assertIn("sha256:", state_text)
        self.assert_mode_0600(self.path(pool_reconciler.STATE_FILE))
        # 审计
        self.assertIn("reconcile.apply", [line["action"] for line in self.audit_lines()])

        post_before = len(self.server.fake.post_calls())
        rc, out, err = self.run_once()
        self.assertEqual(rc, 0, err)
        payload = last_json(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"], {"add": 0, "update": 0, "delete": 0,
                                              "unchanged": 3, "ignored": 0})
        self.assertEqual(len(self.server.fake.post_calls()), post_before, "收敛后不应再写")

    def test_dry_run_makes_no_writes(self):
        rc, out, err = self.run_once(["--dry-run"])
        self.assertEqual(rc, 0, err)
        payload = last_json(out)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["summary"]["add"], 3)
        self.assertEqual(len(payload["actions"]), 3)
        self.assertTrue(all(action["op"] == "add" for action in payload["actions"]))
        self.assertEqual(self.server.fake.post_calls(), [], "dry-run 不得调用写接口")
        self.assertFalse(os.path.exists(self.path(pool_reconciler.STATE_FILE)))
        self.assertNotIn(UPSTREAM_KEY, out, "dry-run 输出不得含明文密钥")
        self.assertIn("仅预览", err)

    def test_key_rotation_updates_existing_deployments(self):
        rc, out, _ = self.run_once()
        self.assertEqual(rc, 0)
        pool_control.write_env_value("OCG_KEY_2", UPSTREAM_KEY2, actor="tester")
        rc, out, err = self.run_once()
        self.assertEqual(rc, 0, err)
        payload = last_json(out)
        self.assertEqual(payload["summary"]["update"], 3, "换 key 应 update 该账号全部 deployment")
        self.assertEqual(payload["summary"]["add"], 0)
        for dep in self.server.fake.deployments.values():
            self.assertEqual(dep["litellm_params"]["api_key"], UPSTREAM_KEY2)
        # 再跑一轮：幂等
        rc, out, err = self.run_once()
        self.assertEqual(rc, 0, err)
        self.assertEqual(last_json(out)["summary"]["update"], 0)

    def test_masked_api_key_rotation_uses_state_fingerprint(self):
        self.server.fake.mask_keys = True
        rc, out, _ = self.run_once()
        self.assertEqual(rc, 0)
        self.assertEqual(self.server.fake.names(), {
            "mimo-v2.6-flash", "mimo-v2.6-flash@a2s1", "mimo-v2.6-flash@a2s2"})
        pool_control.write_env_value("OCG_KEY_2", UPSTREAM_KEY2, actor="tester")
        rc, out, err = self.run_once()
        self.assertEqual(rc, 0, err)
        payload = last_json(out)
        self.assertEqual(payload["summary"]["update"], 3,
                         "远端脱敏时应用状态文件指纹发现密钥轮换")
        # 远端仍是 mask，但状态已更新 → 再跑应幂等
        rc, out, err = self.run_once()
        self.assertEqual(rc, 0, err)
        self.assertEqual(last_json(out)["summary"]["update"], 0)

    def test_delete_extra_managed_but_keep_foreign(self):
        managed_params = {
            "model": "openai/mimo-v2.6-flash",
            "api_base": pool_reconciler.UPSTREAM_OPENAI,
            "api_key": UPSTREAM_KEY,
            "extra_headers": {"user-agent": pool_reconciler.POOL_USER_AGENT,
                              "x-opencode-session": "dsh-acct-2-s9"},
        }
        foreign_params = {"model": "openai/other", "api_base": "https://example.com",
                          "api_key": "sk-other"}
        self.server.fake.seed("mimo-v2.6-flash@a2s9", managed_params, "seed-managed")
        self.server.fake.seed("other-client-model", foreign_params, "seed-foreign")
        rc, out, err = self.run_once()
        self.assertEqual(rc, 0, err)
        summary = last_json(out)["summary"]
        self.assertEqual(summary["add"], 3)
        self.assertEqual(summary["delete"], 1, "多出来的号池标记 deployment 应删除")
        names = self.server.fake.names()
        self.assertIn("other-client-model", names, "非号池 deployment 不得误删")
        self.assertNotIn("mimo-v2.6-flash@a2s9", names)

    def test_missing_env_key_refuses_before_any_api_write(self):
        self.write_control_default(accounts=[
            {"slot": 7, "key_var": "OCG_KEY_7", "enabled": True}])
        self.write_default_env()
        rc, out, err = self.run_once()
        self.assertEqual(rc, 3, "配置错误退出码应为 3")
        payload = last_json(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "config")
        self.assertIn("OCG_KEY_7", payload["error"]["message"])
        self.assertIn("拒绝", err)
        self.assertEqual(self.server.fake.post_calls(), [])
        self.assertEqual(self.server.fake.get_calls(), [], "期望态非法时不应读现状/写接口")

    def test_empty_allowlist_refuses_to_wipe_pool(self):
        self.write_control_default(allow=())
        rc, out, err = self.run_once()
        self.assertEqual(rc, 3)
        payload = last_json(out)
        self.assertIn("拒绝", payload["error"]["message"])
        self.assertEqual(self.server.fake.post_calls(), [])
        self.assertEqual(self.server.fake.names(), set(), "绝不能把号池清空")

    def test_api_unavailable_on_reconcile_is_reported(self):
        self.server.fake.info_404 = True
        rc, out, err = self.run_once()
        self.assertEqual(rc, 2)
        payload = last_json(out)
        self.assertEqual(payload["hot_path"], "unavailable")
        self.assertIn("gen_config.py", payload["fallback"])
        self.assertIn("force-recreate", err)
        self.assertEqual(self.server.fake.post_calls(), [])

    def test_retry_is_bounded(self):
        self.server.fake.fail_counts["/model/new"] = 99  # 一直失败
        os.environ["POOL_RECONCILE_RETRIES"] = "2"
        rc, out, err = self.run_once()
        self.assertEqual(rc, 4, "应用失败应 rc=4")
        # 3 个新增 deployment × (1 次 + 2 次重试) = 9 次；重试次数必须有上限
        attempts = len(self.server.fake.post_calls("/model/new"))
        self.assertEqual(attempts, 9, "每个 op 最多 1+2 次尝试，实际 %d" % attempts)
        payload = last_json(out)
        self.assertFalse(payload["ok"])
        self.assertGreaterEqual(len(payload["errors"]), 1)
        self.assertIn("injected failure", payload["errors"][0]["error"])

    def test_lock_reentry_skips_without_api_calls(self):
        lock_path = self.path(pool_reconciler.LOCK_FILE)
        with pool_control.file_lock(lock_path):
            rc, out, err = self.run_once()
        self.assertEqual(rc, 0, "锁被占用应跳过本轮而不是失败")
        payload = last_json(out)
        self.assertEqual(payload.get("skipped"), "locked")
        self.assertEqual(self.server.fake.calls, [], "跳过时不应调用管理 API")
        self.assertIn("另一个 reconciler 实例", err)

    def test_apply_alias_equals_once(self):
        rc, out, err = self.run_once(["--apply"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(last_json(out)["summary"]["add"], 3)

    def test_watch_emits_one_json_per_cycle(self):
        stop = threading.Event()
        result = {}
        out, err = io.StringIO(), io.StringIO()

        def runner():
            result["rc"] = pool_reconciler.cmd_watch(0.05, stop_event=stop)

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            thread = threading.Thread(target=runner, daemon=True)
            thread.start()
            deadline = time.time() + 10
            while time.time() < deadline and len(self.server.fake.names()) < 3:
                time.sleep(0.05)
            stop.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "watch 应响应退出事件")
        self.assertEqual(result.get("rc"), 0)
        self.assertEqual(len(self.server.fake.names()), 3, "watch 模式应自发完成一轮收敛")
        payloads = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        self.assertGreaterEqual(len(payloads), 1, "watch 每轮应输出一段 JSON")
        self.assertEqual(payloads[0]["summary"]["add"], 3)
        self.assertIn("reconciler watch 启动", err.getvalue())


# ================================================================ 入口
if __name__ == "__main__":
    unittest.main(verbosity=2)
