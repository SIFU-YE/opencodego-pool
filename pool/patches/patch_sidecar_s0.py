#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S0 补丁：给号池 sidecar 增加「会话树键」识别与影子日志（不改任何路由行为）。

背景
    DSH 侧插件 @local/dsh-pool-affinity 在进入模型的消息末尾追加稳定标记
        [pool-tree: <根会话 id>]
    本补丁让 sidecar 把它当作最高优先的会话锚（缺失时行为与改动前逐字一致），
    并写一份影子日志 /opt/litellm/pool-tree-shadow.jsonl 供验收查询。

回滚
    cp -p /opt/litellm/session-sidecar.py.bak-treepin-<ts> /opt/litellm/session-sidecar.py
    systemctl restart litellm-session-sidecar
"""
import os
import re
import shutil
import sys
import time

PATH = "/opt/litellm/session-sidecar.py"
MARK = "treepin-s0"  # 幂等标记

HELPERS = '''
# ================================================================ 会话树键 tree key（2026-10-12 treepin-s0）
# DSH 侧插件 @local/dsh-pool-affinity 在「进入模型的消息」末尾追加稳定标记：
#     [pool-tree: <根会话 id>]
# 目的：一个会话里启动的团队（队长 + 全部子代理）共享同一锚 → 号池可把整棵树钉到同一账号。
# 优先级：树键 > 客户端会话头（非 nginx 随机兜底）> body metadata.session_id > 首条消息哈希。
# 标记缺失时，本文件的行为与改动前逐字一致（fail-open）。
_TREE_RE = re.compile(rb"\\[pool-tree:[ \\t]*([A-Za-z0-9_-]{4,64})\\]")
TREE_SHADOW_FILE = "/opt/litellm/pool-tree-shadow.jsonl"
_TREE_LOG_SEEN = {}
_TREE_LOG_LOCK = threading.Lock()


def tree_key(body_bytes):
    """从请求体里取会话树键；纯字节级正则，绝不改动 messages。"""
    try:
        m = _TREE_RE.search(body_bytes or b"")
        return m.group(1).decode("ascii") if m else None
    except Exception:
        return None


def note_tree(tree, model):
    """S0 影子：每棵树每 60 s 最多记一条（只写文件 + 一行日志），任何异常静默。"""
    try:
        if not tree:
            return
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
                                 "mode": "shadow"}, ensure_ascii=False) + "\\n")
        slog("tree-shadow tree=%s model=%s" % (tree, model))
    except Exception:
        pass


'''

OLD_PRIORITY = '''        body_sid = md.get("session_id")
        fallback = bool(inbound_sid) and bool(_FALLBACK_SID_RE.match(str(inbound_sid)))
        h = _first_text_hash(d.get("messages"))
        if inbound_sid and not fallback:'''

NEW_PRIORITY = '''        body_sid = md.get("session_id")
        fallback = bool(inbound_sid) and bool(_FALLBACK_SID_RE.match(str(inbound_sid)))
        h = _first_text_hash(d.get("messages"))
        tree = tree_key(body_bytes)                  # treepin-s0：DSH 插件注入的会话树键
        if tree:
            final = tree[:64]                        # 最高优先：整棵会话树共享同一锚
        elif inbound_sid and not fallback:'''

OLD_RELAY = '''                out, key = process_request(body, self.headers.get("X-Session-Id"))
                final_sid = key
                if key:
                    sess = key
                if out is not None:
                    send_body = out'''

NEW_RELAY = '''                out, key = process_request(body, self.headers.get("X-Session-Id"))
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
                    note_tree(_tree, _model)'''


def main():
    src = open(PATH, encoding="utf-8").read()
    if MARK in src:
        print("ALREADY-PATCHED (mark %s present); nothing to do" % MARK)
        return 0
    for name, anchor in (("priority", OLD_PRIORITY), ("relay", OLD_RELAY), ("helpers", "def baseline_session(")):
        n = src.count(anchor)
        if n != 1:
            print("ABORT: anchor %s found %d times (expected 1)" % (name, n))
            return 2
    bak = "%s.bak-treepin-%s" % (PATH, time.strftime("%Y%m%d-%H%M"))
    shutil.copy2(PATH, bak)

    out = src.replace("def baseline_session(", HELPERS.lstrip("\n") + "def baseline_session(", 1)
    out = out.replace(OLD_PRIORITY, NEW_PRIORITY, 1)
    out = out.replace(OLD_RELAY, NEW_RELAY, 1)
    if out.count(MARK) < 3:
        print("ABORT: unexpected patch result (mark count %d)" % out.count(MARK))
        return 3
    with open(PATH, "w", encoding="utf-8") as fh:
        fh.write(out)
    print("PATCHED %s" % PATH)
    print("backup  %s" % bak)
    print("mark count in new file: %d" % out.count(MARK))
    # 语法自检
    import py_compile
    try:
        py_compile.compile(PATH, doraise=True)
        print("py_compile OK")
    except Exception as e:
        shutil.copy2(bak, PATH)
        print("py_compile FAILED -> rolled back: %s" % e)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
