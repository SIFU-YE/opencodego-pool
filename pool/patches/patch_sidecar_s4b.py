#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4-b：sidecar 改为「按会话树键把请求 model 改写成池内别名」（替代标签注入）。

变化点
  ① 新增 alias_set()/pick_alias()：别名必须真实存在于 config.yaml（热读缓存），否则不改写（fail-open）
  ② baseline_session 内：算出树键后，若 mode=full 则把 d["model"] 改写为 <model>@a<N>，
     并在 metadata 里记 pool_alias（便于观测；messages 字节仍绝不动）
  ③ _relay：移除 x-litellm-tags 注入（标签路由实测不可靠），决策日志改记 alias

回滚
  cp -p /opt/litellm/session-sidecar.py.bak-treepin-s4b-* /opt/litellm/session-sidecar.py
  systemctl restart litellm-session-sidecar
  或把 pool-affinity.json 的 mode 改回 shadow（秒级，立即停止改写）
"""
import shutil
import sys
import time

PATH = "/opt/litellm/session-sidecar.py"
MARK = "treepin-s4b"

OLD_HELPERS = '''AFFINITY_CONTROL_FILE = "/opt/litellm/pool-affinity.json"'''
NEW_HELPERS = '''AFFINITY_CONTROL_FILE = "/opt/litellm/pool-affinity.json"
_ALIAS_RE = re.compile(r"model_name:\\s*(\\S+@a[0-9]+)")   # treepin-s4b：config.yaml 里的池内别名
_ALIAS_CACHE = {"checked": 0.0, "set": set(), "reason": "init"}'''
# 上一行的 AFFINITY_CONTROL_FILE 定义在文件中唯一，用它当锚点

OLD_STATE = '''AFFINITY_TTL = 5.0'''
NEW_STATE = '''AFFINITY_TTL = 5.0
ALIAS_TTL = 30.0


def alias_set():
    """treepin-s4b：config.yaml 中真实存在的别名集合（30s 缓存，失败退化为空集=不改写）。"""
    now = time.time()
    if now - _ALIAS_CACHE["checked"] < ALIAS_TTL:
        return _ALIAS_CACHE
    _ALIAS_CACHE["checked"] = now
    try:
        with open(AFFINITY_CONFIG_FILE, "r", encoding="utf-8") as fh:
            names = set(_ALIAS_RE.findall(fh.read()))
        _ALIAS_CACHE["set"] = names
        _ALIAS_CACHE["reason"] = "ok:%d" % len(names)
    except Exception as e:
        _ALIAS_CACHE["set"] = set()
        _ALIAS_CACHE["reason"] = "err:%s" % str(e)[:60]
    return _ALIAS_CACHE


def pick_alias(tree, model):
    """按树键确定性选号 → 返回该账号的池内别名；任何异常都返回 None（不改写）。"""
    try:
        if not tree or not isinstance(model, str) or not model or "@a" in model:
            return None
        key_ref, _ = choose_tag(tree)
        if not key_ref:
            return None
        m = re.match(r"^OCG_KEY_([0-9]+)$", key_ref)
        if not m:
            return None
        alias = "%s@a%s" % (model, m.group(1))
        known = alias_set()["set"]
        if known and alias not in known:
            return None
        return alias
    except Exception:
        return None'''

OLD_BODY = '''        md["pool_src"] = "sidecar"'''
NEW_BODY = '''        # treepin-s4b：会话树钉号 —— 按树键把 model 改写成池内别名（客户端可见面不变）
        try:
            if final and affinity_state().get("mode") == "full":
                _alias = pick_alias(final, d.get("model"))
                if _alias:
                    d["model"] = _alias
                    md["pool_alias"] = _alias
        except Exception:
            pass
        md["pool_src"] = "sidecar"'''

OLD_RELAY = '''                    _st = affinity_state()           # treepin-s1：树键 → 确定性选号
                    _picked, _tag = choose_tag(_tree)
                    if _tag:
                        note_affinity(_tree, _model, _picked, _tag, _st["mode"], _st["reason"])
                        if _st["mode"] == "full":
                            aff_tag = _tag'''
NEW_RELAY = '''                    _st = affinity_state()           # treepin-s1：树键 → 确定性选号
                    _picked, _tag = choose_tag(_tree)
                    if _tag:
                        # treepin-s4b：改为别名改写（在 baseline_session 内完成），此处只记决策
                        _alias_want = pick_alias(_tree, _model)
                        note_affinity(_tree, _model, _picked,
                                      _alias_want or _tag, _st["mode"], _st["reason"])'''

OLD_INJECT = '''            if aff_tag:                                 # treepin-s1：注入标签（仅 mode=full）
                for _k in [k for k in list(hdrs) if k.lower() == "x-litellm-tags"]:
                    hdrs.pop(_k, None)
                hdrs["x-litellm-tags"] = aff_tag
'''


def main():
    src = open(PATH, encoding="utf-8").read()
    if MARK in src:
        print("ALREADY-PATCHED (%s)" % MARK)
        return 0
    for name, anchor in (("helpers", OLD_HELPERS), ("state", OLD_STATE), ("body", OLD_BODY),
                         ("relay", OLD_RELAY), ("inject", OLD_INJECT)):
        n = src.count(anchor)
        if n != 1:
            print("ABORT: anchor %s found %d times (expected 1)" % (name, n))
            return 2
    bak = "%s.bak-treepin-s4b-%s" % (PATH, time.strftime("%Y%m%d-%H%M"))
    shutil.copy2(PATH, bak)
    out = (src.replace(OLD_HELPERS, NEW_HELPERS, 1)
              .replace(OLD_STATE, NEW_STATE, 1)
              .replace(OLD_BODY, NEW_BODY, 1)
              .replace(OLD_RELAY, NEW_RELAY, 1)
              .replace(OLD_INJECT, "", 1))
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
