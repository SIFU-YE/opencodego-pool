#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
litellm-session-sidecar —— 127.0.0.1:4001 -> 127.0.0.1:4000
精简版（2026-09-29 简化提效；旧版 1547 行归档于 session-sidecar.py.bak-slim-*）。

只保留两件事（其余全部移除）：
  1. baseline session_id 注入与三源归一（2026-10-07 sidstable）：chat/completions 请求
       metadata.pool_src = "sidecar"
       metadata.session_id = 唯一 final 值，同时写回外发 X-Session-Id 头 —— 无论下游
       取哪一路（实测 LiteLLM pre_call_utils 以 X-Session-Id 头为准、覆盖 body
       metadata），值恒定 → 钉号稳定、上游前缀缓存恒命中（缓存复用第一目标）。
       优先级：客户端自带会话头（非 nginx 随机兜底）> 客户端 body metadata.session_id
               > 稳定哈希 sha256(首条 user 消息)（外网补 ext- 前缀保留来源标记，
                 本地直接调用纯 16hex）。
       nginx 的 ext-<32hex> 每请求随机号（map $pool_ext_sid）只当来源标记，
       不再当会话号 —— 否则外网每请求都是新会话，粘性与缓存全失效。
  2. 纯透传：HTTP 流式转发到 4000，无任何路由决策、无护栏、无兜底；仅对上游偶发误判 400
     与容量准入 429（ttft_admission_refused）做**有界重发**（见文末 2026-10-17 / 2026-10-29 两节）。

移除（动机见 /root/wordsandbox/strategy/ 简化方案）：
  · L/M/H 分档、首跳选号、share 倾斜、est 计价、D3b/req_budget 护栏
  · G0–G7 重钉 + epoch 改写（每次重钉 = 前缀缓存全冷，与缓存优先目标冲突）
  · share-engine 消费（routing-share.json）、shadow-decisions/state 写盘、状态重建 tailer
  · tags 注入（enable_tag_filtering 已同步从 gen_config 移除）
messages 字节绝不改动，仅动 metadata。解析失败 → 原样透传（fail-open 仅此一处）。

2026-10-16 treepin-s6（会话级上游 session）：
  上游要求每个会话带稳定的 x-opencode-session，且据此做路由与前缀缓存优化；
  旧实现给同一账号下的所有会话发同一个常量值，上游只能把它们视为同一个会话，
  该优化的前提不成立。现在按树键把请求分发到 <model>@a<N>s<K> 别名
  （K = 1..POOL_SESSION_BUCKETS），该别名的 deployment 自带唯一的
  x-opencode-session，于是「同树同 session、不同树不同 session」。
  同树的桶号由 sha256 决定，无状态、可重放、重启不变。
  服务端尚未重跑 gen_config（桶别名不存在于 config.yaml）时，自动退回旧的
  <model>@a<N>，行为与改动前完全一致（安全降级）。

2026-10-17 hot-control（看板管理化，sidecar 侧门禁）：
  控制面模块 pool/pool_control.py（自带 2 s TTL 缓存）驱动的三处热生效门禁，
  改控制文件后 ≤TTL 内生效，**不需要重启 sidecar**：
    ① allowlist 热门禁：POST /v1/chat/completions 与 /v1/responses 的 model 不在
       allowlist（且非池内别名 <model>@a<N>[s<K>]）→ 直接 404 model_not_found，
       不透传到上游；
    ② GET /v1/models 按 allowlist 过滤（控制面不可用时回退本文件原有的硬编码集合），
       别名永不外泄；
    ③ 选号命中 cfg["accounts"] 里 enabled:false 的槽位 → 放弃别名改写/key_want，
       退回默认型号由 LiteLLM least-busy 处理，日志 `acct-disabled skip slot=N`。
  控制面任何异常（导入失败、抛错、返回 None、结构不符、解析失败）一律 fail-open：
  保持本文件改动前行为（放行全部模型、硬编码 /v1/models、不做账号过滤），
  绝不让配置故障变成启动失败 / 全站 404。

2026-10-17 idle-timeout（客户端 90s 误杀修复）：
  上游会成簇长静默（upstream-stall.log 里 15 分钟窗口 p50 从 ~7s 跳到 76–170s），
  静默期间 sidecar 一个字节都不发 → 客户端 streamIdleTimeoutMs(90s) 判死，而号池侧
  请求最终仍是 200（sidecar 在请求结束写响应时才拿到 EPIPE，日志即
  `[Errno 32] Broken pipe`）。三项改动：
    ① 静默期与 toolrepair 扣帧期补 SSE 注释心跳（`: hb`），客户端 idle 计时器不再误触发；
    ② 连续静默超过 POOL_SSE_MAX_SILENCE_S 才主动断开，真卡死时不让客户端无限等；
    ③ 流式已开始后上游失败时补零长度终止块，不再二次 send_response（那会把状态行写进正文）。
  开关：POOL_SSE_HEARTBEAT_S（默认 15s，0=关）、POOL_SSE_MAX_SILENCE_S（默认 300s，0=不限）。
2026-10-29 admission-429（上游容量准入 429）：
  新会话首发偶发 429 `[ttft_admission_refused] the cell is at capacity; retry shortly (after 16 s)`，
  上游建议等待后重试；DSH retryPolicy backoff 初始 0.8s、上限 15s，3 次尝试都在饱和窗口内 →
  用户侧「重试延迟 1.7s → 整轮失败」。现 sidecar 命中该签名时按建议等待（上限 20s、默认 5s，
  可用 POOL_ADMISSION_429_MAX_WAIT_S / POOL_ADMISSION_429_DEFAULT_WAIT_S 调整）后原体重发，
  最多 2 次（POOL_ADMISSION_429_MAX_RETRIES）；配额类 429 见下方 2026-11-13 换号逻辑。日志：
  `upstream-admission-429 retry-1/2 wait=16s ...` / `... retry-failed ...`。
2026-11-06 prefill_cap_refused / overloaded：上游容量准入的另外两种签名（实测 prefill after 5s
  与 after 30s；overloaded 无建议秒数），与 ttft_ 共用同一有界等待重试；等待上限默认 30s；
  三种签名都不换账号，仍由后续控制面 hard 淘汰决定是否重钉。
2026-11-13 配额 429 / 等头超时当次换号（生产 11:04–11:06 实锤）：
  账号 8 monthly 限额后，会话仍钉在 `<model>@a8s4` 单 deployment 别名上；账号 9（额度剩 2%）
  的会话 `@a9s2` 等头超时 3 次 → 客户端固定 502 `session-sidecar upstream error`。
  别名 model group 只有一个账号，LiteLLM 无法在同名组内换号，必须在 sidecar 层：
    · 配额 429（`usage limit reached` / `usage limit exceeded` / `Extra Usage` / `Go Plus` /
      `insufficient_quota`）→ `hard repin` 到另一个 healthy 账号的别名后重发，
      最多 POOL_QUOTA_FAILOVER_ATTEMPTS 次（默认 2）；
    · 等头重试耗尽 → 同样换一次别名重发（POOL_HEADER_FAILOVER_ATTEMPTS，默认 1），
      全失败才 502；
    · 换号同时更新树钉号，后续请求不再回退到限额号；仅在尚未向客户端写字节时触发。
  日志：`upstream-quota-429 failover-1/2 from=OCG_KEY_9 to=OCG_KEY_3` /
  `upstream-header-failover 1/1 ...`。
2026-10-31 软淘汰迁移（额度到 ε 线）与连续性保护：
  share-engine 在 used≥98%（ε=2%）时把 pace/share 归零；旧逻辑会让 sticky 立即失效，
  已钉住的会话下一条请求就被硬切到别的账号 → 上游 prefix cache 全冷，且 thinking 的
  encrypted reasoning 与签发 key 绑定（换号回放会 400，2026-10-22 实锤）。现按淘汰分级：
    hard（rate-limited / usage 不可用 / 控制面禁用 / 别名缺失）→ 立即重选（无法避免）；
    low （账号可用、别名仍在，只是额度到线）→ 活跃树暂缓（POOL_REPIN_IDLE_S，默认 600s），
        空闲树才迁移；活跃累计超 POOL_LOW_MAX_S（默认 1800s）强制迁移，避免真撞 429；
  pins 文件升级 v2（tree→{key,ts,defer}）记录树最后活跃时间；POOL_SOFT_REPIN=0 回退旧行为。
  决策日志/侧车日志新增 repin=soft-defer|soft-idle|soft-max|soft-off|hard 便于验收。
"""
import hashlib
import hmac
import os
import queue
import re
import toolrepair
import http.client
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 2026-10-17 独立部署：所有落盘路径统一由 POOL_HOME 派生（默认服务器上的 /opt/litellm）。
HOME = os.environ.get("POOL_HOME", "/opt/litellm")
UP_HOST, UP_PORT = "127.0.0.1", 4000
LISTEN = ("127.0.0.1", 4001)
CHUNK = 65536

# ---- 2026-10-17：客户端 idle 误杀修复（SSE 静默心跳 + 流式异常补终止块）----------
# 现象：DSH 偶发 `pi-ai stream idle timeout after 90000ms`，而号池侧同一请求最终是 200，
#       sidecar 日志在请求结束那一刻才报 `[Errno 32] Broken pipe`（客户端早走了）。
# 归因：上游（opencode.ai）会**成簇**地长静默 —— upstream-stall.log 里 15 分钟窗口
#       p50 会从 ~7s 跳到 76–170s（今日 18 次 stall + 7 次 suspected）。静默期间 sidecar
#       一个字节都不发，客户端满 streamIdleTimeoutMs(90s) 就判死；加上客户端
#       retryPolicy.maxRetries=0，一次抖动直接变成"整轮运行失败"。
# 对策：① 静默期给客户端补 **SSE 注释心跳** `: hb` —— 注释帧被所有 SSE 解析器忽略，
#          不污染数据，只把"零字节静默"变成"有字节但无内容"，客户端 idle 计时器不再误触发；
#       ② 扣帧（toolrepair hold）期间同样补心跳，因为那时上游有数据但 we 不下发；
#       ③ 保留静默上限，真卡死时主动断开而不是无限等待；
#       ④ 流式已开始后上游失败时**补零长度终止块**，绝不再 send_response（那会把状态行
#          写进正文），让客户端立刻收到"流结束"而不是干等 90s。
# 开关（改完重启 sidecar 生效；0 = 关闭该项）：
#   POOL_SSE_HEARTBEAT_S   心跳间隔，默认 15 秒（远小于客户端 90s 阈值）
#   POOL_SSE_MAX_SILENCE_S 允许的连续静默上限，默认 300 秒；超过则断开
SSE_HEARTBEAT_S = float(os.environ.get("POOL_SSE_HEARTBEAT_S", "15") or "15")
SSE_MAX_SILENCE_S = float(os.environ.get("POOL_SSE_MAX_SILENCE_S", "300") or "300")

# 2026-10-20（T1）：上游「响应头等待」预算。
# 失败样本里 sidecar 阻塞在 getresponse() 上，这段时间**一个字节都不发**，客户端只能
# 等自己的 idle 看门狗。这里只治理「拿到响应头之前」这一段（不动下游流式转发）：
#   · 等待超过 WARN   → 记 upstream-header-slow（含 waited），用于量化与告警；
#   · 等待超过 LIMIT  → 断开重发**一次**（此刻尚未向客户端写过任何字节，安全），
#                       仍超时 → 由外层异常分支返回 502，客户端按 SERVER 重试并换号；
#   · 0 = 关闭（保持旧行为：HTTPConnection timeout=3600，等于不设限）。
HEADER_WAIT_WARN_S = float(os.environ.get("POOL_HEADER_WAIT_WARN_S", "20") or "20")
HEADER_WAIT_TIMEOUT_S = float(os.environ.get("POOL_HEADER_TIMEOUT_S", "120") or "120")
# 2026-10-23 实测（18h 日志）：71 次「等到 120s 上限」，其中 48 次发生在首连、
# 25 次换连接后立刻 200（≈2s 就回来）、23 次两连都卡 → 客户端可见 502。
# 即：绝大多数不是「上游真的慢」，而是「首条连接卡住」；只重试 1 次会让 48% 的卡顿
# 变成 502。故重试次数可配（默认 3），且两次尝试之间小睡，避免立刻又落回同一条卡住的连接。
HEADER_WAIT_ATTEMPTS = max(1, int(os.environ.get("POOL_HEADER_ATTEMPTS", "3") or "3"))
HEADER_WAIT_RETRY_DELAY_S = max(0.0, float(os.environ.get("POOL_HEADER_RETRY_DELAY_S", "2") or "2"))
SSE_HB_FRAME = b": hb\n\n"          # SSE 注释帧：以 ':' 开头，客户端必须忽略
# 转发读取必须用 resp.read1(CHUNK)：read(amt) 会阻塞攒满 amt 字节才返回，
# SSE 小分片会被攒成 64KB 一批（思考/正文整块爆发到达、失去流式）；
# read1 每次最多取一个线上分片，才是真透传。（2026-10-03 readfix，bak-readfix-*）
HOP = {"content-length", "transfer-encoding", "connection", "keep-alive",
       "host", "upgrade", "proxy-authorization", "te", "trailer"}
# 2026-10-17：上游 Go 通道偶发把合法请求判成 unsupported_request（见 _relay 内的重试块）。
# 2026-10-27 扩签名：上游还会把**传输层故障错标成 HTTP 400 + type=server_error**，实锤签名：
#   {"error":{"param":"Connection prematurely closed BEFORE response","type":"server_error",
#             "message":"Streaming response failed: [400] Request failed"}}
# （2026-10-27 02:16 mimo-v2.6-pro@a9s2；同簇还有 a8/a10 的 Connection error + ttft_admission_refused，
#   upstream-stall 同窗口判定 stall；DSH 不重试 4xx → 一次抖动废掉整轮）
# 仍然只认签名白名单 —— 其它 400 一律照旧透传，避免把真正的请求错误吞成慢失败。
TRANSIENT_400_MARKS = (
    b"Provide between 1 and 512 text messages",          # 旧签名：unsupported_request 误判
    b"Connection prematurely closed BEFORE response",    # 新签名：流式传输故障错标 400
    b"Streaming response failed",                        # 新签名：同族报文形态
)
TRANSIENT_400_MARK = TRANSIENT_400_MARKS[0]   # 兼容旧引用
# 有界重试预算：命中签名最多重发 2 次（共 3 次尝试）—— 与 _upstream_getresponse 的等头重试同预算；
# 抖动是簇状的（02:16 同簇多账号同挂），一次重发可能仍落在簇内。
TRANSIENT_400_MAX_RETRIES = 2

# 2026-10-29：上游容量准入拒绝（429 ttft_admission_refused / prefill_cap_refused / overloaded）的有界等待重试。
# 现象（08:59 实锤）：新会话首发 429
#   [ttft_admission_refused] the cell is at capacity; retry shortly (after 16 s)
# 上游明确要求等待；DSH retryPolicy backoff 初始 800ms、上限 15s，3 次尝试都落在同一饱和窗口，
# 用户侧表现为「重试延迟 1.7s → 整轮失败」。配额类 429（Go usage limit exceeded）不含该签名，
# 不等待、不重试（与改动前一致）。
# 2026-11-06：新增上游第二/第三种容量签名：[prefill_cap_refused]（实测 after 5s / after 30s）
#   与 [overloaded]（the server is overloaded; retry shortly）。旧代码只认 ttft_ 前缀，直接透传。
# 三个签名共用同一有界等待预算；解析 `after N s` 后原体重发，仍不换号。
# 2026-11-06 16:5x 实测上游已出现 after 30 s，故把默认上限从 20s 提到 30s（可用环境变量覆盖）。
ADMISSION_429_MARKS = (b"ttft_admission_refused", b"prefill_cap_refused", b"[overloaded]")
ADMISSION_429_MARK = ADMISSION_429_MARKS[0]   # 兼容旧引用
ADMISSION_429_MAX_RETRIES = int(os.environ.get("POOL_ADMISSION_429_MAX_RETRIES") or 2)
ADMISSION_429_MAX_WAIT_S = float(os.environ.get("POOL_ADMISSION_429_MAX_WAIT_S") or 30.0)
ADMISSION_429_DEFAULT_WAIT_S = float(os.environ.get("POOL_ADMISSION_429_DEFAULT_WAIT_S") or 5.0)

# 2026-11-13：配额 429 / 等头超时后的「当次换号」。
# 背景（生产 11:04–11:06 实锤）：账号 8 触发 monthly 限额后，会话仍被钉在
# `<model>@a8s4` 这个单 deployment 别名上；sidecar 只对容量准入 429 做原体重发，
# 配额 429 原样透传；随后账号 9 的会话 `@a9s2` 等头超时 3 次 → 固定 502
# `session-sidecar upstream error`。别名组里只有一个账号，LiteLLM 无法在同名
# 模型组内换号，必须在 sidecar 层主动改写成另一个健康账号的别名后重发。
# 仅当**尚未向客户端写出任何字节**时触发；换号预算默认 2 次（配额 429）+ 1 次（等头超时），
# 换号后仍失败则维持原有 429/502 行为。
QUOTA_429_MARKS = (b"usage limit reached", b"usage limit exceeded",
                   b"Extra Usage", b"Go Plus", b"insufficient_quota")
QUOTA_FAILOVER_ATTEMPTS = max(0, int(os.environ.get("POOL_QUOTA_FAILOVER_ATTEMPTS") or 2))
HEADER_FAILOVER_ATTEMPTS = max(0, int(os.environ.get("POOL_HEADER_FAILOVER_ATTEMPTS") or 1))
# 2026-11-13 11:50 实锤：guard 因账号集合变化 `docker restart litellm`，sidecar 换到 a5 后
# 仍连续收到 `Connection reset by peer`（容器启动中）；原逻辑只换 1 次，3 个客户端重试全部 502。
# 传输层 reset/refused 不等待直接 502 太脆：这里按 0.5/1/2/3s 退避重试（可换号则换号），
# 总预算默认 4 次（约 6.5s），足以覆盖一次容器重启的启动窗口；只有真正持续不可用才 502。
TRANSPORT_FAILOVER_ATTEMPTS = max(0, int(os.environ.get("POOL_TRANSPORT_FAILOVER_ATTEMPTS") or 4))
TRANSPORT_FAILOVER_DELAY_S = max(0.0, float(os.environ.get("POOL_TRANSPORT_FAILOVER_DELAY_S") or 0.5))


def _admission_retry_after_s(body):
    """从 429 报文解析上游建议等待秒数：`retry shortly (after 16 s)` → 16s；解析失败给默认值。"""
    try:
        m = re.search(rb"after\s+(\d+(?:\.\d+)?)\s*s", body or b"")
        if m:
            return max(1.0, min(float(m.group(1)), ADMISSION_429_MAX_WAIT_S))
    except Exception:
        pass
    return max(1.0, min(ADMISSION_429_DEFAULT_WAIT_S, ADMISSION_429_MAX_WAIT_S))


def _admission_wait(seconds):
    try:
        time.sleep(max(0.0, float(seconds)))
    except Exception:
        pass


LOG_LOCK = threading.Lock()


def slog(msg):
    with LOG_LOCK:
        print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ================================================================ session_id 注入与三源归一（2026-10-07 sidstable）
_FALLBACK_SID_RE = re.compile(r"^ext-[0-9a-f]{32}$")


def _first_text_hash(messages):
    """会话稳定号锚：sha256(首条 user 消息)；无 user 消息时退化到首条任意角色消息。"""
    if not isinstance(messages, list):
        return None
    first = None
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            first = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, sort_keys=True)
            break
    if first is None:
        for m in messages:
            if isinstance(m, dict):
                c = m.get("content")
                first = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, sort_keys=True)
                break
    if not first:
        return None
    return hashlib.sha256(first[:4096].encode("utf-8", "ignore")).hexdigest()


# ================================================================ 会话树键 tree key（2026-10-12 treepin-s0）
# DSH 侧插件 @local/dsh-pool-affinity 在「进入模型的消息」末尾追加稳定标记。
# 目的：一个会话里启动的团队（队长 + 全部子代理）共享同一锚 → 号池可把整棵树钉到同一账号。
# 优先级：树键 > 客户端会话头（非 nginx 随机兜底）> body metadata.session_id > 首条消息哈希。
# 标记缺失时，本文件的行为与改动前逐字一致（fail-open）。
#
# treepin-s5（2026-10-19）：插件 v2 起改用**零宽字符载荷**（界面上不可见，请求体字节里照旧存在）。
#   载荷 = MAGIC + flag + body，MAGIC = U+2060 U+200D U+200B U+200C（WJ ZWJ ZWSP ZWNJ）
#     flag 0 = 裸 uuid（32 hex）→ body 64 符号（16 字节）
#     flag 1 = session-<uuid>            → body 64 符号（16 字节）
#     flag 2 = 其它 [A-Za-z0-9_-]{4,64}  → body = 4 符号长度 + 4*len 符号（每字节 4 符号）
#     末尾 4 符号 = 校验和 sum(flag+body 各位) mod 256（挡掉碰巧出现在载荷内部的 MAGIC 与截断）
#     再补 1 个 U+2060 终止哨兵（不可见/不可断行/非空白，保证消息末字符不会被任何 trim 吃掉）
#   符号表 = [U+200B, U+200C, U+200D, U+2060] → 0..3（每符号 2 bit，UTF-8 恒为 3 字节）
#   请求体里可能是**原始 UTF-8 字节**（E2 80 8B …），也可能是 **JSON \uXXXX 转义形态**
#   （上游序列化器 ensure_ascii=True 时），两种都认；两种都没有时再退回旧的可见字面量。
#   旧字面量 [pool-tree: <id>] 继续读——老会话/回滚期间照旧生效（向后兼容）。
_ZW_ALPHABET = {0x200B: 0, 0x200C: 1, 0x200D: 2, 0x2060: 3}
_ZW_MAGIC = "\u2060\u200d\u200b\u200c"
_ZW_MAGIC_BYTES = _ZW_MAGIC.encode("utf-8")
_ZW_BYTE_DIGITS = {chr(cp).encode("utf-8"): d for cp, d in _ZW_ALPHABET.items()}
_ZW_ESC_RE = re.compile(rb"\\u2060\\u200d\\u200b\\u200c", re.IGNORECASE)
_ZW_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_TREE_RE = re.compile(rb"\[pool-tree:[ \t]*([A-Za-z0-9_-]{4,64})\]")
TREE_SHADOW_FILE = os.path.join(HOME, "pool-tree-shadow.jsonl")
_TREE_LOG_SEEN = {}
_TREE_LOG_LOCK = threading.Lock()


def _zw_decode(take):
    """按零宽载荷布局解码：take(n) 返回 n 个符号值或 None；任何不合规都返回 None。

    布局 = MAGIC + flag + body + 校验和(4 符号)；校验和 = sum(flag + body 各位) mod 256。
    校验和是必需的：零宽字符只有 4 个，MAGIC 这 4 符号窗口在载荷里约 0.4% 概率碰巧出现，
    没有校验和就会把「碰巧的内部 MAGIC」解成一个看起来合法的假树键。
    """
    head = take(1)
    if not head:
        return None
    flag = head[0]
    digits = [flag]
    if flag in (0, 1):
        body = take(64)
        if not body:
            return None
        digits += body
        hexs = "".join("%02x" % ((body[i] << 6) | (body[i + 1] << 4) | (body[i + 2] << 2) | body[i + 3])
                       for i in range(0, 64, 4))
        uuid = "%s-%s-%s-%s-%s" % (hexs[0:8], hexs[8:12], hexs[12:16], hexs[16:20], hexs[20:])
        key = ("session-" + uuid) if flag == 1 else uuid
    elif flag == 2:
        lb = take(4)
        if not lb:
            return None
        size = (lb[0] << 6) | (lb[1] << 4) | (lb[2] << 2) | lb[3]
        if size < 4 or size > 64:
            return None
        body = take(size * 4)
        if not body:
            return None
        digits += lb + body
        raw = bytes((body[i] << 6) | (body[i + 1] << 4) | (body[i + 2] << 2) | body[i + 3]
                    for i in range(0, len(body), 4))
        try:
            key = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:
        return None                      # flag 3 保留
    if not _ZW_KEY_RE.match(key):
        return None
    chk = take(4)
    if not chk:
        return None
    if ((chk[0] << 6) | (chk[1] << 4) | (chk[2] << 2) | chk[3]) != (sum(digits) & 0xFF):
        return None
    return key


def _zw_take_bytes(body, start):
    """原始 UTF-8 形态的符号读取器。"""
    state = {"i": start}

    def take(count):
        out = []
        i = state["i"]
        for _ in range(count):
            digit = _ZW_BYTE_DIGITS.get(bytes(body[i:i + 3]))
            if digit is None:
                return None
            out.append(digit)
            i += 3
        state["i"] = i
        return out

    return take


def _zw_take_escaped(body, start):
    """JSON \\uXXXX 转义形态的符号读取器（大小写十六进制都认）。"""
    state = {"i": start}

    def take(count):
        out = []
        i = state["i"]
        for _ in range(count):
            chunk = body[i:i + 6]
            if len(chunk) != 6 or chunk[0:2] != b"\\u":
                return None
            try:
                cp = int(chunk[2:6], 16)
            except ValueError:
                return None
            digit = _ZW_ALPHABET.get(cp)
            if digit is None:
                return None
            out.append(digit)
            i += 6
        state["i"] = i
        return out

    return take


_ZW_MAX_TRIES = 512      # 单次请求最多试多少个「候选 MAGIC 位置」（防病态 body 拖 CPU）


def _zw_tree_key(body):
    """零宽载荷 → 树键。

    候选 MAGIC 位置**从左往右**试，取第一个「校验和通过」的载荷：
    真正的载荷起点一定在「载荷内部碰巧出现的 MAGIC」之前，所以从左开始才是对的
    （用 rfind 会从内部假 MAGIC 开始解，必然解错）。同一会话树里所有载荷值相同，
    一个 body 里有多条历史消息的载荷时，取哪一个都等价。
    """
    tries = 0
    at = body.find(_ZW_MAGIC_BYTES)
    while at >= 0 and tries < _ZW_MAX_TRIES:
        tries += 1
        key = _zw_decode(_zw_take_bytes(body, at + len(_ZW_MAGIC_BYTES)))
        if key:
            return key
        at = body.find(_ZW_MAGIC_BYTES, at + 1)
    for hit in _ZW_ESC_RE.finditer(body):
        if tries >= _ZW_MAX_TRIES:
            break
        tries += 1
        key = _zw_decode(_zw_take_escaped(body, hit.end()))
        if key:
            return key
    return None


def tree_key(body_bytes):
    """从请求体里取会话树键；纯字节级，绝不改动 messages。

    顺序：零宽载荷（新，界面不可见）→ 旧的可见字面量 [pool-tree: …]（向后兼容）。
    """
    try:
        body = body_bytes or b""
        key = _zw_tree_key(body)
        if key:
            return key
        m = _TREE_RE.search(body)
        return m.group(1).decode("ascii") if m else None
    except Exception:
        return None


def note_tree(tree, model):
    """S0 影子：每棵树每 60 s 最多记一条（只写文件 + 一行日志），任何异常静默。"""
    try:
        if not tree:
            return
        _pins_touch(tree)          # 2026-10-31：记录树最后活跃时间（软淘汰迁移判定用）
        now = time.time()
        with _TREE_LOG_LOCK:
            last = _TREE_LOG_SEEN.get(tree)
            if last is not None and now - last < 60:
                return
            _TREE_LOG_SEEN[tree] = now
            if len(_TREE_LOG_SEEN) > 4096:
                _TREE_LOG_SEEN.clear()
        with open(TREE_SHADOW_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": int(now), "tree": tree, "model": model,
                                 "mode": "shadow"}, ensure_ascii=False) + "\n")
        slog("tree-shadow tree=%s model=%s" % (tree, model))
    except Exception:
        pass


# ================================================================ 会话树钉号 tree affinity（2026-10-12 treepin-s1）
# 读份额引擎的 routing-share.json，用树键做确定性加权选号 → 一个会话树固定一个账号。
# 选号不落状态：acct = weighted_pick(sha256("tree:" + tree), weights = share)，重启/重放结果一致。
# mode 来自 /opt/litellm/pool-affinity.json（默认 shadow），热读、可秒级回退。
# 一切异常都不干预请求（fail-open）：拿不到份额 → 不加标签，LiteLLM 维持原有 least-busy。
AFFINITY_CONTROL_FILE = os.path.join(HOME, "pool-affinity.json")
# treepin-s4b：config.yaml 里的池内别名 —— 旧式 <model>@a<N> 与 s6 会话桶式 <model>@a<N>s<K>
_ALIAS_RE = re.compile(r"model_name:\s*(\S+@a[0-9]+(?:s[0-9]+)?)")
# 会话桶数，必须与 gen_config.py 的 SESSION_BUCKETS 一致；
# 不一致时 <model>@a<N>s<K> 在 config.yaml 里查不到 → pick_alias 退回旧别名，不会打错号。
SESSION_BUCKETS = max(1, int(os.environ.get("POOL_SESSION_BUCKETS", "8") or "8"))


def session_bucket(tree):
    """树键 → 会话桶号（1..SESSION_BUCKETS）。无状态：重启/重放同一棵树结果一致。"""
    h = hashlib.sha256(("sess:" + str(tree)).encode("utf-8", "ignore")).hexdigest()
    return int(h[:12], 16) % SESSION_BUCKETS + 1


# ================================================================ 动态 session（2026-10-24 T-sess）
# 背景（实测）：上游按 x-opencode-session 计「快档预算」，一个串用约 5-6 次请求
# （~5k 输出 token）后被压到 ~100 t/s，且 15 分钟内不恢复；而号池把全站流量压在
# 每账号 8 个固定串上，这 8 个串早已烧热 ⇒ 默认态就是慢档。冷串可达 ~160 t/s。
# 做法：别名部署不再写静态 x-opencode-session（gen_config/reconciler 的 POOL_DYNAMIC_SESSION），
#       sidecar 每请求按树键/轮次派生一个串，经 litellm 的
#       general_settings.forward_client_headers_to_llm_api 透传给上游（实测可穿透）。
# 档位（POOL_SESSION_ROTATE）：
#   off     默认：注入值 = 旧静态头逐字相同（dsh-acct-{N}[-s{K}]）⇒ 行为零变化，回滚只需改这里
#   tree    每棵会话树一个串（语义正当；一棵会话树就是一个会话）
#   turn    每轮一个串（同一轮内多步共用，轮与轮之间换；轮次指纹=消息条数+末条消息前缀）
#   request 每请求一个串（最快；等于把上游按 session 计的限额摊平，需自行决策风险）
SESSION_HEADER = "x-opencode-session"
SESSION_ROTATE = (os.environ.get("POOL_SESSION_ROTATE") or "off").strip().lower()


def turn_fingerprint(body_bytes):
    """本轮指纹 = **最后一条 user 消息**的内容哈希。

    注意（2026-10-24 修正）：不能把「消息条数」计入指纹 —— 一个 agent 轮次内每一步
    都会追加 assistant/tool 消息，条数每步都变，那会让 turn 档退化成 per-request
    （档位名不副实）。用最后一条 user 消息：同一用户轮次内稳定，用户发下一条消息才变。
    """
    try:
        d = json.loads(body_bytes) or {}
        msgs = d.get("messages")
        if not isinstance(msgs, list):
            msgs = d.get("input") if isinstance(d.get("input"), list) else []
        last_user = None
        for m in (msgs if isinstance(msgs, list) else []):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user = m
        if last_user is None and isinstance(msgs, list) and msgs:
            last_user = msgs[-1] if isinstance(msgs[-1], dict) else None
        if last_user is None:
            return ""
        c = last_user.get("content")
        text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(text[:512].encode("utf-8", "ignore")).hexdigest()[:8]
    except Exception:
        return ""


def acct_of_model(body_bytes):
    """从**已被改写**的请求体 model（<model>@a<N>s<K>）里取账号号；取不到返回 None。

    用最终 body 而不是中途选号结果：避免"改写用 A 号、session 头写成 B 号"的不一致。
    """
    try:
        m = (json.loads(body_bytes) or {}).get("model") or ""
        mm = re.search(r"@a(\d+)s?\d*$", str(m))
        return mm.group(1) if mm else None
    except Exception:
        return None


def pool_session_id(tree, acct, bucket, body_bytes):
    """派生上游 x-opencode-session；返回 None = 不注入（走兜底部署的静态头）。"""
    if not acct:
        return None                      # 没被改写成别名 ⇒ 落到兜底部署，静态头已覆盖
    if SESSION_ROTATE == "off":          # 与原静态头逐字一致 ⇒ 零回归、回滚只改这里
        return "dsh-acct-%s" % acct if not tree else "dsh-acct-%s-s%s" % (acct, bucket)
    if not tree:
        salt = "b" + hashlib.sha256(body_bytes).hexdigest()[:8]
    elif SESSION_ROTATE == "tree":
        salt = ""
    elif SESSION_ROTATE == "turn":
        salt = "t" + turn_fingerprint(body_bytes)
    elif SESSION_ROTATE == "request":
        salt = "r" + hashlib.sha256(body_bytes).hexdigest()[:8]
    else:
        salt = ""
    h = hashlib.sha256(("pses:%s|%s" % (tree, salt)).encode("utf-8", "ignore")).hexdigest()[:12]
    return "dsh-a%s-%s" % (acct, h)


_ALIAS_CACHE = {"checked": 0.0, "set": set(), "ok": False, "reason": "init"}
AFFINITY_SHARE_FILE = os.path.join(HOME, "routing-share.json")
AFFINITY_DECISIONS_FILE = os.path.join(HOME, "pool-affinity-decisions.jsonl")
_AFF_CACHE = {"checked": 0.0, "accounts": [], "mode": "shadow", "reason": "init"}
_AFF_LOCK = threading.Lock()
_AFF_LAST = {}
AFFINITY_TTL = 5.0
ALIAS_TTL = 30.0
ALIAS_FILE = os.path.join(HOME, "pool-aliases.json")


def alias_set():
    """treepin-s4b：config.yaml 中真实存在的别名集合（30s 缓存）。

    ok=False（文件读不到）→ 调用方退回改动前行为；
    ok=True 且集合为空（读到了但确实没有别名）→ 调用方不得改写，
    否则会把请求指向 config.yaml 里不存在的 deployment。
    """
    now = time.time()
    if now - _ALIAS_CACHE["checked"] < ALIAS_TTL:
        return _ALIAS_CACHE
    _ALIAS_CACHE["checked"] = now
    try:
        # 2026-10-17：DB 化后 config.yaml 不再写 model_list，别名清单改由生成器落盘的
        # pool-aliases.json 提供；该文件缺失时仍回退解析 config.yaml（旧部署无缝兼容）。
        names = None
        if os.path.isfile(ALIAS_FILE):
            with open(ALIAS_FILE, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            got = doc.get("aliases") if isinstance(doc, dict) else None
            if isinstance(got, list):
                names = set(str(x) for x in got)
        if names is None:
            with open(AFFINITY_CONFIG_FILE, "r", encoding="utf-8") as fh:
                names = set(_ALIAS_RE.findall(fh.read()))
        _ALIAS_CACHE["set"] = names
        _ALIAS_CACHE["ok"] = True
        _ALIAS_CACHE["reason"] = "ok:%d" % len(names)
    except Exception as e:
        _ALIAS_CACHE["set"] = set()
        _ALIAS_CACHE["ok"] = False
        _ALIAS_CACHE["reason"] = "err:%s" % str(e)[:60]
    return _ALIAS_CACHE


def pick_alias(tree, model, key_ref=None):
    """按树键确定性选号 → 返回该账号的池内别名；任何异常都返回 None（不改写）。

    key_ref 为 None（默认，保持旧调用兼容）：按原逻辑加权选号。
    key_ref 给出时：直接用该账号（粘性钉号），不再重新加权——
    share rebalance 会改变 weighted_pick 的落点，同一会话中途换号会让上游
    400（thinking 模型的 encrypted reasoning 与签发 key 绑定，换号后回放
    旧 key 签发的内容直接被拒，且此后每轮都 400）。
    两种路径都做同样的合法性校验（格式 / 别名存在），通不过一律返回 None。
    """
    try:
        if not tree or not isinstance(model, str) or not model or "@a" in model:
            return None
        if key_ref is None:
            key_ref, _ = choose_tag(tree)
            if not key_ref:
                return None
        elif not re.match(r"^OCG_KEY_[0-9]+$", str(key_ref)):
            return None
        m = re.match(r"^OCG_KEY_([0-9]+)$", key_ref)
        if not m:
            return None
        acct = m.group(1)
        bucketed = "%s@a%ss%d" % (model, acct, session_bucket(tree))
        legacy = "%s@a%s" % (model, acct)
        cache = alias_set()
        known = cache["set"]
        if not cache.get("ok"):       # config 不可读：保持改动前的行为
            return legacy
        if bucketed in known:
            return bucketed
        if legacy in known:           # 尚未生成桶别名：安全降级
            return legacy
        return None
    except Exception:
        return None
AFFINITY_CONFIG_FILE = os.path.join(HOME, "config.yaml")     # treepin-s1b：以它为「账号当前是否在池」的权威
_ACCT_TAG_RE = re.compile(r"acct:(OCG_KEY_[0-9]+)")


def affinity_state():
    """热读控制文件与份额文件（TTL 5 s）。

    返回 {"mode","accounts","states","reason"}：
      · accounts = [(key_ref, share)]，只含「可接收新树」的账号（share>0 且窗口全 ok）；
      · states   = {key_ref: {"share","low","hard","available","gated","rem_pct"}}，
        覆盖 routing-share 里的**全部**账号，供 sticky 区分软淘汰 / 硬淘汰。
    2026-10-31 新增 states（淘汰分级）：`low` = 账号可用但额度用到 ε 线导致 share 归零，
    对已钉住的活跃树允许“暂缓迁移”；`hard` = rate-limited / usage 不可用，必须立即重选。
    """
    now = time.time()
    with _AFF_LOCK:
        if now - _AFF_CACHE["checked"] < AFFINITY_TTL:
            return _AFF_CACHE
        _AFF_CACHE["checked"] = now
        mode = "shadow"
        reason = "control-missing"
        try:
            with open(AFFINITY_CONTROL_FILE, "r", encoding="utf-8") as fh:
                raw = json.load(fh) or {}
            m = str(raw.get("mode") or "shadow")
            mode = m if m in ("shadow", "full") else "shadow"
            reason = "ok"
        except FileNotFoundError:
            mode, reason = "shadow", "control-missing"
        except Exception as e:
            mode, reason = "shadow", "control-error:%s" % str(e)[:60]
        accounts = []
        states = {}
        alloc = "share-missing"
        try:
            with open(AFFINITY_SHARE_FILE, "r", encoding="utf-8") as fh:
                doc = json.load(fh) or {}
            for a in doc.get("accounts") or []:
                key_ref = a.get("key_ref")
                if not key_ref:
                    continue
                key_ref = str(key_ref)
                wins = a.get("windows") or {}
                unhealthy = any((w or {}).get("status") not in (None, "ok") for w in wins.values())
                share = a.get("share")
                share = float(share) if isinstance(share, (int, float)) else 0.0
                avail = bool(a.get("available", True))
                gated = bool(a.get("gated")) or unhealthy or not avail
                # 兼容旧 routing-share（无 hard/low 字段）：按 share/可用性推导
                hard = a.get("hard")
                low = a.get("low")
                if hard is None:
                    hard = gated
                if low is None:
                    low = (not hard) and share <= 0
                states[key_ref] = {"share": share, "low": bool(low), "hard": bool(hard),
                                   "available": avail, "gated": bool(gated),
                                   "rem_pct": a.get("rem_pct")}
                if hard or share <= 0:
                    continue
                accounts.append((key_ref, share))
            try:
                _alias_n = len(alias_set()["set"])
            except Exception:
                _alias_n = 0
            alloc = ("ok" if accounts else "no-healthy-account") + "|alias-groups:%d" % _alias_n
        except Exception as e:
            alloc = "share-error:%s" % str(e)[:60]
        _AFF_CACHE["mode"] = mode
        _AFF_CACHE["accounts"] = accounts
        _AFF_CACHE["states"] = states
        _AFF_CACHE["reason"] = "%s/%s" % (reason, alloc)
        return _AFF_CACHE


def weighted_pick(tree, accounts):
    """确定性加权选号：同一棵树永远落同一账号。"""
    if not tree or not accounts:
        return None
    total = sum(w for _, w in accounts)
    if total <= 0:
        return None
    h = int(hashlib.sha256(("tree:" + str(tree)).encode("utf-8")).hexdigest()[:12], 16)
    point = (h % 1000000) / 1000000.0 * total
    acc = 0.0
    for key_ref, weight in accounts:
        acc += weight
        if point < acc:
            return key_ref
    return accounts[-1][0]


def choose_tag(tree):
    """返回 (key_ref, tag) 或 (None, None)。

    2026-10-17 hot-control：若选号命中控制面里 enabled:false 的槽位，则放弃本次选号
    （不写 key_want、不改写到该槽位别名）→ 请求退回默认型号，由 LiteLLM least-busy 处理。
    控制面不可用（cfg None）或结构不符 → 不过滤，行为与改动前逐字一致。
    """
    try:
        st = affinity_state()
        picked = weighted_pick(tree, st["accounts"])
        if not picked:
            return None, None
        slots = disabled_slots()
        if slots:
            slot = _key_slot(picked)
            if slot is not None and slot in slots:
                note_acct_disabled(slot)
                return None, None
        return picked, "acct:" + picked
    except Exception:
        return None, None


# ---- treepin-s6b：同树同账号粘性钉号（2026-10-22）----
# 背景：share-engine 每轮 rebalance 会改写各账号权重；weighted_pick 对同一棵树会算出
# 不同账号 → 同一会话中途换号。上游 thinking 模型的 encrypted reasoning 与签发 key 绑定，
# 换号后回放旧 key 签发的内容直接 400（not issued to this caller），且此后每轮都 400。
# 实测：2026-10-22 16:10 份额刷新把某树从 a4 换到 a6，随即连续 400。
# 对策：树一旦钉住某账号就不再跟随权重 rebalance 而漂移（sticky）；只有钉住的号不可用
# （被剔除/禁用/别名缺失）时才允许重选。新树仍按当前权重分布（负载均衡不受影响）。
# 归属文件与决策日志同目录；任何异常都回退到旧逻辑（fail-open）。
PIN_FILE = os.path.join(HOME, "pool-tree-pins.json")
_PIN = {"loaded": False, "map": {}, "ts": {}, "defer": {}, "saved": 0.0}
_PIN_LOCK = threading.Lock()
_PIN_CAP = 20000

# ---- 2026-10-31：软淘汰（额度用到 ε 线）下的迁移与连续性保护 ----
# 背景：share-engine 在 used≥（100−EPS_PCT）时把 pace/share 归零；旧实现直接让 sticky 失效，
# 已钉住的会话下一条请求就被硬切到别的账号 → 上游 prefix cache 全冷 + thinking 的
# encrypted reasoning 与签发 key 绑定（换号回放会 400）。这里把淘汰分成：
#   hard：rate-limited / usage 不可用 / 控制面禁用 / 别名缺失  → 必须立即重选；
#   low ：账号仍健康、别名仍在，只是额度到线                        → 活跃树暂缓，空闲树才迁移。
SOFT_REPIN_ENABLE = str(os.environ.get("POOL_SOFT_REPIN", "1")).strip().lower() not in (
    "0", "false", "off", "no")
REPIN_IDLE_S = max(0.0, float(os.environ.get("POOL_REPIN_IDLE_S") or 600.0))
LOW_MAX_S = max(60.0, float(os.environ.get("POOL_LOW_MAX_S") or 1800.0))


def _pins_load_from(path):
    """返回 (tree→key, tree→ts, tree→defer)；兼容 v1（仅 key）与 v2。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh) or {}
        pins = doc.get("pins") if isinstance(doc, dict) else None
        if not isinstance(pins, dict):
            return {}, {}, {}
        mp, ts, defer = {}, {}, {}
        for t, v in pins.items():
            if not isinstance(t, str) or not t:
                continue
            if isinstance(v, dict):
                k, tsv, dfv = v.get("key"), v.get("ts"), v.get("defer")
            else:
                k, tsv, dfv = v, None, None
            if not (isinstance(k, str) and re.match(r"^OCG_KEY_[0-9]+$", k)):
                continue
            mp[t] = k
            if isinstance(tsv, (int, float)) and tsv > 0:
                ts[t] = float(tsv)
            if isinstance(dfv, (int, float)) and dfv > 0:
                defer[t] = float(dfv)
        return mp, ts, defer
    except Exception:
        return {}, {}, {}


def _pins_seed_from_decisions(path, limit=8000):
    """用最近决策日志预填（部署瞬间不产生全量重选）。只取 mode=full 的最后一条。

    同时回填树活跃时间（决策日志 ts 为秒），避免重启后被误判为“空闲树”而立即迁移。
    """
    out, ts = {}, {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        for line in lines[-limit:]:
            try:
                r = json.loads(line)
            except Exception:
                continue
            t = r.get("tree")
            k = r.get("key_want")
            if (r.get("mode") == "full" and isinstance(t, str) and t
                    and isinstance(k, str) and re.match(r"^OCG_KEY_[0-9]+$", k)):
                out[t] = k
                if isinstance(r.get("ts"), (int, float)) and r["ts"] > 0:
                    ts[t] = float(r["ts"])
    except Exception:
        return {}, {}
    return out, ts


def _pins_ensure_loaded():
    if _PIN["loaded"]:
        return
    with _PIN_LOCK:
        if _PIN["loaded"]:
            return
        mp, ts, defer = _pins_load_from(PIN_FILE)
        if not mp:
            mp, ts = _pins_seed_from_decisions(AFFINITY_DECISIONS_FILE)
            if mp:
                _PIN.update(map=mp, ts=dict(ts), defer={})
                _pins_save_locked()
        else:
            _PIN.update(map=mp, ts=dict(ts), defer=dict(defer))
        # v1 pins 没有 ts：从决策日志回填该树最后活跃时间，避免重启后把活跃树的
        # 「软淘汰暂缓」误判成「空闲树直接迁移」（2026-10-31）
        try:
            _seed_map, seed_ts = _pins_seed_from_decisions(AFFINITY_DECISIONS_FILE)
            backfilled = 0
            for t in _PIN["map"]:
                if t not in _PIN["ts"] and isinstance(seed_ts.get(t), (int, float)):
                    _PIN["ts"][t] = float(seed_ts[t])
                    backfilled += 1
            if backfilled:
                _pins_save_locked()
        except Exception:
            pass
        _PIN["loaded"] = True


def _pins_save_locked():
    try:
        tmp = PIN_FILE + ".tmp-%d" % os.getpid()
        pins = {}
        for t, k in _PIN["map"].items():
            rec = {"key": k}
            tsv = _PIN["ts"].get(t)
            dfv = _PIN["defer"].get(t)
            if tsv:
                rec["ts"] = round(float(tsv), 3)
            if dfv:
                rec["defer"] = round(float(dfv), 3)
            pins[t] = rec
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"v": 2, "pins": pins}, fh)
        os.replace(tmp, PIN_FILE)
        _PIN["saved"] = time.time()
    except Exception:
        pass


def _pins_touch(tree, now=None):
    """记录树最后活跃时间（软淘汰迁移判定用）。5s 去抖 + 30s 写盘节流。"""
    if not tree:
        return
    now = time.time() if now is None else now
    try:
        _pins_ensure_loaded()
        with _PIN_LOCK:
            if now - float(_PIN["ts"].get(tree) or 0.0) < 5:
                return
            _PIN["ts"][tree] = now
            if len(_PIN["ts"]) > _PIN_CAP:
                for t in list(_PIN["ts"])[: len(_PIN["ts"]) - _PIN_CAP]:
                    _PIN["ts"].pop(t, None)
            if now - float(_PIN["saved"] or 0.0) >= 30:
                _pins_save_locked()
    except Exception:
        pass


def _pins_remember(tree, key):
    if not tree or not key:
        return
    _pins_ensure_loaded()
    with _PIN_LOCK:
        now = time.time()
        same = _PIN["map"].get(tree) == key
        _PIN["ts"][tree] = now
        if same and tree not in _PIN["defer"]:
            if now - float(_PIN["saved"] or 0.0) >= 30:
                _pins_save_locked()
            return
        _PIN["map"][tree] = key
        _PIN["defer"].pop(tree, None)         # 换号/新钉 → 清除暂缓计时
        while len(_PIN["map"]) > _PIN_CAP:
            _PIN["map"].pop(next(iter(_PIN["map"])))
        _pins_save_locked()


def _pins_defer_get(tree):
    _pins_ensure_loaded()
    with _PIN_LOCK:
        return float(_PIN["defer"].get(tree) or 0.0)


def _pins_defer_set(tree, ts):
    _pins_ensure_loaded()
    with _PIN_LOCK:
        _PIN["defer"][tree] = float(ts)
        _pins_save_locked()


def _pins_defer_clear(tree):
    _pins_ensure_loaded()
    with _PIN_LOCK:
        if _PIN["defer"].pop(tree, None) is not None:
            _pins_save_locked()


def _pins_last_active(tree):
    _pins_ensure_loaded()
    with _PIN_LOCK:
        return float(_PIN["ts"].get(tree) or 0.0)


def _alias_ok_for(tree, model, key_ref):
    """该 model 在 key_ref 上是否有真实别名（config 不可读 → 视为可用，保持旧行为）。"""
    try:
        m = re.match(r"^OCG_KEY_([0-9]+)$", str(key_ref))
        if not m:
            return False
        cache = alias_set()
        if not cache.get("ok"):
            return True
        known = cache.get("set") or set()
        acct = m.group(1)
        return ("%s@a%ss%d" % (model, acct, session_bucket(tree)) in known
                or "%s@a%s" % (model, acct) in known)
    except Exception:
        return False


def sticky_account_ex(tree, model):
    """返回 (可沿用的旧钉选 key_ref | None, repin 原因 | None)。

    2026-10-31 分级：
      · 有钉选且账号在健康份额集合（share>0 且窗口全 ok）→ 沿用（原因 None）；
      · 硬淘汰（rate-limited/usage 不可用/控制面禁用/别名缺失）→ 立即重选（"hard"）；
      · 软低额度（账号可用、别名在，只是额度到 ε 线）→
          - 活跃树（距上次请求 < POOL_REPIN_IDLE_S）→ 继续沿用（"soft-defer"）；
          - 暂缓累计超过 POOL_LOW_MAX_S → 强制迁移（"soft-max"）；
          - 空闲树（≥ POOL_REPIN_IDLE_S）→ 允许迁移（"soft-idle"）；
          - POOL_SOFT_REPIN=0 → 保持旧行为，立即迁移（"soft-off"）。
    任何异常 → 返回 (None, None)（fail-open，走加权）。
    """
    try:
        if not tree or not isinstance(model, str) or not model or "@a" in model:
            return None, None
        _pins_ensure_loaded()
        with _PIN_LOCK:
            pinned = _PIN["map"].get(tree)
        if not pinned or not re.match(r"^OCG_KEY_[0-9]+$", pinned):
            return None, None
        try:
            st = affinity_state()
            eligible = {str(k) for k, _ in (st.get("accounts") or [])}
            info = (st.get("states") or {}).get(pinned) or {}
            mode = st.get("mode")
        except Exception:
            return None, None
        # 控制面禁用 → 硬
        try:
            slots = disabled_slots()
            if slots:
                slot = _key_slot(pinned)
                if slot is not None and slot in slots:
                    _pins_defer_clear(tree)
                    return None, "hard"
        except Exception:
            return None, None
        # 别名缺失 → 硬（改写到不存在的 deployment 会 404）
        if not _alias_ok_for(tree, model, pinned):
            _pins_defer_clear(tree)
            return None, "hard"
        if pinned in eligible:
            _pins_defer_clear(tree)
            return pinned, None
        # 不在「可接新树」集合：区分软/硬
        if not isinstance(info, dict):
            # routing-share 里完全没有该账号（被移除/文档不可读）→ 视为硬淘汰，立即重选
            _pins_defer_clear(tree)
            return None, "hard"
        hard = bool(info.get("hard"))
        if hard or mode != "full":
            _pins_defer_clear(tree)
            return None, "hard"
        if not SOFT_REPIN_ENABLE:
            _pins_defer_clear(tree)
            return None, "soft-off"
        now = time.time()
        last = _pins_last_active(tree)
        idle = (now - last) if last > 0 else None
        if idle is None:
            # 无活跃记录：保守迁移（与旧行为一致，不额外拖延）
            return None, "soft-idle"
        if idle < REPIN_IDLE_S:
            first = _pins_defer_get(tree)
            if not first:
                _pins_defer_set(tree, now)
                first = now
            if now - first < LOW_MAX_S:
                return pinned, "soft-defer"      # 活跃会话：暂缓迁移，保住缓存/推理连续性
            return None, "soft-max"              # 活跃太久：强制迁移，避免真撞 429
        _pins_defer_clear(tree)
        return None, "soft-idle"
    except Exception:
        return None, None



def sticky_account(tree, model):
    """向后兼容包装：只返回可沿用的 key_ref 或 None（repin 原因用 sticky_account_ex）。"""
    return sticky_account_ex(tree, model)[0]

def strip_alias(model):
    """去掉模型名尾部的 @aN / @aNsK 别名后缀（handler 视角的 body 已被改写，需先还原再判定）"""
    if not isinstance(model, str):
        return model
    return re.sub(r"@a[0-9]+(s[0-9]+)?$", "", model)


def _model_of_body(body):
    """从请求体里取 model（只用于日志归因；解析失败返回 None，绝不影响转发）。"""
    try:
        if not body:
            return None
        doc = json.loads(body)
        m = doc.get("model") if isinstance(doc, dict) else None
        return m if isinstance(m, str) else None
    except Exception:
        return None


def resolve_pin_ex(tree, model):
    """粘性钉号解析 → (key, alias, sticky, repin)。

    sticky=True 表示沿用旧钉；repin ∈ None | "hard" | "soft-idle" | "soft-max" |
    "soft-off" | "soft-defer"（软淘汰暂缓时 key 仍是旧号、sticky 仍为 True，
    repin="soft-defer" 只用于观测）。alias 为 None 表示不应改写。
    任何异常 → (None, None, False, None)（fail-open）。
    """
    try:
        if not tree or not isinstance(model, str) or not model or "@a" in model:
            return None, None, False, None
        key, repin = sticky_account_ex(tree, model)
        if key:
            a = pick_alias(tree, model, key)
            if a:
                return key, a, True, ("soft-defer" if repin == "soft-defer" else None)
            # 别名在 alias_set 里但 pick_alias 仍失败（缓存竞态）→ 交回加权，标记 hard
            return None, None, False, "hard"
        key2, _ = choose_tag(tree)
        if not key2:
            return None, None, False, repin
        return key2, pick_alias(tree, model, key2), False, repin
    except Exception:
        return None, None, False, None



def resolve_pin(tree, model):
    """向后兼容包装：返回 (key, alias, sticky)（repin 原因见 resolve_pin_ex）。"""
    return resolve_pin_ex(tree, model)[:3]


def _is_transport_error(err):
    """连接被重置/拒绝/半关闭：通常是 LiteLLM 重启、连接池更新或代理瞬时故障，适合退避重试。"""
    try:
        return isinstance(err, (ConnectionResetError, ConnectionRefusedError, BrokenPipeError,
                                http.client.RemoteDisconnected, http.client.IncompleteRead))
    except Exception:
        return False


def _alias_key_of_body(send_body):
    """从已被改写的请求体 model（<model>@aN[sK]）里取当前账号 key_ref；取不到返回 None。"""
    try:
        m = re.search(r"@a([0-9]+)", _model_of_body(send_body) or "")
        return ("OCG_KEY_%s" % m.group(1)) if m else None
    except Exception:
        return None


def _failover_pin(tree, model, excluded):
    """选一个可换号的目标账号 + 别名；优先 healthy 份额集合，尽量避开 low/hard 状态。

    返回 (key_ref, alias) 或 (None, None)。仅用于「当前账号已限额/不可用」的故障路径，
    不改变新会话的正常加权选号。
    """
    try:
        st = affinity_state()
        excluded = set(excluded or ())
        try:
            disabled = disabled_slots() or set()
        except Exception:
            disabled = set()

        def ok_slot(key):
            slot = _key_slot(key)
            return slot is None or slot not in disabled

        ordered = []
        for key, weight in (st.get("accounts") or []):
            if key in excluded or not ok_slot(key):
                continue
            ordered.append((key, float(weight) if isinstance(weight, (int, float)) else 1.0))
        # healthy 集合为空时，退而求其次：states 里非 hard / 非 low 的账号
        if not ordered:
            for key, info in (st.get("states") or {}).items():
                if key in excluded or not ok_slot(key) or info.get("hard") or info.get("low"):
                    continue
                ordered.append((key, 1.0))
        if not ordered:
            for key, info in (st.get("states") or {}).items():
                if key in excluded or not ok_slot(key) or info.get("hard"):
                    continue
                ordered.append((key, 1.0))
        if not ordered:
            return None, None
        picked = weighted_pick(tree, ordered)
        candidates = [picked] + [k for k, _ in ordered if k != picked] if picked else [k for k, _ in ordered]
        for key in candidates:
            if not key:
                continue
            alias = pick_alias(tree, model, key)
            if alias:
                return key, alias
        return None, None
    except Exception:
        return None, None


def failover_rewrite(send_body, hdrs, tree, base_model, excluded):
    """把请求体改写到另一个账号的别名，并更新 x-opencode-session/Content-Length。

    返回 (new_body, new_headers, new_psess, new_key, new_alias) 或 None。
    只在尚未向客户端写响应头时调用。
    """
    try:
        if not send_body or not tree or not isinstance(base_model, str) or not base_model:
            return None
        key, alias = _failover_pin(tree, base_model, excluded)
        if not key or not alias:
            return None
        doc = json.loads(send_body)
        if not isinstance(doc, dict):
            return None
        doc["model"] = alias
        new_body = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        new_hdrs = dict(hdrs or {})
        acct = acct_of_model(new_body)
        psess = pool_session_id(tree, acct, session_bucket(tree) if tree else None, new_body)
        if psess:
            for k in [k for k in list(new_hdrs) if k.lower() == SESSION_HEADER]:
                new_hdrs.pop(k, None)
            new_hdrs[SESSION_HEADER] = psess
        new_hdrs["Content-Length"] = str(len(new_body))
        # 更新树钉号，后续请求直接走新账号，不再回退到限额号
        _pins_remember(tree, key)
        return new_body, new_hdrs, psess, key, alias
    except Exception:
        return None

def note_affinity(tree, model, picked, tag, mode, reason, repin=None):
    """决策落盘 + 一行日志（每棵树每 60 s 最多一条）。

    repin：2026-10-31 软淘汰迁移标记（None/hard/soft-idle/soft-max/soft-off/soft-defer），
    写入 decisions JSON 的 repin 字段并追加到侧车日志，便于验收“活跃树不换号”。
    """
    try:
        if not tree:
            return
        now = time.time()
        with _AFF_LOCK:
            last = _AFF_LAST.get(tree)
            if last is not None and now - last < 60:
                return
            _AFF_LAST[tree] = now
            if len(_AFF_LAST) > 4096:
                _AFF_LAST.clear()
        injected = bool(tag) and mode == "full"
        rec = {"ts": int(now), "tree": tree, "model": model,
               "key_want": picked, "tag": tag, "mode": mode,
               "reason": reason, "injected": injected}
        if repin:
            rec["repin"] = repin
        with open(AFFINITY_DECISIONS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        slog("tree-affinity tree=%s model=%s key_want=%s mode=%s injected=%s reason=%s%s"
             % (tree, model, picked, mode, injected, reason,
                (" repin=%s" % repin) if repin else ""))
    except Exception:
        pass


# ================================================================ 控制面热读：allowlist 与账号启用（2026-10-17 hot-control）
# 控制面模块 pool/pool_control.py（看板管理化）约定：
#     from pool_control import load_config
#     cfg = load_config()          # None=不可用；否则 cfg["models"]["allow"]、cfg["accounts"]
#     cfg["accounts"] 每项含 slot（整数）与 enabled（bool）
# 该模块自带 2 s TTL 缓存并自行解析 ${POOL_HOME}/pool-config.json：
# 本文件每请求直接调用它（不自己读文件、不再做二次缓存），改配置 ≤2 s 内热生效，无需重启。
# 硬约束：本节的任何路径都必须 fail-open —— 导入失败、抛异常、返回 None、结构不符、
# JSON 解析失败，一律退回本文件改动前的行为（放行全部模型、/v1/models 走原硬编码集合、
# 不做账号过滤），绝不能让控制面故障变成启动失败或全站 404/503。
_POOL_ALIAS_RE = re.compile(r"^.+@a[0-9]+(?:s[0-9]+)?$")
_CTRL_FN = None                                  # 已成功导入的 load_config；None = 尚未成功
_CTRL_LOCK = threading.Lock()
_CTRL_NEXT_TRY = 0.0                             # 导入失败后的下次重试时刻（避免每请求都 import）
_CTRL_RETRY_SEC = 10.0
ACCT_SKIP_LOG_TTL = 10.0                         # 同槽位禁用日志节流（每请求一行会刷爆日志）
_ACCT_SKIP_LAST = {}
_ACCT_SKIP_LOCK = threading.Lock()


def _cfg_get(node, key):
    """控制面结构兼容：dict / MappingProxyType 等映射协议，其次对象属性；取不到或异常 → None。

    pool_control.load_config() 返回的是 types.MappingProxyType 冻结快照（不是 dict 子类、
    也不支持属性访问），所以必须走 node[key] 映射协议，不能只判 isinstance(dict)。
    """
    try:
        if isinstance(node, dict):
            return node.get(key)
        try:
            return node[key]
        except Exception:
            return getattr(node, key, None)
    except Exception:
        return None


def control_config():
    """热读控制面配置；不可用 → None（fail-open）。导入只成功一次，失败每 10 s 重试一次。"""
    global _CTRL_FN, _CTRL_NEXT_TRY
    try:
        fn = _CTRL_FN
        if fn is None:
            now = time.time()
            with _CTRL_LOCK:
                fn = _CTRL_FN
                if fn is None and now >= _CTRL_NEXT_TRY:
                    _CTRL_NEXT_TRY = now + _CTRL_RETRY_SEC
                    try:
                        from pool_control import load_config as fn
                        _CTRL_FN = fn
                    except Exception as e:
                        slog("pool-control import-fail passthrough: %s" % str(e)[:120])
                        return None
        if fn is None:
            return None
        return fn()
    except Exception:
        return None


def allow_models():
    """allowlist → 非空 set；不可用/为空/结构不符 → None（fail-open，不做拦截）。"""
    try:
        allow = _cfg_get(_cfg_get(control_config(), "models"), "allow")
        if not isinstance(allow, (list, tuple, set)):
            return None
        ids = set()
        for x in allow:
            try:
                s = str(x).strip()
            except Exception:
                continue
            if s:
                ids.add(s)
        return ids or None
    except Exception:
        return None


def disabled_slots():
    """控制面里 enabled:false 的 slot 集合；控制面不可用/结构不符 → None（不做账号过滤）。"""
    try:
        cfg = control_config()
        if cfg is None:
            return None
        accounts = _cfg_get(cfg, "accounts")
        if not isinstance(accounts, (list, tuple)):
            return None
        out = set()
        for a in accounts:
            try:
                if _cfg_get(a, "enabled") is not False:
                    continue
                slot = _cfg_get(a, "slot")
                if isinstance(slot, bool):
                    continue
                out.add(int(slot))
            except Exception:
                continue
        return out
    except Exception:
        return None


def _key_slot(key_ref):
    """OCG_KEY_<N> → N；取不到返回 None。"""
    try:
        m = re.search(r"OCG_KEY_([0-9]+)", str(key_ref))
        return int(m.group(1)) if m else None
    except Exception:
        return None


def note_acct_disabled(slot):
    """一行 `acct-disabled skip slot=N`；同槽位 10 s 内最多一条，避免刷爆请求路径日志。"""
    try:
        now = time.time()
        with _ACCT_SKIP_LOCK:
            if now - _ACCT_SKIP_LAST.get(slot, 0.0) < ACCT_SKIP_LOG_TTL:
                return
            _ACCT_SKIP_LAST[slot] = now
            if len(_ACCT_SKIP_LAST) > 256:
                _ACCT_SKIP_LAST.clear()
        slog("acct-disabled skip slot=%s" % slot)
    except Exception:
        pass


# OpenAI 风格 404 错误体（逐字节固定，客户端/DSH 按 code 判读）
DENY_MODEL_BODY = (b'{"error":{"message":"model not found",'
                   b'"type":"invalid_request_error","code":"model_not_found"}}')


def deny_model(body_bytes, method, path):
    """allowlist 热门禁：命中 → 返回要回给客户端的 404 体；放行 / 任何异常 → None。

    只作用于 POST 且路径含 chat/completions 或 /responses 的推理请求；
    控制面不可用、allowlist 为空、body/model 解析失败 → None（原样透传，fail-open）。
    """
    try:
        if method != "POST" or not body_bytes:
            return None
        p = path or ""
        if "chat/completions" not in p and "/responses" not in p:
            return None
        allow = allow_models()
        if not allow:                       # 控制面不可用/为空：连 body 都不用解析
            return None
        try:
            d = json.loads(body_bytes)
        except Exception:
            return None
        if not isinstance(d, dict):
            return None
        model = d.get("model")
        if not isinstance(model, str) or not model:
            return None
        if model in allow:
            return None
        if model.split("@", 1)[0] in allow:  # 客户端显式带别名，但主型号在 allowlist
            return None
        if _POOL_ALIAS_RE.match(model):      # 池内别名 <model>@a<N>[s<K>] 不拦
            return None
        slog("model-not-found model=%s path=%s" % (model[:80], p[:80]))
        return DENY_MODEL_BODY
    except Exception:
        return None


def _first_text_hash_any(d):
    """首条内容哈希：优先 messages（Chat Completions），其次 input（Responses，可为字符串或 item 列表）。"""
    msgs = d.get("messages")
    if msgs:
        return _first_text_hash(msgs)
    inp = d.get("input")
    if isinstance(inp, str):
        return hashlib.sha256(inp[:4096].encode("utf-8", "ignore")).hexdigest()
    if isinstance(inp, list):
        return _first_text_hash(inp)
    return None


def baseline_session(body_bytes, inbound_sid=None):
    """解析 JSON → pool_src 恒注入、session_id 三源归一为唯一 final 值。
    返回 (改写后 dict 或 None, final session_id 或 None)。解析失败 → (None, None) 原样透传。"""
    try:
        d = json.loads(body_bytes)
        if not isinstance(d, dict):
            return None, None
        md = d.get("metadata")
        md = dict(md) if isinstance(md, dict) else {}
        body_sid = md.get("session_id")
        fallback = bool(inbound_sid) and bool(_FALLBACK_SID_RE.match(str(inbound_sid)))
        h = _first_text_hash_any(d)                  # treepin-s3：兼容 Responses 的 input
        tree = tree_key(body_bytes)                  # treepin-s0：DSH 插件注入的会话树键
        if tree:
            final = tree[:64]                        # 最高优先：整棵会话树共享同一锚
        elif inbound_sid and not fallback:
            final = str(inbound_sid)[:64]            # 客户端自带会话头，尊重
        elif body_sid:
            final = str(body_sid)[:64]               # 客户端自带 body 会话号，尊重
        elif h:
            final = ("ext-" + h[:12]) if inbound_sid else h[:16]   # 稳定哈希（外网带来源标记）
        else:
            final = str(inbound_sid) if inbound_sid else None      # 无锚可哈 → 保留随机号兜底
        # treepin-s4b：会话树钉号 —— 按树键把 model 改写成池内别名（客户端可见面不变）
        # treepin-s6b：粘性优先——同树已钉号则沿用（rebalance 不漂移；上游 encrypted
        # reasoning 与签发 key 绑定，中途换号会 400 且此后每轮都 400）。只有钉住的号
        # 不可用（被剔除/禁用/别名缺失）时才回退加权，届时记一次新钉选。
        try:
            if final:
                _key_ref, _alias0, _sticky0, _repin0 = resolve_pin_ex(final, d.get("model"))
                if _repin0:
                    md["pool_repin"] = _repin0          # 观测：软淘汰暂缓/迁移原因
                if _key_ref:
                    md["key_want"] = _key_ref            # pool_logger 的 P3 字段 → 两种协议都能验收
                    if affinity_state().get("mode") == "full" and _alias0:
                        d["model"] = _alias0
                        md["pool_alias"] = _alias0
                        md["sess_class"] = "tree-pinned"
                        _pins_remember(final, _key_ref)
        except Exception:
            pass
        md["pool_src"] = "sidecar"
        if final:
            md["session_id"] = final
        d["metadata"] = md
        return d, final
    except Exception:
        return None, None


def process_request(body_bytes, inbound_sid=None):
    """仅 metadata 注入；messages 字节绝不改动。异常 → 原样透传。"""
    try:
        parsed, sess = baseline_session(body_bytes, inbound_sid)
        if parsed is None:
            return None, None
        msgs_before = json.dumps(parsed.get("messages"), ensure_ascii=False, sort_keys=True)
        out = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        msgs_after = json.dumps(parsed.get("messages"), ensure_ascii=False, sort_keys=True)
        if msgs_before != msgs_after:               # 硬约束：messages 字节绝不改动
            raise RuntimeError("messages mutated")
        return out, sess
    except Exception as e:
        slog("process-err-passthrough %s" % str(e)[:140])
        return None, None


# ================================================================ HTTP 纯透传（原转发逻辑保留）
# ================================================================ 号池实例键 pool instance keys（2026-10-01）
# 任何 DSH/客户端持 sk-pool-* 实例键即可经本 sidecar 使用号池全部模型（openai-completions
# 格式）；sidecar 将实例键换成本机 LITELLM_MASTER_KEY 上送 LiteLLM —— 主键不出号池，
# 实例键逐个可吊销（改 JSON 即时生效，按 mtime 热加载，无需重启）。
# 未带键/未知键 → 原样透传，由 LiteLLM 返回标准 OpenAI 错误体（与本文件 fail-open 一致）。
# 日志只记 label，绝不记键值；messages 字节不动的约束不受影响（只动 Authorization 头）。
KEYS_FILE = os.path.join(HOME, "dsh-instance-keys.json")
ENV_FILE = os.path.join(HOME, ".env")
_BEARER_RE = re.compile(r"^\s*[Bb]earer\s+(\S+)\s*$")


def _instance_keys():
    """Instance keys from JSON + OCG_KEY_* from .env (both map -> master).

    The DSH Models-page route save writes whatever sits in the API-key box
    into the credential store; users habitually paste OCG upstream keys
    there, which LiteLLM rejects as virtual keys (400 no_db). Accepting
    OCG_KEY_* here as aliases makes that habit harmless: both key families
    map to the pool master upstream, so chat/discovery keep working.
    """
    keys = {}
    try:
        with open(KEYS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            keys.update({str(k): str(v) for k, v in data.items() if not str(k).startswith("_")})
    except Exception:
        pass
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("OCG_KEY_") and "=" in line:
                    name, _, val = line.partition("=")
                    val = val.strip().strip(chr(34)).strip(chr(39))
                    if val:
                        keys[val] = name.strip()
    except Exception:
        pass
    return keys


def _master_key():
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("LITELLM_MASTER_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def apply_pool_auth(hdrs):
    """Map a known sk-pool-* instance key to the pool master key upstream.

    nginx lower-cases pass-through header names, so the inbound dict can carry
    ``authorization`` while an earlier write used ``Authorization`` — sending
    both makes LiteLLM read the instance key (virtual-key -> no_db). Delete
    every case variant first, then set exactly one canonical header.
    """
    variants = [k for k in list(hdrs) if k.lower() == "authorization"]
    if not variants:
        return
    raw = hdrs[variants[0]]
    m = _BEARER_RE.match(str(raw))
    if not m:
        return
    presented = m.group(1)
    for candidate, label in _instance_keys().items():
        if hmac.compare_digest(presented, candidate):
            master = _master_key()
            if master:
                for k in variants:
                    hdrs.pop(k, None)
                hdrs["Authorization"] = "Bearer " + master
                slog("pool-key ok label=%s" % label)
            return


class _UpstreamSilent(Exception):
    """上游连续静默超过 POOL_SSE_MAX_SILENCE_S —— 主动断开，别让客户端白等。"""


class _UpstreamStream:
    """把上游 HTTPResponse 的读搬到后台线程，让主线程能观测到"上游静默"。

    http.client.HTTPResponse 没有读超时；直接改 socket 超时又会和 BufferedReader 的
    内部缓冲纠缠。这里用「守护线程 + 队列」：主线程只做 queue.get(timeout)，
    超时就说明这段时间上游一个字节都没来。

    队列不设上限：一旦调用方失败/提前返回，finally 里的 conn.close() 会让 read1
    抛异常、线程自然退出；设上限反而会让生产者在无人消费时永久阻塞。
    """

    def __init__(self, resp, chunk=CHUNK):
        self._resp = resp
        self._chunk = chunk
        self._q = queue.Queue()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        try:
            while True:
                c = self._resp.read1(self._chunk)
                if not c:
                    self._q.put(("eof", None))
                    return
                self._q.put(("data", c))
        except Exception as e:                      # noqa: BLE001
            self._q.put(("error", e))

    def next(self, timeout):
        """('data', bytes) / ('eof', None) / ('error', exc) / ('idle', None)"""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return ("idle", None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # ---- 2026-10-17 流式转发辅助 ------------------------------------------ #
    def end_headers(self):
        # 记下"响应头已发出"：异常分支据此决定是发 502 还是只补终止块。
        self._hdrs_sent = True
        return BaseHTTPRequestHandler.end_headers(self)

    def _w(self, ob):
        """裸写一段字节（已 flush），并记一次"给客户端发过东西"。"""
        self.wfile.write(ob)
        self.wfile.flush()
        self._last_out = time.monotonic()

    def _wchunk(self, ob):
        """按 chunked 帧写一段字节（已 flush）。"""
        self.wfile.write(b"%x\r\n" % len(ob) + ob + b"\r\n")
        self.wfile.flush()
        self._last_out = time.monotonic()

    def _maybe_hb(self):
        """距上次写客户端已超过 SSE_HEARTBEAT_S → 补一条 SSE 注释心跳。

        注释帧（以 ':' 开头）是 SSE 规范里客户端**必须忽略**的保活手段，
        不会产生任何 delta，只是让客户端的 idle 计时器看到"有字节"。
        """
        if not self._hb_on or SSE_HEARTBEAT_S <= 0:
            return
        now = time.monotonic()
        if now - self._last_out < SSE_HEARTBEAT_S:
            return
        if self._framing == "chunked":
            self.wfile.write(b"%x\r\n" % len(SSE_HB_FRAME) + SSE_HB_FRAME + b"\r\n")
        elif self._framing == "close":
            self.wfile.write(SSE_HB_FRAME)
        else:                                   # Content-Length 定长体不能插字节
            return
        self.wfile.flush()
        self._last_out = now
        self._hb_n += 1

    def _iter_chunks(self, resp):
        """逐块读上游；上游静默时 yield None（调用方据此发心跳并检查静默上限）。

        非 SSE 响应保持原来的直读，不引入额外线程。
        """
        if not self._hb_on:
            while True:
                c = resp.read1(CHUNK)
                if not c:
                    return
                yield c
            return
        rd = _UpstreamStream(resp)
        silent = 0.0
        step = SSE_HEARTBEAT_S if SSE_HEARTBEAT_S > 0 else 600.0
        while True:
            kind, payload = rd.next(step)
            if kind == "data":
                silent = 0.0
                yield payload
                continue
            if kind == "eof":
                return
            if kind == "error":
                raise payload
            silent += step
            if SSE_MAX_SILENCE_S > 0 and silent >= SSE_MAX_SILENCE_S:
                raise _UpstreamSilent("upstream silent %.0fs" % silent)
            yield None

    def _upstream_getresponse(self, conn, method, body, hdrs, sess):
        """等上游响应头：带等待预算、慢请求遥测、超时后按原请求体重发（默认共 3 次机会）。

        仅覆盖「拿到响应头之前」：此时尚未向客户端写过任何字节，重发是安全的；
        一旦拿到头，下层照旧把 socket 超时恢复成长值，流式转发逻辑完全不变。
        """
        import socket as _socket

        attempts = HEADER_WAIT_ATTEMPTS if HEADER_WAIT_TIMEOUT_S > 0 else 1
        _model = None                      # 只在失败路径解析：便于把卡顿归因到具体账号别名
        for attempt in range(1, attempts + 1):
            t0 = time.monotonic()
            if HEADER_WAIT_TIMEOUT_S > 0:
                try:
                    conn.sock.settimeout(HEADER_WAIT_TIMEOUT_S)
                except Exception:
                    pass
            try:
                resp = conn.getresponse()
            except (TimeoutError, _socket.timeout) as e:
                waited = time.monotonic() - t0
                if _model is None:
                    _model = _model_of_body(body)
                slog("upstream-header-timeout sess=%s path=%s model=%s attempt=%d/%d waited=%.1fs limit=%.0fs"
                     % (sess, self.path, _model, attempt, attempts, waited, HEADER_WAIT_TIMEOUT_S))
                try:
                    conn.close()
                except Exception:
                    pass
                if attempt >= attempts:
                    # 全试完仍无响应头：外层异常分支返回 502（客户端可见）
                    slog("upstream-header-timeout-final sess=%s path=%s model=%s attempts=%d -> 502"
                         % (sess, self.path, _model, attempts))
                    raise
                if HEADER_WAIT_RETRY_DELAY_S > 0:
                    time.sleep(HEADER_WAIT_RETRY_DELAY_S)
                # 换一条连接重发：LiteLLM least-busy 会重新选账号/连接，避开卡住的那条
                conn = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=3600)
                conn.request(method, self.path, body=body if body else None, headers=hdrs)
                continue
            finally:
                try:
                    conn.sock.settimeout(None)   # 头之后恢复长超时，交给流式转发
                except Exception:
                    pass
            waited = time.monotonic() - t0
            if waited >= HEADER_WAIT_WARN_S:
                slog("upstream-header-slow sess=%s path=%s model=%s waited=%.1fs status=%s attempt=%d/%d"
                     % (sess, self.path, _model if _model is not None else _model_of_body(body),
                        waited, resp.status, attempt, attempts))
            return resp

    def _relay(self, method):
        conn = None
        # 流式转发状态（2026-10-17）：异常分支靠这三个字段决定怎么收尾。
        self._hdrs_sent = False
        self._framing = "length"        # "length" | "chunked" | "close"
        self._hb_on = False             # 是否允许发 SSE 心跳
        self._hb_n = 0                  # 本次请求发了多少条心跳（>0 会记日志，便于量化上游静默）
        self._last_out = time.monotonic()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            sess = "-"
            send_body = body
            final_sid = None
            _tree = None                                # treepin-s0：会话树键（动态 session 也要用）
            _base_model = None                          # 2026-11-13：换号时用于重选别名
            _failed_keys = set()                        # 本次请求已确认不可用的账号
            _psess = None                               # T-sess：本次注入给上游的 x-opencode-session
            aff_tag = None                              # treepin-s1：本请求要注入的账号标签
            # treepin-s3：DSH 走 /v1/responses（provider api=openai-responses），
            # 会话锚与树键两类逻辑都必须覆盖两种协议，否则对 DSH 流量完全失效。
            # 2026-10-17 hot-control：allowlist 热门禁必须在改写/建连之前判定 —— 命中即 404，
            # 绝不触达上游；控制面不可用/解析失败 → deny_model 返回 None，原样透传（fail-open）。
            _deny = deny_model(body, method, self.path)
            if _deny is not None:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(_deny)))
                self.end_headers()
                self.wfile.write(_deny)
                self.wfile.flush()
                return
            if method == "POST" and body and ("chat/completions" in self.path or "/responses" in self.path):
                out, key = process_request(body, self.headers.get("X-Session-Id"))
                final_sid = key
                if key:
                    sess = key
                if out is not None:
                    send_body = out
                _tree = tree_key(send_body)          # treepin-s0：影子观测（不改路由）
                if _tree:
                    try:
                        _model = (json.loads(send_body) or {}).get("model")
                    except Exception:
                        _model = None
                    note_tree(_tree, _model)
                    _st = affinity_state()           # treepin-s1：树键 → 确定性选号（s6b：粘性优先）
                    # 注意：此时 body 里的 model 已被 baseline 改写为别名（@aNsK）；
                    # 剥掉别名后缀再做粘性/加权判定，否则 "@a" 检查会直接跳过，导致决策日志断流。
                    _base_model = strip_alias(_model)
                    _picked, _alias_want, _sticky, _repin = resolve_pin_ex(_tree, _base_model)
                    _tag = ("acct:" + _picked) if _picked else None
                    if _tag:
                        # treepin-s4b：改为别名改写（在 baseline_session 内完成），此处只记决策
                        note_affinity(_tree, _model, _picked,
                                      _alias_want or _tag, _st["mode"], _st["reason"],
                                      repin=_repin)
            conn = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=3600)
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            apply_pool_auth(hdrs)
            if final_sid:
                # 与 body metadata 写同一个 final 值：下游 LiteLLM 实测以头为准，
                # 两路一致才不会被 nginx 的每请求随机号（ext-$request_id）顶掉钉号。
                for _k in [k for k in list(hdrs) if k.lower() == "x-session-id"]:
                    hdrs.pop(_k, None)
                hdrs["X-Session-Id"] = final_sid
            # ---- 2026-10-24（T-sess）：按请求注入 x-opencode-session ------------------
            # 别名部署已不再写静态 session（POOL_DYNAMIC_SESSION=1），靠这个头告诉上游
            # 「这是哪个会话」。POOL_SESSION_ROTATE=off 时注入值与旧静态头逐字相同。
            try:
                _acct = acct_of_model(send_body)
                _psess = pool_session_id(_tree, _acct, session_bucket(_tree) if _tree else None, send_body)
                if _psess:
                    for _k in [k for k in list(hdrs) if k.lower() == SESSION_HEADER]:
                        hdrs.pop(_k, None)
                    hdrs[SESSION_HEADER] = _psess
            except Exception:
                pass
            hdrs["Content-Length"] = str(len(send_body))
            # 2026-11-13：等头超时的当次换号。_upstream_getresponse 内部已对同一别名
            # 重试 HEADER_WAIT_ATTEMPTS 次；全失败后若还有健康账号，改写到另一个别名再试，
            # 避免 3×120s 后直接 502。
            _hdr_failovers = 0
            _transport_retries = 0
            while True:
                conn.request(method, self.path, body=send_body if send_body else None,
                             headers=hdrs)
                _hdr_t0 = time.monotonic()
                try:
                    resp = self._upstream_getresponse(conn, method, send_body, hdrs, sess)
                    self._hdr_s = time.monotonic() - _hdr_t0
                    break
                except Exception as _hdr_err:
                    # 2026-11-13：容器重启/连接池更新造成的 reset/refused 先退避重试，
                    # 不要立刻 502；有别的健康账号就换号，没有就同号等容器起来。
                    if (_is_transport_error(_hdr_err)
                            and _transport_retries < TRANSPORT_FAILOVER_ATTEMPTS
                            and _tree and _base_model and send_body):
                        _transport_retries += 1
                        _delay = min(TRANSPORT_FAILOVER_DELAY_S * (2 ** (_transport_retries - 1)), 3.0)
                        _cur = _alias_key_of_body(send_body)
                        if _cur:
                            _failed_keys.add(_cur)
                        _fo = failover_rewrite(send_body, hdrs, _tree, _base_model, _failed_keys)
                        _new_key = None
                        if _fo:
                            send_body, hdrs, _psess, _new_key, _new_alias = _fo
                        slog("upstream-transport-retry %d/%d sess=%s from=%s to=%s err=%s delay=%.1fs"
                             % (_transport_retries, TRANSPORT_FAILOVER_ATTEMPTS, sess,
                                _cur or "?", _new_key or "same", str(_hdr_err)[:80], _delay))
                        try:
                            time.sleep(max(0.0, _delay))
                        except Exception:
                            pass
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=3600)
                        continue
                    if (_hdr_failovers >= HEADER_FAILOVER_ATTEMPTS
                            or not _tree or not _base_model or not send_body):
                        raise
                    _cur = _alias_key_of_body(send_body)
                    if _cur:
                        _failed_keys.add(_cur)
                    _fo = failover_rewrite(send_body, hdrs, _tree, _base_model, _failed_keys)
                    if not _fo:
                        raise
                    send_body, hdrs, _psess, _new_key, _new_alias = _fo
                    _hdr_failovers += 1
                    slog("upstream-header-failover %d/%d sess=%s from=%s to=%s err=%s"
                         % (_hdr_failovers, HEADER_FAILOVER_ATTEMPTS, sess,
                            _cur or "?", _new_key, str(_hdr_err)[:80]))
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=3600)

            # ---- 2026-10-17 上游偶发误判 400 的有界重试（2026-10-27 扩签名 + 预算 2）---------------
            # 现象：同一会话、同一形态的请求前一分钟还是 200，突然回
            #   [unsupported_request] Provide between 1 and 512 text messages.
            #   （2026-10-27：还会把流式传输故障错标成 400 server_error，见 TRANSIENT_400_MARKS）
            # 判据：① 同会话相邻请求正常；② 用会话日志忠实重建的同形请求体直接打上游是 200；
            #       ③ 8 分钟内 ~10 条同形请求只有 1 条失败 ⇒ 上游侧抖动，不是请求形态问题。
            # 代价：DSH 不重试 4xx、池侧 num_retries=0，一次抖动直接废掉整轮对话。
            # 对策：仅在**尚未向客户端写出任何字节**时，按原请求体重发（最多 TRANSIENT_400_MAX_RETRIES 次）；
            #       其余 400 行为不变。
            if method == "POST" and resp.status == 400 and send_body:
                _err_body = resp.read()              # 400 响应体很小，先读完再决定
                _tries = 0
                while (any(m in _err_body for m in TRANSIENT_400_MARKS)
                       and _tries < TRANSIENT_400_MAX_RETRIES):
                    _tries += 1
                    slog("upstream-transient-400 retry-%d/%d sess=%s path=%s"
                         % (_tries, TRANSIENT_400_MAX_RETRIES, sess, self.path))
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=3600)
                    conn.request(method, self.path, body=send_body, headers=hdrs)
                    resp = self._upstream_getresponse(conn, method, send_body, hdrs, sess)
                    if resp.status != 400:
                        break
                    _err_body = resp.read()
                if resp.status == 400:
                    # 重试耗尽仍是 400（或本来就是非签名 400）：把已读出的字节原样回给客户端
                    if _tries:
                        slog("upstream-transient-400 retry-failed sess=%s status=%s tries=%d"
                             % (sess, resp.status, _tries))
                    self.send_response(resp.status)
                    for _k, _v in resp.getheaders():
                        if _k.lower() in HOP:
                            continue
                        self.send_header(_k, _v)
                    self.send_header("Content-Length", str(len(_err_body)))
                    self.end_headers()
                    self.wfile.write(_err_body)
                    self.wfile.flush()
                    return

            # ---- 2026-10-29 上游容量准入 429 的有界等待重试 ------------------------------
            # 匹配 ttft_admission_refused / prefill_cap_refused / overloaded（容量准入），按上游建议等待后原体重发，
            # 最多 2 次；配额 429 / 其它 429 原样透传。此时尚未向客户端写任何字节，可安全重发。
            if method == "POST" and resp.status == 429 and send_body:
                _err_body = resp.read()
                _tries_a = 0
                while (any(m in _err_body for m in ADMISSION_429_MARKS)
                       and _tries_a < ADMISSION_429_MAX_RETRIES):
                    _tries_a += 1
                    _wait = _admission_retry_after_s(_err_body)
                    _mark = next((m.decode("ascii", "ignore") for m in ADMISSION_429_MARKS
                                  if m in _err_body), "admission")
                    slog("upstream-admission-429 retry-%d/%d wait=%.0fs mark=%s sess=%s path=%s"
                         % (_tries_a, ADMISSION_429_MAX_RETRIES, _wait, _mark, sess, self.path))
                    _admission_wait(_wait)
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=3600)
                    conn.request(method, self.path, body=send_body, headers=hdrs)
                    resp = self._upstream_getresponse(conn, method, send_body, hdrs, sess)
                    self._hdr_s = time.monotonic() - _hdr_t0
                    if resp.status != 429:
                        break
                    _err_body = resp.read()
                # 2026-11-13：配额 429（账号额度耗尽）→ 当次换号重发，而不是透传给客户端。
                # 必须换别名：同别名 model group 只有该账号一个 deployment，LiteLLM 无法换号。
                _tries_q = 0
                while (not any(m in _err_body for m in ADMISSION_429_MARKS)
                       and any(m in _err_body for m in QUOTA_429_MARKS)
                       and _tries_q < QUOTA_FAILOVER_ATTEMPTS
                       and _tree and _base_model and send_body):
                    _cur = _alias_key_of_body(send_body)
                    if _cur:
                        _failed_keys.add(_cur)
                    _fo = failover_rewrite(send_body, hdrs, _tree, _base_model, _failed_keys)
                    if not _fo:
                        break
                    send_body, hdrs, _psess, _new_key, _new_alias = _fo
                    _tries_q += 1
                    slog("upstream-quota-429 failover-%d/%d sess=%s from=%s to=%s alias=%s"
                         % (_tries_q, QUOTA_FAILOVER_ATTEMPTS, sess,
                            _cur or "?", _new_key, _new_alias))
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=3600)
                    try:
                        conn.request(method, self.path, body=send_body, headers=hdrs)
                        _hdr_t0 = time.monotonic()
                        resp = self._upstream_getresponse(conn, method, send_body, hdrs, sess)
                        self._hdr_s = time.monotonic() - _hdr_t0
                    except Exception as _fo_err:
                        slog("upstream-quota-429 failover-error sess=%s tries=%d err=%s"
                             % (sess, _tries_q, str(_fo_err)[:80]))
                        break
                    if resp.status != 429:
                        break
                    _err_body = resp.read()
                if resp.status == 429:
                    if _tries_q:
                        slog("upstream-quota-429 failover-failed sess=%s status=429 tries=%d"
                             % (sess, _tries_q))
                    if _tries_a:
                        slog("upstream-admission-429 retry-failed sess=%s status=429 tries=%d"
                             % (sess, _tries_a))
                    self.send_response(resp.status)
                    for _k, _v in resp.getheaders():
                        if _k.lower() in HOP:
                            continue
                        self.send_header(_k, _v)
                    self.send_header("Content-Length", str(len(_err_body)))
                    self.end_headers()
                    self.wfile.write(_err_body)
                    self.wfile.flush()
                    return

            # ---- tool_call 修复（2026-10-11 toolrepair）：仅请求带 tools 且 200 时启用 ----
            ctype = resp.getheader("Content-Type", "") or ""
            repair_sse = (method == "POST" and "chat/completions" in self.path
                          and resp.status == 200 and b'"tools"' in send_body
                          and "text/event-stream" in ctype)
            repair_json = (method == "POST" and "chat/completions" in self.path
                           and resp.status == 200 and b'"tools"' in send_body
                           and "application/json" in ctype)
            sse = "text/event-stream" in ctype.lower()

            if method == "GET" and "/v1/models" in self.path and resp.status == 200:
                # 2026-10-12：外网模型发现只报硬编码白名单（网关本身不动，直连 :4000 仍是全量）。
                # 2026-10-17 hot-control：改为按控制面 allowlist 过滤（热生效，无需重启）；
                # 控制面不可用/allowlist 为空 → 回退原硬编码集合，行为与改动前一致。
                # 两种模式都剔除池内别名（<model>@a<N>[s<K>]）——别名永不外泄。
                # 解析失败则原样透传（fail-open）。
                import json as _json
                raw = resp.read()
                out = raw
                try:
                    doc = _json.loads(raw.decode("utf-8"))
                    allow = allow_models()
                    if allow:
                        keep = allow
                        src = "allowlist"
                    else:
                        keep = {"mimo-v2.6-flash", "mimo-v2.6-pro",
                                "muse-spark-1.3-contributor", "claude-haiku-5-5",
                                "deepseek-v4.1-flash"}
                        src = "hardcoded"
                    if isinstance(doc.get("data"), list):
                        doc["data"] = [m for m in doc["data"] if isinstance(m, dict)
                                       and m.get("id") in keep
                                       and "@" not in str(m.get("id"))]
                        # 2026-10-12：上游不报限额的模型按 DSH 约定值补齐（与 cordis 配置一致），
                        # 缺失时 DSH 获取填空；已有值的不覆盖。
                        limits = {"mimo-v2.6-flash": (1048576, 131072),
                                  "mimo-v2.6-pro": (1048576, 131072),
                                  "muse-spark-1.3-contributor": (838860, 8192),
                                  "deepseek-v4.1-flash": (1000000, 131072)}
                        for m in doc["data"]:
                            li = limits.get(m.get("id"))
                            if li:
                                if m.get("max_input_tokens") is None:
                                    m["max_input_tokens"] = li[0]
                                if m.get("max_output_tokens") is None:
                                    m["max_output_tokens"] = li[1]
                        out = _json.dumps(doc, ensure_ascii=False).encode("utf-8")
                        slog("models-filtered sess=%s kept=%d src=%s"
                             % (sess, len(doc["data"]), src))
                except Exception as e:
                    slog("models-filter passthrough: %s" % str(e)[:100])
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    kl = k.lower()
                    if kl in ("transfer-encoding", "content-length") or kl in HOP:
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                self.wfile.flush()
                return
            self.send_response(resp.status)
            raw_te = None
            has_cl = False
            for k, v in resp.getheaders():
                kl = k.lower()
                if kl == "transfer-encoding":
                    raw_te = v
                    continue
                if kl in HOP:
                    continue
                if kl == "content-length":
                    if repair_sse or repair_json:
                        continue            # 修复模式下长度会变：SSE 自行 chunked / JSON 重算
                    has_cl = True
                self.send_header(k, v)

            # 定下本次响应的分帧方式，并据此决定是否允许发心跳（只有能自由插入字节的
            # chunked / connection-close 流可以；Content-Length 定长体绝不能插）。
            if repair_sse or (raw_te and "chunked" in raw_te):
                self._framing = "chunked"
            elif has_cl:
                self._framing = "length"
            else:
                self._framing = "close"
            self._hb_on = bool(sse) and self._framing in ("chunked", "close")

            if repair_sse:
                # 重写模式：我们自己做 chunked 出帧；读入缓冲按行切 SSE data 帧，
                # 只改带空 id/name 的 tool_calls，其余字节原样转发。
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                state = toolrepair.StreamToolState()
                n_rep = [0]
                n_lost = [0]
                buf = b""

                for c in self._iter_chunks(resp):
                    if c is None:
                        # 上游静默：补心跳，别让客户端 90s idle 超时误杀
                        self._maybe_hb()
                        continue
                    buf += c
                    while True:
                        i = buf.find(b"\n")
                        if i < 0:
                            break
                        raw, buf = buf[:i + 1], buf[i + 1:]
                        outs, nr, nl = toolrepair.process_sse_line(raw, state)
                        n_rep[0] += nr
                        n_lost[0] += nl
                        for ob in outs:
                            self._wchunk(ob)
                    # 扣帧（toolrepair hold）时上游有数据但不下发 —— 同样要保活
                    self._maybe_hb()
                if buf:                     # 无换行结尾的残帧也走一遍（fail-open 原样）
                    outs, nr, nl = toolrepair.process_sse_line(buf, state)
                    n_rep[0] += nr
                    n_lost[0] += nl
                    for ob in outs:
                        self._wchunk(ob)
                outs, nr, nl = toolrepair.finish_repair_frames(state)
                n_rep[0] += nr
                n_lost[0] += nl
                for ob in outs:
                    self._wchunk(ob)
                # 2026-10-16 fix：终结块必须**裸写**，不能走 wchunk()。
                # wchunk() 会给载荷再套一层长度前缀，把 b"0\r\n\r\n" 变成
                # 「长度 5 的数据块 5\r\n0\r\n\r\n\r\n」，真正的零长度终结块
                # 从未发出 → HTTP/1.1 客户端（DSH）永远等不到响应结束，
                # 拿到全部内容与 [DONE] 后仍挂到自己的 idle 超时（90s/300s）
                # 才失败重试。DSH 每个请求都带 tools，所以每条池请求都中招。
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                if n_rep[0]:
                    slog("toolrepair sse repaired=%d lost_name=%d" % (n_rep[0], n_lost[0]))
            elif repair_json:
                raw = resp.read()
                out_bytes, n, lost = toolrepair.repair_json_tool_calls(raw)
                self.send_header("Content-Length", str(len(out_bytes)))
                self.end_headers()
                self._w(out_bytes)
                if n:
                    slog("toolrepair json repaired=%d lost_name=%d" % (n, lost))
            elif raw_te and "chunked" in raw_te:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for c in self._iter_chunks(resp):
                    if c is None:
                        self._maybe_hb()
                        continue
                    self._wchunk(c)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            elif not has_cl:
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                for c in self._iter_chunks(resp):
                    if c is None:
                        self._maybe_hb()
                        continue
                    self._w(c)
            else:
                self.end_headers()
                for c in self._iter_chunks(resp):
                    if c is None:
                        self._maybe_hb()
                        continue
                    self._w(c)
            _hdr_note = ""
            if getattr(self, "_hdr_s", 0.0) >= HEADER_WAIT_WARN_S:
                _hdr_note = " hdr=%.1fs" % self._hdr_s
            # 2026-10-24（T-sess）：把「本次实际发给上游的 x-opencode-session」记进来。
            # 否则生产上无法审计轮换档位是否按预期工作（只能靠吞吐反推）。
            # ps=- 表示本请求没有注入（未改写成别名 → 走兜底部署的静态头）。
            _ps_note = " ps=%s" % (_psess or "-")
            slog("%s %s sess=%s status=%s%s%s%s"
                 % (method, self.path, sess, resp.status, _ps_note,
                    (" hb=%d" % self._hb_n) if self._hb_n else "", _hdr_note))
        except Exception as e:  # noqa: BLE001
            # 2026-10-19：失败路径也带上 hb 计数。客户端在 ~90s 处断开时，
            # 「有没有发过心跳」是判定"idle 超时是否已修好"的唯一现场证据；
            # 只在成功路径记 hb= 会让失败样本无法归因。
            _hdr_seen = getattr(self, "_hdr_s", None)
            slog("ERR %s %s: %s ps=%s%s%s"
                 % (method, self.path, str(e)[:140], _psess or "-",
                    (" hb=%d" % self._hb_n) if getattr(self, "_hb_n", 0) else "",
                    (" hdr=%.1fs" % _hdr_seen) if _hdr_seen is not None else ""))
            if getattr(self, "_hdrs_sent", False):
                # 响应头/正文已经开始：**绝不能**再 send_response（那会把状态行写进 body，
                # 客户端拿到的是畸形流）。chunked 流补零长度终止块，让客户端立刻看到
                # "流已结束"；定长体/connection-close 只能断开（截断会被客户端立即识别）。
                try:
                    if self._framing == "chunked":
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    else:
                        self.close_connection = True
                except Exception:
                    self.close_connection = True
            else:
                try:
                    payload = b'{"error":{"message":"session-sidecar upstream error"}}'
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    self.wfile.flush()
                except Exception:
                    self.close_connection = True
        finally:
            if conn is not None:
                conn.close()

    def do_GET(self):
        self._relay("GET")

    def do_POST(self):
        self._relay("POST")

    def do_PUT(self):
        self._relay("PUT")

    def do_DELETE(self):
        self._relay("DELETE")


if __name__ == "__main__":
    server = ThreadingHTTPServer(LISTEN, Handler)
    server.daemon_threads = True
    # T-sess：启动时把轮换档位打出来，便于线上核对（每请求日志里的 ps= 是实际注入值）
    slog("session-sidecar (slim) listening 127.0.0.1:%d -> %d "
         "(session_id inject + toolrepair + passthrough) rotate=%s"
         % (LISTEN[1], UP_PORT, SESSION_ROTATE))
    server.serve_forever()
