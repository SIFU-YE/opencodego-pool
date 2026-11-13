#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话树钉号验收报告（只读）。

用法：
    python3 affinity_report.py                # 默认看最近 120 分钟
    python3 affinity_report.py --minutes 30
    python3 affinity_report.py --all --top 30

看什么：
    ① 单账号树占比（验收目标 100%：一个会话树只落一个账号）
    ② 异常明细：同树多账号 —— 若同时有决策记录，可判断是「重钉」还是「账号被门禁剔除后重试」
    ③ 未带树键的流量（形如 ext-<hash> / 纯哈希）占比：说明有多少流量没被钉号覆盖

账号一律以 `acct#<sha256前8>` 呈现，不打印任何密钥材料。
"""
import argparse
import collections
import hashlib
import json
import re
import time

REQ = "/opt/litellm/pool-requests.jsonl"
DEC = "/opt/litellm/pool-affinity-decisions.jsonl"
# sidecar 的「哈希锚」只有两种形态：外网带来源标记 ext-<hash12>、本地纯 hash16。
# 其余（客户端自带会话号、插件注入的树键）都算树键。
HASH_ANCHOR_RE = re.compile(r"^(ext-[0-9a-f]{12}|[0-9a-f]{16})$")
ALIAS_RE = re.compile(r"@a(\d+)$")


def label(acct):
    if not acct or acct == "-":
        return "unknown"
    return "acct#" + hashlib.sha256(str(acct).encode("utf-8")).hexdigest()[:8]


def account_of(rec):
    """账号归属：优先用 model 里的池内别名 @aN（两种协议都可靠）；
    否则退回 keyRef/key（responses 协议的埋点常为空）。"""
    model = str(rec.get("model") or "")
    m = ALIAS_RE.search(model)
    if m:
        return "a" + m.group(1)
    raw = rec.get("keyRef") or rec.get("key") or ""
    return label(raw) if raw else "unknown"


def is_tree_key(sess):
    if not sess:
        return False
    return not HASH_ANCHOR_RE.match(sess)


def load_decisions():
    out = collections.defaultdict(list)
    try:
        for line in open(DEC, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            out[str(r.get("tree"))].append(r)
    except FileNotFoundError:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=120)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()
    cutoff = 0 if a.all else int(time.time()) - a.minutes * 60

    trees = collections.defaultdict(collections.Counter)
    total = 0
    untagged = 0
    for line in open(REQ, encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = r.get("ts") or 0
        if cutoff and ts and ts < cutoff:
            continue
        total += 1
        sess = str(r.get("sess") or "")
        if not is_tree_key(sess):
            untagged += 1
            continue
        trees[sess][account_of(r)] += 1

    multi = {t: c for t, c in trees.items() if len(c) > 1}
    single = len(trees) - len(multi)
    print(f"窗口: {'全部' if a.all else str(a.minutes) + ' 分钟'}   请求 {total} 条")
    print(f"带树键请求: {total - untagged}   未带树键(ext-/哈希锚): {untagged}")
    print(f"会话树: {len(trees)} 棵   单账号树: {single}   多账号树(异常): {len(multi)}")
    if trees:
        print(f"单账号树占比: {single / len(trees) * 100:.1f}%   （验收目标 100%）")
    if multi:
        print("\n异常明细（同树多账号）:")
        dec = load_decisions()
        for t, c in sorted(multi.items(), key=lambda kv: -sum(kv[1].values()))[: a.top]:
            print(f"  {t}  ->  {dict(c)}")
            for r in dec.get(t, [])[-3:]:
                print(f"     决策: ts={r.get('ts')} key_want={r.get('key_want')} mode={r.get('mode')} injected={r.get('injected')} reason={r.get('reason')}")
    if trees:
        print("\n树键分布（按请求数 Top %d）:" % a.top)
        for t, c in sorted(trees.items(), key=lambda kv: -sum(kv[1].values()))[: a.top]:
            n = sum(c.values())
            mark = "OK " if len(c) == 1 else "!! "
            print(f"  {mark}{t}  请求 {n}  账号 {dict(c)}")


if __name__ == "__main__":
    main()
