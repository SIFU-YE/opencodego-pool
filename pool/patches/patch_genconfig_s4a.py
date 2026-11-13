#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4-a：gen_config 生成「池内单账号别名组」，并移除不可靠的 tags/enable_tag_filtering。

为什么
    实测（2026-10-12）main-stable 1.102.1 的 tag_based_routing 部署标签命中极不可靠
    （同模型 4 个账号标签只有某一个能命中）→ 标签方案作废。
    改为「每个账号一个单 deployment 的别名模型组」：命中确定性 = 100%。
    别名只存在于号池内部：sidecar 按会话树键把请求体的 model 改写成 <model>@a<N>，
    而 GET /v1/models 的过滤（keep=5 个正式名）天然不会暴露别名。

产出
    · 5 模型 × N 健康账号 的别名组（如 mimo-v2.6-flash@a5 → 仅 OCG_KEY_5 一个 deployment）
    · 正式模型组的 tags 与 router_settings.enable_tag_filtering 一并移除（回归朴素 least-busy）

回滚
    cp -p /opt/litellm/gen_config.py.bak-treepin-s4a-* /opt/litellm/gen_config.py
    再跑一次生成器并重启 litellm
"""
import shutil
import sys
import time

PATH = "/opt/litellm/gen_config.py"
MARK = "treepin-s4a"

OLD_TAGS = '''        # treepin-s2: 账号标签。sidecar 在 mode=full 时注入 x-litellm-tags: acct:OCG_KEY_N，
        # 配合 enable_tag_filtering 把整棵会话树收窄到该账号的 deployment（客户端可见模型名不变）。
        "      tags:",
        f'        - "acct:OCG_KEY_{acct_idx}"',
    ]'''
NEW_TAGS = '''    ]


def alias_block(model_name, acct_idx):
    """treepin-s4a：单账号别名组 —— 一个组恰好一个 deployment，命中确定性 100%。

    该别名仅供号池内部使用：sidecar 按会话树键把请求 model 改写为 <model>@a<N>，
    客户端（含 GET /v1/models）永远看不到别名。
    """
    return [
        f"  - model_name: {model_name}@a{acct_idx}",
        "    litellm_params:",
        f"      model: openai/{model_name}",
        "      api_base: https://opencode.ai/zen/go/v1",
        f"      api_key: os.environ/OCG_KEY_{acct_idx}",
        "      extra_headers:",
        f"        x-opencode-session: litellm-ocg-{acct_idx}",
    ]'''

OLD_ROUTER = '''    # treepin-s2（2026-10-12）:启用标签路由，供「会话树钉号」把请求收窄到指定账号；
    # 未带标签的请求行为不变（仍走 least-busy）。回滚：删掉本行 + 重跑本脚本 + 重启 litellm。
    "  enable_tag_filtering: true",
'''

NEW_LOOP = '''for m in models:
    for kid in healthy:
        out += alias_block(m, healthy_idx[kid])
for alias, (upstream, acct) in PINNED_ALIASES.items():'''

OLD_LOOP = '''for alias, (upstream, acct) in PINNED_ALIASES.items():'''


def main():
    src = open(PATH, encoding="utf-8").read()
    if MARK in src:
        print("ALREADY-PATCHED (%s)" % MARK)
        return 0
    if "treepin-s2" not in src:
        print("ABORT: 未找到 S2 补丁痕迹")
        return 2
    for name, anchor in (("tags", OLD_TAGS), ("router", OLD_ROUTER), ("loop", OLD_LOOP)):
        n = src.count(anchor)
        if n != 1:
            print("ABORT: anchor %s found %d times (expected 1)" % (name, n))
            return 2
    bak = "%s.bak-treepin-s4a-%s" % (PATH, time.strftime("%Y%m%d-%H%M"))
    shutil.copy2(PATH, bak)
    out = src.replace(OLD_TAGS, NEW_TAGS, 1).replace(OLD_ROUTER, "", 1).replace(OLD_LOOP, NEW_LOOP, 1)
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
    print("py_compile OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
