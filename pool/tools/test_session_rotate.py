#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单测：sidecar 的 x-opencode-session 派生（2026-10-24 T-sess）。

不启动服务、不联网：直接按文件加载 session-sidecar.py 调纯函数。
跑法：  python3 pool/tools/test_session_rotate.py
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CANDIDATES = [
    os.environ.get("SIDECAR_PATH"),
    "/opt/litellm/session-sidecar.py",
    os.path.join(os.path.dirname(HERE), "session-sidecar.py"),
]
path = next((p for p in CANDIDATES if p and os.path.isfile(p)), None)
if not path:
    sys.exit("找不到 session-sidecar.py（可用 SIDECAR_PATH 指定）")
sys.path.insert(0, os.path.dirname(path))
spec = importlib.util.spec_from_file_location("sc", path)
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

TREE = "session-01f11419-98fd-4369-997a-e470278ed81d"
FAILS = []


def body(model="deepseek-v4.1-flash@a6s3", user="hello", extra=0):
    """extra = 同一用户轮次内追加的「assistant/tool 步」（不改最后一条 user 消息）。"""
    msgs = [{"role": "user", "content": user}]
    for i in range(extra):
        msgs.append({"role": "assistant", "content": "step %d" % i})
        msgs.append({"role": "tool", "content": "result %d" % i})
    return json.dumps({"model": model, "messages": msgs}, ensure_ascii=False).encode()


def check(name, got, want):
    ok = got == want
    print("%-58s %s" % (name, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        FAILS.append(name)


b = body()
bucket = sc.session_bucket(TREE)

# 1) 账号从已改写的 model 里取；兜底 model 取不到
check("acct_of_model(@a6s3) = 6", sc.acct_of_model(b), "6")
check("acct_of_model(兜底 model) = None", sc.acct_of_model(body(model="deepseek-v4.1-flash")), None)

# 2) off 档必须与旧静态头逐字一致（回滚安全的前提）
sc.SESSION_ROTATE = "off"
check("off: 有树 → dsh-acct-6-s3", sc.pool_session_id(TREE, "6", 3, b), "dsh-acct-6-s3")
check("off: 无树 → dsh-acct-6", sc.pool_session_id(None, "6", None, b), "dsh-acct-6")
check("off: 无账号 → None（走兜底部署）", sc.pool_session_id(TREE, None, 3, b), None)

# 3) tree 档：同树恒定、异树不同
sc.SESSION_ROTATE = "tree"
t1 = sc.pool_session_id(TREE, "6", bucket, b)
t2 = sc.pool_session_id(TREE, "6", bucket, body(extra=3))
other = sc.pool_session_id("session-ffffffff-ffff-ffff-ffff-ffffffffffff", "6", bucket, b)
check("tree: 同一棵树稳定（消息增长不换串）", t1, t2)
check("tree: 不同树不同串", t1 == other, False)
check("tree: 前缀带账号", t1.startswith("dsh-a6-"), True)

# 4) turn 档：同一用户轮次稳定，换一条 user 消息才换串
sc.SESSION_ROTATE = "turn"
u1 = sc.pool_session_id(TREE, "6", bucket, body(extra=0))
u2 = sc.pool_session_id(TREE, "6", bucket, body(extra=2))      # 同一条 user 消息，只是后续步数增加
u3 = sc.pool_session_id(TREE, "6", bucket, body(user="另一个问题"))
check("turn: 同一用户轮次内稳定（步数增长不换串）", u1, u2)
check("turn: 换用户消息才换串", u1 == u3, False)

# 5) request 档：任何 body 差异都换串
sc.SESSION_ROTATE = "request"
r1 = sc.pool_session_id(TREE, "6", bucket, body(extra=0))
r2 = sc.pool_session_id(TREE, "6", bucket, body(extra=1))
check("request: 每请求换串", r1 == r2, False)

# 6) 未知档位 → 退化成 tree 行为（不抛错）
sc.SESSION_ROTATE = "bogus"
check("未知档位退化为 tree", sc.pool_session_id(TREE, "6", bucket, b),
      sc.pool_session_id(TREE, "6", bucket, body(extra=5)))

# 7) 串长度固定，便于日志与上游处理
sc.SESSION_ROTATE = "turn"
check("派生串长度 = 19（dsh-a6- + 12 hex）", len(sc.pool_session_id(TREE, "6", bucket, b)), 19)

print()
if FAILS:
    print("FAILED: %d 项 -> %s" % (len(FAILS), FAILS))
    sys.exit(1)
print("ALL PASS")
