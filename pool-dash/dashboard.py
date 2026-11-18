#!/usr/bin/env python3
"""OpenCode Go 号池看板 v8：数据图表化（零依赖手绘 SVG）
- v3：模型选择（按 API 类型分类）+ 一键同步 DSH 配置
- v5：移除团队规则整块 + 号池去 pinned（PINNED_ALIASES = {}）
- v6：恢复「Agent Teams · 团队模型规则」编辑器（memberModel/maxMembers/depth/profiles），
      独立服务 /opt/dsh-teams/ 与 /works/dsh-teams/ 已下线，本页为唯一入口
- v7/v7.1：账号↔会话分配 + 按模型 token 统计
- v8：图表化——账号限额环形仪表、请求按小时趋势图（/api/summary 新增 by_hour）、
      状态码占比条、按模型横向条形图、账号 token 模型分布堆叠条（全部零依赖 SVG/DOM）
- 模型清单来源：/opt/litellm/config.yaml（号池实际部署的模型全集）
- 其余：账号限额（GET /inference/go/v1/usage）与刷新即拉新（no-store）保持不变
- v13（2026-10-19 dashfix）：以 **v12 为宿主、零删减**加回驾驶舱层 ——
  7 个页签（overview / pool / usage / perf / routing / config / admin），
  管理控制台以**页签**形式内嵌（`pool_admin_ui.build_panel()` 服务端注入，
  复用 Halo/nginx 既有鉴权与 `/api/admin/*` 契约，未引入任何新鉴权），
  深浅双主题（`localStorage` 记忆），1920×1080 / 2560×1440 走 `clamp()` 自适应网格。
  `/admin` 保留为**前缀无关的跳转入口**（200 + `location.replace(...+'#admin')`），
  不再是 404 死链；独立页面形态已下线。
- v14（2026-10-27 烧量路由图表优化）：P2 目标/实际份额图修正横轴 % 被 AX 覆盖成小数的样式问题，
  改为偏差着色条 + 目标刻度线 + 值/偏差标签 + 覆盖统计；P4 分档分布改环形图（中心合计 + 图例数量占比）；
  P1 加汇总条/24h 样本数/耗尽-重置比条与打穿行底纹；P3 命中率阈值条；P5 预算 meter；
  P6 由一排 chip 改为状态卡 + 门禁/临期/告警码/D7 结构化区块。
- v15（2026-10-28 范围下拉驱动历史数据）：此前 summary 只读当前 pool-requests.jsonl，
  logrotate 每天 0 点把旧文件轮转成 pool-requests.jsonl-YYYYMMDD[.gz] 后看板就只剩「今天」。
  现在 reqlog_history() 把当前文件 + 全部归档一起解析（按文件 size/mtime 增量缓存），
  /api/summary?range=all|24|168 按窗口聚合（KPI/模型/时间线/请求趋势/账号/性能/来源/缓存/并发），
  范围下拉切换即拉取对应窗口；「近24h」口径字段仍固定按 24h 计算。
- v16（2026-10-28 单位与账号显示）：Token 一律用国际单位 M（百万）/B（十亿）/T，
  不再出现「G」；账号一律显示邮箱，不再出现 OCG_KEY_n / Key 尾号 / 池位编号等代号——
  routing 面板的 key_ref、告警文案、账号卡、用量表、配额图、账号×模型图全部换成邮箱，
  已下线账号显示「已下线账号」。
- v17（2026-10-31 账号详情排序 + 会话归属修复）：
  1) 账号详情卡在分组内按**邮箱首字母降序**排列（在池 → 已禁用 → 暂存三组不变）；
  2) 账号·会话分配改用 reqlog_history（当前文件 + 全部轮转归档），此前只读容器内当天
     日志 → 历史会话全被误判「未归属」（logRows 786 → 8.9 万，归属会话 3 → 49）；
  3) 未归属会话按 sid 精确判重并标注原因：no-pool-request（日志期内无该会话号池请求，
     多为历史会话/非池模型）或 no-keyref（请求缺 keyRef 无法归属）；
  4) 外网调用判定修复：P2（09-29）把外网收口到 sidecar 后，metadata 也带 pool_src，
     src 恒为 local，外网身份只剩 sess 的 ext- 前缀可辨；原实现只在 src 缺失时才看前缀，
     导致 09-29 之后的外网调用被误记为本地（by_source/来源图/伪会话行全部受影响）。
     summary 与 account-sessions 两处统一改为「src=local 但 sess 以 ext- 开头 → ext」。
- v18（2026-10-31 用量/性能/烧量路由优化）：
  1) 用量表不再显示「已下线账号」：历史 key 从 .env 轮换备份反查槽位 → 显示邮箱，
     并新增「状态」列（在池/已禁用/暂存/无部署/历史 key · 槽位 N/未知 key），
     上游限流或 usage 查询失败另有徽标；
  2) 模型延迟与吞吐：横轴标签旋转 30° 且强制全显（hideOverlap:false），完整展示 12 个模型名；
  3) 烧量路由去掉 P1/P2… 编号，P3 影子命中 / P4 分档分布 / P5 迁移与缓存 / P6 门禁与健康
     合并为一个 2×2 面板（单个 .rt-blocks 看板）。
- v19（2026-10-31 控制台死配置下线）：管理控制台「DSH 配置 · 模型同步与 Agent Teams」
  「Agent Teams · 团队模型规则」「写入 DSH 配置」三块死配置删除（前端面板/JS/接口一并移除），
  后端业务代码整段注释保留并标注弃用；号池 provider 路由由 dsh-pool-affinity 插件
  声明式提供，本看板不再向 DSH profile 写 YAML。
- v20（2026-11-07 月度 Token）：新增「本月 Token」KPI 卡（本地时区每月 1 日 00:00 起累计，
  独立于范围下拉；卡片详情给 Prompt/Completion/请求数/统计起点）。
"""
import json, os, re, shutil, subprocess, threading, time, urllib.request
import bisect
import glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

try:  # 管理控制台（新增；模块缺失/依赖不可用时主看板行为完全不变）
    import pool_admin as _pool_admin
    _ADMIN = _pool_admin.AdminApp()
except Exception:
    _ADMIN = None

import html as _html_mod

try:  # 管理控制台的「页签片段」（可嵌入 HTML，不含 <html>/<head>/<style>）
    import pool_admin_ui as _pool_admin_ui
except Exception:
    _pool_admin_ui = None

LITELLM = "http://127.0.0.1:4000"
UPSTREAM = "https://opencode.ai/inference/go/v1"   # 2026-10-16 规范路径（旧 /zen/go/v1 为别名）
# 上游明文要求客户端自带可识别 UA（禁止通用 SDK/HTTP 库名）
POOL_USER_AGENT = "dsh-opencodego-pool/1.0"
CFG = "/opt/litellm/config.yaml"
ENV = "/opt/litellm/.env"
POOL_CFG = "/opt/litellm/pool-config.json"    # 控制面：槽位 + enabled（唯一真源）
POOL_DB = "/opt/litellm/pool.db"              # 控制面 SQLite（只读：deployments 计数）
# 2026-10-31 弃用（DSH 配置同步下线，见文件后部「弃用」段）：
# DSH_PATCH = "/root/.dsh/profiles/desktop/cordis.patch.yml"
# DSH_SVC = "dsh-desktop.service"
# DEF_CTX, DEF_MAX = "131072", "8192"
# PROVIDERS = ["litellm", "litellm-responses", "litellm-anthropic"]

# ---------- 接口缓存：single-flight + TTL（2026-10-17） ----------
# /api/summary 每个请求原本都独立重跑全量聚合，而前端 15s 轮询短于单次耗时 → 请求叠加。
SUM_TTL = float(os.environ.get("POOL_SUM_TTL") or 10)
SESS_TTL = float(os.environ.get("POOL_SESS_TTL") or 60)
_JSON_CACHE = {}
_JSON_LOCKS = {}


def _cached_json(name, ttl, fn):
    """single-flight + TTL：同名接口同一时刻只算一次，TTL 内复用同一份 JSON 串。"""
    s = _JSON_CACHE.get(name)
    if s and time.time() - s[0] < ttl:
        return s[1]
    lock = _JSON_LOCKS.get(name)
    if lock is None:
        lock = _JSON_LOCKS.setdefault(name, threading.Lock())
    with lock:
        s = _JSON_CACHE.get(name)
        if s and time.time() - s[0] < ttl:
            return s[1]
        body = json.dumps(fn(), ensure_ascii=False)
        _JSON_CACHE[name] = (time.time(), body)
        return body



def env(k):
    for line in open(ENV):
        if line.startswith(k + "="):
            return line.split("=", 1)[1].strip()
    return ""

def _env_lines():
    """一次性读 .env；缺失 → 空列表。绝不把值写日志。"""
    try:
        with open(ENV) as fh:
            return fh.read().splitlines()
    except Exception:
        return []


def ocg_keys():
    """正式号池槽位 key 变量名（`OCG_KEY_n`）——**不含**暂存 `OCG_PENDING_KEY_n`。"""
    ks = []
    for line in _env_lines():
        line = line.strip()
        if line.startswith("OCG_KEY_") and "=" in line and not line.split("=", 1)[1].startswith("REPLACE"):
            ks.append(line.split("=", 1)[0])
    return sorted(ks, key=lambda k: int(k.rsplit("_", 1)[-1]) if k.rsplit("_", 1)[-1].isdigit() else 0)


def ocg_pending_keys():
    """暂存待启用账号（`OCG_PENDING_KEY_n`）：2026-10-19 账号接入时按设计不写 `OCG_KEY_n`，
    gen_config 的发现正则认不出 → 对号池零副作用；但**看板必须显示它们**，
    否则「新增账号没有显示在看板中」（用户报障 root cause）。
    """
    ks = []
    for line in _env_lines():
        line = line.strip()
        if line.startswith("OCG_PENDING_KEY_") and "=" in line \
                and not line.split("=", 1)[1].startswith("REPLACE"):
            ks.append(line.split("=", 1)[0])
    return sorted(ks, key=lambda k: int(k.rsplit("_", 1)[-1]) if k.rsplit("_", 1)[-1].isdigit() else 0)


def pool_config():
    """控制面 pool-config.json：{slot: enabled}（读不到 → {}，不影响其它面板）。"""
    try:
        doc = json.load(open(POOL_CFG))
    except Exception:
        return {}
    out = {}
    for a in (doc.get("accounts") or []) if isinstance(doc, dict) else []:
        try:
            out[int(a.get("slot"))] = bool(a.get("enabled"))
        except (TypeError, ValueError):
            continue
    return out


def pool_slot_deployments():
    """pool.db → {slot: deployment 数}（只读 sqlite；失败 → {}）。

    2026-10-19 起 LiteLLM 已 DB 化，`config.yaml` 不再写 model_list，
    所以「在池」不能再靠 `os.environ/OCG_KEY_n in config.yaml` 判定（会恒为 False →
    全部账号被误标「已剔除」）。以控制面 pool-config.json 的 enabled + pool.db 的实际
    deployment 计数为准。
    """
    try:
        import sqlite3
        conn = sqlite3.connect("file:%s?mode=ro" % POOL_DB, uri=True, timeout=5)
        try:
            return {int(s): int(n) for s, n in
                    conn.execute("SELECT slot, COUNT(*) FROM deployments GROUP BY slot")}
        finally:
            conn.close()
    except Exception:
        return {}


def account_rows():
    """账号三态全集（看板唯一数据源；不含任何密钥值）。

    state: `inpool`（已启用+有 deployment）/ `disabled`（控制面 enabled=false）/
           `pending`（暂存待启用 `OCG_PENDING_KEY_n`，未进控制面）

    排序：inpool → disabled → pending，各自按槽号升序。
    """
    rows = []
    enabled_map = pool_config()
    deps = pool_slot_deployments()
    for k in ocg_keys():
        slot = k.split("_")[-1]
        try:
            n = int(slot)
        except ValueError:
            continue
        on = enabled_map.get(n)
        if on is None:
            on = True          # 控制面读不到 → 退回 .env 存在即视为启用（旧行为）
        n_dep = deps.get(n, 0)
        rows.append({"slot": n, "key_var": k, "pending": False,
                     "enabled": bool(on), "deployments": n_dep,
                     "state": "inpool" if (on and n_dep > 0) else ("disabled" if not on else "noDeploy")})
    for k in ocg_pending_keys():
        slot = k.split("_")[-1]
        try:
            n = int(slot)
        except ValueError:
            continue
        rows.append({"slot": n, "key_var": k, "pending": True, "enabled": False,
                     "deployments": 0, "state": "pending"})
    order = {"inpool": 0, "noDeploy": 1, "disabled": 1, "pending": 2}
    rows.sort(key=lambda r: (order.get(r["state"], 3), r["slot"]))
    return rows


# ---------- 账号邮箱解析（v10：账号位一律邮箱格式展示，禁止密钥外露） ----------
# 解析链：.env OCG_EMAIL_N → 看板映射 account-emails.json → 上游账号资料端点探测 → 占位邮箱。
# 2026-09-28 实测：上游仅 /inference/go/v1/usage 可用（旧 /zen/go/v1/usage 为同一 Go 通道的别名；响应无 email 字段），
# /account /me /profile /user /whoami /keys 等约 30 个候选资料路径均 404，
# 故 1-4 号位需人工在 account-emails.json 补「OCG_KEY_N → 邮箱」映射（该文件只存邮箱，不存密钥）。
EMAIL_MAP = "/opt/pool-dash/account-emails.json"
PROFILE_PATHS = ("/account", "/me", "/profile", "/whoami", "/account/info", "/user/info")
_PROBE_TTL = 6 * 3600          # 上游探测结果（含失败）进程内缓存 6 小时
_EMAIL_PROBE = {}              # key尾12字 → (邮箱, 探测时间)

def email_map():
    try:
        d = json.load(open(EMAIL_MAP))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def probe_upstream_email(key, tail):
    """尝试上游账号资料端点取邮箱；任何路径都取不到就返回 ""（不写日志、不回传密钥）"""
    now = time.time()
    hit = _EMAIL_PROBE.get(tail)
    if hit and now - hit[1] < _PROBE_TTL:
        return hit[0]
    got = ""
    for path in PROFILE_PATHS:
        try:
            req = urllib.request.Request(
                UPSTREAM + path,
                headers={"Authorization": "Bearer " + key, "User-Agent": POOL_USER_AGENT})
            body = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "replace")
            m = re.search(r'"email"\s*:\s*"([^"@]+@[^"]+)"', body)
            if m:
                got = m.group(1).strip()
                break
        except Exception:
            continue
    _EMAIL_PROBE[tail] = (got, now)
    return got

def account_email(slot, key="", key_var=None, email_var=None):
    """号位 → 邮箱；返回 (邮箱, 来源)。来源: env / map / upstream / placeholder

    `key_var`/`email_var` 可覆盖变量名：暂存账号用 `OCG_PENDING_KEY_n` / `OCG_PENDING_EMAIL_n`。
    """
    kv = key_var or ("OCG_KEY_%s" % slot)
    ev = email_var or ("OCG_EMAIL_%s" % slot)
    v = env(ev)
    if "@" in v:
        return v, "env"
    v = str(email_map().get(kv) or "").strip()
    if "@" in v:
        return v, "map"
    if key:
        v = probe_upstream_email(key, key[-12:])
        if "@" in v:
            try:
                m = email_map()
                m[kv] = v
                open(EMAIL_MAP, "w").write(json.dumps(m, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            except Exception:
                pass
            return v, "upstream"
    # 无从得知真实邮箱：给邮箱格式占位（RFC 2606 .invalid 域，绝不可达），前端加「邮箱待补」徽标
    return ("slot%s@pool.invalid" % slot), "placeholder"

def deployed_entries():
    """部署全集：LiteLLM /model/info。DB 化后 config.yaml 不再写 model_list。"""
    try:
        req = urllib.request.Request(LITELLM + "/model/info",
                                     headers={"Authorization": "Bearer " + env("LITELLM_MASTER_KEY")})
        data = json.load(urllib.request.urlopen(req, timeout=10)).get("data")
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def pool_models():
    """号池可用模型全集（/model/info 的 model_name 去别名聚合；回退 config.yaml）"""
    entries = deployed_entries()
    if entries:
        names = {e.get("model_name") for e in entries if e.get("model_name")}
        return sorted({n.split("@")[0] for n in names})
    try:
        return sorted(set(re.findall(r"^  - model_name: (\S+)", open(CFG).read(), re.M)))
    except Exception:
        return []


def fetch_account_usage(name, key, slot="", src=""):
    tail = (key or "")[-12:]
    # 只放邮箱/号位/尾号，绝不带密钥；src=邮箱来源（placeholder 时前端提示补映射）
    ident = {"key": name, "label": name, "tail": tail, "slot": slot, "src": src}
    try:
        req = urllib.request.Request(
            UPSTREAM + "/usage",
            headers={"Authorization": "Bearer " + key, "User-Agent": POOL_USER_AGENT})
        d = json.load(urllib.request.urlopen(req, timeout=12))["usage"]
        windows = {}
        for w in ("rolling", "weekly", "monthly"):
            v = d.get(w) or {}
            windows[w] = {"status": v.get("status", "unknown"),
                          "percent": v.get("percent", 0),
                          "resetsAt": v.get("resetsAt", "")}
        limited = [w for w, v in windows.items() if v["status"] == "rate-limited"]
        return dict(ident, ok=True, windows=windows,
                    limited=limited, state="limited" if limited else "ok")
    except urllib.error.HTTPError as e:
        return dict(ident, ok=False, state="error",
                    error="HTTP %d" % e.code, windows={}, limited=[])
    except Exception as e:
        return dict(ident, ok=False, state="error",
                    error=str(e)[:80], windows={}, limited=[])

def accounts_usage():
    """账号三态全集的额度查询（在池 / 已禁用 / 暂存待启用）。

    - 行来自 `account_rows()`：`OCG_KEY_n`（控制面 enabled）+ `OCG_PENDING_KEY_n`（暂存）；
    - `state`（上游用量三态）与 `poolState`（池内三态）分字段，互不覆盖：
      poolState ∈ inpool / disabled / noDeploy / pending；
    - 暂存账号也查上游 usage（它们是**真实账号**，只是未启用），
      但 `inPool=False`，绝不进入在池统计口径（KPI / 配额图 / 路由）。
    """
    rows = account_rows()
    if not rows:
        return []
    out = []

    def _one(r):
        k = r["key_var"]
        slot = str(r["slot"])
        val = env(k)
        if r["pending"]:
            label, src = account_email(slot, val, key_var=k, email_var="OCG_PENDING_EMAIL_%s" % slot)
        else:
            label, src = account_email(slot, val)
        if not val:
            return dict({"key": label, "label": label, "tail": "", "slot": slot, "src": src},
                        ok=False, state="error", error="%s 未配置" % k,
                        windows={}, limited=[])
        a = fetch_account_usage(label, val, slot, src)
        a.update({"keyVar": k, "poolState": r["state"], "pending": r["pending"],
                  "enabled": r["enabled"], "deployments": r["deployments"],
                  "inPool": r["state"] == "inpool"})
        return a

    with ThreadPoolExecutor(max_workers=min(10, len(rows))) as ex:
        for a in ex.map(_one, rows):
            out.append(a)
    # 2026-10-31：账号详情按邮箱**首字母降序**（用户要求）。
    # 两级稳定排序：先按邮箱降序，再按分组优先级（在池 → 已禁用 → 暂存）升序；
    # Python 排序稳定，所以同组内保持邮箱降序。
    out.sort(key=lambda a: str(a.get("label") or a.get("key") or "").lower(), reverse=True)
    out.sort(key=lambda a: (0 if a.get("poolState") == "inpool" else
                            2 if a.get("poolState") == "pending" else 1))
    return out

# =============================================================================
# 2026-10-31 弃用（用户要求下线死配置）：管理控制台的
#   · 「DSH 配置 · 模型同步与 Agent Teams」
#   · 「Agent Teams · 团队模型规则」
#   · 「写入 DSH 配置」
# 号池接入改由 dsh-pool-affinity 插件（bundle patch 声明式 provider 路由）提供，
# 本看板不再向 DSH profile 写 YAML / 不再改 Agent Teams 预设。
# 下列业务代码整体注释保留，仅作历史参考；前端面板/接口/JS 已同步移除，不要恢复使用。
# =============================================================================
# # ---------- DSH 模型配置读取/同步 ----------
#
# PROV_HEAD = r"      %s:\n(?:        [^\n]*\n)*?        models:\n"
# MODELS_ROW = re.compile(
#     r"- id:\s*(\S+)\s*\n\s+name:\s*(\S+)\s*\n\s+contextWindow:\s*(\d+)\s*\n\s+maxTokens:\s*(\d+)")
#
# def _prov_head(src, name):
#     m = re.search(PROV_HEAD % re.escape(name), src)
#     return m
#
# def dsh_providers_meta(src):
#     """解析三个 provider 的 api/displayName 与当前 models（含 ctx/max 以便保留）"""
#     out = {}
#     for name in PROVIDERS:
#         m = _prov_head(src, name)
#         if not m:
#             out[name] = {"api": "?", "displayName": name, "models": []}
#             continue
#         head = m.group(0)
#         api = re.search(r"api:\s*(\S+)", head)
#         disp = re.search(r"displayName:\s*(.+)", head)
#         tail = src[m.end():]
#         mm = re.match(r"(?:          [^\n]*\n)+", tail)
#         rows = MODELS_ROW.findall(mm.group(0)) if mm else []
#         out[name] = {"api": api.group(1) if api else "?",
#                      "displayName": disp.group(1).strip() if disp else name,
#                      "models": [{"id": r[0], "ctx": r[2], "max": r[3]} for r in rows]}
#     return out
#
# def dsh_models_config():
#     src = open(DSH_PATCH).read()
#     meta = dsh_providers_meta(src)
#     avail = pool_models()
#     # 可用全集 = 号池模型 ∪ 当前已选（避免号池暂时移除导致勾选丢失）
#     for name in PROVIDERS:
#         for m in meta[name]["models"]:
#             if m["id"] not in avail:
#                 avail.append(m["id"])
#     return {"available": sorted(avail),
#             "providers": {n: {"api": meta[n]["api"], "displayName": meta[n]["displayName"],
#                               "selected": [m["id"] for m in meta[n]["models"]]} for n in PROVIDERS}}
#
# # openai-completions 协议下思考强度（reasoningEfforts）的默认声明：
# # 号池 7 个模型 × 7 档位实测（2026-09-28）中 low/medium/high 是**全部模型都接受**的
# # 交集（glm-5.3 拒 minimal、mimo-v2.6 拒 xhigh/max），故新同步进来的 openai-completions
# # 模型默认给这三档，保证 DSH 连接里一选模型就有档位可选且不会被上游 400 拒。
# # 已有声明（含 reasoningEfforts: false 非推理模型）一律原样保留，绝不覆盖。
# DEFAULT_EFFORTS_BLOCK = "            reasoningEfforts:\n              low: low\n              medium: medium\n              high: high\n"
#
# def dsh_efforts_meta(src):
#     """提取每个 provider 下各模型的 `reasoningEfforts:` 子块原文。
#
#     sync_dsh_models 的替换正则 `(?:          [^\\n]*\\n)+` 会把 models: 整块重写
#     （10 空格起的行全被吃掉，含 12/14 空格的档位行），所以必须先按
#     (provider, model id) 捕获，重建时再回填，否则 DSH 连接的思考强度声明会被抹掉。
#     返回 {provider: {model_id: 子块原文}}；没有声明的模型不出现在结果里。
#     """
#     out = {}
#     for name in PROVIDERS:
#         m = _prov_head(src, name)
#         if not m:
#             continue
#         mm = re.match(r"(?:          [^\n]*\n)+", src[m.end():])
#         if not mm:
#             continue
#         cur_id, buf, got = None, None, {}
#         for line in mm.group(0).splitlines(True):
#             idm = re.match(r"          - id:\s*(\S+)", line)
#             if idm:
#                 if buf is not None and cur_id:
#                     got[cur_id] = "".join(buf)
#                 cur_id, buf = idm.group(1), None
#                 continue
#             if re.match(r"^            reasoningEfforts:", line):
#                 if buf is not None and cur_id:
#                     got[cur_id] = "".join(buf)
#                 buf = [line]
#                 continue
#             if buf is not None:
#                 # 14 空格 = 档位条目，属于同一子块；缩进变浅即结束
#                 if line.startswith("              "):
#                     buf.append(line)
#                 else:
#                     if cur_id:
#                         got[cur_id] = "".join(buf)
#                     buf = None
#         if buf is not None and cur_id:
#             got[cur_id] = "".join(buf)
#         out[name] = got
#     return out
#
# def sync_dsh_models(selection):
#     """selection: {provider: [model ids]}；文本替换 models 块 + YAML 校验 + 失败回滚
#
#     重建 models 块时按 (provider, model id) 回填原 reasoningEfforts 子块；
#     openai-completions 协议下新加入、原本无声明的模型补 DEFAULT_EFFORTS_BLOCK，
#     其余协议保持无声明（档位能力随后续阶段再开放）。"""
#     src = open(DSH_PATCH).read()
#     meta = dsh_providers_meta(src)
#     efforts = dsh_efforts_meta(src)
#     ctx_map = {m["id"]: (m["ctx"], m["max"]) for n in PROVIDERS for m in meta[n]["models"]}
#     backup = DSH_PATCH + ".bak-sync-" + time.strftime("%Y%m%d-%H%M%S")
#     shutil.copy2(DSH_PATCH, backup)
#     new_src = src
#     for name in PROVIDERS:
#         ids = sorted(set(selection.get(name) or []))
#         if not ids:
#             continue  # 不允许清空：跳过并保留原状
#         want_default = meta[name]["api"] == "openai-completions"
#         preserved = efforts.get(name, {})
#         body = ""
#         for i in ids:
#             body += (
#                 "          - id: %s\n            name: %s\n            contextWindow: %s\n            maxTokens: %s\n"
#                 % (i, i, ctx_map.get(i, (DEF_CTX, DEF_MAX))[0], ctx_map.get(i, (DEF_CTX, DEF_MAX))[1]))
#             if i in preserved:
#                 body += preserved[i]          # 原样保留部署声明的档位（含 false）
#             elif want_default:
#                 body += DEFAULT_EFFORTS_BLOCK  # openai-completions 新模型给可选档位
#         pat = re.compile("(" + PROV_HEAD % re.escape(name) + r")(?:          [^\n]*\n)+")
#         new_src, n = pat.subn(lambda mm: mm.group(1) + body, new_src, count=1)
#         if n != 1:
#             return {"ok": False, "error": "provider %s models block not found" % name, "backup": backup}
#     try:
#         import yaml
#         yaml.safe_load(new_src.replace("!!js", "_js"))
#     except Exception as e:
#         shutil.copy2(backup, DSH_PATCH)
#         return {"ok": False, "error": "YAML validation failed, rolled back: %s" % str(e)[:120], "backup": backup}
#     open(DSH_PATCH, "w").write(new_src)
#     return {"ok": True, "backup": backup, "counts": {n: len(selection.get(n) or []) for n in PROVIDERS}}
#
# def restart_dsh():
#     try:
#         subprocess.run(["systemctl", "restart", DSH_SVC], timeout=60, check=True)
#     except Exception as e:
#         return {"restarted": False, "error": str(e)[:120]}
#     time.sleep(10)
#     try:
#         st = subprocess.run(["systemctl", "is-active", DSH_SVC],
#                             capture_output=True, text=True, timeout=10).stdout.strip()
#     except Exception as e:
#         st = "unknown: %s" % str(e)[:60]
#     return {"restarted": True, "service": st}
#
# # ---------- Agent Teams 团队模型规则 ----------
#
# TEAMS_ANCHOR = re.compile(r"- id: agent-teams\n")
#
# def teams_config():
#     """读取 cordis.patch.yml 中 agent-teams 条目（memberModel/maxMembers/profiles）"""
#     base = {"memberModel": "", "memberMaxDepth": 0, "maxMembers": 8, "profiles": {}}
#     try:
#         src = open(DSH_PATCH).read()
#     except Exception as e:
#         return {"available": pool_models(), "config": base, "present": False, "error": str(e)[:120]}
#     m = TEAMS_ANCHOR.search(src)
#     if not m:
#         return {"available": pool_models(), "config": base, "present": False}
#     try:
#         import yaml
#         d = yaml.safe_load(src[m.start():].replace("!!js", "_js"))
#         entry = d[0] if isinstance(d, list) else d
#         cfg = (entry or {}).get("config", {}) if isinstance(entry, dict) else {}
#     except Exception as e:
#         return {"available": pool_models(), "config": base, "present": True, "error": "解析失败: %s" % str(e)[:120]}
#     base["memberModel"] = cfg.get("memberModel", "")
#     base["memberMaxDepth"] = cfg.get("memberMaxDepth", 0)
#     base["maxMembers"] = cfg.get("maxMembers", 8)
#     profs = {}
#     for name, p in (cfg.get("profiles") or {}).items():
#         if not isinstance(p, dict):
#             continue
#         members = [{"name": mm.get("name", ""), "role": mm.get("role", "teammate"),
#                     "model": mm.get("model", "")}
#                    for mm in (p.get("members") or []) if isinstance(mm, dict)]
#         profs[name] = {"description": p.get("description", ""), "members": members}
#     base["profiles"] = profs
#     return {"available": pool_models(), "config": base, "present": True}
#
# SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
# SAFE_ROLE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff -]{0,64}$")
#
# def _yaml_str(s):
#     return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')
#
# def save_teams_config(cfg, restart):
#     """校验并重写 agent-teams 块（条目至文件尾），YAML 校验失败回滚；可选重启 dsh"""
#     member_model = str(cfg.get("memberModel") or "").strip()
#     if not SAFE_NAME.match(member_model):
#         return {"ok": False, "error": "memberModel 非法: %s" % member_model[:40]}
#     try:
#         max_members = int(cfg.get("maxMembers", 8))
#         max_depth = int(cfg.get("memberMaxDepth", 0))
#     except Exception:
#         return {"ok": False, "error": "maxMembers / memberMaxDepth 必须是整数"}
#     if not (1 <= max_members <= 64):
#         return {"ok": False, "error": "maxMembers 需在 1-64 之间"}
#     if not (0 <= max_depth <= 3):
#         return {"ok": False, "error": "memberMaxDepth 需在 0-3 之间"}
#     profs_in = cfg.get("profiles") or {}
#     if len(profs_in) > 16:
#         return {"ok": False, "error": "profiles 最多 16 个模板"}
#     valid_models = set(pool_models())
#     parsed = []
#     for pname, p in profs_in.items():
#         if not SAFE_NAME.match(str(pname or "")):
#             return {"ok": False, "error": "模板名称非法: %s（限字母/数字/_-.，且不以-开头）" % pname}
#         members = p.get("members") or []
#         if not (1 <= len(members) <= 32):
#             return {"ok": False, "error": "模板 %s 成员数需在 1-32 之间" % pname}
#         rows = []
#         for mm in members:
#             name = str((mm or {}).get("name") or "").strip()
#             role = str((mm or {}).get("role") or "").strip()
#             model = str((mm or {}).get("model") or "").strip()
#             if not SAFE_NAME.match(name):
#                 return {"ok": False, "error": "成员名非法: %s" % name[:40]}
#             if not SAFE_ROLE.match(role):
#                 return {"ok": False, "error": "成员 %s 的角色非法" % name}
#             if model not in valid_models:
#                 return {"ok": False, "error": "成员 %s 的模型不在号池清单中: %s" % (name, model[:40])}
#             rows.append((name, role or "teammate", model))
#         parsed.append((pname, str((p or {}).get("description") or "").strip(), rows))
#
#     out = ["# 由号池看板「团队模型规则」页管理：保存即重写本块（自动备份 + YAML 校验）。",
#            "- id: agent-teams",
#            "  config:",
#            "    stateDir: .agent-teams",
#            "    memberProvider: spawn",
#            "    memberModel: %s" % member_model,
#            "    memberMaxDepth: %d" % max_depth,
#            "    maxMembers: %d" % max_members]
#     if parsed:
#         out.append("    profiles:")
#         for pname, desc, rows in parsed:
#             out.append("      %s:" % pname)
#             out.append("        description: %s" % _yaml_str(desc or ("团队模板 " + pname)))
#             out.append("        taskPlanning: captain")
#             out.append("        members:")
#             for name, role, model in rows:
#                 out.append("          - name: %s" % name)
#                 out.append("            role: %s" % _yaml_str(role))
#                 out.append("            provider: litellm")
#                 out.append("            model: %s" % model)
#     new_block = "\n".join(out) + "\n"
#
#     try:
#         src = open(DSH_PATCH).read()
#     except Exception as e:
#         return {"ok": False, "error": str(e)[:120]}
#     backup = DSH_PATCH + ".bak-teams-" + time.strftime("%Y%m%d-%H%M%S")
#     shutil.copy2(DSH_PATCH, backup)
#     m = TEAMS_ANCHOR.search(src)
#     if m:
#         # 只替换 agent-teams 块本身（到下一个顶层 "- id: " 为止），保留下游条目
#         nxt = re.search(r"(?m)^- id: ", src[m.end():])
#         blk_end = (m.end() + nxt.start()) if nxt else len(src)
#         new_src = src[:m.start()] + new_block + (("\n" + src[blk_end:]) if blk_end < len(src) else "")
#     else:
#         new_src = src.rstrip("\n") + "\n\n" + new_block
#     try:
#         import yaml
#         yaml.safe_load(new_src.replace("!!js", "_js"))
#     except Exception as e:
#         shutil.copy2(backup, DSH_PATCH)
#         return {"ok": False, "error": "YAML 校验失败已回滚: %s" % str(e)[:120], "backup": backup}
#     open(DSH_PATCH, "w").write(new_src)
#     r = {"ok": True, "backup": backup, "profiles": len(parsed)}
#     if restart:
#         r.update(restart_dsh())
#     return r

# ---------- 账号 ↔ 会话分配（LiteLLM 请求日志 × DSH 会话时间线对齐） ----------

DSH_SESSIONS = "/root/.dsh/sessions"
LITELLM_CONT = "litellm"
LITELLM_REQLOG = "/app/pool-requests.jsonl"
# 会话 assistant 时间与请求完成时间的对齐容差（v12 引用了常量但没定义）
ALIGN_MS = 8000
LITELLM_REQLOG_HOST = os.environ.get("POOL_REQLOG_HOST") or "/opt/litellm/pool-requests.jsonl"
_REQLOG_LOCK = threading.Lock()
_REQLOG_ST = {"key": None, "off": 0, "rows": None}


def _reqlog_read_host():
    """宿主机 bind-mount 直读 + 增量解析；返回 None 表示宿主机不可用（退回 docker exec）。"""
    path = LITELLM_REQLOG_HOST
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_dev, st.st_ino)
    with _REQLOG_LOCK:
        s = _REQLOG_ST
        if s["rows"] is not None and s["key"] == key and st.st_size >= s["off"]:
            if st.st_size == s["off"]:
                return list(s["rows"])
            try:
                with open(path, "rb") as f:
                    f.seek(s["off"])
                    chunk = f.read()
            except OSError:
                return None
            end = chunk.rfind(b"\n")
            if end < 0:
                return list(s["rows"])
            body = chunk[:end + 1]
            s["off"] += len(body)
            for line in body.splitlines():
                try:
                    s["rows"].append(json.loads(line))
                except Exception:
                    pass
            return list(s["rows"])
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return None
        end = body.rfind(b"\n")
        body = body[:end + 1] if end >= 0 else b""
        rows = []
        for line in body.splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
        s.update({"key": key, "off": len(body), "rows": rows})
        return list(rows)


def litellm_request_log():
    """docker exec 读取容器内 pool-requests.jsonl → [{ts,t0,model,keyRef}]"""
    try:
        p = subprocess.run(["docker", "exec", LITELLM_CONT, "cat", LITELLM_REQLOG],
                           capture_output=True, text=True, timeout=15)
        rows = []
        for line in p.stdout.splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
        return rows
    except Exception:
        return []


# ---------- 历史请求日志（含 logrotate 轮转归档）：范围下拉的数据源 ----------
# 背景（2026-10-28 用户报障「看板只展示一天」）：logrotate 每天 0 点把
# /opt/litellm/pool-requests.jsonl 轮转成 pool-requests.jsonl-YYYYMMDD[.gz]，
# 而 summary 之前只读当前文件 → 图表永远只有「今天」。
# 这里把当前文件 + 全部归档一起解析，按文件 (size, mtime) 做增量缓存：
# 归档不变 → 复用；当前文件增长 → 只重解析该文件。
REQLOG_ARCHIVE_GLOB = LITELLM_REQLOG_HOST + "-*"
REQLOG_MAX_FILES = int(os.environ.get("POOL_REQLOG_MAX_FILES") or 30)
# 只保留 summary 聚合需要的字段，避免多天 6 万行把常驻内存撑大
_REQLOG_KEEP = ("ts", "t0", "model", "pt", "ct", "tt", "cached", "keyRef", "sess", "src")
_HIST_LOCK2 = threading.Lock()
_HIST_FILES = {}      # path -> {"sig": (size, mtime), "rows": [row, ...]}
_HIST_ALL = {"rows": [], "sig": None}


def _reqlog_files():
    files = []
    try:
        if os.path.exists(LITELLM_REQLOG_HOST):
            files.append(LITELLM_REQLOG_HOST)
        import glob as _glob
        for p in sorted(_glob.glob(REQLOG_ARCHIVE_GLOB)):
            try:
                if os.path.isfile(p):
                    files.append(p)
            except OSError:
                pass
    except Exception:
        pass
    return files[-REQLOG_MAX_FILES:] if REQLOG_MAX_FILES > 0 else files


def _parse_reqlog_file(path):
    rows = []
    try:
        import gzip as _gzip
        fh = _gzip.open(path, "rb") if path.endswith(".gz") else open(path, "rb")
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not isinstance(d, dict) or not d.get("ts"):
                    continue
                rows.append({k: d.get(k) for k in _REQLOG_KEEP if k in d})
    except Exception:
        return []
    return rows


def reqlog_history():
    """当前请求日志 + 全部轮转归档 → 按 ts 升序的行列表（跨天历史）。"""
    files = _reqlog_files()
    changed = False
    with _HIST_LOCK2:
        for f in files:
            try:
                st = os.stat(f)
            except OSError:
                continue
            sig = (st.st_size, int(st.st_mtime))
            ent = _HIST_FILES.get(f)
            if ent and ent["sig"] == sig:
                continue
            _HIST_FILES[f] = {"sig": sig, "rows": _parse_reqlog_file(f)}
            changed = True
        for f in list(_HIST_FILES):
            if f not in files:
                _HIST_FILES.pop(f, None)
                changed = True
        if changed or _HIST_ALL.get("sig") != tuple(files):
            rows = []
            for f in files:
                ent = _HIST_FILES.get(f)
                if ent:
                    rows.extend(ent["rows"])
            rows.sort(key=lambda r: int(r.get("ts") or 0))
            _HIST_ALL.update({"rows": rows, "sig": tuple(files)})
        return _HIST_ALL["rows"]


def _range_cutoff_ms(range_key):
    """范围下拉 → 起始毫秒时间戳；all/非法值 → 0（不限，即全部日志期）。"""
    try:
        hours = int(str(range_key or "all").strip())
    except Exception:
        return 0
    if hours <= 0:
        return 0
    return int(time.time() * 1000) - hours * 3600 * 1000


def month_start_ms(now=None):
    """本地时区「本月 1 日 00:00」的 epoch 毫秒；now 为秒级时间戳（默认当前时刻）。"""
    ts = time.time() if now is None else float(now)
    lt = time.localtime(ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)) * 1000)


def month_usage(rows, now=None):
    """聚合本自然月的请求/Token（本地 1 日 00:00 起），供「本月 Token」KPI 使用。"""
    since = month_start_ms(now)
    tot = {"hits": 0, "pt": 0, "ct": 0, "tt": 0,
           "since": since, "month": time.strftime("%Y-%m", time.localtime(since / 1000.0))}
    for r in rows or []:
        try:
            ts = int(r.get("ts") or 0)
        except Exception:
            continue
        if ts < since:
            continue
        try:
            pt, ct, tt = int(r.get("pt") or 0), int(r.get("ct") or 0), int(r.get("tt") or 0)
        except Exception:
            pt = ct = tt = 0
        tot["hits"] += 1
        tot["pt"] += pt
        tot["ct"] += ct
        tot["tt"] += tt
    return tot


def account_labels():
    """OCG_KEY_n / OCG_PENDING_KEY_n 值尾 12 字 / os.environ 引用 → 账号邮箱（解析链见 account_email）"""
    mapping = {}
    for k in ocg_keys():
        suffix = k.split("_")[-1]
        label = account_email(suffix, env(k))[0]
        val = env(k)
        if val:
            mapping[val[-12:]] = label
        mapping["os.environ/" + k] = label
    for k in ocg_pending_keys():
        suffix = k.split("_")[-1]
        label = account_email(suffix, env(k), key_var=k,
                              email_var="OCG_PENDING_EMAIL_%s" % suffix)[0]
        val = env(k)
        if val:
            mapping[val[-12:]] = label
        mapping["os.environ/" + k] = label
    return mapping


# ---- 历史 key 尾号 → (槽位, 邮箱)（2026-10-31）--------------------------------
# 背景：key 轮换后，请求日志里旧 key 的尾号在当前 .env 里找不到 → 用量表显示
# 「已下线账号 …尾号」。用户要求改成展示邮箱 + 状态。旧 key 仍可从 .env 轮换备份
# （write_env_value 每次写前自动备份 .env.bak-*）里按 OCG_KEY_n= 反查槽位，再取邮箱。
# 结果缓存 10 分钟（备份文件不常变，且要避免每个 /api/summary 都扫一遍目录）。
_HIST_TAIL_CACHE = {"ts": 0.0, "map": {}}
_HIST_TAIL_TTL = 600.0
_ENV_KEY_RE = re.compile(r"^OCG_KEY_(\d+)\s*=\s*(\S+)\s*$", re.M)


def historic_tail_map():
    """旧 OCG key 尾 12 字 → (slot, email)。只收「不在当前 .env」的尾号。"""
    now = time.time()
    if now - _HIST_TAIL_CACHE["ts"] < _HIST_TAIL_TTL:
        return _HIST_TAIL_CACHE["map"]
    tails = {}
    try:
        paths = sorted(glob.glob("/opt/litellm/.env*"))
    except Exception:
        paths = []
    for path in paths:
        if path == "/opt/litellm/.env":
            continue          # 当前 .env 由 account_labels() 处理
        try:
            txt = open(path, "r", encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for slot, key in _ENV_KEY_RE.findall(txt):
            key = key.strip().strip('"').strip("'")
            if len(key) < 12:
                continue
            tails.setdefault(key[-12:], int(slot))
    current = set(account_labels().keys())
    out = {}
    for tail, slot in tails.items():
        if tail in current:
            continue
        try:
            email = account_email(str(slot), "")[0]
        except Exception:
            email = ""
        if email:
            out[tail] = (slot, email)
    _HIST_TAIL_CACHE.update(ts=now, map=out)
    return out

_SESS_FILE_CACHE = {}


def _parse_dsh_session_file(f):
    """解析单个会话 zstd 文件 → dict；失败/无 id 返回 None。"""
    meta = {"id": "", "createdAt": 0}
    title, first_msg, model = "", "", ""
    times, assistant_ts = [], []
    try:
        p = subprocess.run(["zstd", "-dc", f], capture_output=True, timeout=20)
        for line in p.stdout.splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type", "")
            if t == "session":
                meta = {"id": d.get("id", ""), "createdAt": d.get("createdAt", 0)}
            elif t == "session/title":
                title = ((d.get("data") or {}).get("title") or "").strip()
            elif t == "user/message" and not first_msg:
                for c in ((d.get("data") or {}).get("content") or []):
                    if c.get("type") == "text" and c.get("text"):
                        first_msg = c["text"].strip().replace("\n", " ")
                        break
            elif t == "request/context":
                model = ((d.get("data") or {}).get("model")) or model
            elif t == "assistant/message":
                if d.get("time"):
                    assistant_ts.append(d["time"])
            if d.get("time"):
                times.append(d["time"])
    except Exception:
        return None
    if not meta["id"]:
        return None
    return {"id": meta["id"],
            "title": title or (first_msg[:24] or meta["id"][:22]),
            "firstMsg": first_msg[:80], "model": model,
            "createdAt": meta["createdAt"],
            "lastActive": max(times) if times else meta["createdAt"],
            "assistantTs": assistant_ts}


def scan_dsh_sessions():
    """扫描 DSH 会话文件；按 (mtime_ns,size) 缓存单文件解析，未改动的不再解压。"""
    import glob
    out = []
    for f in glob.glob(DSH_SESSIONS + "/*/session-*/session.v4.jsonl.zstd"):
        parts = f.split("/")
        ws = parts[4] if len(parts) > 5 else "?"
        try:
            st = os.stat(f)
        except OSError:
            continue
        sig = (st.st_mtime_ns, st.st_size)
        hit = _SESS_FILE_CACHE.get(f)
        if hit and hit[0] == sig:
            parsed = hit[1]
        else:
            parsed = _parse_dsh_session_file(f)
            _SESS_FILE_CACHE[f] = (sig, parsed)
        if parsed:
            out.append(dict(parsed, ws=ws))
    return out


def _build_session_idx(sessions):
    """按模型分桶的 (assistantTs, sid) 有序索引，供 ±ALIGN_MS 二分取窗口。"""
    by_m, no_m, allv = {}, [], []
    for s in sessions:
        sid, model = s["id"], s.get("model")
        for at in s["assistantTs"]:
            item = (at, sid)
            allv.append(item)
            if model:
                by_m.setdefault(model, []).append(item)
            else:
                no_m.append(item)
    for v in by_m.values():
        v.sort()
    no_m.sort()
    allv.sort()
    return {"by_model": by_m, "nomodel": no_m, "all": allv}


def _align_sids(idx, model, ts):
    """命中窗口 [ts-ALIGN_MS, ts+ALIGN_MS] 的会话 id 集合（与原三重循环等价）。"""
    lo, hi = ts - ALIGN_MS, ts + ALIGN_MS
    out = set()
    lists = [idx["by_model"].get(model), idx["nomodel"]] if model else [idx["all"]]
    for arr in lists:
        if not arr:
            continue
        i = bisect.bisect_left(arr, (lo, ""))
        j = bisect.bisect_right(arr, (hi, "\U0010ffff"))
        for _, sid in arr[i:j]:
            out.add(sid)
    return out


def account_sessions():
    """账号 → 服务过的会话列表：请求完成时间与会话 assistant 消息时间 ±8s 对齐

    2026-10-31：数据源从「容器内当天日志」改为「本机当前文件 + 全部轮转归档」
    （reqlog_history，含按文件增量缓存）。此前只读当天文件，导致历史会话全部落进
    「未归属会话」——这正是那个框子里孤儿特别多的主因。
    """
    labels = account_labels()
    reqs = reqlog_history() or litellm_request_log()
    sessions = scan_dsh_sessions()
    smap = {s["id"]: s for s in sessions}
    idx = _build_session_idx(sessions)

    def label_of(ref):
        for suf, lab in labels.items():
            if suf and ref.endswith(suf):
                return lab
        # 未映射的 keyRef：历史轮换尾号（不在当前 .env 任何 key 尾号里），绝不当作已知账号
        return ""

    REAL_LABELS = set(labels.values())

    def norm_ref(ref):
        # 2026-10-12 去重：同一账号在日志里有“完整 key”“末 12 位”两种写法，
        # 且 OCG_EMAIL_5/6 共用同一邮箱；归一写法后按邮箱身份聚合，
        # 未映射的保持按原文 key 独立成卡。
        if ref.startswith("os.environ/"):
            return ref
        return ref[-12:] if len(ref) > 12 else ref

    tails_of = {}
    for suf, lab in labels.items():
        if not suf.startswith("os.environ/"):
            tails_of.setdefault(lab, set()).add(suf)

    per_key = {}
    no_key_sids = set()           # 有请求但 keyRef 为空 → 无法归属账号；命中会话用于归因
    for r in reqs:
        k = r.get("keyRef", "")
        if not k:
            # 2026-10-31：keyRef 空（master/virtual key 直连等）仍算「该会话确实调用过」，
            # 只是无法归属到账号；记下来给未归属会话标原因，不参与账号聚合。
            for sid in _align_sids(idx, r.get("model") or "", r.get("ts", 0)):
                no_key_sids.add(sid)
            continue
        nk = norm_ref(k)
        lab = label_of(nk)
        gkey = lab if lab in REAL_LABELS else nk
        e = per_key.setdefault(gkey, {"hits": 0, "models": Counter(), "tokens": Counter(),
                                      "shits": Counter(), "lab": lab, "raws": set(),
                                      "ext": {"hits": 0, "tt": 0, "first": 0, "last": 0, "models": Counter()}})
        e["raws"].add(k)
        e["hits"] += 1
        mname = r.get("model", "?")
        e["models"][mname] += 1
        try:
            tt = int(r.get("tt") or 0)
        except Exception:
            tt = 0
        e["tokens"][mname] += tt
        ts = r.get("ts", 0)
        sess_id = str(r.get("sess") or "")
        src = r.get("src")
        # 2026-10-31：外网收口到 sidecar（P2，09-29）之后，外网请求的 metadata 也带
        # pool_src=sidecar → src 恒为 local；外网身份改由 nginx/侧车注入的 sess 前缀
        # 「ext-」还原。此前只认 src=ext，导致 09-29 之后的外网调用被误记成本地。
        if src not in ("local", "ext"):
            src = "ext" if sess_id.startswith("ext-") else "local"
        elif src == "local" and sess_id.startswith("ext-"):
            src = "ext"
        if src == "ext":
            # 外网调用：匹配不到 DSH 会话，不走 ±8s 对齐，聚合成「外网 API 调用」伪会话行
            ex = e["ext"]
            ex["hits"] += 1
            ex["tt"] += tt
            ex["models"][mname] += 1
            if ts:
                ex["first"] = min(ex["first"] or ts, ts)
                ex["last"] = max(ex["last"], ts)
            continue
        for sid in _align_sids(idx, r.get("model") or "", ts):
            e["shits"][sid] += 1
    accounts = []
    for kref, e in per_key.items():
        rows = []
        for sid, hits in e["shits"].most_common():
            s = smap.get(sid)
            if not s:
                continue
            rows.append({"sid": sid, "title": s["title"], "firstMsg": s["firstMsg"], "model": s["model"],
                         "ws": s["ws"], "createdAt": s["createdAt"],
                         "lastActive": s["lastActive"], "hits": hits})
        rows.sort(key=lambda x: -x["lastActive"])
        ex = e["ext"]
        if ex["hits"]:
            topm = ", ".join(mm for mm, _ in ex["models"].most_common(3))
            rows.append({"title": "外网 API 调用",
                         "firstMsg": "src=ext · %d 次 · %s tokens · %s" % (
                             ex["hits"], "{:,}".format(ex["tt"]), topm or "-"),
                         "model": topm or "-", "ws": "ext",
                         "createdAt": ex["first"], "lastActive": ex["last"],
                         "hits": ex["hits"], "ext": True, "tokens": ex["tt"]})
            rows.sort(key=lambda x: -x["lastActive"])
        models = [{"model": m, "hits": e["models"][m], "tokens": e["tokens"][m]}
                  for m in sorted(e["tokens"], key=lambda mm: -e["tokens"][mm])
                  if e["models"][m] > 0 or e["tokens"][m] > 0]
        rawtails = {r[-12:] for r in e["raws"] if not r.startswith("os.environ/")}
        alltails = sorted(rawtails | tails_of.get(e["lab"], set()))
        accounts.append({"account": e["lab"] or (HIST_LABEL + " …" + (kref or "")[-8:]),
                         "historic": not e["lab"],
                         "tail": (alltails[0] if alltails else (kref or "")[-12:]),
                         "tails": alltails, "refs": len(e["raws"]), "hits": e["hits"],
                         "tokensTotal": sum(e["tokens"].values()),
                         "models": models, "sessions": rows})
    accounts.sort(key=lambda a: -a["hits"])
    # 未分配到任何账号的会话：按 sid 精确判重（标题可能重复），并给出原因标签：
    #   no-pool-request = 日志期（2026-09-26 起）内没有任何该模型的本地号池请求
    #                     （会话用的是官方/非池模型，或早于请求日志上线）
    #   align-miss      = 有该模型的本地请求，但 ±8s 时间窗没对上（并发/长工具循环等）
    matched = {r.get("sid") for a in accounts for r in a["sessions"] if r.get("sid")}
    orphans = []
    for s in sessions:
        if s["id"] in matched or not s.get("assistantTs"):
            continue
        if s["id"] in no_key_sids:
            reason = "no-keyref"        # 有请求但 keyRef 为空，无法归属账号
        else:
            reason = "no-pool-request"  # 日志期（09-26 起）内该会话没有任何号池请求
        orphans.append({"sid": s["id"], "title": s["title"], "firstMsg": s["firstMsg"],
                        "model": s["model"], "ws": s["ws"], "createdAt": s["createdAt"],
                        "lastActive": s["lastActive"], "reason": reason})
    orphans.sort(key=lambda o: -o["lastActive"])
    orph_by_model = Counter(o.get("model") or "?" for o in orphans)
    return {"accounts": accounts, "orphans": orphans,
            "orphansByModel": [{"model": m, "count": c} for m, c in orph_by_model.most_common(8)],
            "sessionsTotal": len(sessions), "logRows": len(reqs),
            "note": "请求级日志自 2026-09-26 起记录（含轮转归档）；本地请求按 assistant 时间 ±8 秒对齐会话，"
                    "外网调用（sess 以 ext- 开头，或历史行 src=ext）聚合成「外网 API 调用」行，"
                    "同刻并发可能偶有归属偏差；同一邮箱多 key / 不同 key 写法已按邮箱合并。"
                    "未归属会话 = 该会话在请求日志（含轮转归档，2026-09-26 起）中没有对应的号池请求"
                    "（多为历史会话 / 走非池模型 / 更早路由），或请求缺少 keyRef 无法归属；详情见 reason 字段。"}

# ---------- 汇总 ----------

# 日志里出现过、但不在当前 .env 任何 key 尾号里的账号尾号（历史轮换/已下线）。
# 用途：绝不把它们和「当前在池账号」混在一个数字里（旧 KPI「活跃账号 9 / 池内 6」即此 bug）。
HIST_LABEL = "已下线账号"


def _peak_avg(events):
    """区间重叠扫描：events=[(t, ±1)] → (峰值并发, 平均并发)"""
    if not events:
        return 0, 0.0
    ev = sorted(events)
    cur = peak = 0
    area = 0
    last = ev[0][0]
    for t, d in ev:
        if t > last:
            area += cur * (t - last)
            last = t
        cur += d
        if cur > peak:
            peak = cur
    span = ev[-1][0] - ev[0][0]
    return peak, (area / span if span > 0 else float(cur))

# ---------- 账号显示名（2026-10-28：看板一律显示邮箱，不再出现 OCG_KEY_n 代号） ----------
_ACCT_REF_RE = re.compile(r"OCG_(?:PENDING_)?KEY_(\d+)")


def _acct_label_map(usage):
    """account_usage → {OCG_KEY_n: 邮箱}；routing 面板里的 key_ref 用它换成可读名字。"""
    m = {}
    for a in usage or []:
        kv, lab = str(a.get("keyVar") or ""), a.get("label") or a.get("key") or ""
        if kv and lab:
            m[kv] = lab
    return m


def _acct_ref_label(ref, labels):
    lab = labels.get(ref)
    if lab:
        return lab
    try:
        em = str(email_map().get(ref) or "").strip()
        if "@" in em:
            return em
    except Exception:
        pass
    m = _ACCT_REF_RE.match(ref)
    return ("已下线账号 #%s" % m.group(1)) if m else ref


def _relabel_routing(node, labels):
    """递归把 routing 载荷里的 OCG_KEY_n 替换成邮箱（仅展示用，不参与判定逻辑）。"""
    if isinstance(node, dict):
        return {k: _relabel_routing(v, labels) for k, v in node.items()}
    if isinstance(node, list):
        return [_relabel_routing(v, labels) for v in node]
    if isinstance(node, str):
        return _ACCT_REF_RE.sub(lambda m: _acct_ref_label(m.group(0), labels), node)
    return node


def summary(range_key="all"):
    """号池总览。range_key：all=全部日志期 / 24=近24h / 168=近7天（非法值按 all）。"""
    out = {"health": "down", "models": 0, "deployments": 0, "accounts": 0,
           "model_names": [], "total": 0, "by_status": {}, "by_model": {}, "by_hour": [],
           "recent": [], "account_usage": [], "accounts_limited": 0,
           "usage_totals": {"hits": 0, "pt": 0, "ct": 0, "tt": 0, "firstTs": 0, "lastTs": 0},
           "last24": {"hits": 0, "pt": 0, "ct": 0, "tt": 0},
           "month_usage": {"hits": 0, "pt": 0, "ct": 0, "tt": 0, "since": 0, "month": ""},
           "by_account": [], "tokens_by_hour": [],
           "model_perf": [], "concurrency": {"peak": 0, "avg": 0, "hours": 0, "requests": 0, "accounts": []},
           "by_source": {"local": {"hits": 0, "pt": 0, "ct": 0, "tt": 0},
                         "ext": {"hits": 0, "pt": 0, "ct": 0, "tt": 0}},
           "cache_rate": {"overall": {"pt": 0, "cached": 0, "rate": 0}, "by_model": [], "by_hour": []},
           "src_by_hour": [], "top_sessions": []}
    try:
        r = urllib.request.urlopen(LITELLM + "/health/liveliness", timeout=8)
        out["health"] = "alive" if r.status == 200 else str(r.status)
    except Exception:
        pass
    try:
        req = urllib.request.Request(LITELLM + "/v1/models",
                                     headers={"Authorization": "Bearer " + env("LITELLM_MASTER_KEY")})
        ids = [m["id"] for m in json.load(urllib.request.urlopen(req, timeout=10))["data"]]
        out["models"] = len(ids)
        out["model_names"] = sorted(ids)
    except Exception:
        pass
    try:
        entries = deployed_entries()
        if entries:
            out["deployments"] = len(entries)
        else:
            cfg = open(CFG).read()
            out["deployments"] = cfg.count("- model_name:")
    except Exception:
        pass
    # ---- 账号口径（dashfix 2026-10-19）：控制面 pool-config.json + .env 三态为准 ----
    # 旧口径 `re.findall(r"os.environ/(OCG_KEY_\d+)", config.yaml)` 在 LiteLLM DB 化后恒为 0
    # （config.yaml 已无 model_list），且认不出 OCG_PENDING_KEY_n → 新增账号全不可见。
    try:
        arows = account_rows()
        pool_rows = [r for r in arows if not r["pending"]]
        out["accounts"] = len(pool_rows)                       # 池位总数（含禁用）
        out["accounts_enabled"] = sum(1 for r in pool_rows if r["enabled"])
        out["accounts_inpool"] = sum(1 for r in pool_rows if r["state"] == "inpool")
        out["accounts_disabled"] = sum(1 for r in pool_rows if not r["enabled"])
        out["accounts_pending"] = sum(1 for r in arows if r["pending"])
        out["accounts_total"] = len(arows)
        out["accounts_deployed"] = sum(1 for r in pool_rows if r["deployments"] > 0)
    except Exception:
        pass
    err_by_hour = {}
    try:
        p = subprocess.run(["docker", "logs", "-t", "--tail", "3000", "litellm"],
                           capture_output=True, text=True, timeout=20)
        pat = re.compile(r"^(\S+Z?)\s+INFO:\s+\S+ - \"(GET|POST) (\S+) HTTP/1\.1\" (\d{3})")
        rows = []
        for line in (p.stdout + p.stderr).splitlines():
            m = pat.match(line)
            if m:
                ts, method, path, code = m.group(1), m.group(2), m.group(3), int(m.group(4))
                # docker logs -t 是 UTC → 转本地时区，与 token 类图表（epoch→本地）对齐
                from datetime import datetime, timezone
                ts_local = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
                # 2026-10-17：内部管理/健康路径不算用户流量（管理控制台每次打开会 POST /model/update 探测，
                # 无 DB 时 500、有 DB 时 400，若计入会把看板"失败"数虚增）。
                if path.startswith("/model/") or path.startswith("/health") or path.startswith("/config"):
                    continue
                rows.append({"ts": ts_local.strftime("%Y-%m-%d %H:%M:%S"), "method": method, "path": path, "code": code})
        rows.reverse()
        # 容器日志只做「最近 3000 条样本」：用于 2xx/4xx/5xx 占比与最近请求列表。
        # 请求总数 / 按小时趋势一律以请求级日志（含轮转归档）为准，见下方 by_hour。
        out["total"] = len(rows)
        out["by_status"] = dict(Counter(r["code"] for r in rows))
        out["by_status_sample"] = len(rows)
        out["recent"] = rows[:15]
        # 失败数按小时：容器日志样本内聚合，覆盖不到的历史小时记 0（图表里只影响「失败」线）
        for r in rows:
            if r["code"] >= 400:
                err_by_hour[r["ts"][:13]] = err_by_hour.get(r["ts"][:13], 0) + 1
    except Exception:
        pass
    # 按模型/账号/时段的用量聚合：来自请求级日志（当前文件 + logrotate 归档，含真实模型名）
    # range_key 决定窗口：all=全部日志期 / 24 / 168 → 统一在这里切，下面所有聚合自动生效。
    hist_rows = reqlog_history()
    cut_ms = _range_cutoff_ms(range_key)
    log_rows = hist_rows if not cut_ms else [r for r in hist_rows if int(r.get("ts") or 0) >= cut_ms]
    # 月度 KPI 固定按自然月聚合，不随 range 下拉变化（每月 1 日 00:00 自动滚动）。
    out["month_usage"] = month_usage(hist_rows)
    try:
        labels = account_labels()
        hist_labels = historic_tail_map()      # 旧 key 尾号 → (slot, 邮箱)，供历史行显示邮箱+状态

        def acct_display(tail):
            """tail → (显示名, 历史槽位 or None)。当前 key 用邮箱；旧 key 从 .env 备份找回。"""
            if tail in labels:
                return labels[tail], None
            hit = hist_labels.get(tail)
            if hit:
                return hit[1], hit[0]
            return ("未知 key …" + (tail or "")[-8:]), None
        agg, per_acct, hour = {}, {}, {}
        perf, iv_by_tail, all_iv = {}, {}, []   # 模型耗时样本 / 每账号请求区间 / 全局请求区间
        bsrc, src_hour = {}, {}                 # 来源(local/ext)用量 / 来源按小时
        cache_m = {}                            # 按模型缓存命中
        sess_agg = {}                           # 按会话聚合（Top10）
        tot = {"hits": 0, "pt": 0, "ct": 0, "tt": 0, "firstTs": 0, "lastTs": 0}
        cut24 = time.time() * 1000 - 24 * 3600 * 1000
        # 「近 24h」口径固定按 24h 算，不随范围下拉变化（KPI/账号表里明确写「近24h」）
        h24_tot = {"hits": 0, "pt": 0, "ct": 0, "tt": 0}
        h24_tail = {}
        for r24 in hist_rows:
            try:
                ts24 = int(r24.get("ts") or 0)
            except Exception:
                ts24 = 0
            if ts24 < cut24:
                continue
            try:
                p24, c24, t24 = int(r24.get("pt") or 0), int(r24.get("ct") or 0), int(r24.get("tt") or 0)
            except Exception:
                p24 = c24 = t24 = 0
            h24_tot["hits"] += 1
            h24_tot["pt"] += p24
            h24_tot["ct"] += c24
            h24_tot["tt"] += t24
            tail24 = (r24.get("keyRef") or "")[-12:]
            if tail24:
                e24 = h24_tail.setdefault(tail24, [0, 0])
                e24[0] += 1
                e24[1] += t24
        for r in log_rows:
            m = r.get("model") or "?"
            try:
                pt, ct, tt = int(r.get("pt") or 0), int(r.get("ct") or 0), int(r.get("tt") or 0)
            except Exception:
                pt = ct = tt = 0
            ts = int(r.get("ts") or 0)
            try:
                t0 = int(r.get("t0") or ts)
            except Exception:
                t0 = ts
            # 耗时 = ts − t0（请求在途时长），>6h 视为脏数据剔除
            dur = (ts - t0) if (ts and t0 and ts > t0) else -1
            if 0 < dur <= 6 * 3600 * 1000:
                p = perf.setdefault(m, {"n": 0, "durs": [], "ct": 0, "tt": 0})
                p["n"] += 1
                p["durs"].append(dur)
                p["ct"] += ct
                p["tt"] += tt
                all_iv.append((t0, ts))
            # 来源与缓存维度：src 由 pool_logger 写（sidecar 注入 pool_src→local，其余→ext）；
            # 历史行无 src 字段时按 sess 的 ext- 前缀判外网，其余计本地
            try:
                cached = int(r.get("cached") or 0)
            except Exception:
                cached = 0
            src = r.get("src")
            sess_v = str(r.get("sess") or "")
            # 2026-10-31：外网收口到 sidecar 后 src 恒为 local，外网身份靠 sess 的 ext- 前缀还原
            if src not in ("local", "ext"):
                src = "ext" if sess_v.startswith("ext-") else "local"
            elif src == "local" and sess_v.startswith("ext-"):
                src = "ext"
            bs = bsrc.setdefault(src, {"hits": 0, "pt": 0, "ct": 0, "tt": 0})
            bs["hits"] += 1
            bs["pt"] += pt
            bs["ct"] += ct
            bs["tt"] += tt
            cm = cache_m.setdefault(m, {"pt": 0, "cached": 0, "hits": 0})
            cm["pt"] += pt
            cm["cached"] += cached
            cm["hits"] += 1
            e = agg.setdefault(m, {"hits": 0, "tokens": 0, "pt": 0, "ct": 0})
            e["hits"] += 1
            e["tokens"] += tt
            e["pt"] += pt
            e["ct"] += ct
            tot["hits"] += 1
            tot["pt"] += pt
            tot["ct"] += ct
            tot["tt"] += tt
            if ts:
                tot["firstTs"] = min(tot["firstTs"] or ts, ts)
                tot["lastTs"] = max(tot["lastTs"], ts)
                h = time.strftime("%Y-%m-%d %H", time.localtime(ts / 1000))
                hh = hour.setdefault(h, {"h": h, "hits": 0, "tt": 0, "pt": 0, "cached": 0})
                hh["hits"] += 1
                hh["tt"] += tt
                hh["pt"] += pt
                hh["cached"] += cached
                sh = src_hour.setdefault(h, {"h": h, "local": 0, "ext": 0})
                sh[src] += 1
            tail = (r.get("keyRef") or "")[-12:]
            if tail:
                if 0 < dur <= 6 * 3600 * 1000:
                    iv_by_tail.setdefault(tail, []).append((t0, ts))
                pa = per_acct.setdefault(tail, {
                    "tail": tail, "account": acct_display(tail)[0],
                    "historic": tail not in labels,
                    "hist_slot": acct_display(tail)[1],
                    "hits": 0, "pt": 0, "ct": 0, "tt": 0, "lastTs": 0,
                    "h24_hits": 0, "h24_tt": 0,
                    "_models": Counter(), "_tokens": Counter()})
                pa["hits"] += 1
                pa["pt"] += pt
                pa["ct"] += ct
                pa["tt"] += tt
                pa["_models"][m] += 1
                pa["_tokens"][m] += tt
                pa["lastTs"] = max(pa["lastTs"], ts)
            # 按会话聚合（本地 hash sess 与外网 ext- sess 都算），供 Top10 图
            skey = str(r.get("sess") or "")
            if skey:
                se = sess_agg.setdefault(skey, {"sess": skey, "hits": 0, "pt": 0, "ct": 0, "tt": 0,
                                                "tails": set(), "models": Counter(), "first": 0, "last": 0})
                se["hits"] += 1
                se["pt"] += pt
                se["ct"] += ct
                se["tt"] += tt
                if tail:
                    se["tails"].add(tail)
                se["models"][m] += 1
                if ts:
                    se["first"] = min(se["first"] or ts, ts)
                    se["last"] = max(se["last"], ts)
        out["by_model"] = [{"model": m, "hits": v["hits"], "tokens": v["tokens"],
                            "pt": v["pt"], "ct": v["ct"]}
                           for m, v in sorted(agg.items(), key=lambda kv: -kv[1]["hits"])]
        out["usage_totals"] = tot
        out["last24"] = h24_tot
        out["tokens_by_hour"] = sorted(hour.values(), key=lambda x: x["h"])
        # 请求计数按小时：来自请求级日志（含轮转归档）；失败数来自容器日志样本（最近 3000 条），
        # 样本覆盖不到的历史小时 err 记 0——「请求趋势」的柱（总量）是全历史的，失败线只覆盖样本窗口。
        out["by_hour"] = [{"h": hh["h"], "n": hh["hits"], "err": err_by_hour.get(hh["h"], 0)}
                          for hh in sorted(hour.values(), key=lambda x: x["h"])]
        if tot["hits"]:
            out["total"] = tot["hits"]           # KPI「总请求」= 当前范围窗口内的请求数
        out["log_span"] = {"firstTs": tot["firstTs"], "lastTs": tot["lastTs"],
                           "hits": tot["hits"], "range": range_key,
                           "files": len(_HIST_FILES),
                           "status_sample": int(out.get("by_status_sample") or 0)}
        accts = []
        for pa in per_acct.values():
            pa["models"] = [{"model": mm, "hits": pa["_models"][mm], "tokens": pa["_tokens"][mm]}
                            for mm in sorted(pa["_tokens"], key=lambda x: -pa["_tokens"][x])]
            h24v = h24_tail.get(pa.get("tail") or "")
            pa["h24_hits"] = h24v[0] if h24v else 0
            pa["h24_tt"] = h24v[1] if h24v else 0
            del pa["_models"], pa["_tokens"]
            accts.append(pa)
        accts.sort(key=lambda x: -x["tt"])
        h24_total = max(1, sum(pa.get("h24_tt", 0) for pa in accts))
        for pa in accts:
            pa["h24_pct"] = round(pa.get("h24_tt", 0) * 100.0 / h24_total, 1)
        out["by_account"] = accts
        # 口径拆分（旧 KPI「活跃账号 9 / 池内 6」的修正）：
        #   历史尾号 = 日志里出现过、但不在当前 .env 任何 key 尾号里（已轮换/下线）→ 单列、不进账号数
        #   活跃 = 当前账号里有调用的（含禁用槽位，但绝不含历史尾号）
        hist_n = sum(1 for pa in accts if pa.get("historic"))
        out["accounts_historic"] = hist_n
        out["accounts_active"] = len(accts) - hist_n
        # 来源分布：by_source(local/ext) + 来源按小时（图：来源分布）
        for k in ("local", "ext"):
            bsrc.setdefault(k, {"hits": 0, "pt": 0, "ct": 0, "tt": 0})
        out["by_source"] = {k: bsrc[k] for k in ("local", "ext")}
        out["src_by_hour"] = sorted(src_hour.values(), key=lambda x: x["h"])
        # 缓存命中率：cached ÷ pt，整体 / 按模型 / 按小时（图：缓存命中率）
        def _rate(c, p):
            return round(c * 100.0 / p, 1) if p else 0.0
        t_pt = sum(v["pt"] for v in cache_m.values())
        t_cached = sum(v["cached"] for v in cache_m.values())
        out["cache_rate"] = {
            "overall": {"pt": t_pt, "cached": t_cached, "rate": _rate(t_cached, t_pt)},
            "by_model": [{"model": mm, "pt": v["pt"], "cached": v["cached"], "hits": v["hits"],
                          "rate": _rate(v["cached"], v["pt"])}
                         for mm, v in sorted(cache_m.items(), key=lambda kv: -kv[1]["pt"])][:12],
            "by_hour": [{"h": hh2["h"], "pt": hh2["pt"], "cached": hh2["cached"],
                         "rate": _rate(hh2["cached"], hh2["pt"])}
                        for hh2 in sorted(hour.values(), key=lambda x: x["h"])]}
        # 按会话 Top10：tokens/轮次/落地账号数（>1 = 粘性偏差告警）
        top_sess = sorted(sess_agg.values(), key=lambda x: -x["tt"])[:10]
        out["top_sessions"] = [
            {"sess": s["sess"], "hits": s["hits"], "pt": s["pt"], "ct": s["ct"], "tt": s["tt"],
             "accounts": len(s["tails"]), "first": s["first"], "last": s["last"],
             "models": [{"model": mm, "hits": c} for mm, c in s["models"].most_common(3)]}
            for s in top_sess]
        # 图表①各模型中位耗时/P90/输出吞吐：pool-requests.jsonl 的 ts−t0 耗时 + ct 输出 token
        mp = []
        for mname, v in perf.items():
            ds = sorted(v["durs"])
            n = len(ds)
            if not n:
                continue
            med = ds[n // 2] if n % 2 else (ds[n // 2 - 1] + ds[n // 2]) // 2
            p90 = ds[min(n - 1, int(n * 0.9))]
            secs = sum(ds) / 1000.0
            mp.append({"model": mname, "n": n, "medMs": med, "p90Ms": p90,
                       "ctPerSec": round(v["ct"] / secs, 1) if secs else 0,
                       "ttPerSec": round(v["tt"] / secs, 1) if secs else 0,
                       "tokens": v["tt"]})
        mp.sort(key=lambda x: -x["n"])
        out["model_perf"] = mp[:12]
        # 图表②账号并发分布：请求区间 [t0, ts] 重叠扫描 → 峰值/平均在途并发
        gl_pk, gl_av = _peak_avg([(a, 1) for a, _ in all_iv] + [(b, -1) for _, b in all_iv])
        span_ms = (max(b for _, b in all_iv) - min(a for a, _ in all_iv)) if all_iv else 0
        conc = []
        for tref, ivs in iv_by_tail.items():
            pk, av = _peak_avg([(a, 1) for a, _ in ivs] + [(b, -1) for _, b in ivs])
            pa = per_acct.get(tref) or {}
            conc.append({"tail": tref,
                         "account": pa.get("account") or acct_display(tref)[0],
                         "historic": bool(pa.get("historic")) or tref not in labels,
                         "peak": pk, "avg": round(av, 2),
                         "hits": pa.get("hits", 0), "tt": pa.get("tt", 0)})
        conc.sort(key=lambda x: (-x["peak"], -x["hits"]))
        out["concurrency"] = {"peak": gl_pk, "avg": round(gl_av, 2),
                              "hours": round(span_ms / 3.6e6, 1), "requests": len(all_iv),
                              "accounts": conc}
    except Exception:
        pass
    try:
        au = accounts_usage()
        out["account_usage"] = au
        # 受限只算在池账号（旧口径把已禁用槽位的限流也算进去 → KPI「受限 2」误导）；
        # 暂存账号（inPool=False）永不进入该口径。
        out["accounts_limited"] = sum(1 for a in au
                                      if a.get("state") == "limited" and a.get("inPool"))
        out["accounts_limited_disabled"] = sum(1 for a in au if a.get("state") == "limited"
                                               and a.get("poolState") == "disabled")
        out["accounts_unavailable"] = sum(1 for a in au
                                          if a.get("state") == "error" and a.get("inPool"))
        # 2026-10-31：用量表每行附「状态」——当前账号取控制面三态；旧 key 标历史槽位；
        # 其余（外网虚拟 key / 测试 key）标外网/未知，避免统一显示成「已下线账号」。
        _ps_label = {"inpool": "在池", "disabled": "已禁用", "pending": "暂存", "noDeploy": "无部署"}
        _au_by_tail = {a.get("tail"): a for a in au if a.get("tail")}
        for _pa in (out.get("by_account") or []):
            _row = _au_by_tail.get(_pa.get("tail"))
            if _row:
                _pa["status"] = _ps_label.get(_row.get("poolState"), str(_row.get("poolState") or "未知"))
                _pa["status_key"] = _row.get("poolState") or "unknown"
                _pa["upstream_state"] = _row.get("state") or ""
            elif _pa.get("hist_slot"):
                _pa["status"] = "历史 key · 槽位 %s" % _pa["hist_slot"]
                _pa["status_key"] = "historic"
                _pa["upstream_state"] = ""
            else:
                _pa["status"] = "外网/未知 key"
                _pa["status_key"] = "unknown"
                _pa["upstream_state"] = ""
        # 账号数以控制面 pool-config.json 槽位为准（account_rows 已在上面设过）；
        # 仅当控制面读不到时，退回 pool_store 快照计数。
        if not out.get("accounts"):
            try:
                import sys as _sys
                if "/opt/litellm" not in _sys.path:
                    _sys.path.insert(0, "/opt/litellm")
                import pool_store as _ps
                snap = _ps.snapshot()
                _n = snap.get("accounts")
                _n = len(_n) if isinstance(_n, (list, tuple)) else int(_n or 0)
                if _n:
                    out["accounts"] = _n
            except Exception:
                pass
        if not out.get("accounts"):
            out["accounts"] = len([a for a in au if not a.get("pending")])
    except Exception:
        pass
    try:
        out["routing"] = _relabel_routing(routing_summary(),
                                          _acct_label_map(out.get("account_usage") or []))
    except Exception as e:  # P3 面板任何异常 → 未就绪，绝不影响旧面板
        out["routing"] = {"ready": False, "error": str(e)[:200], "panels": {}, "alerts": []}
    return out


# ================================================================ P3 观测面（FINAL §6.1-P3）
# 数据源：routing-share.json（share-engine 落盘）/ shadow-decisions.jsonl（sidecar 影子决策）
#         / pool-requests.jsonl（pool_logger 请求级日志，bind-mount）/ usage 快照 + share-engine 日志。
# 原则：任何数据读不到 → 对应面板 ready=False 渲染「数据未就绪」，绝不报错、绝不影响旧面板。
SHARE_JSON = "/opt/litellm/routing-share.json"
SHADOW_JSONL = "/opt/litellm/shadow-decisions.jsonl"
POOL_JSONL = "/opt/litellm/pool-requests.jsonl"
USAGE_SNAP = "/opt/litellm/usage-snapshot.json"
ENGINE_LOG = "/opt/litellm/share-engine.log"
ALERT_STATE = "/opt/pool-dash/.alert-state.json"
PRICE_IN, PRICE_OUT, PRICE_CACHED = 0.14, 0.28, 0.0028   # $/M tok（同 share-engine 计价校准，非份额数值）
P3_WINDOW_S = 3600.0        # P2 实际份额滚动窗 1h
P3_BASELINE_S = 86400.0     # P5/P6 warm 命中基线窗 24h
P3_G1_MARGIN_S = 6 * 3600   # §2.3 G1：T_exhaust < T_reset − 6h
P3_IDLE_MARGIN_S = 24 * 3600
P1_MAX_HORIZON_S = 365 * 86400   # P1：外推超过 1 年视为「无有效样本」
P3_DEV_PP = 10.0            # A3：份额偏差 10pp×2
P3_WARM_DROP_PP = 0.05      # A6：warm 命中 < 基线 − 5pp
P3_COLD_BUDGET_PCT = 80.0   # A5：冷预算消耗 ≥80%
P3_SHARE_MAX_AGE_S = 1800   # A4：快照 staleness >30min


def _p3_read_json(path):
    try:
        with open(path) as f:
            return json.load(f), None
    except Exception as e:
        return None, str(e)[:80]


def _p3_tail_text(path, n=400):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 200_000))
            return f.read().decode("utf-8", "ignore").splitlines()[-n:]
    except Exception:
        return []


def _p3_tail_jsonl(path, n=500):
    rows = []
    for line in _p3_tail_text(path, n):
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows[-n:]


def _p3_slot_map():
    """OCG_KEY_n ↔ keyRef 尾 24 字符映射（只比对尾缀，绝不打印密钥）。"""
    m = {}
    try:
        for line in open("/opt/litellm/.env"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if re.match(r"^OCG_KEY_\d+$", k) and v:
                m[v[-24:]] = k
    except Exception:
        pass
    return m


def _p3_rec_cost(r):
    """pool-requests 行 → 实测 $（pt/cached/ct × 计价）。"""
    pt = int(r.get("pt") or 0)
    ct = int(r.get("ct") or 0)
    c = int(r.get("cached") or 0)
    return (max(pt - c, 0) * PRICE_IN + c * PRICE_CACHED + ct * PRICE_OUT) / 1e6


def _p3_iso_ts(s):
    try:
        t = str(s or "").strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        from datetime import datetime
        dt = datetime.fromisoformat(t)
        return dt.timestamp()
    except Exception:
        return None


def count_usage_failures(lines, now, window_s=600):
    """share-engine 日志行 → window_s 内 usage 失败计数（A4 判定表达式前半）。"""
    n = 0
    for ln in lines:
        ts = _p3_iso_ts((ln.split(" ") or [""])[0])
        if ts is None or now - ts > window_s:
            continue
        if any(c in ln for c in ("usage_fetch_failed", "usage_unavailable",
                                 "usage_stale_snapshot", "FATAL")):
            n += 1
    return n


def gate_change(gated_now, prev_gates):
    """A7 判定：gated 集合相对上次快照的 剔除（新增）/恢复（减少）。"""
    now_s, prev_s = set(gated_now or []), set(prev_gates or [])
    return sorted(now_s - prev_s), sorted(prev_s - now_s)


def build_routing(share, shadow_rows, pool_rows, usage, engine_lines, slot_map, now,
                  prev_gates=None):
    """纯函数：快照 → 6 面板 + 8 告警（可单测；任何字段缺失 → 对应面板 ready=False）。"""
    share = share if isinstance(share, dict) else None
    shadow_rows = shadow_rows or []
    pool_rows = pool_rows or []
    params = (share or {}).get("params") or {}
    accounts = (share or {}).get("accounts") or []
    slot_map = slot_map or {}

    # ---- 共用：每账号近 24h 实测烧量 $/天（pool-requests 实测 $）
    burn, burn_n, win_cost = {}, {}, {}
    cut = now - P3_BASELINE_S
    for r in pool_rows:
        ts = int(r.get("ts") or 0) / 1000.0
        if ts < cut:
            continue
        slot = slot_map.get(str(r.get("keyRef") or "")[-24:], "unmapped")
        win_cost[slot] = win_cost.get(slot, 0.0) + _p3_rec_cost(r)
        burn_n[slot] = burn_n.get(slot, 0) + 1
    for k, v in win_cost.items():
        burn[k] = v * 86400.0 / P3_BASELINE_S

    # ---- P1 烧量倒计时（T_exhaust vs T_reset 三态：danger / idle / ok）
    p1 = []
    for a in accounts:
        kr = a.get("key_ref")
        try:
            pace = float(a.get("pace_usd_day") or 0.0)
            days = float(a.get("days") or 0.0)
            rem_pct = float(a.get("rem_pct") or 0.0)
        except Exception:
            pace = days = rem_pct = 0.0
        rem_usd = max(0.0, pace * days)          # monthly 口径剩余 $（=pace×days，ε 已扣）
        t_reset = days * 86400.0
        b = burn.get(kr, 0.0)
        bn = burn_n.get(kr, 0)
        has_burn = bn > 0 and b > 0.0
        t_ex = (rem_usd / (b / 86400.0)) if has_burn else None
        if t_ex is not None and t_ex > P1_MAX_HORIZON_S:
            # 近 24h 只有极小样本 → 外推会出「4372460 天」这种无意义值，按无样本处理
            t_ex = None
        if t_ex is None:
            state = "no-sample"
        elif t_ex < t_reset - P3_G1_MARGIN_S:
            state = "danger"
        elif t_ex > t_reset + P3_IDLE_MARGIN_S and rem_pct > 5:
            state = "idle"
        else:
            state = "ok"
        p1.append({"key_ref": kr, "rem_pct": rem_pct, "days": days,
                   "t_reset_s": round(t_reset),
                   "t_exhaust_s": (round(t_ex) if t_ex is not None else None),
                   "burn_usd_day": round(b, 4), "burn_samples": bn,
                   "has_burn": has_burn, "pace_usd_day": pace, "state": state})

    # ---- P2 目标 vs 实际份额（实际 = 近 1h tt 占比；上一窗 = 前 1h，供 10pp×2 判定）
    def _share_window(lo, hi):
        tot, s = {}, 0.0
        for r in pool_rows:
            ts = int(r.get("ts") or 0) / 1000.0
            if ts < lo or ts >= hi:
                continue
            slot = slot_map.get(str(r.get("keyRef") or "")[-24:], "unmapped")
            tt = int(r.get("tt") or 0)
            tot[slot] = tot.get(slot, 0) + tt
            s += tt
        return ({k: v / s for k, v in tot.items()} if s > 0 else {}), s
    cur, cur_s = _share_window(now - P3_WINDOW_S, now)
    prev, prev_s = _share_window(now - 2 * P3_WINDOW_S, now - P3_WINDOW_S)
    p2acc = []
    for a in accounts:
        kr = a.get("key_ref")
        tgt = float(a.get("share") or 0.0)
        act, prv = cur.get(kr, 0.0), prev.get(kr, 0.0)
        p2acc.append({"key_ref": kr, "target": round(tgt, 4), "actual": round(act, 4),
                      "dev_pp": round((act - tgt) * 100.0, 2),
                      "dev_prev_pp": round((prv - tgt) * 100.0, 2)})
    p2 = {"window_s": P3_WINDOW_S, "accounts": p2acc, "has_actual": bool(cur_s),
          "has_prev": bool(prev_s), "ready": share is not None}

    # ---- P3 影子命中率（key_want == key_actual 占比）
    dec = [r for r in shadow_rows if r.get("mode") in ("shadow", "canary", "full")]
    comp = [r for r in dec if r.get("key_want")
            and r.get("key_actual") not in (None, "", "unobserved", "unmapped")]
    matched = sum(1 for r in comp if r.get("key_want") == r.get("key_actual"))
    p3 = {"records": len(dec), "comparable": len(comp), "matched": matched,
          "hit_rate": (round(matched / len(comp), 4) if comp else None),
          "ready": len(dec) > 0}

    # ---- P4 分档分布（shadow-decisions 的 class）
    dist = {}
    for r in shadow_rows:
        k = str(r.get("class") or "?")
        dist[k] = dist.get(k, 0) + 1
    p4 = {"dist": dist, "ready": bool(shadow_rows)}

    # ---- P5 迁移与缓存（G5 预算消耗率 + warm 命中）
    # R1 整改4（T6 §6-①/整改4）：冷预算按 (sess,epoch) 去重——shadow 期同一会话的
    # 逐请求重复建议只计 1 次（原按行计数：31 行 = 2 会话 × 15/16 次 → 310% 观察口径错误）；
    # canary/full 下每个执行重钉 (sess,epoch+1) 恰计 1 次 = 真实冷请求事件。
    dmode = str((share or {}).get("mode") or "")

    def _dedup_n(win_s):
        seen, n = set(), 0
        for r in shadow_rows:
            if not r.get("repin"):
                continue
            if now - float(r.get("ts") or 0) >= win_s:
                continue
            k = (str(r.get("sess") or "?"), int(r.get("epoch") or 0))
            if k in seen:
                continue
            seen.add(k)
            n += 1
        return n

    n_min = _dedup_n(60)
    n_5h = _dedup_n(5 * 3600)
    suggest_5h = sum(1 for r in shadow_rows if r.get("repin")
                     and now - float(r.get("ts") or 0) < 5 * 3600)
    bmin = params.get("repin_budget_per_min")
    b5h = params.get("cold_budget_per_5h")
    cold_pct = (round(100.0 * n_5h / b5h, 1) if b5h else None)
    p5_note = ("shadow 观察口径：冷预算按 (sess,epoch) 去重，决策未执行不消耗真实预算"
               if dmode == "shadow" else "冷预算按 (sess,epoch) 去重")

    def _cache_rate(lo, hi):
        pt = ca = 0
        for r in pool_rows:
            ts = int(r.get("ts") or 0) / 1000.0
            if ts < lo or ts >= hi:
                continue
            pt += int(r.get("pt") or 0)
            ca += int(r.get("cached") or 0)
        return (ca / pt) if pt > 0 else None
    r1 = _cache_rate(now - P3_WINDOW_S, now)
    rb = _cache_rate(now - P3_BASELINE_S, now - P3_WINDOW_S)
    p5 = {"repin_last_min": n_min, "budget_per_min": bmin,
          "cold_last_5h": n_5h, "cold_budget_per_5h": b5h, "cold_budget_pct": cold_pct,
          "cold_dedup": "(sess,epoch)", "suggest_rows_5h": suggest_5h,
          "note": p5_note,
          "cache_rate_1h": (round(r1, 4) if r1 is not None else None),
          "cache_baseline_24h": (round(rb, 4) if rb is not None else None),
          "ready": share is not None}

    # ---- P6 门禁与健康
    gen_ts = _p3_iso_ts((share or {}).get("generated_at")) or _p3_iso_ts((share or {}).get("as_of"))
    share_age = (now - gen_ts) if gen_ts else None
    usage_ts = _p3_iso_ts((usage or {}).get("ts"))
    usage_age = (now - usage_ts) if usage_ts else None
    usage_fail_accts = [a.get("slot") for a in ((usage or {}).get("accounts") or [])
                        if not a.get("ok")]
    fail10 = count_usage_failures(engine_lines or [], now)
    gated_now = [a.get("key_ref") for a in accounts if a.get("gated")]
    # A7 需要跨快照基线；从未见过基线（prev_gates=None）→ 不告警（避免首跑误报）
    added, removed = gate_change(gated_now, prev_gates) if prev_gates is not None else ([], [])
    err_codes = [e.get("code") for e in ((share or {}).get("errors") or []) if isinstance(e, dict)]
    d7 = ((share or {}).get("overflow") or {}).get("alert") == "pool_quota_insufficient" \
        or "pool_quota_insufficient" in err_codes
    p6 = {"status": (share or {}).get("status"), "mode": (share or {}).get("mode"),
          "share_age_s": (round(share_age) if share_age is not None else None),
          "gated": gated_now, "gate_added": added, "gate_removed": removed,
          "errors": err_codes, "d7": bool(d7), "distress": (share or {}).get("distress"),
          "expiring_set": (share or {}).get("expiring_set"),
          "usage_failures_10min": fail10,
          "usage_age_s": (round(usage_age) if usage_age is not None else None),
          "usage_fail_accounts": usage_fail_accts,
          "ready": share is not None}

    panels = {"p1_burn": {"rows": p1, "ready": share is not None and bool(p1)},
              "p2_share": p2, "p3_shadow": p3, "p4_class": p4,
              "p5_repin": p5, "p6_health": p6}

    # ---- 8 条告警（判定表达式随行，便于审计/自测）
    alerts = []

    def _add(aid, name, level, firing, expr, detail):
        alerts.append({"id": aid, "name": name, "level": level,
                       "firing": bool(firing), "expr": expr, "detail": detail})
    danger = [x["key_ref"] for x in p1 if x["state"] == "danger"]
    _add("A1", "打穿预警 T_exhaust<T_reset−6h", "crit", danger,
         "any acct: t_exhaust < t_reset - 6h",
         ("命中: " + ",".join(danger)) if danger else "无账号打穿前打满")
    idle = [x["key_ref"] for x in p1 if x["state"] == "idle"]
    _add("A2", "闲置预警 T_exhaust>T_reset+24h 且剩>5%", "warn", idle,
         "any acct: t_exhaust > t_reset + 24h && rem_pct > 5",
         ("闲置: " + ",".join(idle)) if idle else "无账号过度闲置")
    two_windows = bool(cur_s) and bool(prev_s)   # 无样本窗 → 不得误报份额偏差
    dev2 = [x["key_ref"] for x in p2acc
            if two_windows and abs(x["dev_pp"]) > P3_DEV_PP and abs(x["dev_prev_pp"]) > P3_DEV_PP]
    _add("A3", "份额偏差 10pp×2", "warn", dev2,
         "any acct: |actual-target| > 10pp 连续 2 个 1h 窗",
         ("偏差: " + ",".join(dev2)) if dev2 else "份额偏差均 ≤10pp 或未连续 2 窗")
    _add("A4", "usage 失败≥3/10min 或快照 age>30min", "crit",
         fail10 >= 3 or (share_age is not None and share_age > P3_SHARE_MAX_AGE_S),
         "usage_fail_10min >= 3 || share_age > 1800s",
         "10min 失败 %d 次；routing-share age=%s s" %
         (fail10, round(share_age) if share_age is not None else "?"))
    _add("A5", "冷预算消耗 ≥80%", "warn",
         cold_pct is not None and cold_pct >= P3_COLD_BUDGET_PCT,
         "cold(5h, dedup (sess,epoch)) / cold_budget_per_5h >= 80%",
         "冷请求 %s/5h = %s%%（%s）" % (n_5h, cold_pct, p5_note))
    warm_drop = (r1 is not None and rb is not None and r1 < rb - P3_WARM_DROP_PP)

    def _pct(v):
        # 2026-10-19 dashfix：旧文案直接把原始浮点 % 进字符串（0.8924726470059717），
        # 与同一行里的 0.0% 单位不一致、也难读；统一成百分数两位小数。
        return "—" if v is None else "%.2f%%" % (v * 100.0)

    _add("A6", "warm 命中 1h < 基线−5pp", "warn", warm_drop,
         "cache_rate_1h < cache_baseline_24h - 5pp",
         "1h=%s vs 基线=%s（阈值 −%.0fpp）" % (_pct(r1), _pct(rb), P3_WARM_DROP_PP * 100))
    _add("A7", "门禁剔除/恢复", "warn", bool(added or removed),
         "gated 集合 ≠ 上次快照（新增=剔除 / 减少=恢复）",
         ("剔除 %s；恢复 %s" % (added, removed)) if (added or removed) else "门禁集合无变化")
    _add("A8", "池额度不足（D7）", "warn", d7,
         "overflow.alert == pool_quota_insufficient || errors 含该 code",
         "Σpace < 需求 → 溢出派给先重置号" if d7 else "额度充足")
    return {"panels": panels, "alerts": alerts, "gates_now": gated_now}


def routing_summary(now=None):
    now = time.time() if now is None else now
    share, share_err = _p3_read_json(SHARE_JSON)
    shadow_rows = _p3_tail_jsonl(SHADOW_JSONL, 500)
    pool_rows = _p3_tail_jsonl(POOL_JSONL, 2000)
    usage, _ = _p3_read_json(USAGE_SNAP)
    engine_lines = _p3_tail_text(ENGINE_LOG, 400)
    slot_map = _p3_slot_map()
    prev_gates = None
    try:
        st, _ = _p3_read_json(ALERT_STATE)
        prev_gates = (st or {}).get("gates")
    except Exception:
        pass
    built = build_routing(share, shadow_rows, pool_rows, usage, engine_lines, slot_map,
                          now, prev_gates=prev_gates)
    try:  # A7 需跨快照比较 → 保存本次门禁集合（失败不影响面板）
        with open(ALERT_STATE, "w") as f:
            json.dump({"gates": built.get("gates_now"), "ts": now}, f)
    except Exception:
        pass
    out = {"ready": share is not None, "share_error": share_err,
           "shadow_rows": len(shadow_rows), "generated_ts": round(now, 1)}
    out.update(built)
    return out


def selftest_p3():
    """P3 观测面自测：6 面板 ready 语义（数据未就绪）+ 8 条告警判定表达式（离线合成数据）。"""
    import sys as _sys
    from datetime import datetime, timezone
    n = [0]

    def ok(cond, name, detail=""):
        n[0] += 1
        if not cond:
            print("FAIL", name, detail)
            _sys.exit(1)

    def iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    now = 1790674800.0
    T1, T2, T3 = "A" * 24, "B" * 24, "C" * 24
    slot_map = {T1: "OCG_KEY_1", T2: "OCG_KEY_2", T3: "OCG_KEY_3"}

    def acct(kr, share, pace, days, rem, gated=False):
        return {"key_ref": kr, "share": share, "pace_usd_day": pace, "days": days,
                "rem_pct": rem, "gated": gated}

    def mkshare(accs, generated_at=None, **kw):
        s = {"generated_at": generated_at or iso(now - 60), "mode": "shadow", "status": "ok",
             "params": {"repin_budget_per_min": 2, "cold_budget_per_5h": 10, "l_tilt": 7},
             "accounts": accs, "errors": [], "overflow": {"alert": None}, "expiring_set": []}
        s.update(kw)
        return s

    def prow(tail, tt=100, cached=0, ts=None, cost=None):
        pt = tt if cost is None else int(cost / PRICE_IN * 1e6)
        return {"keyRef": "k" + tail, "ts": int((ts if ts is not None else now - 60) * 1000),
                "tt": tt, "pt": pt, "ct": 0, "cached": cached}

    def A(b):
        return {a["id"]: a for a in b["alerts"]}

    ok(len(build_routing(None, [], [], None, [], {}, now)["alerts"]) == 8, "alerts-count-8")
    ok(all(a.get("expr") for a in build_routing(None, [], [], None, [], {}, now)["alerts"]),
       "alerts-expr-present")

    # A1 打穿：burn 24$/天 vs rem 1$、T_reset 1 天 → T_exhaust 1h < T_reset−6h
    b = build_routing(mkshare([acct("OCG_KEY_1", 0.5, 1.0, 1.0, 100.0)]),
                      [], [prow(T1, tt=10, cost=24.0)], None, [], slot_map, now)
    ok(A(b)["A1"]["firing"] and b["panels"]["p1_burn"]["rows"][0]["state"] == "danger",
       "A1-fires-danger", str(b["panels"]["p1_burn"]))
    b = build_routing(mkshare([acct("OCG_KEY_1", 0.5, 1.0, 1.0, 100.0)]),
                      [], [prow(T1, tt=10, cost=1.0)], None, [], slot_map, now)
    ok(not A(b)["A1"]["firing"], "A1-clear")

    # A2 闲置：T_exhaust > T_reset+24h 且剩 >5%
    b = build_routing(mkshare([acct("OCG_KEY_1", 0.5, 1.0, 10.0, 90.0)]),
                      [], [prow(T1, tt=10, cost=0.05)], None, [], slot_map, now)
    ok(A(b)["A2"]["firing"] and b["panels"]["p1_burn"]["rows"][0]["state"] == "idle",
       "A2-fires-idle", str(b["panels"]["p1_burn"]))
    b = build_routing(mkshare([acct("OCG_KEY_1", 0.5, 1.0, 10.0, 3.0)]),
                      [], [prow(T1, tt=10, cost=0.05)], None, [], slot_map, now)
    ok(not A(b)["A2"]["firing"], "A2-clear-rem-le-5")

    # A3 份额偏差 10pp×2：连续两个 1h 窗都 >10pp
    rows2 = [prow(T1, tt=800, ts=now - 60), prow(T2, tt=200, ts=now - 60),
             prow(T1, tt=800, ts=now - 4000), prow(T2, tt=200, ts=now - 4000)]
    b = build_routing(mkshare([acct("OCG_KEY_1", 0.5, 1.0, 5.0, 50.0),
                               acct("OCG_KEY_2", 0.5, 1.0, 5.0, 50.0)]),
                      [], rows2, None, [], slot_map, now)
    ok(A(b)["A3"]["firing"], "A3-fires-10pp-x2", str(b["panels"]["p2_share"]))
    rows2[2], rows2[3] = prow(T1, tt=520, ts=now - 4000), prow(T2, tt=480, ts=now - 4000)
    b = build_routing(mkshare([acct("OCG_KEY_1", 0.5, 1.0, 5.0, 50.0),
                               acct("OCG_KEY_2", 0.5, 1.0, 5.0, 50.0)]),
                      [], rows2, None, [], slot_map, now)
    ok(not A(b)["A3"]["firing"], "A3-clear-not-consecutive")
    b = build_routing(mkshare([acct("OCG_KEY_1", 0.5, 1.0, 5.0, 50.0),
                               acct("OCG_KEY_2", 0.5, 1.0, 5.0, 50.0)]),
                      [], [], None, [], slot_map, now)
    ok(not A(b)["A3"]["firing"], "A3-clear-no-samples")

    # A4 usage 失败 ≥3/10min 或快照 age>30min
    fail = "%s alert level=warn code=usage_fetch_failed x"
    b = build_routing(mkshare([acct("OCG_KEY_1", 1.0, 1.0, 5.0, 50.0)]),
                      [], [], None, [fail % iso(now - 60)] * 3, slot_map, now)
    ok(A(b)["A4"]["firing"], "A4-fires-3fails")
    b = build_routing(mkshare([acct("OCG_KEY_1", 1.0, 1.0, 5.0, 50.0)]),
                      [], [], None, [fail % iso(now - 60)] * 2, slot_map, now)
    ok(not A(b)["A4"]["firing"], "A4-clear-2fails")
    b = build_routing(mkshare([acct("OCG_KEY_1", 1.0, 1.0, 5.0, 50.0)],
                              generated_at=iso(now - 2000)),
                      [], [], None, [], slot_map, now)
    ok(A(b)["A4"]["firing"], "A4-fires-stale")

    # A5 冷预算消耗 ≥80%（R1 整改4：冷预算按 (sess,epoch) 去重）
    sh9 = [{"repin": True, "ts": now - 100, "mode": "shadow",
            "sess": "a5s%d" % i, "epoch": 0} for i in range(9)]
    b = build_routing(mkshare([acct("OCG_KEY_1", 1.0, 1.0, 5.0, 50.0)]),
                      sh9, [], None, [], slot_map, now)
    ok(A(b)["A5"]["firing"] and b["panels"]["p5_repin"]["cold_budget_pct"] == 90.0,
       "A5-fires-90pct", str(b["panels"]["p5_repin"]))
    b = build_routing(mkshare([acct("OCG_KEY_1", 1.0, 1.0, 5.0, 50.0)]),
                      [{"repin": True, "ts": now - 100, "mode": "shadow",
                        "sess": "a5t%d" % i, "epoch": 0} for i in range(7)],
                      [], None, [], slot_map, now)
    ok(not A(b)["A5"]["firing"], "A5-clear-70pct")
    # R1 整改4 去重断言：同一会话 N 行只计 1（原按行计数会得 90% 误报）
    dup9 = [{"repin": True, "ts": now - 100, "mode": "shadow",
             "sess": "dupsess", "epoch": 0}] * 9
    b = build_routing(mkshare([acct("OCG_KEY_1", 1.0, 1.0, 5.0, 50.0)]),
                      dup9, [], None, [], slot_map, now)
    p5d = b["panels"]["p5_repin"]
    ok(p5d["cold_last_5h"] == 1 and p5d["cold_budget_pct"] == 10.0
       and not A(b)["A5"]["firing"],
       "A5-dedup-same-sess-9rows-count-1", str(p5d))
    ok(p5d.get("suggest_rows_5h") == 9 and p5d.get("cold_dedup") == "(sess,epoch)",
       "A5-raw-suggest-rows-kept-diagnostic", str(p5d))
    ok("观察口径" in str(p5d.get("note")),
       "A5-shadow-obs-note", str(p5d.get("note")))

    # A6 warm 命中 1h < 基线（前 24h）−5pp
    rows6 = [prow(T1, tt=100, cached=50, ts=now - 60),          # 1h: 50%
             prow(T1, tt=100, cached=60, ts=now - 3 * 3600)]    # 基线: 60%
    b = build_routing(mkshare([acct("OCG_KEY_1", 1.0, 1.0, 5.0, 50.0)]),
                      [], rows6, None, [], slot_map, now)
    ok(A(b)["A6"]["firing"], "A6-fires-drop", str(b["panels"]["p5_repin"]))
    rows6[1] = prow(T1, tt=100, cached=52, ts=now - 3 * 3600)   # 基线 52% → 落差 2pp
    b = build_routing(mkshare([acct("OCG_KEY_1", 1.0, 1.0, 5.0, 50.0)]),
                      [], rows6, None, [], slot_map, now)
    ok(not A(b)["A6"]["firing"], "A6-clear")

    # A7 门禁剔除/恢复（跨快照比较）
    b = build_routing(mkshare([acct("OCG_KEY_3", 1.0, 1.0, 5.0, 50.0, gated=True)]),
                      [], [], None, [], slot_map, now, prev_gates=["OCG_KEY_2"])
    ok(A(b)["A7"]["firing"] and b["panels"]["p6_health"]["gate_added"] == ["OCG_KEY_3"]
       and b["panels"]["p6_health"]["gate_removed"] == ["OCG_KEY_2"], "A7-fires-gate-change",
       str(b["panels"]["p6_health"]))
    b = build_routing(mkshare([acct("OCG_KEY_3", 1.0, 1.0, 5.0, 50.0, gated=True)]),
                      [], [], None, [], slot_map, now, prev_gates=["OCG_KEY_3"])
    ok(not A(b)["A7"]["firing"], "A7-clear")
    b = build_routing(mkshare([acct("OCG_KEY_3", 1.0, 1.0, 5.0, 50.0, gated=True)]),
                      [], [], None, [], slot_map, now, prev_gates=None)
    ok(not A(b)["A7"]["firing"], "A7-clear-no-baseline")

    # A8 池额度不足（D7）
    b = build_routing(mkshare([acct("OCG_KEY_1", 1.0, 1.0, 5.0, 50.0)],
                              overflow={"alert": "pool_quota_insufficient"}),
                      [], [], None, [], slot_map, now)
    ok(A(b)["A8"]["firing"], "A8-fires-d7")
    b = build_routing(mkshare([acct("OCG_KEY_1", 1.0, 1.0, 5.0, 50.0)]),
                      [], [], None, [], slot_map, now)
    ok(not A(b)["A8"]["firing"], "A8-clear")

    # 面板 ready 语义：读不到数据 → False（前端渲染「数据未就绪」），绝不报错
    b = build_routing(None, [], [], None, [], {}, now)
    for pid in ("p1_burn", "p2_share", "p3_shadow", "p4_class", "p5_repin", "p6_health"):
        ok(b["panels"][pid]["ready"] is False, "not-ready-" + pid)
    ok("数据未就绪" in HTML, "html-not-ready-marker")
    b = build_routing(None, [{"mode": "shadow", "class": "L", "ts": now,
                              "key_want": "OCG_KEY_1", "key_actual": "OCG_KEY_1"}],
                      [], None, [], slot_map, now)
    ok(b["panels"]["p3_shadow"]["ready"] and b["panels"]["p4_class"]["ready"]
       and b["panels"]["p3_shadow"]["hit_rate"] == 1.0, "shadow-panels-ready")
    ok(count_usage_failures(["%s code=usage_unavailable x" % iso(now - 60)] * 3, now) == 3
       and count_usage_failures(["%s code=usage_unavailable x" % iso(now - 2000)] * 3, now) == 0,
       "usage-fail-window")
    print("selftest-p3-ok: %d asserts" % n[0])
    return 0



HTML = """<!DOCTYPE html><html lang="zh-CN" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpencodeLitellmPool · 号池驾驶舱</title>
<link rel="icon" href="data:,"><!-- 抑制 /favicon.ico 404 -->
<script>/* 主题在首帧前落定，避免闪白/闪黑 */(function(){var t=null;try{t=localStorage.getItem("pool-theme-v1")}catch(e){}document.documentElement.setAttribute("data-theme",t==="light"?"light":"dark");})();</script>
<style>
:root{--bg:#0e1014;--card:#161a21;--card2:#1b1f29;--line:#232833;--line2:#2c3342;--txt:#e6e9ef;--dim:#8b93a3;--ok:#4ade80;--warn:#fbbf24;--err:#f87171;--acc:#60a5fa;--vio:#a78bfa;--cy:#22d3ee;--pk:#f472b6;--orange:#fb923c}
*{box-sizing:border-box;margin:0;padding:0}
html{scrollbar-gutter:stable}
body{background:radial-gradient(1400px 620px at 12% -12%,#18202f 0%,var(--bg) 62%) no-repeat,var(--bg);
 color:var(--txt);font:13px/1.5 -apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
 padding:12px 16px 44px;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#39415a;border-radius:6px;border:2px solid transparent;background-clip:content-box}
::-webkit-scrollbar-thumb:hover{background:#4d5880;background-clip:content-box}
a{color:var(--acc);text-decoration:none}
a:hover{text-decoration:underline}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Noto Sans CJK SC",monospace;letter-spacing:.02em}
/* ---------- 顶栏 ---------- */
.top{position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
 background:rgba(14,16,20,.88);backdrop-filter:blur(10px);border:1px solid var(--line);border-radius:12px;
 padding:9px 12px;margin-bottom:12px}
.brand{display:flex;align-items:center;gap:9px;min-width:0}
.logo{width:26px;height:26px;border-radius:8px;flex:none;background:linear-gradient(135deg,#2563eb,#22d3ee 60%,#4ade80);
 box-shadow:0 0 0 1px #2b3446 inset;display:grid;place-items:center;font-size:13px;font-weight:800;color:#08111f}
.brand b{font-size:14px;letter-spacing:.02em;white-space:nowrap}
.brand .vd{font-size:11px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.top .sp{flex:1 1 auto;min-width:4px}
.ctl{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--dim);white-space:nowrap}
.ctl select,.ctl input[type=number],.ctl input[type=text],.ctl input[type=search]{background:#1d2230;color:var(--txt);
 border:1px solid var(--line2);border-radius:7px;padding:4px 7px;font-size:12px;font-family:inherit}
.ctl select:focus,.ctl input:focus{outline:none;border-color:var(--acc)}
button{background:#1d2230;color:var(--txt);border:1px solid var(--line2);border-radius:7px;padding:4px 10px;
 font-size:12px;font-family:inherit;cursor:pointer;transition:border-color .15s,background .15s}
button:hover{border-color:var(--acc);background:#212838}
button.primary{background:#1d4ed8;border-color:#2f61e8;color:#fff;font-weight:600}
button.primary:hover{background:#2559ea}
button:disabled{opacity:.5;cursor:not-allowed}
.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line2);border-radius:999px;
 padding:2px 9px;font-size:11.5px;color:var(--dim);background:#1a1f2b}
.dot{width:8px;height:8px;border-radius:50%;flex:none}
.switch{position:relative;display:inline-flex;align-items:center;gap:7px;cursor:pointer;user-select:none}
.switch input{position:absolute;opacity:0;width:0;height:0}
.track{width:34px;height:18px;border-radius:999px;background:#2a3142;border:1px solid var(--line2);transition:background .18s}
.track i{display:block;width:12px;height:12px;border-radius:50%;background:#8b93a3;margin:2px;transition:transform .18s,background .18s}
.switch input:checked + .track{background:#1d4ed8;border-color:#2f61e8}
.switch input:checked + .track i{transform:translateX(16px);background:#fff}
/* ---------- KPI ---------- */
.kpis{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:var(--gap);margin-bottom:8px}
.kpi{position:relative;background:linear-gradient(180deg,#181d26,#141820);border:1px solid var(--line);border-radius:12px;
 padding:9px 12px;overflow:hidden;cursor:default;transition:border-color .15s,transform .15s}
.kpi:hover{border-color:#38445c;transform:translateY(-1px)}
.kpi::after{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--k,var(--acc));opacity:.75}
.kpi .k{font-size:11.5px;color:var(--dim);display:flex;align-items:center;gap:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi .v{font-size:21px;font-weight:700;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi .s{font-size:11px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ok{color:var(--ok)}.warn{color:var(--warn)}.err{color:var(--err)}.dimv{color:var(--dim)}
#ktip{position:fixed;z-index:60;display:none;pointer-events:none;max-width:340px;background:rgba(13,15,20,.97);
 border:1px solid var(--line2);border-radius:9px;padding:8px 10px;font-size:12px;line-height:1.65;color:var(--txt);
 box-shadow:0 12px 30px rgba(0,0,0,.5)}
#ktip b{color:#fff}
#ktip .r{display:flex;justify-content:space-between;gap:14px}
#ktip .r i{color:var(--dim);font-style:normal}
/* ---------- 布局 ---------- */
h2.sec{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;font-size:13px;color:var(--txt);
 margin:16px 2px 9px;font-weight:600}
h2.sec span{font-size:11.5px;color:var(--dim);font-weight:400}
.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:11px;align-items:start}
.panel{grid-column:span 12;min-width:0;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;
 transition:border-color .15s}
.panel:hover{border-color:#2b3446}
.c3{grid-column:span 3}.c4{grid-column:span 4}.c5{grid-column:span 5}.c6{grid-column:span 6}.c7{grid-column:span 7}.c8{grid-column:span 8}
.ph{display:flex;align-items:center;gap:8px;padding:8px 11px;border-bottom:1px solid var(--line);
 background:linear-gradient(180deg,#1a1f29,#161a21)}
.ph h3{font-size:12.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ph .tag{font-size:10.5px;color:var(--dim);border:1px solid var(--line);border-radius:999px;padding:0 7px;white-space:nowrap;flex:none}
.ph .grow{flex:1 1 auto;min-width:0}
.pbody{position:relative;padding:8px 10px 10px}
.chart{width:100%;height:248px}
.h180{height:180px}.h210{height:210px}.h280{height:280px}.h300{height:300px}.h150{height:150px}
.pbody.is-empty .chart{visibility:hidden}
.empty{display:none;position:absolute;left:0;right:0;top:0;bottom:0;align-items:center;justify-content:center;
 color:var(--dim);font-size:12px;text-align:center;padding:14px}
.pbody.is-empty .empty{display:flex}
.note{font-size:11px;color:var(--dim);margin-top:6px;line-height:1.55}
.meter{height:5px;border-radius:3px;background:#232a38;overflow:hidden;margin-bottom:3px}
.meter>i{display:block;height:100%;border-radius:3px}
.rt-alert{border:1px solid;border-radius:8px;padding:6px 9px;margin:5px 0;font-size:12px;line-height:1.55;word-break:break-word;overflow-wrap:anywhere}
.rt-alert-d{margin-left:8px;color:var(--txt);word-break:break-word;overflow-wrap:anywhere}
@media (max-width:560px){.rt-alert-d{display:block;margin-left:0;margin-top:2px}}
.rt-alert-e{display:block;color:var(--dim);font-size:10.5px;margin-top:3px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.rt-ok{color:var(--ok);font-size:12px}
/* ---- 烧量路由 v14（2026-10-27）：状态卡 / 分块 / 打穿行 / 微型条 ---- */
.rt-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:9px}
.rt-stat{background:#1a1f29;border:1px solid var(--line);border-radius:9px;padding:7px 9px;min-width:0}
.rt-stat .sv{font-size:15px;font-weight:700;line-height:1.15;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rt-stat .sk{font-size:10.5px;color:var(--dim);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rt-blocks{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.rt-block{border:1px solid var(--line);border-radius:9px;padding:7px 9px;background:#1a1f29;min-width:0}
.rt-block h4{font-size:10.5px;letter-spacing:.05em;color:var(--dim);font-weight:600;margin-bottom:5px}
.rt-block .chips{gap:4px}
.rt-block .chip{font-size:11px;padding:0 6px}
tr.rt-danger td{background:rgba(248,113,113,.07)}
tr.rt-danger:hover td{background:rgba(248,113,113,.12)}
.rt-bar{height:5px;border-radius:3px;background:#232a38;overflow:hidden;margin:2px 0 3px}
.rt-bar>i{display:block;height:100%;border-radius:3px}
.rt-mini{font-size:10.5px;color:var(--dim)}
@media (max-width:1100px){.rt-blocks{grid-template-columns:1fr}}

[data-key="p2"]{display:flex;flex-direction:column}
#panelP2{display:flex;flex-direction:column;gap:6px;flex:1 1 auto}
#panelP2>#chart-p2{flex:1 1 auto;height:auto !important;min-height:280px}
#panelP2>.chips{margin-top:0}
/* ---------- 表格 ---------- */
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{padding:5px 8px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--dim);font-weight:600;background:#1a1f29;position:sticky;top:0;z-index:2}
table.sortable th{cursor:pointer;user-select:none}
table.sortable th:hover{color:var(--txt)}
table.sortable th::after{content:"";opacity:.5;margin-left:4px;font-size:9px}
table.sortable th.asc::after{content:"▲";opacity:.9}
table.sortable th.desc::after{content:"▼";opacity:.9}
tbody tr:hover{background:#1d2331}
tbody tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right}
.scroll{max-height:min(56vh,520px);overflow:auto;overscroll-behavior:contain;border-radius:8px}
.scroll table{min-width:520px}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{background:#1e2431;border:1px solid var(--line);border-radius:6px;padding:1px 7px;font-size:11.5px;color:var(--dim);white-space:nowrap}
.chip b{color:var(--txt)}
.chip .cdot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:middle}
.badge{font-size:11px;border:1px solid var(--line);border-radius:999px;padding:1px 9px;color:var(--dim);background:#1e2431;white-space:nowrap}
.badge.on{color:var(--ok);border-color:#1d4a33}
.badge.off{color:var(--warn);border-color:#4a3d1d}
.badge.bad{color:var(--err);border-color:#4a2626}
/* ---------- 账号卡 ---------- */
.cards2{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:11px}
.det{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:11px 12px;min-width:0}
/* 2026-10-20（用户要求）：卡头原来挤了「邮箱 + 池位 + 密钥尾号 + 在池徽标 + 状态徽标」，
   窄卡时最后一个徽标会被 flex-wrap 顶到第二行、还被 margin-left:auto 推到最右 → 看着像错位。
   现在：**密钥尾号不再占位**（挪进 title 提示），卡头改单行不换行，邮箱超出时省略号截断。 */
.det-head{display:flex;align-items:center;gap:7px;flex-wrap:nowrap;min-width:0}
.det-name{font-weight:700;font-size:13.5px;flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.det-head>.badge{flex:none}
.det-meta{font-size:11px;color:var(--dim);margin:5px 0 8px;display:flex;gap:12px;flex-wrap:wrap}
/* 2026-10-19（task-9）：`.stat` 一排 6 格在窄卡里会把右边界顶出卡片、数字与标签挤在一起。
   ① 轨道最小宽用 min(100%,88px) 兜底：卡片比 88px 还窄时退化成单列，永远不会横向溢出；
   ② 数值不再用 ellipsis 裁掉（那只是藏起来），改允许换行 —— 只有真挤不下才折行，不遮挡不重叠。 */
.statgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,88px),1fr));gap:7px}
.stat{background:var(--card2);border:1px solid var(--line);border-radius:8px;padding:6px 5px;text-align:center;min-width:0}
.stat .sv{font-size:15px;font-weight:700;line-height:1.25;white-space:normal;overflow-wrap:anywhere}
.stat .sv.sm{font-size:11.5px}
.stat .sk{font-size:10px;color:var(--dim);overflow-wrap:anywhere}
/* 窗口三连：窄卡（<~360px 内容宽）自动折行，避免 84px 环在 85px 的格子里溢出 */
.wins{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,112px),1fr));gap:7px;margin:7px 0}
.win{background:var(--card2);border:1px solid var(--line);border-radius:8px;padding:3px 4px 4px;text-align:center;min-width:0}
.win .wn{font-size:10.5px;line-height:1.25;color:var(--dim)}
.win .st{font-size:10.5px;line-height:1.25;font-weight:600}
.win .rst{font-size:10px;line-height:1.25;color:var(--dim);min-height:13px;word-break:break-word}
/* 2026-10-19（task-9）：恢复「限额环形仪表」—— v8（f75582a）的 84px 内联 SVG 甜甜圈，
   v12 驾驶舱化时被删成纯大数字。零依赖、不挂 ECharts（30 卡 × 3 窗口 = 90 实例太重）。
   轨道色用 --line：深色 #232833、浅色 #dbe3ef，两套主题都能看见；文字用 --txt 自适应。 */
.win .donut{display:block;width:84px;height:84px;margin:2px auto 1px}
.donut .track{fill:none;stroke:var(--line);stroke-width:9}
.donut .val{fill:none;stroke-width:9;stroke-linecap:round;transition:stroke-dasharray .6s ease}
.donut text{fill:var(--txt);font-size:15px;font-weight:700;text-anchor:middle}
.donut-na{width:84px;height:84px;margin:5px auto 3px;display:grid;place-items:center;color:var(--dim);
  border:1px dashed var(--line);border-radius:50%;font-size:15px;font-weight:700}
/* ---------- 表单 / 运维区 ---------- */
.prov{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:9px 11px;margin-bottom:9px}
.prov .pname{font-weight:600;font-size:13px}
.prov .papi{font-size:11px;color:var(--acc);background:#1e2431;border:1px solid var(--line);border-radius:6px;padding:1px 8px}
.prov .ph2{display:flex;align-items:center;gap:9px;margin-bottom:7px;flex-wrap:wrap}
.prov .pact{margin-left:auto;display:flex;gap:6px}
.prov input[type=text],.prov input[type=number],.prov select{background:#1d2230;color:var(--txt);
 border:1px solid var(--line2);border-radius:6px;padding:3px 8px;font-size:12px;font-family:inherit}
.gridbox{display:grid;grid-template-columns:repeat(auto-fill,minmax(186px,1fr));gap:3px 10px}
.gridbox label{display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;padding:1px 3px;border-radius:5px}
.gridbox label:hover{background:#1d2331}
.gridbox input{accent-color:var(--acc)}
.recwrap{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:11px}
@media (max-width:820px){.recwrap{grid-template-columns:1fr}}
.msg{font-size:12px}
.warnlbl{color:var(--err);font-weight:700}
#bootWarn{display:none;margin-bottom:10px;border:1px solid #4a2626;background:#2a1618;color:#fca5a5;
 border-radius:10px;padding:8px 12px;font-size:12px}
@media (max-width:1280px){.kpis{grid-template-columns:repeat(3,minmax(0,1fr))}.c3,.c4,.c5{grid-column:span 6}.c7,.c8{grid-column:span 12}}
@media (max-width:820px){
 .kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
 .grid{grid-template-columns:1fr}
 .c3,.c4,.c5,.c6,.c7,.c8{grid-column:span 1}
 .ph .tag{display:none}
 body{padding:10px 10px 40px}
 .brand .vd{display:none}
 .top{gap:8px}
}
@media (max-width:900px){
  /* 窄屏：长注释/徽标允许折行，避免 301px 的不可断行 span 顶出面板 */
  .note,.hint,.sub,.chip,.k{white-space:normal;overflow-wrap:anywhere}
}
/* 2026-10-20（task-10）：告警条 —— 账号从 3 个启用变 7 个（slot 4-10）后 A2 文本变长，
   整条变成 ~167 字符单行；1024 档 `.chip{white-space:nowrap}` 把它顶出面板，判定表达式
   右溢 30px 且被 .panel{overflow:hidden} 裁掉。只放开 #alertBar 内的 chip 换行，
   其它 .chip 依旧是 nowrap 短标签；判定表达式在窄屏另起一行（更好读）。 */
#alertBar .chip{white-space:normal;overflow-wrap:anywhere;max-width:100%}
#alertBar .axpr{color:var(--dim);margin-left:8px}
@media (max-width:1280px){
  #alertBar .axpr{display:block;margin-left:0;margin-top:2px}
}
@media (max-width:820px){
  /* 窄屏钉死单列：.grid 的 12 列模板在有其他样式表介入时可能盖过上面的媒体查询，
     实测 390/768 下 .panel 被挤成 34/88px 宽的竖条 → 用 id 级 + !important 兜底 */
  #gridMain,#gridRoute{grid-template-columns:1fr !important}
  #gridMain>.panel,#gridRoute>.panel{grid-column:1 / -1 !important}
}

/* ===========================================================================
   驾驶舱层（dashfix 2026-10-19）
   在 v12 单页面板布局之上**叠加**页签 / 双主题 / 1920·2560 自适应。
   下面所有规则都在 v12 原样式之后 —— 同优先级时后者胜，属于叠加而非改写；
   v12 的面板、图表、折叠、排序、表格一律不动。
   =========================================================================== */
:root{
  /* 自适应密度：1920×1080 为基准，2560×1440 等比放大；全部走 clamp()，
     不使用 transform:scale（避免字体发虚）。 */
  --gap:clamp(7px,.45vw,12px);
  --pad:clamp(8px,.52vw,14px);
  --fs:clamp(13px,.74vw,16px);
  --fs-kpi:clamp(19px,1.16vw,30px);
  --rad:clamp(9px,.6vw,14px);
  /* v12 未定义、而管理控制台片段（pool_admin_ui）需要的变量 */
  --hover:#212838; --acc2:#22d3ee; --violet:#a78bfa; --pre:#0c1220;
  --okbg:#0e2a22; --okbd:#1d5a45; --warnbg:#2b2210; --warnbd:#5c4718;
  --errbg:#2b1416; --errbd:#5f2626;
  --btn:var(--line2); --btnbd:var(--line2); --sb:#39415a; --sbh:#4d5880;
  --primary:#1d4ed8; --primarybd:#2f61e8;
  --shadow:0 10px 30px rgba(0,0,0,.38);
}
html[data-theme="dark"]{color-scheme:dark}
html[data-theme="light"]{
  color-scheme:light;
  --bg:#eef2f8; --card:#ffffff; --card2:#f4f7fc; --line:#dbe3ef; --line2:#e7edf7;
  --txt:#15202f; --dim:#5a6a85;
  --ok:#0f9d63; --warn:#b45309; --err:#d93a3a; --acc:#2563eb;
  --hover:#eef4fd; --acc2:#0891b2; --violet:#7c3aed; --pre:#f6f8fc;
  --okbg:#e8f8f1; --okbd:#b3e5d0; --warnbg:#fdf4e0; --warnbd:#f0d7a3;
  --errbg:#fdeaea; --errbd:#f3c0c0;
  --btn:#f3f6fb; --btnbd:#d5deec; --sb:#b9c4d6; --sbh:#93a1b8;
  --primary:#2563eb; --primarybd:#1d4ed8;
  --shadow:0 6px 18px rgba(22,38,70,.10);
}
/* 主题按钮：深色下只留「切到浅色」那一半，浅色下只留「切到深色」——
   纯 CSS 二选一，避免首屏闪出两个标签。 */
html[data-theme="dark"] .t-light{display:none}
html[data-theme="light"] .t-dark{display:none}
body{font-size:var(--fs)}
/* ---- v12 把一部分颜色写死成深色底，浅色主题下逐条覆盖 ---- */
html[data-theme="light"] body{background:radial-gradient(1400px 620px at 12% -12%,#dbe6f7 0%,var(--bg) 62%) no-repeat,var(--bg)}
html[data-theme="light"] .top{background:rgba(255,255,255,.86)}
html[data-theme="light"] .kpi{background:linear-gradient(180deg,#ffffff,#f6f9fe)}
html[data-theme="light"] .kpi:hover{border-color:#b9c8e0}
html[data-theme="light"] .ph{background:linear-gradient(180deg,#f7fafe,#eef3fb)}
html[data-theme="light"] .panel:hover{border-color:#c3d1e6}
html[data-theme="light"] th{background:#f1f5fb}
html[data-theme="light"] tbody tr:hover{background:#f2f6fc}
html[data-theme="light"] .chip,html[data-theme="light"] .badge,html[data-theme="light"] .pill{background:var(--card2)}
html[data-theme="light"] .badge.on{border-color:#9fd9c0}
html[data-theme="light"] .badge.off{border-color:#e8cf95}
html[data-theme="light"] .badge.bad{border-color:#eeb4b4}
html[data-theme="light"] .ctl select,html[data-theme="light"] .ctl input,html[data-theme="light"] button{background:var(--btn)}
html[data-theme="light"] button:hover{background:#e8eefa}
html[data-theme="light"] #ktip{background:rgba(255,255,255,.98);box-shadow:0 12px 30px rgba(22,38,70,.18)}
html[data-theme="light"] #ktip b{color:#0b1420}
html[data-theme="light"] .prov .papi{background:var(--card2)}
html[data-theme="light"] .prov input,html[data-theme="light"] .prov select{background:var(--btn)}
html[data-theme="light"] .gridbox label:hover{background:#f2f6fc}
html[data-theme="light"] #bootWarn{background:#fdf0f0;border-color:#f0c6c6;color:#a32626}
html[data-theme="light"] ::-webkit-scrollbar-thumb{background:#c3cddd;background-clip:content-box}
/* ---- KPI 区：auto-fit 自适应增列（宽屏自动铺满 7 张卡，窄屏回落到 3/2 列） ---- */
.kpis{grid-template-columns:repeat(auto-fit,minmax(clamp(158px,10.6vw,262px),1fr));gap:var(--gap)}
.kpi{padding:calc(var(--pad) * .78) var(--pad)}
.kpi .v{font-size:var(--fs-kpi)}
/* ---- 图表高度 / 面板内边距走 clamp：1080p 与 1440p 共用一套规则等比放大 ---- */
.chart{height:clamp(168px,10.4vw,248px)}
.h150{height:clamp(132px,7.8vw,190px)}
.h180{height:clamp(152px,9.4vw,232px)}
.h210{height:clamp(174px,10.9vw,268px)}
.h280{height:clamp(212px,14.6vw,358px)}
.h300{height:clamp(224px,15.6vw,382px)}
.grid{gap:var(--gap)}
/* 2026-10-19（task-7）：同行面板等高，清掉「矮面板下方留白」——
   routing 的 p3/p4/p5（124/253/147）与 1600 宽下 config 的 prov/teams（1003/499）都因
   全局 .grid 的 align-items:start 而失衡。只在这几个网格上覆盖 stretch，全局规则不动；
   图表不受影响：.chart 是定高元素，canvas 尺寸由 .chart 决定，外壳拉伸不会让 canvas 失配。
   （试过再给 .pbody 加 flex:1 + 垂直居中，视觉更平衡，但会在面板内部造出一段可被测到的空白
   —— 自家 dash_diag 的 blank 会报 p3 bodyH=207/used=128/gap=79 —— 故保持「pbody 高度=内容高度」，
   剩余空间留在卡片下沿内部，任何空白检测器都不会误报。） */
#gridMain,#gridPool,#gridUsage,#gridPerf,#gridRoute,#gridConfig{align-items:stretch}
.pbody{padding:var(--pad) calc(var(--pad) * .9)}
.ph{padding:calc(var(--pad) * .7) var(--pad)}
/* 账号卡：显式列数（6 张池位卡 1920/2560 都是 3+3 或 6 张一行，不再 5+1 落单留白） */
.cards2{grid-template-columns:repeat(auto-fit,minmax(min(100%,clamp(300px,19.4vw,520px)),1fr));gap:var(--gap)}
.cards2 .det{padding:var(--pad) calc(var(--pad) * 1.05)}
/* 账号卡容器（2026-10-20 用户要求）：**固定列数、等宽、不拉伸** —— 取消原来的
   flex-wrap + grow「自动适应」（那会让最后一行的卡被拉成别人的两倍宽）。
   列数按断点固定：>2200 五列（2550 下 10 张正好 5+5）/ >1500 四列（1920 下 4+4+2）/
   >1100 三列 / >700 两列 / 更窄一列（断点用 max-width 闭区间，即恰好 1500/1100/700 落在下一档）。
   末行不满时左边对齐、卡宽与上面完全一致。 */
#acctDetail{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--gap)}
#acctDetail>.det{min-width:0}
#gridPool{margin-top:var(--gap)}
@media (max-width:1500px){#acctDetail{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:1100px){#acctDetail{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:700px){#acctDetail{grid-template-columns:minmax(0,1fr)}}
@media (min-width:2200px){#acctDetail{grid-template-columns:repeat(5,minmax(0,1fr))}}
/* 卡内 6 个小指标：固定 6 列、**永远一行**（原来 4 列会自动折成 4+2，用户反馈"小指标卡错误"）。
   第 6 格是「最近活动」的时间（10-20 11:56，比其它值宽），单独给 1.4 份宽度；
   横向 padding 压到 1px、间距 2px，把格子里的可用宽度尽量让给文字；
   字号 clamp 跟视口走（1920 约 11px / 2550 约 13px，窄屏下限 9px）。
   实测：1920 格宽 65 / 末格 91，最宽的 "10-20 11:56"(62px) 与 "Completion"(57px) 都不裁切。
   只作用于账号卡，routing 等面板的 .statgrid 不受影响。 */
#acctDetail .statgrid{grid-template-columns:repeat(5,minmax(0,1fr)) minmax(0,1.4fr);gap:2px}
#acctDetail .statgrid>.stat{min-width:0;text-align:center;padding:6px 1px}
#acctDetail .statgrid .sv{font-size:clamp(9px,.58vw,13px);white-space:nowrap;overflow-wrap:normal}
#acctDetail .statgrid .sk{font-size:clamp(8px,.5vw,10.5px);white-space:nowrap;overflow-wrap:normal}
#acctPending{display:flex;flex-wrap:wrap;gap:var(--gap)}
#acctPending>.det{flex:1 1 clamp(340px,23%,480px);min-width:0}
/* 卡里三个窗口：不随卡宽被拉散，居中 */
.wins{max-width:560px;margin:7px auto}
/* 三态视觉：暂存待启用（虚线 + 淡紫）/ 已禁用（降噪） */
.det.pend{border-style:dashed;border-color:#4a3a7a}
.det.dis{opacity:.94}
html[data-theme="light"] .det.pend{border-color:#c9b8f0}
.badge.pend{color:var(--vio);border-color:#4a3a7a}
.badge.warn{color:var(--warn);border-color:#4a3d1d}
html[data-theme="light"] .badge.pend{border-color:#c9b8f0}
html[data-theme="light"] .badge.warn{border-color:#e8cf95}
tbody tr.hist td{color:var(--dim)}
/* ---- 页签（粘在顶栏之下，横向可滚） ---- */
.tabs{display:flex;gap:2px;margin:0 0 var(--gap);padding:2px 0 0;overflow-x:auto;
  border-bottom:1px solid var(--line);scrollbar-width:thin}
.tabs::-webkit-scrollbar{height:5px}
.tab{flex:none;appearance:none;background:transparent;border:1px solid transparent;border-bottom:none;
  color:var(--dim);font:inherit;font-size:clamp(12.5px,.72vw,16px);font-weight:600;
  padding:8px clamp(12px,.95vw,24px) 9px;border-radius:10px 10px 0 0;cursor:pointer;
  white-space:nowrap;transition:color .15s,background .15s}
.tab:hover{color:var(--txt);background:var(--hover)}
.tab.on{color:var(--txt);background:var(--card);border-color:var(--line);
  box-shadow:inset 0 3px 0 var(--acc),var(--shadow)}
.tpane{display:none}
.tpane.on{display:block;animation:dashfade .18s ease}
@keyframes dashfade{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
.tpane>.grid+.grid,.tpane>.grid+.pair,.tpane>.pair+.grid{margin-top:var(--gap)}
/* ---- 超宽屏（≥2100px，即 2560×1440 档）：成对分区并排，避免单列拉成一条长带 ---- */
.pair{display:grid;grid-template-columns:minmax(0,1fr);gap:var(--gap);margin-top:var(--gap);align-items:start}
.pair>.panel{grid-column:auto;min-width:0}
@media (min-width:2100px){.pair{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:1280px){.kpis{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:820px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
/* 窄屏：v12 只给 #gridMain/#gridRoute 钉了单列兜底，新增页签的网格同样要钉死，
   否则 `.panel{grid-column:span 12}` 在 1 列模板里会生成 11 条隐式轨道 → 横向溢出。 */
@media (max-width:820px){
  #gridMain,#gridRoute,#gridPool,#gridUsage,#gridPerf,#gridConfig{grid-template-columns:1fr !important}
  #gridMain>.panel,#gridRoute>.panel,#gridPool>.panel,#gridUsage>.panel,
  #gridPerf>.panel,#gridConfig>.panel{grid-column:1 / -1 !important}
}
@media (max-width:900px){.fp,.adm-note{white-space:normal;overflow-wrap:anywhere}}
/* ===========================================================================
   管理控制台页签（pool_admin_ui._PANEL 片段）样式。
   片段类名沿用驾驶舱命名，这里全部配齐；与 v12 撞名的 `.grid` 用 `.adm .grid`
   作用域覆盖（v12 的 `.grid` 是 12 列面板网格，片段里是复选项目录网格）。
   =========================================================================== */
.adm{display:block}
.adm-hd{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:var(--gap)}
.adm-title{font-size:clamp(15px,.92vw,20px);font-weight:800}
.adm-note{color:var(--dim);font-size:.86em;flex:1 1 320px;min-width:0}
.adm-grid{display:grid;grid-template-columns:minmax(0,1fr);gap:var(--gap)}
.adm-grid>*{min-width:0}
.adm-grid>.sc{margin:0}
@media (min-width:2100px){.adm-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.adm-wide{grid-column:1/-1}}
.adm .sc{background:var(--card);border:1px solid var(--line);border-radius:var(--rad);padding:var(--pad)}
.adm h2{font-size:clamp(11.5px,.66vw,15px);font-weight:700;color:var(--dim);text-transform:uppercase;
  letter-spacing:.07em;display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin:0 0 7px}
.adm h2::before{content:"";width:3px;height:1em;border-radius:2px;background:var(--acc);align-self:center;flex:none}
.h2sub{text-transform:none;letter-spacing:0;color:var(--dim);font-size:.94em;font-weight:400;flex:1 1 200px;min-width:0}
.hint{color:var(--dim);font-size:.86em}
.fp{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.86em;color:var(--dim);
  word-break:break-all}
.adm .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(clamp(180px,11.5vw,290px),1fr));
  gap:3px 14px;margin:10px 0}
.adm .grid label{display:flex;align-items:center;gap:6px;font-size:.9em;cursor:pointer;padding:2px 3px;
  border-radius:5px;min-width:0}
.adm .grid label:hover{background:var(--hover)}
.adm .grid input{accent-color:var(--acc);flex:none}
.adm .tag{font-size:.76em;color:var(--dim);border:1px solid var(--line);border-radius:4px;padding:0 4px;flex:none}
.adm .grid .tag{margin-left:auto}
/* 2026-10-19（task-7）：模型盒里只有 5 个模型，`.adm .grid` 的 auto-fill 会留出空轨道 →
   右侧大片空白（1920 空 531px / 2550 空 913px）。只给这一个盒子改 flex 均分铺满
   （不动 .grid 全局规则）：basis 240px 保证「模型名 + 已部署标签」不折行，
   grow 让每一行都填满（含换行后的最后一行），窄屏按行折行。 */
#modelsBox{display:flex;flex-wrap:wrap;gap:3px 14px}
#modelsBox>label{flex:1 1 240px;min-width:0}
.banner{border-radius:var(--rad);padding:10px 14px;margin-bottom:var(--gap);font-size:.95em;border:1px solid var(--line)}
.banner.ok{background:var(--okbg);border-color:var(--okbd);color:var(--ok)}
.banner.warn{background:var(--warnbg);border-color:var(--warnbd);color:var(--warn)}
.banner.bad{background:var(--errbg);border-color:var(--errbd);color:var(--err)}
.btn{background:var(--btn);color:var(--txt);border:1px solid var(--btnbd);border-radius:7px;
  padding:4px 11px;font:inherit;font-size:.92em;cursor:pointer}
.btn:hover{border-color:var(--acc)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.primary,button.primary{background:var(--primary);border-color:var(--primarybd);color:#fff;font-weight:600}
.btn.danger,button.danger{background:#b91c1c;border-color:#ef4444;color:#fff;font-weight:600}
.btn.ghost{background:transparent}
.inp,input.inp{background:var(--card2);color:var(--txt);border:1px solid var(--line);border-radius:7px;
  padding:4px 9px;font:inherit;font-size:.92em;max-width:100%}
.inp:focus{outline:none;border-color:var(--acc)}
.opt{display:inline-flex;align-items:center;gap:6px;font-size:.92em;cursor:pointer;padding:2px 4px;border-radius:5px}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0}
.acts{white-space:nowrap}
.acts .btn{margin-right:6px}
.tblwrap{overflow-x:auto}
.adm pre{white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:.88em;color:var(--txt);background:var(--pre);border:1px solid var(--line);border-radius:8px;
  padding:10px;max-height:340px;overflow:auto}
.confirm{border:1px solid var(--warnbd);background:var(--warnbg);border-radius:var(--rad);padding:var(--pad);margin:var(--gap) 0}
.cf-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;gap:10px;flex-wrap:wrap}
.cf-label{color:var(--dim);font-size:.82em;margin:8px 0 3px;text-transform:uppercase;letter-spacing:.06em}
#cfDiff{max-height:180px}
.state{font-size:.86em;color:var(--dim)}
.step{padding:3px 0;font-size:.9em}
.chip.on{color:var(--ok);border-color:var(--okbd)}
.chip.off{color:var(--err);border-color:var(--errbd)}
</style></head><body>
<div id="bootWarn">⚠ 本地 echarts.min.js 未加载（vendor/echarts.min.js 缺失或被拦截），图表不可用；数据接口仍正常。</div>
<div id="ktip" role="tooltip"></div>
<div class="top">
 <div class="brand"><span class="logo">Σ</span><div style="min-width:0">
  <b>OpencodeLitellmPool</b>
  <div class="vd">号池看板 · LiteLLM @ 127.0.0.1:4000 · 上游 opencode.ai/inference/go · 无内置鉴权</div>
 </div></div>
 <div class="sp"></div>
 <span class="pill" id="healthPill"><i class="dot" id="healthDot" style="background:#8b93a3"></i><span id="healthTxt">连接中…</span></span>
 <div class="ctl">范围 <select id="rangeSel">
   <option value="all">全部（日志期）</option><option value="24">近 24 小时</option><option value="168">近 7 天</option>
 </select></div>
 <label class="switch" title="自动刷新（间隔记忆在 localStorage）"><input type="checkbox" id="autoChk"><span class="track"><i></i></span>
  <span style="font-size:12px;color:var(--dim)">自动刷新</span></label>
 <div class="ctl"><select id="ivSel">
   <option value="5000">5s</option><option value="15000">15s</option><option value="30000">30s</option>
   <option value="60000">1min</option><option value="300000">5min</option></select></div>
 <button id="reload">刷新</button>
 <button id="themeBtn" class="pill" title="切换深色 / 浅色主题" aria-label="切换主题"><span class="t-dark">☀ 浅色</span><span class="t-light">☾ 深色</span></button>
</div>
<nav class="tabs" id="tabs" role="tablist">
<button class="tab on" data-tab="overview">总览</button>
<button class="tab" data-tab="pool">账号与额度</button>
<button class="tab" data-tab="usage">用量分析</button>
<button class="tab" data-tab="perf">性能与来源</button>
<button class="tab" data-tab="routing">烧量路由</button>
<button class="tab" data-tab="admin">管理控制台</button>
</nav>
<section class="tpane on" id="tab-overview">
<div class="kpis" id="kpis"></div>
<div class="grid" id="gridMain">
<section class="panel c12" data-key="model">
  <div class="ph"><h3>按模型 Token / 请求量</h3>
   <div class="ctl" style="margin-left:6px">排序 <select id="modelSort">
    <option value="tokens">按 Token</option><option value="hits">按请求</option><option value="model">按名称</option></select></div></div>
  <div class="pbody"><div class="chart h300" id="chart-model" data-chart="1"></div><div class="empty">暂无模型用量（请求级日志自 2026-09-26 起记录）</div>
   <div class="note">横向堆叠：<b>Prompt</b> + <b>Completion</b> = 总 Token；点击图例可切换序列，悬浮看 pt/ct/tt 明细。</div></div>
 </section>
</div>
<div class="pair">
<section class="panel c12" data-key="timeline">
  <div class="ph"><h3>Token 时间线</h3><span class="tag">dataZoom 可拖拽缩放 · 滚轮缩放</span></div>
  <div class="pbody"><div class="chart h300" id="chart-timeline" data-chart="1"></div><div class="empty">日志期内无按小时 Token 记录</div></div>
 </section>
<section class="panel c12" data-key="hours">
  <div class="ph"><h3>请求趋势 · 按小时（请求日志）</h3><span class="tag">柱 = 请求数（含轮转归档）· 线 = 失败(容器日志样本)</span></div>
  <div class="pbody"><div class="chart h300" id="chart-hours" data-chart="1"></div><div class="empty">所选范围内无请求</div></div>
 </section>
</div>
</section>

<section class="tpane" id="tab-pool">
<h2 class="sec">账号详情 <span>三态：在池启用 / 已禁用 / 暂存待启用 · 上游三窗口限额（percent + 重置时间）· 池内实际用量</span></h2>
<div class="chips" id="acctSummary" style="margin-bottom:10px"></div>
<div class="cards2" id="acctDetail"></div>
<h2 class="sec" id="acctPendingHd">暂存待启用 <span>已校验可用但未写入控制面 pool-config.json，不计入在池账号数 / 额度 / 路由统计</span></h2>
<div class="cards2" id="acctPending"></div>
<div class="grid" id="gridPool">
<section class="panel c5" data-key="quota">
  <div class="ph"><h3>账号配额 / 用量</h3><span class="tag">上游 usage 三窗口</span></div>
  <div class="pbody"><div class="chart h300" id="chart-quota" data-chart="1"></div><div class="empty">上游 usage 不可用或未配置号池账号</div>
   <div class="note">柱长 = 已用百分比（上游仅返回 percent/status/resetsAt，无绝对 used/limit）；悬浮显示状态与重置时间。<br/>
    只统计<b>池位账号</b>（在池启用 / 已禁用）；<b>暂存待启用</b>账号不进入本图，见页面上方「暂存待启用」区。</div></div>
 </section>
<section class="panel c7" data-key="sess">
  <div class="ph"><h3>会话 / 钉号分布</h3><span class="tag">Top 会话矩树 · 面积=Token</span></div>
  <div class="pbody"><div class="chart h300" id="chart-sesstreemap" data-chart="1"></div><div class="empty">暂无带 sess 字段的请求聚合</div>
   <div class="note">红块 = 同一会话落地 <b>多个账号</b>（粘性偏差 / 钉号漂移告警）。</div></div>
 </section>
</div>
<h2 class="sec">账号 · 会话分配 <span>请求/Token 日志自 2026-09-26 起 · 会话按 assistant 时间 ±8s 对齐</span></h2>
<div class="grid">
<section class="panel c12" data-key="acctSessions">
 <div class="ph"><h3>账号 → 服务过的会话</h3><span class="tag" id="sessTag"></span></div>
 <div class="pbody"><div id="acctSessions"></div></div>
</section>
</div>
</section>

<section class="tpane" id="tab-usage">
<h2 class="sec">用量详情 <span>请求级日志聚合（pool-requests.jsonl 当前文件 + 轮转归档）· 随右上角范围变化 · 点击表头排序</span></h2>
<div class="grid" id="gridUsage">
<section class="panel c12" data-key="usage">
 <div class="ph"><h3>账号份额与用量</h3><span class="tag">含近 24h 份额</span></div>
 <div class="pbody"><div class="chips" id="acctShareChips" style="margin-bottom:6px"></div>
  <div class="note" id="usageNote">份额按累计 Token 计算。</div>
  <div class="scroll" style="max-height:340px;margin-top:6px"><table class="sortable" id="usageTable">
   <thead><tr><th data-sk="account">账号（邮箱）</th><th data-sk="status">状态</th><th class="num" data-sk="hits">请求</th><th class="num" data-sk="pt">Prompt</th>
   <th class="num" data-sk="ct">Completion</th><th class="num" data-sk="tt">总 Token</th><th class="num" data-sk="h24_tt">近24h Token</th>
   <th class="num" data-sk="h24_pct">近24h占比</th><th>主要模型（Top3）</th><th data-sk="lastTs">最近活动</th></tr></thead>
   <tbody id="acctShare"></tbody></table></div></div>
</section>
</div>
<h2 class="sec">账号 × 模型 / 缓存命中 <span>pool-requests.jsonl 按账号与按小时聚合</span></h2>
<div class="grid">
<section class="panel c12" data-key="acctmodel">
  <div class="ph"><h3>账号 × 模型 Token 堆叠</h3><span class="tag">每账号一条</span></div>
  <div class="pbody"><div class="chart h300" id="chart-acctmodel" data-chart="1"></div><div class="empty">日志期内无账号级 token 记录</div></div>
 </section>
</div>
</section>

<section class="tpane" id="tab-perf">
<h2 class="sec">性能与来源 <span>模型耗时与吞吐 · 账号并发分布 · 本地 vs 外网</span></h2>
<div class="grid" id="gridPerf">
<section class="panel c6" data-key="latency">
  <div class="ph"><h3>模型延迟与吞吐</h3><span class="tag">柱 = 中位/P90 · 线 = 输出 tok/s</span></div>
  <div class="pbody"><div class="chart h300" id="chart-latency" data-chart="1"></div><div class="empty">暂无耗时样本（ts−t0 需请求级日志）</div></div>
 </section>
<section class="panel c6" data-key="src">
  <div class="ph"><h3>来源分布 · 本地 vs 外网</h3><span class="tag">按小时堆叠</span></div>
  <div class="pbody"><div class="chart h300" id="chart-src" data-chart="1"></div><div class="empty">暂无按小时来源数据</div>
   <div class="note" id="srcNote"></div></div>
 </section>
</div>
<div class="grid">
<section class="panel c12" data-key="recent">
  <div class="ph"><h3>错误 / 慢请求明细</h3>
   <input type="search" id="recQ" placeholder="筛选 路径 / 状态码…" style="background:#1d2230;color:var(--txt);border:1px solid var(--line2);border-radius:7px;padding:3px 8px;font-size:12px;width:190px">
   <select id="recOnly" style="background:#1d2230;color:var(--txt);border:1px solid var(--line2);border-radius:7px;padding:3px 7px;font-size:12px">
    <option value="all">全部</option><option value="err">仅失败 ≥400</option><option value="ok">仅成功 &lt;400</option></select></div>
  <div class="pbody">
   <div class="recwrap" id="recWrap">
    <div class="scroll" style="max-height:300px"><table class="sortable" id="recTable">
      <thead><tr><th data-sk="ts">时间</th><th data-sk="method">方法</th><th data-sk="path">路径</th><th data-sk="code" class="num">状态码</th></tr></thead>
      <tbody id="recBody"></tbody></table></div>
    <div><div style="font-size:11.5px;color:var(--dim);margin-bottom:5px">慢模型排行（P90 耗时 → 中位耗时）</div>
     <table class="sortable" id="slowTable"><thead><tr><th data-sk="model">模型</th><th class="num" data-sk="p90Ms">P90</th><th class="num" data-sk="medMs">中位</th><th class="num" data-sk="ctPerSec">tok/s</th></tr></thead>
     <tbody id="slowBody"></tbody></table></div>
   </div>
   <div class="note" id="recNote">数据源：<span class="mono">docker logs litellm --tail 3000</span>（最近 15 条）+ pool-requests.jsonl 耗时聚合；完整请求级日志在 <span class="mono">/opt/litellm/pool-requests.jsonl</span>。</div>
  </div>
 </section>
</div>
</section>

<section class="tpane" id="tab-routing">
<h2 class="sec">烧量路由观测面 <span>取数：routing-share.json / shadow-decisions.jsonl / pool-requests.jsonl / usage 快照；读不到数据的面板显示「数据未就绪」，绝不影响其余面板</span></h2>
<div class="grid" id="gridRoute">
<section class="panel c12" data-key="alerts"><div class="ph"><h3>告警总线</h3>
  <span class="tag">8 条判定 · 只展开触发项，未触发折叠成一行</span></div>
  <div class="pbody" id="alertBar"></div></section>
<section class="panel c7" data-key="p1"><div class="ph"><h3>烧量倒计时</h3>
  <span class="tag">剩余额度 ÷ 近 24h 实测烧量 → 耗尽时间 vs 重置时间</span></div>
  <div class="pbody" id="panelP1"></div></section>
<section class="panel c5" data-key="p2"><div class="ph"><h3>目标 vs 实际份额</h3>
  <span class="tag">近 1h 滚动窗 · 按实际份额排序</span></div>
  <div class="pbody" id="panelP2"></div></section>
<section class="panel c12" data-key="p3"><div class="ph"><h3>影子命中 · 分档 · 迁移缓存 · 门禁健康</h3>
  <span class="tag">key_want/key_actual · 会话分档分布 · G5 预算与 warm 命中 · 快照时效与门禁</span></div>
  <div class="pbody" id="panelP3456"></div></section>
</div>
</section>

<section class="tpane" id="tab-admin">
__ADMIN_PANEL__
<!-- 2026-10-31 弃用并移除：DSH 配置 · 模型同步 / Agent Teams 团队模型规则 / 写入 DSH 配置。
     号池 provider 路由已由 dsh-pool-affinity 插件声明式提供；本页不再写 DSH 配置。
     后端业务代码已注释保留（见 dashboard.py「2026-10-31 弃用」段）。 -->
</section>
<div class="note" id="refresh" style="margin-top:14px"></div>
<script src="vendor/echarts.min.js"></script>
<script>
/* =========================================================================
   号池看板 v12（OpencodeLitellmPool）：界面全部走 ECharts 本地 vendored 5.x
   - 零构建零 CDN：ECharts 以相对路径 vendor/echarts.min.js 引入、fetch("api/...") 亦为相对路径，
     因此经 https://<域名>/works/pool/ 反代前缀访问时不会 404。
   - 只消费既有 JSON 接口：api/summary / api/account-sessions。
     （2026-10-31 弃用：api/teams-config / api/models-config / api/sync-models /
       api/save-teams-config 随「DSH 配置 / Agent Teams」死配置一起下线。）
   - 空数据/缺字段一律走 setEmpty() 占位，绝不抛错白屏。
   ========================================================================= */
var SUM=null,SESSD=null,RT=null;   /* 2026-10-31：TC/MODELS/SEL 随 DSH 配置同步下线移除 */
var INST={},REC={q:"",only:"all",sort:"ts",dir:-1},SLOW={sort:"p90Ms",dir:-1};
var STATE={range:"all",iv:15000,auto:true};
var PAL=["#60a5fa","#4ade80","#fbbf24","#f472b6","#a78bfa","#22d3ee","#fb923c","#94a3b8","#34d399","#e879f9","#facc15","#f87171"];
function $(id){return document.getElementById(id)}
function esc(s){return String(s==null?"":s).split("&").join("&amp;").split("<").join("&lt;").split(">").join("&gt;").split('"').join("&quot;")}
function fmtInt(n){return (Math.round(+n||0)).toLocaleString("en-US")}
function fmtTok(n){n=+n||0;var a=Math.abs(n);
 if(a>=1e12)return (n/1e12).toFixed(2)+"T";if(a>=1e9)return (n/1e9).toFixed(2)+"B";
 if(a>=1e6)return (n/1e6).toFixed(2)+"M";return fmtInt(n)}
function fmtMs(ms){ms=Math.round(+ms||0);if(ms<1000)return ms+"ms";if(ms<60000)return (ms/1000).toFixed(1)+"s";
 var m=Math.floor(ms/60000),s=Math.round((ms%60000)/1000);return m+"m"+(s<10?"0":"")+s+"s"}
function fmtTs(ms){if(!ms)return "-";var d=new Date(+ms),p=function(n){return String(n).padStart(2,"0")};
 return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes())}
function fmtTsShort(ms){if(!ms)return "—";var d=new Date(+ms),p=function(n){return String(n).padStart(2,"0")};
 return p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes())}
function fmtDur(s){if(s==null)return "∞";var h=s/3600;return h>=48?(h/24).toFixed(1)+" 天":h.toFixed(1)+" 小时"}
function rel(iso){if(!iso)return "";var ms=new Date(iso)-Date.now();if(ms<=0)return "已重置";
 var mins=Math.round(ms/60000),h=Math.floor(mins/60),m=mins%60;
 if(h>=24)return (mins/1440).toFixed(1)+" 天后重置";return h>0?h+"h"+m+"m 后重置":m+"m 后重置"}
function arr(a){return Object.prototype.toString.call(a)==="[object Array]"?a:[]}
function obj(o){return (o&&typeof o==="object")?o:{}}
function num(n){return +n||0}
function hLabel(h){return String(h||"").slice(5).split(" ").join("日")+"时"}
function hourMs(h){var p=String(h||"").split(/[^0-9]/).filter(Boolean).map(Number);
 if(p.length<3)return 0;return new Date(p[0],(p[1]||1)-1,p[2]||1,p[3]||0,0,0).getTime()}
function cutMs(){if(STATE.range==="all")return 0;var d=new Date();d.setHours(d.getHours()-num(STATE.range));return d.getTime()}
function rangeName(){return STATE.range==="all"?"全部日志期":STATE.range==="24"?"近 24 小时":STATE.range==="168"?"近 7 天":String(STATE.range||"")}
function inRange(h){var c=cutMs();return !c||hourMs(h)>=c}
function fHours(rows){return arr(rows).filter(function(r){return inRange(r&&r.h)})}
function setEmpty(id,isEmpty,msg){
 var el=$(id);if(!el)return;var box=el.closest(".pbody");if(!box)return;
 box.classList.toggle("is-empty",!!isEmpty);
 var e=box.querySelector(".empty");if(e&&msg)e.textContent=msg}
var LS={get:function(k,d){try{var v=localStorage.getItem(k);return v==null?d:JSON.parse(v)}catch(e){return d}},
        set:function(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
var AX={axisLine:{lineStyle:{color:"#2c3342"}},axisTick:{show:false},axisLabel:{color:"#8b93a3",fontSize:10.5},
        splitLine:{lineStyle:{color:"#1f242e"}}};
var TIPB={backgroundColor:"rgba(13,15,20,.97)",borderColor:"#2c3342",borderWidth:1,padding:[7,10],
          textStyle:{color:"#e6e9ef",fontSize:12},extraCssText:"border-radius:8px;box-shadow:0 10px 26px rgba(0,0,0,.5)"};
function tipAxis(ex){return Object.assign({trigger:"axis",axisPointer:{type:"shadow",shadowStyle:{color:"rgba(96,165,250,.07)"}}},TIPB,ex||{})}
function tipItem(ex){return Object.assign({trigger:"item"},TIPB,ex||{})}
function gridOpt(t,b,l,r){return {left:l==null?6:l,right:r==null?12:r,top:t==null?28:t,bottom:b==null?4:b,containLabel:true}}
function legendOpt(extra){return Object.assign({textStyle:{color:"#8b93a3",fontSize:11},itemWidth:11,itemHeight:8,top:2,right:8},extra||{})}
/* ---------------- KPI ---------------- */
var KPIS=[{k:"total",n:"总请求",c:"#60a5fa"},{k:"success",n:"成功率",c:"#4ade80"},{k:"tt",n:"总 Token",c:"#a78bfa"},
          {k:"mtt",n:"本月 Token",c:"#38bdf8"},
          {k:"cache",n:"缓存命中",c:"#22d3ee"},{k:"acct",n:"在池账号",c:"#fbbf24"},{k:"models",n:"当前模型数",c:"#f472b6"}];
var DETAIL={};
function statusParts(){
 var bs=obj(SUM&&SUM.by_status),o={ok:0,redir:0,c4:0,c5:0,other:0},tot=0;
 Object.keys(bs).forEach(function(k){var n=num(bs[k]),c=num(k);tot+=n;
  if(c>=500)o.c5+=n;else if(c>=400)o.c4+=n;else if(c>=300)o.redir+=n;else if(c>=200)o.ok+=n;else o.other+=n});
 o.tot=tot;return o}
function renderKpis(){
 var d=obj(SUM),ut=obj(d.usage_totals),mu=obj(d.month_usage),cr=obj(obj(d.cache_rate).overall),st=statusParts();
 var byAcct=arr(d.by_account),lim=num(d.accounts_limited);
 var nPool=num(d.accounts),nEn=num(d.accounts_enabled),nDis=num(d.accounts_disabled),
     nPend=num(d.accounts_pending),nAct=num(d.accounts_active),nHist=num(d.accounts_historic),
     nDep=num(d.accounts_deployed);
 var okPct=st.tot?((st.tot-st.c4-st.c5)*100/st.tot):0;
 var vals={
  total:{v:fmtInt(d.total),s:"失败样本 "+fmtInt(st.c4+st.c5)+" · 窗口 "+fmtInt(d.total),cls:(st.c4+st.c5)>0?"warn":""},
  success:{v:(st.tot?okPct.toFixed(1):"—")+(st.tot?"%":""),s:st.tot?("2xx "+fmtInt(st.ok)+" · 3xx "+fmtInt(st.redir)):"暂无请求",cls:st.tot?(okPct>=99?"ok":okPct>=95?"warn":"err"):"dimv"},
  tt:{v:fmtTok(ut.tt),s:"P "+fmtTok(ut.pt)+" / C "+fmtTok(ut.ct),cls:""},
  mtt:{v:fmtTok(mu.tt),s:mu.hits?("自 "+fmtTsShort(mu.since)+" · "+fmtInt(mu.hits)+" 请求"):"本月暂无请求",cls:""},
  cache:{v:num(cr.rate).toFixed(1)+"%",s:"cached "+fmtTok(cr.cached)+" / pt "+fmtTok(cr.pt),cls:num(cr.rate)>=30?"ok":num(cr.rate)>0?"warn":"dimv"},
  /* 口径：启用/池位（不再拿历史日志尾号数当分子 —— 旧「活跃账号 9 / 6」即 9>6 自相矛盾） */
  acct:{v:fmtInt(nEn)+" / "+fmtInt(nPool),
        s:"暂存 "+fmtInt(nPend)+" · 禁用 "+fmtInt(nDis)+" · 受限 "+fmtInt(lim),
        cls:lim>0?"warn":""},
  models:{v:fmtInt(d.models),s:"配置 deployments "+fmtInt(d.deployments),cls:""}};
 $("kpis").innerHTML=KPIS.map(function(x){
  var v=vals[x.k]||{v:"-",s:""};
  return '<div class="kpi" data-k="'+x.k+'" style="--k:'+x.c+'"><div class="k">'+x.n+'</div>'+
   '<div class="v '+(v.cls||"")+'">'+v.v+'</div><div class="s">'+v.s+'</div></div>'}).join("");
 var topM=arr(d.by_model).slice(0,3).map(function(m){return m.model+" · "+fmtTok(m.tokens)}).join("<br/>")||"—";
 var topA=byAcct.slice(0,3).map(function(a){return acctName(a)+" · "+fmtTok(a.tt)}).join("<br/>")||"—";
 var cm=arr(obj(d.cache_rate).by_model).slice(0,3).map(function(m){return m.model+" · "+num(m.rate).toFixed(1)+"%"}).join("<br/>")||"—";
 DETAIL={
  total:'<b>请求总数 '+fmtInt(d.total)+'</b><div class="r"><i>范围</i><span>'+rangeName()+'</span></div><div class="r"><i>失败数样本</i><span>最近 3000 条容器日志</span></div>'+
   '<div class="r"><i>2xx / 3xx</i><span>'+fmtInt(st.ok)+' / '+fmtInt(st.redir)+'</span></div>'+
   '<div class="r"><i>4xx / 5xx</i><span>'+fmtInt(st.c4)+' / '+fmtInt(st.c5)+'</span></div>'+
   '<div class="r"><i>成功率</i><span>'+(st.tot?okPct.toFixed(2)+"%":"—")+'</span></div>',
  success:'<b>成功率 '+(st.tot?okPct.toFixed(2)+"%":"—")+'</b><div class="r"><i>成功(2xx/3xx)</i><span>'+fmtInt(st.ok+st.redir)+'</span></div>'+
   '<div class="r"><i>客户端错误 4xx</i><span>'+fmtInt(st.c4)+'</span></div><div class="r"><i>服务端错误 5xx</i><span>'+fmtInt(st.c5)+'</span></div>'+
   '<div class="r"><i>口径</i><span>status &lt; 400 记为成功</span></div>',
  tt:'<b>累计 '+fmtTok(ut.tt)+' tokens</b><div class="r"><i>Prompt</i><span>'+fmtInt(ut.pt)+'</span></div>'+
   '<div class="r"><i>Completion</i><span>'+fmtInt(ut.ct)+'</span></div>'+
   '<div class="r"><i>日志起点</i><span>'+(ut.firstTs?fmtTs(ut.firstTs):"—")+'</span></div>'+
   '<div class="r"><i>最近一条</i><span>'+(ut.lastTs?fmtTs(ut.lastTs):"—")+'</span></div>'+
   '<div class="r"><i>近 24h</i><span>'+fmtInt(obj(d.last24).hits)+' 请求 · '+fmtTok(obj(d.last24).tt)+'</span></div>',
   mtt:'<b>本月累计 '+fmtTok(mu.tt)+' tokens</b><div class="r"><i>统计周期</i><span>'+esc(mu.month||"—")+' 月（每月 1 日起）</span></div>'+
    '<div class="r"><i>起点</i><span>'+(mu.since?fmtTs(mu.since):"—")+'</span></div>'+
    '<div class="r"><i>Prompt / Completion</i><span>'+fmtTok(mu.pt)+' / '+fmtTok(mu.ct)+'</span></div>'+
    '<div class="r"><i>请求数</i><span>'+fmtInt(mu.hits)+'</span></div>',
  cache:'<b>整体命中 '+num(cr.rate).toFixed(1)+'%</b><div class="r"><i>cached</i><span>'+fmtInt(cr.cached)+'</span></div>'+
   '<div class="r"><i>prompt 分母</i><span>'+fmtInt(cr.pt)+'</span></div><div class="r"><i>按模型 Top3</i><span style="text-align:right">'+cm+'</span></div>',
  acct:'<b>在池启用 '+fmtInt(nEn)+' / 池位 '+fmtInt(nPool)+'</b>'+
   '<div class="r"><i>暂存待启用</i><span>'+fmtInt(nPend)+'</span></div>'+
   '<div class="r"><i>已禁用</i><span>'+fmtInt(nDis)+'（其中受限 '+fmtInt(num(d.accounts_limited_disabled))+'）</span></div>'+
   '<div class="r"><i>有 deployment</i><span>'+fmtInt(nDep)+' / '+fmtInt(nPool)+'</span></div>'+
   '<div class="r"><i>在池受限</i><span>'+fmtInt(lim)+'</span></div>'+
   '<div class="r"><i>日志内有调用</i><span>'+fmtInt(nAct)+' 个当前账号</span></div>'+
   '<div class="r"><i>已下线账号</i><span>'+fmtInt(nHist)+'（已下线，不计入账号数）</span></div>'+
   '<div class="r"><i>用量 Top3</i><span style="text-align:right">'+topA+'</span></div>',
  models:'<b>可用模型 '+fmtInt(d.models)+'</b><div class="r"><i>deployments</i><span>'+fmtInt(d.deployments)+'</span></div>'+
   '<div class="r"><i>Token Top3</i><span style="text-align:right">'+topM+'</span></div>'};
}
(function(){
 var tip=$("ktip");if(!tip)return;
 document.addEventListener("mouseover",function(e){
  var card=e.target.closest?e.target.closest(".kpi"):null;
  if(!card){tip.style.display="none";return}
  var k=card.getAttribute("data-k");if(!DETAIL[k])return;
  tip.innerHTML=DETAIL[k];tip.style.display="block"});
 document.addEventListener("mousemove",function(e){
  if(tip.style.display!=="block")return;
  var w=tip.offsetWidth||260,h=tip.offsetHeight||80,x=e.clientX+14,y=e.clientY+14;
  if(x+w>window.innerWidth-8)x=e.clientX-w-14;
  if(y+h>window.innerHeight-8)y=Math.max(8,e.clientY-h-14);
  tip.style.left=x+"px";tip.style.top=y+"px"});
})();
/* ---------------- 图表 1：状态码占比（环形） ---------------- */
function colorOfCode(k){var n=num(k);return n>=500?"#f87171":n>=400?"#fbbf24":n>=300?"#60a5fa":n>=200?"#4ade80":"#94a3b8"}
/* ---------------- 图表 2：按模型 Token（横向堆叠，可排序） ---------------- */
function renderModelBar(){
 var el=$("chart-model");if(!el||!window.echarts)return;
 var c=INST["chart-model"]||(INST["chart-model"]=echarts.init(el));
 var rows=arr(obj(SUM).by_model).slice(),key=($("modelSort")||{}).value||"tokens";
 rows.sort(function(a,b){
  if(key==="model")return String(a.model).localeCompare(String(b.model));
  return num(b[key])-num(a[key])});
 var top=rows.slice(0,14);
 setEmpty("chart-model",!top.length,"暂无模型用量（请求级日志自 2026-09-26 起记录）");
 var rev=top.slice().reverse();
 function ser(name,col,field){return {name:name,type:"bar",stack:"tok",barMaxWidth:15,
   emphasis:{focus:"series"},itemStyle:{color:col},data:rev.map(function(r){return num(r[field])})}}
 c.setOption({animationDuration:400,
  tooltip:tipAxis({formatter:function(ps){
   var r=rev[ps[0].dataIndex]||{};if(!r.model)return "";
   return "<b>"+esc(r.model)+"</b><br/>请求 "+fmtInt(r.hits)+" 次<br/>Prompt <b>"+fmtInt(r.pt)+"</b><br/>Completion <b>"+
    fmtInt(r.ct)+"</b><br/>总 Token <b>"+fmtInt(r.tokens)+"</b>（"+fmtTok(r.tokens)+"）"}}),
  legend:legendOpt({data:["Prompt","Completion"]}),
  grid:gridOpt(30,4,6,14),
  xAxis:Object.assign({type:"value"},AX,{axisLabel:{color:"#8b93a3",fontSize:10.5,formatter:function(v){return fmtTok(v)}}}),
  yAxis:Object.assign({type:"category",data:rev.map(function(r){return r.model}),
   axisLabel:{color:"#c9d1e0",fontSize:11,width:190,overflow:"truncate"}},AX),
  series:[ser("Prompt","#60a5fa","pt"),ser("Completion","#a78bfa","ct")]},true);
}
/* ---------------- 图表 3：Token 时间线（dataZoom） ---------------- */
function renderTimeline(){
 var el=$("chart-timeline");if(!el||!window.echarts)return;
 var c=INST["chart-timeline"]||(INST["chart-timeline"]=echarts.init(el));
 var rows=fHours(obj(SUM).tokens_by_hour);
 setEmpty("chart-timeline",!rows.length,"日志期内无按小时 Token 记录");
 var x=rows.map(function(r){return hLabel(r.h)});
 function L(name,col,field,extra){return Object.assign({name:name,type:"line",smooth:true,symbol:"circle",symbolSize:4,
   lineStyle:{width:1.8,color:col},itemStyle:{color:col},data:rows.map(function(r){return num(r[field])})},extra||{})}
 c.setOption({animationDuration:400,
  tooltip:tipAxis({formatter:function(ps){
   var r=rows[ps[0].dataIndex]||{};
   return "<b>"+esc(r.h)+"</b><br/>总 Token <b>"+fmtInt(r.tt)+"</b><br/>Prompt "+fmtInt(r.pt)+"<br/>缓存命中 "+fmtInt(r.cached)+
    "<br/>请求 "+fmtInt(r.hits)+" 次"}}),
  legend:legendOpt({data:["总 Token","Prompt","缓存命中"]}),
  grid:gridOpt(30,54,6,10),
  xAxis:Object.assign({type:"category",boundaryGap:false,data:x,axisLabel:{color:"#8b93a3",fontSize:10.5,hideOverlap:true}},AX),
  yAxis:Object.assign({type:"value"},AX,{axisLabel:{color:"#8b93a3",fontSize:10.5,formatter:function(v){return fmtTok(v)}}}),
  dataZoom:[{type:"inside",throttle:40},{type:"slider",height:14,bottom:6,borderColor:"#232833",
   backgroundColor:"#12151c",fillerColor:"rgba(96,165,250,.16)",handleSize:"90%",textStyle:{color:"#8b93a3",fontSize:10}}],
  series:[L("总 Token","#60a5fa","tt",{areaStyle:{color:"rgba(96,165,250,.16)"}}),
          L("Prompt","#22d3ee","pt"),L("缓存命中","#4ade80","cached")]},true);
}
/* ---------------- 图表 4：账号 × 模型 Token 堆叠 ---------------- */
function acctName(a){return (a&&a.account)||"未知账号"}
/* 历史轮换尾号（日志里出现过、当前 .env 已无此 key 尾号）：单列并明确标注，
   绝不与当前在池账号混为一个数字（旧「活跃账号 9 / 池内 6」的根因）。 */
function accountLabel(a){if(!a)return "未知账号";
 return a.account||"已下线账号"}
function renderAcctModel(){
 var el=$("chart-acctmodel");if(!el||!window.echarts)return;
 var c=INST["chart-acctmodel"]||(INST["chart-acctmodel"]=echarts.init(el));
 var accts=arr(obj(SUM).by_account).slice(0,10);
 var tot={};
 accts.forEach(function(a){arr(a.models).forEach(function(m){tot[m.model]=(tot[m.model]||0)+num(m.tokens)})});
 var models=Object.keys(tot).sort(function(a,b){return tot[b]-tot[a]}).slice(0,8);
 var labels=accts.map(accountLabel);
 var series=models.map(function(mm,i){
  var idx={};accts.forEach(function(a){var f=null;arr(a.models).forEach(function(m){if(m.model===mm)f=m});if(f)idx[a.tail||a.account]=num(f.tokens)});
  return {name:mm,type:"bar",stack:"tk",barMaxWidth:26,emphasis:{focus:"series"},
   itemStyle:{color:PAL[i%PAL.length]},data:accts.map(function(a){return idx[a.tail||a.account]||0})}});
 if(models.length>1)series.push({name:"其他",type:"bar",stack:"tk",barMaxWidth:26,itemStyle:{color:"#475069"},
  data:accts.map(function(a){var s=0;arr(a.models).forEach(function(m){if(models.indexOf(m.model)<0)s+=num(m.tokens)});return s})});
 setEmpty("chart-acctmodel",!accts.length||!models.length,"日志期内无账号级 token 记录");
 c.setOption({animationDuration:400,
  tooltip:tipAxis({formatter:function(ps){
   var a=accts[ps[0].dataIndex]||{};var out="<b>"+esc(acctName(a))+"</b><br/>总 Token <b>"+fmtInt(a.tt)+"</b> · 请求 "+fmtInt(a.hits)+" 次<hr style='border:none;border-top:1px solid #2c3342;margin:4px 0'/>";
   ps.forEach(function(p){if(num(p.value)>0)out+="<span style='display:inline-block;width:8px;height:8px;border-radius:2px;background:"+p.color+";margin-right:5px'></span>"+esc(p.seriesName)+" "+fmtInt(p.value)+"<br/>"});
   return out}}),
  legend:legendOpt({type:"scroll",top:2,data:series.map(function(s){return s.name})}),
  grid:gridOpt(46,4,6,10),
  xAxis:Object.assign({type:"category",data:labels,axisLabel:{color:"#c9d1e0",fontSize:10.5,rotate:22,width:150,overflow:"truncate"}},AX),
  yAxis:Object.assign({type:"value"},AX,{axisLabel:{color:"#8b93a3",fontSize:10.5,formatter:function(v){return fmtTok(v)}}}),
  series:series},true);
}
/* ---------------- 图表 5：账号配额 / 用量 ---------------- */
function winOf(a,w){return obj(obj(a.windows)[w])}
function renderQuota(){
 var el=$("chart-quota");if(!el||!window.echarts)return;
 var c=INST["chart-quota"]||(INST["chart-quota"]=echarts.init(el));
 /* 只画池位账号（启用/已禁用）；暂存待启用账号不进入在池额度口径，见账号卡单独展示 */
 var rows=arr(obj(SUM).account_usage).filter(function(a){return a.poolState!=="pending"});
 var WINS=[["rolling","滚动"],["weekly","每周"],["monthly","每月"]];
 setEmpty("chart-quota",!rows.length,"上游 usage 不可用或未配置号池账号");
 var labels=rows.map(function(a){return (a.label||a.key||"账号")+(a.poolState==="disabled"?"（禁用）":"")});
 var series=WINS.map(function(w,i){
  return {name:w[1],type:"bar",barMaxWidth:9,emphasis:{focus:"series"},itemStyle:{color:PAL[i%PAL.length]},
   data:rows.map(function(a){var v=winOf(a,w[0]);return v.percent==null?null:num(v.percent)}),
   markLine:{silent:true,symbol:"none",lineStyle:{color:"#f87171",type:"dashed",width:1},
    label:{show:false},data:[{xAxis:100}]}}});
 c.setOption({animationDuration:400,
  tooltip:tipAxis({axisPointer:{type:"shadow"},formatter:function(ps){
   var a=rows[ps[0].dataIndex]||{};
   var pst=a.poolState==="inpool"?"<span style='color:#4ade80'>在池 · 已启用</span>":
    a.poolState==="disabled"?"<span style='color:#fbbf24'>已禁用 · 未入池</span>":
    a.poolState==="pending"?"<span style='color:#a78bfa'>暂存待启用</span>":
    "<span style='color:#fbbf24'>已启用 · 无部署</span>";
   var out="<b>"+esc(a.label||a.key)+"</b><br/>"+
    pst+(a.deployments?" · 部署 "+fmtInt(a.deployments)+" 条":"")+
    "<hr style='border:none;border-top:1px solid #2c3342;margin:4px 0'/>";
   WINS.forEach(function(w){
    var v=winOf(a,w[0]);
    out+="<span style='color:#8b93a3'>"+w[1]+"</span> "+(v.percent==null?"—":num(v.percent)+"%")+
     " · "+(v.status?esc(v.status):"unknown")+(v.resetsAt?" · "+fmtTs(new Date(v.resetsAt).getTime())+"（"+rel(v.resetsAt)+"）":"")+"<br/>"});
   return out}}),
  legend:legendOpt({data:WINS.map(function(w){return w[1]})}),
  grid:gridOpt(30,4,6,10),
  xAxis:Object.assign({type:"value",max:100},AX,{axisLabel:{color:"#8b93a3",fontSize:10.5,formatter:"{value}%"}}),
  yAxis:Object.assign({type:"category",data:labels,axisLabel:{color:"#c9d1e0",fontSize:10.5,width:140,overflow:"truncate"}},AX),
  series:series},true);
}
/* ---------------- 图表 6：会话 / 钉号分布（矩树） ---------------- */
function renderSessTree(){
 var el=$("chart-sesstreemap");if(!el||!window.echarts)return;
 var c=INST["chart-sesstreemap"]||(INST["chart-sesstreemap"]=echarts.init(el));
 var rows=arr(obj(SUM).top_sessions);
 setEmpty("chart-sesstreemap",!rows.length,"暂无带 sess 字段的请求聚合");
 var data=rows.map(function(r){
  var drift=num(r.accounts)>1;
  return {name:String(r.sess||"?").slice(0,10),value:Math.max(1,num(r.tt)),
   raw:r,itemStyle:{color:drift?"#7f2330":"#1e3a5f",borderColor:drift?"#f87171":"#3b82f6",borderWidth:1}}});
 c.setOption({animationDuration:400,
  tooltip:tipItem({formatter:function(p){var r=obj(p.data.raw);
   var ms=arr(r.models).map(function(m){return esc(m.model)+" "+fmtInt(m.hits)+"次"}).join("<br/>")||"—";
   return "<b>"+esc(r.sess)+"</b><br/>"+fmtInt(r.hits)+" 次 · <b>"+fmtInt(r.tt)+"</b> tokens<br/>"+
    "落地账号 "+num(r.accounts)+" 个"+(num(r.accounts)>1?" <span style='color:#f87171'>⚠ 粘性偏差</span>":"")+
    "<br/>活跃 "+fmtTsShort(r.first)+" → "+fmtTsShort(r.last)+"<hr style='border:none;border-top:1px solid #2c3342;margin:4px 0'/>"+ms}}),
  series:[{type:"treemap",roam:false,nodeClick:false,breadcrumb:{show:false},width:"100%",height:"94%",top:2,
   itemStyle:{borderColor:"#161a21",borderWidth:2,gapWidth:2},emphasis:{itemStyle:{borderColor:"#60a5fa"}},
   label:{show:true,color:"#e6e9ef",fontSize:11,formatter:function(p){
     return String(p.name||"")+String.fromCharCode(10)+fmtTok(num(obj(p.data).value))}},
   upperLabel:{show:false},data:data}]},true);
}
/* ---------------- 图表 9：模型延迟与吞吐（双轴） ---------------- */
function renderLatency(){
 var el=$("chart-latency");if(!el||!window.echarts)return;
 var c=INST["chart-latency"]||(INST["chart-latency"]=echarts.init(el));
 var rows=arr(obj(SUM).model_perf).slice(0,12);
 setEmpty("chart-latency",!rows.length,"暂无耗时样本（ts−t0 需请求级日志）");
 var lab=rows.map(function(r){return r.model});
 c.setOption({animationDuration:400,
  tooltip:tipAxis({formatter:function(ps){var r=rows[ps[0].dataIndex]||{};
   return "<b>"+esc(r.model)+"</b><br/>样本 "+fmtInt(r.n)+" 次<br/>中位 <b>"+fmtMs(r.medMs)+"</b><br/>P90 "+fmtMs(r.p90Ms)+
    "<br/>输出 "+(+r.ctPerSec||0).toFixed(1)+" tok/s<br/>总吞吐 "+(+r.ttPerSec||0).toFixed(1)+" tok/s<br/>Token "+fmtInt(r.tokens)}}),
  legend:legendOpt({data:["中位耗时","P90 耗时","输出 tok/s"]}),
  grid:gridOpt(30,4,6,10),
  xAxis:Object.assign({type:"category",data:lab},AX,{axisLabel:{color:"#c9d1e0",fontSize:9.5,rotate:30,interval:0,hideOverlap:false,margin:6}}),
  yAxis:[Object.assign({type:"value"},AX,{axisLabel:{color:"#8b93a3",fontSize:10.5,formatter:function(v){return fmtMs(v)}}}),
         Object.assign({type:"value",axisLabel:{color:"#fbbf24",fontSize:10.5},splitLine:{show:false}},AX)],
  series:[{name:"中位耗时",type:"bar",barMaxWidth:15,itemStyle:{color:"#60a5fa"},data:rows.map(function(r){return num(r.medMs)})},
          {name:"P90 耗时",type:"bar",barMaxWidth:15,itemStyle:{color:"rgba(96,165,250,.4)"},data:rows.map(function(r){return num(r.p90Ms)})},
          {name:"输出 tok/s",type:"line",yAxisIndex:1,smooth:true,symbolSize:5,lineStyle:{color:"#fbbf24",width:1.8},itemStyle:{color:"#fbbf24"},data:rows.map(function(r){return num(r.ctPerSec)})}]},true);
}
/* ---------------- 图表 10：来源分布 ---------------- */
function renderSrc(){
 var el=$("chart-src");if(!el||!window.echarts)return;
 var c=INST["chart-src"]||(INST["chart-src"]=echarts.init(el));
 var d=obj(SUM),s=obj(d.by_source),lo=obj(s.local),ex=obj(s.ext);
 var rows=fHours(d.src_by_hour);
 var lh=num(lo.hits),eh=num(ex.hits),lt=num(lo.tt),et=num(ex.tt);
 var hT=Math.max(1,lh+eh),tT=Math.max(1,lt+et);
 var note=$("srcNote");
 if(note)note.innerHTML="请求占比：本地 <b>"+(lh*100/hT).toFixed(1)+"%</b> / 外网 <b>"+(eh*100/hT).toFixed(1)+"%</b> · "+
  "Token 占比：本地 <b>"+(lt*100/tT).toFixed(1)+"%</b> / 外网 <b>"+(et*100/tT).toFixed(1)+"%</b>（"+
  fmtInt(lh)+" / "+fmtInt(eh)+" 次请求）";
 setEmpty("chart-src",!rows.length,"暂无按小时来源数据");
 c.setOption({animationDuration:400,
  tooltip:tipAxis({formatter:function(ps){var r=rows[ps[0].dataIndex]||{};
   var t=num(r.local)+num(r.ext);
   return "<b>"+esc(r.h)+"</b><br/>本地 "+fmtInt(r.local)+" 次（"+(t?(r.local*100/t).toFixed(1):"0")+"%）<br/>"+
    "外网 "+fmtInt(r.ext)+" 次（"+(t?(r.ext*100/t).toFixed(1):"0")+"%）"}}),
  legend:legendOpt({data:["本地(sidecar)","外网(nginx)"]}),
  grid:gridOpt(30,4,6,10),
  xAxis:Object.assign({type:"category",data:rows.map(function(r){return hLabel(r.h)}),axisLabel:{color:"#8b93a3",fontSize:10,hideOverlap:true}},AX),
  yAxis:Object.assign({type:"value",axisLabel:{color:"#8b93a3",fontSize:10.5}},AX),
  series:[{name:"本地(sidecar)",type:"bar",stack:"s",barMaxWidth:18,itemStyle:{color:"#60a5fa"},data:rows.map(function(r){return num(r.local)})},
          {name:"外网(nginx)",type:"bar",stack:"s",barMaxWidth:18,itemStyle:{color:"#fbbf24"},data:rows.map(function(r){return num(r.ext)})}]},true);
}
/* ---------------- 图表 12：请求趋势（柱 + 失败线） ---------------- */
function renderHours(){
 var el=$("chart-hours");if(!el||!window.echarts)return;
 var c=INST["chart-hours"]||(INST["chart-hours"]=echarts.init(el));
 var rows=fHours(obj(SUM).by_hour);
 setEmpty("chart-hours",!rows.length,"所选范围内无请求");
 c.setOption({animationDuration:400,
  tooltip:tipAxis({formatter:function(ps){var r=rows[ps[0].dataIndex]||{};
   return "<b>"+esc(r.h)+"</b><br/>请求 <b>"+fmtInt(r.n)+"</b><br/>失败 "+fmtInt(r.err)+
    "（"+(num(r.n)?(num(r.err)*100/num(r.n)).toFixed(1):"0")+"%）"}}),
  legend:legendOpt({data:["请求数","失败(≥400)"]}),
  grid:gridOpt(26,4,6,10),
  xAxis:Object.assign({type:"category",data:rows.map(function(r){return hLabel(r.h)}),axisLabel:{color:"#8b93a3",fontSize:10,hideOverlap:true}},AX),
  yAxis:Object.assign({type:"value",axisLabel:{color:"#8b93a3",fontSize:10.5}},AX),
  series:[{name:"请求数",type:"bar",barMaxWidth:22,itemStyle:{color:"#3b82f6"},data:rows.map(function(r){return num(r.n)})},
          {name:"失败(≥400)",type:"line",smooth:true,symbolSize:5,lineStyle:{color:"#f87171",width:1.8,type:"dashed"},itemStyle:{color:"#f87171"},data:rows.map(function(r){return num(r.err)})}]},true);
}
/* ---------------- 面板 8：错误 / 慢请求明细 ---------------- */
function recentRows(){
 var rows=arr(obj(SUM).recent),c=cutMs();
 rows=rows.filter(function(r){
  if(c&&new Date(String(r.ts).split(" ").join("T")).getTime()<c)return false;
  var code=num(r.code);
  if(REC.only==="err"&&code<400)return false;
  if(REC.only==="ok"&&code>=400)return false;
  if(REC.q){var q=REC.q.toLowerCase();
   if(String(r.path||"").toLowerCase().indexOf(q)<0&&String(r.code||"").indexOf(q)<0&&String(r.method||"").toLowerCase().indexOf(q)<0)return false}
  return true});
 var k=REC.sort,d=REC.dir;
 rows.sort(function(a,b){
  var x=a[k],y=b[k];
  if(k==="code"){x=num(x);y=num(y)}else{x=String(x||"");y=String(y||"")}
  return (x>y?1:x<y?-1:0)*d});
 return rows}
function clsCode(v){var n=num(v);return n>=400?"err":(n>=200&&n<300)?"ok":"warn"}
function renderRecent(){
 var rows=recentRows();
 $("recBody").innerHTML=rows.map(function(r){
  return "<tr><td class='mono'>"+esc(r.ts)+"</td><td>"+esc(r.method)+"</td><td class='mono' title='"+esc(r.path)+"'>"+esc(r.path)+
   "</td><td class='num "+clsCode(r.code)+"'>"+esc(r.code)+"</td></tr>"}).join("")||
  "<tr><td colspan='4' style='color:#8b93a3'>当前筛选下无请求记录</td></tr>";
 var slow=arr(obj(SUM).model_perf).slice();
 slow.sort(function(a,b){var x=a[SLOW.sort],y=b[SLOW.sort];
  if(SLOW.sort==="model")return String(x).localeCompare(String(y))*SLOW.dir;return (num(x)-num(y))*SLOW.dir});
 $("slowBody").innerHTML=slow.slice(0,10).map(function(r){
  return "<tr><td title='"+esc(r.model)+"' style='max-width:150px;overflow:hidden;text-overflow:ellipsis'>"+esc(r.model)+"</td>"+
   "<td class='num'>"+fmtMs(r.p90Ms)+"</td><td class='num'>"+fmtMs(r.medMs)+"</td><td class='num'>"+(+r.ctPerSec||0).toFixed(1)+"</td></tr>"}).join("")||
  "<tr><td colspan='4' style='color:#8b93a3'>暂无耗时样本</td></tr>";
 document.querySelectorAll("#recTable th").forEach(function(th){
  th.classList.remove("asc","desc");if(th.getAttribute("data-sk")===REC.sort)th.classList.add(REC.dir>0?"asc":"desc")});
 document.querySelectorAll("#slowTable th").forEach(function(th){
  th.classList.remove("asc","desc");if(th.getAttribute("data-sk")===SLOW.sort)th.classList.add(SLOW.dir>0?"asc":"desc")});
 var note=$("recNote");
 if(note)note.innerHTML="当前显示 <b>"+rows.length+"</b> 条（容器日志最近 15 条内筛选）；数据源 "+
  "<span class='mono'>docker logs litellm --tail 3000</span> + pool-requests.jsonl 耗时聚合。"}
/* ---------------- 用量详情表 ---------------- */
function renderUsage(d){
 var accts=arr(d.by_account),tot=Math.max(1,accts.reduce(function(s,a){return s+num(a.tt)},0));
 var tot24=Math.max(1,accts.reduce(function(s,a){return s+num(a.h24_tt)},0));
 $("acctShareChips").innerHTML=accts.map(function(a,i){
  return '<span class="chip"'+(a.historic?' title="该 key 在日志期内有调用，但已不在当前 .env（key 轮换）；邮箱从 .env 备份反查"':'')+'><i class="cdot" style="background:'+PAL[i%PAL.length]+'"></i>'+esc(acctName(a))+' · <b>'+fmtTok(a.tt)+
   '</b> tokens（'+(num(a.tt)*100/tot).toFixed(1)+'%）</span>'}).join("")||'<span class="chip">暂无请求日志</span>';
 var note=$("usageNote");
 var nHist=accts.filter(function(a){return a.historic}).length;
 if(note)note.innerHTML="累计 "+fmtInt(accts.reduce(function(s,a){return s+num(a.hits)},0))+" 请求 · "+
  fmtTok(accts.reduce(function(s,a){return s+num(a.tt)},0))+" tokens · 当前账号 <b>"+fmtInt(accts.length-nHist)+
  "</b> 个 + 历史 key <b>"+fmtInt(nHist)+"</b> 个（key 已轮换，邮箱从 .env 备份找回，不计入在池账号数）；"+
  "近 24h 窗口占累计 "+(accts.reduce(function(s,a){return s+num(a.h24_tt)},0)*100/tot).toFixed(1)+"%。"+
  "";
 $("acctShare").innerHTML=accts.map(function(a){
  var top3=arr(a.models).slice(0,3).map(function(m){return esc(m.model)+" · "+fmtInt(m.hits)+"次 · "+fmtTok(m.tokens)}).join("，")||"—";
  var stk=a.status_key||"unknown";
  var stcls=stk==="inpool"?"on":(stk==="disabled"?"off":(stk==="pending"?"pend":(stk==="historic"?"warn":"")));
  var stcell='<span class="badge '+stcls+'" title="'+esc(a.status||"")+'">'+esc(a.status||"—")+'</span>'+
   (a.upstream_state==="limited"?' <span class="badge off" title="上游用量窗口已打满（rate-limited）">上游受限</span>':'')+
   (a.upstream_state==="error"?' <span class="badge off" title="上游 usage 查询失败">用量查询失败</span>':'');
  return "<tr"+(a.historic?" class='hist'":"")+"><td>"+esc(acctName(a))+"</td><td>"+stcell+"</td><td class='num'>"+fmtInt(a.hits)+"</td>"+
   "<td class='num'>"+fmtTok(a.pt)+"</td><td class='num'>"+fmtTok(a.ct)+"</td><td class='num'><b>"+fmtTok(a.tt)+"</b></td>"+
   "<td class='num'>"+fmtTok(a.h24_tt)+"</td><td class='num'>"+num(a.h24_pct).toFixed(1)+"%</td><td>"+top3+"</td>"+
   "<td>"+(a.lastTs?fmtTs(a.lastTs):"—")+"</td></tr>"}).join("")||
  "<tr><td colspan='10' style='color:#8b93a3'>暂无请求日志</td></tr>"}
/* ---------------- 账号详情卡（三态：在池启用 / 已禁用 / 暂存待启用） ----------------
   2026-10-19 dashfix：旧实现把「是否在池」判成 config.yaml 里有没有 os.environ/OCG_KEY_n，
   DB 化后恒为 False → 6 张卡全部误标「已剔除 · 无限额部署」。现以控制面为准（三态）：
     inpool   = pool-config.json enabled=true 且 pool.db 有 deployment  →「在池 · 已启用」
     disabled = pool-config.json enabled=false                          →「已禁用 · 未入池」
     pending  = .env 里 OCG_PENDING_KEY_n（暂存待启用，未进控制面）        →「暂存 · 待启用」
------------------------------------------------------------------------------- */
function renderAcctDetail(){
 var el=$("acctDetail");if(!el)return;
 var usage=arr(obj(SUM).account_usage),usg={};
 arr(obj(SUM).by_account).forEach(function(a){usg[a.tail]=a});
 var sess={};if(SESSD)arr(SESSD.accounts).forEach(function(a){arr(a.tails&&a.tails.length?a.tails:[a.tail]).forEach(function(t){if(t)sess[t]=a})});
 var sum=$("acctSummary");
 if(sum){
  var nP=num(obj(SUM).accounts),nEn=num(obj(SUM).accounts_enabled),nDis=num(obj(SUM).accounts_disabled),
      nQ=num(obj(SUM).accounts_pending),nDep=num(obj(SUM).accounts_deployed),lim=num(obj(SUM).accounts_limited);
  sum.innerHTML='<span class="chip"><i class="cdot" style="background:#4ade80"></i>在池启用 <b>'+fmtInt(nEn)+'</b> / 号位 '+fmtInt(nP)+'</span>'+
   '<span class="chip"><i class="cdot" style="background:#fbbf24"></i>已禁用 <b>'+fmtInt(nDis)+'</b></span>'+
   '<span class="chip"><i class="cdot" style="background:#a78bfa"></i>暂存待启用 <b>'+fmtInt(nQ)+'</b>（不计入在池统计）</span>'+
   '<span class="chip">已部署 <b>'+fmtInt(nDep)+'</b> 槽位</span>'+
   '<span class="chip">在池受限 <b>'+fmtInt(lim)+'</b></span>';
 }
/* 2026-10-19（task-9）：限额环形仪表（v8 f75582a 的 84px 内联 SVG 甜甜圈，v12 被删）。
   只用 SVG，不给每个窗口挂 ECharts（30 卡 × 3 窗口 = 90 实例太重）。 */
 function donutSvg(p,color){
  var r=34,C=(2*Math.PI*r).toFixed(2),v=(Math.min(100,Math.max(0,p))/100*C).toFixed(2);
  return '<svg class="donut" viewBox="0 0 84 84" role="img" aria-label="'+Math.round(p)+'%">'+
   '<circle class="track" cx="42" cy="42" r="'+r+'"></circle>'+
   '<circle class="val" cx="42" cy="42" r="'+r+'" stroke="'+color+'" stroke-dasharray="'+v+' '+C+'" transform="rotate(-90 42 42)"></circle>'+
   '<text x="42" y="47">'+Math.round(p)+'%</text></svg>';
 }
 function card(a){
  var label=a.label||a.key,u=usg[a.tail]||null,s=sess[a.tail]||null;
  var pst=a.poolState||"disabled",isPend=pst==="pending",isDis=pst==="disabled",dep=num(a.deployments);
  /* 2026-10-20：徽标文案收紧（长文案把邮箱挤成省略号）。语义完整的说法全部放进 title。 */
  var pool=isPend?'<span class="badge pend" title="暂存待启用：Key 仅存放在服务器 .env（600 权限），未写入控制面 pool-config.json；不计入在池额度 / 路由统计">暂存</span>':
   isDis?'<span class="badge off" title="已禁用：控制面 pool-config.json 中该槽位 enabled=false，未入池、不参与路由">已禁用</span>':
   (dep>0?'<span class="badge on" title="在池 · 已启用：控制面 enabled=true 且 pool.db 中存在 deployment">在池</span>':
    '<span class="badge warn" title="已启用 · 无部署：控制面 enabled=true 但 pool.db 无 deployment（异常态）">无部署</span>');
  var stCls=a.state==="ok"?"on":(a.state==="limited"?"off":"bad");
  var limList=arr(a.limited).join("/");
  /* 徽标只放短词（长文案会把邮箱挤成省略号），窗口明细进 title */
  var stTxt=a.state==="limited"?"受限":a.state==="error"?"查询失败":"正常";
  var stTip=a.state==="limited"?("受限：上游 "+limList+" 窗口已打满（rate-limited），到重置时间自动恢复"):
    a.state==="error"?"查询失败：上游 usage 接口报错（形状见上方「上游 usage」行）":
    "正常：三个限额窗口都可用";
  var wins=[["Rolling","rolling"],["Weekly","weekly"],["Monthly","monthly"]].map(function(w){
   var v=obj(obj(a.windows)[w[1]]);
   if(v.percent==null)return '<div class="win"><div class="wn">'+w[0]+'</div>'+
    '<div class="donut-na" title="上游未返回该窗口的限额数据"><span>—</span></div>'+
    '<div class="st dimv">无数据</div><div class="rst"></div></div>';
   var p=Math.max(0,Math.min(100,num(v.percent))),lim=v.status==="rate-limited";
   var rms=v.resetsAt?new Date(v.resetsAt).getTime():0;   /* v.resetsAt 是 ISO 字符串，fmtTs* 收 epoch ms */
   var abs=rms?fmtTs(rms):"",short=rms?fmtTsShort(rms):"";
   return '<div class="win"><div class="wn">'+w[0]+'</div>'+
    donutSvg(p,lim?"#f87171":p>=70?"#fbbf24":"#4ade80")+
    '<div class="st '+(lim?"err":"ok")+'">'+esc(v.status||"unknown")+'</div>'+
    '<div class="rst" title="'+(abs?esc(abs)+" 重置":"")+'">'+(short?esc(short)+" · "+rel(v.resetsAt):"—")+'</div></div>'}).join("");
  var chips=u&&arr(u.models).length?arr(u.models).slice(0,4).map(function(m,i){
   return '<span class="chip" title="'+esc(m.model)+'：'+fmtInt(m.hits)+' 次 · '+fmtTok(m.tokens)+' tokens"><i class="cdot" style="background:'+
    PAL[i%PAL.length]+'"></i>'+esc(m.model)+' · '+fmtInt(m.hits)+'次 · '+fmtTok(m.tokens)+'</span>'}).join("")
   :'<span class="chip">日志期内无请求</span>';
  var st=function(v,k,c){return '<div class="stat"><div class="sv '+(c||"")+'">'+v+'</div><div class="sk">'+k+'</div></div>'};
  return '<div class="det'+(isPend?' pend':isDis?' dis':'')+'"><div class="det-head"><span class="det-name" title="'+esc(label)+'">'+esc(label)+'</span>'+
   (a.src==="placeholder"?'<span class="badge off" title="邮箱待补：可在 /opt/pool-dash/account-emails.json 填写 KEY→邮箱 映射">邮箱待补</span>':'')+
   pool+
   '<span class="badge '+stCls+'" style="margin-left:auto" title="'+stTip+'">'+stTxt+'</span></div>'+
   '<div class="det-meta"><span>上游 usage：'+(a.ok?"成功":"失败 "+esc(a.error||""))+'</span>'+
   (u?'<span>日志期内 '+fmtInt(u.hits)+' 请求 · '+fmtTok(u.tt)+' tokens</span>':'<span>日志期内无请求</span>')+
   (s?'<span>对齐会话 '+arr(s.sessions).length+' 个</span>':'')+
   (isPend?'<span>未入池</span>':'<span>部署 '+fmtInt(dep)+' 条</span>')+'</div>'+
   '<div class="wins">'+wins+'</div><div class="statgrid">'+
   st(u?fmtInt(u.hits):"0","池内请求")+st(fmtTok(u?u.tt:0),"总 Token")+st(fmtTok(u?u.pt:0),"Prompt")+st(fmtTok(u?u.ct:0),"Completion")+
   st(s?arr(s.sessions).length:"—","服务会话")+'<div class="stat" title="'+(u&&u.lastTs?fmtTs(u.lastTs):"")+'"><div class="sv sm">'+
   (u&&u.lastTs?fmtTsShort(u.lastTs):"—")+'</div><div class="sk">最近活动</div></div></div>'+
   '<div class="chips" style="margin-top:8px">'+chips+'</div>'+
   (isPend?'<div class="note" style="margin-top:6px">暂存待启用：Key 仅存放在服务器 <span class="mono">.env</span>（600 权限），未写入 <span class="mono">pool-config.json</span>，<b>不计入</b>在池账号数 / 额度 / 路由统计；启用后自动进入上区。</div>':'')+
   '</div>'}
 var poolRows=usage.filter(function(a){return a.poolState!=="pending"});
 var pendRows=usage.filter(function(a){return a.poolState==="pending"});
 el.innerHTML=poolRows.map(card).join("")||
  '<div class="det" style="color:#8b93a3">无账号（/opt/litellm/.env 中没有配置号池 Key）</div>';
 /* 2026-10-19（task-9）：pending=0 时（新账号启用后就是这种形态）「暂存待启用」整块收起，
    不留空标题 / 空壳卡片；有暂存账号时照旧渲染。 */
 var pel=$("acctPending");
 if(pel){
  pel.innerHTML=pendRows.length?pendRows.map(card).join(""):"";
  pel.style.display=pendRows.length?"flex":"none";   /* flex = #acctPending 的 flex-wrap 布局 */
 }
 var phd=$("acctPendingHd");if(phd)phd.style.display=pendRows.length?"":"none"}

/* ---------------- 账号 · 会话分配明细 ---------------- */
async function loadAcctSessions(){
 try{
  var d=await (await fetch("api/account-sessions",{cache:"no-store"})).json();
  SESSD=d;renderAcctDetail();
  var el=$("acctSessions");if(!el)return;
  var tag=$("sessTag");if(tag)tag.textContent=fmtInt(d.sessionsTotal)+" 会话 / "+fmtInt(d.logRows)+" 条请求日志";
  var accts=arr(d.accounts);
  if(!accts.length){el.innerHTML='<div style="color:#8b93a3;font-size:12px">暂无请求日志（callback 自 2026-09-26 起生效，产生号池调用后此处出现账号分配）</div>';return}
  el.innerHTML=accts.map(function(a,ai){
   var total=num(a.tokensTotal),maxTok=Math.max(0,arr(a.models).map(function(x){return num(x.tokens)}).reduce(function(s,v){return Math.max(s,v)},0));
   var models=arr(a.models).map(function(m,i){
    var pct=total?Math.round(num(m.tokens)*100/total):0,top=num(m.tokens)>0&&num(m.tokens)>=maxTok;
    return '<span class="chip" title="'+esc(m.model)+'：'+fmtInt(m.hits)+' 次请求 · '+fmtTok(m.tokens)+' tokens（'+pct+'%）">'+
     '<i class="cdot" style="background:'+PAL[i%PAL.length]+'"></i>'+(top?'<b style="color:#fbbf24">▲</b> ':'')+
     esc(m.model)+' · '+fmtInt(m.hits)+' 次 · '+fmtTok(m.tokens)+' ('+pct+'%)</span>'}).join(" ")||'<span class="chip">无模型记录</span>';
   var rows=arr(a.sessions).map(function(s){
    var badge=s.ext?'<span class="badge off" style="margin-right:6px">外网</span>':"";
    return "<tr><td><div style='font-weight:600'>"+badge+esc(s.title)+"</div><div style='font-size:11px;color:#8b93a3'>"+esc(s.firstMsg||"-")+"</div></td>"+
     "<td>"+esc(s.model||"-")+"</td><td>"+fmtTs(s.createdAt)+"</td><td>"+fmtTs(s.lastActive)+"</td><td>"+fmtInt(s.hits)+
     (s.ext?"<div style='font-size:11px;color:#8b93a3'>"+fmtTok(s.tokens)+" tok</div>":"")+"</td></tr>"}).join("")||
    "<tr><td colspan='5' style='color:#8b93a3'>日志期内无对齐请求</td></tr>";
   return '<div class="prov"><div class="ph2"><span class="papi">#'+(ai+1)+'</span><span class="pname">'+esc(acctName(a))+'</span>'+(a.historic?'<span class="badge off">已下线账号</span>':'')+
    '<span class="chip">请求 <b>'+fmtInt(a.hits)+'</b></span><span class="chip">共 <b>'+fmtTok(total)+'</b> tokens</span>'+
    (num(a.refs)>1?'<span class="chip" title="同一邮箱在日志中有多种 key 写法，已合并">合并'+fmtInt(a.refs)+'种记录</span>':'')+
    '<span class="chip">▲ = 额度消耗主力模型</span></div>'+
    '<div class="chips" style="margin-bottom:6px">'+models+'</div>'+
    '<div class="scroll" style="max-height:260px"><table><thead><tr><th>会话（标题 / 首条消息）</th><th>模型</th><th>创建时间</th><th>最后活跃</th><th>命中</th></tr></thead><tbody>'+
    rows+'</tbody></table></div></div>'}).join("")+
   (arr(d.orphans).length?'<div class="prov"><div class="ph2"><span class="pname" style="color:#fbbf24">未归属会话</span>'+
    '<span class="chip">共 <b>'+fmtInt(arr(d.orphans).length)+'</b> 个</span>'+
    arr(d.orphansByModel).slice(0,5).map(function(m){return '<span class="chip" title="该模型在日志期内没有对齐到任何账号">'+esc(m.model||"?")+' × '+fmtInt(m.count)+'</span>'}).join("")+
    '</div><div style="font-size:11.5px;color:#8b93a3;margin:2px 0 8px">未归属 = 请求日志（含归档，09-26 起）中没有该会话的号池请求（多为历史会话 / 非池模型），或请求缺少 keyRef 无法归属；括号内为判定原因。</div>'+
    arr(d.orphans).map(function(s){return "<div style='font-size:11.5px;color:#8b93a3;margin:3px 0'>"+esc(s.title)+" — "+esc(s.firstMsg||"")+
     "（"+esc(s.model||"?")+" · 活跃 "+fmtTs(s.lastActive)+(s.reason?' · <span style="color:#fbbf24">'+(s.reason==="no-keyref"?"请求缺 keyRef":"日志中无该会话请求")+'</span>':'')+"）</div>"}).join("")+"</div>":"");
 }catch(e){$("acctSessions").innerHTML='<div class="err" style="font-size:12px">加载失败: '+esc(e)+'</div>'}
}
/* ---------------- 路由观测面 P1–P6（2026-10-20 重建） ---------------- */
function rtPct(v,d){return v==null?"—":(num(v)*100).toFixed(d==null?1:d)+"%"}
function fmtAge(s){if(s==null)return "—";s=num(s);if(s<90)return Math.round(s)+" 秒前";if(s<5400)return (s/60).toFixed(1)+" 分钟前";if(s<172800)return (s/3600).toFixed(1)+" 小时前";return (s/86400).toFixed(1)+" 天前"}
/* ---- 烧量路由图表增强（v14 2026-10-27）---- */
function rtPp(v){var d=num(v);return (d>0?"+":"")+d.toFixed(1)+"pp"}
function rtSev(v){var a=Math.abs(num(v));return a>=20?"#f87171":a>=10?"#fbbf24":"#4ade80"}
function rtRemPct(v){var n=num(v);return (n%1?n.toFixed(1):n.toFixed(0))+"%";}
function rtMeter(pct,color,h){var p=Math.max(0,Math.min(100,num(pct)));
 return '<div class="rt-bar"'+(h?' style="height:'+h+'px"':'')+'><i style="width:'+p+'%;background:'+(color||"#60a5fa")+'"></i></div>'}
function rtChip(t,color,title){return '<span class="chip"'+(color?' style="border-color:'+color+';color:'+color+'"':'')+
 (title?' title="'+esc(title)+'"':'')+'>'+t+'</span>'}
function renderP2(){
 var el=$("chart-p2");if(!el||!window.echarts||!el.offsetWidth)return;
 var c=INST["chart-p2"]||(INST["chart-p2"]=echarts.init(el));
 var p2=obj(obj(obj(RT).panels).p2_share);
 var rows=arr(p2.accounts).slice();
 rows.sort(function(a,b){return (num(b.actual)-num(a.actual))||(num(b.target)-num(a.target))});
 rows=rows.filter(function(r){return num(r.actual)>0||num(r.target)>0});
 var maxp=10;rows.forEach(function(r){maxp=Math.max(maxp,num(r.target)*100,num(r.actual)*100)});
 var xmax=Math.min(100,Math.ceil(maxp/10)*10);
 var names=rows.map(function(r){return String(r.key_ref||"")});
 var LIGHT=(document.documentElement.getAttribute("data-theme")==="light");
 var C_TXT=LIGHT?"#334155":"#c9d1e0",C_DIM=LIGHT?"#4b5b74":"#8b93a3",C_LINE=LIGHT?"#c3cfe2":"#2c3342",C_SPLIT=LIGHT?"#e3e9f3":"#1f242e";
 var devColor=function(v){var a=Math.abs(num(v));return a>=20?"#f87171":a>=10?"#fbbf24":C_TXT};
 c.setOption({animationDuration:280,
  tooltip:tipAxis({formatter:function(ps){if(!ps||!ps.length)return "";var r=rows[ps[0].dataIndex]||{};
   var d=num(r.dev_pp),pv=num(r.dev_prev_pp);
   return "<b>"+esc(r.key_ref)+"</b><br/>目标 <b>"+rtPct(r.target)+"</b> · 实际 <b>"+rtPct(r.actual)+"</b>"+
    "<br/>偏差 <b style='color:"+rtSev(d)+"'>"+rtPp(d)+"</b>"+
    "<br/>上一窗偏差 "+rtPp(pv)}}),
  legend:legendOpt({data:["实际份额","目标份额"],textStyle:{color:C_DIM,fontSize:11}}),
  grid:{left:212,right:132,top:30,bottom:24},
  xAxis:{type:"value",min:0,max:xmax,axisLine:{lineStyle:{color:C_LINE}},axisTick:{show:false},
   axisLabel:{color:C_DIM,fontSize:10.5,formatter:function(v){return v+"%"}},
   splitLine:{lineStyle:{color:C_SPLIT}}},
  yAxis:{type:"category",inverse:true,data:names,axisLine:{lineStyle:{color:C_LINE}},axisTick:{show:false},
   axisLabel:{color:LIGHT?"#233246":"#c9d1e0",fontSize:10.5,width:198,overflow:"truncate",formatter:function(v,i){
    var r=rows[i]||{};return (Math.abs(num(r.dev_pp))>=10?"⚠ ":"")+v}}},
  series:[
   {name:"实际份额",type:"bar",barWidth:13,z:3,itemStyle:{color:LIGHT?"#22c55e":"#4ade80",borderRadius:[0,3,3,0]},
    label:{show:true,position:"right",distance:7,fontSize:10.5,color:C_TXT,
     formatter:function(p){var r=rows[p.dataIndex]||{};
      return rtPct(r.actual)+"  "+rtPp(num(r.dev_pp))}},
    data:rows.map(function(r){var d=num(r.dev_pp);
      return {value:+(num(r.actual)*100).toFixed(2),label:{color:devColor(d)}}})},
   {name:"目标份额",type:"scatter",symbol:"rect",symbolSize:[4,16],z:6,itemStyle:{color:"#60a5fa"},
    data:rows.map(function(r,i){return {value:[+(num(r.target)*100).toFixed(2),i],
      itemStyle:{opacity:num(r.target)>0?1:0}}})}]},true);
}

function renderP4(){
 var el=$("chart-p4");if(!el||!window.echarts||!el.offsetWidth)return;
 var c=INST["chart-p4"]||(INST["chart-p4"]=echarts.init(el));
 var p4=obj(obj(obj(RT).panels).p4_class),dist=obj(p4.dist);
 var META={H:{c:"#4ade80",n:"H 热会话"},M:{c:"#60a5fa",n:"M 中温"},L:{c:"#fbbf24",n:"L 冷会话"},ext:{c:"#a78bfa",n:"ext 外网"}};
 var order=["H","M","L","ext"];
 var keys=Object.keys(dist).sort(function(a,b){
  var ia=order.indexOf(a),ib=order.indexOf(b);return (ia<0?99:ia)-(ib<0?99:ib)});
 var LIGHT=(document.documentElement.getAttribute("data-theme")==="light");
 var C_TXT=LIGHT?"#0b1420":"#e6e9ef",C_DIM=LIGHT?"#3b4a63":"#c9d1e0",C_SUB=LIGHT?"#5a6b86":"#8b93a3",C_BORDER=LIGHT?"#ffffff":"#161a21";
 var total=keys.reduce(function(s,k){return s+num(dist[k])},0);
 var data=keys.map(function(k){var m=META[k]||{c:PAL[keys.indexOf(k)%PAL.length],n:k};
  return {name:m.n,value:num(dist[k]),itemStyle:{color:m.c}}});
 c.setOption({animationDuration:280,
  tooltip:tipItem({formatter:function(p){return "<b>"+esc(p.name)+"</b><br/>"+fmtInt(p.value)+" 会话 · "+
    (total?num(p.value)*100/total:0).toFixed(1)+"%"}}),
  legend:{show:true,orient:"vertical",right:2,top:"middle",itemWidth:9,itemHeight:9,icon:"circle",
   textStyle:{color:C_DIM,fontSize:11},legendHoverLink:false,
   formatter:function(name){var d=data.filter(function(x){return x.name===name})[0]||{value:0};
    return name+"  "+fmtInt(d.value)+" ("+(total?num(d.value)*100/total:0).toFixed(1)+"%)"}},
  title:{text:fmtInt(total),subtext:"会话合计",left:"28%",top:"41%",textAlign:"center",
   textStyle:{color:C_TXT,fontSize:19,fontWeight:700},subtextStyle:{color:C_SUB,fontSize:10.5}},
  series:[{type:"pie",radius:["47%","68%"],center:["28%","52%"],label:{show:false},labelLine:{show:false},
   emphasis:{scale:true,scaleSize:3},itemStyle:{borderColor:C_BORDER,borderWidth:2},data:data}]},true);
}

function renderRouting(rt){
 RT=rt||RT;
 var NR='<div class="note">数据未就绪（routing-share.json / shadow-decisions.jsonl 尚未生成）</div>';
 var pids=["panelP1","panelP2","panelP3456"];
 var bar=$("alertBar");
 if(!rt){if(bar)bar.innerHTML=NR;pids.forEach(function(id){var e=$(id);if(e)e.innerHTML=NR});return}
 var P=obj(rt.panels),alerts=arr(rt.alerts),firing=alerts.filter(function(a){return a.firing});
 disposeCharts(["chart-p2","chart-p4"]);
 if(bar){
  var quiet=alerts.filter(function(a){return !a.firing}).map(function(a){return a.id}).join(" ");
  var crit=firing.filter(function(a){return a.level==="crit"}).length;
  bar.innerHTML='<div class="chips" style="margin-bottom:4px">'+
   '<span class="chip"'+(firing.length?' style="border-color:#f87171;color:#f87171"':'')+'>触发 <b>'+firing.length+'</b> / '+alerts.length+'</span>'+
   (firing.length?'<span class="chip">严重 <b style="color:#f87171">'+crit+'</b> · 警告 <b style="color:#fbbf24">'+(firing.length-crit)+'</b></span>':'')+
   (quiet?'<span class="chip">未触发 '+esc(quiet)+'</span>':'')+'</div>'+
   (firing.length?firing.map(function(a){
     var col=a.level==="crit"?"#f87171":"#fbbf24";
     return '<div class="rt-alert" style="border-color:'+col+';border-left-width:3px">'+
      '<b style="color:'+col+'">['+esc(a.id)+'] '+esc(a.name)+'</b>'+
      '<span class="rt-alert-d">'+esc(a.detail)+'</span>'+
      '<span class="rt-alert-e">判定：'+esc(a.expr)+'</span></div>'}).join("")
    :'<div class="rt-ok">✓ '+alerts.length+' 条告警均未触发</div>');
 }
 var p1=obj(P.p1_burn),e1=$("panelP1");
 if(e1){
  if(!p1.ready)e1.innerHTML=NR;
  else{
   var rank={danger:0,idle:1,"no-sample":2,ok:3};
   var rows=arr(p1.rows).slice();
   rows.sort(function(a,b){
    var ra=(rank[a.state]==null?9:rank[a.state]),rb=(rank[b.state]==null?9:rank[b.state]);
    return (ra-rb)||(num(a.rem_pct)-num(b.rem_pct))});
   var cnt={danger:0,idle:0,"no-sample":0,ok:0},sumBurn=0,sumPace=0;
   rows.forEach(function(r){cnt[r.state]=(cnt[r.state]||0)+1;
    sumBurn+=num(r.burn_usd_day);sumPace+=num(r.pace_usd_day)});
   e1.innerHTML='<div class="chips" style="margin-bottom:7px">'+
    '<span class="chip" style="border-color:#f87171;color:#f87171">打穿 <b>'+cnt.danger+'</b></span>'+
    '<span class="chip" style="border-color:#fbbf24;color:#fbbf24">闲置 <b>'+cnt.idle+'</b></span>'+
    '<span class="chip">无样本 <b>'+cnt["no-sample"]+'</b></span>'+
    '<span class="chip">实测烧量合计 <b>$'+sumBurn.toFixed(2)+'/天</b></span>'+
    '<span class="chip">pace 合计 <b>$'+sumPace.toFixed(2)+'/天</b></span></div>'+
    '<div class="tblwrap"><table><thead><tr><th>账号</th><th>剩余</th>'+
    '<th class="num">重置</th><th class="num">耗尽</th><th class="num">实测 $/天</th>'+
    '<th class="num">pace $/天</th><th>状态</th></tr></thead><tbody>'+
    rows.map(function(x){
     var rem=Math.max(0,Math.min(100,num(x.rem_pct)));
     var bc=rem<20?"#f87171":(rem<50?"#fbbf24":"#4ade80");
     var st=x.state==="danger"?'<span class="chip" style="border-color:#f87171;color:#f87171">打穿</span>':
       x.state==="idle"?'<span class="chip" style="border-color:#fbbf24;color:#fbbf24">闲置</span>':
       x.state==="no-sample"?'<span class="chip" title="近 24h 无该账号请求样本，无法外推 T_exhaust">无样本</span>':
       '<span class="chip" style="border-color:#4ade80;color:#4ade80">正常</span>';
     var ex=x.t_exhaust_s==null?'<span class="rt-mini" title="无有效烧量样本，不做外推">—</span>':fmtDur(x.t_exhaust_s);
     var ratio=(x.state==="danger")?100:((x.t_exhaust_s!=null&&num(x.t_reset_s)>0)?Math.min(100,num(x.t_exhaust_s)*100/num(x.t_reset_s)):null);
     var rbar=ratio==null?'':'<div class="rt-bar" title="耗尽 ÷ 重置 时间比（满=先打穿）"><i style="width:'+ratio+'%;background:'+(ratio<100?"#f87171":"#4ade80")+'"></i></div>';
     var bs=num(x.burn_samples);
     var burn=bs>0?'$'+num(x.burn_usd_day).toFixed(2)+'<div class="rt-mini">样本 '+fmtInt(bs)+' 条</div>':
       '<span class="rt-mini" title="近 24h 无请求样本，不做外推">—</span>';
     return '<tr'+(x.state==="danger"?' class="rt-danger"':'')+'>'+
      '<td class="mono">'+esc(x.key_ref)+'</td>'+
      '<td><div class="meter"><i style="width:'+rem+'%;background:'+bc+'"></i></div>'+
      '<span class="rt-mini">'+(rem%1?rem.toFixed(1):rem.toFixed(0))+'%</span></td>'+
      '<td class="num">'+fmtDur(x.t_reset_s)+'</td><td class="num">'+ex+rbar+'</td>'+
      '<td class="num">'+burn+'</td><td class="num">$'+num(x.pace_usd_day).toFixed(2)+'</td>'+
      '<td>'+st+'</td></tr>'}).join("")+
    '</tbody></table></div>'+
    '<div class="note">剩余% 取上游 usage 快照；<b>耗尽</b> = 剩余额度 ÷ 近 24h 实测烧量外推，'+
    '下方细条为「耗尽 ÷ 重置」时间比（红=先打穿）；<b>实测 $/天</b> 下方标注 24h 样本数；'+
    '无有效样本时显示 — 且不参与 A1/A2 判定。</div>';
  }
 }
 var p2=obj(P.p2_share),e2=$("panelP2");
 if(e2){
  if(!p2.ready)e2.innerHTML=NR;
  else{
   var a2=arr(p2.accounts),bad=a2.filter(function(a){return Math.abs(num(a.dev_pp))>10});
   var skip=a2.filter(function(a){return num(a.actual)<=0&&num(a.target)<=0}).length;
   var sumT=0,sumA=0,maxDev=0;
   a2.forEach(function(a){sumT+=num(a.target);sumA+=num(a.actual);
    maxDev=Math.max(maxDev,Math.abs(num(a.dev_pp)))});
   e2.innerHTML='<div class="chart" id="chart-p2" data-chart="1"></div>'+
    '<div class="chips">'+
    '<span class="chip">窗口 <b>'+Math.round(num(p2.window_s)/60)+' min</b></span>'+
    '<span class="chip">目标覆盖 <b>'+rtPct(sumT)+'</b></span>'+
    '<span class="chip">实际覆盖 <b>'+rtPct(sumA)+'</b></span>'+
    '<span class="chip"'+(bad.length?' style="border-color:#f87171;color:#f87171"':'')+'>偏差 &gt;10pp <b>'+bad.length+'</b> 个</span>'+
    '<span class="chip">最大偏差 <b>'+maxDev.toFixed(1)+' pp</b></span>'+
    (skip?'<span class="chip">已省略 <b>'+skip+'</b> 个目标/实际均为 0 的账号</span>':'')+
    (p2.has_actual?'':'<span class="chip" style="border-color:#fbbf24;color:#fbbf24">近 1h 无请求样本</span>')+
    (p2.has_prev?'':'<span class="chip" style="border-color:#fbbf24;color:#fbbf24">上一窗无样本 · A3 不判定</span>')+
    '</div><div class="note">绿条=实际份额，蓝竖线=目标份额；条尾=实际% 与偏差（黄≥10pp / 红≥20pp）；账号前 ⚠=偏差≥10pp。</div>';
   renderP2();
  }
 }
 var p3=obj(P.p3_shadow),p4=obj(P.p4_class),p5=obj(P.p5_repin),p6=obj(P.p6_health),e3456=$("panelP3456");
 if(e3456){
  var sec={shadow:"",klass:"",repin:"",health:""};
  /* 影子命中率 */
  if(!p3.ready)sec.shadow=NR;
  else{
   var hr=num(p3.hit_rate),hcol=hr>=0.99?"#4ade80":(hr>=0.95?"#fbbf24":"#f87171");
   sec.shadow='<div class="statgrid">'+
    '<div class="stat"><div class="sv" style="color:'+hcol+'">'+rtPct(hr)+'</div><div class="sk">影子命中率</div></div>'+
    '<div class="stat"><div class="sv">'+fmtInt(p3.matched)+' / '+fmtInt(p3.comparable)+'</div><div class="sk">命中 / 可比</div></div>'+
    '<div class="stat"><div class="sv">'+fmtInt(p3.records)+'</div><div class="sk">决策记录</div></div></div>'+
    rtMeter(hr*100,hcol,7)+
    '<div class="note" style="margin-top:4px">阈值 ≥99% 说明 key_want 与 key_actual 基本一致；&lt;95% 不宜切入 canary。</div>';
  }
  /* 会话分档分布 */
  if(!p4.ready)sec.klass=NR;
  else sec.klass='<div class="chart" id="chart-p4" data-chart="1" style="height:196px"></div>'+
   '<div class="note">H/M/L = 会话热度分档，ext = 外网来源；图例含数量与占比。</div>';
  /* 迁移与缓存 */
  if(!p5.ready)sec.repin=NR;
  else{
   var repinPct=(num(p5.budget_per_min)>0)?num(p5.repin_last_min)*100/num(p5.budget_per_min):0;
   var coldPct=num(p5.cold_budget_pct),cachePct=num(p5.cache_rate_1h)*100;
   sec.repin='<div class="statgrid">'+
    '<div class="stat"><div class="sv">'+fmtInt(p5.repin_last_min)+' / '+
     (p5.budget_per_min==null?"—":fmtInt(p5.budget_per_min))+'</div><div class="sk">重钉 次/min</div>'+
     rtMeter(repinPct,"#60a5fa")+'</div>'+
    '<div class="stat"><div class="sv'+(coldPct>=80?" err":"")+'">'+
     (p5.cold_budget_pct==null?"—":coldPct.toFixed(0)+"%")+'</div>'+
     '<div class="sk">冷预算 '+fmtInt(p5.cold_last_5h)+' / '+
     (p5.cold_budget_per_5h==null?"—":fmtInt(p5.cold_budget_per_5h))+'（5h）</div>'+
     rtMeter(coldPct,coldPct>=80?"#f87171":"#4ade80")+'</div>'+
    '<div class="stat"><div class="sv">'+rtPct(p5.cache_rate_1h)+'</div>'+
     '<div class="sk">warm 1h · 基线 '+rtPct(p5.cache_baseline_24h)+'</div>'+
     rtMeter(cachePct,"#22d3ee")+'</div></div>'+
    '<div class="note">'+esc(p5.note||"")+'（去重 '+esc(p5.cold_dedup||"—")+'；5h 建议行 '+
    fmtInt(p5.suggest_rows_5h)+'）</div>';
  }
  /* 门禁与健康 */
  if(!p6.ready)sec.health=NR;
  else{
   var p1map={};arr(obj(P.p1_burn).rows).forEach(function(r){p1map[r.key_ref]=r});
   var ageCol=function(x){var v=num(x);return v>1800?"#f87171":(v>900?"#fbbf24":"#4ade80")};
   var aChip=function(k){
    var r=p1map[k]||{},rem=r.rem_pct;
    var c=rem==null?"#8b93a3":(num(rem)<20?"#f87171":(num(rem)<50?"#fbbf24":"#4ade80"));
    return '<span class="chip" title="'+esc(k)+'：剩余 '+rtRemPct(rem)+' · 重置 '+fmtDur(r.t_reset_s)+'">'+
     '<span style="color:'+c+'">●</span> '+esc(k)+
     ' <b style="color:'+c+'">'+rtRemPct(rem)+'</b></span>'};
   var gated=arr(p6.gated),exp=arr(p6.expiring_set),errs=arr(p6.errors);
   var statusOk=String(p6.status)==="ok";
   sec.health='<div class="rt-stats">'+
    '<div class="rt-stat"><div class="sv" style="color:'+(statusOk?"#4ade80":"#f87171")+'">'+
     esc(String(p6.status))+'</div><div class="sk">引擎状态 · '+esc(String(p6.mode))+' 模式</div></div>'+
    '<div class="rt-stat"><div class="sv" style="color:'+ageCol(p6.share_age_s)+'">'+
     fmtAge(p6.share_age_s)+'</div><div class="sk">routing-share 快照（&gt;30min 触发 A4）</div></div>'+
    '<div class="rt-stat"><div class="sv" style="color:'+ageCol(p6.usage_age_s)+'">'+
     fmtAge(p6.usage_age_s)+'</div><div class="sk">usage 快照 · 失败号 '+
     (arr(p6.usage_fail_accounts).length?arr(p6.usage_fail_accounts).map(esc).join(" "):"无")+'</div></div>'+
    '<div class="rt-stat"><div class="sv" style="color:'+(num(p6.usage_failures_10min)>=3?"#f87171":"#4ade80")+'">'+
     fmtInt(p6.usage_failures_10min)+'</div><div class="sk">usage 失败 /10min</div></div>'+
    '</div>'+
    '<div class="chips" style="margin-top:5px">'+
     (gated.length?gated.map(aChip).join(" "):'<span class="rt-mini">门禁：无</span>')+
     (arr(p6.gate_added).length?' '+arr(p6.gate_added).map(function(k){return rtChip("+ "+esc(k),"#f87171","本窗新增门禁")}).join(" "):'')+
     (arr(p6.gate_removed).length?' '+arr(p6.gate_removed).map(function(k){return rtChip("- "+esc(k),"#4ade80","本窗解除门禁")}).join(" "):'')+
    '</div>'+
    '<div class="chips" style="margin-top:4px">'+
     (exp.length?exp.map(aChip).join(" "):'<span class="rt-mini">临期集合 E：无</span>')+
    '</div>'+
    '<div class="chips" style="margin-top:4px">'+
     (errs.length?errs.map(function(c){return rtChip(esc(c),"#fbbf24")}).join(" "):'<span class="rt-mini">引擎告警码：无</span>')+
     (p6.d7?'<span class="chip" style="border-color:#f87171;color:#f87171">D7 额度不足 · 溢出派号生效</span>':
      '<span class="chip" style="border-color:#4ade80;color:#4ade80">D7 额度充足</span>')+
     (p6.distress?'<span class="chip" style="border-color:#fbbf24;color:#fbbf24">distress on</span>':'')+
    '</div>'+
    '<div class="note">门禁 = 当前被压制（rate-limited / 额度不足）的账号，chip 内为剩余额度；'+
    '临期 = 即将重置且余量低的账号；快照 age 超 30min 触发 A4；D7 = Σpace &lt; 需求。</div>';
  }
  e3456.innerHTML='<div class="rt-blocks">'+
   '<div class="rt-block"><h4>影子命中率</h4>'+sec.shadow+'</div>'+
   '<div class="rt-block"><h4>分档分布</h4>'+sec.klass+'</div>'+
   '<div class="rt-block"><h4>迁移与缓存</h4>'+sec.repin+'</div>'+
   '<div class="rt-block"><h4>门禁与健康</h4>'+sec.health+'</div>'+
   '</div>';
  if(p4.ready)renderP4();
 }
}

/* ---------------- 主刷新 ---------------- */
async function load(){
 try{
  var d=await (await fetch("api/summary?range="+encodeURIComponent(STATE.range),{cache:"no-store"})).json();
  SUM=d;
  var alive=d.health==="alive";
  $("healthDot").style.background=alive?"#4ade80":"#f87171";
  $("healthTxt").textContent=alive?"LiteLLM alive":"LiteLLM down";
  renderKpis();renderModelBar();renderTimeline();renderAcctModel();renderQuota();
  renderSessTree();renderLatency();renderSrc();renderHours();
  renderRecent();renderUsage(d);renderAcctDetail();renderRouting(d.routing);
  $("refresh").textContent="每次打开即时拉取 · 自动刷新"+((STATE.auto?"（"+Math.round(STATE.iv/1000)+"s）":"（已关闭）"))+
   " · 更新于 "+new Date().toLocaleTimeString("zh-CN")+" · 全部图表本地 ECharts 渲染，无外网依赖";
 }catch(e){$("refresh").textContent="加载失败: "+e}
}
/* ---------------- 交互：排序 / 刷新 ---------------- */
function resizeAll(){
 Object.keys(INST).forEach(function(id){
  var el=$(id);if(!el||!el.offsetWidth)return;
  try{INST[id].resize()}catch(e){}})}
/* 路由面板每次刷新会重建 DOM，必须先释放旧实例，避免复用到已脱离文档的容器 */
function disposeCharts(ids){
 ids.forEach(function(id){if(INST[id]){try{INST[id].dispose()}catch(e){};delete INST[id]}})}
function bindTables(){
 document.querySelectorAll("table.sortable th").forEach(function(th){
  th.addEventListener("click",function(){
   var key=th.getAttribute("data-sk");if(!key)return;
   if(th.closest("#recTable")){if(REC.sort===key)REC.dir=-REC.dir;else{REC.sort=key;REC.dir=-1}renderRecent()}
   else if(th.closest("#slowTable")){if(SLOW.sort===key)SLOW.dir=-SLOW.dir;else{SLOW.sort=key;SLOW.dir=-1}renderRecent()}
   else if(th.closest("#usageTable")){sortUsage(key)}})});
 function sortUsage(key){
  var accts=arr(obj(SUM).by_account).slice();
  accts.sort(function(a,b){var x=a[key],y=b[key];
   if(key==="account"||key==="tail")return String(x||"").localeCompare(String(y||""));
   return num(y)-num(x)});
  var keep=SUM.by_account;SUM.by_account=accts;renderUsage(SUM);SUM.by_account=keep}}
function boot(){
 STATE.range=LS.get("poolDash.range","all");STATE.iv=num(LS.get("poolDash.iv"))||15000;STATE.auto=LS.get("poolDash.auto",true);
 if(!window.echarts)$("bootWarn").style.display="block";
 $("rangeSel").value=STATE.range;$("ivSel").value=String(STATE.iv);$("autoChk").checked=!!STATE.auto;
 $("rangeSel").addEventListener("change",function(){STATE.range=this.value;LS.set("poolDash.range",STATE.range);load()});
 $("ivSel").addEventListener("change",function(){STATE.iv=num(this.value)||15000;LS.set("poolDash.iv",STATE.iv);restartTimer();load()});
 $("autoChk").addEventListener("change",function(){STATE.auto=this.checked;LS.set("poolDash.auto",STATE.auto);restartTimer();load()});
 $("modelSort").addEventListener("change",function(){LS.set("poolDash.msort",this.value);renderModelBar()});
 $("modelSort").value=LS.get("poolDash.msort","tokens");
 $("reload").addEventListener("click",function(){load();loadAcctSessions()});
 $("recQ").addEventListener("input",function(){REC.q=this.value.trim();renderRecent()});
 $("recOnly").addEventListener("change",function(){REC.only=this.value;renderRecent()});
 window.addEventListener("resize",function(){if(boot._t)clearTimeout(boot._t);boot._t=setTimeout(resizeAll,140)});
 if(window.ResizeObserver){var ro=new ResizeObserver(function(){if(boot._r)clearTimeout(boot._r);boot._r=setTimeout(resizeAll,180)});
  document.querySelectorAll(".pbody").forEach(function(b){ro.observe(b)})}
 initDashNav();bindTables();load();loadAcctSessions();restartTimer()}
function restartTimer(){
 if(boot._iv)clearInterval(boot._iv);
 if(!STATE.auto){$("refresh").textContent="自动刷新已关闭";return}
 boot._iv=setInterval(function(){load();loadAcctSessions()},STATE.iv)}
/* 2026-10-31 弃用并移除：DSH 模型同步（api/models-config + api/sync-models）与
   Agent Teams 团队模型规则（api/teams-config + api/save-teams-config）前端代码。
   面板已删除，这些脚本若保留会在 boot 时对 null 调 addEventListener 抛错。
   后端同名业务函数与路由已整段注释保留（见文件前部「2026-10-31 弃用」段）。 */

/* =========================================================================
   驾驶舱层（dashfix 2026-10-19）：页签 / 双主题 / 哈希路由。
   与 v12 主脚本共用 $ / LS / INST / resizeAll；只新增函数，不重定义 v12 的任何函数。
   ========================================================================= */
var TABS=["overview","pool","usage","perf","routing","admin"];
var TABCUR="overview",THEME_KEY="pool-theme-v1";
function tabOf(b){return (b&&b.dataset&&b.dataset.tab)?b.dataset.tab:""}
/* 页签切换后图表容器才拿到真实宽度，ECharts 需要按新尺寸重算（多档兜底，
   不依赖 v12 的 ResizeObserver 一定能捕捉到 display:none → block）。 */
function dashResizeSoon(){[0,180,600].forEach(function(d){setTimeout(resizeAll,d)})}
function showTab(id,fromHash){
 if(id==="config")id="admin";   /* 2026-10-20：DSH 配置并入控制台，旧书签 #config 重定向 */
 if(TABS.indexOf(id)<0)id="overview";
 TABCUR=id;
 document.querySelectorAll("#tabs .tab").forEach(function(b){b.classList.toggle("on",tabOf(b)===id)});
 document.querySelectorAll(".tpane").forEach(function(p){p.classList.toggle("on",p.id==="tab-"+id)});
 LS.set("poolDash.tab",id);
 if(!fromHash&&location.hash!=="#"+id){
  try{history.replaceState(null,"","#"+id)}catch(e){location.hash=id}}
 dashResizeSoon();
 if(id==="admin"&&typeof window.adminInit==="function"){try{window.adminInit()}catch(e){}}
 /* 路由面板的 P2/P4 图在隐藏页签里 init 时 offsetWidth=0 会被跳过 → 切到本页签重渲染 */
 if(id==="routing"&&RT){try{renderRouting(RT)}catch(e){}}}
function initDashNav(){
 var nav=$("tabs");
 if(nav)nav.addEventListener("click",function(e){
  var b=e.target.closest?e.target.closest(".tab"):null;if(b)showTab(tabOf(b))});
 var tb=$("themeBtn");
 if(tb)tb.addEventListener("click",function(){
  var next=document.documentElement.getAttribute("data-theme")==="light"?"dark":"light";
  document.documentElement.setAttribute("data-theme",next);
  LS.set(THEME_KEY,next);
  if(TABCUR==="routing"&&RT){try{renderRouting(RT)}catch(e){}}
  dashResizeSoon()});
 window.addEventListener("hashchange",function(){
  showTab((location.hash||"").slice(1)||"overview",true)});
 /* 优先级：URL 哈希（/admin 跳转落到 #admin）> localStorage 记忆 > 总览 */
 var want=(location.hash||"").slice(1);
 /* 2026-10-20：DSH 配置页签已并入控制台；旧书签 #config 必须在回退逻辑之前改写 */
 if(want==="config")want="admin";
 if(TABS.indexOf(want)<0)want=LS.get("poolDash.tab","overview");
 showTab(TABS.indexOf(want)<0?"overview":want,true)}

boot();
</script></body></html>"""

# ---------- 驾驶舱层：管理控制台页签注入（dashfix 2026-10-19） ----------
# 管理控制台不再是独立页面，而是本页的一个页签：骨架里的 __ADMIN_PANEL__ 占位符
# 由 render_page() 用 pool_admin_ui.build_panel(_ADMIN.state()) 就地替换。
# 片段渲染失败一律降级成可读占位说明 —— 宁可少一块功能，也不让整页 500。
_ADMIN_PANEL_FALLBACK = (
    '<div class="adm"><div class="banner bad"><b>管理控制台不可用</b> · 未找到 '
    '<span class="mono">pool_admin_ui.py</span>（或版本不匹配：需要 '
    '<span class="mono">pool_admin_ui.build_panel()</span>）。'
    '号池主看板不受影响；补齐该模块后刷新本页。</div></div>')


def render_page():
    """看板首页 = v12 骨架（已按 6 页签重组）+ 服务端注入的管理控制台页签片段。"""
    panel = _ADMIN_PANEL_FALLBACK
    if _ADMIN is not None and _pool_admin_ui is not None:
        build_panel = getattr(_pool_admin_ui, "build_panel", None)
        try:
            if callable(build_panel):
                panel = build_panel(_ADMIN.state())
        except Exception as exc:                       # 兜底：不让管理面拖垮主看板
            panel = ('<div class="adm"><div class="banner bad"><b>管理控制台渲染失败</b> · '
                     + _html_mod.escape(str(exc)[:200], quote=True) + '</div></div>')
    return HTML.replace("__ADMIN_PANEL__", panel)


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body.encode())
    def do_GET(self):
        if _ADMIN is not None and _ADMIN.dispatch(self, "GET"):
            return
        if self.path in ("/", "/index.html"):
            self._send(200, render_page(), "text/html; charset=utf-8")
        elif self.path in ("/admin", "/admin/"):
            # 2026-10-19 dashfix：管理控制台已并入号池看板页签，不再是独立页面。
            # 2026-10-20：顶栏冗余的 href="admin" 入口已删除，页签是唯一入口；
            # 本路由仅为**外部书签/历史链接**保留，直接 404 会变成死链。
            # 返回 200 + 一小段跳转脚本，落到 #admin 页签。
            # 跳转目标**前缀无关**：直接从 location.pathname 反推当前前缀再拼 #admin，
            # 因此 nginx 反代在 /、/works/pool/ 或任何前缀下都正确，不硬编码任何前缀。
            stub = (r'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
                    r'<title>管理控制台 · 跳转中…</title><link rel="icon" href="data:,">'
                    r'</head><body><p>管理控制台已并入号池看板页签，正在跳转…</p>'
                    r"<script>location.replace(location.pathname.replace(/admin\/?$/, '')"
                    r" + '#admin')</script></body></html>")
            self._send(200, stub, "text/html; charset=utf-8")
        elif self.path == "/api/summary" or self.path.startswith("/api/summary?"):
            # 2026-10-28：范围下拉（全部/近24h/近7天）→ 后端按窗口聚合历史请求日志（含轮转归档）
            _rk = "all"
            if "?" in self.path:
                try:
                    from urllib.parse import parse_qs
                    _qs = parse_qs(self.path.split("?", 1)[1])
                    _v = ((_qs.get("range") or ["all"])[0] or "all").strip()
                    if _v in ("all", "24", "168"):
                        _rk = _v
                except Exception:
                    _rk = "all"
            self._send(200, _cached_json("summary:" + _rk, SUM_TTL, lambda: summary(_rk)),
                       "application/json; charset=utf-8")
#         elif self.path == "/api/teams-config":
#             try:
#                 self._send(200, json.dumps(teams_config(), ensure_ascii=False), "application/json; charset=utf-8")
#             except Exception as e:
#                 self._send(500, json.dumps({"error": str(e)[:200]}), "application/json; charset=utf-8")
#         elif self.path == "/api/models-config":
#             try:
#                 self._send(200, json.dumps(dsh_models_config(), ensure_ascii=False), "application/json; charset=utf-8")
#             except Exception as e:
#                 self._send(500, json.dumps({"error": str(e)[:200]}), "application/json; charset=utf-8")
        elif self.path == "/api/account-sessions":
            try:
                self._send(200, _cached_json("sessions", SESS_TTL, account_sessions),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)[:200]}), "application/json; charset=utf-8")
        elif self.path == "/vendor/echarts.min.js":
            # v12 号池看板：本地 vendored ECharts 5.x（零 npm / 零构建 / 零 CDN）。
            # 前端以相对路径 vendor/echarts.min.js 引入，故经 /works/pool/ 反代前缀访问时同样命中；
            # 静态资源给长缓存（不能被 _send 的 no-store 覆盖，故此处自写响应头）。
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "vendor", "echarts.min.js"), "rb") as f:
                    body = f.read()
            except Exception:
                self._send(404, "not found", "text/plain")
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=604800, immutable")
                self.end_headers()
                self.wfile.write(body)
        else:
            self._send(404, "not found", "text/plain")
    def do_POST(self):
        if _ADMIN is not None and _ADMIN.dispatch(self, "POST"):
            return
#         if self.path == "/api/sync-models":
#             try:
#                 ln = int(self.headers.get("Content-Length") or 0)
#                 body = json.loads(self.rfile.read(ln) or b"{}")
#                 sel = body.get("providers") or {}
#                 r = sync_dsh_models(sel)
#                 if r.get("ok"):
#                     r.update(restart_dsh())
#                 self._send(200, json.dumps(r, ensure_ascii=False), "application/json; charset=utf-8")
#             except Exception as e:
#                 self._send(500, json.dumps({"ok": False, "error": str(e)[:200]}), "application/json; charset=utf-8")
#         if self.path == "/api/save-teams-config":
#             try:
#                 ln = int(self.headers.get("Content-Length") or 0)
#                 body = json.loads(self.rfile.read(ln) or b"{}")
#                 r = save_teams_config(body.get("config") or {}, bool(body.get("restart")))
#                 self._send(200, json.dumps(r, ensure_ascii=False), "application/json; charset=utf-8")
#             except Exception as e:
#                 self._send(500, json.dumps({"ok": False, "error": str(e)[:200]}), "application/json; charset=utf-8")
        self._send(404, "not found", "text/plain")

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="OpenCode Go pool dashboard")
    ap.add_argument("--listen", default="127.0.0.1:4010",
                    help="监听地址 host:port（pool-dash=127.0.0.1:4010；pool-dashboard=127.0.0.1:8088）")
    ap.add_argument("--selftest", action="store_true", help="P3 观测面自测（6 面板 + 8 告警）")
    args = ap.parse_args()
    if args.selftest:
        import sys
        sys.exit(selftest_p3())
    _h, _, _p = args.listen.partition(":")
    ThreadingHTTPServer((_h or "127.0.0.1", int(_p or 4010)), H).serve_forever()
