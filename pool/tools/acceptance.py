#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""号池「会话树钉号」一键验收。

用法：python3 /opt/litellm/tools/acceptance.py            # 全量
      python3 /opt/litellm/tools/acceptance.py --quick    # 跳过协议矩阵与压测

判定：
  PASS  必须成立
  WARN  已知待办（如 mimo 在 responses 协议下 400 —— 待 DSH 侧协议修复）
  FAIL  需要立即处理
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:4001"
ENV = "/opt/litellm/.env"
CFG = "/opt/litellm/config.yaml"
TOOLS = "/opt/litellm/tools"

results = []


def record(name, verdict, detail=""):
    results.append((verdict, name, detail))
    print(f"  [{verdict}] {name}" + (f" — {detail}" if detail else ""))


def master_key():
    for line in open(ENV, encoding="utf-8"):
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def post(path, body, key, extra_headers=None, timeout=90):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 **(extra_headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)[:200]


def sub(name, cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        tail = [l for l in (r.stdout or "").strip().splitlines() if l.strip()][-1:]
        record(name, "PASS" if r.returncode == 0 else "FAIL", (tail[0] if tail else "")[:120])
        return r.returncode == 0
    except Exception as e:
        record(name, "FAIL", str(e)[:120])
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    key = master_key()
    py = sys.executable

    print("=== 1) 代码级测试 ===")
    sub("sidecar 单元测试（27 项）", [py, os.path.join(TOOLS, "test_sidecar_units.py")])
    sub("配置标签路由不变量", [py, os.path.join(TOOLS, "config_invariants.py")])

    print("\n=== 2) 热路径冒烟 ===")
    code, body = post("/v1/chat/completions",
                      {"model": "mimo-v2.6-flash", "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": 5}, key)
    record("chat/completions + mimo-flash（未带标签）", "PASS" if code == 200 else "FAIL", f"http={code}")
    code, body = post("/v1/responses",
                      {"model": "deepseek-v4.1-flash", "input": "ping", "max_output_tokens": 16}, key)
    record("responses + dsv4.1-flash（未带标签）", "PASS" if code == 200 else "FAIL", f"http={code}")

    print("\n=== 3) 树键钉号（两种协议，同一棵树）===")
    import time as _time
    tree = "acc-check-01"
    t0 = int(_time.time() * 1000)     # 埋点 ts 是毫秒；只统计本次验收产生的记录
    import re as _re
    alias_re = _re.compile(r"@a(\d+)$")
    seen_alias = []

    def tree_post(path, body, label):
        code, text = post(path, body, key)
        model = ""
        try:
            model = json.loads(text).get("model", "")
        except Exception:
            pass
        m = alias_re.search(str(model))
        if m:
            seen_alias.append("a" + m.group(1))
        record(label, "PASS" if code == 200 else "FAIL", f"http={code} 响应model={model or '?'}")
        return code

    tree_post("/v1/chat/completions",
              {"model": "mimo-v2.6-flash",
               "messages": [{"role": "user", "content": f"ping [pool-tree: {tree}]"}], "max_tokens": 5},
              "chat 带树键")
    tree_post("/v1/responses",
              {"model": "deepseek-v4.1-flash", "input": f"ping [pool-tree: {tree}]", "max_output_tokens": 16},
              "responses 带树键")

    if seen_alias:
        uniq = set(seen_alias)
        record("同树同账号（响应里的池内别名一致）", "PASS" if len(uniq) == 1 else "FAIL",
               f"别名={sorted(uniq)}")
    else:
        record("同树同账号（响应里的池内别名一致）", "WARN", "响应未回显别名（可能 mode=shadow）")

    # 决策与实际落号
    want = None
    try:
        for line in open("/opt/litellm/pool-affinity-decisions.jsonl", encoding="utf-8", errors="replace"):
            r = json.loads(line)
            if r.get("tree") == tree:
                want = r
    except Exception:
        pass
    if want and want.get("injected"):
        record("决策：已改写为池内别名", "PASS", f"key_want={want.get('key_want')} mode={want.get('mode')}")
    else:
        record("决策：已改写为池内别名", "WARN", f"决策记录={want}")

    known, unknown = set(), 0
    try:
        for line in open("/opt/litellm/pool-requests.jsonl", encoding="utf-8", errors="replace"):
            r = json.loads(line)
            if r.get("sess") != tree:
                continue
            if r.get("ts") and r["ts"] < t0:
                continue
            m = alias_re.search(str(r.get("model") or ""))
            kw = str(r.get("key_want") or "")
            if m:
                known.add("a" + m.group(1))
                continue
            if kw.startswith("OCG_KEY_"):
                known.add("a" + kw.split("_")[-1])
                continue
            acct = r.get("keyRef") or r.get("key") or ""
            if acct:
                known.add("k:" + acct[-6:])
            else:
                unknown += 1
    except Exception:
        pass
    if known:
        record("落号归属（埋点）同树单账号", "PASS" if len(known) == 1 else "FAIL", f"账号={sorted(known)}")
    if unknown:
        record("埋点缺口：部分请求无账号字段", "WARN",
               f"{unknown} 条（responses 协议埋点记录改写前的名字；以响应体 model 为准）")

    print("\n=== 4) 协议矩阵（上游能力，供 DSH provider 选型）===")
    matrix = [("chat", "/v1/chat/completions", "mimo-v2.6-flash",
               {"model": "mimo-v2.6-flash", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}),
              ("chat", "/v1/chat/completions", "muse-spark-1.3-contributor",
               {"model": "muse-spark-1.3-contributor", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}),
              ("responses", "/v1/responses", "mimo-v2.6-flash",
               {"model": "mimo-v2.6-flash", "input": "ping", "max_output_tokens": 16}),
              ("responses", "/v1/responses", "muse-spark-1.3-contributor",
               {"model": "muse-spark-1.3-contributor", "input": "ping", "max_output_tokens": 16})]
    if a.quick:
        record("协议矩阵", "WARN", "--quick 跳过")
    else:
        for proto, path, model, body in matrix:
            code, _ = post(path, body, key, timeout=60)
            if code == 0:                       # 超时/瞬时错误 → 重试一次（更长超时）
                code, _ = post(path, body, key, timeout=150)
            expected = 200 if (proto == "chat" and model.startswith("mimo")) or \
                              (proto == "responses" and model.startswith("muse")) else 400
            verdict = "PASS" if code == expected else "WARN"
            record(f"{proto} + {model}", verdict, f"http={code}（期望 {expected}）")

    print("\n=== 5) 会话树钉号总览 ===")
    sub("affinity_report（单账号树占比）", [py, os.path.join(TOOLS, "affinity_report.py"), "--all", "--top", "5"])

    print("\n=== 汇总 ===")
    n_pass = sum(1 for v, _, _ in results if v == "PASS")
    n_warn = sum(1 for v, _, _ in results if v == "WARN")
    n_fail = sum(1 for v, _, _ in results if v == "FAIL")
    print(f"PASS={n_pass}  WARN={n_warn}  FAIL={n_fail}")
    for v, n, d in results:
        if v != "PASS":
            print(f"  {v}: {n} — {d}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
