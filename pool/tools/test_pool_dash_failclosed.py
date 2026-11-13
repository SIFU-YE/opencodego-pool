#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：pool-dash/gen_config.py 必须默认 fail-closed。

它是一份与服务端脱节的副本（无模型白名单、路由设置不同）。若被误跑，会覆盖生产
config.yaml 并 docker restart litellm，连带清空会话亲和与前缀缓存。因此它默认
必须拒绝执行（exit 3），且拒绝动作要发生在任何网络/写盘之前。

断言：
  1) 不带 POOL_DASH_ALLOW_APPLY 运行 → exit 3 且 stderr 含 REFUSING TO RUN；
  2) POOL_DASH_ALLOW_APPLY=1     → 不再被闸门拦住（会继续走真实流程）；
  3) POOL_DASH_ALLOW_APPLY=print → 同样越过闸门（只出产物模式）。

用法：python3 pool/tools/test_pool_dash_failclosed.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.abspath(os.environ.get(
    "POOL_DASH_GEN_CONFIG", os.path.join(HERE, "..", "..", "pool-dash", "gen_config.py")))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  :: " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


def run(allow):
    env = dict(os.environ)
    env.pop("POOL_DASH_ALLOW_APPLY", None)
    if allow is not None:
        env["POOL_DASH_ALLOW_APPLY"] = allow
    p = subprocess.run([sys.executable, SCRIPT], cwd=os.path.dirname(SCRIPT),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


if not os.path.exists(SCRIPT):
    print("SKIP: %s 不存在" % SCRIPT)
    sys.exit(0)

print("被测脚本: %s" % SCRIPT)
print("（本机无 /opt/litellm/.env，放行后必然在读取环境文件时报错——这正好用来证明闸门之外没有隐藏副作用）")

rc, out, err = run(None)
ok("默认运行 → exit 3", rc == 3, "rc=%d" % rc)
ok("默认运行 → 打印 REFUSING TO RUN", "REFUSING TO RUN" in err, err.strip().splitlines()[0] if err.strip() else "(无 stderr)")
ok("默认运行 → 不产出任何配置内容", "model_list:" not in out, "stdout=%d bytes" % len(out))

rc1, out1, err1 = run("1")
ok("ALLOW_APPLY=1 → 不再被闸门拦住", "REFUSING TO RUN" not in err1, "rc=%d" % rc1)

rcp, outp, errp = run("print")
ok("ALLOW_APPLY=print → 同样越过闸门", "REFUSING TO RUN" not in errp, "rc=%d" % rcp)

print()
if fails:
    print("FAILED %d: %s" % (len(fails), fails))
    sys.exit(1)
print("ALL PASS")
