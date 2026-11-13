#!/usr/bin/env python3
"""给 sing-box 出口配置的每个 vless 出站补 `domain_strategy: ipv4_only`（幂等）。

背景（2026-10-16 事故）：本机无 IPv6 默认路由，而十个节点域名（jp-awsNN 的 A/AAAA 主机名）
同时有 A/AAAA 记录。sing-box 默认把解析结果原样交给 Go 拨号器，解析结果里一旦只剩
AAAA（A 查询抖动时解析器返回部分结果），拨号就地 ENETUNREACH：

    ERROR connection: open connection to opencode.ai:443 using outbound/vless[jp-aws01]:
      dial tcp [2406:da14:...]:443: connect: network is unreachable

客户端侧表现为 `litellm.InternalServerError: Connection error` → HTTP 500。
当天 743 次，且 TCP 探测看不到（节点端口是通的），于是坏节点长期占着 current。

用法：
    python3 patch_ipv4_only.py            # 打补丁（自动备份 + 原子替换 + 语法自检）
    python3 patch_ipv4_only.py --check    # 只报告，不改动；有需改动则退出码 1
"""
import json
import os
import shutil
import sys
import time

CFG = os.environ.get("POOL_EGRESS_CFG", "/opt/pool-egress/config.json")
KEY = "domain_strategy"
VAL = "ipv4_only"


def main():
    cfg = json.load(open(CFG, encoding="utf-8"))
    todo = []
    for ob in cfg.get("outbounds", []):
        if ob.get("type") != "vless":
            continue
        if ob.get(KEY) != VAL:
            todo.append(ob.get("tag"))

    print(f"vless outbounds: {sum(1 for o in cfg.get('outbounds', []) if o.get('type') == 'vless')}, "
          f"need {KEY}={VAL}: {len(todo)} {todo}")
    if not todo:
        return 0
    if "--check" in sys.argv:
        return 1

    for ob in cfg["outbounds"]:
        if ob.get("type") == "vless":
            ob[KEY] = VAL

    bak = f"{CFG}.bak-ipv4only-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(CFG, bak)
    tmp = CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1, ensure_ascii=False)
    json.load(open(tmp, encoding="utf-8"))  # 语法自检：写坏了就别替换
    os.chmod(tmp, 0o600)
    os.replace(tmp, CFG)
    print(f"patched {CFG}; backup={bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
