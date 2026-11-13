#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本同步校验：确认「仓库 = 唯一最新版，本地/服务器不超前」。

背景
----
2026-10-20 曾出现反向漂移：Gitee（发布源）比生产服务器旧，服务器上跑着
仓库没有的 models_exclude / sqlite 事实源实现 —— 发布源落后于线上，等于
「谁在服务器上手改，谁就是最新版」，无法审计也无法回滚。

规则（见 docs/版本同步.md）
----
1. 仓库是唯一事实源：任何修改先进仓库，测试通过后推送；
2. 部署只允许「仓库 → 服务器」单向覆盖，禁止在服务器上手改运行文件；
3. 本地（DSH 插件目录）与服务器都不得超前于仓库：
   · 服务器超前（本地没有该内容）→ 立即回灌仓库；
   · 仓库超前 → 部署到本地/服务器；
4. 每次改动后跑一次本脚本，要求输出「全部一致」。

用法
----
    python3 pool/tools/sync_check.py                     # 本机部署目录（默认 /opt/litellm、/opt/pool-dash）
    python3 pool/tools/sync_check.py --ssh agentteam     # 远端服务器（经 ssh 取哈希比对）
    python3 pool/tools/sync_check.py --repo /path/to/repo --litellm /opt/litellm --pool-dash /opt/pool-dash
    python3 pool/tools/sync_check.py --json              # 机器可读输出

比较方式
----
统一按「CRLF → LF 归一化后的 sha256」比较，避免 Windows 检出换行差异造成误报。
MISSING 表示文件在一侧不存在（同样视为漂移）。

退出码：0 全部一致；1 存在漂移；2 运行错误。
"""
import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys

# 仓库相对路径 → 部署绝对路径（只列**运行文件**；测试/工具不参与线上判断）
MANIFEST = [
    # ---- 号池核心（/opt/litellm）----
    ("pool/pool_control.py", "/opt/litellm/pool_control.py"),
    ("pool/pool_reconciler.py", "/opt/litellm/pool_reconciler.py"),
    ("pool/auto_admit.py", "/opt/litellm/auto_admit.py"),
    ("pool/pool_store.py", "/opt/litellm/pool_store.py"),
    ("pool/pool_logger.py", "/opt/litellm/pool_logger.py"),
    ("pool/gen_config.py", "/opt/litellm/gen_config.py"),
    ("pool/session-sidecar.py", "/opt/litellm/session-sidecar.py"),
    ("pool/share-engine.py", "/opt/litellm/share-engine.py"),
    ("pool/upstream_policy.py", "/opt/litellm/upstream_policy.py"),
    ("pool/toolrepair.py", "/opt/litellm/toolrepair.py"),
    ("pool/probe-tag.py", "/opt/litellm/probe-tag.py"),
    ("pool/apply-free-lb.py", "/opt/litellm/apply-free-lb.py"),
    ("pool/apply-cache-retention.py", "/opt/litellm/apply-cache-retention.py"),
    ("pool/apply-mimo-pin.py", "/opt/litellm/apply-mimo-pin.py"),
    ("pool/apply-session-affinity.py", "/opt/litellm/apply-session-affinity.py"),
    ("pool/cache-ttl-test.py", "/opt/litellm/cache-ttl-test.py"),
    ("pool/cache-ttl-test2.py", "/opt/litellm/cache-ttl-test2.py"),
    # ---- 看板 / 管理端（/opt/pool-dash）----
    ("pool-dash/dashboard.py", "/opt/pool-dash/dashboard.py"),
    ("pool-dash/pool_admin.py", "/opt/pool-dash/pool_admin.py"),
    ("pool-dash/pool_admin_ui.py", "/opt/pool-dash/pool_admin_ui.py"),
    ("pool-dash/style-audit.py", "/opt/pool-dash/style-audit.py"),
    ("pool-dash/gen_config.py", "/opt/pool-dash/gen_config.py"),
    ("pool-dash/gen-dsh-cookie.py", "/opt/pool-dash/gen-dsh-cookie.py"),
]


def _norm_sha(path):
    """归一化（CRLF→LF）后的 sha256 前 16 位；文件不存在返回 None。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()[:16]


def _remote_shas(host, paths):
    """经 ssh 取远端文件的归一化 sha256；返回 {path: hash|None}。"""
    script = (
        'for f in "$@"; do '
        'if [ -f "$f" ]; then '
        'printf "%s %s\\n" "$(tr -d \'\\r\' < "$f" | sha256sum | cut -c1-16)" "$f"; '
        'else printf "MISSING %s\\n" "$f"; fi; done'
    )
    # 单参数传给远端 shell：sh -c '<script>' sh <paths...>
    remote = "sh -c %s sh %s" % (shlex.quote(script),
                                 " ".join(shlex.quote(p) for p in paths))
    out = subprocess.run(["ssh", host, remote], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError("ssh 失败: %s" % (out.stderr or "")[:300])
    res = {}
    for line in out.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        h, p = parts
        res[p] = None if h == "MISSING" else h
    return res


def _target_path(root, basename, ssh_mode):
    """部署侧路径：远端强制 POSIX 分隔符（Windows 调用方不能把 \\ 带到远端）。"""
    if ssh_mode:
        return root.replace("\\", "/").rstrip("/") + "/" + basename
    return os.path.join(root, basename)


def _check_plugin(repo, profile):
    """插件版本检查：仓库 dsh-plugin/ 必须是本地 profile 的 junction 目标且版本一致。

    「本地插件超前/落后」的根因通常是本地存了副本而不是 junction，这里直接查出来。
    返回 dict：repo_version / installed_version / link_type / ok / reason
    """
    import json as _json

    def _ver(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return (_json.load(f) or {}).get("version")
        except OSError:
            return None

    repo_pkg = os.path.join(repo, "dsh-plugin", "package.json")
    inst_dir = os.path.join(profile, "node_modules", "@local", "dsh-pool-affinity")
    inst_pkg = os.path.join(inst_dir, "package.json")
    rv, iv = _ver(repo_pkg), _ver(inst_pkg)
    link = None
    try:
        link = _link_type(inst_dir)
    except OSError:
        pass
    reasons = []
    if rv is None:
        reasons.append("仓库 dsh-plugin/package.json 不存在")
    if iv is None:
        reasons.append("已安装插件不存在（未安装？）")
    if rv and iv and rv != iv:
        reasons.append("版本不一致：仓库 %s vs 已安装 %s" % (rv, iv))
    if link not in (None, "Junction", "SymbolicLink", "symlink", None):
        # 非链接 = 本地存了副本，会独立演化
        if link == "":
            reasons.append("本地是实体副本而非 junction（会独立演化）")
    return {"repo_version": rv, "installed_version": iv, "link_type": link,
            "ok": not reasons, "reason": "；".join(reasons)}


def _link_type(path):
    """尽力判断链接类型：Windows junction/symlink 或 POSIX symlink；普通目录返回 ''。"""
    try:
        st = os.lstat(path)
    except OSError:
        raise
    if os.path.islink(path):
        return "symlink"
    # Windows: FILE_ATTRIBUTE_REPARSE_POINT
    if getattr(st, "st_file_attributes", 0) & 0x400:
        return "Junction"
    return ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="校验仓库与部署目录的版本一致性（不得超前）")
    ap.add_argument("--repo", default=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
                    help="仓库根目录（默认：脚本上两级）")
    ap.add_argument("--litellm", default="/opt/litellm")
    ap.add_argument("--pool-dash", default="/opt/pool-dash")
    ap.add_argument("--ssh", default=None, metavar="HOST", help="在远端主机上比对（例如 agentteam）")
    ap.add_argument("--plugin-profile", default=None, metavar="PROFILE",
                    help="DSH profile 目录；给出时校验插件版本与 junction")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    rows = []
    remote_paths = []
    for rel, target in MANIFEST:
        if target.startswith("/opt/litellm/"):
            target = _target_path(a.litellm, os.path.basename(target), bool(a.ssh))
        elif target.startswith("/opt/pool-dash/"):
            target = _target_path(a.pool_dash, os.path.basename(target), bool(a.ssh))
        repo_hash = _norm_sha(os.path.join(a.repo, rel))
        rows.append({"repo_path": rel, "target": target, "repo": repo_hash})
        remote_paths.append(target)

    try:
        if a.ssh:
            remote = _remote_shas(a.ssh, remote_paths)
        else:
            remote = {p: _norm_sha(p) for p in remote_paths}
    except Exception as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return 2

    drift = 0
    for r in rows:
        r["deploy"] = remote.get(r["target"])
        if r["repo"] is None and r["deploy"] is None:
            r["status"] = "BOTH-MISSING"
        elif r["repo"] is None:
            r["status"] = "REPO-MISSING"      # 部署有、仓库无 → 说明线上手改/超前
        elif r["deploy"] is None:
            r["status"] = "NOT-DEPLOYED"
        elif r["repo"] == r["deploy"]:
            r["status"] = "OK"
        else:
            r["status"] = "DRIFT"
        if r["status"] != "OK":
            drift += 1

    plugin = None
    if a.plugin_profile:
        plugin = _check_plugin(a.repo, a.plugin_profile)
        if not plugin["ok"]:
            drift += 1

    if a.json:
        print(json.dumps({"drift": drift, "rows": rows, "plugin": plugin},
                         ensure_ascii=False, indent=2))
    else:
        print("仓库: %s" % os.path.abspath(a.repo))
        print("部署: %s%s" % (a.ssh + ":" if a.ssh else "", a.litellm))
        print()
        print("%-13s %-46s %-18s %s" % ("状态", "仓库相对路径", "仓库", "部署"))
        for r in rows:
            if r["status"] == "OK":
                continue
            print("%-13s %-46s %-18s %s" % (r["status"], r["repo_path"],
                                            r["repo"] or "-", r["deploy"] or "-"))
        if drift == 0:
            print("✅ 全部一致（%d 个运行文件）：仓库即最新版，部署未超前" % len(rows))
        else:
            print()
            print("⚠ 发现 %d 处漂移：" % drift)
            print("  · REPO-MISSING → 服务器上有仓库没有的内容：回灌仓库（优先）或删除")
            print("  · NOT-DEPLOYED/DRIFT → 仓库更新：执行部署覆盖，禁止反向手改")
        if plugin is not None:
            tag = "✅" if plugin["ok"] else "⚠"
            print("%s 插件: 仓库 %s / 已安装 %s / 链接 %s%s" % (
                tag, plugin["repo_version"], plugin["installed_version"],
                plugin["link_type"] or "(实体目录)",
                "" if plugin["ok"] else "  ← " + plugin["reason"]))
    return 0 if drift == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
