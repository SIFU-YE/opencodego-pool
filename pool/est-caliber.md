# est 口径定义（T2 三段费率）— sidecar D3b 护栏与 T8 回放对齐的唯一口径

> 来源：T6 整改 1（HIGH，strategy/ACCEPTANCE-report.md §3.3/§8-1）。本文件是 T8 回放对齐的
> **显式、可复现输入**；/opt/litellm/session-sidecar.py 的 `est_cost_usd()` / `obs_cost_usd()`
> 与本文件逐式对应，任何改动必须同步修订本文件并注明版本。

## 0. 为什么统一

- 旧 sidecar est：`chars/4 × $0.1372/M`（无缓存信用，≈T2 实测的 10–13 倍）→ D3b 判定
  `hour_burn + est ≤ admit_max` 偏严，临期号（KEY_4）大量 `no-eligible-expiring`，
  影子期 key_want 占比 3.4% vs 目标 11.1%（Δ7.7pp）。
- A9 回放（strategy/replay_sim.py，`rate_budget_ok`/`attach_costs`）用 **T2 实测建模 $
  （含缓存命中）** 作 guard_cost，宽松 ~13×。
- 统一到回放主口径（A9-replay-report.md §7：护栏 `unit=t2`、`scope=every_request`、`mult=1.5`）。

## 1. 三段费率（常量，来源 replay_sim.py / share-engine.py 同源校准）

| 段 | 记号 | 费率 ($/M tok) | 来源 |
|---|---|---|---|
| uncached 输入 | `PRICE_IN` | **0.14** | T4 replay_sim 校准值（=share-engine.py 同值） |
| cached read 折价 | `PRICE_CACHED` | **0.0028** | 同上 |
| 输出 | `PRICE_OUT` | **0.28** | 同上 |

（§2.3 的 $0.1372/M、$0.4314/M 为 **G2 缓存损失 L** 的损失口径价，仅用于 G2 收益/损失比较，
不进入 est；est 与 replay guard_cost 一律用上表三段费率。模型维度不做 pro 加价——与
replay_sim guard_cost 完全一致，保证 T8 逐请求可对齐。）

## 2. est 公式（sidecar `est_cost_usd()`）

```
est_pt  = max(len(json.dumps(messages, ensure_ascii=False)) // 4, 1)   # prompt tok 估算
hit     = 命中率（取值链见 §3）
out_r   = 输出比 Σct/Σpt（取值链见 §3）
est_cost = ( est_pt × ((1 − hit) × PRICE_IN + hit × PRICE_CACHED)
           + est_pt × out_r × PRICE_OUT ) / 1e6
```

## 3. hit / out_r 取值链（决策 reason 以 `[caliber:...]` 标注）

| 优先级 | hit 来源 | caliber 标注 | 说明 |
|---|---|---|---|
| 1 | 会话实测：`cum_cached / cum_cached_pt`（pool-requests.jsonl 该 sess 实测 cached/pt，≥1 条带 cached 观测） | `hit:session-obs` | 最可信；out_r 同会话 `cum_ct/cum_pt`（`out:session-obs`） |
| 2 | 同类会话 P50：近 24h pool-requests 中 `sess_class` 相同的会话级 cached/pt 的中位数 | `hit:pool-p50-<cls>` | 整改5 落地后 pool 行带 sess_class 才可用 |
| 3 | 全局 P50：近 24h 全部会话级 cached/pt 的中位数 | `hit:pool-p50` | out_r 取近 24h 全局 `Σct/Σpt`（`out:pool`） |
| 4 | 保守 0（无任何观测） | `hit:conservative-0,out:conservative-0` | 不给缓存信用，est 偏保守（≈旧口径） |

统计窗口：近 24h pool-requests.jsonl 尾部（≤4000 行），进程内缓存 300s（`pool_hit_stats()`）。

## 4. 观测烧量（sidecar `obs_cost_usd()`，D3b 滚动 1h 窗的 hour_burn 用此口径）

```
obs_cost = ( (pt − c) × PRICE_IN + c × PRICE_CACHED + ct × PRICE_OUT ) / 1e6
c        = 实测 cached（记录带 cached 字段时原值，含 0）
         = hit × pt（记录缺 cached 字段时按命中率建模——与 replay_sim attach_costs 同构，
           h_default 链同 §3；不再按全额无缓存计）
```

## 5. 护栏（语义不变，金额同口径可比）

| 护栏 | 判定式 | 来源 |
|---|---|---|
| D3b 速率护栏（every_request，含钉号续请求） | `hour_burn(滚动 1h, obs_cost 口径) + est_cost ≤ admit_max_usd = pace/24 × 1.5`；超限该临期号本轮不参与 L 倾斜抽签（钉号会话触发 G_rate 重钉） | FINAL §2.2.3、A9 §7 |
| 单请求预算护栏 | `est_cost ≤ min(admit_max_usd, req_budget_usd)`（正预算取最小） | FINAL §2.1.3 / §2.2.4 步骤 6 |

`admit_max_usd`、`req_budget_usd`、`pace_usd_day` 全部来自 routing-share.json（share-engine 落盘），
sidecar 零硬编码。est、hour_burn、护栏额度三者同为「实际 $」口径，金额直接可比。

## 6. reason 标注（审计/T8 对齐）

每条 shadow-decisions.jsonl 记录：
- `est_cost`：上式 est（round 10 位）；
- `est_caliber`：如 `hit:session-obs,out:session-obs`；
- `reason` 尾部恒带 ` [caliber:hit:…,out:…]`。

## 7. 版本

- v1（2026-09-29）：T7 整改 1 初版；与 session-sidecar.py 同步部署（.bak-r1-*）。
