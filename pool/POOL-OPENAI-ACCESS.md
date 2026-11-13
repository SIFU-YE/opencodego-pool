# 号池 openai-completions 接入指南（任何 DSH/客户端通用）

架构：`客户端 → nginx /works/pool/v1/ → session-sidecar:4001（实例键→主键映射 + 会话粘性）→ LiteLLM:4000 → 上游号池`
DSH 侧**零代码**：只配「baseURL + API key + 协议」。

## 1. 签发/吊销实例键（号池侧，唯一需要管的地方）

- 键文件：`/opt/litellm/dsh-instance-keys.json`（600，主键**永不**下发）
- 新增：`python3 -c "import secrets;print('sk-pool-'+secrets.token_hex(16))"` → 把 `"sk-pool-…": "<用途标签>"` 写进 JSON → **即时热加载，无需重启**
- 吊销：删掉对应行，同样即时生效
- 现有：`dsh-external-demo`（外部/测试）、`dsh-yesifu-main`（本机 DSH）
- 查看键值：`cat /opt/litellm/dsh-instance-keys.json`（本机操作，勿贴进对话）

## 2. 任何 DSH 的接入（模型配置页 3 步）

1. 路由名/provider：**避开 pi-ai 内置目录名**（如 `openai`/`opencode-go`），推荐 `yesifu-pool`——内置目录同名会导致「获取模型」返回目录而非池子；
2. baseURL：`https://<pool-host>/works/pool/v1`，协议：**openai-completions**；
3. API key：粘贴该实例键 → **获取模型**（应列出 30 个，含 mimo-v2.6-flash/pro）→ 勾选保存。

等价 cordis.patch.yml 片段：
```yaml
- id: llm-pi-ai
  config:
    providers:
      yesifu-pool:
        models:
          - id: mimo-v2.6-flash
            name: MiMo V2.6 Flash
            contextWindow: 1000000
            maxTokens: 128000
            input: [text, image]
            reasoningEfforts: { low: low, high: high }
        apiKeyEnv: POOL_API_KEY        # Models 页存键，或 export 到启动环境
        baseURL: https://<pool-host>/works/pool/v1
        api: openai-completions
```

## 3. 一致性自检（2026-10-01 实测 6/6 绿）

```bash
B=https://<pool-host>/works/pool/v1; K=sk-pool-…
curl -s -H "Authorization: Bearer $K" $B/models                    # object=list,30 个
curl -s -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d '{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"hi"}],"max_tokens":16}' \
  $B/chat/completions                                               # chat.completion
# stream:true → SSE `data:` 帧 + `data: [DONE]`；reasoning_effort=high → reasoning_content ✓
```

## 4. 错误速查

| 状态 | 含义 |
|---|---|
| 401 `No api key passed in` | 没带 Authorization |
| 400 `No connected db` | 键不对（虚拟键/拼错/被吊销的旧 OCG 键） |
| 200 但模型列表无 mimo | provider 撞了 pi-ai 内置目录名（见第 2 步） |

## 5. ⚠️ 保存路由的覆盖陷阱（2026-10-01 实案）

DSH 的 Models 页**保存路由时，会把「API key 输入框」里的值写进凭据库**（覆盖 `OPENCODE_GO_API_KEY`）。
框里若残留 OCG 键 → 覆盖后**所有聊天/发现请求立刻 `400 No connected db`**。

**规矩：保存路由前把 API key 框清空**（平时留空即可——留空 = 走凭据库实例键，获取模型与聊天都自动用它）。
只有刻意要更换该路由的键时才填值，且必须填 `sk-pool-*` 实例键。
**急救**：会话报 `400 no_db` → 大概率是键被覆盖 → 重置：
`credentials.set OPENCODE_GO_API_KEY = dsh-yesifu-main 实例键`（或按本文第 1 节任一实例键）。

### §5.1 OCG 键已被 sidecar 兼容（2026-10-03 起）
sidecar 现将 `/opt/litellm/.env` 中的 `OCG_KEY_1..6` 一并视为合法实例键（映射主键上送）。
**即使保存路由时误把 OCG 键写进凭据库，聊天与获取模型也照常 200**——`400 no_db` 这类故障已根治。
「保存前清空 API key 框」仍是好习惯（避免多键混杂、日志 label 清晰），但不再是硬性要求。
