#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S2 补丁：让 gen_config.py 产出带 deployment tags 的配置，并支持「只写配置不重启」。

三处改动
  ① 每个 deployment 增加 litellm_params.tags: ["acct:<OCG_KEY_N>"]
  ② router_settings 增加 enable_tag_filtering: true（标签路由：把请求收窄到某个账号的 deployment）
  ③ 新增环境变量开关 POOL_SKIP_RESTART=1 → 只写 config.yaml，不执行 docker restart litellm
     （受控上线用：先落盘、在临时实例上验证 tag 语义、确认无误后再自己重启）

回滚
  cp -p /opt/litellm/gen_config.py.bak-treepin-s2-* /opt/litellm/gen_config.py
  cp -p /opt/litellm/config.yaml.bak-guard /opt/litellm/config.yaml   # 如需回到无 tags 配置
"""
import shutil
import sys
import time

PATH = "/opt/litellm/gen_config.py"
MARK = "treepin-s2"

OLD_DEP = '''        "      extra_headers:",
        f"        x-opencode-session: litellm-ocg-{acct_idx}",
    ]'''

NEW_DEP = '''        "      extra_headers:",
        f"        x-opencode-session: litellm-ocg-{acct_idx}",
        # treepin-s2: 账号标签。sidecar 在 mode=full 时注入 x-litellm-tags: acct:OCG_KEY_N，
        # 配合 enable_tag_filtering 把整棵会话树收窄到该账号的 deployment（客户端可见模型名不变）。
        "      tags:",
        f'        - "acct:OCG_KEY_{acct_idx}"',
    ]'''

OLD_ROUTER = '''    "  routing_strategy: least-busy",'''

NEW_ROUTER = '''    "  routing_strategy: least-busy",
    # treepin-s2（2026-10-12）:启用标签路由，供「会话树钉号」把请求收窄到指定账号；
    # 未带标签的请求行为不变（仍走 least-busy）。回滚：删掉本行 + 重跑本脚本 + 重启 litellm。
    "  enable_tag_filtering: true",'''

OLD_RESTART = '''r = subprocess.run(["docker", "restart", "litellm"], capture_output=True, text=True, timeout=60)
print("docker restart litellm:", (r.stdout or r.stderr).strip())'''

NEW_RESTART = '''# treepin-s2: POOL_SKIP_RESTART=1 → 只写配置不重启（受控上线/语义验证用）
if os.environ.get("POOL_SKIP_RESTART") == "1":
    print("POOL_SKIP_RESTART=1: config.yaml written, docker restart skipped")
else:
    r = subprocess.run(["docker", "restart", "litellm"], capture_output=True, text=True, timeout=60)
    print("docker restart litellm:", (r.stdout or r.stderr).strip())'''


def main():
    src = open(PATH, encoding="utf-8").read()
    if MARK in src:
        print("ALREADY-PATCHED (%s)" % MARK)
        return 0
    for name, anchor in (("dep", OLD_DEP), ("router", OLD_ROUTER), ("restart", OLD_RESTART)):
        n = src.count(anchor)
        if n != 1:
            print("ABORT: anchor %s found %d times (expected 1)" % (name, n))
            return 2
    bak = "%s.bak-treepin-s2-%s" % (PATH, time.strftime("%Y%m%d-%H%M"))
    shutil.copy2(PATH, bak)
    out = src.replace(OLD_DEP, NEW_DEP, 1).replace(OLD_ROUTER, NEW_ROUTER, 1).replace(OLD_RESTART, NEW_RESTART, 1)
    if out.count(MARK) < 3:
        print("ABORT: unexpected result (mark count %d)" % out.count(MARK))
        return 3
    with open(PATH, "w", encoding="utf-8") as fh:
        fh.write(out)
    import py_compile
    try:
        py_compile.compile(PATH, doraise=True)
    except Exception as e:
        shutil.copy2(bak, PATH)
        print("py_compile FAILED -> rolled back: %s" % e)
        return 4
    print("PATCHED %s" % PATH)
    print("backup  %s" % bak)
    print("py_compile OK | mark count %d" % out.count(MARK))
    return 0


if __name__ == "__main__":
    sys.exit(main())
