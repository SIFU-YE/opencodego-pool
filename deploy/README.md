# OpencodeLitellmPool · 独立部署套件（deploy/）

在**全新 Linux 主机**上从零部署号池：一条命令起步，幂等可重复执行；
不依赖 Halo、不依赖外部数据库、不配置管理员账号体系，默认只监听 `127.0.0.1`。

> 本文档会被 `bootstrap.sh` 复制到 `${POOL_HOME}/deploy-README.md`，服务器上也能直接看。

---

## 1. 组件与端口

| 组件 | 形态 | 监听 | 作用 |
|---|---|---|---|
| **LiteLLM** | docker 容器（容器名固定 `litellm`） | `127.0.0.1:4000` | 数据面网关：模型 × 账号 deployment 矩阵、会话亲和、上游转发 |
| **session-sidecar** | systemd 服务（宿主 python） | `127.0.0.1:4001` | 客户端入口：实例键→主键改写、会话树钉号、allowlist 门禁、tool_call 修复 |
| **dashboard** | systemd 服务（宿主 python） | `127.0.0.1:4010` | 号池驾驶舱 `/`（管理控制台是其内一个页签，改账号密钥/模型 allowlist） |
| **pool_reconciler** | systemd 服务（宿主 python，常驻轮询） | 无端口 | 把 `.env` / `pool-config.json` 的变更经 LiteLLM 管理 API **热应用**，不重启容器 |
| **share-engine** | 可选 systemd timer（默认不启用） | 无端口 | 计算 `routing-share.json` 份额（观测态，fail-open） |
| **nginx** | 可选（自备） | 自定 | 反代示例见 `nginx-pool-location.conf` |

请求链路：

```
客户端 → :4001 session-sidecar → :4000 LiteLLM(容器) → opencode.ai
                                    ↑
             pool_reconciler ───────┘  （管理 API 热应用，不改容器 env、不重启）
看板 :4010 / →「管理控制台」页签 → 写 pool-config.json / .env → 触发 reconciler 应用
```

**没有 Postgres、没有 Halo、没有 auth_request、没有管理员令牌。**

---

## 2. 前置条件

- **Linux**（x86_64 / arm64；Debian/Ubuntu/RHEL 系均可），建议 ≥ 2 vCPU / 2 GB 内存 / 5 GB 磁盘；
- **Docker Engine**（24+）与 **Docker Compose v2**（命令是 `docker compose`，不是 `docker-compose`）；
- **python3 ≥ 3.10**（只用标准库，无需 pip 安装任何包）；
- **root / sudo**（写 `/opt`、装 systemd 服务、操作 docker）；
- **至少 1 把 OpenCode Go 上游 API key**（每个账号一把；建议 ≥ 2 把）；
- 主机能出网到 `opencode.ai:443`（需要代理时在 `.env` 配 `HTTPS_PROXY`，见 §4.2）；
- 可选：域名 + 证书 + 反向代理 / VPN（远程访问用，本项目不内置鉴权）。

---

## 3. 三步部署

### 第 1 步：一条命令起步

```bash
cd <repo>
sudo ./deploy/bootstrap.sh        # 若仓库未带可执行位，用 sudo bash deploy/bootstrap.sh
```

脚本会依次：

1. **预检**：Linux / root / docker compose v2 / python3≥3.10；
2. 创建 `${POOL_HOME}`（默认 `/opt/litellm`）并平铺安装各组件；
3. 从仓库根 `.env.example` 生成 `${POOL_HOME}/.env`（权限 `600`）；
   若还没有 key，会**安静交互录入**（不回显、不写日志/历史）；`LITELLM_MASTER_KEY` 留空则自动生成；
   随后清理空/占位的 `OCG_KEY_n` 行（避免生成空密钥 deployment）；
4. 跑一次 `gen_config.py` 生成 `config.yaml`；
5. **首次**初始化控制面 `pool-config.json`（allowlist 取自刚生成的 config，账号取自 `.env`；
   已存在则不覆盖）；
6. `docker compose up -d` 启动 LiteLLM，并等待健康检查通过；
7. 安装并启动 systemd：`opencodelitelm-pool-sidecar` / `-dashboard` / `-reconciler`；
8. 冒烟验证（sidecar `/v1/models`、看板首页 `/`、管理面 `/api/admin/status`），打印下一步。

> 已是旧部署、或想先填好 key 再跑：可先
> `cp .env.example /opt/litellm/.env && sudoedit /opt/litellm/.env`，再执行 bootstrap（不会覆盖已有 `.env`）。

常用选项：

```bash
sudo ./deploy/bootstrap.sh --check            # 只预检，不做任何修改
sudo ./deploy/bootstrap.sh --non-interactive  # 缺 key 直接报错（CI/自动化）
sudo ./deploy/bootstrap.sh --pull             # 强制拉取最新 LiteLLM 镜像
sudo ./deploy/bootstrap.sh --allow-snapshot   # 上游不可达时用仓库快照应急启动（仅账号集合一致时可用）
sudo ./deploy/bootstrap.sh --no-systemd       # 不用 systemd，改 nohup 后台启动（调试用）
```

### 第 2 步：验证

> 下面命令按默认 `POOL_HOME=/opt/litellm` 书写；若改过路径，把 `/opt/litellm` 换成你的 `POOL_HOME`。

```bash
# 容器健康（LiteLLM 自己的 liveliness 端点，仅回环可访问）
curl -s http://127.0.0.1:4000/health/liveliness && echo

# 服务状态
systemctl status opencodelitelm-pool-sidecar opencodelitelm-pool-dashboard opencodelitelm-pool-reconciler --no-pager

# 客户端实例键（可吊销、可替换；不要提交到仓库）
sudo cat /opt/litellm/dsh-instance-keys.json

# 以客户端身份拉模型列表（只应看到 allowlist 里的正式模型名，不应出现 @aN 池内别名）
TOKEN=$(sudo python3 -c "import json;print(next(iter(json.load(open('/opt/litellm/dsh-instance-keys.json')))))")
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:4001/v1/models | python3 -m json.tool | head -30

# 冒烟请求（模型名从上面列表里挑一个）
curl -sS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"只回复 ok"}],"max_tokens":8}' \
  http://127.0.0.1:4001/v1/chat/completions
```

看板：

```bash
# 本机浏览器打开 http://127.0.0.1:4010/ （只读看板）
# 管理控制台 http://127.0.0.1:4010/#admin   （看板内页签，直达 hash）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4010/            # 看板首页，期望 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4010/api/admin/status  # 管理面，期望 200（403=Host 白名单拒绝）

# 热更新链路自检（不修改任何配置；0=热路径可用，非 0 会打印降级命令）
sudo python3 /opt/litellm/pool_reconciler.py --check
sudo python3 /opt/litellm/pool_reconciler.py --once --dry-run   # 预览期望态与现状的差异
```

### 第 3 步：接入客户端 + 日常管理

客户端（DSH / 任意 OpenAI 兼容客户端）：

```text
baseURL = http://127.0.0.1:4001/v1          # 本机直连
        = https://<pool-host>/pool/v1       # 经 nginx 反代（可选，见 nginx-pool-location.conf）
apiKey  = sk-pool-…                          # 客户端实例键，来自 /opt/litellm/dsh-instance-keys.json 或看板「管理控制台」页签
```

日常改配置**全部走看板 「管理控制台」页签，热生效、不需要重启容器**：

| 操作 | 入口 | 生效方式 |
|---|---|---|
| 上游账号/密钥增删、启用/停用 | 「管理控制台」页签 → 账号 | 写 `.env` + `pool-config.json` → **reconciler 热应用**（秒~分钟级），不重启容器 |
| 模型 allowlist（开放哪些模型） | 「管理控制台」页签 → 模型 | 同上；sidecar ≤2s 热读，reconciler 同步 deployment |
| 客户端实例键签发/吊销 | 「管理控制台」页签 → 实例键 | **即时生效**（sidecar 每请求重读文件） |
| 钉号模式（shadow/full） | 「管理控制台」页签 → 钉号 | **≤5s 生效**（sidecar 热读） |
| 审计 | 「管理控制台」页签 → 审计 / `${POOL_HOME}/admin-audit.jsonl` | — |

> 首次部署后 「管理控制台」页签 即可用（bootstrap 已用 `.env` + 生成的 config allowlist 初始化控制面）。
> 若页面提示「控制面未初始化」，说明初始化被跳过（例如缺少 `pool_control.py`）；
> 按页面提示先做一次「预览 → 确认」写入 allowlist/账号即可创建控制文件。

> `pool-config.json` 是控制面**唯一事实源**（账号槽位、`models.allow`、`affinity_mode`）；
> bootstrap 首次部署会用「刚生成的 config.yaml allowlist + .env 里的账号」初始化它，之后**以 「管理控制台」页签 为准**，
> 重跑 bootstrap 不会覆盖已有控制文件。文件不存在/损坏时组件一律 **fail-open**
> （不拦截、按生成时的 `config.yaml` 行为跑），不会把站点打挂。

---

## 4. 变量表

### 4.1 `deploy/.env`（部署参数，不含密钥）

首次运行 bootstrap 会从 `deploy/.env.example` 生成，权限 `600`。改完重跑 bootstrap 即可。

> 优先级：**显式环境变量 > `deploy/.env` > 脚本默认值**。例如
> `sudo POOL_HOME=/data/litellm ./deploy/bootstrap.sh` 会覆盖 `deploy/.env` 里的 `POOL_HOME`
> （改完记得把 `deploy/.env` 一起改成一致，方便下次直接重跑）。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `POOL_HOME` | `/opt/litellm` | 所有组件/配置/运行时文件的家目录。**取非默认值时** bootstrap 会建 `/opt/litellm -> $POOL_HOME` 兼容软链（见 §9 已知限制） |
| `LITELLM_IMAGE` | `ghcr.io/berriai/litellm:main-stable` | LiteLLM 镜像；固定版本就换具体 tag，升级用 `--pull` |
| `LITELLM_PORT` | `4000` | LiteLLM 宿主端口（→ 容器 4000），只绑 `127.0.0.1`。**保持 4000**：sidecar/dashboard/reconciler 都固定连 `127.0.0.1:4000`，改端口会让链路断开（除非同步改这些组件） |
| `DASHBOARD_PORT` | `4010` | 看板/「管理控制台」页签 端口，只绑 `127.0.0.1` |
| `RECONCILE_INTERVAL` | `60` | reconciler 轮询间隔（秒） |
| `SHARE_ENGINE_INTERVAL` | `600` | 可选份额引擎间隔（仅注释示例；启用 timer 后固定 10min） |

`session-sidecar` 端口固定 `4001`（脚本内硬编码，不提供变量）。

### 4.2 `${POOL_HOME}/.env`（运行期密钥 / 账号，权限 600，绝不入库）

由仓库根 `.env.example` 生成；**同一个文件也是 LiteLLM 容器的 `env_file`**。

| 变量 | 示例 | 说明 |
|---|---|---|
| `OCG_KEY_1` … `OCG_KEY_N` | `replace-me` | 上游账号 key，一把一把账号；编号 N 即**槽号**（参与会话/缓存身份，删除账号后不要复用编号）。bootstrap 会**删除空/占位的 `OCG_KEY_n` 行**，避免生成空密钥 deployment |
| `OCG_EMAIL_1` … `OCG_EMAIL_N` | `replace-me` | 与 key 对应的展示标识（邮箱/别名），仅用于日志与看板 |
| `LITELLM_MASTER_KEY` | 留空自动生成 | 号池内部主键，绝不下发客户端；容器内 LiteLLM 使用 |
| `POOL_SESSION_BUCKETS` | `8` | 会话桶数 K，改变会改变 deployment 矩阵：**改完重跑 bootstrap**（会重生成 config、重建/重启一次容器并重启 sidecar） |
| `POOL_ADMIN_HOSTS` | 空 | 管理面接口 `/api/admin/*` 的 Host 白名单（逗号分隔主机名）。**网络白名单，不是身份认证**；默认只接受 localhost/127.0.0.1/::1 |
| `HTTPS_PROXY` | 空 | 可选：容器出网代理（宿主机 sing-box 等）。示例 `http://docker-host:<proxy-port>`（compose 已把 `docker-host` 映射到宿主网关） |
| `NO_PROXY` | 空 | 可选：代理排除清单（如 `127.0.0.1,localhost`） |
| `POOL_DIRECT_OPENAI` / `POOL_DIRECT_ANTHROPIC` | 空 | 可选：非 muse 模型的直连反代 api_base。默认指向本机 `http://localhost:4002`；容器内访问宿主机反代时填宿主网关地址（同时会扩展 `upstream_policy.py` 的通道白名单） |
| `POOL_EGRESS_MODELS` | 空 | 可选：必须走日本出口（sing-box）的模型前缀，默认 `muse-spark` |
| `OCG_USAGE_URL` | 空 | 预留；默认用生成器内置的规范端点 |

bootstrap 会把以下**非密钥**变量同步进 `/etc/opencodelitelm-pool.env`（systemd 服务的 env，600）：
`POOL_HOME`、`POOL_DASHBOARD_PORT`、`POOL_RECONCILE_INTERVAL`、`POOL_SESSION_BUCKETS`、`POOL_ADMIN_HOSTS`（若设置）。
其它高级变量如需覆盖（如 `POOL_LITELLM_URL`、`POOL_CONFIG_PATH`、`POOL_CONFIG_TTL`、`POOL_RECONCILER`），
写进 `/etc/opencodelitelm-pool.env` 后 `systemctl restart opencodelitelm-pool-reconciler opencodelitelm-pool-dashboard`。

> 密钥纪律：仓库示例一律是 `replace-me` / `sk-pool-…` / `<pool-host>`；日志与终端只出现
> `OCG_KEY_n` 引用名与账号指纹，不出现密钥值。bootstrap 全程不 echo 任何 key。

---

## 5. 部署后的目录

```
${POOL_HOME}/                     # 默认 /opt/litellm，权限 750
├── .env                          # 密钥/主键（600，绝不入库）
├── config.yaml                   # gen_config.py 生成的 LiteLLM 配置（密钥只以 os.environ/OCG_KEY_n 引用）
├── pool-config.json              # 管理面唯一事实源（models.allow / accounts / affinity_mode），bootstrap 首次初始化
├── dsh-instance-keys.json        # 客户端实例键（600，可随时吊销）
├── pool-affinity.json            # 钉号模式兼容镜像（事实源是 pool-config.json）
├── routing-share.json            # 可选份额引擎输出
├── pool-requests.jsonl           # 逐请求观测（账号/token/缓存），看板与工具读取
├── admin-audit.jsonl             # 管理操作审计（脱敏）
├── .pool-reconciler-state.json   # reconciler 状态（漂移检测用）
├── session-sidecar.py …          # 各组件（bootstrap 平铺安装）
├── dashboard.py / pool_admin*.py / pool_control.py
├── deploy-README.md              # 本文档副本
├── logs/                         # nohup 模式日志（systemd 用 journald）
├── run/                          # nohup 模式 pid
└── tools/                        # 运维/验收脚本（可选）
```

systemd 服务与配置：

```text
/etc/systemd/system/opencodelitelm-pool-sidecar.service
/etc/systemd/system/opencodelitelm-pool-dashboard.service
/etc/systemd/system/opencodelitelm-pool-reconciler.service
/etc/systemd/system/opencodelitelm-pool-share-engine.{service,timer}   # 安装但不启用
/etc/opencodelitelm-pool.env                                          # 600，非密钥 env
```

---

## 6. 手工部署（不用 bootstrap 的等价步骤）

```bash
sudo install -d -m 750 /opt/litellm
sudo cp pool/session-sidecar.py pool/gen_config.py pool/upstream_policy.py pool/pool_logger.py \
        pool/toolrepair.py pool/share-engine.py pool/pool_control.py pool/pool_reconciler.py /opt/litellm/
sudo cp pool-dash/dashboard.py pool-dash/pool_admin.py pool-dash/pool_admin_ui.py /opt/litellm/
sudo install -m 600 .env.example /opt/litellm/.env && sudoedit /opt/litellm/.env   # 填 key / 主键
cd /opt/litellm && sudo POOL_HOME=/opt/litellm POOL_SKIP_RESTART=1 python3 gen_config.py
sudo install -m 600 /dev/null /opt/litellm/pool-requests.jsonl
cd <repo>/deploy && sudo docker compose up -d
# 服务：见 §7，装 unit + /etc/opencodelitelm-pool.env 后 systemctl enable --now
# 控制面：手工部署可跳过初始化（首次在「管理控制台」页签「预览→确认」写入时自动创建 pool-config.json）；
#         想和 bootstrap 一样预置，则直接跑 deploy/bootstrap.sh（幂等，会补齐缺的步骤）
```

---

## 7. 升级 / 回滚

### 升级

```bash
cd <repo> && git pull
sudo ./deploy/bootstrap.sh          # 幂等：更新组件、按需重建容器、重启服务
sudo ./deploy/bootstrap.sh --pull   # 连 LiteLLM 镜像一起升级（会换 tag 内容，建议低峰）
```

- 只改了 `deploy/.env`（端口/路径/间隔）：重跑 bootstrap 即可；
- 只改了上游账号/模型 allowlist：**不用跑 bootstrap**，直接在看板 「管理控制台」页签 操作（热生效）；
- 运行期部署类变更（镜像、`HTTPS_PROXY`、`POOL_SESSION_BUCKETS` 等容器 env）需要重建容器：
  bootstrap 检测到 `${POOL_HOME}/.env` 比容器新时会自动 `docker compose up -d --force-recreate`。
  **注意：`docker restart litellm` 不会重读 env-file**（容器 env 在 create 时固化），必须重建。

### 回滚

| 场景 | 操作 |
|---|---|
| 回滚代码 | `git checkout <tag/commit> && sudo ./deploy/bootstrap.sh` |
| 回滚 LiteLLM 配置 | `sudo cp /opt/litellm/config.yaml.bak-guard /opt/litellm/config.yaml` 后按需重建/热应用 |
| 回滚控制面 | 用备份的 `pool-config.json` 覆盖 → `sudo python3 /opt/litellm/pool_reconciler.py --once` |
| 回滚单个账号/模型 | 看板 「管理控制台」页签 重新应用（带预览与影响面，失败自动回滚） |
| 整体下线 | `cd deploy && sudo docker compose down`；`sudo systemctl disable --now opencodelitelm-pool-{sidecar,dashboard,reconciler}`；归档 `${POOL_HOME}` |

**备份清单**（建议每日）：`${POOL_HOME}/.env`、`config.yaml`、`pool-config.json`、
`dsh-instance-keys.json`、`admin-audit.jsonl`。注意 `.env` 含密钥，备份要加密。

### 从旧部署迁移（docker run + cron 的机器）

旧机上 `gen_config.py` 由 **cron `*/10`** 驱动、且检测到变化会 `docker restart litellm`；
新架构下这必须停掉，否则会和 reconciler 打架（双时间表 + 自动重启清空内存亲和）。

1. 备份 `${POOL_HOME}`（`.env`、`config.yaml`、`dsh-instance-keys.json` 等）；
2. 停旧服务：`sudo systemctl disable --now litellm-session-sidecar pool-dash pool-dashboard`（按实际 unit 名）；
3. **删掉 `crontab -e` 里的 `gen_config.py` 行**（`share-engine.py` / `stall_watch.py` 可按需保留，它们不改配置）；
4. 停掉旧的 `docker run` 容器：`sudo docker rm -f litellm`（容器内无持久数据）；
5. 在新/同机仓库执行 `sudo ./deploy/bootstrap.sh`（会接管容器、服务与初始配置）；
6. `python3 ${POOL_HOME}/pool_reconciler.py --check` 确认热路径可用；之后运行期变更全部走 「管理控制台」页签。

> 迁移后**不要**再跑 `gen_config.py` 的 cron：初始 config 由 bootstrap 跑一次，运行期由 reconciler 热应用。

---

## 8. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| **LiteLLM 容器起不来**（`Restarting`/`Exited`） | `config.yaml` 引用了未注入的 `OCG_KEY_n`（改 `.env` 后没重建容器）；端口占用；配置文件写坏 | `docker logs --tail=100 litellm`；改完 `.env` 用 `docker compose up -d --force-recreate`；`docker ps -a | grep litellm`；`ss -ltnp | grep :4000` |
| **sidecar 连不上 4000**（客户端 502/500） | 容器未起或没 healthy；`LITELLM_PORT` 改过但 sidecar 仍连 4000（sidecar 写死转 127.0.0.1:4000） | `curl 127.0.0.1:4000/health/liveliness`；保持 `LITELLM_PORT=4000`（sidecar 固定转发 4000）；`journalctl -u opencodelitelm-pool-sidecar -n 100` |
| **allowlist 为空导致 fail-open** | `pool-config.json` 不存在/损坏，或 `models.allow` 是空数组：sidecar 视为“控制面不可用”，**放行 `config.yaml` 里的全部模型**（安全默认是不拦，不是全禁） | 属预期降级，不会打挂站点；到 「管理控制台」页签 设置 allowlist 并应用。排查：`cat ${POOL_HOME}/pool-config.json`、`journalctl -u opencodelitelm-pool-sidecar | grep allowlist` |
| **管理面打不开 / 403（`/api/admin/*`）** | dashboard 未启动；Host 不在白名单（反代没透传 Host 或用了域名） | `systemctl status opencodelitelm-pool-dashboard`；在 `.env` 配 `POOL_ADMIN_HOSTS=<你的域名>` 后重跑 bootstrap；反代保持 `proxy_set_header Host $host;` |
| **看板内「管理控制台」页签空白 / 报错** | `pool_admin.py` / `pool_admin_ui.py` 缺失或 import 失败（看板自动降级只读）；dashboard 与 reconciler 版本不一致 | 重跑 bootstrap 安装组件；`journalctl -u opencodelitelm-pool-dashboard | grep -i admin`；`python3 /opt/litellm/pool_reconciler.py --check` |
| **客户端 401 / `no_db`** | 实例键不在 `dsh-instance-keys.json`；或误把上游 key 当实例键用（兼容处理有限） | 「管理控制台」页签 重新签发实例键；确认客户端 `apiKey=sk-pool-…`；sidecar 日志 `pool-key ok label=…` |
| **改完账号/模型不生效** | reconciler 没跑起来；热路径不可用（LiteLLM 管理 API 401/404） | `systemctl status opencodelitelm-pool-reconciler`；`python3 /opt/litellm/pool_reconciler.py --check`；看 「管理控制台」页签 Job 详情。降级：`gen_config.py && docker compose up -d --force-recreate` |
| **gen_config 报错/不写配置** | 上游不可达；所有 key 都 rate-limited；allowlist 过滤后为空 | `tail ${POOL_HOME}/guard.log`；`python3 ${POOL_HOME}/tools/list_accounts.py`；检查网络/代理；账号恢复后重跑 |
| **端口被占用** | 旧进程/旧容器残留 | `ss -ltnp | grep -E ':(4000|4001|4010)'`；停掉冲突进程或改 `deploy/.env` 端口（4001 固定，需改脚本） |

日志入口：

```bash
journalctl -u opencodelitelm-pool-sidecar -f
journalctl -u opencodelitelm-pool-dashboard -f
journalctl -u opencodelitelm-pool-reconciler -f
docker logs -f --tail=200 litellm
tail -f /opt/litellm/pool-requests.jsonl     # 逐请求观测（含账号指纹/token/缓存）
```

---

## 9. 安全模型与已知限制

**安全边界（用户明确要求：不做 Halo 改造、不配管理员权限）：**

- 本项目**不内置账号体系**：管理面的防护 = 默认只监听 `127.0.0.1` + Host 白名单
  （`POOL_ADMIN_HOSTS`，网络白名单而非身份认证）；
- 所有服务默认只绑回环：LiteLLM `4000`、sidecar `4001`、看板 `4010`；
- **远程访问请自行加 VPN / 反向代理鉴权**（basic_auth / oauth2-proxy / mTLS 均可）；
  管理控制台能改上游账号密钥与开放模型，等价于号池根权限，**不要裸暴露公网**；
- 客户端 API（`:4001/v1`）靠 `sk-pool-*` 实例键隔离，可逐个吊销；
- 文件权限：`.env` / `dsh-instance-keys.json` / `pool-config.json` / 审计 600，`POOL_HOME` 750；
- bootstrap 与各组件日志**不打印密钥值**，只打印 `OCG_KEY_n` 引用名与指纹。

**已知限制：**

1. `gen_config.py` 与 `dashboard.py` 仍硬编码 `/opt/litellm`（本轮未改）；因此
   `POOL_HOME` 取非默认值时，bootstrap 会创建兼容软链 `/opt/litellm -> $POOL_HOME`。
   原生支持 `POOL_HOME` 是后续项（`pool_control.py` / `pool_reconciler.py` / `pool_admin.py` /
   `session-sidecar.py` 已原生支持）。
   **部署后再改 `POOL_HOME` 不会被自动迁移**：需先停服务、移动 `${POOL_HOME}` 数据、再改
   `deploy/.env` 重跑 bootstrap（软链会按新路径重建）。
2. 单机单实例，未做多副本/HA；LiteLLM 内存中的会话亲和态在容器重建后会重建
   （思考模型的 `encrypted_content_affinity` 可凭加密块恢复）。
3. 不做多租户、按用户配额、审批流；实例键即信任边界。
4. 反代/证书/鉴权/备份/监控/告警需自备。
5. `pool-egress/`（sing-box 出口层）是环境相关组件，**不包含在本套件**；
   如需要，按 `pool-egress/README.md` 单独部署，并在 `.env` 配 `HTTPS_PROXY`。

**旧 unit 弃用说明：** `pool/systemd/` 下的旧 unit（`litellm-session-sidecar`、`pool-dash`、
`pool-dashboard`、`pool-reconciler`）保留仅作历史兼容，独立部署请用 `deploy/systemd/` 下的干净版；
`pool-egress` 相关 unit 属出口层，不随本套件部署。

---

## 10. 独立部署需要人工准备的最小清单

- [ ] 1 台 Linux 主机（root/sudo，能装 Docker）；
- [ ] Docker Engine + Compose v2 + python3 ≥ 3.10；
- [ ] **N 把 OpenCode Go 上游 API key**（N ≥ 1；多账号才有池化收益，建议 ≥ 2；每把对应一个账号）；
- [ ] 确定 `${POOL_HOME}`（默认 `/opt/litellm`）与三个端口是否可用；
- [ ] 主机到 `opencode.ai:443` 的出网（或一个代理地址，填进 `.env` 的 `HTTPS_PROXY`）；
- [ ] （可选）客户端接入用的域名/证书 + 反向代理 / VPN；
- [ ] （可选）备份位置（`.env`、`config.yaml`、`pool-config.json`、实例键）。

其余全部由 `sudo ./deploy/bootstrap.sh` 完成；装完后日常只碰看板的「管理控制台」页签。

---

## 11. 反代示例（可选）

见 `deploy/nginx-pool-location.conf`：`/pool/v1/` → `127.0.0.1:4001/v1/`，
`/pool/` → `127.0.0.1:4010/`，只做 `proxy_pass` + 安全头 + `Cache-Control: no-store`，
**无 Halo、无 `auth_request`**；文件头有“默认只监听 127.0.0.1；远程访问请自行加 VPN/反代鉴权”的说明。

> 反代路径是**示例、可自定义**：本套件用 `/pool/…`；历史部署常用 `/works/pool/…`（见
> `pool-dash/nginx-pool-location.conf`）。客户端 baseURL 与反代 location 保持一致即可。

```nginx
# 在已有 server{} 里 include 即可
include /opt/litellm/deploy/nginx-pool-location.conf;
```

---

## 12. 卸载

```bash
cd <repo>/deploy && sudo docker compose down
sudo systemctl disable --now opencodelitelm-pool-sidecar opencodelitelm-pool-dashboard \
  opencodelitelm-pool-reconciler opencodelitelm-pool-share-engine.timer
sudo rm -f /etc/systemd/system/opencodelitelm-pool-*.service /etc/systemd/system/opencodelitelm-pool-*.timer
sudo rm -f /etc/opencodelitelm-pool.env
sudo systemctl daemon-reload
# 归档（含密钥，注意加密）后再删：
sudo tar -C / -czf ~/litellm-backup-$(date +%F).tar.gz opt/litellm
# sudo rm -rf /opt/litellm            # ⚠️ 确认备份完成后再执行
```
