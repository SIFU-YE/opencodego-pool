#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清洗历史 pool-requests.jsonl：把 keyRef 中的完整上游密钥截成末 24 位。

背景
----
旧版 pool_logger.py（Gitee 2026-10-20 之前）写 `"keyRef": str(key)`，把
OCG_KEY_n 的完整密钥逐请求落进 pool-requests.jsonl；该文件默认按 umask
创建为 0644。升级到修复版后**新写入**已安全，但**存量行**仍含完整密钥，
需要一次性清洗。

用法
----
    sudo python3 pool/tools/redact_request_log.py                    # 默认 /opt/litellm/pool-requests.jsonl
    sudo python3 pool/tools/redact_request_log.py --path /path/to/log
    sudo python3 pool/tools/redact_request_log.py --dry-run          # 只统计，不改写

行为
----
· 逐行解析 JSON；keyRef 长度 > 24 的截成末 24 位（看板按尾缀映射，功能不变）；
· 解析失败的行原样保留（不丢数据）；
· 原子改写（tempfile + os.replace），改写结果 0600；
· 不打印任何密钥，只打印计数。

退出码：0 成功；1 文件不存在/不可读。
"""
import argparse
import json
import os
import sys
import tempfile

KEEP = 24


def redact(path, dry_run=False):
    total = changed = bad = 0
    if not os.path.isfile(path):
        print("文件不存在: %s" % path, file=sys.stderr)
        return 1
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".redact-", dir=d)
    os.close(fd)
    os.chmod(tmp, 0o600)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as src, \
                open(tmp, "w", encoding="utf-8") as dst:
            for line in src:
                total += 1
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    bad += 1
                    dst.write(line + "\n")
                    continue
                kr = rec.get("keyRef")
                if isinstance(kr, str) and len(kr) > KEEP:
                    rec["keyRef"] = kr[-KEEP:]
                    changed += 1
                dst.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if dry_run:
            os.unlink(tmp)
            print("dry-run: 行数=%d 需改写=%d 解析失败=%d" % (total, changed, bad))
            return 0
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        print("完成: 行数=%d 改写=%d 解析失败=%d 权限=0600" % (total, changed, bad))
        return 0
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description="清洗历史 pool-requests.jsonl 中的完整 keyRef")
    ap.add_argument("--path", default="/opt/litellm/pool-requests.jsonl")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    return redact(a.path, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
