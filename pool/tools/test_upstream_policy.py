#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上游通道策略单测：仅 OpenCode Go 订阅，禁用 Zen / Console（付费）通道。

被测：pool/upstream_policy.py（纯函数，无 I/O）。
    python3 pool/tools/test_upstream_policy.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.abspath(os.environ.get("UPSTREAM_POLICY_PATH", os.path.join(HERE, "..", "upstream_policy.py")))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  :: " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


if not os.path.exists(MOD):
    print("SKIP: %s 不存在" % MOD)
    sys.exit(0)

# 直连反代白名单默认只含本机 loopback；部署机可用 POOL_DIRECT_* 指向自己的网关地址。
# 单测用一个示例主机验证「环境变量可扩展白名单」，不使用任何真实内网地址。
os.environ.setdefault("POOL_DIRECT_OPENAI",
                      "http://relay.example.com:4002/inference/go/openai/v1")

spec = importlib.util.spec_from_file_location("upstream_policy", MOD)
up = importlib.util.module_from_spec(spec)
spec.loader.exec_module(up)

# ---- 允许：Go 订阅通道（规范 + 旧别名） ----
for b in ("https://opencode.ai/inference/go/openai/v1",
          "https://opencode.ai/inference/go/anthropic",
          "https://opencode.ai/zen/go/v1",
          "https://opencode.ai/inference/go/v1/usage",
          # 2026-10-28 出口拆分：直连反代（非 muse 模型绕开日本出口）。
          # 本机反代 + 由 POOL_DIRECT_* 配置的反代主机都应在白名单内。
          "http://127.0.0.1:4002/inference/go/anthropic",
          "http://localhost:4002/inference/go/openai/v1",
          "http://relay.example.com:4002/inference/go/openai/v1"):
    good, why = up.classify(b)
    ok("允许 Go 通道: %s" % b, good, why)

# ---- 禁止：Zen / Console（付费）通道 ----
for b in ("https://opencode.ai/zen/v1",
          "https://opencode.ai/zen/v1/models",
          "https://opencode.ai/inference/v1",
          "https://opencode.ai/inference/v1/models",
          "https://opencode.ai/inference/openai/v1",
          "https://opencode.ai/inference/openai/v1/chat/completions",
          "https://opencode.ai/inference/anthropic/v1/messages",
          "https://opencode.ai/inference/google/v1beta/models/x",
          "https://opencode.ai/inference/systemone/v1/systemone",
          "https://<pool-host>/works/pool/zen",
          "",
          None):
    good, why = up.classify(b)
    ok("拒绝非 Go 通道: %r" % (b,), not good, why)

# ---- 不得误伤：Go 的 anthropic/openai 子路径要能通过 ----
ok("Go anthropic 不误伤", up.classify("https://opencode.ai/inference/go/anthropic")[0])
ok("Go openai 不误伤", up.classify("https://opencode.ai/inference/go/openai/v1")[0])

# ---- 行级校验 ----
lines = [
    "  - model_name: a",
    "    litellm_params:",
    "      api_base: https://opencode.ai/inference/go/openai/v1",
    "      api_key: os.environ/OCG_KEY_2",
    "  - model_name: b",
    "    litellm_params:",
    "      api_base: https://opencode.ai/zen/v1",       # 违规
]
bases = up.extract_bases(lines)
ok("extract_bases 抽出 2 条", bases == ["https://opencode.ai/inference/go/openai/v1",
                                       "https://opencode.ai/zen/v1"], bases)
bad = up.check_lines(lines)
ok("check_lines 只报违规那条", len(bad) == 1 and bad[0][0].endswith("/zen/v1"), bad)
ok("合规产物无违规", up.check_lines(["      api_base: https://opencode.ai/inference/go/openai/v1"]) == [])

try:
    up.assert_lines(lines)
    ok("assert_lines 对违规抛错", False, "未抛错")
except RuntimeError as e:
    ok("assert_lines 对违规抛错", "策略违规" in str(e), str(e)[:60])
ok("assert_lines 对合规返回条数", up.assert_lines(["      api_base: https://opencode.ai/inference/go/openai/v1"]) == 1)

print()
if fails:
    print("FAILED %d: %s" % (len(fails), fails))
    sys.exit(1)
print("ALL PASS")
