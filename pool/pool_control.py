#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""号池热更新控制面（pool_control）：唯一事实源 pool-config.json 的读写 + 审计 + .env 原子写。

OpencodeLitellmPool / 号池热更新控制面，目标：**改配置不重启**。

为什么不能靠 restart（必须写在最前面，否则会做错设计）
--------------------------------------------------------
1) LiteLLM 跑在 docker 容器里，容器环境变量在 `docker create/run` 时**固化**；
   `docker restart` 只重启容器内进程，**不会**重新读取 `--env-file` / `.env`。
   所以「改 .env 里的 OCG_KEY_n → restart」并不会让新 key 生效，必须重建容器
   （docker rm + docker run / compose up --force-recreate）。
2) 重建/重启容器会清空 LiteLLM 内存里的 session affinity 钉号（
   deployment_affinity_ttl_seconds 里的映射）与前缀缓存复用状态，对线上长会话是硬伤。
3) 因此把「哪些账号/模型/钉号模式」抽到本文件管理的 `pool-config.json`（唯一事实源），
   密钥值仍只留在 `.env`（控制文件只存变量名，绝不写密钥值）；由
   `pool_reconciler.py` 通过 LiteLLM 管理 API（/model/info、/model/new、
   /model/update、/model/delete）把期望态**热应用**到运行中的进程，全程不重启。

读侧（sidecar / 看板）：
    cfg = load_config()          # TTL 缓存（默认 2s，POOL_CONFIG_TTL 可调）
    if cfg is None: ...          # 任何异常都返回 None，调用方 fail-open
    models = cfg["models"]["allow"]

写侧（看板 / 运维脚本）：
    atomic_write_json(path, obj, mode=0o600)
    update_models(allow, actor) / update_accounts(accounts, actor)
    set_affinity_mode(mode, actor) / write_env_value(var, value, actor)
所有写操作：读-改-写 + fcntl.flock（Windows 用 msvcrt 兜底）+ 原子替换 + 0600 + 审计。

审计脱敏约定（重要）：
    admin-audit.jsonl 只允许出现**变量名 / sha256 指纹 / 长度 / 行号**这类信息；
    绝不写密钥值。write_env_value 的审计只记录 old/new 的 sha256 指纹与长度；
    append_audit 对明显敏感的字段名（secret/token/password/api_key）自动转指纹。
    需要给页面展示的「前4…后4」指纹属于另一种用途（read_env_fingerprints），
    与审计指纹区分开，避免审计里出现任何明文片段。

环境变量：
    POOL_HOME          号池根目录，默认 /opt/litellm
    POOL_CONFIG_PATH   显式覆盖控制文件路径（测试/应急），优先级高于 POOL_HOME
    POOL_AUDIT_PATH    显式覆盖审计文件路径
    POOL_CONFIG_TTL    读侧缓存 TTL 秒数，默认 2.0
    POOL_AFFINITY_MIRROR  设为 0 时不做 pool-affinity.json 兼容镜像写
"""

import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from types import MappingProxyType

try:  # 内置 SQLite 事实源（2026-10-17）：不可用时整体降级回 JSON 文件
    import pool_store as _store
except Exception:  # pragma: no cover
    _store = None

try:  # POSIX 主路径：fcntl.flock
    import fcntl
except ImportError:  # pragma: no cover - Windows 单测环境
    fcntl = None
try:  # Windows 兜底：msvcrt.locking（stdlib）
    import msvcrt
except ImportError:  # pragma: no cover - Linux 生产环境
    msvcrt = None

DEFAULT_POOL_HOME = "/opt/litellm"
CONFIG_NAME = "pool-config.json"
ENV_NAME = ".env"
AUDIT_NAME = "admin-audit.jsonl"
AFFINITY_MIRROR_NAME = "pool-affinity.json"
DEFAULT_CONFIG_TTL = 2.0
DEFAULT_ACTOR = "unknown"

# 模型 id 白名单：只允许安全字符，避免 YAML / JSON / 管理 API 注入（看板输入不可信）
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
AFFINITY_MODES = ("shadow", "full")
ALLOWED_ACCOUNT_FIELDS = {"slot", "key_var", "email_var", "enabled", "email", "models_exclude",
                          "auto_admit"}


class PoolControlError(Exception):
    """控制面自身错误（文件损坏、非法输入、写盘失败）。"""


# ================================================================ 路径
def pool_home():
    """号池根目录；每次调用都重新读 env，方便独立部署与单测注入临时目录。"""
    return os.path.abspath(os.path.expanduser(os.environ.get("POOL_HOME") or DEFAULT_POOL_HOME))


def config_path():
    """控制文件路径：POOL_CONFIG_PATH 显式覆盖 > ${POOL_HOME}/pool-config.json。"""
    override = os.environ.get("POOL_CONFIG_PATH")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(pool_home(), CONFIG_NAME)


def env_path():
    return os.path.join(pool_home(), ENV_NAME)


def audit_path():
    override = os.environ.get("POOL_AUDIT_PATH")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(pool_home(), AUDIT_NAME)


def affinity_mirror_path():
    return os.path.join(pool_home(), AFFINITY_MIRROR_NAME)


def config_ttl():
    """读侧 TTL；非法值回落默认 2.0s。"""
    raw = os.environ.get("POOL_CONFIG_TTL")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_CONFIG_TTL
    try:
        ttl = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONFIG_TTL
    return ttl if ttl >= 0 else DEFAULT_CONFIG_TTL


def _now():
    """可被单测替换的单调时钟（TTL 判定用）。"""
    return time.monotonic()


def now_iso():
    """审计/控制文件时间戳：UTC ISO8601，秒级。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ================================================================ 不可变快照
def freeze(obj):
    """递归冻结：dict → MappingProxyType，list/tuple → tuple。"""
    if isinstance(obj, dict):
        return MappingProxyType({str(k): freeze(v) for k, v in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(freeze(v) for v in obj)
    return obj


def thaw(obj):
    """冻结快照 → 可变结构（看板渲染、json.dumps 前用）。"""
    if isinstance(obj, MappingProxyType) or isinstance(obj, dict):
        return {k: thaw(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [thaw(v) for v in obj]
    return obj


# ================================================================ 指纹 / 脱敏
def fingerprint(value):
    """审计/状态文件专用指纹：sha256 前 16 hex，不泄露任何明文片段。"""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def mask_fingerprint(value):
    """展示用指纹：前 4…后 4（给管理页面辨认账号用，绝不含全量密钥）。

    注意：审计日志**不使用**它（审计只用 fingerprint()），因为「后 4 位」仍属
    部分明文；这里仅用于 UI 展示 / read_env_fingerprints 的返回。
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    if len(text) <= 8:
        return "*" * len(text)
    return text[:4] + "…" + text[-4:]


def redact_value(value):
    """把可能含密钥的值转成可安全落审计的结构（指纹 + 长度）。"""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return {"fingerprint": fingerprint(text), "len": len(text)}


def _json_safe(value, depth=0):
    """递归转 JSON 安全结构；深/怪对象截断为 str。"""
    if depth > 8:
        return str(value)[:200]
    if isinstance(value, MappingProxyType) or isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, depth + 1) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:500]


_SENSITIVE_FIELD_RE = re.compile(r"(secret|password|token|api_key|access_key|_key_value$)", re.I)


def _sanitize_audit_detail(detail):
    """审计 detail 的纵深防御：敏感字段名自动转指纹，其余转 JSON 安全。"""
    if detail is None:
        return {}
    if not isinstance(detail, dict):
        return {"value": _json_safe(detail)}
    out = {}
    for key, value in detail.items():
        name = str(key)
        if _SENSITIVE_FIELD_RE.search(name) and isinstance(value, str) and value:
            out[name] = {"fingerprint": fingerprint(value), "len": len(value)}
        else:
            out[name] = _json_safe(value)
    return out


# ================================================================ 审计
def append_audit(action=None, detail=None, actor=None, *, target=None, before=None,
                 after=None, source=None, result="ok", entry=None):
    """追加一条审计到 ${POOL_HOME}/admin-audit.jsonl（0600，一行一个 JSON）。

    两种调用方式都支持：
      * 看板推荐的显式签名：append_audit("models.update", {"count": 3}, actor="dashboard")
      * 历史/内部形式：append_audit(action="env.write", target="OCG_KEY_2",
                                    before=redact_value(old), after=redact_value(new))
      * 兼容字典形式：append_audit({"action": "...", "detail": {...}, "actor": "..."})

    脱敏：detail 里字段名含 secret/token/password/api_key 的字符串值自动转 sha256 指纹；
    调用方仍应优先传 redact_value() / 变量名，绝不要把密钥明文塞进来。
    审计写失败只告警（不影响业务写盘结果），告警内容不含任何值。
    """
    if isinstance(action, dict) and entry is None and detail is None:
        entry = dict(action)
        action = entry.pop("action", None)
        actor = entry.pop("actor", actor)
        target = entry.pop("target", target)
        source = entry.pop("source", source)
        result = entry.pop("result", result)
        detail = entry.pop("detail", None)
        if entry:  # 其余字段并入 detail
            merged = dict(detail) if isinstance(detail, dict) else {}
            merged.update(entry)
            detail = merged
    record = {
        "ts": now_iso(),
        "action": str(action or "unknown"),
        "result": str(result),
    }
    if actor is not None:
        record["actor"] = str(actor)
    if target is not None:
        record["target"] = str(target)
    if source is not None:
        record["source"] = str(source)
    merged_detail = {}
    if isinstance(detail, dict):
        merged_detail.update(_sanitize_audit_detail(detail))
    elif detail is not None:
        merged_detail["value"] = _json_safe(detail)
    if before is not None:
        merged_detail["before"] = _json_safe(before)
    if after is not None:
        merged_detail["after"] = _json_safe(after)
    record["detail"] = merged_detail
    try:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        path = audit_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception as exc:  # 审计失败不阻断业务，只告警（不含值）
        print("WARN: 审计写入失败（%s）" % str(exc)[:120], file=sys.stderr)
    if _store is not None and _store.enabled():
        try:
            _store.append_audit(record)
        except Exception as exc:  # 入库失败不影响 jsonl 结果
            print("WARN: 审计入库失败（%s）" % str(exc)[:120], file=sys.stderr)
    return None


# ================================================================ 跨进程文件锁
_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS = {}


def _thread_lock(path):
    """同进程内再串一层线程锁：fcntl/msvcrt 在「同进程不同 fd」上的语义不统一。"""
    with _LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[path] = lock
        return lock


def _acquire_os_lock(fd, timeout):
    """获取 OS 级排它锁；拿不到返回 False。"""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            if msvcrt is not None:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return True
                except OSError:
                    pass
            else:  # pragma: no cover - 理论不可达
                return False
        except (BlockingIOError, OSError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _release_os_lock(fd):
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    except OSError:
        pass


@contextlib.contextmanager
def file_lock(path, timeout=10.0, blocking=True):
    """跨进程 + 跨线程排它锁，防控制面写入丢更新 / reconciler 重入。

    POSIX 用 fcntl.flock（与看板/cron 同一把锁），Windows 用 msvcrt.locking 兜底。
    拿不到锁时：blocking=True → 等 timeout 后抛 TimeoutError；
                  blocking=False → 立即抛 TimeoutError。
    """
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tlock = _thread_lock(path)
    effective_timeout = timeout if blocking else 0.0
    if not tlock.acquire(timeout=max(0.0, float(effective_timeout))):
        raise TimeoutError("控制面文件锁被占用: %s" % path)
    fd = None
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError:
            pass
        if not _acquire_os_lock(fd, effective_timeout):
            raise TimeoutError("控制面文件锁被占用: %s" % path)
        yield
    finally:
        if fd is not None:
            _release_os_lock(fd)
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tlock.release()
        except RuntimeError:  # pragma: no cover
            pass


# ================================================================ 原子写
def _fsync_dir(directory):
    """目录 fsync：保证 rename 落盘（Windows 无法打开目录，忽略）。"""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def atomic_write_json(path, obj, mode=0o600):
    """原子写 JSON：同目录临时文件 → fsync → chmod(0600) → os.replace → 目录 fsync。

    先写临时文件再 rename，读者要么看到旧文件、要么看到完整新文件，绝不会读到半截
    （看板/sidecar 并发读时这一点很关键）。返回 None，失败抛异常。
    """
    path = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".pool-tmp-", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
        _fsync_dir(parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return None


def _atomic_write_text(path, text, mode=0o600):
    """原子写文本（.env 用）：同样的临时文件 + fsync + replace + 0600。"""
    path = os.path.abspath(path)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pool-tmp-", suffix=".env", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
        _fsync_dir(parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ================================================================ 读侧：TTL 缓存
_CACHE_LOCK = threading.Lock()
_CACHE = {"path": None, "checked": None, "doc": None, "error": None}


def invalidate_cache():
    """写侧/测试用：强制下一次 load_config 重新读盘。"""
    with _CACHE_LOCK:
        _CACHE["path"] = None
        _CACHE["checked"] = None
        _CACHE["doc"] = None
        _CACHE["error"] = None


def last_error():
    """最近一次 load_config 失败原因（字符串，不含密钥），成功时为 None。"""
    with _CACHE_LOCK:
        return _CACHE["error"]


def cache_info():
    """诊断用：缓存路径 / 年龄 / 是否命中 / 上次错误。"""
    with _CACHE_LOCK:
        checked = _CACHE["checked"]
        age = None if checked is None else max(0.0, _now() - checked)
        return {
            "path": _CACHE["path"],
            "age_s": age,
            "ttl_s": config_ttl(),
            "cached": checked is not None,
            "error": _CACHE["error"],
        }


def load_config(force=False):
    """读控制文件，返回**不可变快照**；任何异常一律返回 None（fail-open）。

    - TTL 缓存（默认 2s，POOL_CONFIG_TTL 可调）：TTL 内直接返回上次结果，
      过期后首个调用重新读盘；跨进程感知靠 TTL（缓存是进程内的）。
    - force=True：跳过缓存立即重读（reconciler 用这个保证看到最新控制文件）。
    - 失败原因可用 last_error() 读取；不会抛异常，也不打印（调用方自己决定日志级别）。
    - 文件合法但 models.allow 为空 → 返回 cfg、allow 为空 tuple（不是 None）；
      文件缺失/JSON 坏/顶层非 object/读盘异常 → None。
    """
    path = config_path()
    ttl = config_ttl()
    now = _now()
    with _CACHE_LOCK:
        checked = _CACHE["checked"]
        if (not force) and _CACHE["path"] == path and checked is not None and (now - checked) < ttl:
            return _CACHE["doc"]
        try:
            if _store is not None and _store.enabled():
                _doc = _store.read_doc("pool-config")
                if _doc is None:
                    # 首次启用 SQLite：把既有文件导入（幂等），之后以库为准
                    _store.import_json_doc(path, "pool-config")
                    _doc = _store.read_doc("pool-config")
                if _doc is not None:
                    if not isinstance(_doc, dict):
                        raise PoolControlError("pool-config 顶层必须是 JSON object")
                    doc = freeze(_doc)
                    _CACHE.update(path=path, checked=now, doc=doc, error=None)
                    return doc
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise PoolControlError("pool-config.json 顶层必须是 JSON object")
            doc = freeze(raw)
            _CACHE.update(path=path, checked=now, doc=doc, error=None)
        except Exception as exc:
            _CACHE.update(path=path, checked=now, doc=None,
                          error="%s: %s" % (type(exc).__name__, str(exc)[:200]))
        return _CACHE["doc"]


# ================================================================ 默认值 / 校验
def default_config():
    """控制面未初始化时的默认结构（仅写侧创建文件时使用；读侧缺失仍返回 None）。"""
    return {
        "version": 1,
        "models": {"allow": []},
        "accounts": [],
        # 与既有 pool-affinity.json 的默认一致：full（不钉号时由 sidecar 按配置处理）
        "affinity_mode": "full",
        "updated_at": None,
        "updated_by": None,
    }


def _validate_models(allow):
    if isinstance(allow, str):
        allow = [allow]
    if not isinstance(allow, (list, tuple)):
        raise ValueError("models.allow 必须是模型 id 列表")
    out = []
    for item in allow:
        if not isinstance(item, str):
            raise ValueError("模型 id 必须是字符串: %r" % (item,))
        model = item.strip()
        if not model or not MODEL_ID_RE.match(model):
            raise ValueError("模型 id 非法（只允许字母数字与 . _ : / -）: %r" % (item,))
        if model not in out:
            out.append(model)
    return out


def _validate_accounts(accounts):
    """校验账号表；控制文件只存变量名，任何疑似密钥/未知字段一律拒绝。"""
    if not isinstance(accounts, (list, tuple)):
        raise ValueError("accounts 必须是账号条目列表")
    out = []
    seen_slots = set()
    seen_vars = set()
    for idx, item in enumerate(accounts):
        if not isinstance(item, dict):
            raise ValueError("accounts[%d] 必须是 object" % idx)
        unknown = set(item) - ALLOWED_ACCOUNT_FIELDS
        if unknown:
            raise ValueError(
                "accounts[%d] 含不允许字段 %s：控制文件只存变量名（key_var/email_var），绝不存密钥值"
                % (idx, sorted(unknown)))
        slot = item.get("slot")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 1:
            raise ValueError("accounts[%d].slot 必须是 ≥1 的整数" % idx)
        key_var = item.get("key_var")
        if not isinstance(key_var, str) or key_var != "OCG_KEY_%d" % slot:
            raise ValueError("accounts[%d].key_var 必须是 OCG_KEY_%d（与 slot 一致）" % (idx, slot))
        email_var = item.get("email_var")
        # 兼容看板传 email 键：但值必须是变量名，真实邮箱地址会被拒绝，防止 PII/密钥误入控制文件
        if email_var is None and "email" in item:
            email_var = item.get("email")
        if email_var is not None:
            if not isinstance(email_var, str) or email_var != "OCG_EMAIL_%d" % slot:
                raise ValueError("accounts[%d].email_var 必须是 OCG_EMAIL_%d（与 slot 一致）" % (idx, slot))
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("accounts[%d].enabled 必须是 bool" % idx)
        # 2026-10-20：按账号排除模型（例如某工作区因区域限制用不了 deepseek）。
        # 语义：全局 models.allow 减去本列表 = 该账号承载的模型；空/未给 = 不排除。
        excl = item.get("models_exclude")
        if excl is not None:
            if isinstance(excl, str) or not isinstance(excl, (list, tuple)):
                raise ValueError("accounts[%d].models_exclude 必须是模型 id 列表" % idx)
            try:
                excl = _validate_models(excl)
            except ValueError as exc:
                raise ValueError("accounts[%d].models_exclude 非法：%s" % (idx, exc))
        # 2026-10-20：自动准入开关。false = 人工锁定，auto_admit 不得自行放行；
        # 缺省 True（额度刷新后允许自动入池）。
        auto_admit = item.get("auto_admit", True)
        if not isinstance(auto_admit, bool):
            raise ValueError("accounts[%d].auto_admit 必须是 bool" % idx)
        if slot in seen_slots:
            raise ValueError("accounts[%d].slot 重复: %d" % (idx, slot))
        if key_var in seen_vars:
            raise ValueError("accounts[%d].key_var 重复: %s" % (idx, key_var))
        seen_slots.add(slot)
        seen_vars.add(key_var)
        account = {"slot": slot, "key_var": key_var, "enabled": enabled}
        if email_var is not None:
            account["email_var"] = email_var
        if "models_exclude" in item:
            # 显式给过就带上（空列表 = 明确清空排除）；完全没给则留给 update_accounts 保留旧值
            account["models_exclude"] = excl
        if "auto_admit" in item:
            # 同上：只带上显式值，缺省由读侧按 True 处理
            account["auto_admit"] = auto_admit
        out.append(account)
    out.sort(key=lambda a: a["slot"])
    return out


def _validate_affinity_mode(mode):
    if not isinstance(mode, str) or mode not in AFFINITY_MODES:
        raise ValueError("affinity_mode 必须是 %s 之一" % "/".join(AFFINITY_MODES))
    return mode


# ================================================================ 写侧：读-改-写
def _read_config_for_write():
    """写侧读取：缺失 → 默认结构；损坏 → 抛错（绝不静默覆盖坏文件）。"""
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return default_config()
    except Exception as exc:
        raise PoolControlError("控制文件损坏，拒绝覆盖（请先人工修复）: %s" % str(exc)[:160])
    if not isinstance(raw, dict):
        raise PoolControlError("控制文件顶层必须是 JSON object，拒绝覆盖")
    return raw


def _mutate(action, target, mutator, actor=None, source=None, updated_by=None, detail_extra=None):
    """通用读-改-写：文件锁内完成「读→改→原子写」，然后失效缓存 + 审计。

    `detail_extra`：附加到审计 detail 的字段（如 reason），不参与业务写入。
    """
    path = config_path()
    with file_lock(path + ".lock", timeout=10.0):
        doc = _read_config_for_write()
        before, after = mutator(doc)
        doc["version"] = int(doc.get("version") or 1)
        doc["updated_at"] = now_iso()
        doc["updated_by"] = str(updated_by or actor or os.environ.get("POOL_UPDATED_BY") or DEFAULT_ACTOR)
        _stored = False
        if _store is not None and _store.enabled():
            try:
                # 事务写库 + 原子镜像回 pool-config.json（老读数方行为不变）
                _store.write_doc("pool-config", doc, mirror_path=path, mode=0o600)
                _stored = True
            except Exception as exc:  # 库异常 → 退回文件写，绝不丢写
                print("WARN: SQLite 写失败，退回 JSON（%s）" % str(exc)[:120], file=sys.stderr)
        if not _stored:
            atomic_write_json(path, doc, 0o600)
    invalidate_cache()
    audit_detail = {"before": _json_safe(before), "after": _json_safe(after)}
    if isinstance(detail_extra, dict):
        audit_detail.update(_json_safe(detail_extra))
    append_audit(action, audit_detail,
                 actor=actor, target=target, source=source)
    return thaw(freeze(doc))


def update_models(allow, actor=None, *, source=None, updated_by=None):
    """整表覆盖 models.allow；返回更新后的控制文件（可变 dict）。非法输入抛 ValueError。"""
    items = _validate_models(allow)

    def mutator(doc):
        current = doc.get("models") if isinstance(doc.get("models"), dict) else {}
        before = {"allow": _json_safe(current.get("allow"))}
        models = dict(current)
        models["allow"] = items
        doc["models"] = models
        return before, {"allow": items}

    return _mutate("models.update", "models", mutator, actor=actor, source=source, updated_by=updated_by)


def _keep_account_fields(items, incoming):
    """整表覆盖时保留调用方没提到的「账号级开关」。

    - `models_exclude`：管理控制台/旧脚本只认 slot/key_var/enabled/email_var，
      整表回写时不会带 models_exclude；若直接覆盖，按账号的模型排除会被静默清空
      （账号会突然开始承载它承载不了的模型）。
    - `auto_admit`：同理，人工锁定的账号（auto_admit=false）若被整表覆盖丢掉该键，
      自动准入会把它当成"允许"从而误放行。
    规则：**入参里没有该键的槽位，沿用当前控制文件里的值。**
    """
    try:
        cur = load_config(force=True) or {}
    except Exception:
        return items
    cur_map = {}
    for a in (cur.get("accounts") or ()):
        # 注意：load_config 返回冻结对象（MappingProxyType），不能用 isinstance(a, dict) 判断
        if hasattr(a, "get"):
            slot_cur = a.get("slot")
            if not isinstance(slot_cur, int):
                continue
            keep = {}
            if a.get("models_exclude"):
                keep["models_exclude"] = list(a.get("models_exclude"))
            if "auto_admit" in a:
                keep["auto_admit"] = a.get("auto_admit")
            if keep:
                cur_map[slot_cur] = keep
    provided = {a.get("slot") for a in incoming if isinstance(a, dict)}
    for it in items:
        slot = it.get("slot")
        if slot not in cur_map or slot not in provided:
            continue
        for key, val in cur_map[slot].items():
            if key not in it:
                it[key] = val
    return items


def update_accounts(accounts, actor=None, *, source=None, updated_by=None):
    """整表覆盖 accounts；返回更新后的控制文件（可变 dict）。非法输入抛 ValueError。"""
    items = _keep_account_fields(_validate_accounts(accounts), accounts)

    def mutator(doc):
        before = {"accounts": _json_safe(doc.get("accounts"))}
        doc["accounts"] = items
        return before, {"accounts": items}

    return _mutate("accounts.update", "accounts", mutator, actor=actor, source=source, updated_by=updated_by)


def set_account_enabled(slot, enabled, actor=None, *, reason=None, source=None, updated_by=None):
    """原子翻转单个账号的 `enabled`（自动准入 / 运维脚本用）。

    与整表覆盖不同：文件锁内只改目标账号的 `enabled`，其余账号与字段逐字节不变，
    避免与看板并发编辑互相覆盖。返回更新后的控制文件（可变 dict）。
    """
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 1:
        raise ValueError("slot 必须是 ≥1 的整数")
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须是 bool")
    slot = int(slot)
    detail = {"reason": reason} if reason else None

    def mutator(doc):
        accounts = doc.get("accounts")
        if not isinstance(accounts, list):
            raise ValueError("控制文件缺少 accounts 列表")
        for item in accounts:
            if not isinstance(item, dict) or item.get("slot") != slot:
                continue
            before = {"slot": slot, "enabled": bool(item.get("enabled"))}
            item["enabled"] = enabled
            return before, {"slot": slot, "enabled": enabled}
        raise ValueError("控制文件里没有账号槽位 %d" % slot)

    return _mutate("accounts.set_enabled", "slot:%d" % slot, mutator, actor=actor,
                   source=source, updated_by=updated_by, detail_extra=detail)


def _write_affinity_mirror(mode):
    """兼容镜像写 ${POOL_HOME}/pool-affinity.json：给**尚未升级**的旧 sidecar 做 5s 热读。

    事实源仍是 pool-config.json 的 affinity_mode；本函数只写不读，失败只告警。
    设 POOL_AFFINITY_MIRROR=0 可关闭。
    """
    if str(os.environ.get("POOL_AFFINITY_MIRROR", "1")).strip() == "0":
        return None
    path = affinity_mirror_path()
    try:
        atomic_write_json(path, {"mode": mode}, 0o600)
    except Exception as exc:
        print("WARN: pool-affinity.json 兼容镜像写失败（%s）" % str(exc)[:120], file=sys.stderr)
        return None
    return path


def set_affinity_mode(mode, actor=None, *, source=None, updated_by=None):
    """切换钉号模式（shadow/full）。

    - 唯一事实源：pool-config.json 的 affinity_mode（新版 sidecar 读它）；
    - 兼容镜像：pool-affinity.json 的 {"mode": ...}（旧 sidecar 5s TTL 热读）；
    - 不碰 routing-share.json（那是份额引擎产物，不是开关）。
    """
    mode = _validate_affinity_mode(mode)

    def mutator(doc):
        before = {"affinity_mode": doc.get("affinity_mode")}
        doc["affinity_mode"] = mode
        return before, {"affinity_mode": mode}

    result = _mutate("affinity.set", "affinity_mode", mutator, actor=actor, source=source,
                     updated_by=updated_by)
    mirror = _write_affinity_mirror(mode)
    if mirror:
        result["affinity_mirror"] = mirror
    return result


# ================================================================ .env 读写
def _read_env_text(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _unquote(raw):
    """.env 行内值去引号（与 gen_config.py 的解析方式保持一致）。"""
    text = (raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def read_env(path=None):
    """解析 .env → dict（不打印任何值）。缺失文件抛 FileNotFoundError。"""
    path = os.path.abspath(path or env_path())
    env = {}
    for line in _read_env_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        env[key.strip()] = _unquote(value)
    return env


def read_env_fingerprints(vars=None):
    """返回指定变量的**展示用指纹**（前4…后4），绝不返回完整值。

    vars 为 None 时默认取所有 OCG_KEY_n。
    """
    env = read_env()
    if vars is None:
        names = [k for k in env if ENV_VAR_RE.match(k) and k.startswith("OCG_KEY_")]
    elif isinstance(vars, str):
        names = [vars]
    else:
        names = [str(v) for v in (vars or [])]
    out = {}
    for name in names:
        if name in env:
            out[name] = mask_fingerprint(env[name])
    return out


def _validate_env_var(var):
    if not isinstance(var, str) or not ENV_VAR_RE.match(var):
        raise ValueError("环境变量名非法（只允许字母数字下划线，且不以数字开头）: %r" % (var,))


def _validate_env_value(value):
    if not isinstance(value, str):
        raise ValueError("环境变量值必须是字符串")
    for bad, label in (("\n", "换行"), ("\r", "回车"), ("\x00", "NUL")):
        if bad in value:
            raise ValueError("环境变量值不允许包含%s字符（避免 .env 行注入）" % label)
    return value


def _detect_eol(text):
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _parse_env_line(line):
    """解析一行 .env，返回 prefix/key/raw_value/ending；非赋值行返回 None。"""
    ending = ""
    body = line
    for candidate in ("\r\n", "\n", "\r"):
        if body.endswith(candidate):
            ending = candidate
            body = body[:-len(candidate)]
            break
    original = body
    rest = body.lstrip(" \t")
    prefix = original[:len(original) - len(rest)]
    if rest.startswith("export ") or rest.startswith("export\t"):
        prefix += rest[:7]
        rest = rest[7:]
    if "=" not in rest:
        return None
    key, _, raw_value = rest.partition("=")
    key = key.strip()
    if not key or not ENV_VAR_RE.match(key):
        return None
    return {"prefix": prefix, "key": key, "raw_value": raw_value, "ending": ending}


def _format_env_value(raw_old, value):
    """按原行引号风格格式化新值；含空白/#/引号时自动加双引号并转义。"""
    stripped = (raw_old or "").strip()
    quote = stripped[0] if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ("'", '"') else ""
    if quote == "'" and "'" in value:
        quote = '"'  # 单引号无法安全转义时改用双引号
    if quote:
        inner = value.replace("\\", "\\\\").replace(quote, "\\" + quote)
        return quote + inner + quote
    if value == "" or value != value.strip() or any(ch in value for ch in " \t#\"'"):
        inner = value.replace("\\", "\\\\").replace('"', '\\"')
        return '"' + inner + '"'
    return value


def _backup_file(path):
    """写前备份：<path>.bak-<ts>（0600）。中文注释见文件头说明。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = "%s.bak-%s" % (path, stamp)
    counter = 1
    while os.path.exists(backup):
        backup = "%s.bak-%s-%d" % (path, stamp, counter)
        counter += 1
    with open(path, "rb") as fh:
        data = fh.read()
    parent = os.path.dirname(backup) or "."
    fd, tmp = tempfile.mkstemp(prefix=".pool-tmp-", suffix=".bak", dir=parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, backup)
        _fsync_dir(parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return backup


def write_env_value(var, value, actor=None, path=None, *, source=None):
    """原子修改 .env 中一个变量（保持其它行字节不变、行序不变），0600 + 写前备份。

    - var 不存在则追加到文件末尾；
    - 值不能含换行/回车/NUL（防行注入）；
    - 值未变化时不动盘、不备份；
    - 审计只记变量名 + 新旧值 sha256 指纹 + 长度，**不打印/不记录值**；
    - 返回 {"ok", "name", "changed", "fingerprint"(展示用前4…后4), "backup", "audit"}，
      绝不返回完整密钥。
    """
    _validate_env_var(var)
    _validate_env_value(value)
    path = os.path.abspath(path or env_path())
    with file_lock(path + ".lock", timeout=10.0):
        try:
            text = _read_env_text(path)
            exists = True
        except FileNotFoundError:
            text = ""
            exists = False
        eol = _detect_eol(text) if text else "\n"
        old_value = None
        changed = False
        new_lines = []
        for line in text.splitlines(keepends=True) if text else []:
            parsed = _parse_env_line(line)
            if parsed is not None and parsed["key"] == var:
                old_value = _unquote(parsed["raw_value"])
                new_line = (parsed["prefix"] + var + "=" +
                            _format_env_value(parsed["raw_value"], value) + parsed["ending"])
                if new_line != line:
                    changed = True
                new_lines.append(new_line)
            else:
                new_lines.append(line)
        if not changed and exists and old_value is not None:
            return {"ok": True, "name": var, "changed": False,
                    "fingerprint": mask_fingerprint(value), "backup": None, "audit": True}
        if old_value is None:
            # 追加新变量：先补行尾换行（保持原有内容逐字节不变）
            if text and not text.endswith(("\n", "\r")):
                new_lines.append(eol)
            new_lines.append(var + "=" + _format_env_value("", value) + eol)
            changed = True
        backup = _backup_file(path) if exists else None
        _atomic_write_text(path, "".join(new_lines), 0o600)
    append_audit("env.write", None, actor, target=var, source=source,
                 before=redact_value(old_value) if old_value is not None else None,
                 after=redact_value(value))
    return {"ok": True, "name": var, "changed": changed,
            "fingerprint": mask_fingerprint(value), "backup": backup, "audit": True}


def remove_env_value(var, actor=None, path=None, *, source=None):
    """从 .env 删除变量（原子写 + 备份 + 审计指纹）。不存在则不写盘。

    返回 {"ok", "name", "removed", "changed", "fingerprint", "backup"}，不含明文值。
    """
    _validate_env_var(var)
    path = os.path.abspath(path or env_path())
    with file_lock(path + ".lock", timeout=10.0):
        try:
            text = _read_env_text(path)
        except FileNotFoundError:
            return {"ok": True, "name": var, "removed": False, "changed": False,
                    "fingerprint": None, "backup": None}
        kept, removed_values = [], []
        for line in text.splitlines(keepends=True):
            parsed = _parse_env_line(line)
            if parsed is not None and parsed["key"] == var:
                removed_values.append(_unquote(parsed["raw_value"]))
            else:
                kept.append(line)
        if not removed_values:
            return {"ok": True, "name": var, "removed": False, "changed": False,
                    "fingerprint": None, "backup": None}
        backup = _backup_file(path)
        _atomic_write_text(path, "".join(kept), 0o600)
    append_audit("env.delete", {"removed_lines": len(removed_values)}, actor,
                 target=var, source=source,
                 before=redact_value(removed_values[0]), after=None)
    return {"ok": True, "name": var, "removed": True, "changed": True,
            "fingerprint": mask_fingerprint(removed_values[0]), "backup": backup}
