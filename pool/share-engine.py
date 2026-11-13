#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""share-engine.py — 号池「额度临期烧量」动态路由 · P0 份额引擎

依据：strategy/FINAL-improved-routing-strategy.md
      §6.1-P0（新增 share-engine.py + cron */10 + routing-share.json schema）
      §2.1.1/§2.1.2（数据源与公式）、§2.1.3（保护项）、§2.1.4（D7 需求与溢出）、
      §2.2.1（分档阈值与临期集合 E）、§4.3（推荐参数回填）、§5.3 E2（usage 失败 fail-open）

职责（S0 影子期：只算不注入，不进请求路径）：
  每 600 s 由 cron 执行（也可手动 python3 /opt/litellm/share-engine.py）：
    1) 进程内读 /opt/litellm/.env 各 OCG_KEY_N（密钥绝不打印，日志只用 OCG_KEY_N/OCG_EMAIL_N）；
    2) GET https://opencode.ai/inference/go/v1/usage 取三窗口 status/percent/resetsAt；
       查询失败 → 用最近成功快照（≤30min）；超期/无快照 → status=degraded 且不注入（E2 fail-open）；
       只对 pool-config.json 中 enabled=true 的在池账号取数；停用号不参与取数/快照（2026-10-27：a1 长期 403 停用曾把快照链卡死）
    3) 按 §2.1.2 公式算 pace/share + §2.1.3 保护项；
    4) 原子写 /opt/litellm/routing-share.json（tmp+rename，mode 初始 shadow）。

公式（§2.1.2）：
    rem[i][w]  = max(0, (100 − percent[i][w] − ε) / 100 × Q_w)      ε = 2% 安全余量
    days[i][w] = (resetsAt[i][w] − now) / 86400
    pace[i]    = min_w( rem[i][w] / days[i][w] )                     §2.1.1：w ∈ {monthly, weekly}
                                                                 （rolling 作突发护栏，进预算护栏不进 pace）
    share[i]   = cap_and_smooth( pace[i] / Σ_j pace[j] )

保护项（§2.1.3 / §4.3）：
    ε = 2%（percent_used ≥ 98 → pace=0 → share→0，A1/E6）
    share_cap = 40%；重置后 1h 内 ≤30%；全池告急（Σpace<需求 或 全部临期，E3）≤60%
    max_share_delta_pp = ±10pp（相对上次落盘的 routing-share.json）
    percent ≤ 5% 只用上游读数；其余读数做 ≥6h 差分平滑（§5.2，整数 1pp 粒度噪声）
    临期集合 E = { i : days_monthly ≤ max(2d, 0.5 × max_j days_j) }（D5，禁用硬 ≤3d）
       E 为空 → 显式告警 + 不倾斜（本引擎只记录告警，倾斜在 P1 sidecar 消费）
    溢出规则（D7 / §2.1.4）：Σpace < 需求 D → excess 按 days_to_reset 升序派给先重置的号
       + P1「池额度不足」告警；需求 D 采用终稿 §2.1.4 口径 8.857 $/天（T4 小时画像前推）
    admit_max_usd = pace / 24 × 1.5（D3b 速率护栏：临期号每小时烧量上限，供 P1 消费）
    req_budget_usd = 0.5 × min(rem_monthly, rem_rolling)（§2.1.3 单请求预算护栏，供 P1 消费）
    淘汰分级（2026-10-31）：账号输出新增 low / hard ——
      low  = 账号健康但额度到 ε 线（share 被归零）：sidecar 对已钉住的活跃树「暂缓迁移」，
             空闲树/超时才迁移（保上游 prefix cache 与 encrypted reasoning 连续性）；
      hard = 门禁（rate-limited）/ usage 不可用：必须立即重选。

class_thresholds（§2.2.1 口径 B）：pool-requests.jsonl 滚动 7 天、多请求会话（≥2 req）tt 的
    P50/P90，每日重算一次写入 state 与输出；样本 <10 → 保守阈值 3M/18M + 告警。

用法：
    python3 /opt/litellm/share-engine.py             # 实际运行（联网，写 routing-share.json + 快照 + 日志）
    python3 /opt/litellm/share-engine.py --dry-run   # 不联网：读最近 usage 快照算一遍并打印 JSON，退出 0
    python3 /opt/litellm/share-engine.py --selftest  # 内置样例断言（公式/ε/cap/±10pp/E/溢出），退出 0

绝不修改：session-sidecar.py / gen_config.py / config.yaml / nginx 配置；绝不触发 docker restart。
"""

import copy
import fcntl
import json
import os
import sys
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# ---------------------------------------------------------------- 路径
BASE = "/opt/litellm"
ENV_PATH = os.path.join(BASE, ".env")
OUT_PATH = os.path.join(BASE, "routing-share.json")
SNAPSHOT_PATH = os.path.join(BASE, "usage-snapshot.json")
POOL_CONFIG_PATH = os.path.join(BASE, "pool-config.json")
STATE_PATH = os.path.join(BASE, "share-engine.state.json")
LOG_PATH = os.path.join(BASE, "share-engine.log")
LOCK_PATH = os.path.join(BASE, "share-engine.lock")
POOL_PATH = os.path.join(BASE, "pool-requests.jsonl")
USAGE_URL = "https://opencode.ai/inference/go/v1/usage"   # 2026-10-16 规范路径（旧 /zen/go/v1/usage 为别名）
# 上游明文要求客户端自带可识别 UA（禁止通用 SDK/HTTP 库名）
POOL_USER_AGENT = "dsh-opencodego-pool/1.0"

# ---------------------------------------------------------------- 参数（§4.3 终稿回填）
EPS_PCT = 2.0                 # ε 安全余量（百分点）
SHARE_CAP = 0.40              # 默认份额上限 40%
SHARE_CAP_RESET = 0.30        # 重置后 1h 内 30%
SHARE_CAP_DISTRESS = 0.60     # 全池告急（E3）临时 60%
MAX_DELTA_PP = 10.0           # 每次复算变幅 ≤ ±10pp（相对上次落盘）
L_TILT = 7                    # §2.2.3 临期集合 ×7 倾斜（P1 消费，P0 只落参数）
K_GAIN = 3                    # G2 收益倍数（pro 档 10 由 P1 按模型切换）
WARM_HORIZON_S = 5400         # G4 冷点判定 5400s
REPIN_BUDGET_PER_MIN = 2      # G5 全池重钉 ≤2 次/min
COLD_BUDGET_PER_5H = 10       # G5 每 5h 窗口冷请求 ≤10 次
EXPIRY_RULE = "days_to_reset(monthly) <= max(2d, 0.5*max_j days_j)"   # §2.2.1 D5

MODES = ("shadow", "canary", "full")
REQUIRED_TOP = ("generated_at", "mode", "status", "params", "accounts",
                "class_thresholds", "expiring_set", "errors")
PARAM_KEYS = ("eps_pct", "share_cap", "max_share_delta_pp", "K", "warm_horizon_s",
              "repin_budget_per_min", "cold_budget_per_5h", "l_tilt", "expiry_rule")

# ---------------------------------------------------------------- 配额与窗口（§2.1.1）
# flash 档：月 $60 → weekly $30 / rolling(5h) $12；绝对 $ 有 ±50% 误差，但份额只依赖 percent/days（§2.1.1）
Q_W = {"rolling": 12.0, "weekly": 30.0, "monthly": 60.0}
ALL_WINS = ("rolling", "weekly", "monthly")
PACE_WINS = ("monthly", "weekly")   # §2.1.1：pace = min(monthly, weekly)；rolling 作突发护栏
BUDGET_WINS = ("monthly", "rolling")  # §2.1.3 单请求预算护栏双口径

# ---------------------------------------------------------------- 需求（§2.1.4 D7）
DEMAND_USD_DAY = 8.857        # T4 小时画像前推需求（回放窗实测 7.42、前推 8.857；D7 超配 1.31–1.33×）
DEMAND_SOURCE = "t4_forward"

# ---------------------------------------------------------------- 时序保护
SNAPSHOT_MAX_AGE_S = 1800     # E2：最近成功快照 ≤30min，超期不注入
SMOOTH_HOURS = 6.0            # §5.2：≥6h 差分平滑
SMOOTH_LOW_PCT = 5.0          # §5.2：percent≤5% 只用上游读数
RESET_CAP_S = 3600            # 重置后 1h 内 cap 30%（E7）

# ---------------------------------------------------------------- 计费/本地数据（T4 replay_sim 校准值）
PRICE_IN = 0.14
PRICE_OUT = 0.28
PRICE_CACHED = 0.0028
CLASS_LOOKBACK_D = 7
CLASS_MIN_SESSIONS = 10
CLASS_FALLBACK_P50 = 3_000_000     # 样本不足 → 保守阈值 3M/18M（§2.2.1）
CLASS_FALLBACK_P90 = 18_000_000


# ================================================================ 基础工具
def utc_iso(ts):
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    """ISO-8601（含 Z / 微秒）→ epoch 秒。"""
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def redact(text, secrets=()):
    """任何进入日志/输出的文本都过一遍：密钥值一律替换。"""
    out = str(text)
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "[REDACTED]")
    return out


def log(msg, secrets=()):
    line = "%s %s" % (utc_iso(time.time()), redact(msg, secrets))
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")
    return line


def err(code, level, msg, secrets=()):
    return {"code": code, "level": level, "msg": redact(msg, secrets)}


def load_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def atomic_write_json(path, obj, perm=0o600):
    """原子写：tmp + fsync + rename，绝不留半截文件。"""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False, sort_keys=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, perm)
        os.replace(tmp, path)
        try:
            dfd = os.open(os.path.dirname(path) or ".", os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_env():
    """读 .env（只在进程内使用；绝不打印）。返回 {名字: 值}。"""
    envs = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                envs[k] = v
    return envs


# ================================================================ 上游 usage
def fetch_usage(key):
    """GET 上游 usage 端点 → {窗口: {status, percent, resetsAt}}；异常上抛由调用方处理。"""
    req = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": "Bearer " + key, "User-Agent": POOL_USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    usage = data.get("usage") or {}
    out = {}
    for w in ALL_WINS:
        win = usage.get(w)
        if isinstance(win, dict):
            out[w] = {
                "status": win.get("status") or "unknown",
                "percent": win.get("percent"),
                "resetsAt": win.get("resetsAt"),
            }
    if not out:
        raise RuntimeError("usage 响应缺少窗口字段")
    return out


def read_snapshot():
    """读最近一次成功快照（候选按 ts 取最新）。返回 (accounts_by_slot, ts_epoch, path) 或 None。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = []
    for p in (SNAPSHOT_PATH, os.path.join(here, "usage-snapshot.json"),
              "/root/wordsandbox/strategy/usage-snapshot.json"):
        if p not in cands and os.path.isfile(p):
            cands.append(p)
    best = None
    for p in cands:
        try:
            with open(p) as f:
                data = json.load(f)
            ts = parse_ts(data.get("ts"))
            slots = {}
            for a in data.get("accounts") or []:
                if a.get("windows"):
                    slots[a.get("slot")] = a["windows"]
            if not slots:
                continue
            if best is None or ts > best[1]:
                best = (slots, ts, p)
        except Exception:
            continue
    return best


# ================================================================ §2.1.3 / §5.2 纯函数
def smooth_percent(pct, resets_at, anchor, as_of):
    """§5.2：percent≤5% 只用上游读数；其余 ≥6h 差分平滑（6h 内按已历时间线性接受差分）。

    返回 (生效百分比, 新 anchor)；anchor = {"ts","percent","resets_at"}。
    """
    pct = float(pct)
    if pct <= SMOOTH_LOW_PCT or pct >= 100.0:
        return pct, {"ts": as_of, "percent": pct, "resets_at": resets_at}
    if not anchor or anchor.get("resets_at") != resets_at:
        return pct, {"ts": as_of, "percent": pct, "resets_at": resets_at}
    try:
        age_h = (as_of - float(anchor.get("ts", as_of))) / 3600.0
    except (TypeError, ValueError):
        return pct, {"ts": as_of, "percent": pct, "resets_at": resets_at}
    if age_h < 0 or age_h >= SMOOTH_HOURS:
        return pct, {"ts": as_of, "percent": pct, "resets_at": resets_at}
    p0 = float(anchor.get("percent", pct))
    val = p0 + (pct - p0) * (age_h / SMOOTH_HOURS)
    return round(val, 2), anchor   # 满 6h 才整体接受并重锚


def window_pace(percent, days, quota):
    """单窗口配速 $/天（rem 已扣 ε）。days≤0（重置已过/未知）→ None。"""
    if percent is None or days is None or days <= 0:
        return None
    rem = max(0.0, (100.0 - float(percent) - EPS_PCT) / 100.0 * quota)
    return rem / days


def share_cap_for(reset_recent, distress):
    """§2.1.3 份额上限：默认 40%；重置后 1h 内 30%；全池告急 60%（取最严并集）。"""
    cap = SHARE_CAP_DISTRESS if distress else SHARE_CAP
    if reset_recent:
        cap = min(cap, SHARE_CAP_RESET)
    return cap


def _normalize(v, lo, hi, active, iters=200):
    """把 active 分量按盒约束 [lo, hi] 归一化到 Σ=1；不可行返回 False（v 已尽量推进）。"""
    for _ in range(iters):
        s = sum(v[i] for i in active)
        if abs(s - 1.0) <= 1e-9:
            return True
        if s <= 0:
            return False
        if s < 1.0:
            deficit = 1.0 - s
            rooms = [max(0.0, hi[i] - v[i]) for i in active]
            total = sum(rooms)
            if total <= 1e-15:
                return False
            if deficit > total:
                deficit = total          # 推到 hi 后仍不够 → 下一轮 total=0 → 不可行
            for idx, i in enumerate(active):
                v[i] += deficit * (rooms[idx] / total)
        else:
            surplus = s - 1.0
            rooms = [max(0.0, v[i] - lo[i]) for i in active]
            total = sum(rooms)
            if total <= 1e-15:
                return False
            if surplus > total:
                surplus = total
            for idx, i in enumerate(active):
                v[i] -= surplus * (rooms[idx] / total)
    return abs(sum(v[i] for i in active) - 1.0) <= 1e-9


def project_shares(raw, prev, caps, max_delta, forced_zero):
    """份额投影：Δ 限速（±max_delta，相对上次落盘）→ 上限 cap → 归一化（盒约束迭代）。

    raw/prev/caps 与 forced_zero 按同一顺序对齐；forced_zero（门禁/无数据）恒为 0。
    返回 (shares, protected_flags, notes)。约束不可行时按优先级放宽 Δ（保留 cap），再记录 cap 不可行。
    """
    n = len(raw)
    active = [i for i in range(n) if not forced_zero[i]]
    if not active:
        return [0.0] * n, [False] * n, ["no_active_accounts"]
    if sum(raw[i] for i in active) <= 0:
        return [0.0] * n, [False] * n, ["pace_zero_all"]

    def solve(use_delta):
        lo = [0.0] * n
        hi = [max(0.0, min(1.0, float(caps[i]))) for i in range(n)]
        for i in active:
            p = prev[i] if i < len(prev) else None
            if p is not None and p > 0 and use_delta:
                lo[i] = max(0.0, p - max_delta)
                hi[i] = min(hi[i], p + max_delta)
            elif p == 0 and use_delta:
                hi[i] = min(hi[i], max_delta)   # 上次 0（门禁恢复）→ 最多 +10pp
        v = []
        flags = [False] * n
        for i in range(n):
            if forced_zero[i]:
                v.append(0.0)
                continue
            x = raw[i]
            c = min(max(x, lo[i]), hi[i])
            if abs(c - x) > 1e-12:
                flags[i] = True
            v.append(c)
        ok = _normalize(v, lo, hi, active)
        return v, flags, ok

    v, flags, ok = solve(use_delta=True)
    notes = []
    if not ok:
        # Δ 盒不可行 → 放宽变幅限速（cap 是硬保护，优先保留）§2.1.3
        v2, flags2, ok2 = solve(use_delta=False)
        if any(prev[i] is not None for i in active):
            notes.append("share_delta_relaxed")
        v, flags, ok = v2, flags2, ok2
        if not ok:
            # Σcap < 1（活跃号过少 / 上限过严，数学上不可能 Σ=1 且不超 cap）：
            # 按 cap 比例分摊 = 最小化相对超限，保证 Σ=1，交由消费方并告警。
            notes.append("share_cap_infeasible")
            tot_cap = sum(max(0.0, float(caps[i])) for i in active)
            if tot_cap > 0:
                v = [0.0] * n
                for i in active:
                    v[i] = max(0.0, float(caps[i])) / tot_cap
            flags = [True if not forced_zero[i] else False for i in range(n)]
            ok = abs(sum(v[i] for i in active) - 1.0) <= 1e-9
    return v, flags, notes


def percentile(vals, q):
    """线性插值分位数（numpy 默认口径）。"""
    xs = sorted(vals)
    n = len(xs)
    if n == 0:
        raise ValueError("empty")
    if n == 1:
        return float(xs[0])
    k = (n - 1) * q
    f = int(k)
    c = min(f + 1, n - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


# ================================================================ 本地数据（class_thresholds / 需求）
def get_class_thresholds(state, as_of, errors):
    """§2.2.1 口径 B：滚动 7 天多请求会话 tt P50/P90，每日重算一次。"""
    cur = state.get("class") if isinstance(state.get("class"), dict) else {}
    today = datetime.fromtimestamp(as_of, tz=timezone.utc).strftime("%Y-%m-%d")
    if isinstance(cur.get("updated_at"), str) and cur["updated_at"][:10] == today \
            and "p50_tt" in cur and "p90_tt" in cur:
        return cur                      # 今日已重算 → 直接复用
    st, sn = {}, {}
    cause = None
    try:
        cutoff = as_of * 1000 - CLASS_LOOKBACK_D * 86400 * 1000
        with open(POOL_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("_probe"):
                    continue
                ts = r.get("ts")
                if not isinstance(ts, (int, float)) or ts < cutoff:
                    continue
                s = r.get("sess")
                if not s:
                    continue
                st[s] = st.get(s, 0) + (r.get("tt") or 0)
                sn[s] = sn.get(s, 0) + 1
    except FileNotFoundError:
        cause = "pool-requests.jsonl 不存在"
    except Exception as e:
        cause = "%s: %s" % (type(e).__name__, e)
    multi = [st[s] for s in st if sn[s] >= 2]
    if len(multi) < CLASS_MIN_SESSIONS:
        res = {"p50_tt": CLASS_FALLBACK_P50, "p90_tt": CLASS_FALLBACK_P90,
               "updated_at": utc_iso(as_of), "n_sessions": len(multi), "fallback": True}
        errors.append(err("class_thresholds_fallback", "warn",
                          "多请求会话样本 %d < %d%s → 退回保守阈值 3M/18M"
                          % (len(multi), CLASS_MIN_SESSIONS, "（%s）" % cause if cause else "")))
    else:
        res = {"p50_tt": int(round(percentile(multi, 0.5))),
               "p90_tt": int(round(percentile(multi, 0.9))),
               "updated_at": utc_iso(as_of), "n_sessions": len(multi), "fallback": False}
    state["class"] = res
    return res


def estimate_demand():
    """需求 D（§2.1.4 D7 口径：T4 小时画像前推 8.857 $/天）。

    P0 采用终稿常量（该值即 D7「超配 31–33%」的度量口径）；后续可升级为
    上游 percent 差分实测烧速（state 已保留 anchors 历史）。
    """
    return DEMAND_USD_DAY, DEMAND_SOURCE


# ================================================================ 核心：建输出
def build_output(accounts_raw, as_of, state, prev_out, source,
                 extra_errors=(), secrets=(), demand=None, demand_source=None,
                 class_thr=None, force_degraded=False):
    """accounts_raw: [{"key_ref","email_ref","windows": {w:{status,percent,resetsAt}}|None}]
    返回 (routing-share dict, state dict)。纯计算 + 读 state；不写文件、不联网。
    """
    errors = [dict(e) for e in extra_errors]
    state = copy.deepcopy(state) if state else {}
    anchors = state.setdefault("anchors", {})

    # ---------- 1) 读数平滑 + 窗口明细（§5.2）
    infos = []
    for a in accounts_raw:
        kr = a["key_ref"]
        wins = a.get("windows")
        entry = {"key_ref": kr, "email_ref": a.get("email_ref"),
                 "windows": {}, "available": wins is not None, "gated": wins is None}
        if wins is not None:
            gated = False
            for w in ALL_WINS:
                win = wins.get(w) or {}
                status = win.get("status") or "unknown"
                pct_up = win.get("percent")
                resets = win.get("resetsAt")
                if status == "rate-limited":
                    gated = True          # 门禁优先（I-2，gen_config 同口径：任一窗口限流即剔除）
                eff = pct_up
                if pct_up is not None:
                    eff, new_anchor = smooth_percent(
                        pct_up, resets, (anchors.get(kr) or {}).get(w), as_of)
                    anchors.setdefault(kr, {})[w] = new_anchor
                days = None
                if resets:
                    try:
                        days = (parse_ts(resets) - as_of) / 86400.0
                    except Exception:
                        errors.append(err("bad_resets_at", "warn",
                                          "%s %s resetsAt 无法解析: %r" % (kr, w, resets)))
                entry["windows"][w] = {
                    "status": status,
                    "percent": eff,
                    "percent_upstream": pct_up,
                    "resetsAt": resets,
                    "days": round(days, 4) if days is not None else None,
                    "_days": days,
                }
            entry["gated"] = gated
        infos.append(entry)

    # ---------- 2) pace（§2.1.2，min(monthly, weekly)，ε 已含在 rem 内）
    for e in infos:
        e["pace"] = 0.0
        e["per_win"] = {}
        if not e["available"] or e["gated"]:
            continue
        paces = {}
        for w in PACE_WINS:
            wi = e["windows"].get(w) or {}
            paces[w] = window_pace(wi.get("percent"), wi.get("_days"), Q_W[w])
        e["per_win"] = paces
        valid = [v for v in paces.values() if v is not None]
        if valid:
            e["pace"] = min(valid)
        else:
            errors.append(err("pace_no_valid_window", "warn",
                              "%s monthly/weekly 窗口均不可用（重置已过/读数缺失），pace=0" % e["key_ref"]))

    active = [e for e in infos if e["available"] and not e["gated"]]
    sum_pace = sum(e["pace"] for e in active)

    # ---------- 3) 需求 D + 溢出规则（§2.1.4 D7）
    if demand is None:
        demand, demand_source = estimate_demand()
    overflow_order = sorted(
        [e for e in active if (e["windows"].get("monthly") or {}).get("_days") is not None],
        key=lambda e: e["windows"]["monthly"]["_days"])
    overflowing = bool(active) and sum_pace < demand

    # ---------- 4) 临期集合 E（§2.2.1 D5）
    with_days = [((e["windows"].get("monthly") or {}).get("_days"), e) for e in active]
    with_days = [(d, e) for d, e in with_days if d is not None]
    thr = None
    expiring = []
    if with_days:
        max_days = max(d for d, _ in with_days)
        thr = max(2.0, 0.5 * max_days)
        expiring = [e["key_ref"] for d, e in with_days if d <= thr]
    if not expiring:
        errors.append(err("expiring_set_empty", "warn",
                          "临期集合 E 为空（threshold=%s）→ 上层不倾斜并告警（§2.2.1 D5 / §5-R7）"
                          % ("%.2fd" % thr if thr is not None else "n/a")))
    all_expiring = bool(active) and bool(with_days) and len(with_days) == len(active) \
        and all(d <= thr for d, _ in with_days)

    # ---------- 5) 溢出/告急告警 + 份额上限（§2.1.3 / E3）
    distress = overflowing or all_expiring
    if overflowing:
        errors.append(err("pool_quota_insufficient", "warn",
                          "池额度不足（D7）：Σpace %.2f < 需求 %.2f $/天（超配 %.2f×）→ "
                          "溢出按 days_to_reset 升序派给先重置的号 + P1 池额度不足告警"
                          % (sum_pace, demand, demand / sum_pace if sum_pace > 0 else float("inf"))))
    caps = {}
    reset_recent_map = {}
    for e in active:
        reset_recent = False
        for wi in e["windows"].values():
            d = wi.get("_days")
            if d is not None and -RESET_CAP_S / 86400.0 <= d <= 0:
                reset_recent = True      # resetsAt 落在 [now-1h, now] → 重置后 1h 内
        reset_recent_map[e["key_ref"]] = reset_recent
        caps[e["key_ref"]] = share_cap_for(reset_recent, distress)

    # ---------- 6) 归一化 + 保护项投影（Δ ±10pp 相对上次落盘、cap、门禁强制 0）
    order = [e["key_ref"] for e in infos]
    raw_map = {}
    if sum_pace > 0:
        for e in active:
            raw_map[e["key_ref"]] = e["pace"] / sum_pace
    raw = [raw_map.get(kr, 0.0) for kr in order]
    prev_shares = {}
    if isinstance(prev_out, dict):
        for a in prev_out.get("accounts") or []:
            if isinstance(a, dict) and "share" in a:
                prev_shares[a.get("key_ref")] = a.get("share")
    prev = [prev_shares.get(kr) for kr in order]
    capl = [caps.get(kr, 0.0) for kr in order]
    forced = [not (e["available"] and not e["gated"]) for e in infos]
    shares, flags, notes = project_shares(raw, prev, capl, MAX_DELTA_PP / 100.0, forced)
    for nt in notes:
        if nt == "no_active_accounts":
            errors.append(err("no_active_accounts", "warn",
                              "无可用账号（全部门禁剔除/无数据）→ share 全 0，fail-open"))
        elif nt == "pace_zero_all":
            errors.append(err("pace_zero_all", "warn",
                              "全部可用账号 pace=0（多数已 ≥98%%，E6）→ share 全 0，fail-open"))
        elif nt == "share_delta_relaxed":
            errors.append(err("share_delta_relaxed", "warn",
                              "±10pp 变幅盒与份额上限联合不可行 → 本轮放宽变幅、保留 cap（§2.1.3）"))
        elif nt == "share_cap_infeasible":
            errors.append(err("share_cap_infeasible", "warn",
                              "Σcap < 1（活跃号过少，无法同时满足 Σshare=1 与 cap）→ "
                              "按 cap 比例分摊（最小化相对超限），消费方按权重归一化"))
    # 舍入到 6 位并把残差补到最大项（Σ 始终保持 1）
    if sum(shares) > 0:
        rs = [round(s, 6) for s in shares]
        diff = round(1.0 - sum(rs), 6)
        if abs(diff) > 0:
            rs[max(range(len(rs)), key=lambda i: rs[i])] += diff
            rs = [round(x, 6) for x in rs]
        shares = rs

    # ---------- 7) class_thresholds（每日重算）
    if class_thr is None:
        class_thr = get_class_thresholds(state, as_of, errors)
    else:
        class_thr = copy.deepcopy(class_thr)

    # ---------- 8) 账号 schema（§6.1）
    accounts = []
    for i, e in enumerate(infos):
        kr = e["key_ref"]
        pace = e["pace"]
        if e["available"]:
            m = e["windows"].get("monthly") or {}
            rem_pct = None
            if m.get("percent") is not None:
                rem_pct = round(max(0.0, 100.0 - float(m["percent"])), 2)
            days_m = m.get("_days")
            # 单请求预算护栏（§2.1.3）：0.5 × min(rem_monthly, rem_rolling)，rem 已扣 ε
            bud = []
            for w in BUDGET_WINS:
                wi = e["windows"].get(w) or {}
                p = wi.get("percent")
                if p is None or wi.get("status") == "rate-limited":
                    continue
                bud.append(max(0.0, (100.0 - float(p) - EPS_PCT) / 100.0 * Q_W[w]))
            req_budget = 0.5 * min(bud) if bud else 0.0
            windows_out = {}
            for w in ALL_WINS:
                wi = e["windows"].get(w) or {}
                windows_out[w] = {
                    "status": wi.get("status"),
                    "percent": wi.get("percent"),
                    "percent_upstream": wi.get("percent_upstream"),
                    "resetsAt": wi.get("resetsAt"),
                    "days": wi.get("days"),
                }
        else:
            rem_pct, days_m, req_budget, windows_out = None, None, 0.0, {}
        # ---- 2026-10-31：淘汰分级（供 sidecar 判断「软低额度」还是「硬不可用」）----
        # hard：门禁（任一窗口 rate-limited）或 usage 不可用 → 请求已无法/不应打到该号
        # low ：账号仍可用（窗口健康、别名仍在），只是额度用到 ε 线（默认 used≥98%）→
        #       pace/share 归零。这类号不该再接收新树，但对已钉住的活跃树应允许“暂缓迁移”，
        #       避免会话中途换号导致上游 prefix cache 全冷 + encrypted reasoning 回放 400。
        _hard = (not bool(e["available"])) or bool(e["gated"])
        _low = (not _hard) and (shares[i] <= 0 or pace <= 0
                                or (rem_pct is not None and rem_pct <= EPS_PCT))
        accounts.append({
            "key_ref": kr,
            "email_ref": e.get("email_ref"),
            "windows": windows_out,
            "rem_pct": rem_pct,
            "days": round(days_m, 4) if days_m is not None else None,
            "pace_usd_day": round(pace, 4),
            "share": shares[i],
            "share_capped": bool(flags[i]),
            "admit_max_usd": round(pace / 24.0 * 1.5, 6),   # D3b 速率护栏 $/h
            "req_budget_usd": round(req_budget, 6),          # §2.1.3 单请求预算
            "gated": bool(e["gated"]),
            "available": bool(e["available"]),
            "hard": bool(_hard),                             # 2026-10-31 淘汰分级
            "low": bool(_low),                               # 2026-10-31 淘汰分级
        })

    # ---------- 9) 状态与顶层 schema
    if sum_pace <= 0 and not any(c == "no_active_accounts" for c in
                                 (x.get("code") for x in errors)):
        if not active:
            errors.append(err("no_usable_pace", "warn", "Σpace=0 → status=degraded，fail-open"))
    fatal = any(e.get("level") == "fatal" for e in errors)
    no_data = any(not e["available"] for e in infos)
    degraded = force_degraded or fatal or no_data or sum_pace <= 0
    mode = "shadow"
    if isinstance(prev_out, dict) and prev_out.get("mode") in MODES:
        mode = prev_out.get("mode")          # 保留人工/上层设置的模式（回滚开关 §5.1）

    params = {
        "eps_pct": EPS_PCT,
        "share_cap": SHARE_CAP,
        "max_share_delta_pp": MAX_DELTA_PP,
        "K": K_GAIN,
        "warm_horizon_s": WARM_HORIZON_S,
        "repin_budget_per_min": REPIN_BUDGET_PER_MIN,
        "cold_budget_per_5h": COLD_BUDGET_PER_5H,
        "l_tilt": L_TILT,
        "expiry_rule": EXPIRY_RULE,
    }

    out = {
        "generated_at": utc_iso(as_of),
        "mode": mode,
        "status": "degraded" if degraded else "ok",
        "params": params,
        "accounts": accounts,
        "class_thresholds": class_thr,
        "expiring_set": expiring,
        "errors": errors,
        # ---- 扩展字段（消费方 P1/P3 使用；§6.1 必需字段以上齐全）
        "source": source,
        "as_of": utc_iso(as_of),
        "sum_pace_usd_day": round(sum_pace, 4),
        "demand_usd_day": round(demand, 4),
        "demand_source": demand_source,
        "expiring_threshold_days": round(thr, 4) if thr is not None else None,
        "overflow": {
            "overflowing": overflowing,
            "demand_usd_day": round(demand, 4),
            "sum_pace_usd_day": round(sum_pace, 4),
            "excess_usd_day": round(max(0.0, demand - sum_pace), 4),
            "assign_order": [e["key_ref"] for e in overflow_order],
            "alert": "pool_quota_insufficient" if overflowing else None,
        },
        "distress": distress,
    }
    # 输出错误文本统一脱敏
    for e in out["errors"]:
        e["msg"] = redact(e.get("msg", ""), secrets)
    return out, state


def degraded_skeleton(reason):
    """兜底输出：任何崩溃路径也写出合法 §6.1 schema（fail-open）。"""
    return {
        "generated_at": utc_iso(time.time()),
        "mode": "shadow",
        "status": "degraded",
        "params": {
            "eps_pct": EPS_PCT, "share_cap": SHARE_CAP, "max_share_delta_pp": MAX_DELTA_PP,
            "K": K_GAIN, "warm_horizon_s": WARM_HORIZON_S,
            "repin_budget_per_min": REPIN_BUDGET_PER_MIN,
            "cold_budget_per_5h": COLD_BUDGET_PER_5H, "l_tilt": L_TILT,
            "expiry_rule": EXPIRY_RULE,
        },
        "accounts": [],
        "class_thresholds": {"p50_tt": CLASS_FALLBACK_P50, "p90_tt": CLASS_FALLBACK_P90,
                             "updated_at": utc_iso(time.time()), "n_sessions": 0, "fallback": True},
        "expiring_set": [],
        "errors": [{"code": "engine_crashed", "level": "fatal", "msg": str(reason)[:500]}],
        "source": "none",
        "sum_pace_usd_day": 0.0,
        "demand_usd_day": DEMAND_USD_DAY,
        "demand_source": DEMAND_SOURCE,
        "expiring_threshold_days": None,
        "overflow": {"overflowing": False, "demand_usd_day": DEMAND_USD_DAY,
                     "sum_pace_usd_day": 0.0, "excess_usd_day": 0.0,
                     "assign_order": [], "alert": None},
        "distress": False,
    }


# ================================================================ 运行模式
def acquire_lock():
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def pool_enabled_key_ids(key_ids):
    """筛出「在池」账号：pool-config.json 中显式 enabled=false 的剔除；
    未列出的账号保持参与；配置缺失/全禁用时回退为全部（保持旧行为）。"""
    try:
        with open(POOL_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f) or {}
        disabled = {a.get("key_var") for a in (cfg.get("accounts") or [])
                    if a.get("key_var") and a.get("enabled") is False}
        kept = [k for k in key_ids if k not in disabled]
        return kept or key_ids
    except Exception:
        return key_ids


def run_live():
    envs = load_env()
    secrets = tuple(v for v in envs.values() if v)
    key_ids = sorted(
        [k for k in envs if k.startswith("OCG_KEY_") and not envs[k].startswith("REPLACE")],
        key=lambda k: int(k.split("_")[-1]))
    key_ids_all = list(key_ids)
    key_ids = pool_enabled_key_ids(key_ids)

    lock = acquire_lock()
    if lock is None:
        log("skip: another share-engine run holds the lock")
        return 0
    try:
        state = load_json(STATE_PATH) or {}
        prev, extra = None, []
        try:
            with open(OUT_PATH) as f:
                prev = json.load(f)
        except FileNotFoundError:
            pass
        except Exception as e:
            extra.append(err("prev_output_unreadable", "warn",
                             "上次 routing-share.json 不可读（首轮/无 Δ 锚点）: %s" % type(e).__name__))

        now = time.time()
        skipped = [k for k in key_ids_all if k not in key_ids]
        if skipped:
            extra.append(err("usage_disabled_skipped", "info",
                             "停用号不取 usage：%s（pool-config.json enabled=false）"
                             % ",".join(skipped)))
        # ---- 取数（进程内读密钥，绝不外泄；并发 6 路）
        accounts_raw, fetch_errors, all_live = [], [], True

        def _one(kid):
            return kid, fetch_usage(envs[kid])

        results = {}
        with ThreadPoolExecutor(max_workers=max(1, len(key_ids))) as ex:
            futs = [ex.submit(_one, kid) for kid in key_ids]
            for fu in futs:
                try:
                    kid, wins = fu.result()
                    results[kid] = wins
                except Exception as e:
                    all_live = False
                    # 记录失败但不含任何响应体密钥
                    fetch_errors.append(("?", type(e).__name__, str(e)[:160]))

        for kid in key_ids:
            n = kid.split("_")[-1]
            email_ref = ("OCG_EMAIL_%s" % n) if ("OCG_EMAIL_%s" % n) in envs else None
            wins = results.get(kid)
            if wins is None:
                accounts_raw.append({"key_ref": kid, "email_ref": email_ref, "windows": None})
            else:
                accounts_raw.append({"key_ref": kid, "email_ref": email_ref, "windows": wins})
        failed = [kid for kid in key_ids if kid not in results]
        for kid in failed:
            et = [e for e in fetch_errors]
            detail = "; ".join("%s: %s" % (t, m) for _, t, m in et)[:200]
            extra.append(err("usage_fetch_failed", "warn",
                             "%s usage 查询失败（%s）" % (kid, detail)))

        # ---- 快照兜底（E2：≤30min 可用，超期 → degraded 不注入）
        source = "live" if all_live else "none"
        if not all_live:
            snap = read_snapshot()
            if snap:
                slots, ts, path = snap
                age = now - ts
                fillable = [k for k in failed if k in slots]
                if age <= SNAPSHOT_MAX_AGE_S:
                    for a in accounts_raw:
                        if a["windows"] is None and a["key_ref"] in slots:
                            a["windows"] = slots[a["key_ref"]]
                    source = "snapshot" if not fillable else "live+snapshot"
                    extra.append(err("usage_snapshot_fallback", "warn",
                                     "usage 查询失败 → 用最近成功快照（age=%ds ≤ %ds, %s）"
                                     % (int(age), SNAPSHOT_MAX_AGE_S, os.path.basename(path))))
                else:
                    extra.append(err("usage_stale_snapshot", "fatal",
                                     "usage 查询失败且快照过期（age=%ds > %ds）→ degraded 不注入（E2）"
                                     % (int(age), SNAPSHOT_MAX_AGE_S)))
                    source = "snapshot_stale"
            else:
                extra.append(err("usage_unavailable", "fatal",
                                 "usage 查询失败且无任何快照 → degraded 不注入（E2）"))

        # ---- 计算 + 落盘
        out, state2 = build_output(accounts_raw, now, state, prev, source,
                                   extra_errors=extra, secrets=secrets)

        # 在池账号全量成功 → 刷新快照（供 E2 / --dry-run）；停用号不参与，不再阻塞快照
        if all_live:
            snap_obj = {
                "ts": utc_iso(now),
                "accounts": [
                    {"slot": a["key_ref"], "ok": True, "windows": a["windows"]}
                    for a in accounts_raw if a["windows"] is not None
                ],
            }
            atomic_write_json(SNAPSHOT_PATH, snap_obj, 0o600)

        atomic_write_json(OUT_PATH, out, 0o644)

        # 自校验：读回必须是合法 §6.1 schema（原子写 → 无半截文件）
        rb = load_json(OUT_PATH)
        missing = [k for k in REQUIRED_TOP if not (isinstance(rb, dict) and k in rb)]
        if missing:
            raise RuntimeError("routing-share.json 读回缺字段: %s" % missing)

        atomic_write_json(STATE_PATH, state2, 0o600)
        lines = log_summary(out, prev, secrets)
        if sys.stdout.isatty():
            for ln in lines:
                print(ln)
        return 0
    except Exception:
        log("FATAL unhandled: " + redact(traceback.format_exc(), secrets))
        try:
            atomic_write_json(OUT_PATH, degraded_skeleton("unhandled crash, see share-engine.log"), 0o644)
        except Exception:
            pass
        return 1
    finally:
        try:
            os.close(lock)
        except OSError:
            pass


def log_summary(out, prev, secrets=()):
    prev_shares = {}
    if isinstance(prev, dict):
        for a in prev.get("accounts") or []:
            prev_shares[a.get("key_ref")] = a.get("share")
    head = ("run status=%s mode=%s src=%s accounts=%d active=%d "
            "sum_pace=%s demand=%s(%s) overflow=%s E=%s thr=%s "
            "class=p50:%s,p90:%s,n=%s,fallback=%s errors=%d") % (
        out["status"], out["mode"], out.get("source"),
        len(out["accounts"]),
        sum(1 for a in out["accounts"] if not a["gated"]),
        out.get("sum_pace_usd_day"), out.get("demand_usd_day"), out.get("demand_source"),
        "yes" if out["overflow"]["overflowing"] else "no",
        ",".join(out["expiring_set"]) or "EMPTY",
        out.get("expiring_threshold_days"),
        out["class_thresholds"].get("p50_tt"), out["class_thresholds"].get("p90_tt"),
        out["class_thresholds"].get("n_sessions"), out["class_thresholds"].get("fallback"),
        len(out["errors"]),
    )
    lines = [log(head, secrets)]
    for a in out["accounts"]:
        ps = prev_shares.get(a["key_ref"])
        if ps is None:
            delta = "new"
        else:
            delta = "%+.2fpp" % ((a["share"] - ps) * 100.0)
        lines.append(log(
            "%s share=%.2f%% delta=%s pace=%.4f admit=%.4f req_budget=%.4f "
            "capped=%s gated=%s rem_m=%s days_m=%s" % (
                a["key_ref"], a["share"] * 100.0, delta, a["pace_usd_day"],
                a["admit_max_usd"], a["req_budget_usd"],
                "yes" if a["share_capped"] else "no",
                "yes" if a["gated"] else "no",
                a["rem_pct"], a["days"]), secrets))
    for e in out["errors"]:
        lines.append(log("alert level=%s code=%s %s" % (e["level"], e["code"], e["msg"]), secrets))
    return lines


def run_dry():
    """--dry-run：不联网；读最近 usage 快照（同目录优先）算一遍并打印 JSON；恒退出 0。"""
    try:
        snap = read_snapshot()
        extra = []
        if snap:
            slots, ts, path = snap
            as_of = ts
            source = "snapshot"
            extra.append(err("dry_run_snapshot", "info",
                             "离线试算：使用快照 %s（ts=%s, age=%ds）"
                             % (path, utc_iso(ts), int(time.time() - ts))))
            accounts_raw = [
                {"key_ref": slot, "email_ref": None, "windows": wins}
                for slot, wins in sorted(slots.items())
            ]
        else:
            as_of = parse_ts(SAMPLE["ts"])
            source = "sample"
            extra.append(err("dry_run_sample_data", "warn",
                             "离线试算：无 usage 快照，使用内置样例数据（§2.1.5 快照）"))
            accounts_raw = [
                {"key_ref": slot, "email_ref": None, "windows": wins}
                for slot, wins in sorted(SAMPLE["windows"].items())
            ]
        state = load_json(STATE_PATH) or {}
        prev = None
        try:
            with open(OUT_PATH) as f:
                prev = json.load(f)
        except Exception:
            prev = None
        out, _ = build_output(accounts_raw, as_of, state, prev, source, extra_errors=extra)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps(degraded_skeleton("dry-run: " + redact(traceback.format_exc())),
                         indent=2, ensure_ascii=False))
        return 0


# 内置样例（= strategy/usage-snapshot.json 2026-09-29 01:47Z，FINAL §2.1.5 同一时点）
SAMPLE = {
    "ts": "2026-09-29T01:47:40Z",
    "windows": {
        "OCG_KEY_1": {
            "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-09-29T06:40:39Z"},
            "weekly": {"status": "ok", "percent": 21, "resetsAt": "2026-10-05T00:00:00Z"},
            "monthly": {"status": "ok", "percent": 27, "resetsAt": "2026-10-24T13:22:25Z"}},
        "OCG_KEY_2": {
            "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-09-29T06:47:44Z"},
            "weekly": {"status": "ok", "percent": 0, "resetsAt": "2026-10-05T00:00:00Z"},
            "monthly": {"status": "rate-limited", "percent": 100, "resetsAt": "2026-10-03T03:28:42Z"}},
        "OCG_KEY_3": {
            "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-09-29T06:47:45Z"},
            "weekly": {"status": "ok", "percent": 0, "resetsAt": "2026-10-05T00:00:00Z"},
            "monthly": {"status": "rate-limited", "percent": 100, "resetsAt": "2026-09-30T11:46:27Z"}},
        "OCG_KEY_4": {
            "rolling": {"status": "ok", "percent": 3, "resetsAt": "2026-09-29T06:20:35Z"},
            "weekly": {"status": "ok", "percent": 8, "resetsAt": "2026-10-05T00:00:00Z"},
            "monthly": {"status": "ok", "percent": 90, "resetsAt": "2026-10-03T11:00:10Z"}},
        "OCG_KEY_5": {
            "rolling": {"status": "ok", "percent": 1, "resetsAt": "2026-09-29T06:19:40Z"},
            "weekly": {"status": "ok", "percent": 3, "resetsAt": "2026-10-05T00:00:00Z"},
            "monthly": {"status": "ok", "percent": 3, "resetsAt": "2026-10-27T15:44:11Z"}},
        "OCG_KEY_6": {
            "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-09-29T06:20:48Z"},
            "weekly": {"status": "ok", "percent": 2, "resetsAt": "2026-10-05T00:00:00Z"},
            "monthly": {"status": "ok", "percent": 3, "resetsAt": "2026-10-27T15:47:53Z"}},
    },
}


# ================================================================ --selftest
def selftest():
    """内置样例断言：公式 / ε / cap / ±10pp / E 集合 / 溢出规则 / 平滑 / schema。不联网、不写文件。"""
    n = [0]

    def ok(name, cond, detail=""):
        if not cond:
            raise AssertionError("%s: %s" % (name, detail))
        n[0] += 1

    def win(status, pct, resets):
        return {"status": status, "percent": pct, "resetsAt": resets}

    as_of = parse_ts(SAMPLE["ts"])

    # ---------- 1) §2.1.2 公式：pace = min(monthly, weekly)，rem 扣 ε
    d_m = (parse_ts("2026-10-24T13:22:25Z") - as_of) / 86400.0          # KEY_1 monthly 25.48d
    d_w = (parse_ts("2026-10-05T00:00:00Z") - as_of) / 86400.0          # KEY_1 weekly  5.93d
    pm = (100 - 27 - EPS_PCT) / 100.0 * Q_W["monthly"] / d_m
    pw = (100 - 21 - EPS_PCT) / 100.0 * Q_W["weekly"] / d_w
    ok("formula_window_pace_monthly", abs(window_pace(27, d_m, 60.0) - pm) < 1e-12, pm)
    ok("formula_window_pace_weekly", abs(window_pace(21, d_w, 30.0) - pw) < 1e-12, pw)
    ok("formula_min_binding_monthly", pm < pw and abs(pm - 1.6717) < 1e-3, pm)

    # ---------- 2) ε 保护：percent ≥ 98 → pace 0
    ok("eps_98_zero", window_pace(98, 5.0, 60.0) == 0.0)
    ok("eps_100_zero", window_pace(100, 5.0, 60.0) == 0.0)
    ok("eps_97_positive", (window_pace(97, 5.0, 60.0) or 0) > 0)
    ok("eps_negative_days_none", window_pace(50, -0.1, 60.0) is None)

    # ---------- 3) 整合样例（§2.1.5 快照）：pace / share / E / 溢出
    accounts_raw = [{"key_ref": k, "email_ref": None, "windows": w}
                    for k, w in sorted(SAMPLE["windows"].items())]
    class_fixed = {"p50_tt": 6187551, "p90_tt": 26360925,
                   "updated_at": "2026-09-29T01:47:40Z", "n_sessions": 25, "fallback": False}
    out, _st = build_output(accounts_raw, as_of, {}, None, "sample",
                            demand=8.857, demand_source="t4_forward", class_thr=class_fixed)
    ok("schema_required_keys", all(k in out for k in REQUIRED_TOP),
       [k for k in REQUIRED_TOP if k not in out])
    ok("schema_mode_shadow", out["mode"] == "shadow", out["mode"])
    ok("schema_status_ok", out["status"] == "ok", out["status"])
    ok("schema_param_keys", set(out["params"]) == set(PARAM_KEYS), sorted(out["params"]))
    ok("schema_accounts_6", len(out["accounts"]) == 6, len(out["accounts"]))
    ok("params_values", out["params"]["eps_pct"] == 2.0 and out["params"]["share_cap"] == 0.40
       and out["params"]["max_share_delta_pp"] == 10 and out["params"]["K"] == 3
       and out["params"]["warm_horizon_s"] == 5400
       and out["params"]["repin_budget_per_min"] == 2
       and out["params"]["cold_budget_per_5h"] == 10
       and out["params"]["l_tilt"] == 7
       and "max(2d" in out["params"]["expiry_rule"], out["params"])

    acc = {a["key_ref"]: a for a in out["accounts"]}
    # T4 回放口径：Σpace 6.65–6.78 $/天（§2.1.4）
    ok("sum_pace_t4_range", 6.65 <= out["sum_pace_usd_day"] <= 6.78, out["sum_pace_usd_day"])
    ok("pace_key1", abs(acc["OCG_KEY_1"]["pace_usd_day"] - pm) < 1e-3, acc["OCG_KEY_1"]["pace_usd_day"])
    d_m4 = (parse_ts("2026-10-03T11:00:10Z") - as_of) / 86400.0
    pm4 = (100 - 90 - EPS_PCT) / 100.0 * Q_W["monthly"] / d_m4
    ok("pace_key4_min_monthly", abs(acc["OCG_KEY_4"]["pace_usd_day"] - pm4) < 1e-3,
       acc["OCG_KEY_4"]["pace_usd_day"])
    # 门禁（rate-limited）→ share 强制 0（E1）
    ok("gate_key2_share0", acc["OCG_KEY_2"]["share"] == 0.0 and acc["OCG_KEY_2"]["gated"])
    ok("gate_key3_share0", acc["OCG_KEY_3"]["share"] == 0.0 and acc["OCG_KEY_3"]["gated"])
    # 归一化
    ok("share_sums_to_1", abs(sum(a["share"] for a in out["accounts"]) - 1.0) < 1e-6,
       sum(a["share"] for a in out["accounts"]))
    ok("active_shares_positive", all(acc[k]["share"] > 0 for k in
                                     ("OCG_KEY_1", "OCG_KEY_4", "OCG_KEY_5", "OCG_KEY_6")))
    # D3b 速率护栏折算
    ok("admit_max_formula",
       abs(acc["OCG_KEY_1"]["admit_max_usd"] - pm / 24 * 1.5) < 1e-6,
       acc["OCG_KEY_1"]["admit_max_usd"])
    # §2.1.3 单请求预算 = 0.5 × min(rem_monthly, rem_rolling)
    rb1 = 0.5 * min((100 - 27 - 2) / 100.0 * 60.0, (100 - 0 - 2) / 100.0 * 12.0)
    ok("req_budget", abs(acc["OCG_KEY_1"]["req_budget_usd"] - rb1) < 1e-6,
       acc["OCG_KEY_1"]["req_budget_usd"])

    # ---------- 4) 临期集合 E（§2.2.1 D5：连续式、禁 ≤3d、空集告警）
    ok("expiring_is_key4", out["expiring_set"] == ["OCG_KEY_4"], out["expiring_set"])
    thr_exp = 0.5 * max((parse_ts("2026-10-27T15:47:53Z") - as_of) / 86400.0,
                        (parse_ts("2026-10-27T15:44:11Z") - as_of) / 86400.0)
    ok("expiring_threshold", abs(out["expiring_threshold_days"] - max(2.0, thr_exp)) < 1e-3,
       out["expiring_threshold_days"])
    ok("expiring_not_hard_3d", (parse_ts("2026-10-03T11:00:10Z") - as_of) / 86400.0 > 3.0
       and "OCG_KEY_4" in out["expiring_set"])   # 4.38d > 3d，硬阈值会漏 → D5 修正验证
    ok("no_expiring_alert_when_nonempty",
       not any(e["code"] == "expiring_set_empty" for e in out["errors"]))

    # ---------- 5) 溢出规则（D7 / §2.1.4）
    ok("overflow_flag", out["overflow"]["overflowing"] is True)
    ok("overflow_ratio_d7", 1.25 <= 8.857 / out["sum_pace_usd_day"] <= 1.40,
       8.857 / out["sum_pace_usd_day"])
    ok("overflow_excess", abs(out["overflow"]["excess_usd_day"]
                              - (8.857 - out["sum_pace_usd_day"])) < 1e-6)
    ok("overflow_alert_code", any(e["code"] == "pool_quota_insufficient" and e["level"] == "warn"
                                  for e in out["errors"]),
       [e["code"] for e in out["errors"]])
    # 超出部分按 days_to_reset 升序（先重置者先接）
    ok("overflow_assign_order", out["overflow"]["assign_order"] ==
       ["OCG_KEY_4", "OCG_KEY_1", "OCG_KEY_5", "OCG_KEY_6"],
       out["overflow"]["assign_order"])
    ok("overflow_assign_active_only", "OCG_KEY_2" not in out["overflow"]["assign_order"])
    # 无溢出分支
    out2, _ = build_output(accounts_raw, as_of, {}, None, "sample",
                           demand=1.0, class_thr=class_fixed)
    ok("no_overflow_when_demand_low", out2["overflow"]["overflowing"] is False
       and not any(e["code"] == "pool_quota_insufficient" for e in out2["errors"]))

    # ---------- 6) E 空集 → 告警且不降级（fail-open 盲区堵上，D5/M13）
    import copy as _copy
    raw_e = _copy.deepcopy(accounts_raw)
    for a in raw_e:
        if a["key_ref"] in ("OCG_KEY_1", "OCG_KEY_4", "OCG_KEY_5", "OCG_KEY_6"):
            a["windows"]["monthly"]["resetsAt"] = "2026-10-27T15:44:11Z"   # 全部 28.58d
    out3, _ = build_output(raw_e, as_of, {}, None, "sample",
                           demand=8.857, class_thr=class_fixed)
    ok("expiring_empty_alert", out3["expiring_set"] == [] and
       any(e["code"] == "expiring_set_empty" for e in out3["errors"]), out3["expiring_set"])
    ok("expiring_empty_not_degraded", out3["status"] == "ok", out3["status"])

    # ---------- 7) 份额上限：40% / 重置后 1h 30% / 全池告急 60%（§2.1.3）
    ok("cap_default", share_cap_for(False, False) == 0.40)
    ok("cap_after_reset", share_cap_for(True, False) == 0.30)
    ok("cap_distress", share_cap_for(False, True) == 0.60)
    ok("cap_distress_and_reset", share_cap_for(True, True) == 0.30)
    v, fl, nt = project_shares([0.90, 0.0333, 0.0333, 0.0334], [None] * 4,
                               [0.40] * 4, 0.10, [False] * 4)
    ok("cap40_applied", v[0] <= 0.40 + 1e-9 and abs(sum(v) - 1.0) < 1e-9, v)
    ok("cap40_flag", fl[0] is True)
    v, fl, nt = project_shares([0.90, 0.0333, 0.0333, 0.0334], [None] * 4,
                               [0.30] * 4, 0.10, [False] * 4)
    ok("cap30_applied", v[0] <= 0.30 + 1e-9 and abs(sum(v) - 1.0) < 1e-9, v)
    v, fl, nt = project_shares([0.55, 0.15, 0.15, 0.15], [None] * 4,
                               [0.60] * 4, 0.10, [False] * 4)
    ok("cap60_allows_55", abs(v[0] - 0.55) < 1e-9, v)   # 告急 60% 上限不夹 55%

    # ---------- 8) 变幅限速 ±10pp（相对上次落盘）
    v, fl, nt = project_shares([0.97, 0.01, 0.01, 0.01], [0.25, 0.25, 0.25, 0.25],
                               [0.40] * 4, 0.10, [False] * 4)
    ok("delta_bounds", all(abs(v[i] - 0.25) <= 0.10 + 1e-9 for i in range(4)), v)
    ok("delta_sums_1", abs(sum(v) - 1.0) < 1e-9, sum(v))
    ok("delta_clamped_flag", fl[0] is True and v[0] <= 0.35 + 1e-9, v)
    ok("delta_respects_cap", all(v[i] <= 0.40 + 1e-9 for i in range(4)), v)
    # 上次 0（门禁恢复）→ 最多 +10pp（可行盒内）
    v, fl, nt = project_shares([0.30, 0.35, 0.35], [0.0, 0.5, 0.5],
                               [0.60, 0.60, 0.60], 0.10, [False] * 3)
    ok("delta_from_zero", v[0] <= 0.10 + 1e-9 and abs(sum(v) - 1.0) < 1e-9, v)
    # Σcap < 1（仅 2 个活跃号且 cap 40%）→ 按 cap 比例分摊，Σ 仍为 1 并告警
    v, fl, nt = project_shares([0.60, 0.40], [None, None], [0.40, 0.40], 0.10, [False, False])
    ok("cap_infeasible_keeps_sum1", abs(sum(v) - 1.0) < 1e-9 and "share_cap_infeasible" in nt
       and abs(v[0] - 0.5) < 1e-9, (v, nt))
    # 强制 0（门禁）不参与 Δ 平滑
    v, fl, nt = project_shares([0.0, 0.6, 0.4], [0.3, 0.4, 0.3],
                               [0.4, 0.4, 0.4], 0.10, [True, False, False])
    ok("forced_zero_gate", v[0] == 0.0 and abs(sum(v) - 1.0) < 1e-9, v)

    # ---------- 9) ≥6h 差分平滑 + percent≤5 只用上游读数（§5.2）
    resets = "2026-10-24T13:22:25Z"
    eff, an = smooth_percent(4, resets, {"ts": as_of - 3600, "percent": 80.0,
                                         "resets_at": resets}, as_of)
    ok("smooth_low_pct_raw", eff == 4.0 and an["percent"] == 4.0, (eff, an))   # ≤5% 直接采信并重锚
    eff, an = smooth_percent(14, resets, {"ts": as_of - 3 * 3600, "percent": 10.0,
                                          "resets_at": resets}, as_of)
    ok("smooth_3h_interpolate", abs(eff - 12.0) < 0.01, eff)                    # 3h → 接受一半差分
    eff, an = smooth_percent(14, resets, {"ts": as_of - 7 * 3600, "percent": 10.0,
                                          "resets_at": resets}, as_of)
    ok("smooth_ge6h_raw", eff == 14.0 and an["ts"] == as_of, (eff, an))
    eff, an = smooth_percent(14, resets, {"ts": as_of - 3 * 3600, "percent": 10.0,
                                          "resets_at": "2026-10-24T13:22:26Z"}, as_of)
    ok("smooth_reset_change_raw", eff == 14.0, eff)
    eff, an = smooth_percent(100, resets, {"ts": as_of - 3600, "percent": 10.0,
                                           "resets_at": resets}, as_of)
    ok("smooth_ratelimited_raw", eff == 100.0, eff)

    # ---------- 10) class_thresholds：分位数 + 每日复用 + 保守回退
    ok("percentile_p50", abs(percentile(list(range(1, 101)), 0.5) - 50.5) < 1e-9)
    ok("percentile_p90", abs(percentile(list(range(1, 101)), 0.9) - 90.1) < 1e-9)
    today = datetime.fromtimestamp(as_of, tz=timezone.utc).strftime("%Y-%m-%d")
    st = {"class": {"p50_tt": 1, "p90_tt": 2, "updated_at": today + "T00:00:00Z",
                    "n_sessions": 11, "fallback": False}}
    errs = []
    got = get_class_thresholds(st, as_of, errs)
    ok("class_daily_reuse", got["p50_tt"] == 1 and not errs, (got, errs))

    # ---------- 11) 查询失败 → 无数据账号 → degraded（E2 fail-open）
    raw_deg = _copy.deepcopy(accounts_raw)
    raw_deg[0]["windows"] = None
    outd, _ = build_output(raw_deg, as_of, {}, None, "none",
                           extra_errors=[err("usage_unavailable", "fatal", "no data")],
                           demand=8.857, class_thr=class_fixed)
    ok("e2_degraded", outd["status"] == "degraded", outd["status"])
    ok("e2_schema_intact", all(k in outd for k in REQUIRED_TOP))
    ok("e2_share0_for_nodata", outd["accounts"][0]["share"] == 0.0)
    ok("e2_share_still_norm", abs(sum(a["share"] for a in outd["accounts"]) - 1.0) < 1e-6)

    # ---------- 12) 上次 mode 保留（回滚开关 §5.1）
    outm, _ = build_output(accounts_raw, as_of, {}, {"mode": "canary"}, "sample",
                           demand=1.0, class_thr=class_fixed)
    ok("mode_preserved", outm["mode"] == "canary", outm["mode"])

    print("selftest: %d assertions passed" % n[0])
    return 0


# ================================================================ 入口
def main(argv):
    if "--selftest" in argv:
        try:
            return selftest()
        except AssertionError as e:
            print("selftest FAILED: %s" % e, file=sys.stderr)
            return 1
    if "--dry-run" in argv:
        return run_dry()
    return run_live()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
