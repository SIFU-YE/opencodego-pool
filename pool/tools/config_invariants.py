#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""config.yaml 结构不变量（S4a 别名机制版）。

不变量：
  1) 每个模型 × 每个账号都有单账号别名组 <model>@a<N>（命中确定性 = 该账号）
  2) 别名组的 api_key 与别名序号一致
  3) 正式模型组仍存在（客户端可见名不变）
  4) 已移除不可靠的标签路由：无 enable_tag_filtering、无 acct: 标签
  5) 别名组恰好一个 deployment（否则不叫"单账号"）
  6) 配置里的账号 ⊇ 份额引擎当前健康账号
"""
import collections
import json
import re
import subprocess
import sys
import os

CFG = "/opt/litellm/config.yaml"
SHARE = "/opt/litellm/routing-share.json"
GEN = "/opt/litellm/gen_config.py"
FORMAL = ["mimo-v2.6-flash", "mimo-v2.6-pro", "muse-spark-1.3-contributor",
          "claude-haiku-5-5", "deepseek-v4.1-flash"]

passed = failed = 0


def ok(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} {extra}")


cfg = open(CFG, encoding="utf-8").read()
blocks = re.findall(r"(?ms)^  - model_name: (\S+)\n(.*?)(?=^  - model_name: |\Z)", cfg)
groups = collections.defaultdict(list)
for name, body in blocks:
    key = re.search(r"api_key: os\.environ/(OCG_KEY_\d+)", body)
    groups[name].append(key.group(1) if key else None)

alias_names = [n for n in groups if "@a" in n]
formal_names = [n for n in groups if "@a" not in n]

print("=== 别名机制结构不变量 ===")
ok("正式模型组齐全", set(formal_names) >= set(FORMAL), f"{sorted(formal_names)}")
ok("无 enable_tag_filtering 残留", "enable_tag_filtering" not in cfg)
ok("无 acct: 标签残留", "acct:OCG_KEY_" not in cfg)

alias_by_model = collections.defaultdict(dict)
bad_pair = []
for n in alias_names:
    m = re.match(r"^(.+)@a(\d+)$", n)
    if not m:
        bad_pair.append((n, "命名异常"))
        continue
    model, idx = m.group(1), m.group(2)
    if groups[n] != ["OCG_KEY_" + idx]:
        bad_pair.append((n, groups[n]))
    alias_by_model[model][idx] = groups[n]
ok("别名命名与 api_key 一致", not bad_pair, str(bad_pair[:4]))
ok("别名组恰好一个 deployment（单账号）",
   all(len(v) == 1 for n, v in groups.items() if "@a" in n),
   str([n for n, v in groups.items() if "@a" in n and len(v) != 1][:3]))

acct_sets = {frozenset(v) for v in alias_by_model.values()}
ok("每个模型都有完整的账号别名覆盖", len(alias_by_model) == len(FORMAL) and len(acct_sets) == 1,
   f"模型={sorted(alias_by_model)} 覆盖={[sorted(s) for s in acct_sets]}")
if alias_by_model:
    cov = sorted(next(iter(alias_by_model.values())))
    print(f"  别名账号覆盖: {cov}（{len(cov)} 个账号 × {len(alias_by_model)} 模型 = {len(alias_names)} 个别名组）")

try:
    sh = json.load(open(SHARE, encoding="utf-8"))
    healthy = {a["key_ref"] for a in sh.get("accounts", [])
               if isinstance(a.get("share"), (int, float)) and a.get("share") > 0}
    # alias_by_model 的值是 {序号: [key_ref]}，这里要取 key_ref 本身
    cfg_accts = set()
    for per_idx in alias_by_model.values():
        for keys in per_idx.values():
            cfg_accts.update(keys)
    ok("配置账号 ⊇ 份额引擎健康账号", healthy <= cfg_accts, f"缺 {sorted(healthy - cfg_accts)}")
except Exception as e:
    print("  WARN 无法读取 routing-share.json:", e)

print("\n=== gen_config 再生成稳定性（POOL_SKIP_RESTART=1）===")
before = cfg
e2 = dict(os.environ)
e2["POOL_SKIP_RESTART"] = "1"
r = subprocess.run([sys.executable, GEN], capture_output=True, text=True, env=e2, timeout=120)
after = open(CFG, encoding="utf-8").read()
tail = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
print("  生成器输出尾行:", tail[:100])
ok("再生成后配置逐字节不变", before == after,
   "差异行数=%d" % (len(before.splitlines()) if before != after else 0))

print(f"\ninvariants: pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
