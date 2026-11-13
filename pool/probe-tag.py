#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe-tag.py — B2 专项探针（FINAL §6.1-P2 / §5-B2，S1 进入条件）

只读观测 + 极小流量（mimo-v2.6-flash, max_tokens=1, temperature=0，约 10 条请求）。
直连 127.0.0.1:4000（绕过 sidecar，隔离变量：只测 litellm 自身的 tag/affinity 语义），
落号观测 = pool-requests.jsonl 的 keyRef（= .env OCG_KEY_N 原值，精确匹配，绝不打印）。

验证两件事：
  ① 请求带 metadata.tags=["acct:<N>"] 时是否只落该账号 deployment；
  ② tag 与 session_affinity 同时存在时的执行顺序（谁优先），
     是否符合 §2.2 依赖的语义（钉号会话的保持 / 首跳按 tag 收口）。

产出：/opt/litellm/probe-tag-report.md（含 PASS/FAIL 结论与杠杆 B 备选改法，FAIL 才附）。
退出码：0 = 报告已写出（无论结论）；1 = 环境故障无法完成探测。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ENV_PATH = "/opt/litellm/.env"
POOL_PATH = "/opt/litellm/pool-requests.jsonl"
REPORT_PATH = "/opt/litellm/probe-tag-report.md"
URL = "http://127.0.0.1:4000/v1/chat/completions"
MODEL = "mimo-v2.6-flash"
GATED_ACCT = "acct:2"          # 门禁剔除号（config 中无此 tag）→ 测硬过滤行为


def load_env():
    envs = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                envs[k] = v
    return envs


def slot_index(envs):
    """{OCG_KEY_N 原值: OCG_KEY_N} + 兜底 {末6位: OCG_KEY_N}（只在内存，不打印）。"""
    exact, tails = {}, {}
    for k in envs:
        if k.startswith("OCG_KEY_") and not envs[k].startswith("REPLACE"):
            exact[envs[k]] = k
            tails[envs[k][-6:]] = k
    return exact, tails


def classify(keyref, exact, tails):
    if keyref in exact:
        return exact[keyref]
    for t, slot in tails.items():
        if keyref.endswith(t):
            return slot
    return "?"


def send(master_key, tags=None, session_id=None):
    md = {}
    if tags is not None:
        md["tags"] = list(tags)
    if session_id:
        md["session_id"] = session_id
    body = {"model": MODEL, "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1, "temperature": 0}
    if md:
        body["metadata"] = md
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + master_key,
                 "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()[:300].decode("utf-8", "ignore"), t0
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode("utf-8", "ignore"), t0
    except Exception as e:  # noqa: BLE001
        return -1, "%s: %s" % (type(e).__name__, str(e)[:160]), t0


def read_new_rows(offset, timeout=8.0):
    """从 offset 起读完整新行，返回 (rows, new_offset)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        size = os.path.getsize(POOL_PATH)
        if size > offset:
            with open(POOL_PATH, "rb") as f:
                f.seek(offset)
                data = f.read()
            nl = data.rfind(b"\n")
            if nl >= 0:
                chunk = data[:nl + 1]
                rows = []
                for ln in chunk.split(b"\n"):
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rows.append(json.loads(ln))
                    except Exception:
                        continue
                if rows:
                    return rows, offset + len(chunk)
        time.sleep(0.2)
    return [], offset


def litellm_version():
    """容器无 pip/python（distroless）→ 用镜像名做版本标识。"""
    try:
        import subprocess
        r = subprocess.run(["docker", "inspect", "litellm",
                            "--format", "{{.Config.Image}} | started {{.State.StartedAt}}"],
                           capture_output=True, text=True, timeout=30)
        return (r.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def main():
    envs = load_env()
    mk = envs.get("LITELLM_MASTER_KEY", "")
    if not mk:
        print("FATAL: LITELLM_MASTER_KEY missing", file=sys.stderr)
        return 1
    exact, tails = slot_index(envs)
    offset = os.path.getsize(POOL_PATH)
    start_offset = offset
    cases = []          # 结果矩阵

    def case(cid, desc, tags, session_id, expect):
        nonlocal offset
        status, snippet, t0 = send(mk, tags=tags, session_id=session_id)
        rows, offset = read_new_rows(offset)
        # 归属：有 session → 只认 sess==本探针 sid 的行；无 session → 只认 sess=="" 的行
        # （经 sidecar/nginx 的正常流量恒带 sess，可与探针行无歧义区分）
        want_sess = session_id or ""
        mine = [r for r in rows if (r.get("sess") or "") == want_sess]
        # 非 200 的请求不会产生 pool 行：不冒领（迟到的兄弟行交给收尾对账归属给200 case）
        acct = (classify(mine[-1].get("keyRef", ""), exact, tails)
                if mine and 200 <= status < 300 else None)
        cases.append({
            "id": cid, "desc": desc,
            "tags": tags, "session": session_id,
            "http": status, "rows": len(mine), "rows_any": len(rows),
            "acct": acct, "sess": want_sess,
            "expect": expect, "t0f": t0,
            "err": "" if 200 <= status < 300 else snippet.replace("\n", " ")[:200],
            "t0": datetime.fromtimestamp(t0, tz=timezone.utc).strftime("%H:%M:%SZ"),
        })
        time.sleep(0.8)

    # C1 对照：无 tag 无 session（least-busy，任意号均正常）
    case("C1", "对照：无 tags、无 session_id", None, None, "any-pool")
    # C2 tag 过滤 ①：acct:1 ×2
    case("C2a", "tags=[acct:1]", ["acct:1"], None, "OCG_KEY_1")
    case("C2b", "tags=[acct:1] 复测", ["acct:1"], None, "OCG_KEY_1")
    # C3 tag 过滤 ①：acct:5 ×2
    case("C3a", "tags=[acct:5]", ["acct:5"], None, "OCG_KEY_5")
    case("C3b", "tags=[acct:5] 复测", ["acct:5"], None, "OCG_KEY_5")
    # C4 顺序 ②：同 session 先 acct:1 建钉，再 acct:5 撞钉
    # （pool_logger 只记 metadata.session_id[:16] → 探针 sid 必须 ≤16 字符才能对账）
    s1 = ("p2b2probe" + os.urandom(4).hex())[:16]
    case("C4a", "同 session 首跳 tags=[acct:1]（建钉）", ["acct:1"], s1, "OCG_KEY_1")
    case("C4b", "同 session 再发 tags=[acct:5]（tag 撞 affinity）", ["acct:5"], s1,
         "OBSERVE(affinity→KEY_1 / tag→KEY_5 / 401→拒绝)")
    # C5 反向复测：先 acct:5 建钉，再 acct:1 撞钉（排除顺序偶发）
    s2 = ("p2b2probe" + os.urandom(4).hex())[:16]
    case("C5a", "另一 session 首跳 tags=[acct:5]（建钉）", ["acct:5"], s2, "OCG_KEY_5")
    case("C5b", "同 session 再发 tags=[acct:1]（tag 撞 affinity）", ["acct:1"], s2,
         "OBSERVE(affinity→KEY_5 / tag→KEY_1)")
    # C6 对照：钉号 session 不带 tags → affinity 是否仍生效（tag 过滤不破坏普通钉号）
    case("C6", "钉号 session（C4 后）不带 tags", None, s1, "OBSERVE(应=钉号 C4a 的落号)")
    # C7 硬过滤：门禁号 tag（config 无 acct:2 deployment）
    case("C7", "tags=[acct:2]（该 tag 无 deployment，测硬过滤）", [GATED_ACCT], None,
         "ERROR(无匹配 deployment)")

    # ---- 收尾对账：重读本轮全部新行，按 sess/t0 二次归属（防慢写/并发误配）
    with open(POOL_PATH, "rb") as f:
        f.seek(start_offset)
        allrows = []
        for ln in f.read().split(b"\n"):
            ln = ln.strip()
            if ln:
                try:
                    allrows.append(json.loads(ln))
                except Exception:
                    pass
    by_sess = {}
    nosess = []
    for r in allrows:
        s = r.get("sess") or ""
        if s:
            by_sess.setdefault(s, []).append(r)
        else:
            nosess.append(r)
    nosess.sort(key=lambda r: r.get("t0") or 0)
    no_sess_cases = [x for x in cases if not x["session"] and x["http"] == 200]
    # 无 sess 的探针行按发送顺序对齐（只取 t0 落在各 case 时间窗附近的行）
    ni = 0
    for x in no_sess_cases:
        while ni < len(nosess):
            r = nosess[ni]
            if abs((r.get("t0") or 0) / 1000.0 - x["t0f"]) <= 6.0:
                x["acct"] = classify(r.get("keyRef", ""), exact, tails)
                x["rows"] = x["rows"] or 1
                ni += 1
                break
            ni += 1
    for x in cases:
        if x["session"] and 200 <= x["http"] < 300:   # 非 200 的行不存在，不冒领兄弟 case 的行
            rs = by_sess.get(x["session"]) or []
            if rs:
                x["acct"] = classify(rs[-1].get("keyRef", ""), exact, tails)
                x["rows"] = len(rs)

    # ---- 判定
    c = {x["id"]: x for x in cases}
    tag_ok = all(c[k]["acct"] == c[k]["expect"] for k in ("C2a", "C2b", "C3a", "C3b")
                 if c[k]["acct"])
    tag_ok = tag_ok and all(c[k]["acct"] for k in ("C2a", "C2b", "C3a", "C3b"))

    # C4b/C5b 顺序判定：
    #   200 + 落钉号侧   → affinity 优先（tag 未改投、未否决）
    #   200 + 落 tag 目标 → tag 优先（钉被覆盖改投）
    #   401 "tags configuration" → affinity 先定钉 + tag 硬闸门：冲突直接拒绝（钉不被改投）
    def judge(bcase, aff_slot, tag_slot):
        x = c[bcase]
        if x["http"] == 200:
            if x["acct"] == aff_slot:
                return "affinity"
            if x["acct"] == tag_slot:
                return "tag"
            return "inconclusive"
        if x["http"] == 401 and "tags configuration" in (x["err"] or ""):
            return "gate"
        return "inconclusive"

    ord4 = judge("C4b", "OCG_KEY_1", "OCG_KEY_5")
    ord5 = judge("C5b", "OCG_KEY_5", "OCG_KEY_1")
    if ord4 == ord5 and ord4 != "inconclusive":
        order = ord4
    elif ord4 != "inconclusive" and ord5 == "inconclusive":
        order = ord4
    elif ord5 != "inconclusive" and ord4 == "inconclusive":
        order = ord5
    elif ord4 != "inconclusive" and ord5 != "inconclusive":
        order = "inconsistent"
    else:
        order = "inconclusive"

    # C7：非 200 = 硬过滤生效；200 且落号 = tag 被忽略
    c7 = c["C7"]
    if c7["http"] != 200 and c7["rows"] == 0:
        c7_verdict = "hard-filter（无匹配 deployment → 拒绝，符合 enable_tag_filtering 语义）"
    elif c7["http"] == 200 and c7["acct"]:
        c7_verdict = "tag 被忽略（fallback 到 %s）→ 过滤不生效的反证" % c7["acct"]
    else:
        c7_verdict = "不确定（http=%s rows=%d）" % (c7["http"], c7["rows"])

    b2_tag = "PASS" if tag_ok else "FAIL"
    b2_order = order
    overall = "PASS" if (b2_tag == "PASS" and order in ("affinity", "tag", "gate")) else "FAIL"

    # ---- 报告
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = []
    A = lines.append
    A("# B2 专项验证报告 — tag 语义与执行顺序（P2 / S1 进入条件）")
    A("")
    A("- 时间：%s" % now)
    A("- 脚本：`/opt/litellm/probe-tag.py`（只读观测 + 约 10 条 max_tokens=1 探针，直连 `:4000`，不经 sidecar）")
    A("- 配置：`enable_tag_filtering: true`，120 个 deployment 全部带 `tags: [\"acct:<N>\"]`（N∈{1,4,5,6}，各 30）")
    A("- litellm 版本：%s；模型 `%s`；观测面 `pool-requests.jsonl` 的 `keyRef`（精确匹配 .env，输出只用 OCG_KEY_N）"
      % (litellm_version(), MODEL))
    A("")
    A("## 结论（先看这里）")
    A("")
    A("| # | 判定项 | 结果 |")
    A("|---|---|---|")
    A("| ① | `metadata.tags=[\"acct:N\"]` 是否只落该账号 | **%s** |" % b2_tag)
    A("| ② | tag 与 session_affinity 同时存在时谁优先 | **%s** |"
      % {"gate": "affinity 先定钉 + tag 硬闸门：冲突不改投、直接 401 拒绝",
         "affinity": "affinity（钉号）优先，tag 被钉号短路",
         "tag": "tag（过滤）优先，钉号被 tag 压过",
         "inconsistent": "两次撞钉结果不一致（异常，需复跑）",
         "inconclusive": "无法判定（探针落号未取到）"}[order])
    A("| — | **B2 总体（S1 进入条件）** | **%s** |" % overall)
    A("| — | C7 门禁 tag 硬过滤 | %s |" % c7_verdict)
    A("")
    A("### 对 §2.2 / P1 sidecar 的含义")
    A("")
    if order == "gate":
        A("- **实测语义（C4b/C5b 均 401 `Not allowed to access model due to tags configuration`）**："
          "affinity **先**按 session_id 定钉（钉不被 tag 改投），tag 作为**准入闸门**校验选中的 "
          "deployment——两者冲突时请求被 401 拒绝，而不是改投另一账号。")
        A("- 对 §2.2.4 的含义：")
        A("  1. **首跳**（新 session_id、litellm 无钉）：tags 必然生效（① PASS）→ share/L 倾斜的"
          "首跳收口可用；首跳后 litellm 记钉，sidecar 同步记状态表，两者一致。")
        A("  2. **已钉会话绝不可以发 tags**（§2.2.4 step3 「已钉 → 不注入」为**强制约束**而非建议）："
          "发了 = 401 请求失败，不是无害 no-op。")
        A("  3. **重钉（G0–G7）必须先改 session_id**（epoch 改写 → 新键无钉）再发新 tags——"
          "§2.3 的实现路线正是如此，语义满足。")
        A("  4. ⚠️ **sidecar 状态表与 litellm 内存钉号必须同步**：sidecar 重启丢状态后若把已钉会话当"
          "新会话重发 tags → 401 风险（litellm 钉 TTL 21600s 仍在）→ 必须按 §5.2 从 pool_logger "
          "尾部重建状态表 / 或状态未知时不发 tags（fail-open）。**列为 P1 必检项。**")
        A("  5. ext 流量（nginx `ext-<request_id>` 每请求唯一 → litellm 永不记钉）：tags 安全可用。")
    elif order == "affinity":
        A("- **affinity 优先**：钉号会话上再发 tags 是**无效操作**（被短路）。sidecar §2.2.4 的"
          "「step3 已钉 → 不注入 tags」与该语义天然一致（不冲突也无需依赖 tag 纠偏）；"
          "重钉（G0–G7）必须靠 **epoch 改写 session_id（新键无钉）+ 新 tags** 才能换号——这正是"
          " §2.3 的实现路线，语义满足。")
        A("- 首跳（新 session_id、无钉）：tags 必然生效（① PASS），share/L 倾斜的首跳收口可用。")
        A("- ⚠️ 注意：**不能**指望「对已钉会话发新 tags 来改道」；sidecar 若发了，落号仍按钉走，"
          "影子日志会表现为 key_want≠key_actual（属预期，不算 bug）。")
    elif order == "tag":
        A("- **tag 优先**：请求带 tags 时钉号被压过。sidecar **必须严格遵守 §2.2.4 step3**——"
          "已钉会话绝不发 tags，否则每条请求都会重新选号（钉号失效 + 缓存击穿）。"
          "重钉仍走 epoch 改写（新 session_id）后发新 tags，语义满足。")
        A("- 首跳（无钉）：tags 生效（① PASS），share/L 倾斜收口可用。")
    else:
        A("- 顺序未能判定（复跑 `python3 /opt/litellm/probe-tag.py`）。① 若为 FAIL 则不可进入 S1。")
    if b2_tag == "FAIL":
        A("")
        A("## ⚠️ FAIL — 切杠杆 B（模型别名）具体改法【未实施，仅方案】")
        A("")
        A("若 tag 过滤不生效（C2/C3 落号不按 tag），按 §2.3 兜底顺序切杠杆 B（历史 PINNED_ALIASES 已验证）：")
        A("")
        A("1. `gen_config.py`：删掉本次新增的 `tags:` 行与 `enable_tag_filtering: true`；")
        A("2. `dep_block()` 改为**按账号生成模型别名**：`model_name = f\"{m}@A{N}\"`，"
          "`litellm_params.model = openai/{m}`（上游模型不变、api_key 按 N），即每个账号一组别名；")
        A("3. sidecar（P1）按 `key_want` 选模型别名 `f\"{client_model}@A{N}\"` 代替注入 tags；")
        A("4. 重跑 `gen_config.py` → 受控 `docker restart litellm`（低峰）→ 复验。")
        A("5. 回滚锚点：`*.bak-p2-*`。")
    A("")
    A("## 探针矩阵")
    A("")
    A("| case | 请求 metadata | 期望 | HTTP | 落号 | sess | 结果 |")
    A("|---|---|---|---|---|---|---|")
    for x in cases:
        md = []
        if x["tags"]:
            md.append("tags=%s" % json.dumps(x["tags"], ensure_ascii=False))
        else:
            md.append("无 tags")
        md.append("session=%s" % (x["session"] or "无"))
        got = x["acct"] or ("—" if (x["http"] != 200 or x["rows"] == 0) else "?")
        if x["expect"] == "any-pool":
            res = "PASS" if x["http"] == 200 and x["acct"] and x["acct"] != "?" else "FAIL"
        elif x["expect"].startswith("OBSERVE"):
            if x["http"] == 200:
                res = "观测→%s" % x["acct"]
            else:
                res = "观测→HTTP %d %s" % (x["http"], (x["err"] or "")[:70])
        elif x["expect"].startswith("ERROR"):
            res = "PASS" if x["http"] != 200 and x["rows"] == 0 else \
                  ("反证(tag 被忽略)" if x["http"] == 200 else "不确定")
        else:
            res = "PASS" if (x["http"] == 200 and x["acct"] == x["expect"]) else "FAIL"
        A("| %s | %s | %s | %s | %s | %s | %s |"
          % (x["id"], "，".join(md), x["expect"], x["http"], got,
             (x["sess"] or "—"), res))
    A("")
    A("> C4b/C5b 的期望列即为顺序判定依据：落 `affinity→` 侧 = 钉优先（tag 无效）；落 `tag→` 侧 = "
      "tag 改投；**401 `tags configuration` = affinity 先定钉 + tag 硬闸门（冲突拒绝）**。")
    A("")
    A("## 判定明细")
    A("")
    A("- ① tag 过滤：C2a/C2b 落号=%s，C3a/C3b 落号=%s → **%s**"
      % ([c[k]["acct"] for k in ("C2a", "C2b")], [c[k]["acct"] for k in ("C3a", "C3b")], b2_tag))
    A("- ② 顺序：C4b http=%d 落号=%s err=%s（钉=OCG_KEY_1/目标=OCG_KEY_5）；"
      "C5b http=%d 落号=%s err=%s（钉=OCG_KEY_5/目标=OCG_KEY_1） → **%s**"
      % (c["C4b"]["http"], c["C4b"]["acct"], (c["C4b"]["err"] or "—")[:90],
         c["C5b"]["http"], c["C5b"]["acct"], (c["C5b"]["err"] or "—")[:90], order))
    A("- C6（钉号 s1 不带 tags）http=%d 落号=%s（钉=C4a 的 %s，一致=%s）→ affinity 在无 tags 下仍生效"
      % (c["C6"]["http"], c["C6"]["acct"], c["C4a"]["acct"],
         c["C6"]["acct"] == c["C4a"]["acct"]))
    A("- C1 对照落号=%s（无 tag 正常路由 = 过滤开启不伤存量流量）" % c["C1"]["acct"])
    A("- C7（门禁 tag）：http=%d rows=%d → %s" % (c7["http"], c7["rows"], c7_verdict))
    A("")
    A("## 原始证据（pool-requests.jsonl 片段，脱敏）")
    A("")
    A("```")
    A("case  t0(UTC)   http  rows  acct            sess")
    for x in cases:
        A("%-5s %-9s %-5d %-5d %-15s %s"
          % (x["id"], x["t0"], x["http"], x["rows"], x["acct"] or "—", x["sess"] or "—"))
    A("```")
    A("")
    A("## 约束与说明")
    A("")
    A("- 探针直连 `:4000`（隔离 sidecar 变量）；nginx→`:4001` 收口已由冒烟单独验证"
      "（`GET /works/pool/v1/models` → 401 JSON，sidecar log 同刻出现 `status=401`）。")
    A("- 探针请求不带 `pool_src` → pool_logger 记 `src=ext`（约 10 条，max_tokens=1，成本 ≈ $0.001 级）。")
    A("- 本脚本不改任何配置；`routing-share.json` / `pool_logger.py` / 看板未被 P2 触碰。")
    A("- 回滚锚点：`config.yaml.bak-p2-*`、`gen_config.py.bak-p2-*`、`yesifu.conf.bak-p2-*`。")
    A("")
    with open(REPORT_PATH, "w") as f:
        f.write("\n".join(lines))
    print("report written: %s | tag=%s order=%s overall=%s"
          % (REPORT_PATH, b2_tag, b2_order, overall))
    return 0


if __name__ == "__main__":
    sys.exit(main())
