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
"""
import json, os, re, shutil, subprocess, threading, time, urllib.request
import bisect
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

try:  # 管理控制台（新增；模块缺失/依赖不可用时主看板行为完全不变）
    import pool_admin as _pool_admin
    _ADMIN = _pool_admin.AdminApp()
except Exception:
    _ADMIN = None

LITELLM = "http://127.0.0.1:4000"
UPSTREAM = "https://opencode.ai/inference/go/v1"   # 2026-10-16 规范路径（旧 /zen/go/v1 为别名）
# 上游明文要求客户端自带可识别 UA（禁止通用 SDK/HTTP 库名）
POOL_USER_AGENT = "dsh-opencodego-pool/1.0"
CFG = "/opt/litellm/config.yaml"
ENV = "/opt/litellm/.env"
DSH_PATCH = "/root/.dsh/profiles/desktop/cordis.patch.yml"
DSH_SVC = "dsh-desktop.service"
DEF_CTX, DEF_MAX = "131072", "8192"
PROVIDERS = ["litellm", "litellm-responses", "litellm-anthropic"]

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

def ocg_keys():
    ks = []
    for line in open(ENV):
        line = line.strip()
        if line.startswith("OCG_KEY_") and "=" in line and not line.split("=", 1)[1].startswith("REPLACE"):
            ks.append(line.split("=", 1)[0])
    return sorted(ks)

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

def account_email(slot, key=""):
    """号位 → 邮箱；返回 (邮箱, 来源)。来源: env / map / upstream / placeholder"""
    v = env("OCG_EMAIL_%s" % slot)
    if "@" in v:
        return v, "env"
    v = str(email_map().get("OCG_KEY_%s" % slot) or "").strip()
    if "@" in v:
        return v, "map"
    if key:
        v = probe_upstream_email(key, key[-12:])
        if "@" in v:
            try:
                m = email_map()
                m["OCG_KEY_%s" % slot] = v
                open(EMAIL_MAP, "w").write(json.dumps(m, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            except Exception:
                pass
            return v, "upstream"
    # 无从得知真实邮箱：给邮箱格式占位（RFC 2606 .invalid 域，绝不可达），前端加「邮箱待补」徽标
    return "***@***" % slot, "placeholder"

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
    keys = ocg_keys()
    if not keys:
        return []
    try:
        cfg = open(CFG).read()
    except Exception:
        cfg = ""
    out = []
    with ThreadPoolExecutor(max_workers=min(8, len(keys))) as ex:
        futs = {}
        for k in keys:
            slot = k.split("_")[-1]
            val = env(k)
            label, src = account_email(slot, val)
            futs[ex.submit(fetch_account_usage, label, val, slot, src)] = k
        for f in as_completed(futs):
            a = f.result()
            a["inPool"] = ("os.environ/" + futs[f]) in cfg  # 是否在当前 config.yaml 部署
            out.append(a)
    out.sort(key=lambda a: (int(a.get("slot") or 0), a.get("tail") or ""))
    return out

# ---------- DSH 模型配置读取/同步 ----------

PROV_HEAD = r"      %s:\n(?:        [^\n]*\n)*?        models:\n"
MODELS_ROW = re.compile(
    r"- id:\s*(\S+)\s*\n\s+name:\s*(\S+)\s*\n\s+contextWindow:\s*(\d+)\s*\n\s+maxTokens:\s*(\d+)")

def _prov_head(src, name):
    m = re.search(PROV_HEAD % re.escape(name), src)
    return m

def dsh_providers_meta(src):
    """解析三个 provider 的 api/displayName 与当前 models（含 ctx/max 以便保留）"""
    out = {}
    for name in PROVIDERS:
        m = _prov_head(src, name)
        if not m:
            out[name] = {"api": "?", "displayName": name, "models": []}
            continue
        head = m.group(0)
        api = re.search(r"api:\s*(\S+)", head)
        disp = re.search(r"displayName:\s*(.+)", head)
        tail = src[m.end():]
        mm = re.match(r"(?:          [^\n]*\n)+", tail)
        rows = MODELS_ROW.findall(mm.group(0)) if mm else []
        out[name] = {"api": api.group(1) if api else "?",
                     "displayName": disp.group(1).strip() if disp else name,
                     "models": [{"id": r[0], "ctx": r[2], "max": r[3]} for r in rows]}
    return out

def dsh_models_config():
    src = open(DSH_PATCH).read()
    meta = dsh_providers_meta(src)
    avail = pool_models()
    # 可用全集 = 号池模型 ∪ 当前已选（避免号池暂时移除导致勾选丢失）
    for name in PROVIDERS:
        for m in meta[name]["models"]:
            if m["id"] not in avail:
                avail.append(m["id"])
    return {"available": sorted(avail),
            "providers": {n: {"api": meta[n]["api"], "displayName": meta[n]["displayName"],
                              "selected": [m["id"] for m in meta[n]["models"]]} for n in PROVIDERS}}

# openai-completions 协议下思考强度（reasoningEfforts）的默认声明：
# 号池 7 个模型 × 7 档位实测（2026-09-28）中 low/medium/high 是**全部模型都接受**的
# 交集（glm-5.3 拒 minimal、mimo-v2.6 拒 xhigh/max），故新同步进来的 openai-completions
# 模型默认给这三档，保证 DSH 连接里一选模型就有档位可选且不会被上游 400 拒。
# 已有声明（含 reasoningEfforts: false 非推理模型）一律原样保留，绝不覆盖。
DEFAULT_EFFORTS_BLOCK = "            reasoningEfforts:\n              low: low\n              medium: medium\n              high: high\n"

def dsh_efforts_meta(src):
    """提取每个 provider 下各模型的 `reasoningEfforts:` 子块原文。

    sync_dsh_models 的替换正则 `(?:          [^\\n]*\\n)+` 会把 models: 整块重写
    （10 空格起的行全被吃掉，含 12/14 空格的档位行），所以必须先按
    (provider, model id) 捕获，重建时再回填，否则 DSH 连接的思考强度声明会被抹掉。
    返回 {provider: {model_id: 子块原文}}；没有声明的模型不出现在结果里。
    """
    out = {}
    for name in PROVIDERS:
        m = _prov_head(src, name)
        if not m:
            continue
        mm = re.match(r"(?:          [^\n]*\n)+", src[m.end():])
        if not mm:
            continue
        cur_id, buf, got = None, None, {}
        for line in mm.group(0).splitlines(True):
            idm = re.match(r"          - id:\s*(\S+)", line)
            if idm:
                if buf is not None and cur_id:
                    got[cur_id] = "".join(buf)
                cur_id, buf = idm.group(1), None
                continue
            if re.match(r"^            reasoningEfforts:", line):
                if buf is not None and cur_id:
                    got[cur_id] = "".join(buf)
                buf = [line]
                continue
            if buf is not None:
                # 14 空格 = 档位条目，属于同一子块；缩进变浅即结束
                if line.startswith("              "):
                    buf.append(line)
                else:
                    if cur_id:
                        got[cur_id] = "".join(buf)
                    buf = None
        if buf is not None and cur_id:
            got[cur_id] = "".join(buf)
        out[name] = got
    return out

def sync_dsh_models(selection):
    """selection: {provider: [model ids]}；文本替换 models 块 + YAML 校验 + 失败回滚

    重建 models 块时按 (provider, model id) 回填原 reasoningEfforts 子块；
    openai-completions 协议下新加入、原本无声明的模型补 DEFAULT_EFFORTS_BLOCK，
    其余协议保持无声明（档位能力随后续阶段再开放）。"""
    src = open(DSH_PATCH).read()
    meta = dsh_providers_meta(src)
    efforts = dsh_efforts_meta(src)
    ctx_map = {m["id"]: (m["ctx"], m["max"]) for n in PROVIDERS for m in meta[n]["models"]}
    backup = DSH_PATCH + ".bak-sync-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(DSH_PATCH, backup)
    new_src = src
    for name in PROVIDERS:
        ids = sorted(set(selection.get(name) or []))
        if not ids:
            continue  # 不允许清空：跳过并保留原状
        want_default = meta[name]["api"] == "openai-completions"
        preserved = efforts.get(name, {})
        body = ""
        for i in ids:
            body += (
                "          - id: %s\n            name: %s\n            contextWindow: %s\n            maxTokens: %s\n"
                % (i, i, ctx_map.get(i, (DEF_CTX, DEF_MAX))[0], ctx_map.get(i, (DEF_CTX, DEF_MAX))[1]))
            if i in preserved:
                body += preserved[i]          # 原样保留部署声明的档位（含 false）
            elif want_default:
                body += DEFAULT_EFFORTS_BLOCK  # openai-completions 新模型给可选档位
        pat = re.compile("(" + PROV_HEAD % re.escape(name) + r")(?:          [^\n]*\n)+")
        new_src, n = pat.subn(lambda mm: mm.group(1) + body, new_src, count=1)
        if n != 1:
            return {"ok": False, "error": "provider %s models block not found" % name, "backup": backup}
    try:
        import yaml
        yaml.safe_load(new_src.replace("!!js", "_js"))
    except Exception as e:
        shutil.copy2(backup, DSH_PATCH)
        return {"ok": False, "error": "YAML validation failed, rolled back: %s" % str(e)[:120], "backup": backup}
    open(DSH_PATCH, "w").write(new_src)
    return {"ok": True, "backup": backup, "counts": {n: len(selection.get(n) or []) for n in PROVIDERS}}

def restart_dsh():
    try:
        subprocess.run(["systemctl", "restart", DSH_SVC], timeout=60, check=True)
    except Exception as e:
        return {"restarted": False, "error": str(e)[:120]}
    time.sleep(10)
    try:
        st = subprocess.run(["systemctl", "is-active", DSH_SVC],
                            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e:
        st = "unknown: %s" % str(e)[:60]
    return {"restarted": True, "service": st}

# ---------- Agent Teams 团队模型规则 ----------

TEAMS_ANCHOR = re.compile(r"- id: agent-teams\n")

def teams_config():
    """读取 cordis.patch.yml 中 agent-teams 条目（memberModel/maxMembers/profiles）"""
    base = {"memberModel": "", "memberMaxDepth": 0, "maxMembers": 8, "profiles": {}}
    try:
        src = open(DSH_PATCH).read()
    except Exception as e:
        return {"available": pool_models(), "config": base, "present": False, "error": str(e)[:120]}
    m = TEAMS_ANCHOR.search(src)
    if not m:
        return {"available": pool_models(), "config": base, "present": False}
    try:
        import yaml
        d = yaml.safe_load(src[m.start():].replace("!!js", "_js"))
        entry = d[0] if isinstance(d, list) else d
        cfg = (entry or {}).get("config", {}) if isinstance(entry, dict) else {}
    except Exception as e:
        return {"available": pool_models(), "config": base, "present": True, "error": "解析失败: %s" % str(e)[:120]}
    base["memberModel"] = cfg.get("memberModel", "")
    base["memberMaxDepth"] = cfg.get("memberMaxDepth", 0)
    base["maxMembers"] = cfg.get("maxMembers", 8)
    profs = {}
    for name, p in (cfg.get("profiles") or {}).items():
        if not isinstance(p, dict):
            continue
        members = [{"name": mm.get("name", ""), "role": mm.get("role", "teammate"),
                    "model": mm.get("model", "")}
                   for mm in (p.get("members") or []) if isinstance(mm, dict)]
        profs[name] = {"description": p.get("description", ""), "members": members}
    base["profiles"] = profs
    return {"available": pool_models(), "config": base, "present": True}

SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
SAFE_ROLE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff -]{0,64}$")

def _yaml_str(s):
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')

def save_teams_config(cfg, restart):
    """校验并重写 agent-teams 块（条目至文件尾），YAML 校验失败回滚；可选重启 dsh"""
    member_model = str(cfg.get("memberModel") or "").strip()
    if not SAFE_NAME.match(member_model):
        return {"ok": False, "error": "memberModel 非法: %s" % member_model[:40]}
    try:
        max_members = int(cfg.get("maxMembers", 8))
        max_depth = int(cfg.get("memberMaxDepth", 0))
    except Exception:
        return {"ok": False, "error": "maxMembers / memberMaxDepth 必须是整数"}
    if not (1 <= max_members <= 64):
        return {"ok": False, "error": "maxMembers 需在 1-64 之间"}
    if not (0 <= max_depth <= 3):
        return {"ok": False, "error": "memberMaxDepth 需在 0-3 之间"}
    profs_in = cfg.get("profiles") or {}
    if len(profs_in) > 16:
        return {"ok": False, "error": "profiles 最多 16 个模板"}
    valid_models = set(pool_models())
    parsed = []
    for pname, p in profs_in.items():
        if not SAFE_NAME.match(str(pname or "")):
            return {"ok": False, "error": "模板名称非法: %s（限字母/数字/_-.，且不以-开头）" % pname}
        members = p.get("members") or []
        if not (1 <= len(members) <= 32):
            return {"ok": False, "error": "模板 %s 成员数需在 1-32 之间" % pname}
        rows = []
        for mm in members:
            name = str((mm or {}).get("name") or "").strip()
            role = str((mm or {}).get("role") or "").strip()
            model = str((mm or {}).get("model") or "").strip()
            if not SAFE_NAME.match(name):
                return {"ok": False, "error": "成员名非法: %s" % name[:40]}
            if not SAFE_ROLE.match(role):
                return {"ok": False, "error": "成员 %s 的角色非法" % name}
            if model not in valid_models:
                return {"ok": False, "error": "成员 %s 的模型不在号池清单中: %s" % (name, model[:40])}
            rows.append((name, role or "teammate", model))
        parsed.append((pname, str((p or {}).get("description") or "").strip(), rows))

    out = ["# 由号池看板「团队模型规则」页管理：保存即重写本块（自动备份 + YAML 校验）。",
           "- id: agent-teams",
           "  config:",
           "    stateDir: .agent-teams",
           "    memberProvider: spawn",
           "    memberModel: %s" % member_model,
           "    memberMaxDepth: %d" % max_depth,
           "    maxMembers: %d" % max_members]
    if parsed:
        out.append("    profiles:")
        for pname, desc, rows in parsed:
            out.append("      %s:" % pname)
            out.append("        description: %s" % _yaml_str(desc or ("团队模板 " + pname)))
            out.append("        taskPlanning: captain")
            out.append("        members:")
            for name, role, model in rows:
                out.append("          - name: %s" % name)
                out.append("            role: %s" % _yaml_str(role))
                out.append("            provider: litellm")
                out.append("            model: %s" % model)
    new_block = "\n".join(out) + "\n"

    try:
        src = open(DSH_PATCH).read()
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    backup = DSH_PATCH + ".bak-teams-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(DSH_PATCH, backup)
    m = TEAMS_ANCHOR.search(src)
    if m:
        # 只替换 agent-teams 块本身（到下一个顶层 "- id: " 为止），保留下游条目
        nxt = re.search(r"(?m)^- id: ", src[m.end():])
        blk_end = (m.end() + nxt.start()) if nxt else len(src)
        new_src = src[:m.start()] + new_block + (("\n" + src[blk_end:]) if blk_end < len(src) else "")
    else:
        new_src = src.rstrip("\n") + "\n\n" + new_block
    try:
        import yaml
        yaml.safe_load(new_src.replace("!!js", "_js"))
    except Exception as e:
        shutil.copy2(backup, DSH_PATCH)
        return {"ok": False, "error": "YAML 校验失败已回滚: %s" % str(e)[:120], "backup": backup}
    open(DSH_PATCH, "w").write(new_src)
    r = {"ok": True, "backup": backup, "profiles": len(parsed)}
    if restart:
        r.update(restart_dsh())
    return r

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

def account_labels():
    """OCG_KEY_n 值尾 12 字 / os.environ 引用 → 账号邮箱（解析链见 account_email）"""
    mapping = {}
    for k in ocg_keys():
        suffix = k.split("_")[-1]
        label = account_email(suffix, env(k))[0]
        val = env(k)
        if val:
            mapping[val[-12:]] = label
        mapping["os.environ/" + k] = label
    return mapping

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
    """账号 → 服务过的会话列表：请求完成时间与会话 assistant 消息时间 ±8s 对齐"""
    labels = account_labels()
    reqs = litellm_request_log()
    sessions = scan_dsh_sessions()
    smap = {s["id"]: s for s in sessions}
    idx = _build_session_idx(sessions)

    def label_of(ref):
        for suf, lab in labels.items():
            if suf and ref.endswith(suf):
                return lab
        # 未映射的 keyRef：同样给邮箱格式占位，绝不把密钥/尾号当账号名展示
        return "***@***" if ref else "***@***"

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
    for r in reqs:
        k = r.get("keyRef", "")
        if not k:
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
        src = r.get("src")
        if src not in ("local", "ext"):
            src = "ext" if str(r.get("sess") or "").startswith("ext-") else "local"
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
            rows.append({"title": s["title"], "firstMsg": s["firstMsg"], "model": s["model"],
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
        accounts.append({"account": e["lab"], "tail": (alltails[0] if alltails else (kref or "")[-12:]),
                         "tails": alltails, "refs": len(e["raws"]), "hits": e["hits"],
                         "tokensTotal": sum(e["tokens"].values()),
                         "models": models, "sessions": rows})
    accounts.sort(key=lambda a: -a["hits"])
    # 未分配到任何账号的会话（日志期外或对齐失败）也给出，便于核对
    matched = {r["ws"] + "/" + r["title"] for a in accounts for r in a["sessions"]}
    orphans = [{"title": s["title"], "firstMsg": s["firstMsg"], "model": s["model"],
                "ws": s["ws"], "createdAt": s["createdAt"], "lastActive": s["lastActive"]}
               for s in sessions
               if s["ws"] + "/" + s["title"] not in matched and s["assistantTs"]]
    return {"accounts": accounts, "orphans": orphans,
            "sessionsTotal": len(sessions), "logRows": len(reqs),
            "note": "请求级日志自 2026-09-26 起记录；本地请求按 assistant 时间 ±8 秒对齐会话，"
                    "外网调用（src=ext 或 sess ext- 前缀）聚合成「外网 API 调用」行，同刻并发可能偶有归属偏差；"
                    "同一邮箱多 key / 不同 key 写法已按邮箱合并"}

# ---------- 汇总 ----------

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

def summary():
    out = {"health": "down", "models": 0, "deployments": 0, "accounts": 0,
           "model_names": [], "total": 0, "by_status": {}, "by_model": {}, "by_hour": [],
           "recent": [], "account_usage": [], "accounts_limited": 0,
           "usage_totals": {"hits": 0, "pt": 0, "ct": 0, "tt": 0, "firstTs": 0, "lastTs": 0},
           "last24": {"hits": 0, "pt": 0, "ct": 0, "tt": 0},
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
        try:
            cfg = open(CFG).read()
            out["accounts"] = len(set(re.findall(r"os.environ/(OCG_KEY_\d+)", cfg)))
        except Exception:
            pass
    except Exception:
        pass
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
        out["total"] = len(rows)
        out["by_status"] = dict(Counter(r["code"] for r in rows))
        out["recent"] = rows[:15]
        hourly = {}
        for r in rows:
            h = r["ts"][:13]  # "YYYY-MM-DD HH"
            e = hourly.setdefault(h, [0, 0])
            e[0] += 1
            if r["code"] >= 400:
                e[1] += 1
        out["by_hour"] = [{"h": k, "n": v[0], "err": v[1]} for k, v in sorted(hourly.items())]
    except Exception:
        pass
    # 按模型/账号/时段的用量聚合：来自请求级日志（pool-requests.jsonl 含真实模型名，自 2026-09-26 起记录）
    log_rows = litellm_request_log()
    try:
        labels = account_labels()
        agg, per_acct, hour = {}, {}, {}
        perf, iv_by_tail, all_iv = {}, {}, []   # 模型耗时样本 / 每账号请求区间 / 全局请求区间
        bsrc, src_hour = {}, {}                 # 来源(local/ext)用量 / 来源按小时
        cache_m = {}                            # 按模型缓存命中
        sess_agg = {}                           # 按会话聚合（Top10）
        tot = {"hits": 0, "pt": 0, "ct": 0, "tt": 0, "firstTs": 0, "lastTs": 0}
        last24 = {"hits": 0, "pt": 0, "ct": 0, "tt": 0}
        cut24 = time.time() * 1000 - 24 * 3600 * 1000
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
            if src not in ("local", "ext"):
                src = "ext" if str(r.get("sess") or "").startswith("ext-") else "local"
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
            if ts >= cut24:
                last24["hits"] += 1
                last24["pt"] += pt
                last24["ct"] += ct
                last24["tt"] += tt
            tail = (r.get("keyRef") or "")[-12:]
            if tail:
                if 0 < dur <= 6 * 3600 * 1000:
                    iv_by_tail.setdefault(tail, []).append((t0, ts))
                pa = per_acct.setdefault(tail, {
                    "tail": tail, "account": labels.get(tail) or "***@***",
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
                if ts >= cut24:
                    pa["h24_hits"] += 1
                    pa["h24_tt"] += tt
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
        out["last24"] = last24
        out["tokens_by_hour"] = sorted(hour.values(), key=lambda x: x["h"])
        accts = []
        for pa in per_acct.values():
            pa["models"] = [{"model": mm, "hits": pa["_models"][mm], "tokens": pa["_tokens"][mm]}
                            for mm in sorted(pa["_tokens"], key=lambda x: -pa["_tokens"][x])]
            del pa["_models"], pa["_tokens"]
            accts.append(pa)
        accts.sort(key=lambda x: -x["tt"])
        h24_total = max(1, sum(pa.get("h24_tt", 0) for pa in accts))
        for pa in accts:
            pa["h24_pct"] = round(pa.get("h24_tt", 0) * 100.0 / h24_total, 1)
        out["by_account"] = accts
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
                         "account": pa.get("account") or labels.get(tref) or "***@***",
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
        out["accounts_limited"] = sum(1 for a in au if a["state"] == "limited")
        # 账号数以 SQLite 控制面（pool_store）为准；DB 化后 config.yaml 已无 os.environ 引用
        try:
            import sys as _sys
            if "/opt/litellm" not in _sys.path:
                _sys.path.insert(0, "/opt/litellm")
            import pool_store as _ps
            _n = len(_ps.snapshot().get("accounts") or [])
            if _n:
                out["accounts"] = _n
        except Exception:
            pass
        if not out.get("accounts"):
            out["accounts"] = len(au)
    except Exception:
        pass
    try:
        out["routing"] = routing_summary()
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
    burn, win_cost = {}, {}
    cut = now - P3_BASELINE_S
    for r in pool_rows:
        ts = int(r.get("ts") or 0) / 1000.0
        if ts < cut:
            continue
        slot = slot_map.get(str(r.get("keyRef") or "")[-24:], "unmapped")
        win_cost[slot] = win_cost.get(slot, 0.0) + _p3_rec_cost(r)
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
        t_ex = (rem_usd / (b / 86400.0)) if b > 0 else float("inf")
        if t_ex < t_reset - P3_G1_MARGIN_S:
            state = "danger"
        elif t_ex > t_reset + P3_IDLE_MARGIN_S and rem_pct > 5:
            state = "idle"
        else:
            state = "ok"
        p1.append({"key_ref": kr, "rem_pct": rem_pct, "days": days,
                   "t_reset_s": round(t_reset),
                   "t_exhaust_s": (round(t_ex) if t_ex != float("inf") else None),
                   "burn_usd_day": round(b, 4), "pace_usd_day": pace, "state": state})

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
    _add("A6", "warm 命中 1h < 基线−5pp", "warn", warm_drop,
         "cache_rate_1h < cache_baseline_24h - 5pp",
         "1h=%s vs 基线=%s" % (r1, rb))
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



HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpencodeLitellmPool · 号池看板</title>
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
.kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:12px}
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
.ph .fold{margin-left:auto;flex:none;width:24px;height:22px;line-height:1;padding:0;color:var(--dim);background:transparent}
.ph .fold:hover{color:var(--txt)}
.panel.collapsed .pbody{display:none}
.panel.collapsed .fold{transform:rotate(-90deg)}
.pbody{position:relative;padding:8px 10px 10px}
.chart{width:100%;height:248px}
.h180{height:180px}.h210{height:210px}.h280{height:280px}.h300{height:300px}.h150{height:150px}
.pbody.is-empty .chart{visibility:hidden}
.empty{display:none;position:absolute;left:0;right:0;top:0;bottom:0;align-items:center;justify-content:center;
 color:var(--dim);font-size:12px;text-align:center;padding:14px}
.pbody.is-empty .empty{display:flex}
.note{font-size:11px;color:var(--dim);margin-top:6px;line-height:1.55}
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
.det-head{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.det-name{font-weight:700;font-size:13.5px}
.det-meta{font-size:11px;color:var(--dim);margin:5px 0 8px;display:flex;gap:12px;flex-wrap:wrap}
.statgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(88px,1fr));gap:7px}
.stat{background:var(--card2);border:1px solid var(--line);border-radius:8px;padding:6px 5px;text-align:center;min-width:0}
.stat .sv{font-size:15px;font-weight:700;line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stat .sv.sm{font-size:11.5px}
.stat .sk{font-size:10px;color:var(--dim)}
.wins{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin:7px 0}
.win{background:var(--card2);border:1px solid var(--line);border-radius:8px;padding:6px 5px;text-align:center;min-width:0}
.win .wn{font-size:10.5px;color:var(--dim)}
.win .wv{font-size:16px;font-weight:700}
.win .st{font-size:10.5px;font-weight:600}
.win .rst{font-size:10px;color:var(--dim);min-height:13px;word-break:break-word}
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
@media (max-width:820px){
  /* 窄屏钉死单列：.grid 的 12 列模板在有其他样式表介入时可能盖过上面的媒体查询，
     实测 390/768 下 .panel 被挤成 34/88px 宽的竖条 → 用 id 级 + !important 兜底 */
  #gridMain,#gridRoute{grid-template-columns:1fr !important}
  #gridMain>.panel,#gridRoute>.panel{grid-column:1 / -1 !important}
}
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
 <button id="foldAll" title="折叠/展开全部面板">全部折叠</button>
 <button id="reload">刷新</button>
 <a class="pill" href="admin" title="号池管理控制台（同源相对路径，反代前缀下同样可用）">管理控制台 →</a>
</div>
<div class="kpis" id="kpis"></div>
<div class="grid" id="gridMain">
 <section class="panel c4" data-key="status">
  <div class="ph"><h3>状态码占比</h3><span class="tag">环形 + 中心总数</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody"><div class="chart h210" id="chart-status" data-chart="1"></div><div class="empty">暂无请求记录（容器日志窗口内无请求）</div></div>
 </section>
 <section class="panel c8" data-key="model">
  <div class="ph"><h3>按模型 Token / 请求量</h3>
   <div class="ctl" style="margin-left:6px">排序 <select id="modelSort">
    <option value="tokens">按 Token</option><option value="hits">按请求</option><option value="model">按名称</option></select></div>
   <button class="fold" title="折叠">▾</button></div>
  <div class="pbody"><div class="chart h300" id="chart-model" data-chart="1"></div><div class="empty">暂无模型用量（请求级日志自 2026-09-26 起记录）</div>
   <div class="note">横向堆叠：<b>Prompt</b> + <b>Completion</b> = 总 Token；点击图例可切换序列，悬浮看 pt/ct/tt 明细。</div></div>
 </section>
 <section class="panel c12" data-key="timeline">
  <div class="ph"><h3>Token 时间线</h3><span class="tag">dataZoom 可拖拽缩放 · 滚轮缩放</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody"><div class="chart h300" id="chart-timeline" data-chart="1"></div><div class="empty">日志期内无按小时 Token 记录</div></div>
 </section>
 <section class="panel c7" data-key="acctmodel">
  <div class="ph"><h3>账号 × 模型 Token 堆叠</h3><span class="tag">每账号一条</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody"><div class="chart h300" id="chart-acctmodel" data-chart="1"></div><div class="empty">日志期内无账号级 token 记录</div></div>
 </section>
 <section class="panel c5" data-key="quota">
  <div class="ph"><h3>账号配额 / 用量</h3><span class="tag">上游 usage 三窗口</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody"><div class="chart h300" id="chart-quota" data-chart="1"></div><div class="empty">上游 usage 不可用或未配置号池账号</div>
   <div class="note">柱长 = 已用百分比（上游仅返回 percent/status/resetsAt，无绝对 used/limit）；悬浮显示状态与重置时间。</div></div>
 </section>
 <section class="panel c6" data-key="sess">
  <div class="ph"><h3>会话 / 钉号分布</h3><span class="tag">Top 会话矩树 · 面积=Token</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody"><div class="chart h280" id="chart-sesstreemap" data-chart="1"></div><div class="empty">暂无带 sess 字段的请求聚合</div>
   <div class="note">红块 = 同一会话落地 <b>多个账号</b>（粘性偏差 / 钉号漂移告警）。</div></div>
 </section>
 <section class="panel c6" data-key="acctsess">
  <div class="ph"><h3>账号 ↔ 会话数</h3><span class="tag">本地对齐 / 外网调用</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody"><div class="chart h280" id="chart-acctsess" data-chart="1"></div><div class="empty">暂无账号-会话对齐数据</div></div>
 </section>
 <section class="panel c6" data-key="cache">
  <div class="ph"><h3>缓存命中 · 按小时</h3><span class="tag">柱 = pt/cached · 线 = 命中率%</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody"><div class="chart h280" id="chart-cache" data-chart="1"></div><div class="empty">暂无带 cached 字段的请求级日志</div></div>
 </section>
 <section class="panel c6" data-key="latency">
  <div class="ph"><h3>模型延迟与吞吐</h3><span class="tag">柱 = 中位/P90 · 线 = 输出 tok/s</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody"><div class="chart h280" id="chart-latency" data-chart="1"></div><div class="empty">暂无耗时样本（ts−t0 需请求级日志）</div></div>
 </section>
 <section class="panel c6" data-key="src">
  <div class="ph"><h3>来源分布 · 本地 vs 外网</h3><span class="tag">按小时堆叠</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody"><div class="chart h210" id="chart-src" data-chart="1"></div><div class="empty">暂无按小时来源数据</div>
   <div class="note" id="srcNote"></div></div>
 </section>
 <section class="panel c6" data-key="conc">
  <div class="ph"><h3>账号并发分布</h3><span class="tag">区间重叠扫描</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody"><div class="chart h210" id="chart-conc" data-chart="1"></div><div class="empty">暂无并发区间样本</div>
   <div class="chips" id="concChips" style="margin-top:6px"></div></div>
 </section>
 <section class="panel c12" data-key="hours">
  <div class="ph"><h3>请求趋势 · 按小时（容器日志）</h3><span class="tag">柱 = 全部 · 线 = 失败(≥400)</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody"><div class="chart h180" id="chart-hours" data-chart="1"></div><div class="empty">容器日志窗口内无请求</div></div>
 </section>
 <section class="panel c12" data-key="recent">
  <div class="ph"><h3>错误 / 慢请求明细</h3>
   <input type="search" id="recQ" placeholder="筛选 路径 / 状态码…" style="background:#1d2230;color:var(--txt);border:1px solid var(--line2);border-radius:7px;padding:3px 8px;font-size:12px;width:190px">
   <select id="recOnly" style="background:#1d2230;color:var(--txt);border:1px solid var(--line2);border-radius:7px;padding:3px 7px;font-size:12px">
    <option value="all">全部</option><option value="err">仅失败 ≥400</option><option value="ok">仅成功 &lt;400</option></select>
   <button class="fold" title="折叠">▾</button></div>
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
<h2 class="sec">烧量路由观测面 P1–P6 <span>取数：routing-share.json / shadow-decisions.jsonl / pool-requests.jsonl / usage 快照；读不到数据的面板显示「数据未就绪」，绝不影响其余面板</span></h2>
<div class="grid" id="gridRoute">
 <section class="panel c12" data-key="alerts"><div class="ph"><h3>告警总线（8 条判定）</h3><button class="fold" title="折叠">▾</button></div>
  <div class="pbody" id="alertBar"></div></section>
 <section class="panel c6" data-key="p1"><div class="ph"><h3>P1 烧量倒计时</h3><span class="tag">T_exhaust vs T_reset</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody" id="panelP1"></div></section>
 <section class="panel c6" data-key="p2"><div class="ph"><h3>P2 目标 vs 实际份额</h3><span class="tag">1h 滚动窗</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody" id="panelP2"></div></section>
 <section class="panel c4" data-key="p3"><div class="ph"><h3>P3 影子命中率</h3><button class="fold" title="折叠">▾</button></div>
  <div class="pbody" id="panelP3"></div></section>
 <section class="panel c4" data-key="p4"><div class="ph"><h3>P4 分档分布</h3><button class="fold" title="折叠">▾</button></div>
  <div class="pbody" id="panelP4"></div></section>
 <section class="panel c4" data-key="p5"><div class="ph"><h3>P5 迁移与缓存</h3><span class="tag">G5 预算</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody" id="panelP5"></div></section>
 <section class="panel c12" data-key="p6"><div class="ph"><h3>P6 门禁与健康</h3><button class="fold" title="折叠">▾</button></div>
  <div class="pbody" id="panelP6"></div></section>
</div>
<h2 class="sec">账号详情 <span>身份 / Key 尾号 / 池内状态 / 上游三窗口限额（percent + 重置时间）/ 池内实际用量</span></h2>
<div class="cards2" id="acctDetail"></div>
<h2 class="sec">用量详情 <span>请求级日志聚合（pool-requests.jsonl，自 2026-09-26 起）· 点击表头排序</span></h2>
<div class="grid"><section class="panel c12" data-key="usage">
 <div class="ph"><h3>账号份额与用量</h3><span class="tag">含近 24h 份额</span><button class="fold" title="折叠">▾</button></div>
 <div class="pbody"><div class="chips" id="acctShareChips" style="margin-bottom:6px"></div>
  <div class="note" id="usageNote">份额按累计 Token 计算。</div>
  <div class="scroll" style="max-height:340px;margin-top:6px"><table class="sortable" id="usageTable">
   <thead><tr><th data-sk="account">账号</th><th data-sk="tail">Key 尾号</th><th class="num" data-sk="hits">请求</th><th class="num" data-sk="pt">Prompt</th>
   <th class="num" data-sk="ct">Completion</th><th class="num" data-sk="tt">总 Token</th><th class="num" data-sk="h24_tt">近24h Token</th>
   <th class="num" data-sk="h24_pct">近24h占比</th><th>主要模型（Top3）</th><th data-sk="lastTs">最近活动</th></tr></thead>
   <tbody id="acctShare"></tbody></table></div></div>
</section></div>
<h2 class="sec">账号 · 会话分配 <span>请求/Token 日志自 2026-09-26 起 · 会话按 assistant 时间 ±8s 对齐</span></h2>
<div class="grid"><section class="panel c12" data-key="acctSessions">
 <div class="ph"><h3>账号 → 服务过的会话</h3><span class="tag" id="sessTag"></span><button class="fold" title="折叠">▾</button></div>
 <div class="pbody"><div id="acctSessions"></div></div>
</section></div>
<h2 class="sec">运维 · 模型同步与 Agent Teams <span>写 dsh 配置（自动备份 + YAML 校验）→ 重启 dsh-desktop 生效</span></h2>
<div class="grid">
 <section class="panel c6" data-key="prov">
  <div class="ph"><h3>模型选择 · 按 API 类型同步到 DSH</h3><button class="fold" title="折叠">▾</button></div>
  <div class="pbody" id="provsel"></div>
 </section>
 <section class="panel c6" data-key="teams">
  <div class="ph"><h3>Agent Teams · 团队模型规则</h3><button class="fold" title="折叠">▾</button></div>
  <div class="pbody">
   <div class="prov" id="teamsScalar"><div class="ph2"><span class="pname">全局成员默认</span>
    <label style="display:flex;align-items:center;gap:6px;font-size:12px">memberModel <select id="tcMM"></select></label>
    <label style="display:flex;align-items:center;gap:6px;font-size:12px">maxMembers <input id="tcMX" type="number" min="1" max="64" style="width:62px"></label>
    <label style="display:flex;align-items:center;gap:6px;font-size:12px">memberMaxDepth <input id="tcMD" type="number" min="0" max="3" style="width:62px"></label>
   </div></div>
   <div id="teamsBox"></div>
   <div style="display:flex;align-items:center;gap:10px;margin-top:10px;flex-wrap:wrap">
    <button id="teamAddProf">+ 新增模板</button>
    <button id="teamsSave" class="primary">保存团队规则并重启</button>
    <span class="msg" id="teamsMsg"></span>
   </div>
  </div>
 </section>
</div>
<div class="grid" style="margin-top:11px">
 <section class="panel c12" data-key="sync">
  <div class="ph"><h3>写入 DSH 配置</h3><span class="tag">backup + YAML 校验 + 重启</span><button class="fold" title="折叠">▾</button></div>
  <div class="pbody">
   <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <button id="syncBtn" class="primary">同步模型到 DSH 并重启服务</button>
    <span class="msg" id="syncMsg"></span>
   </div>
   <div class="note">模板用 <span class="mono">/agent-teams --profile 名称</span> 调用；同步约 15 秒后生效（dsh-desktop 重启）。</div>
  </div>
 </section>
</div>
<div class="note" id="refresh" style="margin-top:14px"></div>
<script src="vendor/echarts.min.js"></script>
<script>
/* =========================================================================
   号池看板 v12（OpencodeLitellmPool）：界面全部走 ECharts 本地 vendored 5.x
   - 零构建零 CDN：ECharts 以相对路径 vendor/echarts.min.js 引入、fetch("api/...") 亦为相对路径，
     因此经 https://<域名>/works/pool/ 反代前缀访问时不会 404。
   - 只消费既有 JSON 接口：api/summary / api/teams-config / api/models-config /
     api/account-sessions / api/sync-models(POST) / api/save-teams-config(POST)。
   - 空数据/缺字段一律走 setEmpty() 占位，绝不抛错白屏。
   ========================================================================= */
var SUM=null,SESSD=null,TC=null,MODELS=[],SEL={},RT=null;
var INST={},FOLDED={},REC={q:"",only:"all",sort:"ts",dir:-1},SLOW={sort:"p90Ms",dir:-1};
var STATE={range:"all",iv:15000,auto:true};
var PAL=["#60a5fa","#4ade80","#fbbf24","#f472b6","#a78bfa","#22d3ee","#fb923c","#94a3b8","#34d399","#e879f9","#facc15","#f87171"];
function $(id){return document.getElementById(id)}
function esc(s){return String(s==null?"":s).split("&").join("&amp;").split("<").join("&lt;").split(">").join("&gt;").split('"').join("&quot;")}
function fmtInt(n){return (Math.round(+n||0)).toLocaleString("en-US")}
function fmtTok(n){n=+n||0;var a=Math.abs(n);
 if(a>=1e12)return (n/1e12).toFixed(2)+"T";if(a>=1e9)return (n/1e9).toFixed(2)+"G";
 if(a>=1e6)return (n/1e6).toFixed(2)+"M";if(a>=1e3)return (n/1e3).toFixed(1)+"k";return String(n)}
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
          {k:"cache",n:"缓存命中",c:"#22d3ee"},{k:"acct",n:"活跃账号",c:"#fbbf24"},{k:"models",n:"当前模型数",c:"#f472b6"}];
var DETAIL={};
function statusParts(){
 var bs=obj(SUM&&SUM.by_status),o={ok:0,redir:0,c4:0,c5:0,other:0},tot=0;
 Object.keys(bs).forEach(function(k){var n=num(bs[k]),c=num(k);tot+=n;
  if(c>=500)o.c5+=n;else if(c>=400)o.c4+=n;else if(c>=300)o.redir+=n;else if(c>=200)o.ok+=n;else o.other+=n});
 o.tot=tot;return o}
function renderKpis(){
 var d=obj(SUM),ut=obj(d.usage_totals),cr=obj(obj(d.cache_rate).overall),st=statusParts();
 var byAcct=arr(d.by_account),lim=num(d.accounts_limited);
 var okPct=st.tot?((st.tot-st.c4-st.c5)*100/st.tot):0;
 var vals={
  total:{v:fmtInt(d.total),s:"失败 "+fmtInt(st.c4+st.c5)+" · 窗口 "+fmtInt(st.tot),cls:(st.c4+st.c5)>0?"warn":""},
  success:{v:(st.tot?okPct.toFixed(1):"—")+(st.tot?"%":""),s:st.tot?("2xx "+fmtInt(st.ok)+" · 3xx "+fmtInt(st.redir)):"暂无请求",cls:st.tot?(okPct>=99?"ok":okPct>=95?"warn":"err"):"dimv"},
  tt:{v:fmtTok(ut.tt),s:"P "+fmtTok(ut.pt)+" / C "+fmtTok(ut.ct),cls:""},
  cache:{v:num(cr.rate).toFixed(1)+"%",s:"cached "+fmtTok(cr.cached)+" / pt "+fmtTok(cr.pt),cls:num(cr.rate)>=30?"ok":num(cr.rate)>0?"warn":"dimv"},
  acct:{v:fmtInt(byAcct.length)+" / "+fmtInt(d.accounts),s:"受限 "+fmtInt(lim)+" · 部署 "+fmtInt(d.deployments),cls:lim>0?"err":""},
  models:{v:fmtInt(d.models),s:"配置 deployments "+fmtInt(d.deployments),cls:""}};
 $("kpis").innerHTML=KPIS.map(function(x){
  var v=vals[x.k]||{v:"-",s:""};
  return '<div class="kpi" data-k="'+x.k+'" style="--k:'+x.c+'"><div class="k">'+x.n+'</div>'+
   '<div class="v '+(v.cls||"")+'">'+v.v+'</div><div class="s">'+v.s+'</div></div>'}).join("");
 var topM=arr(d.by_model).slice(0,3).map(function(m){return m.model+" · "+fmtTok(m.tokens)}).join("<br/>")||"—";
 var topA=byAcct.slice(0,3).map(function(a){return a.account+" · "+fmtTok(a.tt)}).join("<br/>")||"—";
 var cm=arr(obj(d.cache_rate).by_model).slice(0,3).map(function(m){return m.model+" · "+num(m.rate).toFixed(1)+"%"}).join("<br/>")||"—";
 DETAIL={
  total:'<b>请求总数 '+fmtInt(d.total)+'</b><div class="r"><i>容器日志窗口</i><span>docker logs --tail 3000</span></div>'+
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
  cache:'<b>整体命中 '+num(cr.rate).toFixed(1)+'%</b><div class="r"><i>cached</i><span>'+fmtInt(cr.cached)+'</span></div>'+
   '<div class="r"><i>prompt 分母</i><span>'+fmtInt(cr.pt)+'</span></div><div class="r"><i>按模型 Top3</i><span style="text-align:right">'+cm+'</span></div>',
  acct:'<b>活跃账号 '+byAcct.length+' / 池内 '+fmtInt(d.accounts)+'</b><div class="r"><i>受限账号</i><span>'+fmtInt(lim)+'</span></div>'+
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
function renderStatus(){
 var el=$("chart-status");if(!el||!window.echarts)return;
 var c=INST["chart-status"]||(INST["chart-status"]=echarts.init(el));
 var bs=obj(SUM&&SUM.by_status),ent=Object.keys(bs).map(function(k){return [String(k),num(bs[k])]}).filter(function(x){return x[1]>0});
 ent.sort(function(a,b){return b[1]-a[1]});
 var tot=ent.reduce(function(s,x){return s+x[1]},0);
 setEmpty("chart-status",!ent.length,"暂无请求记录（容器日志窗口内无请求）");
 c.setOption({animationDuration:400,tooltip:tipItem({formatter:function(p){
   return "<b>"+esc(p.name)+"</b><br/>次数 "+fmtInt(p.value)+"<br/>占比 "+(tot?(p.value*100/tot).toFixed(1):"0")+"%"}}),
  legend:{show:false},
  title:{text:fmtInt(tot),subtext:"总请求",left:"50%",top:"38%",textAlign:"center",
   textStyle:{color:"#e6e9ef",fontSize:22,fontWeight:700},subtextStyle:{color:"#8b93a3",fontSize:11}},
  series:[{type:"pie",radius:["54%","78%"],center:["50%","52%"],avoidLabelOverlap:true,
   itemStyle:{borderColor:"#161a21",borderWidth:2},label:{show:false},labelLine:{show:false},
   emphasis:{scale:true,scaleSize:5,itemStyle:{shadowBlur:12,shadowColor:"rgba(0,0,0,.5)"}},
   data:ent.map(function(x){return {name:x[0],value:x[1],itemStyle:{color:colorOfCode(x[0])}}})}]},true);
}
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
  xAxis:Object.assign({type:"value",axisLabel:{color:"#8b93a3",fontSize:10.5,formatter:function(v){return fmtTok(v)}}},AX),
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
  yAxis:Object.assign({type:"value",axisLabel:{color:"#8b93a3",fontSize:10.5,formatter:function(v){return fmtTok(v)}}},AX),
  dataZoom:[{type:"inside",throttle:40},{type:"slider",height:14,bottom:6,borderColor:"#232833",
   backgroundColor:"#12151c",fillerColor:"rgba(96,165,250,.16)",handleSize:"90%",textStyle:{color:"#8b93a3",fontSize:10}}],
  series:[L("总 Token","#60a5fa","tt",{areaStyle:{color:"rgba(96,165,250,.16)"}}),
          L("Prompt","#22d3ee","pt"),L("缓存命中","#4ade80","cached")]},true);
}
/* ---------------- 图表 4：账号 × 模型 Token 堆叠 ---------------- */
function accountLabel(a){return a.account&&a.account!=="***@***"?(a.account+(a.tail?" ·…"+a.tail:"")):(a.tail?"…"+a.tail:"未知账号")}
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
   var a=accts[ps[0].dataIndex]||{};var out="<b>"+esc(a.account||a.tail)+"</b><br/>总 Token <b>"+fmtInt(a.tt)+"</b> · 请求 "+fmtInt(a.hits)+" 次<hr style='border:none;border-top:1px solid #2c3342;margin:4px 0'/>";
   ps.forEach(function(p){if(num(p.value)>0)out+="<span style='display:inline-block;width:8px;height:8px;border-radius:2px;background:"+p.color+";margin-right:5px'></span>"+esc(p.seriesName)+" "+fmtInt(p.value)+"<br/>"});
   return out}}),
  legend:legendOpt({type:"scroll",top:2,data:series.map(function(s){return s.name})}),
  grid:gridOpt(46,4,6,10),
  xAxis:Object.assign({type:"category",data:labels,axisLabel:{color:"#c9d1e0",fontSize:10.5,rotate:22,width:150,overflow:"truncate"}},AX),
  yAxis:Object.assign({type:"value",axisLabel:{color:"#8b93a3",fontSize:10.5,formatter:function(v){return fmtTok(v)}}},AX),
  series:series},true);
}
/* ---------------- 图表 5：账号配额 / 用量 ---------------- */
function winOf(a,w){return obj(obj(a.windows)[w])}
function renderQuota(){
 var el=$("chart-quota");if(!el||!window.echarts)return;
 var c=INST["chart-quota"]||(INST["chart-quota"]=echarts.init(el));
 var rows=arr(obj(SUM).account_usage);
 var WINS=[["rolling","滚动"],["weekly","每周"],["monthly","每月"]];
 setEmpty("chart-quota",!rows.length,"上游 usage 不可用或未配置号池账号");
 var labels=rows.map(function(a){return a.label||a.key||a.tail||"账号"});
 var series=WINS.map(function(w,i){
  return {name:w[1],type:"bar",barMaxWidth:9,emphasis:{focus:"series"},itemStyle:{color:PAL[i%PAL.length]},
   data:rows.map(function(a){var v=winOf(a,w[0]);return v.percent==null?null:num(v.percent)}),
   markLine:{silent:true,symbol:"none",lineStyle:{color:"#f87171",type:"dashed",width:1},
    label:{show:false},data:[{xAxis:100}]}}});
 c.setOption({animationDuration:400,
  tooltip:tipAxis({axisPointer:{type:"shadow"},formatter:function(ps){
   var a=rows[ps[0].dataIndex]||{};
   var out="<b>"+esc(a.label||a.key)+"</b> <span style='color:#8b93a3'>…"+esc(a.tail||"")+"</span><br/>"+
    "池内 "+(a.inPool?"<span style='color:#4ade80'>有部署</span>":"<span style='color:#fbbf24'>已剔除</span>")+
    (a.slot?" · 池位 "+esc(a.slot):"")+"<hr style='border:none;border-top:1px solid #2c3342;margin:4px 0'/>";
   WINS.forEach(function(w){
    var v=winOf(a,w[0]);
    out+="<span style='color:#8b93a3'>"+w[1]+"</span> "+(v.percent==null?"—":num(v.percent)+"%")+
     " · "+(v.status?esc(v.status):"unknown")+(v.resetsAt?" · "+fmtTs(new Date(v.resetsAt).getTime())+"（"+rel(v.resetsAt)+"）":"")+"<br/>"});
   return out}}),
  legend:legendOpt({data:WINS.map(function(w){return w[1]})}),
  grid:gridOpt(30,4,6,10),
  xAxis:Object.assign({type:"value",max:100,axisLabel:{color:"#8b93a3",fontSize:10.5,formatter:"{value}%"}},AX),
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
/* ---------------- 图表 7：账号 ↔ 会话数 ---------------- */
function renderAcctSess(){
 var el=$("chart-acctsess");if(!el||!window.echarts)return;
 var c=INST["chart-acctsess"]||(INST["chart-acctsess"]=echarts.init(el));
 var rows=arr(obj(SESSD).accounts).slice(0,12);
 setEmpty("chart-acctsess",!rows.length,"暂无账号-会话对齐数据");
 var lab=[],loc=[],ext=[],hits=[];
 rows.forEach(function(a){
  var ss=arr(a.sessions),e=0,l=0;
  ss.forEach(function(s){if(s&&s.ext)e++;else l++});
  lab.push(a.account||a.tail);loc.push(l);ext.push(e);hits.push(num(a.hits))});
 c.setOption({animationDuration:400,
  tooltip:tipAxis({formatter:function(ps){var a=rows[ps[0].dataIndex]||{};
   return "<b>"+esc(a.account||a.tail)+"</b><br/>会话 "+fmtInt(loc[ps[0].dataIndex]+ext[ps[0].dataIndex])+
    " 个（本地 "+fmtInt(loc[ps[0].dataIndex])+" / 外网 "+fmtInt(ext[ps[0].dataIndex])+"）<br/>请求 "+fmtInt(a.hits)+
    " 次<br/>Token <b>"+fmtInt(a.tokensTotal)+"</b>（"+fmtTok(a.tokensTotal)+"）"}}),
  legend:legendOpt({data:["本地会话","外网调用","请求数"]}),
  grid:gridOpt(30,4,6,10),
  xAxis:Object.assign({type:"category",data:lab,axisLabel:{color:"#c9d1e0",fontSize:10.5,rotate:22,width:120,overflow:"truncate"}},AX),
  yAxis:[Object.assign({type:"value",name:"会话",nameTextStyle:{color:"#8b93a3",fontSize:10}},AX),
         Object.assign({type:"value",name:"请求",nameTextStyle:{color:"#8b93a3",fontSize:10},splitLine:{show:false}},AX)],
  series:[{name:"本地会话",type:"bar",stack:"s",barMaxWidth:22,itemStyle:{color:"#60a5fa"},data:loc},
          {name:"外网调用",type:"bar",stack:"s",barMaxWidth:22,itemStyle:{color:"#fbbf24"},data:ext},
          {name:"请求数",type:"line",yAxisIndex:1,smooth:true,symbolSize:5,lineStyle:{color:"#4ade80",width:1.6},itemStyle:{color:"#4ade80"},data:hits}]},true);
}
/* ---------------- 图表 8：缓存命中（双轴） ---------------- */
function renderCache(){
 var el=$("chart-cache");if(!el||!window.echarts)return;
 var c=INST["chart-cache"]||(INST["chart-cache"]=echarts.init(el));
 var cr=obj(obj(SUM).cache_rate),rows=fHours(cr.by_hour);
 setEmpty("chart-cache",!rows.length,"暂无带 cached 字段的请求级日志");
 var x=rows.map(function(r){return hLabel(r.h)});
 c.setOption({animationDuration:400,
  tooltip:tipAxis({formatter:function(ps){var r=rows[ps[0].dataIndex]||{};
   return "<b>"+esc(r.h)+"</b><br/>prompt "+fmtInt(r.pt)+"<br/>cached "+fmtInt(r.cached)+
    "<br/>命中率 <b>"+num(r.rate).toFixed(1)+"%</b>"}}),
  legend:legendOpt({data:["prompt","cached","命中率"]}),
  grid:gridOpt(30,4,6,10),
  xAxis:Object.assign({type:"category",data:x,axisLabel:{color:"#8b93a3",fontSize:10,hideOverlap:true}},AX),
  yAxis:[Object.assign({type:"value",axisLabel:{color:"#8b93a3",fontSize:10.5,formatter:function(v){return fmtTok(v)}}},AX),
         Object.assign({type:"value",max:100,axisLabel:{color:"#22d3ee",fontSize:10.5,formatter:"{value}%"},splitLine:{show:false}},AX)],
  series:[{name:"prompt",type:"bar",stack:"p",barMaxWidth:16,itemStyle:{color:"rgba(96,165,250,.55)"},data:rows.map(function(r){return num(r.pt)})},
          {name:"cached",type:"bar",stack:"p",barMaxWidth:16,itemStyle:{color:"#22d3ee"},data:rows.map(function(r){return num(r.cached)})},
          {name:"命中率",type:"line",yAxisIndex:1,smooth:true,symbolSize:5,lineStyle:{color:"#4ade80",width:1.8},itemStyle:{color:"#4ade80"},data:rows.map(function(r){return num(r.rate)})}]},true);
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
  xAxis:Object.assign({type:"category",data:lab,axisLabel:{color:"#c9d1e0",fontSize:10.5,rotate:22,width:150,overflow:"truncate"}},AX),
  yAxis:[Object.assign({type:"value",axisLabel:{color:"#8b93a3",fontSize:10.5,formatter:function(v){return fmtMs(v)}}},AX),
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
/* ---------------- 图表 11：账号并发分布 ---------------- */
function renderConc(){
 var el=$("chart-conc");if(!el||!window.echarts)return;
 var c=INST["chart-conc"]||(INST["chart-conc"]=echarts.init(el));
 var cc=obj(obj(SUM).concurrency),rows=arr(cc.accounts).slice(0,12);
 var chips=$("concChips");
 if(chips)chips.innerHTML='<span class="chip">全局峰值 <b>'+fmtInt(cc.peak)+'</b></span>'+
  '<span class="chip">平均在途 <b>'+(+cc.avg||0).toFixed(2)+'</b></span>'+
  '<span class="chip">窗口 <b>'+(+cc.hours||0).toFixed(1)+'</b> h</span>'+
  '<span class="chip">有效请求 <b>'+fmtInt(cc.requests)+'</b></span>';
 setEmpty("chart-conc",!rows.length,"暂无并发区间样本");
 c.setOption({animationDuration:400,
  tooltip:tipAxis({formatter:function(ps){var r=rows[ps[0].dataIndex]||{};
   return "<b>"+esc(r.account||r.tail)+"</b><br/>峰值并发 <b>"+fmtInt(r.peak)+"</b><br/>平均在途 "+(+r.avg||0).toFixed(2)+
    "<br/>请求 "+fmtInt(r.hits)+" 次<br/>Token "+fmtInt(r.tt)}}),
  legend:legendOpt({data:["峰值并发","平均在途"]}),
  grid:gridOpt(30,4,6,10),
  xAxis:Object.assign({type:"category",data:rows.map(function(r){return r.account||r.tail}),
   axisLabel:{color:"#c9d1e0",fontSize:10.5,rotate:22,width:130,overflow:"truncate"}},AX),
  yAxis:[Object.assign({type:"value",name:"并发",nameTextStyle:{color:"#8b93a3",fontSize:10}},AX),
         Object.assign({type:"value",splitLine:{show:false},axisLabel:{color:"#fbbf24",fontSize:10.5}},AX)],
  series:[{name:"峰值并发",type:"bar",barMaxWidth:24,itemStyle:{color:"#a78bfa"},data:rows.map(function(r){return num(r.peak)})},
          {name:"平均在途",type:"line",yAxisIndex:1,smooth:true,symbolSize:5,lineStyle:{color:"#fbbf24",width:1.8},itemStyle:{color:"#fbbf24"},data:rows.map(function(r){return +r.avg||0})}]},true);
}
/* ---------------- 图表 12：请求趋势（柱 + 失败线） ---------------- */
function renderHours(){
 var el=$("chart-hours");if(!el||!window.echarts)return;
 var c=INST["chart-hours"]||(INST["chart-hours"]=echarts.init(el));
 var rows=fHours(obj(SUM).by_hour);
 setEmpty("chart-hours",!rows.length,"容器日志窗口内无请求");
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
  return '<span class="chip"><i class="cdot" style="background:'+PAL[i%PAL.length]+'"></i>'+esc(a.account)+' · <b>'+fmtTok(a.tt)+
   '</b> tokens（'+(num(a.tt)*100/tot).toFixed(1)+'%）</span>'}).join("")||'<span class="chip">暂无请求日志</span>';
 var note=$("usageNote");
 if(note)note.innerHTML="累计 "+fmtInt(accts.reduce(function(s,a){return s+num(a.hits)},0))+" 请求 · "+
  fmtTok(accts.reduce(function(s,a){return s+num(a.tt)},0))+" tokens · "+accts.length+" 个账号有过号池调用；"+
  "近 24h 窗口占累计 "+(accts.reduce(function(s,a){return s+num(a.h24_tt)},0)*100/tot).toFixed(1)+"%。";
 $("acctShare").innerHTML=accts.map(function(a){
  var top3=arr(a.models).slice(0,3).map(function(m){return esc(m.model)+" · "+fmtInt(m.hits)+"次 · "+fmtTok(m.tokens)}).join("，")||"—";
  return "<tr><td>"+esc(a.account)+"</td><td class='mono'>…"+esc(a.tail)+"</td><td class='num'>"+fmtInt(a.hits)+"</td>"+
   "<td class='num'>"+fmtTok(a.pt)+"</td><td class='num'>"+fmtTok(a.ct)+"</td><td class='num'><b>"+fmtTok(a.tt)+"</b></td>"+
   "<td class='num'>"+fmtTok(a.h24_tt)+"</td><td class='num'>"+num(a.h24_pct).toFixed(1)+"%</td><td>"+top3+"</td>"+
   "<td>"+(a.lastTs?fmtTs(a.lastTs):"—")+"</td></tr>"}).join("")||
  "<tr><td colspan='10' style='color:#8b93a3'>暂无请求日志</td></tr>"}
/* ---------------- 账号详情卡（数值展示；配额图见 ECharts 面板） ---------------- */
function renderAcctDetail(){
 var el=$("acctDetail");if(!el)return;
 var usage=arr(obj(SUM).account_usage),usg={};
 arr(obj(SUM).by_account).forEach(function(a){usg[a.tail]=a});
 var sess={};if(SESSD)arr(SESSD.accounts).forEach(function(a){arr(a.tails&&a.tails.length?a.tails:[a.tail]).forEach(function(t){if(t)sess[t]=a})});
 el.innerHTML=usage.map(function(a){
  var label=a.label||a.key,u=usg[a.tail]||null,s=sess[a.tail]||null;
  var pool=a.inPool?'<span class="badge on">在池 · 有部署</span>':'<span class="badge off">已剔除 · 无限额部署</span>';
  var stCls=a.state==="ok"?"on":"bad";
  var stTxt=a.state==="limited"?("受限（"+arr(a.limited).join("/")+"）"):a.state==="error"?"查询失败":"正常";
  var wins=[["Rolling","rolling"],["Weekly","weekly"],["Monthly","monthly"]].map(function(w){
   var v=obj(obj(a.windows)[w[1]]);
   if(v.percent==null)return '<div class="win"><div class="wn">'+w[0]+'</div><div class="wv dimv">—</div><div class="st dimv">无数据</div><div class="rst"></div></div>';
   var p=Math.max(0,Math.min(100,num(v.percent))),lim=v.status==="rate-limited";
   var abs=v.resetsAt?fmtTs(new Date(v.resetsAt).getTime()):"";
   return '<div class="win"><div class="wn">'+w[0]+'</div><div class="wv" style="color:'+(lim?"#f87171":p>=70?"#fbbf24":"#4ade80")+'">'+Math.round(p)+'%</div>'+
    '<div class="st '+(lim?"err":"ok")+'">'+esc(v.status||"unknown")+'</div><div class="rst">'+(abs?abs+"<br/>"+rel(v.resetsAt):"—")+'</div></div>'}).join("");
  var chips=u&&arr(u.models).length?arr(u.models).slice(0,4).map(function(m,i){
   return '<span class="chip" title="'+esc(m.model)+'：'+fmtInt(m.hits)+' 次 · '+fmtTok(m.tokens)+' tokens"><i class="cdot" style="background:'+
    PAL[i%PAL.length]+'"></i>'+esc(m.model)+' · '+fmtInt(m.hits)+'次 · '+fmtTok(m.tokens)+'</span>'}).join("")
   :'<span class="chip">日志期内无请求</span>';
  var st=function(v,k,c){return '<div class="stat"><div class="sv '+(c||"")+'">'+v+'</div><div class="sk">'+k+'</div></div>'};
  return '<div class="det"><div class="det-head"><span class="det-name">'+esc(label)+'</span>'+
   (a.slot?'<span class="badge" title="号池槽位编号">池位 '+esc(a.slot)+'</span>':'')+
   (a.src==="placeholder"?'<span class="badge off" title="邮箱待补：可在 /opt/pool-dash/account-emails.json 填写 KEY→邮箱 映射">邮箱待补</span>':'')+
   '<span class="mono" style="font-size:11px;color:#8b93a3" title="API Key 尾号；完整 Key 只存于 /opt/litellm/.env（600）">…'+esc(a.tail||"-")+'</span>'+pool+
   '<span class="badge '+stCls+'" style="margin-left:auto">'+stTxt+'</span></div>'+
   '<div class="det-meta"><span>上游 usage：'+(a.ok?"成功":"失败 "+esc(a.error||""))+'</span>'+
   (u?'<span>日志期内 '+fmtInt(u.hits)+' 请求 · '+fmtTok(u.tt)+' tokens</span>':'<span>日志期内无请求</span>')+
   (s?'<span>对齐会话 '+arr(s.sessions).length+' 个</span>':'')+'</div>'+
   '<div class="wins">'+wins+'</div><div class="statgrid">'+
   st(u?fmtInt(u.hits):"0","池内请求")+st(fmtTok(u?u.tt:0),"总 Token")+st(fmtTok(u?u.pt:0),"Prompt")+st(fmtTok(u?u.ct:0),"Completion")+
   st(s?arr(s.sessions).length:"—","服务会话")+'<div class="stat" title="'+(u&&u.lastTs?fmtTs(u.lastTs):"")+'"><div class="sv sm">'+
   (u&&u.lastTs?fmtTsShort(u.lastTs):"—")+'</div><div class="sk">最近活动</div></div></div>'+
   '<div class="chips" style="margin-top:8px">'+chips+'</div></div>'}).join("")||
  '<div class="det" style="color:#8b93a3">无账号（/opt/litellm/.env 中没有配置号池 Key）</div>'}
/* ---------------- 账号 · 会话分配明细 ---------------- */
async function loadAcctSessions(){
 try{
  var d=await (await fetch("api/account-sessions",{cache:"no-store"})).json();
  SESSD=d;renderAcctDetail();renderAcctSess();
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
   return '<div class="prov"><div class="ph2"><span class="papi">#'+(ai+1)+'</span><span class="pname">'+esc(a.account)+'</span>'+
    '<span class="chip">请求 <b>'+fmtInt(a.hits)+'</b></span><span class="chip">共 <b>'+fmtTok(total)+'</b> tokens</span>'+
    (num(a.refs)>1?'<span class="chip" title="同一邮箱在日志中有多种 key 写法，已合并">合并'+fmtInt(a.refs)+'种记录</span>':'')+
    '<span class="chip">▲ = 额度消耗主力模型</span></div>'+
    '<div class="chips" style="margin-bottom:6px">'+models+'</div>'+
    '<div class="scroll" style="max-height:260px"><table><thead><tr><th>会话（标题 / 首条消息）</th><th>模型</th><th>创建时间</th><th>最后活跃</th><th>命中</th></tr></thead><tbody>'+
    rows+'</tbody></table></div></div>'}).join("")+
   (arr(d.orphans).length?'<div class="prov"><div class="ph2"><span class="pname" style="color:#fbbf24">未归属会话</span>'+
    '<span class="chip">日志期内有 LLM 调用但未对齐到账号</span></div>'+
    arr(d.orphans).map(function(s){return "<div style='font-size:11.5px;color:#8b93a3;margin:3px 0'>"+esc(s.title)+" — "+esc(s.firstMsg||"")+
     "（"+esc(s.model||"?")+" · 活跃 "+fmtTs(s.lastActive)+"）</div>"}).join("")+"</div>":"");
 }catch(e){$("acctSessions").innerHTML='<div class="err" style="font-size:12px">加载失败: '+esc(e)+'</div>'}
}
/* ---------------- 路由观测面 P1–P6 ---------------- */
function renderP2(){
 var el=$("chart-p2");if(!el||!window.echarts||!el.offsetWidth)return;
 var c=INST["chart-p2"]||(INST["chart-p2"]=echarts.init(el));
 var p2=obj(obj(obj(RT).panels).p2_share),rows=arr(p2.accounts).slice(0,12);
 c.setOption({animationDuration:300,
  tooltip:tipItem({formatter:function(p){var r=rows[p.dataIndex]||{};
   return "<b>"+esc(r.key_ref)+"</b><br/>"+p.seriesName+" "+(num(p.value)*100).toFixed(1)+"%<br/>偏差 "+(+r.dev_pp||0)+" pp（上一窗 "+(+r.dev_prev_pp||0)+" pp）"}}),
  legend:legendOpt({data:["目标","实际"]}),
  grid:gridOpt(28,4,6,10),
  xAxis:Object.assign({type:"value",max:1,axisLabel:{color:"#8b93a3",fontSize:10.5,formatter:function(v){return (v*100).toFixed(0)+"%"}}},AX),
  yAxis:Object.assign({type:"category",data:rows.map(function(r){return r.key_ref}),axisLabel:{color:"#c9d1e0",fontSize:10.5,width:130,overflow:"truncate"}},AX),
  series:[{name:"目标",type:"bar",barMaxWidth:12,itemStyle:{color:"#60a5fa"},data:rows.map(function(r){return num(r.target)})},
          {name:"实际",type:"bar",barMaxWidth:12,itemStyle:{color:"#4ade80"},data:rows.map(function(r){return num(r.actual)})}]},true);
}
function renderP4(){
 var el=$("chart-p4");if(!el||!window.echarts||!el.offsetWidth)return;
 var c=INST["chart-p4"]||(INST["chart-p4"]=echarts.init(el));
 var p4=obj(obj(obj(RT).panels).p4_class),dist=obj(p4.dist);
 var data=Object.keys(dist).map(function(k,i){return {name:k,value:num(dist[k]),itemStyle:{color:PAL[i%PAL.length]}}});
 c.setOption({animationDuration:300,tooltip:tipItem(),legend:{show:false},
  series:[{type:"pie",radius:["46%","72%"],center:["50%","52%"],label:{color:"#c9d1e0",fontSize:10.5,formatter:"{b} {c}"},
   itemStyle:{borderColor:"#161a21",borderWidth:2},data:data}]},true);
}
function renderRouting(rt){
 RT=rt||RT;
 var NR='<div style="color:#8b93a3;font-size:12px;padding:4px 0">数据未就绪</div>';
 var bar=$("alertBar");
 if(!rt){if(bar)bar.innerHTML=NR;["panelP1","panelP2","panelP3","panelP4","panelP5","panelP6"].forEach(function(id){var e=$(id);if(e)e.innerHTML=NR});return}
 var firing=arr(rt.alerts).filter(function(a){return a.firing});
 if(bar)bar.innerHTML=firing.length?firing.map(function(a){
   var col=a.level==="crit"?"#f87171":"#fbbf24";
   return '<div class="chip" style="display:block;margin:4px 0;border-color:'+col+';color:'+col+'">['+esc(a.id)+'] '+esc(a.name)+
    ' — '+esc(a.detail)+'<span style="color:#8b93a3;margin-left:8px">判定：'+esc(a.expr)+'</span></div>'}).join("")
  :'<div style="color:#4ade80;font-size:12px">✓ 8 条告警均未触发</div>';
 var P=obj(rt.panels);
 disposeCharts(["chart-p2","chart-p4"]);
 var p1=obj(P.p1_burn),e1=$("panelP1");
 if(e1)e1.innerHTML=(!p1.ready)?NR:
  '<table><thead><tr><th>账号</th><th class="num">剩余%</th><th class="num">T_reset</th><th class="num">T_exhaust</th>'+
  '<th class="num">实测烧量 $/天</th><th class="num">pace $/天</th><th>状态</th></tr></thead><tbody>'+
  arr(p1.rows).map(function(x){
   var st=x.state==="danger"?'<span class="chip" style="border-color:#f87171;color:#f87171">打穿风险</span>':
     x.state==="idle"?'<span class="chip" style="border-color:#fbbf24;color:#fbbf24">闲置</span>':'<span class="chip">正常</span>';
   return '<tr><td class="mono">'+esc(x.key_ref)+'</td><td class="num">'+esc(x.rem_pct)+'</td><td class="num">'+fmtDur(x.t_reset_s)+
    '</td><td class="num">'+(x.t_exhaust_s==null?"∞":fmtDur(x.t_exhaust_s))+'</td><td class="num">'+esc(x.burn_usd_day)+
    '</td><td class="num">'+esc(x.pace_usd_day)+'</td><td>'+st+'</td></tr>'}).join("")+'</tbody></table>';
 var p2=obj(P.p2_share),e2=$("panelP2");
 if(e2)e2.innerHTML=(!p2.ready)?NR:((p2.has_actual?"":'<div style="color:#8b93a3;font-size:11.5px">近 1h 无请求样本（实际份额待数据）</div>')+
  '<div class="chart h180" id="chart-p2" data-chart="1" style="margin-top:6px"></div>');
 if(p2.ready)renderP2();
 var p3=obj(P.p3_shadow),e3=$("panelP3");
 if(e3)e3.innerHTML=(!p3.ready)?NR:'<div class="statgrid">'+
  '<div class="stat"><div class="sv">'+(p3.hit_rate==null?"—":(num(p3.hit_rate)*100).toFixed(1)+"%")+'</div><div class="sk">命中率</div></div>'+
  '<div class="stat"><div class="sv">'+fmtInt(p3.matched)+"/"+fmtInt(p3.comparable)+'</div><div class="sk">命中/可比</div></div>'+
  '<div class="stat"><div class="sv">'+fmtInt(p3.records)+'</div><div class="sk">影子记录</div></div></div>';
 var p4=obj(P.p4_class),e4=$("panelP4");
 if(e4)e4.innerHTML=(!p4.ready)?NR:'<div class="chart h150" id="chart-p4" data-chart="1"></div>'+
  '<div class="chips" style="margin-top:6px">'+Object.keys(obj(p4.dist)).map(function(k){
   return '<span class="chip">'+esc(k)+': <b>'+fmtInt(obj(p4.dist)[k])+'</b></span>'}).join("")+'</div>';
 if(p4.ready)renderP4();
 var p5=obj(P.p5_repin),e5=$("panelP5");
 if(e5)e5.innerHTML=(!p5.ready)?NR:'<div class="statgrid">'+
  '<div class="stat"><div class="sv">'+fmtInt(p5.repin_last_min)+"/"+(p5.budget_per_min==null?"—":fmtInt(p5.budget_per_min))+'</div><div class="sk">重钉 次/min</div></div>'+
  '<div class="stat"><div class="sv '+(p5.cold_budget_pct!=null&&num(p5.cold_budget_pct)>=80?"err":"")+'">'+
   (p5.cold_budget_pct==null?"—":esc(p5.cold_budget_pct)+"%")+'</div><div class="sk">冷预算 '+fmtInt(p5.cold_last_5h)+"/"+
   (p5.cold_budget_per_5h==null?"—":fmtInt(p5.cold_budget_per_5h))+'（5h）</div></div>'+
  '<div class="stat"><div class="sv">'+(p5.cache_rate_1h==null?"—":(num(p5.cache_rate_1h)*100).toFixed(1)+"%")+'</div><div class="sk">warm 1h（基线 '+
   (p5.cache_baseline_24h==null?"—":(num(p5.cache_baseline_24h)*100).toFixed(1)+"%")+'）</div></div></div>'+
  (p5.note?'<div class="note">'+esc(p5.note)+'</div>':"");
 var p6=obj(P.p6_health),e6=$("panelP6");
 if(e6)e6.innerHTML=(!p6.ready)?NR:'<div class="chips">'+
  '<span class="chip">status: '+esc(String(p6.status))+'</span><span class="chip">mode: '+esc(String(p6.mode))+'</span>'+
  '<span class="chip">snapshot age: '+(p6.share_age_s==null?"—":esc(p6.share_age_s)+"s")+'</span>'+
  '<span class="chip">usage 失败/10min: '+fmtInt(p6.usage_failures_10min)+'</span>'+
  '<span class="chip">usage age: '+(p6.usage_age_s==null?"—":esc(p6.usage_age_s)+"s")+'</span>'+
  '<span class="chip">门禁: '+(arr(p6.gated).map(esc).join(",")||"无")+'</span>'+
  '<span class="chip">expiring: '+(arr(p6.expiring_set).map(esc).join(",")||"空")+'</span>'+
  (p6.d7?'<span class="chip" style="border-color:#f87171;color:#f87171">D7 池额度不足</span>':'<span class="chip">D7 额度充足</span>')+
  (arr(p6.errors).length?'<span class="chip" style="border-color:#fbbf24;color:#fbbf24">errors: '+arr(p6.errors).map(esc).join(",")+'</span>':"")+
  '</div>'}
/* ---------------- 主刷新 ---------------- */
async function load(){
 try{
  var d=await (await fetch("api/summary",{cache:"no-store"})).json();
  SUM=d;
  var alive=d.health==="alive";
  $("healthDot").style.background=alive?"#4ade80":"#f87171";
  $("healthTxt").textContent=alive?"LiteLLM alive":"LiteLLM down";
  renderKpis();renderStatus();renderModelBar();renderTimeline();renderAcctModel();renderQuota();
  renderSessTree();renderCache();renderLatency();renderSrc();renderConc();renderHours();
  renderRecent();renderUsage(d);renderAcctDetail();renderAcctSess();renderRouting(d.routing);
  $("refresh").textContent="每次打开即时拉取 · 自动刷新"+((STATE.auto?"（"+Math.round(STATE.iv/1000)+"s）":"（已关闭）"))+
   " · 更新于 "+new Date().toLocaleTimeString("zh-CN")+" · 全部图表本地 ECharts 渲染，无外网依赖";
 }catch(e){$("refresh").textContent="加载失败: "+e}
}
/* ---------------- 交互：折叠 / 排序 / 刷新 ---------------- */
function applyFold(){
 document.querySelectorAll(".panel").forEach(function(p){
  var k=p.getAttribute("data-key");if(!k)return;
  p.classList.toggle("collapsed",!!FOLDED[k])});
 var all=document.querySelectorAll(".panel").length,F=Object.keys(FOLDED).filter(function(k){return FOLDED[k]}).length;
 $("foldAll").textContent=(F&&F>=all)?"全部展开":"全部折叠"}
function toggleFold(p){
 var k=p.getAttribute("data-key");if(!k)return;
 FOLDED[k]=!p.classList.contains("collapsed");
 p.classList.toggle("collapsed",!!FOLDED[k]);
 LS.set("poolDash.fold",FOLDED);applyFold();
 if(!p.classList.contains("collapsed")){
  requestAnimationFrame(function(){
   Object.keys(INST).forEach(function(id){try{INST[id].resize()}catch(e){}});
   if(RT)renderRouting(RT)})}}
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
 FOLDED=LS.get("poolDash.fold",{p1:true,p2:true,p3:true,p4:true,p5:true,p6:true,prov:true,teams:true,sync:true,acctSessions:true,usage:true});
 STATE.range=LS.get("poolDash.range","all");STATE.iv=num(LS.get("poolDash.iv"))||15000;STATE.auto=LS.get("poolDash.auto",true);
 if(!window.echarts)$("bootWarn").style.display="block";
 $("rangeSel").value=STATE.range;$("ivSel").value=String(STATE.iv);$("autoChk").checked=!!STATE.auto;
 $("rangeSel").addEventListener("change",function(){STATE.range=this.value;LS.set("poolDash.range",STATE.range);load()});
 $("ivSel").addEventListener("change",function(){STATE.iv=num(this.value)||15000;LS.set("poolDash.iv",STATE.iv);restartTimer();load()});
 $("autoChk").addEventListener("change",function(){STATE.auto=this.checked;LS.set("poolDash.auto",STATE.auto);restartTimer();load()});
 $("modelSort").addEventListener("change",function(){LS.set("poolDash.msort",this.value);renderModelBar()});
 $("modelSort").value=LS.get("poolDash.msort","tokens");
 $("reload").addEventListener("click",function(){load();loadAcctSessions()});
 $("foldAll").addEventListener("click",function(){
  var keys=[];document.querySelectorAll(".panel").forEach(function(p){var k=p.getAttribute("data-key");if(k)keys.push(k)});
  var anyOpen=keys.some(function(k){return !FOLDED[k]});
  keys.forEach(function(k){FOLDED[k]=anyOpen});
  LS.set("poolDash.fold",FOLDED);applyFold();requestAnimationFrame(resizeAll)});
 document.querySelectorAll(".panel .ph").forEach(function(ph){
  ph.addEventListener("click",function(e){
   if(e.target.closest("select")||e.target.closest("input"))return;
   var b=e.target.closest("button");
   if(b&&!b.classList.contains("fold"))return;
   var p=ph.closest(".panel");if(p)toggleFold(p)})});
 $("recQ").addEventListener("input",function(){REC.q=this.value.trim();renderRecent()});
 $("recOnly").addEventListener("change",function(){REC.only=this.value;renderRecent()});
 window.addEventListener("resize",function(){if(boot._t)clearTimeout(boot._t);boot._t=setTimeout(resizeAll,140)});
 if(window.ResizeObserver){var ro=new ResizeObserver(function(){if(boot._r)clearTimeout(boot._r);boot._r=setTimeout(resizeAll,180)});
  document.querySelectorAll(".pbody").forEach(function(b){ro.observe(b)})}
 applyFold();bindTables();loadProv();loadTeams();load();loadAcctSessions();restartTimer()}
function restartTimer(){
 if(boot._iv)clearInterval(boot._iv);
 if(!STATE.auto){$("refresh").textContent="自动刷新已关闭";return}
 boot._iv=setInterval(function(){load();loadAcctSessions()},STATE.iv)}
/* ---------------- 模型选择（api/models-config + api/sync-models） ---------------- */
async function loadProv(){
 try{
  var d=await (await fetch("api/models-config",{cache:"no-store"})).json();
  var box=$("provsel"),avail=arr(d.available),provs=obj(d.providers);
  box.innerHTML=Object.keys(provs).map(function(name){
   var p=obj(provs[name]);SEL[name]={};arr(p.selected).forEach(function(m){SEL[name][m]=true});
   var items=avail.map(function(m){
    return '<label><input type="checkbox" data-p="'+esc(name)+'" value="'+esc(m)+'"'+(SEL[name][m]?" checked":"")+'>'+esc(m)+'</label>'}).join("");
   return '<div class="prov" data-p="'+esc(name)+'"><div class="ph2"><span class="pname">'+esc(p.displayName)+'</span>'+
    '<span class="papi">'+esc(p.api)+'</span><span class="chip">已选 '+arr(p.selected).length+' / '+avail.length+'</span>'+
    '<span class="pact"><button data-bulk="'+esc(name)+'" data-on="1">全选</button><button data-bulk="'+esc(name)+'" data-on="0">清空</button></span></div>'+
    '<div class="gridbox">'+items+'</div></div>'}).join("")||'<div style="color:#8b93a3;font-size:12px">模型配置为空</div>';
  box.onchange=function(e){var t=e.target;
   if(t.matches&&t.matches("input[type=checkbox]"))SEL[t.getAttribute("data-p")][t.value]=t.checked};
  box.onclick=function(e){var b=e.target.closest?e.target.closest("button[data-bulk]"):null;if(b)bulk(b.getAttribute("data-bulk"),b.getAttribute("data-on")==="1")};
 }catch(e){$("provsel").textContent="模型配置加载失败: "+e}}
function bulk(name,on){
 document.querySelectorAll('.prov[data-p="'+name+'"] input[type=checkbox]').forEach(function(cb){
  cb.checked=!!on;SEL[name][cb.value]=!!on})}
$("syncBtn").addEventListener("click",async function(){
 var btn=$("syncBtn"),msg=$("syncMsg");
 btn.disabled=true;msg.textContent="正在写入配置并重启 dsh-desktop…";msg.className="msg";
 var payload={};
 Object.keys(SEL).forEach(function(name){
  payload[name]=Object.keys(SEL[name]).filter(function(k){return SEL[name][k]})});
 try{
  var r=await (await fetch("api/sync-models",{method:"POST",cache:"no-store",
   headers:{"Content-Type":"application/json"},body:JSON.stringify({providers:payload})})).json();
  if(r.ok){msg.textContent="✔ 已同步（"+Object.keys(obj(r.counts)).map(function(k){return k+":"+obj(r.counts)[k]}).join("，")+
    "）· 服务 "+(r.service||"已重启")+" · 备份 "+r.backup;msg.className="msg ok";setTimeout(loadProv,2000)}
  else{msg.textContent="✘ 同步失败: "+(r.error||"未知错误");msg.className="msg err"}
 }catch(e){msg.textContent="✘ 请求失败: "+e;msg.className="msg err"}
 btn.disabled=false});
/* ---------------- Agent Teams（api/teams-config + api/save-teams-config） ---------------- */
function opts(cur){return MODELS.map(function(m){
 return '<option value="'+esc(m)+'"'+(m===cur?" selected":"")+'>'+esc(m)+'</option>'}).join("")}
function memberRow(mm){
 return '<div class="ph2" data-tc="member" style="gap:6px;margin:4px 0">'+
  '<input class="tc-mname" placeholder="成员名" value="'+esc(mm.name||"")+'" style="width:110px">'+
  '<input class="tc-mrole" placeholder="角色 role" value="'+esc(mm.role||"teammate")+'" style="width:120px">'+
  '<select class="tc-mmodel">'+opts(mm.model||MODELS[0]||"")+'</select>'+
  '<button type="button" class="tc-del">删除成员</button></div>'}
function profBox(name,p){
 var members=arr(p&&p.members).map(memberRow).join("");
 return '<div class="prov" data-tc="prof" data-name="'+esc(name)+'"><div class="ph2">'+
  '<span class="pname">模板 '+esc(name)+'</span><span class="chip">/agent-teams --profile '+esc(name)+'</span>'+
  '<span class="pact"><button type="button" class="tc-delprof">删除模板</button></span></div>'+
  '<div style="margin:4px 0"><span style="font-size:11.5px;color:#8b93a3">描述：</span>'+
  '<input class="tc-pdesc" value="'+esc(p&&p.description||"")+'" style="width:60%"></div>'+
  '<div class="tc-members">'+members+'</div><button type="button" class="tc-add">+ 添加成员</button></div>'}
function renderTeams(){
 if(!TC)return;
 $("tcMM").innerHTML=opts(TC.memberModel||"");
 $("tcMX").value=(TC.maxMembers==null?8:TC.maxMembers);
 $("tcMD").value=(TC.memberMaxDepth==null?0:TC.memberMaxDepth);
 var box=$("teamsBox"),profs=obj(TC.profiles);
 box.innerHTML=Object.keys(profs).map(function(n){return profBox(n,profs[n])}).join("")||
  '<div style="color:#8b93a3;font-size:12px">暂无模板 —— 点击「+ 新增模板」创建</div>';
 box.onclick=function(e){
  var t=e.target;
  if(t.classList.contains("tc-del")){t.parentNode.remove();return}
  if(t.classList.contains("tc-add")){
   var wrap=t.parentNode.querySelector(".tc-members");
   wrap.insertAdjacentHTML("beforeend",memberRow({name:"member"+(Math.floor(Math.random()*90)+10),role:"teammate",model:MODELS[0]||""}));return}
  var dp=t.closest?t.closest(".tc-delprof"):null;
  if(dp){var name=dp.closest("[data-tc=prof]").getAttribute("data-name");delete TC.profiles[name];renderTeams()}}}
async function loadTeams(){
 try{
  var d=await (await fetch("api/teams-config",{cache:"no-store"})).json();
  MODELS=arr(d.available);TC=obj(d.config);if(!TC.profiles)TC.profiles={};renderTeams()
 }catch(e){$("teamsMsg").textContent="团队配置加载失败: "+e}}
$("teamAddProf").addEventListener("click",function(){
 var name=prompt("模板名称（/agent-teams --profile 调用时使用，限字母数字_-）");
 if(!name)return;
 if(!/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/.test(name)){alert("名称非法");return}
 if(TC.profiles[name]){alert("模板已存在");return}
 TC.profiles[name]={description:"团队模板 "+name,members:[{name:"worker",role:"teammate",model:MODELS[0]||""}]};
 renderTeams()});
$("teamsSave").addEventListener("click",async function(){
 var btn=$("teamsSave"),msg=$("teamsMsg");
 btn.disabled=true;msg.textContent="正在保存并重启 dsh-desktop…";msg.className="msg";
 var profiles={};
 document.querySelectorAll('#teamsBox [data-tc="prof"]').forEach(function(box){
  var name=box.getAttribute("data-name"),members=[];
  box.querySelectorAll('[data-tc="member"]').forEach(function(r){
   members.push({name:r.querySelector(".tc-mname").value.trim(),role:r.querySelector(".tc-mrole").value.trim(),
    model:r.querySelector(".tc-mmodel").value})});
  profiles[name]={description:box.querySelector(".tc-pdesc").value,members:members}});
 var payload={config:{memberModel:$("tcMM").value,maxMembers:parseInt($("tcMX").value)||8,
  memberMaxDepth:parseInt($("tcMD").value)||0,profiles:profiles},restart:true};
 try{
  var r=await (await fetch("api/save-teams-config",{method:"POST",cache:"no-store",
   headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)})).json();
  if(r.ok){msg.textContent="✔ 已保存（模板 "+r.profiles+" 个）· 服务 "+(r.service||"已重启")+" · 备份 "+r.backup;msg.className="msg ok"}
  else{msg.textContent="✘ 保存失败: "+(r.error||"未知错误");msg.className="msg err"}
 }catch(e){msg.textContent="✘ 请求失败: "+e;msg.className="msg err"}
 btn.disabled=false});
boot();
</script></body></html>"""

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
            self._send(200, HTML, "text/html; charset=utf-8")
        elif self.path == "/api/summary":
            self._send(200, _cached_json("summary", SUM_TTL, summary), "application/json; charset=utf-8")
        elif self.path == "/api/teams-config":
            try:
                self._send(200, json.dumps(teams_config(), ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)[:200]}), "application/json; charset=utf-8")
        elif self.path == "/api/models-config":
            try:
                self._send(200, json.dumps(dsh_models_config(), ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)[:200]}), "application/json; charset=utf-8")
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
        if self.path == "/api/sync-models":
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(ln) or b"{}")
                sel = body.get("providers") or {}
                r = sync_dsh_models(sel)
                if r.get("ok"):
                    r.update(restart_dsh())
                self._send(200, json.dumps(r, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)[:200]}), "application/json; charset=utf-8")
        if self.path == "/api/save-teams-config":
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(ln) or b"{}")
                r = save_teams_config(body.get("config") or {}, bool(body.get("restart")))
                self._send(200, json.dumps(r, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)[:200]}), "application/json; charset=utf-8")
        else:
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
