#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S1b 补丁：选号结果与 config.yaml 里「真实存在的账号标签」取交集，避免 401。

为什么需要
    sidecar 的候选账号来自 routing-share.json（份额引擎，10 分钟一跑）；
    若某账号刚被门禁从 config.yaml 剔除、而份额文件还没更新，
    注入它的标签会让 LiteLLM 直接返回
        401 Not allowed to access model due to tags configuration
    —— 这是客户端可见的错误。取交集后，只会给「当前配置里确实存在的账号」打标签。

回滚
    cp -p /opt/litellm/session-sidecar.py.bak-treepin-s1b-* /opt/litellm/session-sidecar.py
    systemctl restart litellm-session-sidecar
"""
import shutil
import sys
import time

PATH = "/opt/litellm/session-sidecar.py"
MARK = "treepin-s1b"

OLD_CONST = '''AFFINITY_TTL = 5.0'''

NEW_CONST = '''AFFINITY_TTL = 5.0
AFFINITY_CONFIG_FILE = "/opt/litellm/config.yaml"     # treepin-s1b：以它为「账号当前是否在池」的权威
_ACCT_TAG_RE = re.compile(r"acct:(OCG_KEY_[0-9]+)")'''

OLD_ALLOC = '''                accounts.append((str(key_ref), share))
            alloc = "ok" if accounts else "no-healthy-account"'''

NEW_ALLOC = '''                accounts.append((str(key_ref), share))
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

OLD_REASON = '''        _AFF_CACHE["reason"] = "%s/%s" % (reason, alloc)'''

NEW_REASON = '''        _AFF_CACHE["reason"] = "%s/%s" % (reason, alloc)  # treepin-s1b: alloc 含交集信息'''


def main():
    src = open(PATH, encoding="utf-8").read()
    if MARK in src:
        print("ALREADY-PATCHED (%s)" % MARK)
        return 0
    if "treepin-s1" not in src:
        print("ABORT: S1 patch missing")
        return 2
    for name, anchor in (("const", OLD_CONST), ("alloc", OLD_ALLOC), ("reason", OLD_REASON)):
        n = src.count(anchor)
        if n != 1:
            print("ABORT: anchor %s found %d times (expected 1)" % (name, n))
            return 2
    bak = "%s.bak-treepin-s1b-%s" % (PATH, time.strftime("%Y%m%d-%H%M"))
    shutil.copy2(PATH, bak)
    out = src.replace(OLD_CONST, NEW_CONST, 1).replace(OLD_ALLOC, NEW_ALLOC, 1).replace(OLD_REASON, NEW_REASON, 1)
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
