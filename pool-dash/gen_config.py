#!/usr/bin/env python3
"""OpenCode Go 号池配置生成器（pool-dash 侧**脱节副本**，默认 fail-closed）

⚠️ 生产生成器是 /opt/litellm/gen_config.py。本文件与它已脱节：
   · 没有模型白名单（会把上游全部模型入池；服务端只放 5 个）
   · 路由设置不同（usage-based-routing-v2 / allowed_fails / cooldown_time / num_retries / timeout 600）
   误跑会覆盖生产 config.yaml 并 docker restart litellm，连带清空会话亲和与前缀缓存。
   因此默认拒绝执行（exit 3），必须显式放行：
     POOL_DASH_ALLOW_APPLY=print  → 只把生成的 YAML 打到 stdout，不写盘、不重启
     POOL_DASH_ALLOW_APPLY=1      → 真的写 config.yaml 并重启 litellm（仅限明确要用本副本时）
"""
import json, os, subprocess, sys, urllib.request

# upstream_policy 可能不在本目录（生产：/opt/litellm 或 /opt/pool-dash；
# 本地仓库冒烟：仓库的 pool/）——导入前把候选目录补进 sys.path，避免 ModuleNotFoundError
# 让 fail-closed 闸门来不及生效。
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, "/opt/litellm", "/opt/pool-dash",
           os.path.abspath(os.path.join(_HERE, "..", "pool"))):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

import upstream_policy   # 上游通道策略：仅 Go 订阅，禁用 Zen/Console

ENV_PATH = "/opt/litellm/.env"
CFG_PATH = "/opt/litellm/config.yaml"
GUARD_LOG = "/opt/litellm/guard.log"

# 2026-10-16 与服务端 gen_config.py 对齐：规范端点 + 可识别 UA + 会话桶别名
UPSTREAM_OPENAI = "https://opencode.ai/inference/go/openai/v1"
UPSTREAM_ANTHROPIC = "https://opencode.ai/inference/go/anthropic"
UPSTREAM_MODELS = "https://opencode.ai/inference/go/v1/models"
UPSTREAM_USAGE = "https://opencode.ai/inference/go/v1/usage"
POOL_USER_AGENT = "dsh-opencodego-pool/1.0"
SESSION_BUCKETS = max(1, int(os.environ.get("POOL_SESSION_BUCKETS", "8") or "8"))
MODEL_ROUTES = {"claude-haiku-5-5": ("anthropic", UPSTREAM_ANTHROPIC)}


def route_of(model_name):
    return MODEL_ROUTES.get(model_name, ("openai", UPSTREAM_OPENAI))


# ---------------------------------------------------------------- fail-closed 闸门（2026-10-16）
# 放在任何网络/写盘动作之前：默认拒绝执行，避免误跑覆盖生产配置。
_APPLY = os.environ.get("POOL_DASH_ALLOW_APPLY", "")
if _APPLY not in ("1", "print"):
    sys.stderr.write(
        "REFUSING TO RUN (fail-closed) —— 这是 pool-dash 的脱节副本，不是生产生成器。\n"
        "  差异：无模型白名单、路由设置与服务端不同；误跑会覆盖生产 config.yaml 并重启 LiteLLM，\n"
        "        连带清空会话亲和与前缀缓存。\n"
        "  生产请用：  /usr/bin/python3 /opt/litellm/gen_config.py\n"
        "  只看产物：  POOL_DASH_ALLOW_APPLY=print python3 %s   （打到 stdout，不写盘不重启）\n"
        "  确要用本副本：POOL_DASH_ALLOW_APPLY=1 python3 %s\n"
        % (os.path.basename(__file__), os.path.basename(__file__))
    )
    sys.exit(3)

envs = {}
for line in open(ENV_PATH, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        envs[k] = v
key_ids = [k for k in sorted(envs) if k.startswith("OCG_KEY_") and not envs[k].startswith("REPLACE")]

def usage_windows(key):
    """Go 订阅 usage 三窗口；查询失败时 fail-open（视为健康）"""
    try:
        req = urllib.request.Request(UPSTREAM_USAGE,
                                     headers={"Authorization": "Bearer " + key, "User-Agent": POOL_USER_AGENT})
        d = json.load(urllib.request.urlopen(req, timeout=15))["usage"]
        return {w: (d.get(w) or {}).get("status", "unknown") for w in ("rolling", "weekly", "monthly")}
    except Exception as e:
        print(f"WARN: usage query failed ({str(e)[:60]}), fail-open")
        return {w: "ok" for w in ("rolling", "weekly", "monthly")}

# ---------- 账号健康门禁 ----------
healthy, rejected = [], []
for kid in key_ids:
    key = envs[kid]
    wins = usage_windows(key)
    limited = [w for w, s in wins.items() if s == "rate-limited"]
    if limited:
        rejected.append((kid, limited))
        print(f"EXCLUDE {kid}: rate-limited windows = {','.join(limited)}")
    else:
        healthy.append(kid)
        print(f"INCLUDE {kid}: " + ",".join(f"{w}={s}" for w, s in wins.items()))

if not healthy:
    print("ALL accounts rate-limited — keep current config unchanged, restart skipped")
    with open(GUARD_LOG, "a", encoding="utf-8") as f:
        f.write(f"{subprocess.run(['date','+%F %T'], capture_output=True, text=True).stdout.strip()} "
                f"ALL-LIMITED rejected={[k for k,_ in rejected]}\n")
    sys.exit(0)

keys = [envs[k] for k in healthy]
healthy_idx = {kid: int(kid.split("_")[-1]) for kid in healthy}

req = urllib.request.Request(UPSTREAM_MODELS,
                             headers={"Authorization": "Bearer " + keys[0], "User-Agent": POOL_USER_AGENT})
models = [m["id"] for m in json.load(urllib.request.urlopen(req, timeout=30))["data"]]
print(f"upstream models: {len(models)}, healthy accounts: {len(healthy)}/{len(key_ids)}")

# 单账号钉住别名：供 dsh agent-teams 成员会话复用上游 prompt 缓存（会话亲和）。
# {别名: (上游模型, 钉住账号序号)} —— 三个角色模型分摊到三个账号，KEY_4 留作机动。
PINNED_ALIASES = {}  # 2026-09-25: 号池不再提供 pinned 单账号别名，回归纯负载均衡（历史见 .bak-unpinned）

def _session_id_for(acct_idx, bucket=None):
    """上游 x-opencode-session：非别名=账号常量兜底；别名=账号×桶常量（会话级）。"""
    return f"dsh-acct-{acct_idx}" if bucket is None else f"dsh-acct-{acct_idx}-s{bucket}"


def dep_block(model_name, acct_idx):
    """一个 deployment 块；acct_idx 为 OCG_KEY_N 的 N（无树键流量的兜底路径）"""
    prefix, api_base = route_of(model_name)
    return [
        f"  - model_name: {model_name}",
        "    litellm_params:",
        f"      model: {prefix}/{model_name}",
        f"      api_base: {api_base}",
        f"      api_key: os.environ/OCG_KEY_{acct_idx}",
        "      rpm: 30",
        "      tpm: 200000",
        "      max_parallel_requests: 4",
        "      extra_headers:",
        f"        user-agent: {POOL_USER_AGENT}",
        f"        x-opencode-session: {_session_id_for(acct_idx)}",
    ]


def alias_block(model_name, acct_idx, bucket):
    """会话桶别名：sidecar 按树键把 model 改写为 <model>@a<N>s<K>（客户端不可见）。"""
    prefix, api_base = route_of(model_name)
    return [
        f"  - model_name: {model_name}@a{acct_idx}s{bucket}",
        "    litellm_params:",
        f"      model: {prefix}/{model_name}",
        f"      api_base: {api_base}",
        f"      api_key: os.environ/OCG_KEY_{acct_idx}",
        "      rpm: 30",
        "      tpm: 200000",
        "      max_parallel_requests: 4",
        "      extra_headers:",
        f"        user-agent: {POOL_USER_AGENT}",
        f"        x-opencode-session: {_session_id_for(acct_idx, bucket)}",
    ]

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
       f"{', '.join(k for k, _ in rejected) if rejected else '无'}）",
       f"# 2026-10-16 起：上游 {UPSTREAM_OPENAI}；UA={POOL_USER_AGENT}；"
       f"别名 <model>@a<N>s<K>，K=1..{SESSION_BUCKETS}（会话级 x-opencode-session）",
       "model_list:"]
for m in models:
    for kid in healthy:
        out += dep_block(m, healthy_idx[kid])
for m in models:
    for kid in healthy:
        for _b in range(1, SESSION_BUCKETS + 1):
            out += alias_block(m, healthy_idx[kid], _b)
for alias, (upstream, acct) in PINNED_ALIASES.items():
    if upstream not in models:
        print(f"WARN: pinned upstream model {upstream} not in upstream list, skip {alias}")
        continue
    out += dep_block(alias, acct)
out += [
    "",
    "litellm_settings:",
    "  num_retries: 2",
    # 注意：此副本已与服务端 /opt/litellm/gen_config.py 脱节（服务端 09-29 简化：
    # 删重试、request_timeout=3600 保 850s 长生成、least-busy+30天亲和、无熔断；
    # 2026-10-12 诊断见桌面交接文档）。同步前以服务端文件为准，勿直接套用本副本。
    "  request_timeout: 600",
    # 与服务端一致：上游 TLS 握手偶发被重置（aiohttp ConnectionResetError 104），改用 httpx 传输
    "  disable_aiohttp_transport: true",
    "  callbacks: pool_logger.pool_logger",
    "",
    "router_settings:",
    "  routing_strategy: usage-based-routing-v2",
    "  allowed_fails: 3",
    # 同上：服务端现无此三项，同步前以服务端为准。
    "  cooldown_time: 60",
    "  num_retries: 2",
    "",
    "general_settings:",
    "  master_key: os.environ/LITELLM_MASTER_KEY",
    *_db_extra_lines(),
]
new_content = "\n".join(out) + "\n"
DEPLOY_COUNT = len(models) * len(healthy) * (1 + SESSION_BUCKETS)

# ---- 上游通道策略（机器强制）：仅 OpenCode Go 订阅；命中 Zen/Console 立即拒绝 ----
_bad_bases = upstream_policy.check_lines(out)
if _bad_bases:
    sys.stderr.write(
        "REFUSING 上游通道策略违规（仅允许 Go 订阅）：\n"
        + "".join("  · %s → %s\n" % (_b, _r) for _b, _r in _bad_bases)
    )
    sys.exit(4)

if _APPLY == "print":          # 只出产物：绝不写盘、不重启
    sys.stdout.write(new_content)
    sys.stderr.write("# (print 模式) %d deployments；未写盘、未重启\n" % DEPLOY_COUNT)
    sys.exit(0)

# ---------- 变化检测：一致则不动 ----------
try:
    old = open(CFG_PATH, encoding="utf-8").read()
except FileNotFoundError:
    old = ""
stamp = subprocess.run(["date", "+%F %T"], capture_output=True, text=True).stdout.strip()
if old == new_content:
    print("config unchanged — restart skipped")
    sys.exit(0)

# 显式 UTF-8：默认编码在非 UTF-8 平台（Windows GBK）会把中文注释写成乱码
open(CFG_PATH + ".bak-guard", "w", encoding="utf-8").write(old)
open(CFG_PATH, "w", encoding="utf-8").write(new_content)
print(f"config.yaml written: {DEPLOY_COUNT} deployments "
      f"(was {old.count('- model_name:') if old else 0}), backup: config.yaml.bak-guard")
r = subprocess.run(["docker", "restart", "litellm"], capture_output=True, text=True, timeout=60)
print("docker restart litellm:", (r.stdout or r.stderr).strip())
with open(GUARD_LOG, "a") as f:
    f.write(f"{stamp} ROTATE healthy={healthy} rejected={[k for k, _ in rejected]} "
            f"deployments={DEPLOY_COUNT}\n")
