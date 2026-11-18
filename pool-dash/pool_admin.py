#!/usr/bin/env python3
"""OpencodeLitellmPool 管理控制台后端（``/api/admin/*``）。

2026-10-17 改版：管理控制台不再是独立页面 ``/admin``，而是号池看板的一个页签。
本模块因此**只提供 ``/api/admin/*`` 接口**；页面骨架由 ``pool_admin_ui.build_panel()``
产出、由 ``dashboard.render_page()`` 注入看板。``/admin`` 已下线（404 并给出去处）。
接口本身仍保留 Host 白名单硬校验：并入页签后，写接口的暴露面没有任何放宽。

设计要点（与维护者裁定一致）：
- **不做鉴权 / 不做 CSRF / 不做 Halo 改造**：默认只监听 ``127.0.0.1``，远程访问由使用者
  自备反向代理/VPN（见页面顶部与 README）。仅保留最小服务端硬防护：
  ① ``Host`` 必须是 localhost/127.0.0.1/::1 或 ``POOL_ADMIN_HOSTS`` 白名单；
  ② 请求体 ≤ 64KB；③ 写接口只接受 ``application/json``；④ 方法白名单。
- **所有写操作四段式**：``preview``（diff + 影响面 + token）→ 用户输入 typed confirm
  （模型名/槽号/label/mode，必须与 ``confirmExpected`` 完全一致）→ ``apply`` → 结果反馈。
  token 单次有效、TTL 120s；同一配置重复预览返回"无变化"。
- **秒级 vs 热应用**：实例键（sidecar 每请求重读）与钉号模式（sidecar 5s TTL）同步执行、
  即时返回；模型 allowlist / 上游账号（.env + 控制文件）落盘后调 reconciler ``--once``
  热应用，走后台线程 + ``jobId`` 轮询，绝不阻塞请求线程。
- **依赖（由 hot-control 工位实现，本模块只调用）**：``pool/pool_control.py``
  （load_config / atomic_write_json / update_models / update_accounts /
  set_affinity_mode / write_env_value / read_env_fingerprints / append_audit /
  remove_env_value）与 ``pool/pool_reconciler.py``（``--check/--dry-run/--once``，
  stdout 结构化 JSON、stderr 诊断）。两者不可用 → 页面只读 + 明确错误，主看板照常。
- **密钥永不回显**：控制文件只存 ``key_var``；密钥值只经 ``write_env_value`` 写 ``.env``；
  列表只显示 ``read_env_fingerprints()`` 的指纹；实例键完整值只在签发响应里出现一次；
  审计 ``append_audit`` 自动脱敏，本模块传入的 detail 也只含指纹/尾号。
- **安全输出**：页面所有动态值经 ``pool_admin_ui.esc``；JSON 用 ``json.dumps``；
  model id 白名单 ``^[A-Za-z0-9._:-]{1,64}$``；label ``^[a-z0-9-]{1,32}$``；
  绝不把用户输入拼进 YAML/JSON 文本。

本模块可被 dashboard.py 以 ``import pool_admin; AdminApp()`` 挂载；导入失败时由
dashboard 侧降级（不挂路由，主看板不受影响）。
"""
from __future__ import annotations

import html
import hashlib
import importlib
import inspect
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

try:  # 页面模块与后端一起发布；缺失时仍可启动（页面返回最小错误页）
    import pool_admin_ui
except Exception:  # pragma: no cover - 打包异常兜底
    pool_admin_ui = None

PROJECT = "OpencodeLitellmPool"
ACTOR = "dashboard"
VERSION = "1.0.0"

MAX_BODY = 64 * 1024
PREVIEW_TTL = 120.0
MAX_PREVIEWS = 128
MAX_JOBS = 64
JOB_TTL = 3600.0

HOST_ALLOW = frozenset({"localhost", "127.0.0.1", "::1"})
MODEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
LABEL_RE = re.compile(r"^[a-z0-9-]{1,32}$")
EMAIL_RE = re.compile(r"^[^@\s<>]{1,64}@[^@\s<>]{1,190}$")
KEY_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{8,512}$")
SLOT_MIN, SLOT_MAX = 1, 999

# 脱敏：任何形似密钥的长串（sk-... / sk-pool-... / 32+ 位 hex）都折叠成指纹。
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9._~+/-]{8,}|[A-Fa-f0-9]{32,})")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _short(exc, limit=200):
    return str(exc).replace("\n", " ")[:limit] or exc.__class__.__name__


def _iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts or time.time()))


def _tail(value, n=4):
    s = str(value or "")
    return s[-n:] if len(s) >= n else s


def _fingerprint(value):
    """本地计算的候选 key 指纹（sha256 前 16 hex；与 pool_control 指纹算法一致时可直接对比）。"""
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:16]


def _mget(node, key, default=None):
    """兼容 dict / MappingProxyType / 任意映射协议（pool_control 返回冻结快照）。"""
    if node is None:
        return default
    try:
        if isinstance(node, dict):
            return node.get(key, default)
        try:
            return node[key]
        except Exception:
            return getattr(node, key, default)
    except Exception:
        return default


def _find_key(obj, key, depth=0):
    """在嵌套 JSON 里深度优先找第一个同名 key（解析 reconciler --check 输出用）。"""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            got = _find_key(v, key, depth + 1)
            if got is not None:
                return got
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            got = _find_key(v, key, depth + 1)
            if got is not None:
                return got
    return None


def _redact(value, depth=0):
    """递归脱敏（审计/诊断输出兜底；正常数据本身不含完整密钥）。"""
    if depth > 6:
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kl = str(k).lower()
            if any(tok in kl for tok in ("secret", "token", "password", "api_key", "apikey", "key")):
                # key 名字里带 key 的字段：只保留指纹/尾号；值本身若已是指纹则原样
                if isinstance(v, str) and (v.startswith("sha256:") or "…" in v or len(v) <= 12):
                    out[k] = v
                elif isinstance(v, str) and v:
                    out[k] = _fingerprint(v)
                else:
                    out[k] = v
            else:
                out[k] = _redact(v, depth + 1)
        return out
    if isinstance(value, list):
        return [_redact(v, depth + 1) for v in value]
    if isinstance(value, tuple):
        return [_redact(v, depth + 1) for v in value]
    if isinstance(value, str):
        return _SECRET_RE.sub(lambda m: _fingerprint(m.group(0)), value)
    return value


def _esc(value):
    """HTML 转义（含引号）。"""
    if pool_admin_ui is not None:
        return pool_admin_ui.esc(value)
    return html.escape(str(value if value is not None else ""), quote=True)


class _HTTPError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message


def _bad_request(message, code="bad_request"):
    return _HTTPError(400, code, message)


def _not_found(message="资源不存在"):
    return _HTTPError(404, "not_found", message)


def _conflict(message, code="conflict"):
    return _HTTPError(409, code, message)


def _unavailable(message, code="unavailable"):
    return _HTTPError(503, code, message)


# --------------------------------------------------------------------------- #
# 路径解析（全部在调用时读 env，便于测试用临时 POOL_HOME）
# --------------------------------------------------------------------------- #
def pool_home():
    return os.path.abspath(os.environ.get("POOL_HOME") or "/opt/litellm")


def config_path():
    return os.path.abspath(os.environ.get("POOL_CONFIG_PATH")
                           or os.path.join(pool_home(), "pool-config.json"))


def keys_path():
    return os.path.join(os.environ.get("POOL_ADMIN_HOME") or pool_home(),
                        "dsh-instance-keys.json")


def affinity_path():
    return os.path.join(os.environ.get("POOL_ADMIN_HOME") or pool_home(),
                        "pool-affinity.json")


def audit_path():
    return os.path.abspath(os.environ.get("POOL_AUDIT_PATH")
                           or os.path.join(pool_home(), "admin-audit.jsonl"))


def config_yaml_path():
    return os.path.abspath(os.environ.get("POOL_CONFIG_YAML")
                           or os.path.join(pool_home(), "config.yaml"))


def pool_db_path():
    """控制面 SQLite（deployments 真相源）：POOL_DB_PATH > ${POOL_HOME}/pool.db。

    2026-10-19：LiteLLM 已 DB 化，config.yaml 不再写 model_list；
    部署实况（行数/模型名）必须从 pool.db 的 deployments 表读（只读打开）。
    """
    return os.path.abspath(os.environ.get("POOL_DB_PATH")
                           or os.path.join(pool_home(), "pool.db"))


def env_path():
    return os.path.abspath(os.path.join(pool_home(), ".env"))


def _import_control():
    """导入 pool_control。

    两种部署布局都要能工作：
      · 仓库布局：``pool-dash/../pool/pool_control.py``；
      · 平铺部署（生产服务器）：``${POOL_HOME}/pool_control.py``（默认 ``/opt/litellm/``）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.normpath(os.path.join(here, "..", "pool")), pool_home()):
        if os.path.isfile(os.path.join(cand, "pool_control.py")) and cand not in sys.path:
            sys.path.insert(0, cand)
    return importlib.import_module("pool_control")


def _find_reconciler(home=None):
    """reconciler 路径：POOL_RECONCILER > ${POOL_HOME}/pool_reconciler.py > pool-dash/../pool/。"""
    env = (os.environ.get("POOL_RECONCILER") or "").strip()
    if env:
        return os.path.abspath(env)
    home = home or pool_home()
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(home, "pool_reconciler.py"),
                 os.path.normpath(os.path.join(here, "..", "pool", "pool_reconciler.py"))):
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    return ""


def _pos_count(fn):
    """函数接受的位置参数个数（None=拿不到签名）。用于兼容依赖模块的两种签名。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    n = 0
    for p in sig.parameters.values():
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            n += 1
        elif p.kind is p.VAR_POSITIONAL:
            return 99
    return n


def _call_optional_actor(fn, *args):
    """兼容 update_models(models) / update_models(models, actor) 两种契约。"""
    n = _pos_count(fn)
    if n is None or n >= len(args) + 1:
        try:
            return fn(*args, ACTOR)
        except TypeError:
            if n is None:
                return fn(*args)
            raise
    return fn(*args)


# --------------------------------------------------------------------------- #
# 主应用
# --------------------------------------------------------------------------- #
class AdminApp:
    """管理控制台应用：dashboard.py 只负责把 GET/POST 交进来。"""

    def __init__(self, control=None, home=None, reconciler=None):
        self.home = os.path.abspath(home) if home else pool_home()
        self.control = control
        self.control_error = ""
        if self.control is None:
            try:
                self.control = _import_control()
            except Exception as exc:
                self.control = None
                self.control_error = "pool_control 导入失败：%s" % _short(exc)
        self.reconciler = reconciler if reconciler is not None else _find_reconciler(self.home)
        self._lock = threading.RLock()
        self._previews = {}
        self._jobs = {}
        self._check_cache = {"ts": 0.0, "result": None}

    # -- 依赖状态 ---------------------------------------------------------- #
    def _cfg(self):
        """返回 (cfg, uninitialized)。cfg 不可用 → _HTTPError(503)。"""
        if self.control is None:
            raise _unavailable(self.control_error or "管理模块未安装（缺少 pool_control）",
                               "control_missing")
        try:
            cfg = self.control.load_config()
        except Exception as exc:
            raise _unavailable("控制面读取失败：%s" % _short(exc), "control_error")
        if cfg is not None:
            return cfg, False
        err = ""
        try:
            le = getattr(self.control, "last_error", None)
            if callable(le):
                err = str(le() or "")[:200]
        except Exception:
            err = ""
        path = config_path()
        if os.path.exists(path):
            raise _unavailable("控制面文件损坏或不可读：%s%s"
                               % (path, ("（%s）" % err) if err else ""), "control_corrupt")
        return {}, True

    def _reconciler_ok(self):
        return bool(self.reconciler) and os.path.isfile(self.reconciler)

    def state(self):
        """页面/status 共用的只读状态快照（不跑子进程，保证看板秒开）。"""
        state = {
            "project": PROJECT,
            "version": VERSION,
            "home": self.home,
            "config_path": config_path(),
            "keys_path": keys_path(),
            "reconciler": self.reconciler,
            "control_ok": self.control is not None,
            "reconciler_ok": self._reconciler_ok(),
            "readonly": False,
            "uninitialized": False,
            "reason": "",
            "capabilities": {},
            "affinity_mode": "shadow",
        }
        problems = []
        cfg = None
        if self.control is None:
            problems.append(self.control_error or "pool_control 不可用")
        else:
            try:
                cfg = self.control.load_config()
            except Exception as exc:
                problems.append("控制面读取异常：%s" % _short(exc))
                cfg = None
            if cfg is None:
                err = ""
                try:
                    le = getattr(self.control, "last_error", None)
                    if callable(le):
                        err = str(le() or "")[:200]
                except Exception:
                    err = ""
                if os.path.exists(config_path()):
                    problems.append("控制面文件损坏/不可读%s"
                                    % (("（%s）" % err) if err else ""))
                else:
                    state["uninitialized"] = True
        if not self._reconciler_ok():
            problems.append("reconciler 不可用（缺少 pool_reconciler.py）")
        state["readonly"] = bool(problems)
        state["reason"] = "；".join(problems)
        writable = not state["readonly"]
        state["capabilities"] = {
            "models": writable,
            "accounts": writable,
            "keys": writable,
            "affinity": writable,
            "audit": True,
        }
        state["affinity_mode"] = self._affinity_mode(cfg)
        return state

    def _affinity_mode(self, cfg=None):
        """sidecar 读 pool-affinity.json（镜像）；控制面 affinity_mode 为唯一事实源。"""
        mirror = ""
        try:
            with open(affinity_path(), "r", encoding="utf-8") as fh:
                raw = json.load(fh) or {}
            m = str(_mget(raw, "mode") or "")
            if m in ("shadow", "full"):
                mirror = m
        except Exception:
            mirror = ""
        control = ""
        try:
            if cfg is None:
                cfg = self.control.load_config() if self.control else None
            m = str(_mget(cfg, "affinity_mode") or "")
            if m in ("shadow", "full"):
                control = m
        except Exception:
            control = ""
        return control or mirror or "shadow"

    # -- HTTP 入口 --------------------------------------------------------- #
    def dispatch(self, handler, method):
        """dashboard.py 的 class H 调用；返回 True 表示本模块已接管该路径。"""
        try:
            path = urlsplit(getattr(handler, "path", "") or "").path
        except Exception:
            path = getattr(handler, "path", "") or ""
        if not path.startswith("/api/admin/"):
            return False
        try:
            if not self._host_allowed(handler):
                self._send_json(handler, 403, {
                    "ok": False,
                    "error": {"code": "forbidden_host",
                              "message": "Host 不被允许：仅接受 localhost/127.0.0.1/::1 或 POOL_ADMIN_HOSTS 白名单"}})
                return True
            if method == "GET":
                self._handle_get(handler, path)
            elif method == "POST":
                self._handle_post(handler, path)
            else:
                raise _HTTPError(405, "method_not_allowed", "仅支持 GET/POST")
        except _HTTPError as exc:
            self._send_json(handler, exc.status, {
                "ok": False, "error": {"code": exc.code, "message": exc.message}})
        except Exception as exc:  # 兜底：绝不让看板 500 崩溃
            self._send_json(handler, 500, {
                "ok": False,
                "error": {"code": "internal_error", "message": "内部错误：%s" % _short(exc)}})
        return True

    def _host_allowed(self, handler):
        raw = (handler.headers.get("Host") or "").strip()
        if not raw:
            return False
        name = raw
        if name.startswith("["):                      # [::1]:4010
            end = name.find("]")
            name = name[1:end] if end > 0 else name
        elif name.count(":") == 1:                    # localhost:4010
            name = name.split(":", 1)[0]
        name = name.strip().lower()
        allow = set(HOST_ALLOW)
        for item in (os.environ.get("POOL_ADMIN_HOSTS") or "").split(","):
            if item.strip():
                allow.add(item.strip().lower())
        return name in allow

    # -- 响应 -------------------------------------------------------------- #
    def _send(self, handler, status, body, ctype):
        data = body.encode("utf-8")
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", ctype)
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.send_header("X-Frame-Options", "DENY")
            handler.send_header("Referrer-Policy", "no-referrer")
            if ctype.startswith("text/html"):
                handler.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                    "connect-src 'self'; img-src 'none'; base-uri 'none'; form-action 'none'")
            handler.end_headers()
            handler.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, handler, status, obj):
        self._send(handler, status,
                   json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                   "application/json; charset=utf-8")

    def _ok(self, handler, data=None):
        self._send_json(handler, 200, {"ok": True, "data": data if data is not None else {}})

    def _read_json(self, handler):
        ctype = (handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise _HTTPError(415, "unsupported_media_type",
                             "只接受 Content-Type: application/json")
        raw_len = handler.headers.get("Content-Length")
        if raw_len is None:
            raise _HTTPError(411, "length_required", "缺少 Content-Length")
        try:
            length = int(raw_len)
        except (TypeError, ValueError):
            raise _bad_request("Content-Length 非法")
        if length < 0:
            raise _bad_request("Content-Length 非法")
        if length > MAX_BODY:
            raise _HTTPError(413, "payload_too_large", "请求体超过 64KB 上限")
        raw = handler.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            raise _bad_request("请求体不是合法 JSON")
        if not isinstance(body, dict):
            raise _bad_request("请求体必须是 JSON 对象")
        return body

    # -- GET 路由 ---------------------------------------------------------- #
    def _handle_get(self, handler, path):
        # 2026-10-17：/admin 独立页面已下线；页面本体由 dashboard.render_page()
        # 以 pool_admin_ui.build_panel() 片段注入看板的「管理控制台」页签。
        if path == "/api/admin/status":
            state = self.state()
            check = self._check(force=True)
            state["hotPath"] = check.get("state", "unknown")
            state["hotPathRaw"] = check.get("raw") or {}
            state["jobs"] = len(self._jobs)
            state["previews"] = len(self._previews)
            self._ok(handler, state)
            return
        if path == "/api/admin/models":
            self._ok(handler, self._get_models())
            return
        if path == "/api/admin/accounts":
            self._ok(handler, self._get_accounts())
            return
        if path == "/api/admin/instance-keys":
            self._ok(handler, self._get_instance_keys())
            return
        if path == "/api/admin/affinity":
            cfg = None
            try:
                cfg, _uninit = self._cfg()
            except _HTTPError:
                cfg = None
            mirror = None
            try:
                with open(affinity_path(), "r", encoding="utf-8") as fh:
                    mirror = _mget(json.load(fh) or {}, "mode")
            except Exception:
                mirror = None
            self._ok(handler, {
                "mode": self._affinity_mode(cfg),
                "controlMode": str(_mget(cfg, "affinity_mode") or "") or None,
                "mirrorMode": mirror,
                "ttlSeconds": 5,
            })
            return
        if path == "/api/admin/audit":
            qs = parse_qs(urlsplit(handler.path).query)
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except (TypeError, ValueError):
                limit = 50
            self._ok(handler, self._get_audit(limit))
            return
        if path.startswith("/api/admin/jobs/"):
            job_id = path[len("/api/admin/jobs/"):].strip("/")
            if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", job_id or ""):
                raise _not_found("job 不存在")
            self._ok(handler, self._get_job(job_id))
            return
        raise _not_found()

    # -- POST 路由 --------------------------------------------------------- #
    def _handle_post(self, handler, path):
        body = self._read_json(handler)
        remote = ""
        try:
            remote = str(handler.client_address[0])
        except Exception:
            remote = ""
        if path == "/api/admin/models/preview":
            self._ok(handler, self._preview_models(body, remote))
            return
        if path == "/api/admin/models/apply":
            self._ok(handler, self._apply_models(body, remote))
            return
        if path == "/api/admin/accounts/preview":
            self._ok(handler, self._preview_accounts(body, remote))
            return
        if path == "/api/admin/accounts/apply":
            self._ok(handler, self._apply_accounts(body, remote))
            return
        if path == "/api/admin/instance-keys/preview":
            self._ok(handler, self._preview_keys(body, remote))
            return
        if path == "/api/admin/instance-keys/apply":
            self._ok(handler, self._apply_keys(body, remote))
            return
        if path == "/api/admin/affinity/preview":
            self._ok(handler, self._preview_affinity(body, remote))
            return
        if path == "/api/admin/affinity/apply":
            self._ok(handler, self._apply_affinity(body, remote))
            return
        raise _not_found()

    # -- 依赖调用适配层 ---------------------------------------------------- #
    def _require_writable(self):
        state = self.state()
        if state["readonly"]:
            raise _unavailable("管理面当前为只读：%s" % (state["reason"] or "依赖不可用"),
                               "readonly")
        return state

    def _write_env(self, name, value):
        if self.control is None:
            raise _unavailable("pool_control 不可用", "control_missing")
        fn = getattr(self.control, "write_env_value", None)
        if not callable(fn):
            raise _unavailable("pool_control 缺少 write_env_value", "control_incomplete")
        n = _pos_count(fn)
        if n is None or n > 2:
            res = _call_optional_actor(fn, name, value)
        else:
            res = fn(name, value)
        return res if isinstance(res, dict) else {}

    def _remove_env(self, name):
        fn = getattr(self.control, "remove_env_value", None) if self.control else None
        if not callable(fn):
            return {"ok": False, "removed": False, "note": "pool_control 未提供 remove_env_value"}
        try:
            res = fn(name)
        except Exception as exc:
            return {"ok": False, "removed": False, "error": _short(exc)}
        return res if isinstance(res, dict) else {"ok": True, "removed": True}

    def _fingerprints(self, names):
        if self.control is None:
            return {}
        fn = getattr(self.control, "read_env_fingerprints", None)
        if not callable(fn):
            return {}
        try:
            n = _pos_count(fn)
            if n is None or n >= 1:
                res = fn(list(names))
            else:
                res = fn()
        except TypeError:
            try:
                res = fn(list(names))
            except TypeError:
                res = fn()
        except Exception:
            return {}
        out = {}
        if isinstance(res, dict):
            for k, v in res.items():
                out[str(k)] = str(v)
        return out

    def _write_json(self, path, data):
        if self.control is None:
            raise _unavailable("pool_control 不可用", "control_missing")
        fn = getattr(self.control, "atomic_write_json", None)
        if not callable(fn):
            raise _unavailable("pool_control 缺少 atomic_write_json", "control_incomplete")
        parent = os.path.dirname(path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError:
                pass
        n = _pos_count(fn)
        if n is None or n >= 3:
            try:
                fn(path, data, 0o600)
            except TypeError:
                fn(path, data)
        else:
            fn(path, data)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _call_control(self, name, *args):
        if self.control is None:
            raise _unavailable("pool_control 不可用", "control_missing")
        fn = getattr(self.control, name, None)
        if not callable(fn):
            raise _unavailable("pool_control 缺少 %s" % name, "control_incomplete")
        try:
            return _call_optional_actor(fn, *args)
        except ValueError as exc:
            raise _bad_request(str(exc) or "参数非法")
        except _HTTPError:
            raise
        except Exception as exc:
            raise _unavailable("%s 执行失败：%s" % (name, _short(exc)), "control_error")

    def _audit(self, action, detail, remote=""):
        """审计（绝不因审计失败影响主流程；detail 不得含完整密钥）。"""
        entry = {
            "ts": _iso(),
            "action": str(action)[:64],
            "actor": ACTOR,
            "remote": str(remote or "")[:64],
        }
        if isinstance(detail, dict):
            entry.update({str(k): v for k, v in detail.items()})
        else:
            entry["detail"] = str(detail)[:2000]
        entry = _redact(entry)
        if self.control is None:
            return "control 不可用，审计未记录"
        fn = getattr(self.control, "append_audit", None)
        if not callable(fn):
            return "pool_control 缺少 append_audit"
        try:
            params = {}
            try:
                params = set(inspect.signature(fn).parameters)
            except (TypeError, ValueError):
                params = set()
            n = _pos_count(fn)
            if "entry" in params or (n is not None and n <= 1 and "action" not in params):
                fn(entry)
            elif "action" in params and n is not None and n >= 3:
                fn(entry["action"], entry, ACTOR)
            elif "action" in params and n == 2:
                fn(entry["action"], entry)
            elif n is not None and n >= 3:
                fn(entry["action"], entry, ACTOR)
            elif n == 2:
                fn(entry["action"], entry)
            else:
                fn(entry)
        except Exception as exc:
            return "审计写入失败：%s" % _short(exc)
        return ""

    # -- reconciler 子进程（测试 monkeypatch 点：run_reconciler） ---------- #
    def run_reconciler(self, mode, timeout=30):
        """调用 pool_reconciler.py。mode ∈ {--check, --dry-run, --once}。

        返回 {"ok", "rc", "json", "stdout", "stderr", "error", "cmd"}；
        stdout 约定为结构化 JSON，诊断在 stderr。所有异常都收敛为 ok=False。
        """
        if not self._reconciler_ok():
            return {"ok": False, "rc": None, "json": None, "stdout": "", "stderr": "",
                    "error": "reconciler 不可用（缺少 pool_reconciler.py）", "cmd": ""}
        python = os.environ.get("POOL_PYTHON") or sys.executable or "python3"
        cmd = [python, self.reconciler, mode]
        try:
            cp = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                cwd=os.path.dirname(self.reconciler) or None)
        except subprocess.TimeoutExpired:
            return {"ok": False, "rc": None, "json": None, "stdout": "",
                    "stderr": "", "error": "reconciler 超时（>%ss）" % timeout,
                    "cmd": " ".join(cmd)}
        except Exception as exc:
            return {"ok": False, "rc": None, "json": None, "stdout": "", "stderr": "",
                    "error": "reconciler 启动失败：%s" % _short(exc), "cmd": " ".join(cmd)}
        out = (cp.stdout or "").strip()
        err = (cp.stderr or "").strip()
        doc = None
        if out:
            try:
                doc = json.loads(out)
            except Exception:
                doc = None
        ok = cp.returncode == 0 and doc is not None
        error = ""
        if cp.returncode != 0:
            error = "reconciler %s 退出码 %d" % (mode, cp.returncode)
            if err:
                error += "：" + err.splitlines()[-1][:200]
        elif doc is None:
            error = "reconciler %s stdout 不是 JSON" % mode
        return {"ok": ok, "rc": cp.returncode, "json": doc,
                "stdout": out[-8000:], "stderr": err[-8000:],
                "error": error, "cmd": " ".join(cmd)}

    def _check(self, force=False):
        """--check 热路径探测（10s 缓存；status 页面/apply 预检用）。"""
        now = time.time()
        with self._lock:
            cache = self._check_cache
            if not force and cache["result"] is not None and now - cache["ts"] < 10.0:
                return cache["result"]
        raw = self.run_reconciler("--check", timeout=15)
        doc = raw.get("json")
        state = _find_key(doc, "hot_path")
        if state is None:
            state = _find_key(doc, "hotPath")
        if state is None:
            state = _find_key(doc, "hot")
        if isinstance(state, bool):
            state = "available" if state else "unavailable"
        state = str(state or "").strip().lower()
        if state not in ("available", "unavailable"):
            if raw.get("ok"):
                state = "unknown"
            else:
                state = "unavailable" if self._reconciler_ok() else "unknown"
        result = {"state": state, "raw": raw}
        with self._lock:
            self._check_cache = {"ts": now, "result": result}
        return result

    # -- 预览 token -------------------------------------------------------- #
    def _stash(self, kind, payload, expected, **extra):
        token = secrets.token_hex(12)
        now = time.time()
        with self._lock:
            # 清理过期预览
            for key in [k for k, v in self._previews.items() if v["expire"] < now]:
                self._previews.pop(key, None)
            while len(self._previews) >= MAX_PREVIEWS:
                self._previews.pop(next(iter(self._previews)))
            item = {"kind": kind, "payload": payload, "expected": expected,
                    "created": now, "expire": now + PREVIEW_TTL, "meta": extra}
            self._previews[token] = item
        return token

    def _peek(self, body, kind):
        token = str(body.get("token") or "").strip()
        if not token:
            raise _bad_request("缺少 preview token，请先预览")
        with self._lock:
            item = self._previews.get(token)
        if item is None:
            raise _conflict("预览已失效或已被使用，请重新预览", "preview_expired")
        if item["expire"] < time.time():
            with self._lock:
                self._previews.pop(token, None)
            raise _conflict("预览已过期（TTL 120s），请重新预览", "preview_expired")
        if item["kind"] != kind:
            raise _conflict("预览类型不匹配，请重新预览", "preview_mismatch")
        confirm = str(body.get("confirm") if body.get("confirm") is not None else "").strip()
        if confirm != item["expected"]:
            raise _conflict("确认串不匹配，未执行（应输入：%s）" % item["expected"],
                            "confirm_mismatch")
        if item["meta"].get("ack_required") and body.get("acknowledgeDegraded") is not True:
            raise _conflict("热路径不可用：需要额外确认 acknowledgeDegraded=true 才能执行",
                            "degraded_ack_required")
        with self._lock:
            self._previews.pop(token, None)      # token 单次有效
        return item

    # -- 模型 -------------------------------------------------------------- #
    @staticmethod
    def _models_in_yaml(text):
        """从 LiteLLM config.yaml 文本提取主模型名（去掉 @a<N> 池内别名）。"""
        names = set()
        for raw in re.findall(r"^\s*-\s*model_name:\s*['\"]?([^\s'\"]+)", text or "", re.M):
            base = re.split(r"@a[0-9]+", raw, maxsplit=1)[0]
            if base:
                names.add(base)
        return names

    def _deployed_models(self):
        """已部署模型的主模型名集合（去掉 @a<N> / @a<N>s<M> 池内别名）。

        2026-10-19（task-5）：LiteLLM 已 DB 化 → 真相源改为 pool.db 的 deployments 表
        （只读 ``mode=ro``）；``config.yaml`` 仅在 DB 读不到时作为回退（fail-soft，页面不 500）。
        返回值语义保持不变：主模型名集合（逐模型「已部署/未部署」标签继续正确）。
        """
        stats = self._deployment_stats()
        if stats["rows"] is not None:
            return sorted(stats["models"])
        try:
            with open(config_yaml_path(), "r", encoding="utf-8") as fh:
                text = fh.read()
        except Exception:
            return []
        return sorted(self._models_in_yaml(text))

    @staticmethod
    def _base_models(names):
        """model_name 列表 → 主模型名集合（`x@a4s1` / `x@a6` → `x`）。"""
        out = set()
        for raw in names or ():
            base = re.split(r"@a[0-9]+", str(raw or ""), maxsplit=1)[0].strip()
            if base:
                out.add(base)
        return out

    def _deployment_stats(self):
        """pool.db deployments 实况：``{rows, models, source}``。

        - ``rows`` = ``COUNT(*)``（与看板 KPI「部署」、LiteLLM /model/info 同源同值）；
        - ``models`` = 去别名后的主模型名集合；
        - DB 不存在/无表/不可读 → ``rows=None, source=""``（调用方回退旧行为，绝不抛异常）。
        只读打开（``mode=ro``），绝不写库。
        """
        path = pool_db_path()
        try:
            import sqlite3
            conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
            try:
                row = conn.execute("SELECT COUNT(*) FROM deployments").fetchone()
                rows = int(row[0]) if row else 0
                names = [r[0] for r in conn.execute(
                    "SELECT DISTINCT model_name FROM deployments")]
            finally:
                conn.close()
        except Exception:
            return {"rows": None, "models": set(), "source": ""}
        return {"rows": rows, "models": self._base_models(names), "source": "pool.db"}

    def _catalog_models(self):
        """离线候选模型目录：控制面 models.available + model-catalog.json + config.yaml 历史备份。

        目的：某个模型被移出 allowlist、reconciler 删除 deployment 后，用户仍能在页面上
        重新勾选回来（可用集合不因下线而丢失）。只读本地文件、绝不联网。
        """
        names = set()
        base = os.path.dirname(config_yaml_path())
        # 1) 部署方约定的离线目录（可选文件，格式 [..] 或 {"models":[..]}）
        try:
            with open(os.path.join(base, "model-catalog.json"), "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            items = doc.get("models") if isinstance(doc, dict) else doc
            if isinstance(items, list):
                names |= {str(x).strip() for x in items if str(x).strip()}
        except Exception:
            pass
        # 2) config.yaml 历史备份（gen_config / 管理面变更都会留 .bak-*）
        try:
            import glob
            pattern = os.path.join(base, "config.yaml.bak-*")
            for path in sorted(glob.glob(pattern))[-20:]:
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        names |= self._models_in_yaml(fh.read())
                except Exception:
                    continue
        except Exception:
            pass
        return names

    def _account_rows(self, cfg):
        accounts = []
        raw = _mget(cfg, "accounts") or ()
        for item in raw:
            try:
                slot = int(_mget(item, "slot"))
            except (TypeError, ValueError):
                continue
            accounts.append({
                "slot": slot,
                "key_var": str(_mget(item, "key_var") or "OCG_KEY_%d" % slot),
                "email_var": str(_mget(item, "email_var") or ""),
                "enabled": _mget(item, "enabled") is not False,
            })
        accounts.sort(key=lambda a: a["slot"])
        return accounts

    def _env_emails(self):
        """从 .env 读 OCG_EMAIL_<n>（非机密，仅供展示；密钥值绝不读取）。"""
        out = {}
        try:
            with open(env_path(), "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("OCG_EMAIL_") or "=" not in line:
                        continue
                    name, _, value = line.partition("=")
                    if name.count("_") == 2:
                        out[name] = value.strip().strip('"').strip("'")
        except Exception:
            pass
        return out

    def _allowlist(self, cfg):
        models = _mget(cfg, "models")
        allow = _mget(models, "allow") or ()
        out = []
        for x in allow:
            try:
                out.append(str(x))
            except Exception:
                continue
        return out

    def _get_models(self):
        deployed = self._deployed_models()
        stats = self._deployment_stats()
        try:
            cfg, uninit = self._cfg()
        except _HTTPError as exc:
            rows = stats["rows"] if stats["rows"] is not None else 0
            return {"degraded": True, "reason": exc.message, "allow": [], "available": deployed,
                    "deployed": deployed, "deployedRows": rows, "deployedModels": deployed,
                    "deployments": rows, "deploymentsEstimated": 0,
                    "deploymentsSource": stats["source"] or "unavailable",
                    "accountCount": 0, "readonly": True, "uninitialized": False}
        accounts = self._account_rows(cfg)
        allow = self._allowlist(cfg)
        available = sorted(set(allow) | set(deployed) | self._catalog_models()
                           | {str(x) for x in (_mget(_mget(cfg, "models"), "available") or ())})
        enabled = sum(1 for a in accounts if a["enabled"])
        # 实况优先：deployments = pool.db COUNT(*)（与看板 KPI「部署」同源同值）；
        # DB 不可读才回退到旧的估算公式，并如实标注来源，避免「已部署 0 / 预计 15」与 135 打架。
        estimated = len(allow) * enabled
        if stats["rows"] is not None:
            rows, source = stats["rows"], stats["source"]
        else:
            rows, source = estimated, "estimated(config.yaml fallback)"
        return {"degraded": False, "reason": "", "allow": allow, "available": available,
                "deployed": deployed, "deployedRows": rows, "deployedModels": deployed,
                "deployments": rows, "deploymentsEstimated": estimated,
                "deploymentsSource": source,
                "accountCount": enabled,
                "readonly": self.state()["readonly"], "uninitialized": uninit}

    def _preview_models(self, body, remote):
        self._require_writable()
        raw_models = body.get("models")
        if not isinstance(raw_models, (list, tuple)):
            raise _bad_request("models 必须是数组")
        if len(raw_models) > 256:
            raise _bad_request("模型数量过多（上限 256）")
        wanted, seen = [], set()
        for item in raw_models:
            if not isinstance(item, str):
                raise _bad_request("模型 id 必须是字符串")
            model = item.strip()
            if not MODEL_RE.fullmatch(model):
                raise _bad_request("非法模型 id：%r（允许 [A-Za-z0-9._:-]，长度 1-64）" % item)
            if model not in seen:
                seen.add(model)
                wanted.append(model)
        cfg, _uninit = self._cfg()
        current = self._allowlist(cfg)
        added = [m for m in wanted if m not in current]
        removed = [m for m in current if m not in wanted]
        if not added and not removed:
            raise _bad_request("无变化：提交的 allowlist 与当前一致")
        accounts = self._account_rows(cfg)
        enabled = sum(1 for a in accounts if a["enabled"])
        delta_add = len(added) * enabled
        delta_del = len(removed) * enabled
        details = []
        for m in added:
            details.append("+ %s（新增，约 +%d 个 deployment）" % (m, enabled))
        for m in removed:
            details.append("- %s（下线，约 -%d 个 deployment）" % (m, enabled))
        impact = [
            "allowlist：%d → %d 个模型；启用账号 %d 个。" % (len(current), len(wanted), enabled),
            "预计 deployment 变化：+%d / -%d（当前 %d）。"
            % (delta_add, delta_del, len(current) * enabled),
        ]
        if removed:
            impact.append("⚠ 将下线 %d 个模型：客户端（DSH 等）若仍引用将收到 404 model_not_found。"
                          % len(removed))
        if not wanted:
            impact.append("⚠ allowlist 为空：所有模型将被下线，客户端全部不可用。")
        # 预览必须调 reconciler --dry-run（结构化 actions/summary）
        dry = self.run_reconciler("--dry-run", timeout=45)
        check = self._check()
        hot = check.get("state", "unknown")
        ack_required = hot == "unavailable"
        dry_doc = dry.get("json") or {}
        dry_actions = _mget(dry_doc, "actions") or []
        dry_summary = _mget(dry_doc, "summary") or {}
        if dry.get("ok"):
            details.append("reconciler --dry-run：actions=%d summary=%s"
                           % (len(dry_actions) if isinstance(dry_actions, list) else 0,
                              json.dumps(_redact(dry_summary), ensure_ascii=False)[:400]))
        else:
            details.append("reconciler --dry-run 失败：%s（apply 将被阻止）"
                           % (dry.get("error") or "未知原因"))
        if ack_required:
            details.append("⚠ 热路径自检显示 unavailable：apply 将退化为 gen_config + "
                           "docker restart（较慢，需要额外确认）")
        elif hot == "unknown":
            details.append("提示：热路径自检结果 unknown，apply 时将再次预检。")
        expected = removed[0] if removed else added[0]
        token = self._stash("models", {"models": wanted, "dry_ok": bool(dry.get("ok")),
                                       "check": hot},
                            expected, ack_required=ack_required)
        return {
            "token": token,
            "title": "应用模型 allowlist 变更",
            "diff": {"added": added, "removed": removed, "unchanged": [m for m in wanted if m in current]},
            "confirmExpected": expected,
            "confirmHint": "请输入%s：%s" % ("将被下线的模型名" if removed else "新增的模型名",
                                          expected),
            "impact": impact,
            "details": details,
            "extra": ("reconciler dry-run 输出：%s" % (dry.get("stdout") or "")[:1500])
            if dry.get("stdout") else "",
            "hotPath": hot,
            "ackRequired": ack_required,
            "ttlSeconds": int(PREVIEW_TTL),
        }

    def _apply_models(self, body, remote):
        self._require_writable()
        item = self._peek(body, "models")
        payload = item["payload"]
        if not payload.get("dry_ok"):
            raise _conflict("reconciler --dry-run 未通过，已阻止写入（先修复 reconciler）",
                            "dry_run_failed")
        job_id = self._new_job("models", remote)
        wanted = list(payload["models"])
        actor = ACTOR

        def work(job):
            warnings = []
            rollback = ""
            snapshot = self._snapshot(config_path())
            before_allow = []
            if isinstance(snapshot, dict) and snapshot.get("kind") == "json":
                before_allow = list(self._allowlist(snapshot.get("data") or {}))
            self._job_step(job, "预检 reconciler --check", "running")
            check = self._check(force=True)
            self._job_step(job, "预检 reconciler --check", "ok",
                           "hot_path=%s" % check.get("state"))
            if check.get("state") == "unavailable":
                warnings.append("热路径 unavailable：本次执行退化为 gen_config + docker restart")
            self._job_step(job, "写入控制面 pool-config.json", "running")
            try:
                self._call_control("update_models", wanted)
            except Exception as exc:
                self._job_step(job, "写入控制面 pool-config.json", "error", _short(exc))
                raise
            self._job_step(job, "写入控制面 pool-config.json", "ok",
                           "models=%d" % len(wanted))
            self._job_step(job, "reconciler --once 热应用", "running")
            applied = self.run_reconciler("--once", timeout=180)
            self._job_step(job, "reconciler --once 热应用",
                           "ok" if applied.get("ok") else "error",
                           applied.get("error") or self._summary_text(applied.get("json")))
            if not applied.get("ok"):
                if snapshot is not None:
                    self._restore(config_path(), snapshot)
                    rollback = "reconciler 失败，已回滚控制面文件；LiteLLM 侧可能部分应用，请重跑 --once 校正"
                self._audit("models.apply", {"target": "models.allow", "result": "error",
                                             "error": applied.get("error") or "",
                                             "count": len(wanted)}, remote)
                job["result"] = {"applied": False, "rollback": rollback,
                                 "reconciler": _redact(applied.get("json"))}
                raise RuntimeError(applied.get("error") or "reconciler 执行失败")
            self._audit("models.apply", {
                "target": "models.allow", "result": "ok",
                "before": before_allow, "after": wanted, "count": len(wanted),
                "hotPath": check.get("state"),
            }, remote)
            job["result"] = {"applied": True, "models": wanted,
                             "rollback": rollback, "warnings": warnings,
                             "reconciler": _redact(applied.get("json")),
                             "stderr": applied.get("stderr") or ""}

        self._start_job(job_id, work)
        return {"jobId": job_id, "state": "running"}

    # -- 账号 -------------------------------------------------------------- #
    def _get_accounts(self):
        try:
            cfg, uninit = self._cfg()
        except _HTTPError as exc:
            return {"degraded": True, "reason": exc.message, "accounts": [],
                    "enabledCount": 0, "nextSlot": 1, "modelsCount": 0,
                    "deployments": 0, "readonly": True}
        rows = self._account_rows(cfg)
        fingerprints = self._fingerprints([a["key_var"] for a in rows])
        emails = self._env_emails()
        out = []
        for a in rows:
            fp = fingerprints.get(a["key_var"], "")
            out.append({
                "slot": a["slot"],
                "key_var": a["key_var"],
                "email_var": a["email_var"],
                "email": emails.get(a["email_var"], "") if a["email_var"] else "",
                "fingerprint": fp,
                "keyPresent": bool(fp),
                "enabled": a["enabled"],
            })
        allow = self._allowlist(cfg)
        enabled = sum(1 for a in rows if a["enabled"])
        # 与 _get_models() 同一口径：优先 pool.db 实况行数，DB 不可读才回退估算
        stats = self._deployment_stats()
        deployments = stats["rows"] if stats["rows"] is not None else len(allow) * enabled
        return {"degraded": False, "reason": "", "accounts": out,
                "enabledCount": enabled, "nextSlot": self._next_slot(cfg),
                "modelsCount": len(allow), "deployments": deployments,
                "readonly": self.state()["readonly"], "uninitialized": uninit}

    def _audit_slots(self):
        """审计文件里出现过的最大的"删除/新增账号槽号"（用于槽号单调分配，防复用）。"""
        slots = set()
        try:
            with open(audit_path(), "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "slot" not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    for holder in (entry, entry.get("detail") or {}):
                        if not isinstance(holder, dict):
                            continue
                        for key in ("slot", "target_slot"):
                            try:
                                slots.add(int(holder.get(key)))
                            except (TypeError, ValueError):
                                continue
                        target = holder.get("target")
                        if isinstance(target, str):
                            m = re.search(r"a(\d{1,3})", target)
                            if m:
                                slots.add(int(m.group(1)))
        except FileNotFoundError:
            return set()
        except Exception:
            return set()
        return slots

    def _max_known_slot(self, cfg):
        slots = {a["slot"] for a in self._account_rows(cfg)}
        slots |= self._audit_slots()
        for name in self._env_names():
            m = re.fullmatch(r"OCG_KEY_([0-9]{1,3})", name)
            if m:
                slots.add(int(m.group(1)))
        return max(slots) if slots else 0

    def _next_slot(self, cfg):
        return max(1, self._max_known_slot(cfg) + 1) if cfg is not None else 1

    def _env_names(self):
        names = []
        try:
            with open(env_path(), "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        names.append(line.split("=", 1)[0].strip())
        except Exception:
            pass
        return names

    def _validate_slot(self, raw):
        try:
            slot = int(raw)
        except (TypeError, ValueError):
            raise _bad_request("槽号必须是整数")
        if slot < SLOT_MIN or slot > SLOT_MAX:
            raise _bad_request("槽号范围 %d-%d" % (SLOT_MIN, SLOT_MAX))
        return slot

    @staticmethod
    def _normalize_accounts(accounts):
        """规范化成 pool_control.update_accounts 接受的字段（未知字段一律不传）。"""
        out = []
        for item in accounts or ():
            try:
                slot = int(_mget(item, "slot"))
            except (TypeError, ValueError):
                continue
            entry = {
                "slot": slot,
                "key_var": str(_mget(item, "key_var") or "OCG_KEY_%d" % slot),
                "enabled": _mget(item, "enabled") is not False,
            }
            email_var = str(_mget(item, "email_var") or "").strip()
            if email_var:
                entry["email_var"] = email_var
            out.append(entry)
        out.sort(key=lambda a: a["slot"])
        return out

    def _preview_accounts(self, body, remote):
        self._require_writable()
        cfg, _uninit = self._cfg()
        action = str(body.get("action") or "upsert").strip().lower()
        if action not in ("upsert", "update", "rotate", "toggle", "delete"):
            raise _bad_request("action 必须是 upsert/update/rotate/toggle/delete")
        slot = self._validate_slot(body.get("slot"))
        current = self._account_rows(cfg)
        by_slot = {a["slot"]: a for a in current}
        key = body.get("key")
        if key is not None:
            if not isinstance(key, str):
                raise _bad_request("key 必须是字符串")
            key = key.strip()
            if not key:
                key = None
            elif not KEY_RE.fullmatch(key):
                raise _bad_request("key 格式非法（不允许空白/控制字符，长度 8-512）")
        email = body.get("email")
        if email is not None:
            if not isinstance(email, str):
                raise _bad_request("email 必须是字符串")
            email = email.strip()
            if email and not EMAIL_RE.fullmatch(email):
                raise _bad_request("邮箱格式非法")
        details = []
        warnings = []
        new_accounts = [dict(a) for a in current]
        n_by_slot = {a["slot"]: a for a in new_accounts}
        key_fp_before = ""
        key_fp_after = _fingerprint(key) if key else ""
        if action in ("upsert", "update"):
            if slot in by_slot:
                target = n_by_slot[slot]
                key_fp_before = self._fingerprints([target["key_var"]]).get(target["key_var"], "")
                if email:
                    target["email_var"] = "OCG_EMAIL_%d" % slot
                    details.append("~ a%d 邮箱：→ %s" % (slot, email))
                if key:
                    details.append("~ a%d 轮换 key：指纹 %s → %s"
                                   % (slot, key_fp_before or "（缺失）", key_fp_after))
                else:
                    details.append("~ a%d 更新元数据（key 不变）" % slot)
                if "enabled" in body:
                    enabled = bool(body.get("enabled"))
                    target["enabled"] = enabled
                    details.append("~ a%d 启用状态：%s" % (slot, "启用" if enabled else "禁用"))
            else:
                if not key:
                    raise _bad_request("新增账号必须提供上游 key（只写 .env，不回显）")
                max_known = self._max_known_slot(cfg)
                if slot <= max_known:
                    raise _bad_request(
                        "槽号必须单调分配：新槽号需 > %d（删除后保留空洞，绝不复用）" % max_known,
                        "slot_reuse")
                n_by_slot[slot] = {"slot": slot, "key_var": "OCG_KEY_%d" % slot,
                                   "email_var": ("OCG_EMAIL_%d" % slot) if email else "",
                                   "enabled": bool(body.get("enabled", True))}
                details.append("+ a%d 新增账号%s（key 指纹 %s）"
                               % (slot, ("（%s）" % email) if email else "", key_fp_after))
        elif action == "rotate":
            if slot not in by_slot:
                raise _not_found("槽 a%d 不存在" % slot)
            if not key:
                raise _bad_request("轮换必须提供新 key")
            target = n_by_slot[slot]
            key_fp_before = self._fingerprints([target["key_var"]]).get(target["key_var"], "")
            details.append("~ a%d 轮换 key：指纹 %s → %s"
                           % (slot, key_fp_before or "（缺失）", key_fp_after))
        elif action == "toggle":
            if slot not in by_slot:
                raise _not_found("槽 a%d 不存在" % slot)
            target = n_by_slot[slot]
            target["enabled"] = not target["enabled"]
            details.append("~ a%d 启用状态：%s" % (slot, "启用" if target["enabled"] else "禁用"))
            if not target["enabled"]:
                warnings.append("禁用账号后，reconciler 会删除该槽的 deployment，"
                                "在途会话可能失败；旧 key 保留在 .env。")
        elif action == "delete":
            if slot not in by_slot:
                raise _not_found("槽 a%d 不存在" % slot)
            n_by_slot.pop(slot, None)
            details.append("- a%d 删除账号（槽号保留空洞，不得复用）" % slot)
            warnings.append("删除会下线该槽的 deployment；旧 key 将在热应用后从 .env 删除。")
        new_accounts = self._normalize_accounts(list(n_by_slot.values()))
        if key is None and not email and new_accounts == self._normalize_accounts(current):
            raise _bad_request("无变化：提交的账号状态与当前一致")
        old_enabled = sum(1 for a in current if a["enabled"])
        new_enabled = sum(1 for a in new_accounts if a["enabled"])
        allow = self._allowlist(cfg)
        delta = len(allow) * (new_enabled - old_enabled)
        impact = [
            "启用账号：%d → %d 个；models %d 个。" % (old_enabled, new_enabled, len(allow)),
            "预计 deployment 变化：%+d（当前 %d → %d）。"
            % (delta, len(allow) * old_enabled, len(allow) * new_enabled),
            "密钥只写 .env（原子+0600+备份），控制面只存变量名；页面/审计永不回显完整值。",
        ]
        impact.extend(warnings)
        check = self._check()
        hot = check.get("state", "unknown")
        ack_required = hot == "unavailable"
        if hot == "unavailable":
            details.append("⚠ 热路径 unavailable：apply 将退化为 gen_config + docker restart，需要额外确认")
        expected = str(slot)
        token = self._stash("accounts",
                            {"action": action, "slot": slot, "key": key, "email": email,
                             "enabled": body.get("enabled") if "enabled" in body else None,
                             "accounts": new_accounts},
                            expected, ack_required=ack_required)
        return {
            "token": token,
            "title": "应用账号变更（槽 a%d）" % slot,
            "diff": {
                "action": action,
                "slot": slot,
                "addedSlots": [a["slot"] for a in new_accounts if a["slot"] not in by_slot],
                "removedSlots": [s for s in by_slot if s not in {a["slot"] for a in new_accounts}],
                "accountSlots": [a["slot"] for a in new_accounts],
                "fingerprintBefore": key_fp_before,
                "fingerprintCandidate": key_fp_after,
            },
            "confirmExpected": expected,
            "confirmHint": "请输入受影响槽号：%d" % slot,
            "impact": impact,
            "details": details,
            "extra": "apply 顺序：write_env_value(.env) → update_accounts(控制面) → "
                     "reconciler --once；任一步失败会回滚并明确报告。",
            "hotPath": hot,
            "ackRequired": ack_required,
            "ttlSeconds": int(PREVIEW_TTL),
        }

    def _apply_accounts(self, body, remote):
        self._require_writable()
        item = self._peek(body, "accounts")
        payload = item["payload"]
        job_id = self._new_job("accounts", remote)

        def work(job):
            action = payload["action"]
            slot = payload["slot"]
            key = payload.get("key")
            email = payload.get("email")
            new_accounts = self._normalize_accounts(payload["accounts"])
            warnings = []
            env_backup = ""
            rolled_env = False
            wrote = False
            control_snapshot = self._snapshot(config_path())
            env_name = "OCG_KEY_%d" % slot

            def rollback(reason):
                nonlocal rolled_env
                notes = []
                if control_snapshot is not None:
                    self._restore(config_path(), control_snapshot)
                    notes.append("控制面已回滚")
                if env_backup and os.path.isfile(env_backup) and not rolled_env:
                    try:
                        shutil.copy2(env_backup, env_path())
                        try:
                            os.chmod(env_path(), 0o600)
                        except OSError:
                            pass
                        rolled_env = True
                        notes.append(".env 已从备份回滚")
                    except Exception as exc:
                        notes.append(".env 回滚失败：%s" % _short(exc))
                job["result"] = dict(job["result"] or {})
                job["result"].update({"applied": False, "rollback": "；".join(notes),
                                      "error": reason})
                return notes

            try:
                self._job_step(job, "预检 reconciler --check", "running")
                check = self._check(force=True)
                self._job_step(job, "预检 reconciler --check", "ok",
                               "hot_path=%s" % check.get("state"))
                if check.get("state") == "unavailable":
                    warnings.append("热路径 unavailable：本次执行退化为 gen_config + docker restart")
                wrote = True
                # 1) 先写 .env（密钥/邮箱），保证 reconciler 能读到新值
                self._job_step(job, "写入 .env", "running")
                written = []
                if key:
                    res = self._write_env(env_name, key)
                    env_backup = str(res.get("backup") or "")
                    written.append("%s（指纹 %s）" % (env_name, res.get("fingerprint") or ""))
                if email:
                    res = self._write_env("OCG_EMAIL_%d" % slot, email)
                    if not env_backup:
                        env_backup = str(res.get("backup") or "")
                    written.append("OCG_EMAIL_%d" % slot)
                self._job_step(job, "写入 .env", "ok",
                               ("已更新 " + "、".join(written)) if written else "无需写 .env")
                # 2) 写控制面
                self._job_step(job, "写入控制面 pool-config.json", "running")
                self._call_control("update_accounts", new_accounts)
                self._job_step(job, "写入控制面 pool-config.json", "ok",
                               "accounts=%d" % len(new_accounts))
                # 3) 热应用
                self._job_step(job, "reconciler --once 热应用", "running")
                applied = self.run_reconciler("--once", timeout=180)
                self._job_step(job, "reconciler --once 热应用",
                               "ok" if applied.get("ok") else "error",
                               applied.get("error") or self._summary_text(applied.get("json")))
                if not applied.get("ok"):
                    err = applied.get("error") or "reconciler 执行失败"
                    rollback(err)
                    job["result"]["reconciler"] = _redact(applied.get("json"))
                    self._audit("accounts.apply", {
                        "target": "a%d" % slot, "action": action, "result": "error",
                        "error": err,
                    }, remote)
                    raise RuntimeError(err)
                # 4) 成功后清理：删除账号 → 真删旧 key
                if action == "delete":
                    removed = self._remove_env(env_name)
                    self._job_step(job, "清理 .env 旧 key", "ok",
                                   "removed=%s" % removed.get("removed"))
                self._audit("accounts.apply", {
                    "target": "a%d" % slot, "action": action, "result": "ok",
                    "keyVar": env_name, "keyFingerprint": _fingerprint(key) if key else "",
                    "emailVar": ("OCG_EMAIL_%d" % slot) if email else "",
                    "enabledCount": sum(1 for a in new_accounts if a.get("enabled")),
                    "accountSlots": [a["slot"] for a in new_accounts],
                    "hotPath": check.get("state"),
                }, remote)
                job["result"] = {"applied": True, "action": action, "slot": slot,
                                 "warnings": warnings,
                                 "reconciler": _redact(applied.get("json")),
                                 "stderr": applied.get("stderr") or ""}
            except Exception as exc:
                if wrote and job["result"] is None:
                    rollback(_short(exc))
                elif job["result"] is not None and not job["result"].get("error"):
                    job["result"]["error"] = _short(exc)
                raise

        self._start_job(job_id, work)
        return {"jobId": job_id, "state": "running"}

    # -- 实例键 ------------------------------------------------------------ #
    def _read_keys(self):
        path = keys_path()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            raise _unavailable("实例键文件不可读：%s" % _short(exc), "keys_unreadable")
        if not isinstance(doc, dict):
            raise _unavailable("实例键文件格式异常（应为 {key: label} 对象）", "keys_corrupt")
        return doc

    def _get_instance_keys(self):
        out = []
        try:
            doc = self._read_keys()
        except _HTTPError as exc:
            return {"degraded": True, "reason": exc.message, "keys": []}
        for key, label in doc.items():
            if str(key).startswith("_"):          # sidecar 忽略 _ 前缀，保留兼容
                continue
            out.append({
                "label": str(label),
                "tail": _tail(key),
                "lastUsed": "—",
                "createdAt": "",
            })
        out.sort(key=lambda x: x["label"])
        return {"degraded": False, "reason": "", "keys": out,
                "file": keys_path(), "readonly": self.state()["readonly"]}

    def _preview_keys(self, body, remote):
        self._require_writable()
        action = str(body.get("action") or "").strip().lower()
        label = body.get("label")
        if not isinstance(label, str) or not LABEL_RE.fullmatch(label.strip()):
            raise _bad_request("label 非法（允许小写字母/数字/-，长度 1-32）")
        label = label.strip()
        doc = self._read_keys()
        existing_labels = {str(v) for k, v in doc.items() if not str(k).startswith("_")}
        if action == "issue":
            if label in existing_labels:
                raise _conflict("label 已存在：%s" % label, "label_exists")
            details = ["+ 签发实例键 label=%s（格式 sk-pool-<32hex>）" % label,
                       "完整键只在 apply 响应里出现一次，之后只能看到 label + 尾 4。"]
            impact = ["即时生效：sidecar 每请求重读 dsh-instance-keys.json。",
                      "持键客户端可访问号池全部 allowlist 模型；请妥善保管。"]
        elif action == "revoke":
            if label not in existing_labels:
                raise _not_found("实例键 label 不存在：%s" % label)
            details = ["- 吊销实例键 label=%s" % label,
                       "现有客户端将在下一个请求收到 401，不可恢复（可重新签发）。"]
            impact = ["即时生效：sidecar 每请求重读 dsh-instance-keys.json。"]
        else:
            raise _bad_request("action 必须是 issue 或 revoke")
        token = self._stash("keys", {"action": action, "label": label}, label)
        return {
            "token": token,
            "title": "签发实例键" if action == "issue" else "吊销实例键",
            "confirmExpected": label,
            "confirmHint": "请输入 label：%s" % label,
            "impact": impact,
            "details": details,
            "extra": "实例键操作不需要 reconciler，确认后立即执行。",
            "hotPath": "instant",
            "ackRequired": False,
            "ttlSeconds": int(PREVIEW_TTL),
        }

    def _apply_keys(self, body, remote):
        self._require_writable()
        item = self._peek(body, "keys")
        payload = item["payload"]
        action = payload["action"]
        label = payload["label"]
        doc = self._read_keys()
        if action == "issue":
            existing = {str(v) for k, v in doc.items() if not str(k).startswith("_")}
            if label in existing:
                raise _conflict("label 已存在：%s" % label, "label_exists")
            key = "sk-pool-" + secrets.token_hex(16)
            while key in doc:
                key = "sk-pool-" + secrets.token_hex(16)
            doc[key] = label
            self._write_json(keys_path(), doc)
            self._audit("keys.issue", {"target": label, "tail": _tail(key), "result": "ok"}, remote)
            return {"key": key, "label": label, "tail": _tail(key), "applied": "instant"}
        if action == "revoke":
            removed = False
            for k in [k for k, v in doc.items() if not str(k).startswith("_") and str(v) == label]:
                doc.pop(k, None)
                removed = True
            if not removed:
                raise _not_found("实例键 label 不存在：%s" % label)
            self._write_json(keys_path(), doc)
            self._audit("keys.revoke", {"target": label, "result": "ok"}, remote)
            return {"revoked": True, "label": label, "applied": "instant"}
        raise _bad_request("action 必须是 issue 或 revoke")

    # -- 钉号模式 ---------------------------------------------------------- #
    def _preview_affinity(self, body, remote):
        self._require_writable()
        mode = str(body.get("mode") or "").strip().lower()
        if mode not in ("shadow", "full"):
            raise _bad_request("mode 必须是 shadow 或 full")
        cfg, _uninit = self._cfg()
        current = self._affinity_mode(cfg)
        if mode == current:
            raise _bad_request("无变化：当前钉号模式已是 %s" % mode)
        details = ["~ affinity mode：%s → %s" % (current, mode)]
        if mode == "full":
            impact = ["full：同一会话树固定到同一上游账号（提升前缀缓存命中，可能改变负载分布）。",
                      "sidecar 5s 热读 pool-affinity.json，无需重启；若出现异常可秒级切回 shadow。"]
        else:
            impact = ["shadow：只记录树键与选号决策，不改变路由（安全默认）。",
                      "sidecar 5s 热读，无需重启。"]
        token = self._stash("affinity", {"mode": mode}, mode)
        return {
            "token": token,
            "title": "切换钉号模式",
            "confirmExpected": mode,
            "confirmHint": "请输入目标模式：%s" % mode,
            "impact": impact,
            "details": details,
            "extra": "切换后 ≤5s 由 sidecar 生效（无需重启）。",
            "hotPath": "instant",
            "ackRequired": False,
            "ttlSeconds": int(PREVIEW_TTL),
        }

    def _apply_affinity(self, body, remote):
        self._require_writable()
        item = self._peek(body, "affinity")
        mode = item["payload"]["mode"]
        self._call_control("set_affinity_mode", mode)
        self._audit("affinity.set", {"target": "affinity_mode", "after": mode, "result": "ok"}, remote)
        return {"mode": mode, "effectiveWithinSeconds": 5, "applied": "instant"}

    # -- 审计 -------------------------------------------------------------- #
    def _get_audit(self, limit=50):
        limit = max(1, min(int(limit or 50), 200))
        path = audit_path()
        entries = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except FileNotFoundError:
            return {"entries": [], "file": path, "count": 0}
        except Exception as exc:
            return {"entries": [], "file": path, "count": 0,
                    "error": "审计文件不可读：%s" % _short(exc)}
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if isinstance(entry, dict):
                entries.append(_redact(entry))
        entries.reverse()
        return {"entries": entries, "file": path, "count": len(entries)}

    # -- Job --------------------------------------------------------------- #
    def _new_job(self, kind, remote=""):
        job_id = secrets.token_hex(8)
        now = time.time()
        with self._lock:
            for jid in [k for k, v in self._jobs.items()
                        if v["finished"] and now - v["finished"] > JOB_TTL]:
                self._jobs.pop(jid, None)
            while len(self._jobs) >= MAX_JOBS:
                finished = [k for k, v in self._jobs.items() if v["finished"]]
                victim = (min(finished, key=lambda k: self._jobs[k]["finished"])
                          if finished else next(iter(self._jobs)))
                self._jobs.pop(victim, None)
            self._jobs[job_id] = {
                "id": job_id, "kind": kind, "state": "running",
                "created": now, "finished": None, "steps": [],
                "result": None, "error": None, "remote": str(remote or "")[:64],
            }
        return job_id

    def _job_step(self, job, name, state, detail=""):
        """记录 Job 步骤；同名连续步骤合并为一条（running → ok/error），前端更清爽。"""
        with self._lock:
            item = {"name": str(name)[:120], "state": state,
                    "detail": str(detail or "")[:1500], "ts": _iso()}
            steps = job["steps"]
            if steps and steps[-1]["name"] == item["name"]:
                steps[-1].update(item)
            else:
                steps.append(item)

    def _start_job(self, job_id, work):
        def runner():
            with self._lock:
                job = self._jobs.get(job_id)
            if job is None:
                return
            try:
                work(job)
                with self._lock:
                    job["state"] = "done"
                    job["finished"] = time.time()
            except Exception as exc:
                with self._lock:
                    job["state"] = "error"
                    job["error"] = _short(exc)
                    job["finished"] = time.time()
        threading.Thread(target=runner, name="admin-job-" + job_id, daemon=True).start()

    def _get_job(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise _not_found("job 不存在或已过期：%s" % job_id)
            data = json.loads(json.dumps(job, ensure_ascii=False, default=str))
        result = data.get("result")
        output_parts = []
        if isinstance(result, dict):
            rec = result.get("reconciler")
            if rec is not None:
                output_parts.append("reconciler: " + json.dumps(_redact(rec), ensure_ascii=False)[:6000])
            if result.get("stderr"):
                output_parts.append("stderr: " + str(result["stderr"])[:2000])
            for key in ("rollback", "error"):
                if result.get(key):
                    output_parts.append("%s: %s" % (key, result[key]))
            if result.get("warnings"):
                output_parts.append("warnings: " + "；".join(map(str, result["warnings"])))
        data["output"] = "\n".join(output_parts)[:8000]
        return data

    # -- 快照/回滚工具 ----------------------------------------------------- #
    def _snapshot(self, path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return {"kind": "json", "data": json.load(fh)}
        except FileNotFoundError:
            return None
        except Exception:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    return {"kind": "raw", "data": fh.read()}
            except Exception:
                return None

    def _restore(self, path, snapshot):
        if not snapshot:
            return
        try:
            if snapshot["kind"] == "json":
                self._write_json(path, snapshot["data"])
            else:
                tmp = path + ".rollback.tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(snapshot["data"])
                os.replace(tmp, path)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        except Exception:
            pass

    @staticmethod
    def _summary_text(doc):
        if not isinstance(doc, dict):
            return ""
        summary = None
        for key in ("summary", "actions_summary", "stats"):
            if isinstance(doc.get(key), dict):
                summary = doc[key]
                break
        if summary is None:
            actions = doc.get("actions")
            if isinstance(actions, list):
                return "actions=%d" % len(actions)
            return ""
        return json.dumps(_redact(summary), ensure_ascii=False)[:300]


__all__ = ["AdminApp", "PROJECT", "ACTOR", "pool_home", "config_path", "keys_path",
           "audit_path", "affinity_path", "_import_control", "_find_reconciler"]
