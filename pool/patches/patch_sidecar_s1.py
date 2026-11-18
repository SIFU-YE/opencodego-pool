#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S1 补丁：给号池 sidecar 增加「会话树钉号」选号逻辑（默认 shadow，不注入）。

做什么
    读 /opt/litellm/routing-share.json（份额引擎产出）→ 以树键做**确定性加权选号**
        acct = weighted_pick(seed = sha256("tree:" + tree), weights = 各账号 share)
    同一棵树永远得到同一账号（无需状态表，重启不丢，跨模型一致）。
    健康过滤：任一窗口 status != ok、或 share <= 0 的账号被排除。
    mode=full 时向 LiteLLM 注入头  x-litellm-tags: acct:<KEY_REF>（LiteLLM 标签路由）。
    mode 由 /opt/litellm/pool-affinity.json 热读，默认 shadow（只记决策，不注入）。

回滚
    cp -p /opt/litellm/session-sidecar.py.bak-treepin-s1-* /opt/litellm/session-sidecar.py
    systemctl restart litellm-session-sidecar
    或把 pool-affinity.json 的 mode 改回 shadow（秒级、无需重启）
"""
import json
import shutil
import sys
import time

PATH = "/opt/litellm/session-sidecar.py"
MARK = "treepin-s1"
SEED = "def baseline_session("

HELPERS = '''
# ================================================================ 会话树钉号 tree affinity（2026-10-12 treepin-s1）
# 读份额引擎的 routing-share.json，用树键做确定性加权选号 → 一个会话树固定一个账号。
# 选号不落状态：acct = weighted_pick(sha256("tree:" + tree), weights = share)，重启/重放结果一致。
# mode 来自 /opt/litellm/pool-affinity.json（默认 shadow），热读、可秒级回退。
# 一切异常都不干预请求（fail-open）：拿不到份额 → 不加标签，LiteLLM 维持原有 least-busy。
AFFINITY_CONTROL_FILE = "/opt/litellm/pool-affinity.json"
AFFINITY_SHARE_FILE = "/opt/litellm/routing-share.json"
AFFINITY_DECISIONS_FILE = "/opt/litellm/pool-affinity-decisions.jsonl"
_AFF_CACHE = {"checked": 0.0, "accounts": [], "mode": "shadow", "reason": "init"}
_AFF_LOCK = threading.Lock()
_AFF_LAST = {}
AFFINITY_TTL = 5.0


def affinity_state():
    """热读控制文件与份额文件（TTL 5 s）。返回 {"mode","accounts","reason"}。"""
    now = time.time()
    with _AFF_LOCK:
        if now - _AFF_CACHE["checked"] < AFFINITY_TTL:
            return _AFF_CACHE
        _AFF_CACHE["checked"] = now
        mode = "shadow"
        reason = "control-missing"
        try:
            with open(AFFINITY_CONTROL_FILE, "r", encoding="utf-8") as fh:
                raw = json.load(fh) or {}
            m = str(raw.get("mode") or "shadow")
            mode = m if m in ("shadow", "full") else "shadow"
            reason = "ok"
        except FileNotFoundError:
            mode, reason = "shadow", "control-missing"
        except Exception as e:
            mode, reason = "shadow", "control-error:%s" % str(e)[:60]
        accounts = []
        alloc = "share-missing"
        try:
            with open(AFFINITY_SHARE_FILE, "r", encoding="utf-8") as fh:
                doc = json.load(fh) or {}
            for a in doc.get("accounts") or []:
                key_ref = a.get("key_ref")
                if not key_ref:
                    continue
                wins = a.get("windows") or {}
                unhealthy = any((w or {}).get("status") not in (None, "ok") for w in wins.values())
                share = a.get("share")
                share = float(share) if isinstance(share, (int, float)) else 0.0
                if unhealthy or share <= 0:
                    continue
                accounts.append((str(key_ref), share))
            alloc = "ok" if accounts else "no-healthy-account"
        except Exception as e:
            alloc = "share-error:%s" % str(e)[:60]
        _AFF_CACHE["mode"] = mode
        _AFF_CACHE["accounts"] = accounts
        _AFF_CACHE["reason"] = "%s/%s" % (reason, alloc)
        return _AFF_CACHE


def weighted_pick(tree, accounts):
    """确定性加权选号：同一棵树永远落同一账号。"""
    if not tree or not accounts:
        return None
    total = sum(w for _, w in accounts)
    if total <= 0:
        return None
    h = int(hashlib.sha256(("tree:" + str(tree)).encode("utf-8")).hexdigest()[:12], 16)
    point = (h % 1000000) / 1000000.0 * total
    acc = 0.0
    for key_ref, weight in accounts:
        acc += weight
        if point < acc:
            return key_ref
    return accounts[-1][0]


def choose_tag(tree):
    """返回 (key_ref, tag) 或 (None, None)。"""
    try:
        st = affinity_state()
        picked = weighted_pick(tree, st["accounts"])
        if not picked:
            return None, None
        return picked, "acct:" + picked
    except Exception:
        return None, None


def note_affinity(tree, model, picked, tag, mode, reason):
    """决策落盘 + 一行日志（每棵树每 60 s 最多一条）。"""
    try:
        if not tree:
            return
        now = time.time()
        with _AFF_LOCK:
            last = _AFF_LAST.get(tree)
            if last is not None and now - last < 60:
                return
            _AFF_LAST[tree] = now
            if len(_AFF_LAST) > 4096:
                _AFF_LAST.clear()
        injected = bool(tag) and mode == "full"
        with open(AFFINITY_DECISIONS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": int(now), "tree": tree, "model": model,
                                 "key_want": picked, "tag": tag, "mode": mode,
                                 "reason": reason, "injected": injected},
                                ensure_ascii=False) + "\\n")
        slog("tree-affinity tree=%s model=%s key_want=%s mode=%s injected=%s reason=%s"
             % (tree, model, picked, mode, injected, reason))
    except Exception:
        pass


'''

OLD_INIT = '''            sess = "-"
            send_body = body
            final_sid = None'''

NEW_INIT = '''            sess = "-"
            send_body = body
            final_sid = None
            aff_tag = None                              # treepin-s1：本请求要注入的账号标签'''

OLD_S0 = '''                _tree = tree_key(send_body)          # treepin-s0：影子观测（不改路由）
                if _tree:
                    try:
                        _model = (json.loads(send_body) or {}).get("model")
                    except Exception:
                        _model = None
                    note_tree(_tree, _model)'''

NEW_S1 = '''                _tree = tree_key(send_body)          # treepin-s0：影子观测（不改路由）
                if _tree:
                    try:
                        _model = (json.loads(send_body) or {}).get("model")
                    except Exception:
                        _model = None
                    note_tree(_tree, _model)
                    _st = affinity_state()           # treepin-s1：树键 → 确定性选号
                    _picked, _tag = choose_tag(_tree)
                    if _tag:
                        note_affinity(_tree, _model, _picked, _tag, _st["mode"], _st["reason"])
                        if _st["mode"] == "full":
                            aff_tag = _tag'''

OLD_INJECT = '''            hdrs["Content-Length"] = str(len(send_body))'''

NEW_INJECT = '''            if aff_tag:                                 # treepin-s1：注入标签（仅 mode=full）
                for _k in [k for k in list(hdrs) if k.lower() == "x-litellm-tags"]:
                    hdrs.pop(_k, None)
                hdrs["x-litellm-tags"] = aff_tag
            hdrs["Content-Length"] = str(len(send_body))'''


def dry_run():
    sys.path.insert(0, "/opt/litellm")
    import importlib.util
    spec = importlib.util.spec_from_file_location("sc", PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    st = mod.affinity_state()
    print("mode =", st["mode"], "| reason =", st["reason"])
    print("healthy accounts (key_ref, share):", st["accounts"])
    for tree in ("session-4e7011c7-2246-4e22-8f91-3ca4f93e6295", "session-aaa", "session-bbb", "session-ccc", "session-ddd"):
        print("  %-40s -> %s" % (tree, mod.weighted_pick(tree, st["accounts"])))
    return 0


def main():
    if "--dry-run" in sys.argv:
        return dry_run()
    src = open(PATH, encoding="utf-8").read()
    if MARK in src:
        print("ALREADY-PATCHED (%s present)" % MARK)
        return 0
    if "treepin-s0" not in src:
        print("ABORT: S0 patch not found; apply patch_sidecar_s0.py first")
        return 2
    for name, anchor in (("helpers", SEED), ("init", OLD_INIT), ("s0block", OLD_S0), ("inject", OLD_INJECT)):
        n = src.count(anchor)
        if n != 1:
            print("ABORT: anchor %s found %d times (expected 1)" % (name, n))
            return 2
    bak = "%s.bak-treepin-s1-%s" % (PATH, time.strftime("%Y%m%d-%H%M"))
    shutil.copy2(PATH, bak)
    out = src.replace(SEED, HELPERS.lstrip("\n") + SEED, 1)
    out = out.replace(OLD_INIT, NEW_INIT, 1)
    out = out.replace(OLD_S0, NEW_S1, 1)
    out = out.replace(OLD_INJECT, NEW_INJECT, 1)
    if out.count(MARK) < 4:
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
