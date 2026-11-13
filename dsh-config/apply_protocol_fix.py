#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 侧协议修复：把号池模型按「上游实际支持的协议」拆到对应 provider。

背景：zen 的模型是协议隔离的（mimo 系只支持 chat/completions、muse-spark 只支持 responses、
claude-haiku-5-5 三种都不支持）。而 profile 里 `providers.litellm.api` 写死 openai-responses，
且 `api` 只在 provider 级存在（不支持按模型指定）→ 用 mimo 的 5 个角色一律 400。

模式：
  A（推荐）litellm 改为 openai-completions，放 mimo×2 + dsv4.1；新增 litellm-responses 放 muse-spark；
            reviewer 角色改指 litellm-responses/muse-spark-1.3-contributor；允许清单同步。
  B（最保守）现有 litellm 一字不动；新增 litellm-chat(openai-completions) 只放 mimo×2；
            qa/architect/dba/perf/sec 改指 litellm-chat/<mimo>；允许清单同步。

用法：
    python3 apply_protocol_fix.py --mode A --dry-run     # 只打印 diff，不改文件
    python3 apply_protocol_fix.py --mode A --apply       # 备份后落盘
回滚：每个被改文件同级生成 .bak-protofix-<时间戳>
"""
import argparse
import difflib
import os
import re
import shutil
import sys
import time

HOME = os.path.expanduser("~")
PATCH = os.path.join(HOME, ".dsh", "profiles", "desktop", "cordis.patch.yml")
ROLES = os.path.join(HOME, ".dsh", "roles")

MIMO = ["mimo-v2.6-flash", "mimo-v2.6-pro"]
MUSE = "muse-spark-1.3-contributor"
DSV41 = "deepseek-v4.1-flash"
HAIKU = "claude-haiku-5-5"

MIMO_ROLES = ["qa", "architect", "dba", "perf", "sec"]
MIMO_MODEL = {"qa": "mimo-v2.6-flash", "architect": "mimo-v2.6-pro",
              "dba": "mimo-v2.6-pro", "perf": "mimo-v2.6-pro", "sec": "mimo-v2.6-pro"}


def model_block(name, indent="          "):
    """按现有风格生成一个模型条目（缩进与 profile 中一致）。"""
    spec = {
        "muse-spark-1.3-contributor": (838860, 8192),
        "claude-haiku-5-5": (200000, 64000),
        "mimo-v2.6-flash": (1048576, 131072),
        "mimo-v2.6-pro": (1048576, 131072),
        "deepseek-v4.1-flash": (1000000, 131072),
    }[name]
    return (f"{indent}- id: {name}\n"
            f"{indent}  name: {name}\n"
            f"{indent}  contextWindow: {spec[0]}\n"
            f"{indent}  maxTokens: {spec[1]}\n"
            f"{indent}  input:\n"
            f"{indent}    - text\n"
            f"{indent}    - image\n")


def provider_block(name, api, models, display=None):
    lines = [f"      {name}:",
             f"        displayName: {display or name}",
             "        apiKeyEnv: LITELLM_API_KEY",
             f"        api: {api}",
             "        baseURL: https://<pool-host>/works/pool/v1",
             "        models:"]
    return "\n".join(lines) + "\n" + "".join(model_block(m) for m in models)


def extract_block(text, model, indent="          "):
    """取出某个模型条目的完整文本块。"""
    start = text.find(f"{indent}- id: {model}\n")
    if start < 0:
        return None, None, None
    nxt = text.find(f"{indent}- id: ", start + 1)
    end = nxt if nxt > 0 else len(text)
    return text[start:end], start, end


def set_allowlist(text, pairs):
    """替换 subagent-model-selection-settings.allowedModels 列表。"""
    m = re.search(r"(?ms)^(\s*)allowedModels:\n((?:\s*-\s*provider:.*\n\s*model:.*\n)+)", text)
    if not m:
        return text, False
    indent = m.group(1)
    body = "".join(f"{indent}  - provider: {p}\n{indent}    model: {mm}\n" for p, mm in pairs)
    return text[:m.start()] + f"{indent}allowedModels:\n{body}" + text[m.end():], True


def set_role(text, provider, model):
    new = re.sub(r"(?m)^provider: .*$", f"provider: {provider}", text)
    new = re.sub(r"(?m)^model: .*$", f"model: {model}", new)
    return new, (new != text)


def plan_mode_a(patch):
    """litellm → completions（去掉 muse/haiku），新增 litellm-responses 放 muse。"""
    muse_block, s_muse, e_muse = extract_block(patch, MUSE)
    haiku_block, s_haiku, e_haiku = extract_block(patch, HAIKU)
    if muse_block is None or haiku_block is None:
        raise SystemExit("ABORT: 找不到 muse/haiku 模型块")
    if not (s_muse < s_haiku):
        raise SystemExit("ABORT: muse 与 haiku 块顺序异常")
    out = patch[:s_muse] + patch[e_muse:]                      # 只删 muse 块：haiku 保留在 completions 列表里（选择器不丢模型）
    out, n = re.subn(r"(?m)^(        api: )openai-responses$", r"\1openai-completions", out, count=1)
    if n != 1:
        raise SystemExit("ABORT: 未找到 litellm 的 api 行")
    # 在主 provider 之后插入新 provider（保持 providers 层缩进）
    anchor = "        baseURL: https://<pool-host>/works/pool/v1\n        models:\n"
    tail_start = out.find(anchor)
    if tail_start < 0:
        raise SystemExit("ABORT: 找不到 providers 结构锚点")
    # 找到主 provider 模型列表结束（下一个顶格 8 空格的键，或列表结束）
    m = re.search(r"(?m)^        models:\n((?:          .*\n|\n)*)", out)
    if not m:
        raise SystemExit("ABORT: 找不到 models 列表")
    insert_at = m.end()
    resp = provider_block("litellm-responses", "openai-responses", [MUSE, DSV41],
                          display="litellm (responses)")
    out = out[:insert_at] + resp + out[insert_at:]
    out, ok = set_allowlist(out, [("litellm", MIMO[0]), ("litellm", MIMO[1]),
                                  ("litellm-responses", MUSE), ("litellm", DSV41)])
    if not ok:
        raise SystemExit("ABORT: 未找到 allowedModels")
    return out


def plan_mode_b(patch):
    """litellm 不动；追加 litellm-chat(completions) 放 mimo×2。"""
    m = re.search(r"(?m)^        models:\n((?:          .*\n|\n)*)", patch)
    if not m:
        raise SystemExit("ABORT: 找不到 models 列表")
    insert_at = m.end()
    blk = provider_block("litellm-chat", "openai-completions", MIMO, display="litellm (chat)")
    out = patch[:insert_at] + blk + patch[insert_at:]
    out, ok = set_allowlist(out, [("litellm-chat", MIMO[0]), ("litellm-chat", MIMO[1]),
                                  ("litellm", MUSE), ("litellm", DSV41)])
    if not ok:
        raise SystemExit("ABORT: 未找到 allowedModels")
    return out


def show_diff(path, old, new):
    d = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                             fromfile=path + " (now)", tofile=path + " (after)")
    sys.stdout.writelines(d)


def backup_path_for(path):
    """备份落点：角色目录里**每个 .md 都会被当作角色加载**，所以角色备份必须放到目录外。"""
    ts = time.strftime("%Y%m%d-%H%M")
    parent = os.path.dirname(path)
    if os.path.basename(parent) == "roles":
        out = os.path.join(os.path.dirname(parent), "roles-backup")
        os.makedirs(out, exist_ok=True)
        return os.path.join(out, os.path.basename(path) + f".bak-protofix-{ts}")
    return f"{path}.bak-protofix-{ts}"


def write_with_backup(path, new):
    bak = backup_path_for(path)
    shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(new)
    print(f"WROTE {path}   (backup: {bak})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["A", "B"], required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not (a.dry_run or a.apply):
        raise SystemExit("需要 --dry-run 或 --apply")

    patch = open(PATCH, encoding="utf-8").read()
    try:
        import yaml
        before = yaml.safe_load(patch.replace("!!js ", ""))
    except Exception as e:
        raise SystemExit(f"ABORT: 现有 profile 补丁不是合法 YAML: {e}")

    new_patch = plan_mode_a(patch) if a.mode == "A" else plan_mode_b(patch)
    try:
        import yaml
        yaml.safe_load(new_patch.replace("!!js ", ""))
    except Exception as e:
        raise SystemExit(f"ABORT: 生成的补丁不是合法 YAML（未写入）: {e}")

    # 角色改动
    role_changes = []
    if a.mode == "A":
        role_changes.append(("reviewer", "litellm-responses", MUSE))
    else:
        for r in MIMO_ROLES:
            role_changes.append((r, "litellm-chat", MIMO_MODEL[r]))

    print(f"=== 模式 {a.mode} 预览 ===")
    show_diff(PATCH, patch, new_patch)
    for name, prov, model in role_changes:
        p = os.path.join(ROLES, f"{name}.md")
        if not os.path.exists(p):
            print(f"!! 角色文件不存在: {p}")
            continue
        old = open(p, encoding="utf-8").read()
        new, changed = set_role(old, prov, model)
        if changed:
            show_diff(p, old, new)

    if a.dry_run:
        print("\n[dry-run] 未写入任何文件。加 --apply 落盘（会自动备份）。")
        return 0

    write_with_backup(PATCH, new_patch)
    for name, prov, model in role_changes:
        p = os.path.join(ROLES, f"{name}.md")
        old = open(p, encoding="utf-8").read()
        new, changed = set_role(old, prov, model)
        if changed:
            write_with_backup(p, new)
    print("\n完成。下一步：重启 DSH，然后用 pool/tools/affinity_report.py 复验。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
