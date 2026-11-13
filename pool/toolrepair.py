# -*- coding: utf-8 -*-
"""toolrepair - 号池响应侧畸形 tool_call 修复核心（2026-10-11）。

上游号（OpenCode Go 订阅通道）偶发吐出畸形 function call：tool_call 的 id/name 为空串。
空 id 会让下游 DSH 的会话格式校验在追加 tool/result 时抛 SessionFormatError
（"format v4 tool/result ... requires toolCallId matching its tool source"），
该会话之后每条消息都崩，属会话级损坏。

修复原则（无 I/O、无副作用；fail-open）：
  - 只动 id/name 为空的 tool_calls；正常调用一个字节不变；
  - id 空 -> 合成 call_<24hex>（OpenAI 形制）；
  - name 空且 arguments 呈「工具名键 + <parameter=...> XML 方言 / 嵌套对象」
    -> 确定性还原为正常 function call；还原不了 -> 原样放行（下游按普通工具
    错误处理，不再损坏会话）。
接线见 session-sidecar.py 的 repair_sse / repair_json 两个分支。
"""
import json
import re
import secrets

_SYNTH_ID_PREFIX = "call_"
_PARAM_ITEM_RE = re.compile(r"<parameter=([A-Za-z0-9_\-]+)>")
_END_TAG = "<" + chr(47) + "parameter>"


def _synth_call_id():
    return _SYNTH_ID_PREFIX + secrets.token_hex(12)


def _unwrap_dialect_arguments(arguments):
    try:
        obj = json.loads(arguments)
    except Exception:
        return None, None
    if not isinstance(obj, dict) or not obj:
        return None, None
    if len(obj) == 1:
        name, value = next(iter(obj.items()))
        if isinstance(name, str) and name and isinstance(value, dict):
            return name, json.dumps(value, ensure_ascii=False)
    dialect = [(k, v) for k, v in obj.items()
               if isinstance(v, str) and _PARAM_ITEM_RE.search(v)]
    if len(dialect) != 1:
        return None, None
    name, value = dialect[0]
    if not isinstance(name, str) or not name:
        return None, None
    parts = _PARAM_ITEM_RE.split(value)
    params = {}
    for i in range(1, len(parts) - 1, 2):
        key = parts[i]
        body = parts[i + 1]
        if body.endswith(_END_TAG):
            body = body[:-len(_END_TAG)]
        params[key] = body
    for k, v in obj.items():
        if k != name and k not in params:
            params[k] = v
    if not params:
        return None, None
    return name, json.dumps(params, ensure_ascii=False)


def repair_tool_call(tc):
    """就地修复单个 tool_call dict。返回 'id'|'name'|None。"""
    if not isinstance(tc, dict):
        return None
    fn = tc.get("function")
    if not isinstance(fn, dict):
        fn = {}
        tc["function"] = fn
    fixed = None
    cid = tc.get("id")
    if not isinstance(cid, str) or not cid:
        tc["id"] = _synth_call_id()
        fixed = "id"
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        args = fn.get("arguments")
        args = args if isinstance(args, str) else ""
        implied, norm = _unwrap_dialect_arguments(args)
        if implied:
            fn["name"] = implied
            fn["arguments"] = norm
            fixed = fixed or "name"
    return fixed


def repair_tool_call_list(tool_calls):
    n = 0
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if repair_tool_call(tc):
                n += 1
    return n


def repair_json_tool_calls(raw):
    """非流式 JSON 响应整体修复。返回 (输出字节, 修复数, 名字未还原数)。"""
    try:
        d = json.loads(raw)
    except Exception:
        return raw, 0, 0
    n = 0
    lost = 0
    try:
        for choice in (d.get("choices") or []):
            msg = (choice or {}).get("message") or {}
            tcs = msg.get("tool_calls")
            if not isinstance(tcs, list):
                continue
            n += repair_tool_call_list(tcs)
            for tc in tcs:
                fn = tc.get("function") if isinstance(tc, dict) else None
                if isinstance(fn, dict) and not fn.get("name"):
                    lost += 1
        if not n:
            return raw, 0, 0
        return json.dumps(d, ensure_ascii=False).encode("utf-8"), n, lost
    except Exception:
        return raw, 0, 0


class StreamToolState:
    """按 tool_call index 缓冲坏帧；正常 index 直通。"""

    def __init__(self):
        self.pending = {}
        self.live = set()


def _frame_tool_calls(d):
    try:
        choices = d.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        delta = (choices[0] or {}).get("delta")
        if not isinstance(delta, dict):
            return None
        tc = delta.get("tool_calls")
        return tc if isinstance(tc, list) else None
    except Exception:
        return None


def finish_repair_frames(state):
    """block 结束补发。返回 (行列表, 修复数, 名字未还原数)。"""
    out = []
    n = 0
    lost = 0
    for idx in sorted(state.pending):
        p = state.pending.pop(idx)
        tc = {"index": idx, "type": "function",
              "id": p["id"] or _synth_call_id(),
              "function": {"name": p["name"], "arguments": p["args"]}}
        if repair_tool_call(tc):
            n += 1
        if not tc["function"].get("name"):
            lost += 1
            continue
        frame = {"choices": [{"index": 0, "delta": {"tool_calls": [tc]},
                              "finish_reason": None}], "object": "chat.completion.chunk"}
        out.append(b"data: " + json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n")
    return out, n, lost


def process_sse_line(raw, state):
    """处理一行 SSE。返回 (转发行列表, 修复数, 名字未还原数)。fail-open。"""
    try:
        s = raw.decode("utf-8", "replace")
        if not s.startswith("data:"):
            return [raw], 0, 0
        payload = s[5:].strip()
        if payload == "[DONE]":
            out, n, lost = finish_repair_frames(state)
            out.append(raw)
            return out, n, lost
        d = json.loads(payload)
        if not isinstance(d, dict):
            return [raw], 0, 0
        tool_calls = _frame_tool_calls(d)
        if tool_calls is None:
            return [raw], 0, 0
        hold = False
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index", 0)
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            bad = (not isinstance(tc.get("id"), str) or not tc.get("id")
                   or not isinstance(fn.get("name"), str) or not fn.get("name"))
            if bad and idx not in state.live:
                p = state.pending.setdefault(idx, {"id": "", "name": "", "args": ""})
                if isinstance(tc.get("id"), str) and tc.get("id"):
                    p["id"] = tc["id"]
                if isinstance(fn.get("name"), str) and fn.get("name"):
                    p["name"] = fn["name"]
                if isinstance(fn.get("arguments"), str):
                    p["args"] += fn["arguments"]
                hold = True
            elif not bad:
                state.live.add(idx)
                state.pending.pop(idx, None)
        if not hold:
            return [raw], 0, 0
        try:
            delta = d["choices"][0]["delta"]
            delta.pop("tool_calls", None)
            if any(delta.values()):
                return [b"data: " + json.dumps(d, ensure_ascii=False).encode("utf-8") + b"\n"], 1, 0
            return [], 1, 0
        except Exception:
            return [], 1, 0
    except Exception:
        return [raw], 0, 0
