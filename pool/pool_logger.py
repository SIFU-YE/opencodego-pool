"""LiteLLM custom callback: 记录每条成功请求使用了哪个号池账号 (OCG_KEY_n) 及 token 用量。
写入容器内 /app/pool-requests.jsonl，由号池看板 (dashboard.py) 经 docker exec 读取。
失败静默——绝不影响主请求流程。
⚠️ 升级/重建 litellm 容器后需重新 docker cp 本文件（docker restart 不受影响）。
⚠️ 当前 /opt/litellm/pool_logger.py ↔ /app/pool_logger.py 是单文件 bind-mount：
   · docker cp 对该挂载无效（device busy）；
   · 编辑宿主文件必须 in-place 写入（cat 新内容 > 本文件，保持 inode），否则容器仍看到
     旧 inode 的旧内容；若已被原子替换（换 inode），需 docker exec 从容器侧同步一份，
     保持两边 md5 一致；
   · 修改后 docker restart litellm 生效（callback 随进程加载）。
P3（FINAL §6.1-P3）：每条记录追加 share_want/key_want/sess_class/repin_epoch/est_cost
（从 metadata 读，缺省 null/""；老字段与老读者绝不破坏、失败静默）。

2026-10-24 流式观测（T-obs，§A of docs/巡检-20261024-流式吞吐与颗粒度体检.md）：
新增 5 个字段，全部失败静默、缺省 null：
  · ttft_ms  请求发出 → 收到**第一个带 delta 的 chunk**（含玄思 reasoning_content）的毫秒
  · ttfv_ms  请求发出 → 收到**第一个可见正文 token**（delta.content 非空）的毫秒
              （ttfv−ttft ≈ 思考/推理阶段耗时）
  · chunks   本次流式收到的 chunk 数（带 delta 的才算）
  · gen_ms   第一个 chunk → 最后一个 chunk 的毫秒（tps 的分母）
  · tps      completion_tokens / gen_s（真实解码速率；不含 TTFT，与 DSH 显示口径一致）
  · hook     "hit"/"miss"：流式钩子状态是否成功关联到本次请求（自检，正常应恒为 hit）
实现：async_post_call_streaming_iterator_hook 包裹流式生成器逐 chunk 打点，
      状态按 litellm_call_id 暂存；async_log_success_event 里取用并清理（带 900s TTL 兜底，
      流式中断/客户端断开时不会泄漏）。
"""
import asyncio
import json
import os
import threading
import time

try:
    from litellm.integrations.custom_logger import CustomLogger

    _HAVE_BASE = True
except Exception:  # pragma: no cover
    _HAVE_BASE = False

BASE = CustomLogger if _HAVE_BASE else object
LOG_PATH = "/app/pool-requests.jsonl"

# ---- 失败静默 + 有界自证：同一条路径最多打 20 次诊断，便于定位"为什么没写日志" ----
_DEBUG_BUDGET = {}
_DEBUG_LOCK = threading.Lock()


def _dbg(tag):
    """在"静默失败"的 except 里调用：不限流会刷爆日志，故每条 tag 最多 20 次。"""
    try:
        with _DEBUG_LOCK:
            n = _DEBUG_BUDGET.get(tag, 0)
            if n >= 20:
                return
            _DEBUG_BUDGET[tag] = n + 1
        import traceback
        print("[pool_logger-debug %s] %s\n%s" % (time.strftime("%H:%M:%S"), tag, traceback.format_exc()[:600]),
              flush=True)
    except Exception:  # noqa: BLE001
        pass

# ---- 流式打点状态：key = litellm_call_id -----------------------------------
_STREAM = {}
_STREAM_LOCK = threading.Lock()
_STREAM_TTL_S = 900.0


def _call_key(d):
    """从 request_data / kwargs 里取稳定的请求键（各版本字段位置不同，逐个兜底）。"""
    if not isinstance(d, dict):
        return None
    k = d.get("litellm_call_id")
    if k:
        return str(k)
    md = d.get("metadata") or {}
    if isinstance(md, dict):
        for kk in ("litellm_call_id", "trace_id", "request_id"):
            if md.get(kk):
                return str(md[kk])
    return None


def _purge_stale(now):
    """TTL 兜底：流式异常/客户端断开时不会走到 log_success，防止状态泄漏。"""
    if not _STREAM:
        return
    for k, v in list(_STREAM.items()):
        if now - v.get("t0", now) > _STREAM_TTL_S:
            _STREAM.pop(k, None)


def _delta_of(chunk):
    """取 (是否有delta, 正文文本, 推理文本)；任何异常返回空。"""
    try:
        chs = getattr(chunk, "choices", None) or []
        if not chs:
            return False, "", ""
        d = getattr(chs[0], "delta", None)
        if d is None:
            return False, "", ""
        txt = getattr(d, "content", None) or ""
        rz = getattr(d, "reasoning_content", None) or ""
        if not rz:
            rz = getattr(d, "reasoning", None) or ""
        return True, (txt if isinstance(txt, str) else ""), (rz if isinstance(rz, str) else "")
    except Exception:  # noqa: BLE001
        return False, "", ""


def _append_log(line):
    """追加一行日志；文件以 0600 创建（不依赖进程 umask）。

    · 日志**不含完整密钥**：keyRef 只留末 24 位（看板按尾缀映射，功能不受影响）；
    · 但日志仍含会话 id / 用量 / 账号尾号等运营数据，不该被同机其他用户读到，
      所以显式 0600 创建。
    """
    fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        # 旧版本创建的文件可能是 644：每次写入顺手收紧（fchmod 在 Linux 上有效）
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _ts_ms(t):
    """把 litellm 传进来的时间戳转成 epoch 毫秒。

    2026-10-24 实测：本版本（1.102.1）在流式回调里传的 start_time/end_time
    **不是 datetime**，而是 pydantic 对象，`.timestamp()` 直接 AttributeError，
    老代码把它静默吞掉 → 整条记录不写（这就是"日志断流"的真正原因）。
    这里按 datetime / 数值 / ISO 串 / pydantic(dict) 逐级兜底，取不到就 0。
    """
    if t is None:
        return 0
    for getter in (lambda x: x.timestamp(), lambda x: x):
        try:
            v = getter(t)
            if isinstance(v, (int, float)):
                v = float(v)
                if v > 1e11:          # 已经是毫秒
                    return int(v)
                if v > 1e9:           # 秒
                    return int(v * 1000)
        except Exception:  # noqa: BLE001
            pass
    # pydantic 对象 / dict：找常见字段
    try:
        d = t if isinstance(t, dict) else (
            t.model_dump() if hasattr(t, "model_dump") else getattr(t, "__dict__", {}) or {})
        if isinstance(d, dict):
            for k in ("end_time", "start_time", "timestamp", "created_at", "created", "time"):
                if k in d and d[k] is not None:
                    return _ts_ms(d[k])
            for k in ("endTime", "startTime"):
                if k in d and d[k] is not None:
                    v = float(d[k])
                    return int(v if v > 1e11 else v * 1000)
    except Exception:  # noqa: BLE001
        pass
    # ISO 字符串
    try:
        import datetime as _dt
        s = str(t)
        return int(_dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:  # noqa: BLE001
        pass
    try:
        if not _TS_TYPE_SEEN:
            _TS_TYPE_SEEN.append(1)
            print("[pool_logger-debug ts] 无法解析的时间对象 type=%r repr=%r" % (type(t), repr(t)[:200]),
                  flush=True)
    except Exception:  # noqa: BLE001
        pass
    return 0


_TS_TYPE_SEEN = []
_SHAPE_SEEN = []

# ---- 2026-10-24：sync / async 两条回调可能对同一请求都触发（配置里 callbacks 会同时注册两边）。
# 用 call_id 去重，保证「一请求一条记录」；TTL 兜底防止内存增长。
_DONE = {}
_DONE_LOCK = threading.Lock()
_DONE_TTL_S = 300.0


def _claim_once(key):
    """首次见到返回 True；120s 内重复返回 False（用于 sync/async 双回调去重）。"""
    if not key:
        return True
    now = time.time()
    with _DONE_LOCK:
        for k, ts in list(_DONE.items()):
            if now - ts > _DONE_TTL_S:
                _DONE.pop(k, None)
        if key in _DONE:
            return False
        _DONE[key] = now
        return True


def _usage_of(kwargs, response_obj):
    """防御式提取 token 用量（流式/非流式/异常响应都不得抛错）"""
    pt = ct = tt = 0
    try:
        u = (response_obj.get("usage") if isinstance(response_obj, dict) else getattr(response_obj, "usage", None)) or {}
        if isinstance(u, dict):
            pt = int(u.get("prompt_tokens") or 0)
            ct = int(u.get("completion_tokens") or 0)
            tt = int(u.get("total_tokens") or (pt + ct))
        else:
            pt = int(getattr(u, "prompt_tokens", 0) or 0)
            ct = int(getattr(u, "completion_tokens", 0) or 0)
            tt = int(getattr(u, "total_tokens", 0) or (pt + ct))
    except Exception:
        pass
    if not tt:
        try:
            cu = kwargs.get("complete_streaming_usage") or {}
            pt = int(cu.get("prompt_tokens") or 0)
            ct = int(cu.get("completion_tokens") or 0)
            tt = int(cu.get("total_tokens") or (pt + ct))
        except Exception:
            pass
    return pt, ct, tt


def _cached_of(kwargs, response_obj):
    """防御式提取 prompt 缓存命中 token（prompt_tokens_details.cached_tokens / cacheRead），取不到为 0，绝不抛错"""
    try:
        u = (response_obj.get("usage") if isinstance(response_obj, dict) else getattr(response_obj, "usage", None)) or {}
        if isinstance(u, dict):
            ptd = u.get("prompt_tokens_details") or {}
            v = ptd.get("cached_tokens") if isinstance(ptd, dict) else None
            if v is None:
                v = u.get("cache_read_input_tokens") or u.get("prompt_cache_hit_tokens") or u.get("cacheRead")
            return int(v or 0)
        ptd = getattr(u, "prompt_tokens_details", None)
        v = getattr(ptd, "cached_tokens", None) if ptd is not None else None
        if v is None:
            v = getattr(u, "cache_read_input_tokens", None) or getattr(u, "prompt_cache_hit_tokens", None)
        return int(v or 0)
    except Exception:
        return 0


def _stream_metrics(key):
    """取并清理该请求的流式打点状态。

    返回 (first_wall, first_visible_wall, last_wall, chunks) 或 None。
    这里回传**绝对时间**（time.time() 秒），TTFT 由调用方用请求开始时间（start_time）相减得到——
    钩子自己开始的时刻晚于请求开始（要等上游响应头到齐），用它做基准会把 TTFT 算成个位数毫秒。
    """
    if not key:
        return None
    with _STREAM_LOCK:
        st = _STREAM.pop(key, None)
    if not st:
        return None
    return st.get("first"), st.get("first_visible"), st.get("last"), (st.get("n") or 0)


class PoolLogger(BASE):
    # ---------------------------------------------------------------- 流式打点
    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict, response, request_data):
        """逐 chunk 打点后原样透传。**任何异常都必须继续 yield chunk**，绝不能吞掉/改写流。"""
        key = None
        st = None
        try:
            key = _call_key(request_data)
            now = time.time()
            _purge_stale(now)
            if key:
                st = {"t0": now, "first": None, "first_visible": None, "last": None, "n": 0}
                with _STREAM_LOCK:
                    _STREAM[key] = st
        except Exception:  # noqa: BLE001
            st = None
            _dbg("hook_setup")

        async for chunk in response:
            try:
                if st is not None:
                    now = time.time()
                    has_delta, txt, rz = _delta_of(chunk)
                    if has_delta:
                        st["n"] += 1
                        st["last"] = now
                        if st["first"] is None:
                            st["first"] = now
                        # 首个**可见正文** token：只看 delta.content，不含 reasoning_content
                        # （否则 ttfv≡ttft，ttfv−ttft 这个"思考耗时"就没意义了）
                        if st["first_visible"] is None and txt:
                            st["first_visible"] = now
            except Exception:  # noqa: BLE001
                _dbg("hook_chunk")
            yield chunk

    def _record(self, kwargs, start_time, end_time, response_obj=None, via="?"):
        try:
            lp = kwargs.get("litellm_params") or {}
            key = lp.get("api_key") or ""
            pt, ct, tt = _usage_of(kwargs, response_obj)
            cached = _cached_of(kwargs, response_obj)
            # 去重键：优先 litellm_call_id；取不到时退化成元组（同一请求 sync/async 的元组一致）
            _dk = _call_key(kwargs)
            if not _dk:
                _dk = "_anon_%s_%s_%s" % (_ts_ms(start_time), ct, str(key)[-8:])
            if not _claim_once(_dk):
                return
            try:
                _md = kwargs.get("metadata") or lp.get("metadata") or {}
                sess = str(_md.get("session_id") or "")[:16]
                # 来源：经 session-sidecar 恒注入 pool_src=sidecar → 本机/DSH 流量；
                # 外网经 nginx 注入 X-Session-Id（ext- 前缀）→ metadata.session_id，无 pool_src → ext
                src = "local" if str(_md.get("pool_src") or "") == "sidecar" else "ext"
            except Exception:
                sess, src, _md = "", "ext", {}
            # ---- 2026-10-24 流式观测：TTFT / 首可见 token / chunk 数 / 解码跨度 / tps ----
            ttft_ms = ttfv_ms = chunks = gen_ms = None
            tps = None
            hook = "miss"
            t0_rec = _ts_ms(start_time)      # 请求开始（litellm 给的 start_time，epoch ms）
            try:
                _k = _call_key(kwargs) or _call_key({"metadata": _md})
                m = _stream_metrics(_k)
                if m is not None:
                    hook = "hit"
                    first_w, first_v_w, last_w, chunks = m
                    if first_w and t0_rec:
                        ttft_ms = max(0, int(first_w * 1000) - t0_rec)
                    if first_v_w and t0_rec:
                        ttfv_ms = max(0, int(first_v_w * 1000) - t0_rec)
                    if first_w and last_w and last_w >= first_w:
                        gen_ms = int((last_w - first_w) * 1000)
                    if ct and gen_ms and gen_ms >= 50:
                        tps = round(ct / (gen_ms / 1000.0), 1)
            except Exception:  # noqa: BLE001
                pass
            rec = {
                "sess": sess,
                "src": src,
                "ts": _ts_ms(end_time),
                "t0": t0_rec,
                "model": kwargs.get("model", ""),
                "key": str(key)[-20:],
                "keyRef": (str(key)[-24:] if key else ""),   # 只留末 24 位：看板按尾缀映射，不落完整密钥
                "pt": pt, "ct": ct, "tt": tt,
                "cached": cached,
                # 流式观测字段（老读者忽略未知字段；缺省 null）
                "ttft_ms": ttft_ms,
                "ttfv_ms": ttfv_ms,
                "chunks": chunks,
                "gen_ms": gen_ms,
                "tps": tps,
                "hook": hook,
                "via": via,
            }
            # ---- P3 观测面字段（FINAL §6.1-P3：share_want/key_want/sess_class/repin_epoch/est_cost）
            # 从 metadata 读（sidecar 决策注入）；缺省 null/""；
            # 老字段与老读者绝不破坏、本块失败静默（绝不影响主请求流程）
            try:
                def _num(v, cast):
                    if v is None or v == "":
                        return None
                    try:
                        return cast(v)
                    except Exception:
                        return None
                rec["share_want"] = _num(_md.get("share_want"), float)
                rec["key_want"] = (str(_md.get("key_want")) if _md.get("key_want") not in (None, "") else None)
                rec["sess_class"] = str(_md.get("sess_class") or _md.get("x-session-class") or "")
                rec["repin_epoch"] = _num(_md.get("repin_epoch"), int)
                rec["est_cost"] = _num(_md.get("est_cost"), float)
            except Exception:
                rec.setdefault("share_want", None)
                rec.setdefault("key_want", None)
                rec.setdefault("sess_class", "")
                rec.setdefault("repin_epoch", None)
                rec.setdefault("est_cost", None)
            _append_log(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            _dbg("record")

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        # 写文件放后台线程，绝不在 LiteLLM 事件循环里做阻塞 IO
        threading.Thread(target=self._record,
                         args=(kwargs, start_time, end_time, response_obj, "sync"),
                         daemon=True).start()

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        # 2026-10-24 修：这里原来把实参顺序写反成 (kwargs, response_obj, start_time, end_time)
        # → _record 收到 start_time=ModelResponse，`start_time.timestamp()` 抛 AttributeError，
        # 被外层 fail-silent 吞掉 ⇒ 这条回调路径一条记录都写不出来（日志"断流"的真实原因之一）。
        # 现在与 log_success_event 同一顺序，并由 _claim_once 去重，保证「一请求一条记录」。
        await asyncio.to_thread(self._record, kwargs, start_time, end_time, response_obj, "async")


pool_logger = PoolLogger()
