#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""号池自动准入（auto-admit）：账号额度刷新后**自动**重新加入号池。

背景
----
额度耗尽（某窗口 rate-limited）或订阅暂停时，账号被 `pool-config.json` 的
`enabled=false` 挡在池外。额度窗口 `resetsAt` 到点后上游会自动恢复，但控制面
不会自己把开关翻回来 —— 于是每次都要人工去点。本模块把这一步自动化。

判定规则（**只看上游事实，不猜**）
--------------------------------
只有当账号**同时**满足以下条件才准入：

1. 账号当前 `enabled=false`；
2. 账号没有显式锁定（`auto_admit: false`）；
3. `.env` 里该账号的密钥存在且非空；
4. `GET {UPSTREAM}/usage` 返回 200 且含窗口字段；
5. **没有任何窗口** `status == "rate-limited"`（rolling/weekly/monthly 任一即拒）；
6. `monthly.percent < POOL_AUTO_ADMIT_MAX_MONTHLY`（默认 100，留空即 99% 也允许）。

不满足的（例如订阅失效的 403 `EntitlementError`、仍在限流、网络失败）保持禁用，
只在结果里记录原因 —— 绝不对不可自愈的账号反复尝试入池。

安全开关
--------
- `POOL_AUTO_ADMIT=0`          总开关（默认 1=开）
- `POOL_AUTO_ADMIT_MAX_MONTHLY` monthly 百分比上限（默认 100）
- `POOL_USAGE_URL`              上游 usage 地址覆盖（测试用）
- `--dry-run`                   只输出决策，不写控制文件
- `--slot N`                    只检查指定槽位
- `--json`                      机器可读输出

准入动作走 `pool_control.set_account_enabled()`（文件锁内读-改-写 + 审计），
与人工在看板上的开关是同一个事实源，不存在第二个开关。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

import pool_control

UPSTREAM = os.environ.get("POOL_USAGE_URL") or "https://opencode.ai/inference/go/v1"
# 上游明文要求客户端自带可识别 UA（禁止通用 SDK/HTTP 库名）
POOL_USER_AGENT = "dsh-opencodego-pool/1.0"
USAGE_TIMEOUT_S = 12.0
ACTOR = "auto-admit"
WINDOWS = ("rolling", "weekly", "monthly")


# ================================================================ 配置
def enabled():
    """总开关：POOL_AUTO_ADMIT=0 关闭。"""
    return str(os.environ.get("POOL_AUTO_ADMIT", "1")).strip() not in ("0", "false", "False", "off")


def max_monthly_percent():
    try:
        return float(os.environ.get("POOL_AUTO_ADMIT_MAX_MONTHLY", "100"))
    except (TypeError, ValueError):
        return 100.0


# ================================================================ 上游探测
def fetch_usage(key, timeout=USAGE_TIMEOUT_S):
    """查上游额度。返回 {ok, usage} 或 {ok: False, status, error}；不抛异常。"""
    if not key:
        return {"ok": False, "status": None, "error": "empty-key"}
    try:
        req = urllib.request.Request(
            UPSTREAM + "/usage",
            headers={"Authorization": "Bearer " + key, "User-Agent": POOL_USER_AGENT},
        )
        body = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        doc = json.loads(body)
        if isinstance(doc.get("usage"), dict):
            return {"ok": True, "usage": doc}
        return {"ok": False, "status": None, "error": "usage-missing"}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": "http-%d" % exc.code}
    except Exception as exc:  # 网络/解析
        return {"ok": False, "status": None, "error": "%s: %s" % (type(exc).__name__, str(exc)[:80])}


# ================================================================ 决策
def decide(usage_doc):
    """由 `/usage` 响应判断是否准入。返回 (admit: bool, reason: str)。"""
    if not isinstance(usage_doc, dict):
        return False, "usage-invalid"
    windows = usage_doc.get("usage")
    if not isinstance(windows, dict) or not windows:
        return False, "usage-empty"
    limited = [w for w in WINDOWS if isinstance(windows.get(w), dict)
               and windows[w].get("status") == "rate-limited"]
    if limited:
        return False, "rate-limited:" + ",".join(limited)
    monthly = windows.get("monthly") if isinstance(windows.get("monthly"), dict) else {}
    try:
        percent = float(monthly.get("percent"))
    except (TypeError, ValueError):
        percent = 0.0
    cap = max_monthly_percent()
    if percent >= cap:
        return False, "monthly-percent=%g%%>=%g" % (percent, cap)
    return True, "quota-ok:monthly=%g%%" % percent


# ================================================================ 主流程
def _accounts(config):
    return [a for a in (config.get("accounts") or ()) if hasattr(a, "get")]


def run(dry_run=False, slots=None, fetch=None, now=None):
    """对一个批次执行自动准入；返回结构化结果。不抛异常（内部失败落 errors）。"""
    started = time.time()
    fetch = fetch or fetch_usage
    result = {
        "ok": True,
        "dry_run": bool(dry_run),
        "checked": 0,
        "admitted": [],
        "skipped": [],
        "errors": [],
        "elapsed_s": 0.0,
    }
    if not enabled():
        result["ok"] = True
        result["skipped_all"] = "disabled(POOL_AUTO_ADMIT=0)"
        return result

    config = pool_control.load_config(force=True)
    if config is None:
        result["ok"] = False
        result["errors"].append("pool-config 不可读：%s" % pool_control.last_error())
        return result

    try:
        env = pool_control.read_env()
    except Exception as exc:
        result["ok"] = False
        result["errors"].append("读取 .env 失败：%s" % str(exc)[:120])
        return result

    only = None
    if slots:
        only = {int(s) for s in slots}

    for account in _accounts(config):
        slot = account.get("slot")
        if only is not None and slot not in only:
            continue
        if account.get("enabled", True):
            continue  # 已在池内，无事可做
        if account.get("auto_admit", True) is False:
            result["skipped"].append({"slot": slot, "reason": "locked(auto_admit=false)"})
            continue
        key_var = account.get("key_var") or ("OCG_KEY_%s" % slot)
        key = (env.get(key_var) or "").strip()
        if not key:
            result["skipped"].append({"slot": slot, "reason": "no-key(%s)" % key_var})
            continue

        result["checked"] += 1
        got = fetch(key)
        if not got.get("ok"):
            result["skipped"].append({"slot": slot, "reason": "usage-error:%s" % got.get("error")})
            continue
        admit, reason = decide(got.get("usage"))
        if not admit:
            result["skipped"].append({"slot": slot, "reason": reason})
            continue
        if dry_run:
            result["admitted"].append({"slot": slot, "reason": reason, "dry_run": True})
            continue
        try:
            pool_control.set_account_enabled(
                slot, True, actor=ACTOR, reason="auto-admit:" + reason,
                source="auto_admit")
        except Exception as exc:
            result["errors"].append({"slot": slot, "error": str(exc)[:160]})
            continue
        result["admitted"].append({"slot": slot, "reason": reason})

    if result["admitted"]:
        result["ok"] = result["ok"] and not result["errors"]
    result["elapsed_s"] = round(time.time() - started, 3)
    return result


# ================================================================ CLI
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="号池自动准入：额度刷新后自动把 enabled=false 的账号重新加入号池。")
    parser.add_argument("--dry-run", action="store_true", help="只输出决策，不写控制文件")
    parser.add_argument("--slot", type=int, action="append", help="只检查指定槽位（可多次）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--quiet", action="store_true", help="成功时不打印摘要")
    args = parser.parse_args(argv)

    result = run(dry_run=args.dry_run, slots=args.slot)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print("自动准入：检查 %d 个禁用账号，准入 %d 个，跳过 %d 个%s"
              % (result["checked"], len(result["admitted"]), len(result["skipped"]),
                 "（dry-run）" if args.dry_run else ""))
        for item in result["admitted"]:
            print("  + slot %s 准入：%s" % (item["slot"], item["reason"]))
        for item in result["skipped"]:
            print("  - slot %s 跳过：%s" % (item["slot"], item["reason"]))
        for item in result["errors"]:
            print("  ! %s" % json.dumps(item, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
