# Changelog · @local/dsh-pool-affinity

## 2.1.5 — 2026-10-21

### muse-spark 加回思考档位（网关侧能力已补齐）

- `litellm-reaspons` / `muse-spark-1.3-contributor` 重新声明
  `reasoningEfforts: off/minimal/low/medium/high/xhigh`（**不含 `max`**，官方目录里 muse 的
  `max` 映射是 `null`）；`off: null` 仍是默认，即默认请求不带 `reasoning.effort`。
- 前提：号池 reconciler 现在会给每条 deployment 登记
  `model_info.supports_reasoning: true`，并且更新通道改走
  `PATCH /model/{id}/update`（旧 `POST /model/update` 不写 model_info）。
  线上验证：带 `reasoning.effort: low` 的 `/v1/responses` 请求返回 **HTTP 200**。
- 仍是**单条** muse 路由，不新增 provider、不改名字。

## 2.1.4 — 2026-10-21

### 开箱即用：模型元数据按 opencode 官方目录补齐 + 去掉两个隐式前置条件

- **apiKeyEnv 改为 `LITELLM_API_KEY`**（原 `LITELLM_REASPONS_API_KEY` /
  `LITELLM_COMPLETIONS_API_KEY`）。后两个名字在多数部署里根本不存在，装完插件
  还得手工补凭据才能用，与「装好即用」矛盾。分协议独立密钥的部署可在 profile 里覆盖。
- **muse-spark-1.3-contributor**：`contextWindow 1048576 / maxTokens 131072`
  （原 858860/130000 是估的）；**移除 reasoningEfforts** —— 号池 litellm 的 cost map
  对它没有 `supports_reasoning`，声明档位会让 DSH 发 `reasoning.effort`，
  上游直接 400 `UnsupportedParamsError`。
- **deepseek-v4.1-flash / mimo-v2.6-flash / mimo-v2.6-pro**：contextWindow、
  maxTokens、reasoningEfforts、compat 全部对齐官方目录（DSH 内置
  `opencode-go.json` 即其镜像）；显示名改为官方名（DeepSeek V4.1 Flash 等）。
- 结论：**装本插件 + 一个 `LITELLM_API_KEY` 凭据**即可得到三条可用号池路由
  （completions / responses / 直连 opencode-go），不再需要 profile 里另写 llm-pi-ai。

## 2.1.3 — 2026-10-21

### 修复：v2.1.2 的 `thinkingFormat: deepseek` 触发 litellm `UnsupportedParamsError`

- 现象（升级 v2.1.2 重启后首轮）：400
  `litellm.UnsupportedParamsError: openai does not support parameters: ['thinking'],
  for model=deepseek-v4.1-flash. To drop these, set litellm.drop_params=True ...`
- 原因：号池 `litellm-completions` 的 baseURL 是 **OpenAI 兼容网关**；pi-ai 的
  `thinkingFormat: deepseek` 会发顶层 `thinking: {type: enabled}`（DeepSeek 原生 API 参数），
  litellm 按 openai provider 校验 → 直接拒绝。
- 修复：`deepseek-v4.1-flash` 的 compat 改为 `thinkingFormat: openai`（只发
  `reasoning_effort`，与上线前的旧行为一致）。`requiresReasoningContentOnAssistantMessages: true`
  **保留**——它只控制历史回放时补 `reasoning_content`，与 thinking 参数格式无关：
  - `requiresReasoningContentOnAssistantMessages` → 修 “reasoning_content must be passed back”；
  - `thinkingFormat` → 决定 thinking 参数怎么发；OpenAI 兼容网关只能用 `openai`。
- `dsh-config/validate_pi_ai_providers.py` 同步收紧：DeepSeek 自定义 provider 禁止
  `thinkingFormat: deepseek`，要求 `openai` 或省略。
- 其余（mimo/muse/opencode-go）配置不变。

## 2.1.2 — 2026-10-21

### 修复：DeepSeek 思考模式 `reasoning_content` 400 + reasoningEfforts 全模式对齐

- 现象（sandbox17/v1，2026-10-21）：长会话续写返回 400
  `The reasoning_content in the thinking mode must be passed back to the API`
  （request_id `44514e30-…` / `745b7429-…`，turns 10–11）。
- 根因：`litellm-completions` 是**自定义 provider**（id 不是 `deepseek`，baseURL 也不含
  `deepseek.com`），pi-ai 的自动检测不会打开 DeepSeek 兼容项，而 model `compat` 里也没写：
  1) 回放历史时不给“无 reasoning 的 assistant 消息”补 `reasoning_content` → 上游校验失败；
  2) thinking 参数按通用 OpenAI 口径发送，而不是 DeepSeek 的 `thinking:{type:enabled}`。
  会话证据：失败请求前最新 assistant 消息只有 `tool_call`、replay 块无 reasoning；
  这条消息留在历史里，导致之后每轮继续 400。
- 修复：按 pi-ai 官方 `deepseek` / `opencode-go` catalog 补齐每个模型的
  `reasoningEfforts` 与 `compat`：
  - `deepseek-v4.1-flash`：`off/low/high/max`；compat 增
    `thinkingFormat: deepseek`、`requiresReasoningContentOnAssistantMessages: true`、
    `supportsStore/supportsDeveloperRole: false`、`maxTokensField: max_tokens`、
    `supportsStrictMode: true`。
  - `mimo-v2.6-flash/pro`：`off/minimal/low/medium/high`；compat 与官方 catalog 对齐。
  - `muse-spark-1.3-contributor`（responses）：`off/minimal/low/medium/high/xhigh`。
  - `opencode-go/deepseek-v4.1-flash`：沿用 provider 目录自带能力，不重复声明。
- 说明：profile 若同时定义 `- id: llm-pi-ai`（本机当前就是），profile 层优先；
  本机 profile 的 provider 段已同步为同一份配置。
- 校验：`python dsh-config/validate_pi_ai_providers.py <profile>` 新增
  reasoningEfforts/compat 检查并通过。
- 重启 DSH 生效；历史会话续写应不再 400（compat 会补空 `reasoning_content`）。

## 2.1.1 — 2026-10-20

### 修复：provider 路由改为单实例覆盖（v2.1.0 的多实例方案不可用）

- v2.1.0 用 insert 新增 dsh-pool-affinity-llm 行（第二个 dsh-llm-pi-ai 实例）。
  实测该行运行时 `fiberPhase=failed`（同包多实例在适配器/模型发现注册阶段冲突），
  结果是号池 provider 路由**整批缺失**，模型选择器里看不到池模型。
- 改为 Loader 的 **id-targeted override**：`- id: llm-pi-ai` 覆盖 base 行，config 整体替换为
  号池 providers。单实例，无重复注册。
- 前提（不变）：profile patch 里不得再定义 `- id: llm-pi-ai`（否则 profile 层会盖掉插件）。

验证（重启后）：`plugin_manager list_plugins` 中只应有一个 `include:llm-pi-ai`（active），
不再出现 `include:dsh-pool-affinity-llm`；模型选择器可见三条号池路由。
## 2.1.0 — 2026-10-20

### 新增：号池 provider 路由并入本插件

- `cordis.patch.yml` 的 `insert` 列表新增一行 `dsh-pool-affinity-llm`
  （`@deepseek-ai/dsh-llm-pi-ai`），承载三条号池路由：
  `litellm-reaspons`（openai-responses）、`litellm-completions`（openai-completions）、
  `opencode-go`。
- 内置 **T0 配置**（见内部文档《上游静默与首字节治理》，位置见仓库根 `PRIVATE-DATA.md`）：
  - `streamIdleTimeoutMs: 2147483647`（= `MAX_TIMER_DELAY_MS`，等价关闭 pi-ai 看门狗；
    schema 下限 > 0，不能写 0）
  - `retryPolicy.maxRetries: 2`（默认 `retryableCodes` 含 `TIMEOUT` → 超时自动重试）
  - `retryPolicy.backoff: { initialDelayMs: 800, maxDelayMs: 15000, jitterRatio: 0.2 }`
- 目的：**只装一个插件**即可完成「号池接入 + 会话钉号」，换机/重装只需搬一处配置。

### 迁移（必须成对执行）

1. 备份 `~/.dsh/profiles/desktop/cordis.patch.yml`；
2. 删除其中 `- id: llm-pi-ai` 整段（否则同 provider 路由名重复注册 → 插件加载失败：
   `registering a route another adapter already owns`）；
3. 重启 DSH。

回滚同样成对：恢复插件 `cordis.patch.yml` 与 profile patch 的迁移前快照，再重启。

### 未变

- `index.js` 的树键注入逻辑、零宽编码规格、sidecar 消费契约均未改动
  （`selftest.mjs` 81 项、`simulate.mjs` 30 项在本次发布中仍全过）。

## 2.0.0 — 2026-10-18

- 标记改为零宽字符载荷（U+200B/U+200C/U+200D/U+2060）：界面不可见，出站请求体字节保留。
- 编码紧凑化（uuid 128bit → 64 符号），载荷 74 个不可见字符。

## 1.0.1 — 2026-10-17

- 可见字面量 `[pool-tree: <id>]` 时代；修复升级识别。
