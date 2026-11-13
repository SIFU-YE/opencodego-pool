#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""号池热更新应用器（pool_reconciler）：把 pool-config.json + .env 的期望态热应用。

OpencodeLitellmPool / 号池热更新控制面，目标：**改配置不重启**。

为什么不能靠 restart（这是本文件所有设计的根因）
------------------------------------------------
1) LiteLLM 在 docker 容器内运行，容器环境变量在 `docker create/run` 时**固化**；
   `docker restart` 只重启进程，**不会**重新读取 `--env-file`/`.env`。
   于是 config.yaml 里的 `api_key: os.environ/OCG_KEY_n` 在进程启动时解析一次，
   之后改 `.env` 必须重建容器才生效——而重建容器会清空内存 session affinity
   钉号与前缀缓存，线上长会话被硬打断。
2) 所以热路径**不走 config.yaml 的 os.environ 引用**，而是读 `.env` 里的
   **字面量密钥**，通过 LiteLLM 管理 API（/model/info、/model/new、
   /model/update、/model/delete，带 `Authorization: Bearer $LITELLM_MASTER_KEY`）
   把 deployment 增删改直接推到运行中的进程：换 key 即 update，加账号即 new，
   删账号即 delete，全程不重启。
3) 前提：LiteLLM 管理 API 可写（版本支持且有 master key 权限）。启动时用
   `--check` 做能力探测；不可用（404/401/403/连不上）就**明确报不可用**并打印
   降级命令 `gen_config.py + docker restart litellm`，绝不静默假装成功。

与 gen_config.py 的一致性（只读它、不改它）
------------------------------------------
deployment 命名与 litellm_params 必须与 pool/gen_config.py 完全一致，否则 sidecar
按 `config.yaml` 找别名 / 按会话桶改写时会打空：
  · 兜底 deployment：model_name = `<model>`
  · 会话桶别名：     model_name = `<model>@a<slot>s<K>`，K=1..POOL_SESSION_BUCKETS（默认 8）
  · litellm_params.model = `<provider>/<model>`，provider 默认 openai，
    `claude-haiku-5-5` 走 anthropic（与 gen_config.MODEL_ROUTES 一致）
  · api_base = https://opencode.ai/inference/go/{openai/v1|anthropic}
  · extra_headers.user-agent = dsh-opencodego-pool/1.0
  · extra_headers.x-opencode-session = dsh-acct-<slot>[-s<K>]
上述常量是从 gen_config.py 复制来的（它 import 即执行、会查上游，不能直接 import），
改协议/路由时两处必须同步。

幂等/差异计算
-------------
期望集合来自控制文件 + .env；现状来自 GET /model/info；只对差异执行 add/update/delete。
密钥轮换检测：
  · 远端返回明文 api_key → 直接比对；
  · 远端脱敏（含 `*` / 仍是 `os.environ/...` 引用）→ 用本地 `.pool-reconciler-state.json`
    里保存的 sha256 指纹比对（状态文件不含密钥明文）；
  · 每次成功应用后回写状态；只保留「期望集合内」的条目。
删除只针对带号池标记（user-agent + x-opencode-session 均符合本池约定）的 deployment，
不会误删别人接进来的模型。注意 LiteLLM 的删除端点是 `POST /model/delete`（body 为
`{"id": <model_info.id>}`），不是 HTTP DELETE 方法。

热路径 vs 冷启动清单（运维必须知道）
------------------------------------
本工具只改**运行中进程**的 deployment 集合，不重写 config.yaml。config.yaml 仍是
冷启动/重建容器时的清单，由 gen_config.py 维护：若控制文件里的模型 allowlist 与
config.yaml 已不一致，下次容器重建会回到 config.yaml 的旧清单。建议在低峰期重跑
gen_config.py（可带 POOL_SKIP_RESTART=1 只写文件）让冷清单与热状态对齐；不一致期间
`--check`/`--once` 正常，但如果 LiteLLM 重启，下一轮 reconciler 会依据 /model/info
重新收敛。删除账号时槽号不可复用（槽号是 x-opencode-session / 别名的一部分，
复用会让老会话漂到别的上游账号、破坏前缀缓存），槽号分配是看板侧策略。

只依赖 Python 3.11 stdlib，不监听任何端口。

CLI（stdout 只放结构化 JSON，诊断走 stderr；密钥永不输出）
----------------------------------------------------------
  python3 pool_reconciler.py --check                        # 能力探测：0=可用，非0=不可用
  python3 pool_reconciler.py --once [--dry-run]             # 跑一轮（默认 --once）
  python3 pool_reconciler.py --apply                        # --once 的别名（看板兼容）
  python3 pool_reconciler.py --watch [--interval 60] [--dry-run]   # 常驻，TTL 轮询
  python3 pool_reconciler.py --dry-run                      # 预览：{"actions":[...],"summary":{...}}

退出码：0 成功/已收敛/被文件锁跳过；2 管理 API 不可用（热路径降级）；
        3 控制文件/期望态错误；4 应用或收敛校验失败。
环境变量：POOL_HOME、POOL_CONFIG_PATH、POOL_SESSION_BUCKETS、POOL_LITELLM_URL
          （兼容 LITELLM_BASE_URL）、LITELLM_MASTER_KEY（缺省从 ${POOL_HOME}/.env 读）、
          POOL_RECONCILE_RETRIES（默认 3）、POOL_RECONCILE_BACKOFF（默认 0.5s）、
          POOL_RECONCILE_TIMEOUT（默认 10s）、POOL_RECONCILE_INTERVAL（默认 60s）。
"""

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

import pool_control

try:  # 自动准入是可选能力：模块缺失/导入失败不得影响收敛主流程
    import auto_admit
except Exception as _auto_admit_exc:  # pragma: no cover
    auto_admit = None
    _AUTO_ADMIT_IMPORT_ERROR = "%s: %s" % (type(_auto_admit_exc).__name__, str(_auto_admit_exc)[:120])
else:
    _AUTO_ADMIT_IMPORT_ERROR = None

# ---- 与 gen_config.py / session-sidecar.py 保持一致的常量（只读复制，改动需三处同步）----
POOL_USER_AGENT = "dsh-opencodego-pool/1.0"
UPSTREAM_OPENAI = "https://opencode.ai/inference/go/openai/v1"
UPSTREAM_ANTHROPIC = "https://opencode.ai/inference/go/anthropic"

# 2026-10-28（出口拆分）：muse-spark 系列有地区限制——直连 opencode.ai 返回
# 403 RegionError: not available in your country，必须走日本出口；其余模型直连可用。
# 为避免「日本出口节点故障 → 所有模型一起 500」，非 muse 的 deployment 改走本机
# nginx 直连反代（本机 127.0.0.1:4002；容器内访问宿主机时，用 POOL_DIRECT_* 把地址
# 指向宿主机 Docker 网关或 host-gateway 别名；nginx 直连 opencode.ai，不经 sing-box）。
# 可用环境变量覆盖，便于回滚与换部署：POOL_DIRECT_OPENAI / POOL_DIRECT_ANTHROPIC / POOL_EGRESS_MODELS。
DIRECT_OPENAI = (os.environ.get("POOL_DIRECT_OPENAI")
                 or "http://localhost:4002/inference/go/openai/v1").strip()
DIRECT_ANTHROPIC = (os.environ.get("POOL_DIRECT_ANTHROPIC")
                    or "http://localhost:4002/inference/go/anthropic").strip()
EGRESS_MODELS = tuple(m.strip() for m in
                      (os.environ.get("POOL_EGRESS_MODELS") or "muse-spark").split(",")
                      if m.strip())
MODEL_ROUTES = {
    "claude-haiku-5-5": ("anthropic", DIRECT_ANTHROPIC),
}
SESSION_HEADER = "x-opencode-session"
DEFAULT_SESSION_BUCKETS = 8

DEFAULT_LITELLM_URL = "http://127.0.0.1:4000"
STATE_FILE = ".pool-reconciler-state.json"
LOCK_FILE = ".pool-reconciler.lock"

EXIT_OK = 0
EXIT_UNAVAILABLE = 2
EXIT_CONFIG = 3
EXIT_APPLY = 4

MANAGED_FIELDS = ("model", "api_base", "user-agent", SESSION_HEADER)

# 与 gen_config.py 的 REASONING_MODELS 保持一致（两处必须同源，否则热路径与冷启动不一致）。
# 语义：让 LiteLLM 放行 reasoning/thinking 参数；模型本身是否支持由上游决定。
REASONING_MODELS = {
    "mimo-v2.6-flash",
    "mimo-v2.6-pro",
    "muse-spark-1.3-contributor",
    "claude-haiku-5-5",
    "deepseek-v4.1-flash",
}


# ================================================================ 环境参数（调用时读取，便于单测注入）
def litellm_base_url():
    raw = os.environ.get("POOL_LITELLM_URL") or os.environ.get("LITELLM_BASE_URL") or DEFAULT_LITELLM_URL
    return raw.rstrip("/")


def session_buckets():
    raw = os.environ.get("POOL_SESSION_BUCKETS")
    try:
        buckets = int(raw) if raw not in (None, "") else DEFAULT_SESSION_BUCKETS
    except (TypeError, ValueError):
        buckets = DEFAULT_SESSION_BUCKETS
    return max(1, buckets)


def dynamic_session():
    """x-opencode-session 是否改由 sidecar 按请求注入（与 gen_config.py 同一开关）。

    ``POOL_DYNAMIC_SESSION=1`` 时别名部署不再写静态 session 头。**必须与 sidecar 的
    ``POOL_SESSION_ROTATE`` 配套上线**：sidecar 先开始注入，再撤静态头；否则上游 400。
    """
    return (os.environ.get("POOL_DYNAMIC_SESSION") or "0").strip().lower() in ("1", "true", "yes")


def _env_int(name, default):
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    raw = os.environ.get(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def retries():
    return max(0, _env_int("POOL_RECONCILE_RETRIES", 3))


def backoff():
    return max(0.0, _env_float("POOL_RECONCILE_BACKOFF", 0.5))


def http_timeout():
    return max(0.1, _env_float("POOL_RECONCILE_TIMEOUT", 10.0))


def watch_interval():
    return max(1.0, _env_float("POOL_RECONCILE_INTERVAL", 60.0))


def master_key():
    """管理 API 的 master key：优先进程环境，其次 ${POOL_HOME}/.env。"""
    key = os.environ.get("LITELLM_MASTER_KEY")
    if key:
        return key.strip()
    try:
        return (pool_control.read_env().get("LITELLM_MASTER_KEY") or "").strip()
    except Exception:
        return ""


# ================================================================ 输出
def log(message):
    """诊断信息 → stderr；stdout 仅留给结构化 JSON（看板解析）。"""
    sys.stderr.write(str(message) + "\n")
    sys.stderr.flush()


def emit(payload):
    """结构化结果 → stdout（一行 JSON）。密钥绝不进入 payload。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def fallback_command():
    home = pool_control.pool_home()
    return "python3 %s/gen_config.py && docker compose up -d --force-recreate" % home


def unavailable_text(reason):
    return (
        "【热路径不可用】%s\n"
        "  降级到冷路径（会重建 litellm 容器：必须重建才能重读 .env 的新密钥；\n"
        "  restart 不会重读 --env-file；重建会清空内存 session affinity / 前缀缓存）：\n"
        "    %s\n"
        "  排查：① LiteLLM 版本需支持 GET /model/info 与 POST /model/{new,update,delete}；\n"
        "        ② ${POOL_HOME}/.env 的 LITELLM_MASTER_KEY 必须是可调用管理 API 的 master key；\n"
        "        ③ 管理 API 默认监听 127.0.0.1:4000（可用 POOL_LITELLM_URL 覆盖）。"
        % (reason, fallback_command())
    )


# ================================================================ 异常
class ReconcileError(Exception):
    """幂等应用过程中的可诊断错误。"""


class ConfigError(ReconcileError):
    """控制文件/期望态非法：拒绝收敛（尤其防止空 allowlist 误删整池）。"""


class AdminApiUnavailable(ReconcileError):
    """管理 API 不可用（404/401/403/连接失败）：热路径降级。"""


class AdminApiError(ReconcileError):
    """管理 API 业务错误；retryable 表示是否值得重试。"""

    def __init__(self, message, status=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class ApplyError(ReconcileError):
    """应用/校验失败，携带已执行结果供输出。"""

    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result or {}


# ================================================================ 期望态（deployment 集合）
def _egress_model(model):
    return any(model.startswith(m) for m in EGRESS_MODELS)


def _route_of(model):
    if _egress_model(model):
        return ("openai", UPSTREAM_OPENAI)
    return MODEL_ROUTES.get(model, ("openai", DIRECT_OPENAI))


def deployment(model, slot, key, bucket=None):
    """构造一个期望 deployment（与 gen_config.py 的 dep_block/alias_block 逐字段对齐）。

    bucket=None → 兜底 deployment `<model>`；
    bucket=K    → 会话桶别名 `<model>@a<slot>s<K>`。

    2026-10-24（T-sess）：``POOL_DYNAMIC_SESSION=1`` 时**别名不再写静态
    x-opencode-session**（改由 sidecar 按请求注入，经 litellm 头透传），
    兜底 deployment 永远保留静态头（无树键流量靠它）。
    """
    prefix, api_base = _route_of(model)
    if bucket is None:
        name = model
        session = "dsh-acct-%d" % slot
    else:
        name = "%s@a%ds%d" % (model, slot, bucket)
        session = "" if dynamic_session() else "dsh-acct-%d-s%d" % (slot, bucket)
    headers = {"user-agent": POOL_USER_AGENT}
    if session:
        headers[SESSION_HEADER] = session
    return {
        "model_name": name,
        "litellm_params": {
            # 注意：这里是**字面量 key**，不是 os.environ/OCG_KEY_n —— 容器 env 固化，
            # 热路径必须把 .env 的当前值直接推给 LiteLLM（换 key 即 update）。
            "model": "%s/%s" % (prefix, model),
            "api_base": api_base,
            "api_key": key,
            "extra_headers": headers,
        },
        # 2026-10-21：LiteLLM 的 cost map 不认识号池的别名模型 ⇒ 默认“不支持推理”，
        # 客户端一发 reasoning.effort 就被 UnsupportedParamsError 拒掉。对确实支持
        # 思考强度的模型显式登记，热路径（/model/new、/model/update）同样要带。
        "model_info": {"supports_reasoning": True} if model in REASONING_MODELS else {},
    }


def build_desired(config, env, buckets=None):
    """从控制文件快照 + .env 构造期望 deployment 集合：{model_name: deployment}。

    安全闸门（宁可不动，也不做半池/空池）：
      · 控制文件不可读 → ConfigError；
      · allowlist 为空 → ConfigError（否则差异计算会把整池删光）；
      · 没有 enabled 账号 → ConfigError；
      · enabled 账号的 key_var 在 .env 缺失/为空/仍是 REPLACE 占位 → ConfigError。
    """
    if config is None:
        raise ConfigError("控制文件不可读/不可解析（%s）：%s（拒绝变更，保持现状）"
                          % (pool_control.config_path(), pool_control.last_error()))
    models_block = config.get("models")
    allow = None
    # 注意：load_config 返回的是 MappingProxyType，不是 dict 子类；用映射协议判断
    if models_block is not None and hasattr(models_block, "get"):
        allow = models_block.get("allow")
    try:
        models = pool_control._validate_models(allow or ())
    except ValueError as exc:
        raise ConfigError("models.allow 非法: %s" % exc)
    if not models:
        raise ConfigError("models.allow 为空：拒绝收敛（否则会清空整个号池）。"
                          "请先在控制文件里写入至少一个模型。")
    accounts_block = config.get("accounts") or ()
    enabled = []
    for account in accounts_block:
        try:
            slot = int(account.get("slot"))
            key_var = account.get("key_var")
            is_enabled = account.get("enabled", True)
        except (TypeError, ValueError):
            raise ConfigError("accounts 条目非法: %r" % (account,))
        if not is_enabled:
            continue
        key = env.get(key_var)
        if not key or key.strip() == "" or key.startswith("REPLACE"):
            raise ConfigError(
                "账号槽 %s 的密钥变量 %s 在 .env 中缺失/为空/仍是 REPLACE 占位："
                "拒绝热应用（否则会造成半池）。请先写 .env。" % (slot, key_var))
        # 2026-10-20：按账号排除模型（区域限制等）。非法值直接拒绝，不做「静默忽略」——
        # 忽略等于把模型派给承载不了它的账号，客户端会收到 400。
        raw_excl = account.get("models_exclude") or ()
        try:
            excl = frozenset(pool_control._validate_models(raw_excl))
        except ValueError as exc:
            raise ConfigError("账号槽 %s 的 models_exclude 非法：%s" % (slot, exc))
        if excl and len(excl) >= len(models):
            log("WARN: 账号槽 %d 排除了全部 %d 个模型，该账号不会获得任何 deployment" % (slot, len(models)))
        enabled.append((slot, key, excl))
    if not enabled:
        raise ConfigError("控制文件中没有 enabled 账号：拒绝收敛（否则会清空整个号池）")
    bucket_count = session_buckets() if buckets is None else max(1, int(buckets))
    desired = {}
    for model in models:
        for slot, key, excl in enabled:
            if model in excl:
                continue
            dep = deployment(model, slot, key)
            desired[identity(dep)] = dep          # 每账号一条**同名**兜底，不能按 model_name 折叠
            for bucket_index in range(1, bucket_count + 1):
                alias = deployment(model, slot, key, bucket=bucket_index)
                desired[identity(alias)] = alias
    return desired


def _params_signature(params):
    """受管字段的规范化视图（不含 api_key，用于差异比较）。"""
    params = params or {}
    headers = params.get("extra_headers") or {}
    return {
        "model": params.get("model"),
        "api_base": params.get("api_base"),
        "user-agent": headers.get("user-agent"),
        SESSION_HEADER: headers.get(SESSION_HEADER),
    }


def _params_sig_hash(params):
    return hashlib.sha256(json.dumps(_params_signature(params), sort_keys=True,
                                     ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def identity(entry):
    """deployment 的**池内唯一标识**。

    · 兜底 deployment ``<model>``：每个账号一条**同名**条目（LiteLLM 允许同名多
      deployment 做负载均衡），model_name 不唯一 ⇒ 用 ``model_name|x-opencode-session``
      区分（兜底恒为 ``dsh-acct-<slot>``）。
    · 会话桶别名 ``<model>@a<slot>s<K>``：名字天然唯一（含账号+桶）⇒ **直接用
      model_name 做身份**。这样 ``POOL_DYNAMIC_SESSION=1`` 去掉静态 session 头时，
      差异表现为 ``update``（320 条热更新），而不是 ``add 320 + delete 320``
      （中间态池子会短暂少一半且多 640 次 API 调用）。
    """
    name = str(entry.get("model_name") or "")
    if "@a" in name:
        return name
    params = entry.get("litellm_params") or {}
    headers = params.get("extra_headers") or {}
    return "%s|%s" % (name, headers.get(SESSION_HEADER) or "")


_ALIAS_NAME_RE = re.compile(r"@a\d+s\d+$")


def _is_managed(entry):
    """是否为号池自己管理的 deployment（删除前的防误删判定）。

    2026-10-24：动态模式下别名**没有** session 头，旧判据
    （``session.startswith("dsh-acct-")``）会把它们判成"非我管理" ⇒ 被过滤掉后
    差异计算会以为它们不存在而重复 add。改为：UA 相符 **且**
    （session 形如 dsh-acct-* 或 名字是本池的别名命名 ``@a<N>s<K>``）。
    """
    params = entry.get("litellm_params") or {}
    headers = params.get("extra_headers") or {}
    if headers.get("user-agent") != POOL_USER_AGENT:
        return False
    if str(headers.get(SESSION_HEADER) or "").startswith("dsh-acct-"):
        return True
    return _ALIAS_NAME_RE.search(str(entry.get("model_name") or "")) is not None


def _entry_id(entry):
    info = entry.get("model_info") or {}
    return info.get("id") or entry.get("id")


def _key_matches(current_params, wanted_key, state_entry):
    """判断远端现有 api_key 是否已等于期望值。

    远端可能脱敏（含 *）或仍是 os.environ/ 引用，无法直接比对 → 用状态文件里的
    sha256 指纹判断；状态缺失时按「不一致」处理（保守多一次 update，而不是漏更新）。
    """
    current = (current_params or {}).get("api_key")
    if isinstance(current, str) and current:
        if current == wanted_key:
            return True
        if not current.startswith("os.environ/") and "*" not in current:
            return False  # 明确不同（明文）
    if isinstance(state_entry, dict):
        return state_entry.get("key_fp") == pool_control.fingerprint(wanted_key)
    # 远端**不返回 api_key**（LiteLLM 会把它剥掉），本地又没有该 identity 的指纹 = 首轮。
    # 此时无法证明"已轮换"，按"未变"处理：否则首跑会把整池 180 条全部重写一遍。
    # 需要强制下发时用 --force-keys（或先落一次状态文件再轮换密钥）。
    return True


def plan_actions(desired, current, state=None, buckets=None, force_keys=False):
    """差异计算：返回只含差异的计划（不做任何写操作）。

    ``desired`` / ``current`` 都按 :func:`identity` 索引（同名兜底不会互相覆盖）。
    ``force_keys=True`` 时对所有条目强制下发 api_key（显式轮换用）。

    返回 dict：
      {"adds": [deployment...], "updates": [{...deployment, "identity", "id", "reasons"}...],
       "deletes": [{"identity","id","model_name"}...], "unchanged": n, "ignored": n}
    """
    state = state or {}
    current_by_id = {}
    for entry in current or []:
        if not isinstance(entry, dict):
            continue
        key = identity(entry)
        if key not in current_by_id:
            current_by_id[key] = entry
    plan = {"adds": [], "updates": [], "deletes": [], "unchanged": 0, "ignored": 0}
    for key, want in desired.items():
        existing = current_by_id.get(key)
        name = want["model_name"]
        if existing is None:
            plan["adds"].append(want)
            continue
        current_params = existing.get("litellm_params") or {}
        reasons = []
        want_sig = _params_signature(want.get("litellm_params"))
        current_sig = _params_signature(current_params)
        for field in MANAGED_FIELDS:
            if want_sig.get(field) != current_sig.get(field):
                reasons.append("params.%s" % field)
        # model_info.supports_reasoning 也要收敛：老 deployment 是“没有该字段”，
        # 不比较就永远判 unchanged，档位会一直 400。
        want_reasoning = bool((want.get("model_info") or {}).get("supports_reasoning"))
        have_reasoning = bool(((existing.get("model_info") or {}).get("supports_reasoning")))
        if want_reasoning != have_reasoning:
            reasons.append("model_info.supports_reasoning")
        if force_keys or not _key_matches(current_params, want["litellm_params"]["api_key"],
                                          state.get(key)):
            reasons.append("api_key.rotated")
        if reasons:
            plan["updates"].append({
                "identity": key,
                "model_name": name,
                "litellm_params": want["litellm_params"],
                "model_info": dict(want.get("model_info") or {}),
                "id": _entry_id(existing),
                "reasons": reasons,
            })
        else:
            plan["unchanged"] += 1
    for key, existing in current_by_id.items():
        if key in desired:
            continue
        if not _is_managed(existing):
            plan["ignored"] += 1
            continue
        dep_id = _entry_id(existing)
        item = {"identity": key, "model_name": existing.get("model_name")}
        if not dep_id:
            # 无法定位 id 的托管 deployment 不能静默跳过，交给调用方报警
            item["id"] = None
            item["error"] = "托管 deployment 缺少 id，无法删除"
        else:
            item["id"] = dep_id
        plan["deletes"].append(item)
    return plan


def plan_is_empty(plan):
    return not plan["adds"] and not plan["updates"] and not plan["deletes"]


def plan_summary(plan):
    return {
        "add": len(plan["adds"]),
        "update": len(plan["updates"]),
        "delete": len(plan["deletes"]),
        "unchanged": plan["unchanged"],
        "ignored": plan["ignored"],
    }


def plan_actions_json(plan):
    """给看板/CLI 的安全动作清单（api_key 只出现 sha256 指纹，绝不出现明文）。"""
    actions = []
    for dep in plan["adds"]:
        actions.append({"op": "add", "model_name": dep["model_name"], "identity": identity(dep),
                        "key_fingerprint": pool_control.fingerprint(dep["litellm_params"]["api_key"])})
    for item in plan["updates"]:
        actions.append({"op": "update", "model_name": item["model_name"],
                        "identity": item.get("identity"),
                        "deployment_id": item.get("id"), "reasons": item["reasons"],
                        "key_fingerprint": pool_control.fingerprint(item["litellm_params"]["api_key"])})
    for item in plan["deletes"]:
        actions.append({"op": "delete", "model_name": item["model_name"],
                        "identity": item.get("identity"),
                        "deployment_id": item.get("id")})
    return actions


# ================================================================ 管理 API 客户端
class LiteLLMAdminClient:
    """LiteLLM 管理 API 最小客户端（urllib，stdlib）。"""

    def __init__(self, base_url=None, master_key_value=None, timeout=None):
        self.base_url = (base_url or litellm_base_url()).rstrip("/")
        self.master_key = (master_key_value if master_key_value is not None else master_key()).strip()
        self.timeout = timeout if timeout is not None else http_timeout()
        if not self.master_key:
            raise AdminApiUnavailable(
                "缺少 LITELLM_MASTER_KEY（进程环境与 %s/.env 都没有）" % pool_control.pool_home())

    def _request(self, method, path, payload=None):
        url = self.base_url + path
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method=method,
            headers={
                "Authorization": "Bearer " + self.master_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "opencodego-pool-reconciler/1.0",
            })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                detail = exc.read()[:300].decode("utf-8", "replace")
            except Exception:
                detail = ""
            if status in (401, 403):
                raise AdminApiUnavailable(
                    "管理 API 无权限（HTTP %d）：LITELLM_MASTER_KEY 不是 master key 或无管理权限"
                    % status)
            if status == 404 and path == "/model/info":
                raise AdminApiUnavailable(
                    "管理端点不存在（HTTP 404）：当前 LiteLLM 版本可能不支持 /model/info 管理 API")
            retryable = status == 429 or 500 <= status < 600
            raise AdminApiError("管理 API %s %s 返回 HTTP %d: %s"
                                % (method, path, status, detail.replace("\n", " ")[:200]),
                                status=status, retryable=retryable)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AdminApiUnavailable("无法连接 LiteLLM 管理 API（%s）：%s"
                                      % (self.base_url, str(exc)[:160]))
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise AdminApiError("管理 API 返回非 JSON 内容（%s %s）" % (method, path))

    def list_deployments(self):
        """GET /model/info → deployment 列表。"""
        data = self._request("GET", "/model/info")
        if isinstance(data, dict):
            items = data.get("data")
            if items is None:
                items = data.get("models")
        elif isinstance(data, list):
            items = data
        else:
            items = None
        if not isinstance(items, list):
            raise AdminApiError("GET /model/info 返回结构不符合预期（缺少 data 列表）")
        return [item for item in items if isinstance(item, dict)]

    def add_model(self, deployment_dict):
        return self._request("POST", "/model/new", deployment_dict)

    def update_model(self, payload):
        """更新一条 deployment。

        2026-10-21：改走 PATCH /model/{id}/update。旧接口 POST /model/update 只写
        litellm_params —— LiteLLM 仅从 model_info 里取 id 做定位，能力字段
        （supports_reasoning 等）**永远不会落库**，于是热路径每次都判 update、
        档位也一直 400。PATCH 是补丁语义，model_info 会与库内记录合并。
        """
        info = dict(payload.get("model_info") or {})
        model_id = info.get("id")
        if model_id:
            body = {k: v for k, v in payload.items() if k != "model_info"}
            body["model_info"] = info
            return self._request("PATCH", "/model/%s/update" % model_id, body)
        return self._request("POST", "/model/update", payload)

    def delete_model(self, deployment_id):
        return self._request("POST", "/model/delete", {"id": deployment_id})


def _call_with_retries(fn, description):
    """有限次重试：仅对可重试错误退避重试，不可用（404/401/403/网络断）立即上抛。"""
    attempts = retries() + 1
    delay = backoff()
    last_error = None
    for attempt in range(attempts):
        try:
            return fn()
        except AdminApiUnavailable:
            raise
        except AdminApiError as exc:
            last_error = exc
            if not exc.retryable or attempt >= attempts - 1:
                raise
            log("WARN: %s 失败（%s），%.2fs 后重试 %d/%d"
                % (description, str(exc)[:160], delay, attempt + 1, attempts - 1))
            if delay:
                time.sleep(delay)
            delay = min(delay * 2, 5.0) if delay else 0.0
    raise last_error


def probe_capability(client):
    """能力探测：**读 + 写**双重探测，返回 (ok, reason)。

    只探读是不够的（真实踩过）：LiteLLM 未接数据库时 ``GET /model/info`` 一切正常，
    但 ``POST /model/{new,update,delete}`` 返回 500 ``No DB Connected`` ——
    此时"改配置不重启"根本不成立，必须明确报不可用（并给出补救），而不是假装可用。

    写探针用**必然不存在的目标** ``__capability_probe__``：要么因找不到目标而报错
    （说明写接口可响应=可用），要么因未接 DB 而报错（=不可用），都不会创建/修改任何 deployment。
    """
    try:
        items = client.list_deployments()
    except ReconcileError as exc:
        return False, str(exc)
    try:
        client.update_model({"model_name": "__capability_probe__",
                             "litellm_params": {"model": "openai/__capability_probe__"}})
    except (AdminApiUnavailable, AdminApiError) as exc:
        text = str(exc)
        if "no db connected" in text.lower():
            return False, (
                "写路径不可用：LiteLLM 管理 API 的 /model/{new,update,delete} 需要数据库"
                "（%s）。补救：给 LiteLLM 配 DATABASE_URL + store_model_in_db（或改用 compose "
                "带 postgres）后重建容器；在此之前账号/密钥变更只能走冷路径。" % text[:180])
        if isinstance(exc, AdminApiUnavailable):
            return False, text
        # 其它 4xx/5xx：写接口本身可响应（参数/目标类错误），视为写路径可用
        return True, ("GET /model/info 可用（%d 个 deployment）；写接口可响应（%s）"
                      % (len(items), text[:120]))
    return True, "GET /model/info 可用（%d 个 deployment）；写接口可用" % len(items)


# ================================================================ 本地状态（只存指纹）
def state_path():
    return os.path.join(pool_control.pool_home(), STATE_FILE)


def load_state():
    """读 reconciler 状态：优先内置 SQLite（pool_store），不可用回退 JSON 文件。"""
    raw = None
    try:
        if getattr(pool_control, "_store", None) is not None and pool_control._store.enabled():
            raw = pool_control._store.read_doc("reconciler-state")
            if raw is None:                     # 首次启用：把既有 JSON 导入
                pool_control._store.import_json_doc(state_path(), "reconciler-state")
                raw = pool_control._store.read_doc("reconciler-state")
    except Exception:
        raw = None
    if raw is None:
        try:
            with open(state_path(), "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            return {}
    deployments = raw.get("deployments") if isinstance(raw, dict) else None
    if not isinstance(deployments, dict):
        return {}
    return {str(k): v for k, v in deployments.items() if isinstance(v, dict)}


def save_state(entries):
    doc = {
        "version": 1,
        "updated_at": pool_control.now_iso(),
        "litellm_url": litellm_base_url(),
        "buckets": session_buckets(),
        "deployments": entries,
    }
    stored = False
    try:
        if getattr(pool_control, "_store", None) is not None and pool_control._store.enabled():
            pool_control._store.write_doc("reconciler-state", doc, mirror_path=state_path(), mode=0o600)
            stored = True
    except Exception:
        stored = False
    if not stored:
        pool_control.atomic_write_json(state_path(), doc, 0o600)


def _state_entry_for(deployment_dict, deployment_id=None):
    """状态条目：只保存密钥指纹与受管字段签名，绝不保存密钥明文。"""
    params = deployment_dict["litellm_params"]
    return {
        "key_fp": pool_control.fingerprint(params["api_key"]),
        "params_sig": _params_sig_hash(params),
        "id": deployment_id,
    }


# ================================================================ 应用
def apply_plan(plan, client):
    """逐条应用差异；单项失败不阻断其它独立项，最终汇总错误。

    记账用 :func:`identity`（同名兜底各算一条），发给 LiteLLM 的 ``model_name`` 保持原样。
    """
    result = {"added": [], "updated": [], "deleted": [], "errors": []}
    for dep in plan["adds"]:
        name = dep["model_name"]
        key = identity(dep)
        try:
            _call_with_retries(lambda d=dep: client.add_model(d), "新增 %s" % key)
            result["added"].append(key)
        except ReconcileError as exc:
            result["errors"].append({"op": "add", "identity": key, "model_name": name,
                                     "error": str(exc)[:300]})
    for item in plan["updates"]:
        name = item["model_name"]
        key = item.get("identity") or name
        payload = {"model_name": name, "litellm_params": item["litellm_params"]}
        merged_info = dict(item.get("model_info") or {})
        if item.get("id"):
            merged_info["id"] = item["id"]
        if merged_info:
            payload["model_info"] = merged_info
        try:
            _call_with_retries(lambda p=payload: client.update_model(p), "更新 %s" % key)
            result["updated"].append(key)
        except ReconcileError as exc:
            result["errors"].append({"op": "update", "identity": key, "model_name": name,
                                     "error": str(exc)[:300]})
    for item in plan["deletes"]:
        name = item["model_name"]
        key = item.get("identity") or name
        if not item.get("id"):
            result["errors"].append({"op": "delete", "identity": key, "model_name": name,
                                     "error": item.get("error") or "缺少 deployment id"})
            continue
        try:
            _call_with_retries(lambda i=item["id"]: client.delete_model(i), "删除 %s" % key)
            result["deleted"].append(key)
        except ReconcileError as exc:
            result["errors"].append({"op": "delete", "identity": key, "model_name": name,
                                     "error": str(exc)[:300]})
    return result


def verify_converged(desired, current):
    """应用后二次校验：期望 deployment 都存在且受管字段一致。返回 (missing, mismatched)。

    按 :func:`identity` 索引（同名兜底必须逐条校验，不能只校验第一条）。
    """
    by_id = {}
    for entry in current:
        if not isinstance(entry, dict):
            continue
        key = identity(entry)
        if key not in by_id:
            by_id[key] = entry
    missing, mismatched = [], []
    for key, want in desired.items():
        existing = by_id.get(key)
        if existing is None:
            missing.append(key)
            continue
        current_sig = _params_signature(existing.get("litellm_params") or {})
        if current_sig != _params_signature(want.get("litellm_params")):
            mismatched.append(key)
    return missing, mismatched


def _refresh_state(desired, current_after, prior_state, failed_ids):
    """回写状态：成功/未变的条目更新指纹；失败条目保留旧状态以便下轮重试。

    状态文件按 identity 索引（含 session），便于下次判断"密钥是否换过"。
    """
    by_id = {}
    for entry in current_after:
        if not isinstance(entry, dict):
            continue
        key = identity(entry)
        if key not in by_id:
            by_id[key] = entry
    entries = {}
    for key, want in desired.items():
        if key in failed_ids:
            if key in prior_state:
                entries[key] = prior_state[key]
            continue
        current = by_id.get(key)
        deployment_id = _entry_id(current) if current else None
        old = prior_state.get(key) if isinstance(prior_state.get(key), dict) else {}
        entry = _state_entry_for(want, deployment_id or old.get("id"))
        entries[key] = entry
    save_state(entries)
    return entries


# ================================================================ 单轮收敛
def _run_auto_admit(dry_run=False):
    """跑一轮自动准入；任何异常都只记 WARN，绝不影响本轮收敛。

    准入会改 pool-config.json 的 enabled → 必须在读取期望集合**之前**执行。
    """
    report = {"ok": True, "skipped": "module-unavailable",
              "reason": _AUTO_ADMIT_IMPORT_ERROR, "admitted": [], "skipped_accounts": []}
    if auto_admit is None:
        log("WARN: 自动准入模块不可用（%s），跳过" % _AUTO_ADMIT_IMPORT_ERROR)
        return report
    if not auto_admit.enabled():
        log("自动准入已关闭（POOL_AUTO_ADMIT=0）")
        return {"ok": True, "skipped": "disabled", "admitted": [], "skipped_accounts": []}
    try:
        result = auto_admit.run(dry_run=dry_run)
    except Exception as exc:  # 防御：自动准入绝不能让 reconciler 失败
        log("WARN: 自动准入执行异常（不影响收敛）：%s" % str(exc)[:160])
        return {"ok": False, "error": str(exc)[:160], "admitted": [], "skipped_accounts": []}
    result["skipped_accounts"] = result.pop("skipped", [])
    admitted = result.get("admitted") or []
    if admitted:
        log("自动准入：%d 个账号额度已恢复，%s入池：%s"
            % (len(admitted), "将" if dry_run else "",
               ", ".join("slot%s(%s)" % (a.get("slot"), a.get("reason")) for a in admitted)))
    elif result.get("skipped_accounts"):
        log("自动准入：本轮无可入池账号（%s）"
            % "; ".join("slot%s:%s" % (a.get("slot"), a.get("reason"))
                        for a in result["skipped_accounts"]))
    for err in (result.get("errors") or []):
        log("WARN: 自动准入失败项：%s" % json.dumps(err, ensure_ascii=False)[:160])
    return result


def run_once(dry_run=False, force_keys=False, auto_admit_run=True):
    """执行一轮收敛，返回结构化结果 dict；失败抛 ReconcileError 子类。

    ``force_keys=True``：对所有 deployment 强制下发当前 .env 的 api_key（显式轮换用）。
    ``auto_admit_run=False``：跳过本轮自动准入（--no-auto-admit / 排障用）。
    """
    started = time.time()
    admit_report = _run_auto_admit(dry_run=dry_run) if auto_admit_run else None
    config = pool_control.load_config(force=True)
    try:
        env = pool_control.read_env()
    except FileNotFoundError:
        raise ConfigError("缺少 %s：热路径需要从 .env 读取密钥值（控制文件里只有变量名）"
                          % pool_control.env_path())
    desired = build_desired(config, env)
    client = LiteLLMAdminClient()
    current = client.list_deployments()
    prior_state = load_state()
    plan = plan_actions(desired, current, state=prior_state, force_keys=force_keys)
    actions = plan_actions_json(plan)
    summary = plan_summary(plan)
    log("期望 %d 个 deployment（%d 模型 × 账号 × (1+%d 会话桶)），现状 %d 个：%s"
        % (len(desired), len((config.get("models") or {}).get("allow") or ()),
           session_buckets(), len(current), json.dumps(summary, ensure_ascii=False)))
    if plan["deletes"]:
        for item in plan["deletes"]:
            if not item.get("id"):
                log("ERROR: 托管 deployment %s 无 id，无法删除（需要人工处理）" % item["model_name"])
    if dry_run:
        if plan_is_empty(plan):
            log("已收敛，无需变更")
        return {
            "ok": True,
            "dry_run": True,
            "actions": actions,
            "summary": summary,
            "auto_admit": admit_report,
            "elapsed_s": round(time.time() - started, 3),
        }
    if plan_is_empty(plan):
        log("已收敛，无需变更")
        # 关键：即使零变更也要落一次状态基线。否则状态文件永远是空的，
        # 而"远端不返回 api_key + 无本地指纹"按"未变"处理 → 之后换 key 永远检测不到。
        try:
            _refresh_state(desired, current, prior_state, set())
        except Exception as exc:
            log("WARN: 状态基线写入失败（不影响本轮零变更结果）：%s" % str(exc)[:120])
        return {
            "ok": True,
            "dry_run": False,
            "actions": actions,
            "summary": summary,
            "auto_admit": admit_report,
            "elapsed_s": round(time.time() - started, 3),
        }
    log("开始应用：新增 %d / 更新 %d / 删除 %d"
        % (summary["add"], summary["update"], summary["delete"]))
    result = apply_plan(plan, client)
    failed_ids = {err.get("identity") or err.get("model_name") for err in result["errors"]}
    missing, mismatched = [], []
    try:
        current_after = client.list_deployments()
        missing, mismatched = verify_converged(desired, current_after)
    except ReconcileError as exc:
        log("WARN: 应用后校验未能完成：%s" % str(exc)[:160])
        current_after = []
    verify_errors = []
    for name in mismatched:
        verify_errors.append({"op": "verify", "model_name": name, "error": "受管字段与期望不一致"})
    for name in missing:
        verify_errors.append({"op": "verify", "model_name": name, "error": "应用后仍缺失"})
    all_errors = result["errors"] + verify_errors
    applied = {"add": len(result["added"]), "update": len(result["updated"]),
               "delete": len(result["deleted"]), "errors": len(all_errors)}
    try:
        _refresh_state(desired, current_after, prior_state, failed_ids | set(missing) | set(mismatched))
    except Exception as exc:
        log("WARN: 状态文件回写失败（不影响已生效变更）：%s" % str(exc)[:120])
    payload = {
        "ok": not all_errors,
        "dry_run": False,
        "actions": actions,
        "summary": summary,
        "applied": applied,
        "errors": all_errors,
        "auto_admit": admit_report,
        "elapsed_s": round(time.time() - started, 3),
    }
    if all_errors:
        pool_control.append_audit("reconcile.apply", {"applied": applied, "errors": len(all_errors)},
                                  "reconciler", target="litellm", result="error")
        raise ApplyError("收敛失败：%d 个操作/校验错误" % len(all_errors), payload)
    pool_control.append_audit("reconcile.apply", {"applied": applied}, "reconciler", target="litellm")
    return payload


# ================================================================ CLI
def cmd_check():
    try:
        client = LiteLLMAdminClient()
    except ReconcileError as exc:
        reason = str(exc)
        log(unavailable_text(reason))
        emit({"ok": False, "hot_path": "unavailable", "reason": reason,
              "fallback": fallback_command()})
        return EXIT_UNAVAILABLE
    ok, reason = probe_capability(client)
    if ok:
        log("热路径可用：%s" % reason)
        emit({"ok": True, "hot_path": "available", "reason": reason, "fallback": None})
        return EXIT_OK
    log(unavailable_text(reason))
    emit({"ok": False, "hot_path": "unavailable", "reason": reason,
          "fallback": fallback_command()})
    return EXIT_UNAVAILABLE


def _locked(fn):
    """非阻塞文件锁：cron/timer 重叠触发时本轮直接跳过（短命、可并发）。"""
    lock_path = os.path.join(pool_control.pool_home(), LOCK_FILE)
    try:
        with pool_control.file_lock(lock_path, timeout=0.0, blocking=False):
            try:
                fn()
                return EXIT_OK
            except AdminApiUnavailable as exc:
                log(unavailable_text(str(exc)))
                emit({"ok": False, "hot_path": "unavailable", "reason": str(exc),
                      "fallback": fallback_command()})
                return EXIT_UNAVAILABLE
            except ConfigError as exc:
                log("ERROR: %s" % exc)
                emit({"ok": False, "error": {"code": "config", "message": str(exc)}})
                return EXIT_CONFIG
            except ApplyError as exc:
                log("ERROR: %s" % exc)
                payload = dict(exc.result or {})
                payload.setdefault("ok", False)
                payload.setdefault("error", {"code": "apply", "message": str(exc)})
                emit(payload)
                return EXIT_APPLY
            except ReconcileError as exc:
                log("ERROR: %s" % exc)
                emit({"ok": False, "error": {"code": "reconcile", "message": str(exc)}})
                return EXIT_APPLY
    except TimeoutError:
        log("另一个 reconciler 实例正在运行，跳过本轮")
        emit({"ok": True, "skipped": "locked",
              "actions": [], "summary": {"add": 0, "update": 0, "delete": 0,
                                         "unchanged": 0, "ignored": 0}})
        return EXIT_OK


def cmd_once(dry_run=False, force_keys=False, auto_admit_run=True):
    def _run():
        payload = run_once(dry_run=dry_run, force_keys=force_keys, auto_admit_run=auto_admit_run)
        if dry_run:
            log("dry-run 完成：仅预览，未调用任何写接口")
        elif payload.get("applied"):
            log("完成：新增 %d / 更新 %d / 删除 %d%s"
                % (payload["applied"]["add"], payload["applied"]["update"],
                   payload["applied"]["delete"],
                   "" if payload["ok"] else "（有错误）"))
        else:
            log("完成：本轮无需变更（已收敛）")
        emit(payload)
    return _locked(_run)


def cmd_watch(interval, dry_run=False, stop_event=None, auto_admit_run=True):
    """常驻轮询：每 interval 秒跑一轮（--dry-run 时每轮只预览）。

    stop_event 为内部/测试钩子（threading.Event）；生产由 SIGTERM/SIGINT 置停。
    """
    stop = stop_event or threading.Event()

    def _handle(signum, frame):  # noqa: ARG001 - signal handler 签名固定
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError, AttributeError):
            pass
    log("reconciler watch 启动：POOL_HOME=%s, 间隔 %.1fs, dry_run=%s"
        % (pool_control.pool_home(), interval, dry_run))
    while not stop.is_set():
        cmd_once(dry_run=dry_run, auto_admit_run=auto_admit_run)
        deadline = time.monotonic() + interval
        while not stop.is_set() and time.monotonic() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    log("reconciler watch 退出")
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        description="号池热更新应用器：把 pool-config.json + .env 经 LiteLLM 管理 API 热应用，不重启容器。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="跑一轮就退出（默认）")
    mode.add_argument("--apply", action="store_true", help="--once 的别名（看板兼容）")
    mode.add_argument("--watch", action="store_true", help="常驻，按 --interval 周期轮询")
    parser.add_argument("--interval", type=float, default=None,
                        help="--watch 轮询间隔秒数（默认 60，POOL_RECONCILE_INTERVAL 可覆盖）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做的增删改，不调用写接口")
    parser.add_argument("--check", action="store_true", help="只做热路径能力探测")
    parser.add_argument("--force-keys", action="store_true",
                        help="强制对所有 deployment 下发当前 .env 的 api_key（显式轮换/首轮基线用）")
    parser.add_argument("--no-auto-admit", action="store_true",
                        help="跳过本轮自动准入（额度刷新后自动把 enabled=false 的账号放回池内）")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    auto = not args.no_auto_admit
    if args.check:
        return cmd_check()
    if args.watch:
        return cmd_watch(args.interval if args.interval is not None else watch_interval(),
                         dry_run=args.dry_run, auto_admit_run=auto)
    return cmd_once(dry_run=args.dry_run, force_keys=args.force_keys, auto_admit_run=auto)


if __name__ == "__main__":
    sys.exit(main())
