# 私密资料与凭据（不随仓库分发）

本仓库（OpencodeLitellmPool / `opencodego-pool`）只包含**代码、模板与公开文档**。
内部文档与真实凭据全部放在仓库之外，通过环境变量或外部路径引用；仓库内只有占位符模板。

## 1. 内部文档

- 位置：环境变量 `OPENCODEGO_POOL_PRIVATE_DATA` 指定的目录；未设置时按仓库根解析相对路径
  `../../private-data/opencodego-pool`（即与 `gitee-plugins/` 同级的 `private-data/opencodego-pool`）。
- 内容：`docs/` —— 内部设计方案、部署方案、变更记录、服务器巡检、运维约定等
  （2026-11-12 起从仓库迁出，保留在原私密资料根的 `docs/` 下）。
- 仓库 `.gitignore` 已忽略 `docs/`，防止再次误入库；`git archive HEAD` 导出的快照不含该目录。
- 引用方式：`${OPENCODEGO_POOL_PRIVATE_DATA:-../../private-data/opencodego-pool}/docs/`。
  测试与脚本不要依赖该目录存在，缺省时应能降级跳过。

## 2. 凭据（运行时，只在部署机 / 本地环境）

**服务端**：`${POOL_HOME}/.env`（默认 `POOL_HOME=/opt/litellm`，文件权限 600，由
`deploy/bootstrap.sh` 从仓库根 `.env.example` 生成）：

| 变量名 | 用途 |
|---|---|
| `OCG_KEY_1..N` | 上游 OpenCode Go 账号 API key；编号即槽号，参与会话身份，删除后不要复用旧编号 |
| `OCG_EMAIL_1..N` | 账号展示标识（邮箱/别名），只用于日志与看板展示，不参与鉴权 |
| `LITELLM_MASTER_KEY` | 号池内部主键，LiteLLM 管理 API 与 reconciler 使用；不下发客户端 |
| `HTTPS_PROXY` / `NO_PROXY` | 可选：容器出网代理 |
| `POOL_DIRECT_OPENAI` / `POOL_DIRECT_ANTHROPIC` | 可选：非 muse 模型的直连反代 api_base（容器内访问宿主机反代时使用） |
| `POOL_EGRESS_MODELS` | 可选：必须走日本出口（sing-box）的模型前缀，默认 `muse-spark` |

**客户端**：只持有可吊销的实例键 `sk-pool-*`，来自 `dsh-instance-keys.json`
（`${POOL_HOME}/dsh-instance-keys.json`，运行时文件，已在 `.gitignore` 忽略）或看板「管理控制台」页签。

**出口层**：`/opt/pool-egress/` 下的 `clash-secret`、`config.json` 由 `pool-egress/gen_config.py`
在部署机本地生成，不入库；`POOL_DOCKER_BRIDGE`（可选）用于指定 sing-box 在 Docker 网桥上的监听地址。

**原则**：

- 仓库里只有占位符模板：`.env.example`、`deploy/.env.example`；
- 任何真实 key / 口令 / 连接串都不写入仓库文件、不写进提交说明、不贴进文档；
- 代码只读环境变量（`os.environ` / `process.env`），不读取仓库内明文凭据文件；
- 日志、看板、审计只输出引用名（`OCG_KEY_N`）、指纹或尾号，不输出完整值。

## 3. 运行期数据（已忽略，不入库）

`pool-requests.jsonl` 等请求日志、`pool-config.json`、`pool-affinity*.json`、
`dsh-instance-keys.json`、`*.key` / `*.pem`、看板截图等（见 `.gitignore`）。
这些都是部署机本地状态，重建/迁移请从部署机直接拷贝，不走 Git。

## 4. 发布前检查

对 `git archive` 导出的快照（而不是工作树）做机械扫描，退出码必须为 0：

```bash
git archive --format=tar --output <snapshot.tar> HEAD
mkdir -p <snapshot-dir> && tar -xf <snapshot.tar> -C <snapshot-dir>
node scripts/scan-secrets.mjs --root <snapshot-dir> --include-docs
```

同时检查 tracked 清单，确认没有：`docs/`、`.env`（`.env.example` 除外）、
`*.key` / `*.pem` / `*.credentials`、`dsh-instance-keys.json`、`db-connections.json`、
`*.cpt`、`*.xlsx`、`*.sqlite` / `*.db`、会话日志与截图。

扫描脚本零依赖，规则见 `scripts/scan-secrets.mjs`；本地提交前还有全局 Git 隐私门禁兜底，
但不要依赖它替代上面这次快照扫描。
