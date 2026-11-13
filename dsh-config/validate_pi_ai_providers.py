#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线校验本机 DSH 的 llm-pi-ai provider 配置（不改运行中实例）。

校验点（依据 cordis Config schema 与号池协议矩阵）：
  1) providers 下每个 provider 的键 ⊆ {displayName, apiKeyEnv, api, baseURL, models}
  2) api ∈ {openai-completions, openai-responses, anthropic-messages}
  3) 每个模型的键 ⊆ {id, name, contextWindow, maxTokens, input, reasoningEfforts, compat}
     且 id/name 必填
  4) 与号池实测协议矩阵一致：completions provider 不得含 muse-spark；responses provider 含 muse-spark
  5) 5 个正式模型仍然全部出现在某个 provider 里（不丢模型）
  6) reasoningEfforts 合法：档位 ⊆ THINKING_LEVELS、非 off 不得为 null、至少声明一个非 off 档位
  7) DeepSeek（经自定义 provider）必须声明 pi-ai 兼容项：
     thinkingFormat=deepseek、requiresReasoningContentOnAssistantMessages=true、
     maxTokensField=max_tokens；且档位只能是 off/low/high/max（上游 catalog 口径）
"""
import sys

import yaml

PATCH = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\<user>\.dsh\profiles\desktop\cordis.patch.yml"
APIS = {"openai-completions", "openai-responses", "anthropic-messages"}
PROVIDER_KEYS = {"displayName", "apiKeyEnv", "api", "baseURL", "models", "headers", "transport",
                 "streamIdleTimeoutMs", "timeoutMs", "retryPolicy"}
MODEL_KEYS = {"id", "name", "contextWindow", "maxTokens", "input", "reasoningEfforts",
              "compat", "displayName", "thinkingBudgets"}
# DSH dsh-llm-pi-ai 的思考档位全集（见 THINKING_LEVELS）；未声明的档位会被钉成 unsupported。
THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
# 上游 zen catalog 对 deepseek-v4.1-flash 的档位：off/low/high/max（其余为 null=不支持）。
DEEPSEEK_LEVELS = {"off", "low", "high", "max"}
# 这些 provider 的 api/baseURL/模型能力来自 pi-ai 内置 catalog，插件只覆盖 apiKey 与模型清单，
# 因此允许省略 api（不按 APIS 枚举校验）。
CATALOG_PROVIDERS = {"opencode-go"}
# claude-haiku-5-5 已按用户要求从 DSH 选择器删除（2026-10-13）
FORMAL = {"mimo-v2.6-flash", "mimo-v2.6-pro", "muse-spark-1.3-contributor",
          "deepseek-v4.1-flash"}

problems = []
raw = open(PATCH, encoding="utf-8").read().replace("!!js ", "")
entries = yaml.safe_load(raw)
pi = next((e for e in entries if isinstance(e, dict) and e.get("id") == "llm-pi-ai"), None)
if not pi:
    print("FAIL: 补丁里找不到 llm-pi-ai 行")
    sys.exit(1)
providers = (pi.get("config") or {}).get("providers") or {}
print(f"providers: {sorted(providers)}")
seen = set()
for name, conf in providers.items():
    if not isinstance(conf, dict):
        problems.append(f"{name}: 不是对象")
        continue
    extra = set(conf) - PROVIDER_KEYS
    if extra:
        problems.append(f"{name}: 未知键 {sorted(extra)}")
    # provider 顶层 maxRetries/maxRetryDelayMs 已被 dsh-llm-pi-ai 移除（加载即抛错），
    # 必须写进 retryPolicy —— 这是最容易踩的一个坑。
    if "maxRetries" in conf or "maxRetryDelayMs" in conf:
        problems.append(f"{name}: 顶层 maxRetries/maxRetryDelayMs 已被上游移除，必须放进 retryPolicy")
    rp = conf.get("retryPolicy")
    if rp is not None:
        if not isinstance(rp, dict):
            problems.append(f"{name}: retryPolicy 不是对象")
        else:
            mode = rp.get("mode")
            if mode not in ("normal", "always"):
                problems.append(f"{name}: retryPolicy.mode={mode!r} 不在 {{normal, always}}")
            rextra = set(rp) - {"mode", "maxRetries", "retryableCodes", "backoff"}
            if rextra:
                problems.append(f"{name}: retryPolicy 未知键 {sorted(rextra)}")
            if "maxRetries" in rp and (not isinstance(rp["maxRetries"], int) or rp["maxRetries"] < 0):
                problems.append(f"{name}: retryPolicy.maxRetries 必须是非负整数")
    sidle = conf.get("streamIdleTimeoutMs")
    if sidle is not None and (not isinstance(sidle, (int, float)) or sidle <= 0):
        problems.append(f"{name}: streamIdleTimeoutMs 必须是正数")
    api = conf.get("api")
    if api is None and name in CATALOG_PROVIDERS:
        pass
    elif api not in APIS:
        problems.append(f"{name}: api={api!r} 不在枚举内")
    models = conf.get("models") or []
    ids = [m.get("id") for m in models if isinstance(m, dict)]
    seen |= set(ids)
    for m in models:
        if not isinstance(m, dict):
            problems.append(f"{name}: 模型项不是对象")
            continue
        mextra = set(m) - MODEL_KEYS
        if mextra:
            problems.append(f"{name}/{m.get('id')}: 模型未知键 {sorted(mextra)}")
        if not m.get("id") or not m.get("name"):
            problems.append(f"{name}: 模型缺 id/name: {m}")
        # —— reasoningEfforts 结构校验（v2.1.2 起）——
        eff = m.get("reasoningEfforts")
        if eff is not None and eff is not False:
            mid = m.get("id")
            if not isinstance(eff, dict) or not eff:
                problems.append(f"{name}/{mid}: reasoningEfforts 必须是非空字典，或 false，或省略")
            else:
                bad = set(eff) - THINKING_LEVELS
                if bad:
                    problems.append(f"{name}/{mid}: reasoningEfforts 未知档位 {sorted(bad)}")
                for level, wire in eff.items():
                    if wire is None:
                        if level != "off":
                            problems.append(f"{name}/{mid}: reasoningEfforts.{level}=null 只有 off 允许")
                    elif not isinstance(wire, str) or not wire:
                        problems.append(f"{name}/{mid}: reasoningEfforts.{level} 的 wire 值必须是非空字符串")
                if not any(level != "off" for level in eff):
                    problems.append(f"{name}/{mid}: reasoningEfforts 至少声明一个非 off 档位")
                # —— DeepSeek 自定义 provider：必须带 pi-ai 兼容项（否则 reasoning_content 400）——
                if api == "openai-completions" and str(mid).startswith("deepseek-"):
                    compat = m.get("compat") or {}
                    tf = compat.get("thinkingFormat")
                    if tf == "deepseek":
                        problems.append(
                            f"{name}/{mid}: litellm 兼容网关不能用 thinkingFormat=deepseek"
                            "（会发 `thinking` 参数被 litellm UnsupportedParamsError 拒绝）；请用 openai")
                    elif tf not in (None, "openai"):
                        problems.append(
                            f"{name}/{mid}: thinkingFormat={tf!r} 不适用于 OpenAI 兼容网关（请用 openai 或省略）")
                    if compat.get("requiresReasoningContentOnAssistantMessages") is not True:
                        problems.append(
                            f"{name}/{mid}: 必须 compat.requiresReasoningContentOnAssistantMessages=true"
                            "（否则回放历史缺 reasoning_content → 上游 400）")
                    if compat.get("maxTokensField") != "max_tokens":
                        problems.append(f"{name}/{mid}: 必须 compat.maxTokensField=max_tokens（DeepSeek 口径）")
                    for level in sorted(set(eff) - DEEPSEEK_LEVELS):
                        problems.append(
                            f"{name}/{mid}: DeepSeek 上游不支持 reasoningEfforts.{level}（zen catalog 为 null）")
                    for level in ("low", "high", "max"):
                        if level not in eff:
                            problems.append(f"{name}/{mid}: DeepSeek 应声明 reasoningEfforts.{level}")
    print(f"  {name}: api={api} models={ids}")
    if api == "openai-completions" and "muse-spark-1.3-contributor" in ids:
        problems.append(f"{name}: muse-spark 在 completions provider 里（上游不支持该协议）")
    if api == "openai-responses" and any(i in ids for i in ("mimo-v2.6-flash", "mimo-v2.6-pro")):
        problems.append(f"{name}: mimo 在 responses provider 里（上游不支持该协议）")

missing = FORMAL - seen
if missing:
    problems.append(f"模型选择器丢失模型: {sorted(missing)}")

print()
if problems:
    print("problems=%d" % len(problems))
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("problems=0  [OK] provider 结构合法、协议分配正确、5 个正式模型齐全")
