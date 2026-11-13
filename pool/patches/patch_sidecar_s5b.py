#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S5b：决策日志的 reason 改为「别名覆盖度」；并更新控制文件说明文字（标签机制已废弃）。

背景
    S1b 的「与 config.yaml 标签取交集」在标签机制废弃后已失效，日志里恒为 config-unknown，
    容易被误读为「配置缺失」。真正的安全阀已移到 pick_alias（别名必须真实存在，否则不改写）。
"""
import json
import shutil
import sys
import time

PATH = "/opt/litellm/session-sidecar.py"
CTRL = "/opt/litellm/pool-affinity.json"
MARK = "treepin-s5b"

OLD = '''                accounts.append((str(key_ref), share))
            # treepin-s1b：与 config.yaml 中真实存在的账号标签取交集，避免给已剔除账号打标签 → 401
            known = set()
            try:
                with open(AFFINITY_CONFIG_FILE, "r", encoding="utf-8") as fh2:
                    for line in fh2:
                        m2 = _ACCT_TAG_RE.search(line)
                        if m2:
                            known.add(m2.group(1))
            except Exception:
                known = set()
            if known:
                before = len(accounts)
                accounts = [a for a in accounts if a[0] in known]
                if len(accounts) != before:
                    alloc_extra = "|config-intersect:%d->%d" % (before, len(accounts))
                else:
                    alloc_extra = "|config-intersect:same"
            else:
                alloc_extra = "|config-unknown"
            alloc = ("ok" if accounts else "no-healthy-account") + alloc_extra'''

NEW = '''                accounts.append((str(key_ref), share))
            # treepin-s5b：标签机制已废弃。真正的安全阀在 pick_alias()——别名必须真实存在于
            # config.yaml，否则不改写（fail-open）。这里只报告别名覆盖度，便于运维判读。
            try:
                _alias_n = len(alias_set()["set"])
            except Exception:
                _alias_n = 0
            alloc = ("ok" if accounts else "no-healthy-account") + "|alias-groups:%d" % _alias_n'''


def main():
    src = open(PATH, encoding="utf-8").read()
    if MARK in src:
        print("ALREADY-PATCHED (%s)" % MARK)
    else:
        if src.count(OLD) != 1:
            print("ABORT: anchor found %d times" % src.count(OLD))
            return 2
        shutil.copy2(PATH, PATH + ".bak-treepin-s5b-" + time.strftime("%Y%m%d-%H%M"))
        out = src.replace(OLD, NEW, 1)
        with open(PATH, "w", encoding="utf-8") as fh:
            fh.write(out)
        import py_compile
        try:
            py_compile.compile(PATH, doraise=True)
        except Exception as e:
            shutil.copy2(PATH + ".bak-treepin-s5b-" + time.strftime("%Y%m%d-%H%M"), PATH)
            print("py_compile FAILED -> rolled back: %s" % e)
            return 4
        print("PATCHED %s | py_compile OK" % PATH)

    d = json.load(open(CTRL, encoding="utf-8"))
    d["note"] = ("shadow = 只记决策不改写（安全默认）；full = 按会话树键把 model 改写为池内别名 "
                 "<model>@a<N>（标签路由已废弃，见 docs/标签路由不可用与替代方案.md）")
    d["updated"] = time.strftime("%Y-%m-%d %H:%M") + " s5b"
    json.dump(d, open(CTRL, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("CTRL updated:", json.dumps(d, ensure_ascii=False)[:160])
    return 0


if __name__ == "__main__":
    sys.exit(main())
