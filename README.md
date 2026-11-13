# OpencodeLitellmPool

> **项目更名**：原名「opencodego 号池 / OpenCode Go Pool」，自 2026-10-17 起统一为 **OpencodeLitellmPool**。
> 仓库目录名与 DSH 插件路径暂保留 `opencodego-pool`（改名会打断已安装的插件链接），仓库/文档层面统一用新名。

把多个 [OpenCode](https://opencode.ai) **Go 订阅**账号聚合成一个 **OpenAI 兼容网关**：
一个 `baseURL` + 一把实例键接入，客户端零改造，同时做到**会话级亲和**、**账号级配额门禁**、
**改配置不重启**与**逐请求可观测**。

面向 [DSH（DeepSeek Harness）](https://github.com/deepseek-ai/deepseek-harness)、OpenViking 等支持
OpenAI 协议的编码代理。纯自托管、可独立部署，不依赖任何托管中转服务，也不依赖特定账号体系。

> **English TL;DR** — A self-hosted OpenAI-compatible gateway that pools multiple *OpenCode Go
> subscription* accounts into one endpoint, with per-conversation-tree account affinity
> (to preserve upstream prompt cache), per-conversation `x-opencode-session`, account health
> gating, **hot config updates without restarts**, a built-in admin page, and per-request
> observability. Docs are in Chinese; code and scripts are commented in Chinese.

---

## 为什么需要它

- **OpenCode Go 是订阅制**：月度额度按模型折算成美元上限（5 小时 / 每周 / 每月三窗口），单账号并发与速率有限；
- **多账号直接轮询会毁掉前缀缓存**：上游按「会话」做路由与缓存优化，请求在不同账号间跳来跳去等于每次冷启动；
- **客户端不该感知这些**：客户端只该看到 5 个正式模型名 + 一个 baseURL。

因此本项目的两个核心设计：

| 设计 | 作用 |
|---|---|
| **会话树钉号**（tree affinity） | 一个会话树（队长 + 全部子代理）的全部请求，确定性地落在**同一个账号**上 → 前缀缓存不丢 |
| **会话桶**（session bucket） | 每棵会话树在上游独占一个 `x-opencode-session` → 上游的「按会话路由/缓存」才真正生效 |

两者都是**无状态、可重放**的：`sha256(树键)` 决定账号与桶，重启、回放同一棵树结果一致。

## 特性

- **OpenAI 兼容**：`/v1/chat/completions`、`/v1/responses`、`/v1/models`；按模型分列协议（chat / responses / anthropic）；
- **实例键隔离**：客户端只拿到可单独吊销的 `sk-pool-*` 实例键，上游主键永不出池；
- **账号健康门禁**：定时拉上游 `usage` 三窗口，`rate-limited` 的账号自动剔除、恢复后自动回归；
- **份额引擎（shadow）**：按剩余额度与重置倒计时算各账号目标份额，可只算不注入（观察态）；
- **通道策略（机器强制）**：**只允许 Go 订阅通道**，禁用 Zen / Console（付费）通道——生成器写盘前校验，
  命中即拒绝写盘并退出（`exit 4`）；
- **出口层可自愈**：`pool-egress/` 用 sing-box 多节点 selector + 节点看门狗做健康探测与自动切换，
  并修复过「IPv6 拨号导致 `Connection error → 500`」；
- **逐请求可观测**：每个请求的实际账号、token 用量、缓存命中、会话树落点，落 JSONL + 看板；
- **流式保活（不被"上游静默"误杀）**：上游会成簇地长静默（15 分钟窗口 p50 可从 7s 跳到
  76–170s），静默期 sidecar 会给客户端补 **SSE 注释心跳** `: hb`（注释帧被 SSE 解析器忽略、
  不产生内容），避免客户端 `streamIdleTimeoutMs`（90s）误判成流失败；流式中途上游失败时
  **补零长度终止块**，快速失败而不是挂到客户端 idle 超时；日志里的 `status=200 hb=N`
  就是"这次被保活救回"的证据（内部变更记录《变更-20261017-客户端idle超时修复》，见 `PRIVATE-DATA.md`）；
- **改配置不重启（热更新）**：模型 allowlist、账号增删/密钥轮换、实例键、钉号模式都在页面上改，
  由**热读控制面 + reconciler** 生效 —— 不需要 `docker restart`，也不需要重建容器；
- **看板即驾驶舱**：按 FineReport / Smartbi 驾驶舱看板通用规范排布——「总览 → 分区 → 明细」
  三段式页签、KPI 数字卡置顶、异常预警区紧随其后；深色为默认、可一键切浅色；
  关键尺寸全部 `clamp()` 自适应，**1920×1080 与 2560×1440 下都满屏不溢出**（2560 档自动把成对分区并排）；
- **内置管理面（看板内「管理控制台」页签）**：模型 allowlist 自助编辑、账号与密钥轮换、实例键签发/吊销、钉号模式切换；
  **不内置账号体系**（默认只监听 `127.0.0.1`，远程访问请自备反代/VPN）；
- **可独立部署**：`deploy/` 提供 compose + bootstrap + systemd + 通用 nginx 示例，路径统一走 `POOL_HOME`，
  不依赖 Halo、不依赖特定域名；
- **工具箱修复**：上游偶发吐畸形 `tool_call`（空 id/name）与瞬时 400 误判，网关侧有界修复/重发；
- **测试齐备**：sidecar 单测、会话桶回归、流终止回归、通道策略、fail-closed、生成器离线冒烟（见「测试」）。

## 架构

```
客户端（DSH / OpenViking / 任意 OpenAI 兼容客户端）
   │  Authorization: Bearer sk-pool-*        ← 实例键（可逐个吊销，主键不出号池）
   ▼
nginx  /pool/v1/  ──►  127.0.0.1:4001  session-sidecar.py
   │                              │  ① 会话锚三源归一（含 DSH 插件注入的会话树键）
   │                              │  ② 实例键 → 主键改写
   │                              │  ③ 树键 → 确定性选号 + 会话桶 → 改写 model 为 <model>@a<N>s<K>
   │                              ▼
   │                        127.0.0.1:4000  LiteLLM（docker）
   │                              │  模型 × 健康账号 × (1 兜底 + K 会话桶) 个 deployment
   │                              ▼
   │                        pool-egress：sing-box 多节点 selector + 节点看门狗
   │                              ▼
   │                        opencode.ai/inference/go/openai/v1   ← 仅 Go 订阅通道
   ▼
旁路（定时/回调，均不在请求关键路径上）
   share-engine.py   cron */10  → routing-share.json      份额引擎（当前 mode=shadow，只算不注入）
   gen_config.py     bootstrap 跑一次 → config.yaml        初始 deployment 矩阵（运行期变更走 reconciler）
   pool_logger.py    litellm 回调 → pool-requests.jsonl    每条请求实际用了哪个账号（观测面）

控制面（改配置不重启）
   ${POOL_HOME}/pool-config.json   唯一事实源：模型 allowlist / 账号槽位 / 钉号模式（密钥值仍在 .env）
        │
        ├─► session-sidecar.py      TTL 2s 热读：模型白名单拦截 + /v1/models 过滤 + 禁用槽位跳过
        ├─► pool_reconciler.py      用 LiteLLM 管理 API 动态增删改 deployment（账号增删 / 密钥轮换）
        └─► 看板「管理控制台」页签    自助编辑，预览 → 确认串 → 应用 → 轮询结果
```

## 热更新：改配置不重启

| 你改的东西 | 生效方式 | 生效延迟 |
|---|---|---|
| 模型 allowlist | sidecar 热读控制面，请求路径上拦截 + `/v1/models` 过滤 | **≤2s** |
| 钉号模式 `shadow`/`full` | sidecar 5s TTL 热读 `pool-affinity.json` | **≤5s** |
| 客户端实例键（签发/吊销） | sidecar 每请求重读 `dsh-instance-keys.json` | **即时** |
| 账号新增/删除/启用禁用、密钥轮换 | reconciler 通过 LiteLLM 管理 API 增删改 deployment | **≤60s** |

> 只有**结构性变更**（协议路由、部署拓扑）才需要重跑 `gen_config.py` + 重启 LiteLLM —— 这类不在管理页范围内。
> 目标 LiteLLM 若不支持管理 API，reconciler 会**明确报错并给出降级命令**，绝不静默假装成功。

## 管理控制台（看板页签）

```bash
python3 /opt/pool-dash/dashboard.py --listen 127.0.0.1:4010   # 然后访问 http://127.0.0.1:4010/ 切到「管理控制台」页签（直达 /#admin）
```

> 2026-10-17 起管理控制台**不再是独立页面**：它和总览、账号、用量、性能、路由、DSH 配置
> 并列成一个页签，共用同一套主题与自适应布局；`GET /admin` 已下线（404），
> 写接口仍是 `/api/admin/*`，Host 白名单校验一分不减。

- **不内置鉴权**：默认只监听本机；远程请自备反向代理 + 鉴权 / VPN。仅保留服务端硬校验
  （Host 白名单、JSON、请求体上限、输入白名单、输出转义）；
- **密钥永不回显**：列表只显示指纹（前 4 + 后 4）；实例键仅在签发响应里出现一次；
- 所有写操作都是「预览 diff + 影响面 → 输入确认串 → 应用 → 轮询结果」，并记脱敏审计。

## 快速开始（独立部署）

前置：Linux + Docker + Python 3.11+；N 个 OpenCode **Go 订阅**账号（每账号一个 API key）。

```bash
# 一条命令起步（幂等、可重复执行；完整说明见 deploy/README.md）
sudo bash deploy/bootstrap.sh
#   建 ${POOL_HOME:-/opt/litellm} → 生成 .env → 跑一次 gen_config（初始 deployment 矩阵）
#   → docker compose up（LiteLLM）→ 起 sidecar / dashboard / reconciler

sudo $EDITOR ${POOL_HOME:-/opt/litellm}/.env    # 填 OCG_KEY_n / OCG_EMAIL_n / LITELLM_MASTER_KEY
```

部署完成后：

| 入口 | 地址 |
|---|---|
| 客户端 baseURL（反代后） | `https://<pool-host>/pool/v1` |
| sidecar 直连（本机） | `http://127.0.0.1:4001/v1` |
| 看板（含管理控制台页签） | `http://127.0.0.1:4010/`（直达管理面：`/#admin`） |

**自检**（不消耗上游额度）：

```bash
curl -s -H "Authorization: Bearer sk-pool-…" http://127.0.0.1:4001/v1/models   # 应只列 allowlist 内的正式模型名
python3 pool/tools/test_session_buckets.py        # 会话桶回归
python3 pool/tools/smoke_gen_config.py            # 生成器离线端到端冒烟
python3 pool/tools/test_upstream_policy.py        # 通道策略（仅 Go 订阅）
python3 pool/tools/test_pool_control.py           # 热更新控制面
```

**接入 DSH**：在 profile 的 provider 里填 `baseURL=https://<pool-host>/pool/v1` + 实例键，
并按 `dsh-config/` 下的示例声明 `streamIdleTimeoutMs` / `retryPolicy` / `reasoningEfforts`。

## 目录

| 路径 | 说明 |
|---|---|
| `pool/session-sidecar.py` | **请求关键路径**：会话锚归一 + 实例键改写 + 树键钉号 + 会话桶 + tool_call 修复；另含 2026-10-17 上游偶发 `[unsupported_request] Provide between 1 and 512 text messages` 的**有界重试**（只认这一条签名、只重发一次） |
| `pool/gen_config.py` | 生成 `config.yaml`（deployment 矩阵 + 通道策略校验 + 健康门禁） |
| `pool/upstream_policy.py` | **通道策略**：只允许 Go 订阅前缀，拒绝 Zen/Console；纯函数、可单测 |
| `pool/pool_control.py` | **热更新控制面**：唯一事实源（模型白名单/账号槽位/钉号模式）的原子读写、TTL 缓存、审计（脱敏）、`.env` 原子写 |
| `pool/pool_reconciler.py` | **热应用器**：把期望态与 LiteLLM 现状 diff，用管理 API 动态增删改 deployment（**改配置不重启**）；无热路径时明确报错并给降级命令 |
| `pool/tools/test_sidecar_transient_retry.py` | **上述有界重试的回归测试**：命中签名→重试一次 / 其它 400→不重试 / 正常 200→不重试 |
| `pool/config.yaml` | LiteLLM 配置（密钥走 `os.environ`，不含明文） |
| `pool/share-engine.py` | 份额引擎（当前 shadow） |
| `pool/pool_logger.py` | LiteLLM 回调：逐请求记录账号与 token 用量 |
| `pool/toolrepair.py` | 流式 `tool_call` 修复（空 id/name） |
| `pool/patches/` | 历史补丁与一次性验证脚本（保留作证据与复现） |
| `pool/tools/` | 运维与验收工具：守卫、看板数据、单测、回归 |
| `pool/systemd/` | 服务单元（sidecar / 看板 / 出口） |
| `pool-egress/` | **出口层**（sing-box 多节点 selector + 节点看门狗）：配置生成器、看门狗、IPv4 修复补丁与回归测试；含 2026-10-16「Connection error → 500」事故复盘 |
| `pool-dash/` | 号池看板与构建脚本；`pool_admin.py` / `pool_admin_ui.py` 提供 **看板内「管理控制台」页签与 `/api/admin/*`**（无内置鉴权、默认只听本机）；`gen_config.py` 是**脱节副本**，已 fail-closed |
| `deploy/` | **独立部署套件**：`bootstrap.sh`（一条命令起步）、`docker-compose.yml`、systemd unit、通用 nginx 反代示例（无 Halo 依赖）、部署与故障排查文档 |
| `dsh-plugin/` | **DSH 侧插件**：把会话树键注入出站消息（`[pool-tree: …]`） |
| `dsh-config/` | DSH provider 离线校验器 + 客户端补丁 |
| `PRIVATE-DATA.md` | 私密资料与凭据说明：内部文档位置、`.env` 变量清单、发布前扫描方式（内部文档不随仓库分发） |

## 测试

```bash
python3 pool/tools/test_sidecar_units.py              # sidecar 单测（真实模块，含「messages 字节不动」硬约束）
python3 pool/tools/test_session_buckets.py            # 会话桶：确定性/分散/降级链
python3 pool/tools/test_sidecar_stream_termination.py # 流式三件套：终结块 / 静默心跳 / 中途断开不挂死
python3 pool/tools/test_upstream_policy.py            # 通道策略：放行 Go、拒绝 Zen/Console
python3 pool/tools/test_pool_dash_failclosed.py       # 看板副本必须默认拒绝执行
python3 pool/tools/smoke_gen_config.py                # 生成器离线端到端（stub 网络 + 真 YAML 解析）
```

> `test_sidecar_stream_termination.py` 覆盖三代真实故障：① 终结块套娃（客户端等不到流结束）；
> ② **上游长静默 → 客户端 `pi-ai stream idle timeout after 90000ms`**（现在有 SSE 注释心跳保活）；
> ③ 流式中途上游 RST（现在补终止块、立即返回，不再二次 `send_response`）。
> 拿旧版 sidecar 跑 ②③ 会失败，见内部变更记录《变更-20261017-客户端idle超时修复》（见 `PRIVATE-DATA.md`）。

看板侧（在 `pool-dash/` 目录下跑）：

```bash
python3 test_pool_admin.py                # 管理面：Host/体积/确认串/密钥不回显/转义/降级只读
python3 test_dashboard_page.py            # 驾驶舱页面装配：7 页签、管理面片段注入、/admin 已下线
python3 dashboard.py --selftest           # P3 观测面（6 面板 + 8 告警）自测
python3 build_mock.py && python3 mock_dashboard.py &   # 罐头数据 mock（:4011），用于视觉验证
python3 style-audit.py --shot /tmp/shots http://127.0.0.1:4011/   # 7 视口 × 全部页签的几何/裁切/溢出/主题自检
```

> `style-audit.py` 覆盖 **2560×1440 与 1920×1080**（本次驾驶舱改版的验收基准），
> 并逐个点过全部页签 —— 隐藏页签里的图表量不到宽度，只测首屏会漏掉整类问题。
> 需要 Playwright（`pip install playwright` + `playwright install chromium`）；
> 没装浏览器时会自动退回系统 Chrome/Edge。

## 运维纪律

- **重启纪律**：任何 `docker restart litellm` 都会清空亲和/钉号内存态 → 安排在低峰并事前告知；
  见内部运维文档《RUNBOOK-restart-discipline》（见 `PRIVATE-DATA.md`）。
- **回滚层级**（自轻到重）：① `pool-affinity.json` 的 `mode` 改回 `shadow`（秒级，无需重启）→
  ② 重启 sidecar → ③ 还原 `config.yaml` 并重启 litellm → ④ 还原生成器三件套。
- **观测面**：`pool-requests.jsonl`（逐请求账号/用量/缓存）、`pool-affinity-decisions.jsonl`（选号决策）、
  看板、以及 `tools/stall_watch.py` 的延迟监控。

## 密钥与安全

- `.env`、`dsh-instance-keys.json`、账号邮箱映射、运行时日志/状态**绝不入库**（`.gitignore` 已覆盖）；
- 代码与日志只打印**引用名**（`OCG_KEY_N` / 实例键标签 / 账号指纹），不打印值；
- 客户端只持有可吊销的实例键；主键只在号池内部使用；
- 内部文档（`docs/`）与私密资料**不随仓库分发**：已迁出并加入 `.gitignore`，位置与引用方式见 `PRIVATE-DATA.md`；
- 文档中的 `<pool-host>` 是**脱敏占位符**，请替换为你自己的域名。

## 开源说明

- **许可证**：**Apache-2.0（含专利授权）** —— 见 `LICENSE`；版权与第三方声明见 `NOTICE`；
- **不含**任何真实凭据、实例键、账号身份或运行时数据；`git archive HEAD` 快照经敏感信息扫描 0 命中
  （命令见 `PRIVATE-DATA.md`）；
- 内部设计/变更/巡检文档**不随仓库分发**（`docs/` 已迁出并忽略）；
- 文档与脚本中的 **`<pool-host>` 是脱敏占位符**，使用前替换为你自己的域名；
- 多账号聚合请自行确认符合上游 ToS；本项目**不提供账号、不代管服务**，仅供自用规模的自托管。

## 文档索引

内部设计 / 变更 / 巡检 / 运维文档**不随本仓库分发**（`docs/` 已迁出并加入 `.gitignore`）：
统一存放在私密资料目录（环境变量 `OPENCODEGO_POOL_PRIVATE_DATA`，默认与 `gitee-plugins/`
同级的 `private-data/opencodego-pool` 下的 `docs/`），引用方式与发布前检查见
[PRIVATE-DATA.md](PRIVATE-DATA.md)。

仅存在于私密资料目录的文档包括：《会话钉号方案》《标签路由不可用与替代方案》《运维约定》
《开源准备清单》、`design/` 部署方案与反雪崩负载均衡设计、以及全部变更记录/巡检报告。

面向外部读者和贡献者的公开说明都收敛在本 README、`deploy/README.md`、
`pool-egress/README.md`、`dsh-plugin/README.md` 与各脚本头部注释中。
