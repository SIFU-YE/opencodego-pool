#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSE 到达颗粒度 / 吞吐探针（号池排障用，只读探测，不改任何配置）

用法（部署机）:
    python3 sse_probe.py <target> [runs]

target:
    up1 .. up6   直连上游 opencode.ai（用 OCG_KEY_n，走 sing-box 代理，与 litellm 同路径）
    ll           litellm :4000（跳过 sidecar）
    sc           session-sidecar :4001（完整号池链路）
    pub          https://yesifu.site/works/pool/v1（再加 nginx + 公网）
  任意 target 加后缀 `+nt` = 请求不带 tools（sidecar 走纯透传而非 toolrepair 重写分支）

指标:
    hdr_ms      等到响应头
    first_ms    首个内容 token 到达（相对请求发出）
    dur         首 token → 末 token 的生成跨度（t/s 分母）
    tps         usage.completion_tokens / dur
    cps         chars / dur
    reads       非空 socket read 次数        bytes/read  每次 read 平均字节
    ev/read     每次 read 里平均/最大 SSE 事件数（>1 说明被合批）
    batched     「一次 read 拿到 >=2 个内容事件」的占比 —— 颗粒度粗的直接证据
    gap p50/p90/max  相邻内容事件到达间隔
"""
import json
import os
import socket
import ssl
import statistics
import sys
import time
import http.client

API = "/v1/chat/completions"
UP_HOST = "opencode.ai"
UP_PATH = "/inference/go/openai/v1/chat/completions"
PROXY = ("127.0.0.1", 10808)

SYSTEM = "你是一个中文技术写手，回答要连贯、具体。"
USER = ("请用中文详细解释 TCP 慢启动的全过程，约 600 字，分点说明，"
        "包含拥塞窗口、ssthresh、丢包后的处理，不要使用 Markdown 标题。")

TOOLS = [{
    "type": "function",
    "function": {
        "name": "ping",
        "description": "check connectivity of a host",
        "parameters": {"type": "object",
                       "properties": {"host": {"type": "string"}},
                       "required": ["host"]},
    },
}]


def make_body(with_tools=True, max_tokens=None):
    model = os.environ.get("PROBE_MODEL", "deepseek-v4.1-flash")
    if max_tokens is None:
        max_tokens = int(os.environ.get("PROBE_MAXTOK", "520"))
    # PROBE_PROMPT_FILE：用一段固定长文本当 user 消息（前缀缓存/A-B 实验必须逐字节相同）
    user = USER
    pfile = os.environ.get("PROBE_PROMPT_FILE")
    if pfile:
        with open(pfile, "r", encoding="utf-8") as f:
            user = f.read()
    # PROBE_TREE：在正文末尾注入 sidecar 能识别的「会话树键」零宽载荷，
    # 用来在生产路径上真实复现 tree/turn 轮换（PROBE_TURN_NONCE 变更"本轮"指纹）。
    tree = os.environ.get("PROBE_TREE")
    if tree:
        try:
            from zw_tree_key import encode_tree_key
            nonce = os.environ.get("PROBE_TURN_NONCE", "")
            user = user + (("\n[nonce %s]" % nonce) if nonce else "") + encode_tree_key(tree)
        except Exception as e:  # noqa: BLE001
            print("WARN: 树键注入失败: %s" % e, file=sys.stderr)
    b = {
        "model": model,
        "stream": True,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
    }
    if os.environ.get("PROBE_USAGE", "1") == "1":
        b["stream_options"] = {"include_usage": True}
    if with_tools:
        b["tools"] = TOOLS
        b["tool_choice"] = "auto"
    return b


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[int(round((len(xs) - 1) * p))]


def tunnel(host, port):
    """经 sing-box 代理建 CONNECT 隧道并做 TLS（与 litellm 容器同一出网路径）。"""
    s = socket.create_connection(PROXY, timeout=30)
    s.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
               % (host, port, host, port)).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = s.recv(4096)
        if not d:
            raise IOError("proxy closed during CONNECT")
        buf += d
    if b" 200 " not in buf.split(b"\r\n")[0]:
        raise IOError("proxy CONNECT failed: %r" % buf[:120])
    ctx = ssl.create_default_context()
    return ctx.wrap_socket(s, server_hostname=host)


class TlsConn(http.client.HTTPConnection):
    """把已建好的 TLS socket 塞给 http.client（复用其响应解析与 read1）。"""

    def __init__(self, sock, timeout):
        http.client.HTTPConnection.__init__(self, UP_HOST, 443, timeout=timeout)
        self.sock = sock

    def connect(self):
        pass


def tls_direct(host, port):
    """不经 sing-box，直连上游（用于区分「上游 batching」与「出口代理 batching」）。"""
    ctx = ssl.create_default_context()
    s = socket.create_connection((host, port), timeout=30)
    return ctx.wrap_socket(s, server_hostname=host)


def target_of(name):
    """返回 (label, connect_fn, path, headers, with_tools)

    后缀可叠加：+nt = 不带 tools；+np = 绕过 sing-box 直连上游
    """
    opts = set()
    base = name
    while True:
        if base.endswith("+nt"):
            opts.add("nt"); base = base[:-3]
        elif base.endswith("+np"):
            opts.add("np"); base = base[:-3]
        else:
            break
    with_tools = "nt" not in opts
    master = os.environ.get("PROBE_KEY") or os.environ.get("LITELLM_MASTER_KEY", "")
    hdr = {"Content-Type": "application/json", "Accept": "text/event-stream"}

    if base.startswith("up"):
        n = base[2:]
        key = os.environ["OCG_KEY_" + n]
        h = dict(hdr)
        h["Authorization"] = "Bearer " + key
        h["User-Agent"] = "dsh-opencodego-pool/1.0"
        _sess = os.environ.get("PROBE_SESSION")
        if _sess == "-" or _sess == "none":
            pass                      # 明确不发 session 头（普通 OpenAI 客户端的行为）
        else:
            h["x-opencode-session"] = _sess or ("probe-" + time.strftime("%H%M%S"))
        if "np" in opts:
            return ("upstream-" + n + "-direct", lambda: TlsConn(tls_direct(UP_HOST, 443), 180),
                    UP_PATH, h, with_tools)
        return ("upstream-" + n + "-viaproxy", lambda: TlsConn(tunnel(UP_HOST, 443), 180),
                UP_PATH, h, with_tools)
    if base in ("ll", "sc", "pub"):
        h = dict(hdr)
        h["Authorization"] = "Bearer " + master
        # 本地/公网这几跳默认不带 x-opencode-session；显式给 PROBE_SESSION 时加上，
        # 用于验证「客户端头能否穿透 litellm 到上游」（需 litellm 开 forward_client_headers_to_llm_api）
        _s = os.environ.get("PROBE_SESSION")
        if _s and _s not in ("-", "none"):
            h["x-opencode-session"] = _s
    if base == "ll":
        return ("litellm:4000", lambda: http.client.HTTPConnection("127.0.0.1", 4000, timeout=180),
                API, h, with_tools)
    if base == "sc":
        return ("sidecar:4001", lambda: http.client.HTTPConnection("127.0.0.1", 4001, timeout=180),
                API, h, with_tools)
    if base == "pub":
        return ("public", lambda: http.client.HTTPSConnection("yesifu.site", 443, timeout=180),
                "/works/pool/v1/chat/completions", h, with_tools)
    raise SystemExit("未知 target: " + name)


def one_run(target):
    label, mkconn, path, hdrs, with_tools = target_of(target)
    payload = json.dumps(make_body(with_tools)).encode()
    conn = mkconn()
    t0 = time.monotonic()
    conn.request("POST", path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    t_hdr = time.monotonic() - t0
    meta = {
        "status": resp.status,
        "ctype": resp.getheader("Content-Type"),
        "te": resp.getheader("Transfer-Encoding"),
        "ce": resp.getheader("Content-Encoding"),
        "cl": resp.getheader("Content-Length"),
    }
    reads = []
    events = []
    usage = None
    buf = b""
    while True:
        c = resp.read1(65536)
        if not c:
            break
        t = time.monotonic() - t0
        reads.append((t, len(c)))
        buf += c
        while True:
            i = buf.find(b"\n\n")
            if i < 0:
                break
            raw, buf = buf[:i], buf[i + 2:]
            for line in raw.split(b"\n"):
                if not line.startswith(b"data:"):
                    continue
                p = line[5:].strip()
                if p == b"[DONE]":
                    events.append((t, "done", 0))
                    continue
                try:
                    d = json.loads(p)
                except Exception:
                    continue
                if isinstance(d.get("usage"), dict):
                    usage = d["usage"]
                ch = (d.get("choices") or [{}])[0]
                delta = ch.get("delta") or {}
                txt = delta.get("content") or ""
                rz = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if txt or rz:
                    events.append((t, "r" if (rz and not txt) else "t", len(txt) + len(rz)))
    conn.close()

    content = [e for e in events if e[1] in ("t", "r")]
    first = content[0][0] if content else None
    last = content[-1][0] if content else None
    dur = (last - first) if (first is not None and last is not None and last > first) else None
    chars = sum(e[2] for e in content)
    gaps = [(content[i][0] - content[i - 1][0]) * 1000 for i in range(1, len(content))]
    # 「同一瞬间到达」占比：>0 说明上游把多个事件攒成一批下发（颗粒度粗）
    instant = (sum(1 for g in gaps if g < 3.0) / float(len(gaps))) if gaps else None
    # 一次 read 里拿到几个内容事件：按 read 时间戳给事件分组
    per_read = {}
    for t, kind, n in content:
        per_read[t] = per_read.get(t, 0) + 1
    counts = list(per_read.values())
    batched = (sum(1 for c in counts if c >= 2) / float(len(counts))) if counts else None
    ct = None
    if isinstance(usage, dict):
        ct = usage.get("completion_tokens") or usage.get("total_tokens")
    # 每秒到达字符数：看速率是否随时间衰减（前段快后段慢）
    prof = {}
    for t, kind, n in content:
        b = int(t)
        prof[b] = prof.get(b, 0) + n
    profile = [prof.get(b, 0) for b in range(0, (max(prof) + 1) if prof else 1)]

    return {
        "target": label,
        "tools": with_tools,
        "status": meta["status"],
        "te": meta["te"] or meta["cl"],
        "ce": meta["ce"],
        "hdr_ms": round(t_hdr * 1000),
        "first_ms": round(first * 1000) if first else None,
        "dur_ms": round(dur * 1000) if dur else None,
        "chars": chars,
        "events": len(content),
        "cps": round(chars / dur, 1) if dur else None,
        "tokens": ct,
        "tps": round(ct / dur, 1) if (dur and ct) else None,
        "reads": len(reads),
        "bytes": sum(r[1] for r in reads),
        "bytes_per_read": round(sum(r[1] for r in reads) / len(reads)) if reads else None,
        "ev_per_read": round(statistics.mean(counts), 2) if counts else None,
        "ev_per_read_max": max(counts) if counts else None,
        "batched_ratio": round(batched, 3) if batched is not None else None,
        "gap_p50_ms": round(pct(gaps, 0.5)) if gaps else None,
        "gap_p90_ms": round(pct(gaps, 0.9)) if gaps else None,
        "gap_max_ms": round(max(gaps)) if gaps else None,
        # 前 25 个内容事件的到达间隔（ms），用于肉眼判断"一词一顿"还是"一段一段"
        "trace": [round(g) for g in gaps[:25]],
        # 相对首 token 的每秒字符数：看速率随时间是否衰减
        "profile": profile,
        # 用量/前缀缓存（判断"缓存是否与 session 绑定"用）
        "pt": (usage or {}).get("prompt_tokens") if isinstance(usage, dict) else None,
        "cached": (((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens")
                   if isinstance(usage, dict) else None),
    }


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "sc"
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    out = []
    for _ in range(runs):
        try:
            r = one_run(target)
        except Exception as e:  # noqa: BLE001
            r = {"target": target, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}
        print(json.dumps(r, ensure_ascii=False), flush=True)
        out.append(r)
    ok = [r for r in out if "error" not in r]
    if ok and len(ok) > 1:
        agg = {}
        for k in ("tps", "cps", "hdr_ms", "first_ms", "batched_ratio", "gap_p50_ms", "ev_per_read"):
            vals = [r[k] for r in ok if r.get(k) is not None]
            if vals:
                agg[k] = round(statistics.mean(vals), 2)
        print("SUMMARY " + json.dumps(agg, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
