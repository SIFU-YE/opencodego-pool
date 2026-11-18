#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S5：把「期望账号」写进 metadata.key_want，让埋点对两种协议都能归属。

为什么
    pool_logger 记录的账号（key/keyRef）在 Responses 协议下为空——
    实测该协议下埋点记的是「改写前的模型名」，无法据此归属账号。
    而 pool_logger 本来就会把 metadata.key_want 记进 pool-requests.jsonl（P3 观测面），
    所以 sidecar 只需把选号结果写进 metadata.key_want，两种协议都能验收。

回滚
    cp -p /opt/litellm/session-sidecar.py.bak-treepin-s5-* /opt/litellm/session-sidecar.py
    systemctl restart litellm-session-sidecar
"""
import shutil
import sys
import time

PATH = "/opt/litellm/session-sidecar.py"
MARK = "treepin-s5"

OLD = '''        try:
            if final and affinity_state().get("mode") == "full":
                _alias = pick_alias(final, d.get("model"))
                if _alias:
                    d["model"] = _alias
                    md["pool_alias"] = _alias
        except Exception:
            pass'''

NEW = '''        try:
            if final:
                _key_ref, _ = choose_tag(final)          # treepin-s5：期望账号写入 metadata
                if _key_ref:
                    md["key_want"] = _key_ref            # pool_logger 的 P3 字段 → 两种协议都能验收
                    if affinity_state().get("mode") == "full":
                        _alias = pick_alias(final, d.get("model"))
                        if _alias:
                            d["model"] = _alias
                            md["pool_alias"] = _alias
                            md["sess_class"] = "tree-pinned"
        except Exception:
            pass'''


def main():
    src = open(PATH, encoding="utf-8").read()
    if MARK in src:
        print("ALREADY-PATCHED (%s)" % MARK)
        return 0
    if src.count(OLD) != 1:
        print("ABORT: anchor found %d times" % src.count(OLD))
        return 2
    bak = "%s.bak-treepin-s5-%s" % (PATH, time.strftime("%Y%m%d-%H%M"))
    shutil.copy2(PATH, bak)
    out = src.replace(OLD, NEW, 1)
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
