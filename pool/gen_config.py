#!/usr/bin/env python3
"""OpenCode Go 号池配置生成器 v2:
拉取上游模型列表和 .env 中的 OCG_KEY_N,生成 config.yaml.
v2 新增:Go 订阅限额健康门禁,任一窗口 rate-limited 的账号自动剔除出号池,
恢复后自动加回,配置变化才重启 litellm,不变则不动.
手动运行: python3 /opt/litellm/gen_config.py (cron 每 10 分钟自动巡检)
"""
import json, os, subprocess, sys, time, urllib.request

import upstream_policy   # 上游通道策略：仅 Go 订阅，禁用 Zen/Console（见 upstream_policy.py）

ENV_PATH = "/opt/litellm/.env"
CFG_PATH = "/opt/litellm/config.yaml"
GUARD_LOG = "/opt/litellm/guard.log"
# 2026-10-12 fail-closed:usage 查询连续失败计数,上游 API 抖动时坏键能剔除出去,
# 而不是永久 fail-open,成功一次即清零,ALL 剔除时仍走 keep-config 不动.
FAILCOUNT_PATH = "/opt/litellm/usage_failcount.json"
FAIL_CLOSED_AFTER = 3

# ------------------------------------------------ 上游端点 / 客户端身份 / 会话粒度（2026-10-16）
# 依据官方 v2 文档（https://opencode.ai/docs/go/ 的 Endpoints 表）：
#   · Go 订阅按协议分列于 /inference/go/<proto>/v1[...]；
#   · 付费 Console 通道是同主机的 /inference/v1（模型表不同，含 *-free）。
#   · 2026-10-16 实测：/zen/go/v1/models 与 /inference/go/v1/models 返回**逐字节相同**的
#     Go 模型表（3085B）；/zen/v1 与 /inference/v1 同为另一张表（7390B）。
#     ⇒ 旧路径并非"付费通道"，但已改用规范路径。回退只需把下面几行换回
#       https://opencode.ai/zen/go/v1
UPSTREAM_OPENAI = "https://opencode.ai/inference/go/openai/v1"      # chat/completions + responses
UPSTREAM_ANTHROPIC = "https://opencode.ai/inference/go/anthropic"   # + /v1/messages
UPSTREAM_MODELS = "https://opencode.ai/inference/go/v1/models"
UPSTREAM_USAGE = "https://opencode.ai/inference/go/v1/usage"

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
# 官方明文要求客户端自带可识别 UA（"rather than a generic SDK or HTTP-library name"）；
# LiteLLM/curl 的默认 UA 正是被点名的形态，且上游对异常流量有监控。
POOL_USER_AGENT = "dsh-opencodego-pool/1.0"
# 会话级 x-opencode-session 分桶数：同一会话树确定性命中同一桶，上游由此看到
# 「每会话一个 session」而不是「每账号一个巨型 session」，路由与前缀缓存优化才成立。
# 必须与 session-sidecar.py 的 POOL_SESSION_BUCKETS 一致；不一致时 sidecar 自动退回
# 旧别名（<model>@aN），行为与改动前等价，属安全降级。
SESSION_BUCKETS = max(1, int(os.environ.get("POOL_SESSION_BUCKETS", "8") or "8"))

# 2026-10-24（T-sess）：x-opencode-session 改为「sidecar 按请求注入」，部署不再写静态值。
# 动机（实测）：上游按 session 计「快档预算」，一个串用约 5-6 次请求（~5k 输出 token）后
# 被压到 ~100 t/s；号池 8 个固定桶号被全站流量烧热 ⇒ 默认态就是慢档。
# 机制：litellm general_settings.forward_client_headers_to_llm_api=true 会把客户端
#       所有 x-* 头透传上游，于是 sidecar 每请求注入自己的 x-opencode-session 即可。
# 取值：0（默认）= 别名照旧写静态头，行为与改动前逐字节一致；
#       1        = 别名不写静态头，改由 sidecar 注入（**必须先让 sidecar 开始注入**，
#                  否则上游直接 400 MissingSessionID）。
# 注意：**兜底部署 `<model>`（无 @a 后缀）永远保留静态头**——没有树键的流量
#       （curl / 内部脚本直连 :4000 / 未装插件的客户端）不会被改写成别名，
#       它们落在这条兜底部署上，没有静态头就会 400。
DYNAMIC_SESSION = (os.environ.get("POOL_DYNAMIC_SESSION") or "0").strip().lower() in ("1", "true", "yes")

# 模型→(LiteLLM provider 前缀, api_base)。默认 openai 协议；
# claude-haiku-5-5 官方只在 anthropic /v1/messages 提供（旧配置走 chat/completions 必 400）。
MODEL_ROUTES = {
    # claude 走 anthropic 协议；直连反代同样分协议路径
    "claude-haiku-5-5": ("anthropic", DIRECT_ANTHROPIC),
}


def _egress_model(model_name):
    # 地区限制模型（muse-*）必须走日本出口；其余走直连反代。
    return any(model_name.startswith(m) for m in EGRESS_MODELS)


def route_of(model_name):
    if _egress_model(model_name):
        return ("openai", UPSTREAM_OPENAI)
    return MODEL_ROUTES.get(model_name, ("openai", DIRECT_OPENAI))

envs = {}
for line in open(ENV_PATH, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        envs[k] = v.strip().strip('"').strip("'").strip()
key_ids = [k for k in sorted(envs) if k.startswith("OCG_KEY_") and not envs[k].startswith("REPLACE")]

def load_failcounts():
    try:
        with open(FAILCOUNT_PATH) as fh:
            d = json.load(fh)
        return {str(k): int(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_failcounts(d):
    # atomic write of fail counts, avoid partial file
    try:
        tmp = FAILCOUNT_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, FAILCOUNT_PATH)
    except Exception as e:
        print(f"WARN: failcount persist failed ({str(e)[:60]})")


def parse_acct_idx(kid):
    # trailing number of key name, None if no number
    tail = kid.split("_")[-1]
    return int(tail) if tail.isdigit() else None


def usage_windows(key):
    """Go 订阅 usage 三窗口,返回 (windows, query_failed).
    查询失败时,调用方按连续失败计数决策,3 次内 fail-open 视为健康保行为不变,
    连续 FAIL_CLOSED_AFTER 次失败则 fail-closed 剔除.
    """
    try:
        req = urllib.request.Request(UPSTREAM_USAGE,
                                     headers={"Authorization": "Bearer " + key, "User-Agent": POOL_USER_AGENT})
        d = json.load(urllib.request.urlopen(req, timeout=15))["usage"]
        return {w: (d.get(w) or {}).get("status", "unknown") for w in ("rolling", "weekly", "monthly")}, False
    except Exception as e:
        print(f"WARN: usage query failed ({str(e)[:60]})")
        return {w: "ok" for w in ("rolling", "weekly", "monthly")}, True


failcounts = load_failcounts()
failcounts_dirty = False

# 账号健康门禁
healthy, rejected = [], []
for kid in key_ids:
    key = envs[kid]
    wins, qfail = usage_windows(key)
    if qfail:
        failcounts[kid] = failcounts.get(kid, 0) + 1
        failcounts_dirty = True
        if failcounts[kid] >= FAIL_CLOSED_AFTER:
            rejected.append((kid, [f"usage-query-failx{failcounts[kid]}"]))
            print(f"EXCLUDE {kid}: usage query failed {failcounts[kid]}x consecutive (fail-closed)")
            continue
        print(f"WARN: {kid} query failed ({failcounts[kid]}/{FAIL_CLOSED_AFTER}), fail-open this round")
        healthy.append(kid)
        print(f"INCLUDE {kid}: rolling=ok,weekly=ok,monthly=ok")
        continue
    if failcounts.get(kid):
        failcounts[kid] = 0
        failcounts_dirty = True
    limited = [w for w, s in wins.items() if s == "rate-limited"]
    if limited:
        rejected.append((kid, limited))
        print(f"EXCLUDE {kid}: rate-limited windows = {','.join(limited)}")
    else:
        healthy.append(kid)
        print(f"INCLUDE {kid}: " + ",".join(f"{w}={s}" for w, s in wins.items()))

if failcounts_dirty:
    save_failcounts(failcounts)

if not healthy:
    print("ALL accounts rate-limited, keep current config unchanged, restart skipped")
    with open(GUARD_LOG, "a", encoding="utf-8") as f:
        f.write(f"{subprocess.run(['date','+%F %T'], capture_output=True, text=True).stdout.strip()} "
                f"ALL-LIMITED rejected={[k for k,_ in rejected]}\n")
    sys.exit(0)

keys = [envs[k] for k in healthy]
healthy_idx = {kid: parse_acct_idx(kid) for kid in healthy if parse_acct_idx(kid) is not None}
for kid in [k for k in healthy if k not in healthy_idx]:
    print(f"WARN: skip key without number suffix: {kid}")
healthy = [kid for kid in healthy if kid in healthy_idx]
keys = [envs[k] for k in healthy]
if not healthy:
    print("WARN: all keys skipped by name check, keep config unchanged, restart skipped")
    sys.exit(0)

models = None
models_err = ""
for _key in keys:
    try:
        _req = urllib.request.Request(UPSTREAM_MODELS,
                                      headers={"Authorization": "Bearer " + _key, "User-Agent": POOL_USER_AGENT})
        models = [m["id"] for m in json.load(urllib.request.urlopen(_req, timeout=30))["data"]]
        break
    except Exception as e:
        models_err = str(e)[:60]
        continue
if models is None:
    print(f"WARN: models query failed for all keys ({models_err}), keep config unchanged, restart skipped")
    sys.exit(0)

# 模型 allowlist(2026-09-29 简化):冻结在 DSH 实际引用的 30 个模型上.
# 上游新增模型不再自动入池,config 不因上游模型清单抖动而变化,不触发自动重启.
# 重启会清空 LiteLLM 内存中的 session affinity 钉号,破坏前缀缓存复用.
MODEL_ALLOWLIST = {
    # 2026-10-12:收敛到 DSH 开放 5 模型(4+dsv4.1),其余 26 个全池停用.
    # 注意:改动触发 litellm 重启会清空内存会话亲和,即前缀缓存重建.
    "mimo-v2.6-flash", "mimo-v2.6-pro",
    "muse-spark-1.3-contributor", "claude-haiku-5-5", "deepseek-v4.1-flash",
}
# 2026-10-17：模型白名单优先取 SQLite 控制面（pool-config.models.allow），
# 让「管理控制台改白名单」直接生效；读不到才用上面的冻结集合。
try:
    import pool_control as _pc
    _c = _pc.load_config(force=True) or {}
    _allow = [m for m in ((_c.get("models") or {}).get("allow") or []) if isinstance(m, str)]
    if _allow:
        MODEL_ALLOWLIST = set(_allow)
        print("allowlist(source=pool_store): %d" % len(_allow))
except Exception as _e:
    print("allowlist(source=builtin): %s" % str(_e)[:60])

# 2026-10-20：按账号排除模型（控制面 accounts[].models_exclude）。
# 典型场景：某工作区没开 Global 区域，deepseek 会 400，必须先把它从该账号的 deployment 里去掉。
_MODELS_EXCLUDE = {}
try:
    _cfg = globals().get("_c") or {}
    for _a in (_cfg.get("accounts") or ()):
        try:
            _slot = int(_a.get("slot"))
        except (TypeError, ValueError, AttributeError):
            continue
        _ex = _a.get("models_exclude") or ()
        if _ex:
            _MODELS_EXCLUDE[_slot] = set(str(x) for x in _ex)
    if _MODELS_EXCLUDE:
        print("models_exclude: " + ", ".join(
            "a%d 不含 %s" % (s, ",".join(sorted(v))) for s, v in sorted(_MODELS_EXCLUDE.items())))
except Exception as _e:
    print("WARN: models_exclude 读取失败（%s），按全模型展开" % str(_e)[:60])

skipped = [m for m in models if m not in MODEL_ALLOWLIST]
models = [m for m in models if m in MODEL_ALLOWLIST]
if not models:
    print("WARN: allowlist filtered out all models, keep config unchanged, restart skipped")
    sys.exit(0)
print(f"upstream models: {len(models)} (allowlist, skipped {len(skipped)}), "
      f"healthy accounts: {len(healthy)}/{len(key_ids)}")

# 单账号钉住别名:供 dsh agent-teams 成员会话复用上游 prompt 缓存,即会话亲和.
# 格式为别名到上游模型和钉住账号序号的映射,目前为空,回归纯负载均衡.
PINNED_ALIASES = {}  # 2026-09-25:号池不再提供 pinned 单账号别名,回归纯负载均衡,历史见 .bak-unpinned

def _session_id_for(acct_idx, bucket=None):
    """上游 x-opencode-session 值。

    非别名 deployment → 账号级常量（无树键的外来流量兜底，行为同改动前）；
    别名 deployment     → 「账号×桶」常量，使每棵会话树在上游独占一个 session。
    """
    if bucket is None:
        return f"dsh-acct-{acct_idx}"
    return f"dsh-acct-{acct_idx}-s{bucket}"


# 2026-10-21：LiteLLM 对**自己 cost map 里不认识**的模型，一律按“不支持推理”处理：
# 客户端一发 reasoning.effort / thinking，它就在转发前抛
#   UnsupportedParamsError: <model> doesn't support `reasoning.effort`
#   (its model cost map entry lacks `supports_reasoning`)
# 号池的模型名是上游别名（muse-spark-1.3-contributor 等），LiteLLM 的 cost map
# 只有 openrouter/meta/muse-spark-* 这种带前缀的键 → 匹配不上 → 档位全被拒。
# 这里给**确实支持思考强度**的模型显式登记 model_info.supports_reasoning，
# 让 LiteLLM 放行该参数（能力本身仍由上游决定）。
REASONING_MODELS = {
    "mimo-v2.6-flash",
    "mimo-v2.6-pro",
    "muse-spark-1.3-contributor",
    "claude-haiku-5-5",
    "deepseek-v4.1-flash",
}


def model_info_lines(model_name, indent="    "):
    """deployment 的 model_info 段（YAML 模式）；非推理模型返回空列表。"""
    if model_name not in REASONING_MODELS:
        return []
    return [
        f"{indent}model_info:",
        f"{indent}  supports_reasoning: true",
    ]


def dep_block(model_name, acct_idx):
    """一个 deployment 块,acct_idx 为 OCG_KEY_N 的 N（无树键流量的兜底路径）。"""
    prefix, api_base = route_of(model_name)
    return [
        f"  - model_name: {model_name}",
        "    litellm_params:",
        f"      model: {prefix}/{model_name}",
        f"      api_base: {api_base}",
        f"      api_key: os.environ/OCG_KEY_{acct_idx}",
        "      extra_headers:",
        f"        user-agent: {POOL_USER_AGENT}",
        f"        x-opencode-session: {_session_id_for(acct_idx)}",
    ] + model_info_lines(model_name)


def dep_spec(model_name, acct_idx, bucket=None):
    """把一条 deployment 规格化成 dict（供 --dump-deployments / adopt 脚本消费）。"""
    prefix, api_base = route_of(model_name)
    name = model_name if bucket is None else "%s@a%ds%d" % (model_name, acct_idx, bucket)
    # 兜底部署永远带静态 session；别名在动态模式下为空（由 sidecar 注入）
    if bucket is None:
        session = _session_id_for(acct_idx)
    else:
        session = "" if DYNAMIC_SESSION else _session_id_for(acct_idx, bucket)
    spec = {"model_name": name, "model": "%s/%s" % (prefix, model_name),
            "api_base": api_base, "api_key_env": "OCG_KEY_%d" % acct_idx,
            "user_agent": POOL_USER_AGENT,
            "session": session}
    if model_name in REASONING_MODELS:
        spec["model_info"] = {"supports_reasoning": True}
    return spec


def write_aliases(path, names):
    """把池内别名清单落盘（sidecar 靠它判断"哪些别名真实存在"；DB 化后 config 不再写 model_list）。"""
    tmp = path + ".tmp"
    doc = {"version": 1, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "aliases": sorted(set(names))}
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def alias_block(model_name, acct_idx, bucket):
    """treepin-s4a/s6：单账号×单桶别名组 —— 一个组恰好一个 deployment，命中确定性 100%。

    该别名仅供号池内部使用：sidecar 按会话树键把请求 model 改写为 <model>@a<N>s<K>，
    客户端（含 GET /v1/models）永远看不到别名。

    treepin-s6（2026-10-16）：加入桶维度。改动前同账号所有会话共用一个
    x-opencode-session 常量，上游只能把它们视为同一个会话，"optimize routing and
    prompt caching" 的前提不成立；分桶后每棵会话树独占一个 session（同树恒定、可重放）。
    """
    prefix, api_base = route_of(model_name)
    lines = [
        f"  - model_name: {model_name}@a{acct_idx}s{bucket}",
        "    litellm_params:",
        f"      model: {prefix}/{model_name}",
        f"      api_base: {api_base}",
        f"      api_key: os.environ/OCG_KEY_{acct_idx}",
        "      extra_headers:",
        f"        user-agent: {POOL_USER_AGENT}",
    ]
    # 2026-10-24：动态模式下别名不再写静态 session（由 sidecar 按请求注入）
    if not DYNAMIC_SESSION:
        lines.append(f"        x-opencode-session: {_session_id_for(acct_idx, bucket)}")
    return lines + model_info_lines(model_name)

# 会话粘性(方案B,2026-09-28):带 session_id 的请求钉住首跳 deployment,
# 无 session_id 的照常均衡,同会话前缀缓存恒命中,会话间负载均衡.
# 2026-10-12:思考模型加 encrypted_content_affinity(LiteLLM 原生加密边界亲和,
# 无需缓存、重启不丢:首轮自由均衡,后续凭加密块钉回原 deployment)。
THINKING_MODELS = {"muse-spark-1.3-contributor", "claude-haiku-5-5"}
affinity = "".join(
    f"    {m}: [session_affinity" + (", encrypted_content_affinity" if m in THINKING_MODELS else "") + "]" + ("\n" if i + 1 < len(models) else "")
    for i, m in enumerate(models)
)
def _db_extra_lines():
    """DB 连接串与 store_model_in_db：只从 db-url.txt（600）读，缺省返回空=不启用 DB 模式。

    store_model_in_db 是 /model/new|update|delete 的硬开关（litellm proxy_server.py:4854 读
    general_settings）；打开后启动仍以本 config.yaml 的 model_list 为准（config 里的
    deployment 不进 DB，重启即回到生成态，避免形成第二个事实源）。
    """
    try:
        url = open(os.environ.get("POOL_DB_URL_FILE") or "/opt/litellm/db-url.txt", encoding="utf-8").read().strip()
    except Exception:
        url = ""
    if not url:
        return []
    return ["  database_url: " + url, "  store_model_in_db: true"]


out = ["# 由 gen_config.py 自动生成，请勿手改；增删账号/模型后重跑: python3 /opt/litellm/gen_config.py",
       f"# 2026-09-26 起：Go 订阅限额 rate-limited 的账号自动剔除（当前剔除: "
       f"{', '.join(k for k, _ in rejected) if rejected else '无'})",
       f"# 2026-10-16 起：上游 {UPSTREAM_OPENAI}；UA={POOL_USER_AGENT}；"
       f"别名 <model>@a<N>s<K>，K=1..{SESSION_BUCKETS}（会话级 x-opencode-session）",
       ]
# DB 化（2026-10-17）：POOL_MODEL_LIST_SOURCE=db 时不再写 model_list —— deployment 由
# LiteLLM DB 承载（管理 API 可写），config.yaml 只留 general/router/affinity 设置。
MODEL_LIST_SOURCE = (os.environ.get("POOL_MODEL_LIST_SOURCE") or "config").strip().lower()
ALIAS_NAMES = []
SPECS = []
ml_lines = ["model_list:"]
for m in models:
    for kid in healthy:
        if m in _MODELS_EXCLUDE.get(healthy_idx[kid], ()):
            continue
        ml_lines += dep_block(m, healthy_idx[kid])
        SPECS.append({"model_name": m, "acct": healthy_idx[kid], "bucket": None})
for m in models:
    for kid in healthy:
        if m in _MODELS_EXCLUDE.get(healthy_idx[kid], ()):
            continue
        for _b in range(1, SESSION_BUCKETS + 1):
            ml_lines += alias_block(m, healthy_idx[kid], _b)
            ALIAS_NAMES.append("%s@a%ds%d" % (m, healthy_idx[kid], _b))
            SPECS.append({"model_name": m, "acct": healthy_idx[kid], "bucket": _b})
for alias, (upstream, acct) in PINNED_ALIASES.items():
    if upstream not in models:
        print(f"WARN: pinned upstream model {upstream} not in upstream list, skip {alias}")
        continue
    ml_lines += dep_block(alias, acct)
    ALIAS_NAMES.append(alias)
if MODEL_LIST_SOURCE != "db":
    out += ml_lines
# deployment 清单落进 SQLite 事实源（供看板/审计查询；不存明文密钥）
try:
    import pool_store as _ps
    _rows = []
    for _x in SPECS:
        _d = dep_spec(_x["model_name"], _x["acct"], _x["bucket"])
        _rows.append({"identity": "%s|%s" % (_d["model_name"], _d["session"]),
                      "model_name": _d["model_name"], "model": _d["model"],
                      "slot": _x["acct"], "bucket": _x["bucket"],
                      "params": {"model": _d["model"], "api_base": _d["api_base"],
                                 "api_key_env": _d["api_key_env"],
                                 "x-opencode-session": _d["session"]}})
    print("deployments -> pool.db: %d" % _ps.write_deployments(_rows))
except Exception as _e:
    print("WARN: deployments 未写入 pool.db（%s）" % str(_e)[:80])

if "--dump-deployments" in sys.argv:
    # 打标记：生成过程本身会往 stdout 打印进度，消费方只认这一行
    print("SPECS_JSON:" + json.dumps([dep_spec(x["model_name"], x["acct"], x["bucket"]) for x in SPECS],
                                     ensure_ascii=False))
    raise SystemExit(0)
out += [
    "",
    "litellm_settings:",
    # 2026-09-29 简化:删 num_retries,不重试即不做兜底,request_timeout 提到 3600s.
    # 实测流式生成 p99约230s,max约850s,600s 会腰斩长生成,属于 TPS 干扰.
    "  request_timeout: 3600",
    # 2026-10-16：把上游传输从 aiohttp 换成 httpx。两个动机，第二个才是关键：
    #  ① 上游 TLS 握手偶发被重置（LiteLLM 旧日志 aiohttp/connector.py _start_tls_connection
    #     → ConnectionResetError [Errno 104]），客户端表现为 500 Connection error；
    #  ② **出口路径**：容器有 HTTPS_PROXY 指向宿主机 sing-box（地址由部署机 .env 配置）。
    #     aiohttp 默认不读代理环境变量 → 一直直连出网；httpx 默认读 → 走操作者配置的代理。
    #     实测交错各 30 次：走代理 30/30、均 0.642s；直连 30/30、均 1.104s（两条都健康，代理更快）。
    #     即本键同时把出口从"忽略代理"改为"遵循代理"——这正是容器配 HTTPS_PROXY 的本意。
    # 注意：若日后要强制直连，应给容器补 NO_PROXY 含 opencode.ai（改 env 需重建容器），
    #       而不是把本键关掉（关掉只会回到 aiohttp 的隐式直连）。
    "  disable_aiohttp_transport: true",
    "  callbacks: pool_logger.pool_logger",
    "",
    "router_settings:",
    "  routing_strategy: least-busy",
    # 2026-09-29 简化:删 enable_tag_filtering/tags,决策引擎已停用,删 allowed_fails,
    # cooldown_time/num_retries,熔断加重试是限速悬崖与兜底,是 subagent 429 停滞的放大器.
    # affinity TTL 从 21600(6h)调到 2592000(30d),闲置会话钉号不再过期迁移,守住前缀缓存命中率.
    "  deployment_affinity_ttl_seconds: 2592000",
    "  model_group_affinity_config:",
    affinity,
    "",
    "general_settings:",
    "  master_key: os.environ/LITELLM_MASTER_KEY",
    # 2026-10-24（T-sess）：把客户端 x-* 头透传上游 —— sidecar 靠它每请求注入
    # x-opencode-session（litellm 1.102.1 的 _get_forwardable_headers 会转发所有 x- 头）。
    # 必须写在生成器里：手改 config.yaml 会被 */10 的 guard（gen_config 重跑）覆盖掉。
    "  forward_client_headers_to_llm_api: true",
    *_db_extra_lines(),
]
new_content = "\n".join(out) + "\n"
# deployment 数 = Σ(该账号可承载模型数 × (1 兜底 + SESSION_BUCKETS 会话桶)) + 钉住别名
# 2026-10-20：account 级 models_exclude 会让各账号不相等，不能再按「模型×账号」乘出来
DEPLOY_COUNT = len(SPECS) + len(PINNED_ALIASES)

# ---- 上游通道策略（机器强制）：仅 OpenCode Go 订阅；命中 Zen/Console 立即拒绝写盘与重启 ----
_bad_bases = upstream_policy.check_lines(out)
if _bad_bases:
    sys.stderr.write(
        "REFUSING TO WRITE config.yaml —— 上游通道策略要求仅使用 OpenCode Go 订阅（禁用 Zen/Console）：\n"
        + "".join("  · %s → %s\n" % (_b, _r) for _b, _r in _bad_bases)
    )
    sys.exit(4)

# 变化检测,一致则不动
try:
    old = open(CFG_PATH, encoding="utf-8").read()
except FileNotFoundError:
    old = ""
stamp = subprocess.run(["date", "+%F %T"], capture_output=True, text=True).stdout.strip()
ALIAS_FILE = os.path.join(os.path.dirname(CFG_PATH) or ".", "pool-aliases.json")
if old == new_content:
    # 配置没变也要保证别名清单在盘上（DB 化后 sidecar 靠它解析别名）
    write_aliases(ALIAS_FILE, ALIAS_NAMES)
    print("config unchanged, restart skipped")
    sys.exit(0)

# 显式 UTF-8：默认编码在非 UTF-8 平台（Windows GBK）会把中文注释写成乱码
open(CFG_PATH + ".bak-guard", "w", encoding="utf-8").write(old)
open(CFG_PATH, "w", encoding="utf-8").write(new_content)
write_aliases(ALIAS_FILE, ALIAS_NAMES)
print(f"config.yaml written: {DEPLOY_COUNT} deployments "
      f"(was {old.count('- model_name:') if old else 0}), backup: config.yaml.bak-guard")
# treepin-s2: POOL_SKIP_RESTART=1 → 只写配置不重启（受控上线/语义验证用）
if os.environ.get("POOL_SKIP_RESTART") == "1":
    print("POOL_SKIP_RESTART=1: config.yaml written, docker restart skipped")
else:
    r = subprocess.run(["docker", "restart", "litellm"], capture_output=True, text=True, timeout=60)
    print("docker restart litellm:", (r.stdout or r.stderr).strip())
with open(GUARD_LOG, "a", encoding="utf-8") as f:
    f.write(f"{stamp} ROTATE healthy={healthy} rejected={[k for k,_ in rejected]} "
            f"deployments={DEPLOY_COUNT}\n")
