# DSH 侧插件：号池接入（会话钉号 + provider 路由 · v2.1）

给 DSH（DeepSeek Harness）用的 Host 插件，v2.1.0 起承担**两件事**：

> 1. 在每个「进入模型」的消息序列末尾，附加一个稳定的会话树键；
> 2. 提供号池的 provider 路由（`litellm-*` / `opencode-go`），含 **T0：看门狗上限 + 重试/退避**
>    —— 装一个插件即可接入号池，不必再手改 profile 的 provider 段。

号池 sidecar 读到该树键后，把整个会话树（队长会话 + 全部子代理）钉到**同一个上游账号**。

**v2.0.0 起标记是零宽字符载荷（U+200B/U+200C/U+200D/U+2060），界面上完全看不见**；
但它在**出站请求体字节里照旧存在**，sidecar 直接按字节解码，钉号语义一字未改。

---

## 1. 为什么改（v1.0.x 的可见性 bug，已复现）

v1.0.0 / v1.0.1 把**可见字面量** `[pool-tree: <根会话 id>]` 追加到最后一条消息文本末尾：

```
帮我重构这段代码
[pool-tree: session-fbb609ed-ad3b-4d9f-a1e1-05008ee4c39b]
```

`agent/pre-step` 返回的 `decision.messages` 会被 DSH **原样持久化并渲染**
（`dsh-agent-loop/lib/index.js` L1067-L1073）：

```js
if (firstAttempt) for (const message of decision.messages)
  if (message.source.kind === "runtime-context") { … }        // 用 loop 自己的投影顶替
  else this.session.append("user/message", message, { surfaceOp: "append" });   // ← 落库 → GUI 渲染
```

所以标记必然出现在会话记录与 GUI 转录里。复现证据（本仓库 `_ops-fix/affinity-invisible/`）：

```
$ python _ops-fix/_dump_sessions.py fbb609ed
$ grep -n "pool-tree" _ops-fix/_sessions_text/session-fbb609ed-*.txt
1217: [user/message] {"content": [{"type": "text", "text": "相关信息从…搜索\n[pool-tree: session-fbb609ed-…]"}]}
```

## 2. 为什么不做「模型可见但界面不渲染」的隐藏通道（接缝调研结论）

调研过 `app.asar` 里全部相关实现，结论是**没有可用的隐藏文本通道**，依据如下：

| 候选接缝 | 结论 | 证据（asar 内路径 + 行号） |
|---|---|---|
| `agent/pre-step` 的 `decision.messages` | ❌ 只以 `surfaceOp:'append'` 落库，落库即渲染 | `dsh-agent-loop/lib/index.js` L1067-L1073 |
| 不渲染的 content part 类型 | ❌ 不存在；`text/image/file/tool-*` 都会渲染 | `dsh-llm/lib/types/content.js` 全文 |
| surface **replacement**（"replacement copies stay model-only"） | ⚠️ 理论可行但接缝不可达 | `dsh-client-ui-chat/lib/client.js` L9464-L9471 说明 replacement 副本只给模型；但 pre-step 无权指定 surfaceOp，且补一条 replace 事件必须在 `session/event` 回调里再 `append`，被 `dsh-session/lib/index.js` L1568-L1572 明确拒绝：`session append cannot reenter while another append is being published` |
| message projection（`ctx.sessions.registerMessageProjection`） | ⚠️ 能改派生内容不改落库内容，但代价高 | `dsh-session/lib/index.js` L1729-L1736、L429-L439。要生效必须**预测未来 seq**（pre-step 时新消息还没落库，中间还夹着数量不定的 `system/message`），或退化成「首个请求没有标记」；且**插件被卸载后，用过该 projection 的会话会拒绝再派生**（L1724-L1726），是会把用户会话卡死的失败模式 |
| provider 动态 header | ❌ 不存在该 API | `agent/request` 只能改 `LlmCallConfig`；`llm/stream` 无 headers |

因此选择**零宽字符编码**：注入点、注入位置、钉号语义全不变，只是把「可读文本」换成「不可见格式字符」。

## 3. 编码规格（zw-tree-key v1）

### 3.1 符号表（4 个安全零宽字符）

| 符号值 | 码点 | 名称 | 说明 |
|---|---|---|---|
| 0 | U+200B | ZERO WIDTH SPACE | 不是 Unicode White_Space；JS `trim()` / Python `strip()` 都去不掉 |
| 1 | U+200C | ZERO WIDTH NON-JOINER | 同上 |
| 2 | U+200D | ZERO WIDTH JOINER | 同上 |
| 3 | U+2060 | WORD JOINER | 同上，且**专门设计为不可断行、不可见** |

**刻意不用 U+FEFF（ZWNBSP/BOM）**：它在 JS 的 `String.prototype.trim()` 白名单里，会被末尾清理吃掉。

### 3.2 载荷布局

```
MAGIC(4 符号) + flag(1 符号) + body + 校验和(4 符号) + 终止哨兵(1 符号)
MAGIC = U+2060 U+200D U+200B U+200C        (WJ ZWJ ZWSP ZWNJ，四符号互不相同)
```

| flag | 适用键 | body |
|---|---|---|
| 0 | 裸 uuid（`8-4-4-4-12` 小写 hex） | 16 字节 → 64 符号 |
| 1 | `session-<uuid>`（**线上常见形态**） | 16 字节 → 64 符号 |
| 2 | 其它 `[A-Za-z0-9_-]{4,64}` 串 | 4 符号字节长度 + 每字节 4 符号 |

- 每字节 → 4 个符号（2 bit/符号，MSB 在前）。
- **校验和** = `sum(flag 与 body 各位符号值) mod 256`，编码成 4 符号。必需：零宽字符只有 4 种，
  MAGIC 这个 4 符号窗口在载荷内部约有 0.4%/窗口 的概率**碰巧出现**（本仓库自检里就构造出了这种样本），
  没有校验和就可能把「碰巧的内部 MAGIC」解成一个看起来合法的假树键。
- 常见树键 `session-<uuid>` 的载荷长度 = **4+1+64+4+1 = 74 个零宽字符**。
- 解码规则：从**最左**的 MAGIC 开始逐个候选解码，取第一个「校验和通过」的；
  严格模式下还要求载荷**正好落在文本末尾**。真正的载荷起点一定在内部偶发 MAGIC 之前，
  所以从左扫描是正确且确定的（用 `lastIndexOf`/`rfind` 会从内部假 MAGIC 起解 → 解错）。

### 3.3 注入位置与不变式

1. **只在消息文本末尾追加**（前缀可见文本一个字节都不动）→ 不伤前缀缓存。
2. **幂等**：文本末尾已有合法载荷则改写值不叠加；同树同值第二次调用直接返回原数组引用。
3. **fail-open**：任何异常、键不合 `{4,64}` 形状、找不到可注入目标 → 原样返回下游决策。
4. **跳过 `source.kind === 'runtime-context'` 的消息**：DSH 会用 loop 自己的投影顶替它
   （`dsh-agent-loop/lib/index.js` L1067-L1073），把标记打上去等于没注入；
   若整个 decision 只有 runtime-context，则**不注入**（不硬塞）。
   > 顺带修掉一个 v1 的隐藏缺陷：v1 无条件打「最后一条」，一旦最后一条是 runtime-context，
   > 标记就被 loop 丢掉 → 该请求其实没钉上号。
5. **升级路径**：若某条消息里已有旧的可见字面量 `[pool-tree: …]`（`markerKey` 可配），
   会把它**擦掉并替换成零宽载荷**，同树同值继续可解。

## 4. 树键规则（v1.0.1 起未变，含真实取证）

- 树键 = 沿 `SessionHeader.parentSession` 上溯到的最上层会话 id。
- **`id` 与 `parentSession` 形态不一致**（前者无前缀、后者带 `session-` 前缀），因此
  `ctx.sessions.get(parentId)` 常常查不到 → 此时**退回 `parentSession` 的值本身**，
  **绝不能退回子会话自己的 id**（v1.0.0 的缺陷：会让每个子代理各自成树，钉号失效）。
- 真实子会话头（2026-10-12 取证）：
  ```json
  {"id":"58822bbf-3aa3-463a-8933-91506896885d",
   "parentSession":"session-4e7011c7-2246-4e22-8f91-3ca4f93e6295",
   "origin":"subagent","delegationDepth":1}
  ```

## 5. 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 关掉即完全不介入 |
| `markerKey` | `pool-tree` | **仅用于识别/升级旧可见字面量**；零宽载荷本身与它无关 |
| `maxHops` | `8` | `parentSession` 上溯的最大层数（防环） |

### 5.1 开箱即用的模型路由（v2.1.4）

插件自带的 `cordis.patch.yml` 会**覆盖** base 的 `llm-pi-ai` 行，直接提供三条号池路由，
装完插件只需一个凭据即可用：

| 路由 | 协议 | 模型 | 凭据 |
|---|---|---|---|
| `litellm-completions` | openai-completions | DeepSeek V4.1 Flash、MiMo-V2.6-Flash / Pro | `LITELLM_API_KEY` |
| `litellm-reaspons` | openai-responses | Muse Spark 1.3 Contributor | `LITELLM_API_KEY` |
| `opencode-go` | 目录内置（直连兜底） | DeepSeek V4.1 Flash | `OPENCODE_GO_API_KEY` |

元数据（contextWindow / maxTokens / reasoningEfforts / compat）取自 opencode 官方目录
（DSH 内置的 `opencode-go.json` 是它的镜像）。两个刻意的取舍：

1. **`muse-spark-1.3-contributor` 的档位依赖网关能力** —— v2.1.5 起声明
   `off/minimal/low/medium/high/xhigh`（不含 `max`）。前提是号池侧已给 deployment 登记
   `model_info.supports_reasoning: true`；旧网关漏登记时会 400 `UnsupportedParamsError`，
   那种环境请退回 v2.1.4 的行为（删掉 `reasoningEfforts` 段）。
2. **不再使用 `LITELLM_COMPLETIONS_API_KEY` / `LITELLM_REASPONS_API_KEY`** —— 这两个名字
   在多数机器上不存在。分协议独立密钥的部署可在 profile 里覆盖 `apiKeyEnv`。

> ⚠️ 前置：profile 的 `cordis.patch.yml` **不得再定义 `- id: llm-pi-ai`**，否则 profile
> 层会整段盖掉本插件（profile 优先级更高）。二者只保留一处。

## 6. 自检

```bash
node selftest.mjs     # 纯函数断言 81 项：编码往返 / 界面不可见 / 幂等 / 内部偶发 MAGIC / 校验和 / 边界 / 树键上溯
node simulate.mjs     # 集成断言 30 项：模拟 agent/pre-step 回调链（真实血缘、runtime-context 跳过、出站字节可解）
```

关键「界面不可见」断言（`selftest.mjs` / `simulate.mjs` 都有）：

- 注入后去掉零宽字符的文本与原文本**逐字相同**（`stripZeroWidth(注入后) === 原文`）；
- 注入文本里不含 `pool-tree` 字样、不含任何 ASCII 可打印字符；
- 载荷 `trim()` 后不变、末字符不可被 trim 掉；
- 树键仍能从**原始字节**还原（`decodeTreeKey` / `extractTreeKey`）。

## 7. 消费端契约（号池 sidecar）

`pool/session-sidecar.py` 的 `tree_key(body_bytes)` 必须同时认三种形态：

1. **原始 UTF-8 零宽字节**（DSH/JS 默认：`JSON.stringify` 不转义零宽字符）→ `E2 80 8B …`
2. **JSON `\uXXXX` 转义形态**（上游序列化器 `ensure_ascii=True` 时）→ ASCII 的 `\u200b`
3. **旧的可见字面量** `[pool-tree: <id>]`（向后兼容；老会话与回滚期间照旧生效）

离线单测（不依赖网络/号池运行时文件）：

```bash
python pool/tools/test_session_tree_key.py     # 43 项：三种形态同值 + 内部偶发 MAGIC + 校验和 + fail-open
```

## 8. 生效与验证

**上线顺序（重要）**：先部署 sidecar（新解码向后兼容，旧插件写的字面量照旧能读），
**再重启 DSH** 让新插件生效。别反过来——反了会有一段时间「标记不可见但谁也读不到」，钉号静默失效。

1. sidecar（`/opt/litellm/session-sidecar.py`）：见内部变更文档（位置见仓库根 `PRIVATE-DATA.md`），重启 `litellm-session-sidecar`。
2. DSH：**重启 DSH**（插件是 host 侧模块，热改不生效；见「生效条件」）。

重启后验证：

- GUI 里新消息末尾**看不到** `[pool-tree: …]`；
- `~/.dsh/pool-tree-shadow.jsonl` / sidecar 日志 `tree-affinity tree=… key_want=… injected=true` 里
  `tree=` 仍是**队长会话 id**（不是子代理自己的 id）；
- `~/.dsh/pool-affinity-decisions.jsonl` 的 `tree=` 同理。

## 9. 生效条件

- 首次安装后需要**重启 DSH** 才会加载新包。
- **替换已安装包（改代码）同样需要重启**——DSH 会继续跑旧模块代。
- 本仓库通过 junction 挂载：`C:\Users\<user>\.dsh\profiles\desktop\node_modules\@local\dsh-pool-affinity`
  → `gitee-plugins\opencodego-pool\dsh-plugin\`，改真源即改生效内容。

## 10. 回滚

1. **插件回滚**：把 `index.js` / `selftest.mjs` / `simulate.mjs` 回退到 v1.0.1（`git revert` 或
   `git checkout <v1.0.1 提交> -- dsh-plugin/`），然后**重启 DSH**。回滚后标记重新变成可见字面量，
   sidecar 的旧分支照旧能读（第 7 节第 3 条）→ 钉号不停。
2. **sidecar 回滚**：只回退 `tree_key()` 到 `_TREE_RE` 单分支，重启 `litellm-session-sidecar`。
   此时**新插件写的零宽载荷读不到** → 钉号回落为「客户端会话头 / body session_id / 首条消息哈希」，
   不会报错但会丢树钉号 → 所以 sidecar 回滚必须与插件回滚一起做。
3. 回滚验证：`python pool/tools/test_session_tree_key.py`（旧字面量那组必须 PASS）。

## 11. 已知限制 / 风险

- 载荷占 74 个不可见字符（约几十个 token/请求），比 v1 的可见字面量略长；
  编码对 uuid 形态做了紧凑化（128 bit → 64 符号），非 uuid 形态会更长一点。
- 零宽字符在当前 DSH 文本链路上**不会被 trim/normalize**：`dsh-llm`、`dsh-llm-deepseek`、
  `dsh-agent-loop` 全文没有对消息文本的 `trim()`/`normalize()`（唯一一处 `text.trim() !== ''` 在
  `dsh-llm/lib/index.js` L1094，只作用于**中断的助手流块**，与用户消息无关）；
  且字母表特意排除了 JS `trim()` 会吃的 U+FEFF。
- 会话记录里仍然**存在**载荷（只是不可见）；若用户全选复制粘贴，会带上不可见字符——这是
  「模型可见 + 界面不可见」这一约束下的固有代价（隐藏通道不可得，见第 2 节）。
- 升级前的历史消息仍留着旧的可见字面量（无法追溯修改落库事件）；它们与载荷同值，不冲突。

## 12. v2.1：provider 路由并入本插件（2026-10-20）

### 12.1 为什么要合并

原先号池 provider 路由写在 `~/.dsh/profiles/desktop/cordis.patch.yml` 里，与插件是两处配置；
换机/重装要同时搬两个地方，容易漏。v2.1.1 起把它们收进插件的 `cordis.patch.yml`：

```yaml
- insert:
    - id: dsh-pool-affinity        # 树键注入（Host 插件）
      name: '@local/dsh-pool-affinity'
      config: { enabled: true, markerKey: pool-tree, maxHops: 8 }

# 号池 provider 路由：**覆盖** base 的 llm-pi-ai 行（id-targeted，config 整体替换）
- id: llm-pi-ai
  name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    providers: { litellm-reaspons: …, litellm-completions: …, opencode-go: … }
```

> ⚠️ **不要用「插入第二个 `dsh-llm-pi-ai` 实例」的方式合并**。v2.1.0 曾这样做，
> 运行时该行 `fiberPhase=failed`（同包多实例在适配器 / 模型发现注册阶段冲突），
> 结果是号池路由整批缺失、模型选择器里看不到池模型。单实例覆盖没有这个问题。
>
> 因此 profile 里原有的 `- id: llm-pi-ai` 段必须删掉（本仓库已通过迁移脚本完成）。

### 12.2 T0：看门狗与重试（已内置默认值）

| 字段 | 值 | 说明 |
|---|---|---|
| `streamIdleTimeoutMs` | `2147483647` | = `MAX_TIMER_DELAY_MS`，等价关闭 pi-ai 看门狗（schema 下限 >0，不能写 0） |
| `retryPolicy.maxRetries` | `2` | 默认 `retryableCodes` 含 `TIMEOUT` → 超时会重试 |
| `retryPolicy.backoff` | `800 / 15000 / 0.2` | 初始延迟 / 上限 / 抖动 |

背景与结论见内部文档《上游静默与首字节治理》（位置见仓库根 `PRIVATE-DATA.md`）。

### 12.3 覆盖 baseURL / 模型目录

profile patch 的优先级高于 bundle，可在 `~/.dsh/profiles/desktop/cordis.patch.yml` 里覆盖
**同一行 id**（`llm-pi-ai`）：

```yaml
- id: llm-pi-ai
  name: '@deepseek-ai/dsh-llm-pi-ai'   # truthy name 只做断言，不改名
  config:
    providers: { … }                    # ⚠️ config 是整体替换、不是深合并：必须重述全部路由
```

只想换网关地址时，最省事的是 fork 本插件改 `cordis.patch.yml` 里的 `baseURL`
（两条 `litellm-*` 路由），再重新安装。

### 12.4 迁移（从「profile 里手写 provider」迁到本插件）

1. 备份 `~/.dsh/profiles/desktop/cordis.patch.yml`；
2. 删除其中的 `- id: llm-pi-ai` 整段（保持其余行不动）；
3. 确认本插件已在 `dsh.profile.bundles` 中（安装过即有）；
4. **重启 DSH**；
5. 验证：设置里能看到 `litellm-reaspons` / `litellm-completions` / `opencode-go` 三条路由，
   发一条 `provider: litellm-completions` 的请求正常返回。

### 12.5 回滚

```bash
# 迁移前的快照：<文件>.bak-merge110-<ts>
cp dsh-plugin/cordis.patch.yml.bak-merge110-<ts>              dsh-plugin/cordis.patch.yml
cp ~/.dsh/profiles/desktop/cordis.patch.yml.bak-merge110-<ts>  ~/.dsh/profiles/desktop/cordis.patch.yml
```

然后重启 DSH。**回滚时插件与 profile 必须同时回滚**，否则两边都定义同一路由
→ 重复注册、插件加载失败（日志：`registering a route another adapter already owns`）。

---

## 13. DeepSeek 思考协议兼容与 reasoningEfforts 对齐（v2.1.2）

### 13.1 现象

长会话续写（尤其接了 `cpt-mcp` 这类多工具、长历史会话）时，上游返回：

```
400 invalid_request_error
The reasoning_content in the thinking mode must be passed back to the API.
Received Model Group=deepseek-v4.1-flash@a9s4
```

### 13.2 根因

`litellm-completions` 是**自定义 provider**：pi-ai 的 `detectCompat` 用
`provider === "deepseek" || baseURL.includes("deepseek.com")` 判断是否 DeepSeek；
本路由 id 是 `litellm-completions`、baseURL 是 `yesifu.site/...` → 判定为**非 DeepSeek**。
若 model `compat` 也没有显式声明，则：

1. `requiresReasoningContentOnAssistantMessages` 为 `false`：pi-ai 回放历史时，
   **不会**给没有 reasoning 的 assistant 消息补 `reasoning_content`（空串也不补）；
2. `thinkingFormat` 为 `openai`：请求按通用 `reasoning_effort` 发送，而非 DeepSeek
   `thinking:{type:enabled}`。

DeepSeek 在上游对 thinking 续写校验 `reasoning_content`；一旦历史里出现
“只有 tool_call、没有 reasoning”的 assistant 消息（本机 sandbox17 会话实测 28 条），
下一次续写即 400，且该消息留在历史里使后续每轮继续 400。

### 13.3 配置契约（与 pi-ai 官方 catalog 对齐）

| provider / model | reasoningEfforts（未列出=不支持，DSH 钉 null） | compat 关键项 |
|---|---|---|
| `litellm-completions` / `deepseek-v4.1-flash` | `off, low, high, max` | `thinkingFormat: openai`、`requiresReasoningContentOnAssistantMessages: true`、`maxTokensField: max_tokens`、`supportsStore: false`、`supportsDeveloperRole: false`、`supportsStrictMode: true` |
| `litellm-completions` / `mimo-v2.6-flash`、`mimo-v2.6-pro` | `off, minimal, low, medium, high` | `supportsStore: false`、`supportsDeveloperRole: false`、`supportsStrictMode: true`、`maxTokensField: max_tokens` |
| `litellm-reaspons` / `muse-spark-1.3-contributor` | `off, minimal, low, medium, high, xhigh` | （responses；沿用目录默认） |
| `opencode-go` / `deepseek-v4.1-flash` | 目录自带，不重复声明 | 目录自带 |

> `off: null` 表示“支持关闭思考、且关闭时发送空参数”；其余档位若上游不支持则**不要声明**
> （DSH 会把未声明档位显式钉成 unsupported）。
>
> **v2.1.3 修正**：不要在号池 litellm 路由上使用 `thinkingFormat: deepseek`——网关是
> OpenAI 兼容 provider，会以 `UnsupportedParamsError: ... ['thinking']` 拒绝；`deepseek` 格式
> 只适用于直连 DeepSeek 原生 API。`requiresReasoningContentOnAssistantMessages` 才负责补历史
> `reasoning_content`，与 `thinkingFormat` 相互独立：前者修 “must be passed back”，后者只决定
> thinking 参数怎么发。

### 13.4 本机部署与校验

1. 插件已通过 junction 挂载到 profile：改本目录即改生效内容；
2. **profile 若也定义了 `- id: llm-pi-ai`**（本机当前就是），profile 层优先，必须同步为同一份
   provider 配置（否则插件更新会被 profile 盖掉）；
3. 离线校验：
   ```bash
   python dsh-config/validate_pi_ai_providers.py ~/.dsh/profiles/desktop/cordis.patch.yml
   python dsh-config/validate_pi_ai_providers.py dsh-plugin/cordis.patch.yml
   ```
4. **重启 DSH** 生效；历史会话直接续写即可（回放时 compat 会补空 `reasoning_content`）。

### 13.5 回滚

```bash
# 插件回滚
git revert <2.1.3 commit>
# profile 回滚（本机同步时生成的快照）
cp ~/.dsh/profiles/desktop/cordis.patch.yml.bak-deepseek-thinking-<ts> \
   ~/.dsh/profiles/desktop/cordis.patch.yml
```
然后重启 DSH。回滚后旧的 DeepSeek 长会话仍可能 400（历史里已含无 reasoning 的消息），
需要新开会话或等 sidecar/上游策略变化。
