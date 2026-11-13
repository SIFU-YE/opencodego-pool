# pool-egress：号池出口（sing-box + 节点看门狗）

号池到上游 `opencode.ai` 的唯一出口。litellm 容器用 `HTTPS_PROXY=http://<宿主网关>:10808`
（地址由部署机 `.env` 配置，不写进仓库）把所有上游流量交给这里的 sing-box；sing-box 通过一个
selector（`muse-egress`）在 10 个日本 AWS vless 节点之间选一个，看门狗（`watchdog.py`）负责把
selector 切到健康节点。

服务器落地位置：`/opt/pool-egress/`（`config.json`、`watchdog.py`、`gen_config.py`、
`quarantine.json`、`watchdog.log`、`clash-secret`），服务：`pool-egress.service`、
`pool-egress-watchdog.service`。本目录是这两个脚本与补丁工具的仓库副本。

## 2026-10-16 事故：Connection error → 500

**症状**：DSH 一轮运行直接失败

```
litellm.InternalServerError: InternalServerError: OpenAIException - Connection error..
Received Model Group=deepseek-v4.1-flash@a5s6  (Fallbacks=None, LiteLLM Retried: 2 times)
```

**定位过程**

| 观察 | 证据 |
|---|---|
| 失败不是单账号/单会话 | 同一分钟内 a5s4 / a5s6 / a4s3 / a6s4 等多个别名同时 500 |
| 失败在 HTTP 客户端建连阶段 | litellm traceback 终止于 `httpcore/_async/http_proxy.py start_tls` → `httpcore.ConnectError` |
| 出口才是现场 | sing-box 日志：`using outbound/vless[jp-aws01]: dial tcp [2406:da14:...]:443: connect: network is unreachable` |
| 报的那个 IPv6 **是节点自己的** | jp-aws01 节点域名的 AAAA 正是日志里的 `2406:da14:…`；每个节点报的地址都不同 |
| 本机没有 IPv6 出口 | `ip -6 route` 无默认路由（只有 eth0 上一个 ULA 地址） |
| 量级 | 当天 743 次 ENETUNREACH（另有 35 次 IPv4 i/o timeout），9 个 AWS 节点全中招，按小时分布 |
| 看门狗为什么没躲开 | 5s 稳态探测只做 **TCP connect**（本机→节点 59ms 一直通过），120s 全链路 sweep 虽然探到 `jp-aws01=fail`，但结果只喂给 quarantine，**不会换掉 current** |

**根因**：出口拨号策略缺省。节点域名同时有 A/AAAA，sing-box 把解析结果直接交给 Go 拨号器；
一旦解析结果里只剩 AAAA（A 查询抖动时解析器会返回部分结果），拨号立刻 ENETUNREACH，
而本机根本没有 IPv6 路由 —— 这类失败 100% 会失败，且 TCP 探测看不见。

**修复**

1. `domain_strategy: ipv4_only`（`gen_config.py` + `patch_ipv4_only.py`）：只解析 A、只拨 IPv4。
   十个节点域名均已确认存在 A 记录（AWS 东京 EC2）。
   差分验证：拿只有 AAAA 的 `ipv6.test-ipv6.com` 当出站域名 —— 打补丁前是
   `dial tcp [2a01:...]:443: connect: network is unreachable`（与线上报错逐字同形），
   打补丁后变成 `lookup ipv6.test-ipv6.com: empty result`：**IPv6 地址再也不会被拨**。
2. `watchdog.py`：稳态每 30s 对 current 做一次全链路探测（`delay_probe` 走 clash API），
   失败即走既有的换节点流程并把 current 罚 5 分钟（`CHAIN_PENALTY`）。TCP 通过但链路已死
   的节点不再能长期占位。

**注意**：`domain_strategy` 在 sing-box 1.12 已标记 deprecated（1.14 移除）。
升级到 1.14 前必须迁到新的 DNS 形式（`dns.rules[].action=route` + `strategy=ipv4_only`
或出站 `domain_resolver` 指向 ipv4-only 解析器），否则本修复会随字段一起失效。

**回滚**：`/opt/pool-egress/backup-20261016/` 与 `config.json.bak-ipv4only-*`
（脚本同样保留 `.bak-*`）；回滚 config 后 `systemctl restart pool-egress`。

## 常用操作

```bash
python3 /opt/pool-egress/patch_ipv4_only.py --check   # 出口配置是否已钉 IPv4
sing-box check -c /opt/pool-egress/config.json        # 配置语法/字段校验
systemctl restart pool-egress                         # 换出口节点配置（会短暂断流~1s）
systemctl restart pool-egress-watchdog                # 重载看门狗
tail -f /opt/pool-egress/watchdog.log                 # 换节点/体检记录
journalctl -u pool-egress --since "10 min ago" | grep ERROR   # 出口报错
```

判读：`switch ... confirmed` 是换节点；新增的 `full-scan trigger-chain-fail=-1ms cur=...`
表示「TCP 通、链路死」，是 2026-10-16 修复引入的判据。

## 2026-10-28 出口拆分：只有 muse 走日本出口

**起因**：10-28 上午 10 个日本节点 IP 全部间歇不可达（sing-box `dial tcp ... i/o timeout`），
所有模型共用这条出口 → LiteLLM 全量 `Connection error` → 500。实测 `opencode.ai` 直连可用，
只有 `muse-spark-1.3-contributor` 直连返回 `403 RegionError: not available in your country`
（地区限制），必须走日本出口。

**现架构**：

```
非 muse 模型：  LiteLLM deployment api_base → http://<宿主网关>:4002/inference/go/...
                （地址由 POOL_DIRECT_OPENAI / POOL_DIRECT_ANTHROPIC 配置）
                → nginx direct-relay（本机，直连 opencode.ai，不经 sing-box）
muse 模型：     LiteLLM deployment api_base → https://opencode.ai/inference/go/openai/v1
                → sing-box muse-egress（日本节点）→ opencode.ai
```

- nginx 配置：`pool-egress/direct-relay.nginx.conf`，服务器落地
  `/www/server/nginx/conf/vhost/direct-relay.conf`，默认只监听 `127.0.0.1:4002`；
  容器内访问时按文件注释把 `<docker-bridge-gateway>:4002` 一并放开（不要监听 0.0.0.0）；
- 路由开关在 `pool/gen_config.py` 与 `pool/pool_reconciler.py`（两处同步）：
  `EGRESS_MODELS=("muse-spark",)`，环境变量 `POOL_EGRESS_MODELS` / `POOL_DIRECT_OPENAI` /
  `POOL_DIRECT_ANTHROPIC` 可覆盖；改完跑 `gen_config.py` + `pool_reconciler.py --once`；
- sing-box 入站：`gen_config.py` 的 Docker 网桥监听地址由 `POOL_DOCKER_BRIDGE` 提供
  （未设置时只监听 `127.0.0.1:10808`，容器内走出口需显式配置）；
- `pool/upstream_policy.py` 白名单默认放行本机反代，并从 `POOL_DIRECT_*` 推导部署方配置的
  反代主机前缀（仍是 `/inference/go/` Go 通道），仓库不写死内网地址。

**注意**：

1. 日本出口故障只影响 muse；muse 仍会等 35s 才 500（出口不通时没有快速失败）。
2. 非 muse 流量出口 IP 是本机 IP；若上游风控出现异常，把 `POOL_EGRESS_MODELS` 调大或回滚三份脚本
   （备份 `*.bak-pre-egress-split-*`）即可全部回到日本出口。
3. watchdog 的「全节点隔离时只 keep-current」缺陷仍在；muse 依赖它，建议后续加固。
