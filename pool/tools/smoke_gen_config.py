#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_config.py 离线端到端冒烟：stub 掉网络与 docker，真实跑完整生成流程并断言产物。

用途：改 gen_config.py（端点 / UA / 会话桶 / 别名形态）后做回归，不需要上游密钥、不碰生产。
    python3 pool/tools/smoke_gen_config.py [gen_config.py 路径]
若有 PyYAML，会额外做一次真实 YAML 解析校验；没有则跳过该项。
"""
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import urllib.request

# Windows 控制台默认 cp936，中文断言名会 UnicodeEncodeError → 强制 UTF-8 输出
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

GC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "gen_config.py")
GC = os.path.abspath(GC)

tmp = tempfile.mkdtemp(prefix="gc-smoke-")
ENV = os.path.join(tmp, ".env")
CFG = os.path.join(tmp, "config.yaml")
GUARD = os.path.join(tmp, "guard.log")
FAILC = os.path.join(tmp, "usage_failcount.json")

with open(ENV, "w", encoding="utf-8") as f:
    for n in (2, 4, 5, 6):
        f.write("OCG_KEY_%d=sk-unit-%d\n" % (n, n))

MODELS = ["mimo-v2.6-flash", "mimo-v2.6-pro", "muse-spark-1.3-contributor",
          "claude-haiku-5-5", "deepseek-v4.1-flash", "not-in-allowlist-model"]


def fake_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    if "usage" in url:
        return io.BytesIO(json.dumps({"usage": {w: {"status": "ok", "percent": 1, "resetsAt": 1}
                                                 for w in ("rolling", "weekly", "monthly")}}).encode())
    if "models" in url:
        return io.BytesIO(json.dumps({"data": [{"id": m} for m in MODELS]}).encode())
    raise RuntimeError("unexpected url " + url)


class FakeRun:
    stdout = "2026-10-16 17:00:00\n"
    stderr = ""


urllib.request.urlopen = fake_urlopen
import subprocess  # noqa: E402

subprocess.run = lambda *a, **k: FakeRun()
os.environ["POOL_SKIP_RESTART"] = "1"
# pool-dash 副本带 fail-closed 闸门；冒烟场景（路径已改到临时目录、subprocess 已 stub）显式放行
os.environ["POOL_DASH_ALLOW_APPLY"] = "1"

src = open(GC, encoding="utf-8").read()
# 让被测生成器能 import upstream_policy：生产里它在脚本同目录（/opt/litellm、/opt/pool-dash），
# 本地冒烟跑 pool-dash 副本时则位于仓库的 pool/ 目录。
sys.path.insert(0, os.path.dirname(GC))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
for const, val in (("ENV_PATH", ENV), ("CFG_PATH", CFG), ("GUARD_LOG", GUARD),
                   ("FAILCOUNT_PATH", FAILC)):
    # 用 lambda 替换：re.sub 的字符串替换会吃掉反斜杠转义（Windows 路径）
    src = re.sub(r'^%s = ".*?"$' % const, lambda m, v=val: "%s = %r" % (m.group(0).split(" ")[0], v),
                 src, count=1, flags=re.M)

g = {"__name__": "__main__", "__file__": GC}
try:
    exec(compile(src, GC, "exec"), g)
except SystemExit:
    pass

cfg = open(CFG, encoding="utf-8").read()
fails = []


def ok(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  :: " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


K = g["SESSION_BUCKETS"]
expected_models = [m for m in MODELS if m in g.get("MODEL_ALLOWLIST", set(MODELS))]
dep_lines = re.findall(r"^  - model_name: (\S+)$", cfg, flags=re.M)
ok("allowlist 生效" if "MODEL_ALLOWLIST" in g else "无 allowlist（该副本自行全量入池）",
   ("not-in-allowlist-model" not in cfg) or ("MODEL_ALLOWLIST" not in g))
ok("deployment 数 = 模型×账号×(1+桶)",
   len(dep_lines) == len(expected_models) * 4 * (1 + K),
   "got=%d expect=%d" % (len(dep_lines), len(expected_models) * 4 * (1 + K)))

# 2026-10-28 出口拆分：非 muse 走本机直连反代（DIRECT_*），muse 才走规范 Go 端点。
# 逐模型用 route_of 推导期望 api_base 与协议前缀，不再假定全部走规范端点。
route_of = g.get("route_of")
ok("生成器暴露 route_of（供路由断言）", callable(route_of))
expected_bases = {}
expected_prefix = {}
if callable(route_of):
    for model_name in expected_models:
        prefix, base = route_of(model_name)
        expected_prefix[model_name] = prefix
        expected_bases[base] = expected_bases.get(base, 0) + 4 * (1 + K)
ok("api_base 与出口拆分一致",
   bool(expected_bases) and all(cfg.count("api_base: %s" % base) == count
                                for base, count in expected_bases.items()),
   {base: cfg.count("api_base: %s" % base) for base in expected_bases})
ok("模型协议前缀与路由一致",
   all("model: %s/%s" % (prefix, model_name) in cfg
       for model_name, prefix in expected_prefix.items()), expected_prefix)
ok("无残留旧端点 /zen/go/v1", "zen/go/v1" not in cfg)

alias_names = [d for d in dep_lines if "@a" in d]
ok("别名数 = 模型×账号×桶", len(alias_names) == len(expected_models) * 4 * K, len(alias_names))
bucketed = re.findall(r"@a([0-9]+)s([0-9]+)$", "\n".join(alias_names), flags=re.M)
ok("别名形如 @a<N>s<K>", len(bucketed) == len(alias_names), len(bucketed))
ok("桶号范围 1..K", all(1 <= int(b) <= K for _, b in bucketed), sorted({int(b) for _, b in bucketed}))
per_acct = {}
for a, b in bucketed:
    per_acct.setdefault(a, set()).add(int(b))
ok("每个账号铺满 K 个桶", all(len(v) == K for v in per_acct.values()),
   {k: len(v) for k, v in per_acct.items()})

sess = re.findall(r"x-opencode-session: (\S+)", cfg)
ok("每个 deployment 都有 x-opencode-session", len(sess) == len(dep_lines), len(sess))
ok("会话号 = 账号×(1+桶) 且无多余重复", len(set(sess)) == 4 * (1 + K), "%d unique" % len(set(sess)))
ok("会话号不含旧常量 litellm-ocg-", "litellm-ocg-" not in cfg)
ok("UA 已写入 extra_headers", cfg.count("user-agent: %s" % g["POOL_USER_AGENT"]) == len(dep_lines),
   cfg.count("user-agent: %s" % g["POOL_USER_AGENT"]))
ok("usage/models 端点已切",
   g["UPSTREAM_USAGE"].endswith("/inference/go/v1/usage") and g["UPSTREAM_MODELS"].endswith("/inference/go/v1/models"))

# 上游通道策略：产物里不得出现 Zen/Console（付费）通道
_up_candidates = [os.path.join(os.path.dirname(GC), "upstream_policy.py"),
                  os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "upstream_policy.py"))]
_up_path = next((p for p in _up_candidates if os.path.exists(p)), None)
if _up_path:
    _spec = importlib.util.spec_from_file_location("upstream_policy", _up_path)
    _up = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_up)
    _bad = _up.check_lines(cfg.splitlines())
    ok("上游通道策略：产物全部为 Go 订阅", not _bad, _bad)
else:
    print("  SKIP  未找到 upstream_policy.py，跳过通道策略断言")

try:
    import yaml
    doc = yaml.safe_load(cfg)
    ml = doc.get("model_list") or []
    ok("产物是合法 YAML", len(ml) == len(dep_lines), len(ml))
    ok("deployment 结构完整", all("model_name" in d and "litellm_params" in d for d in ml))
    # 2026-10-21：推理模型必须声明 model_info.supports_reasoning —— 否则 LiteLLM
    # 按“cost map 不认识该模型 = 不支持推理”处理，客户端一发 reasoning.effort 就 400。
    want_reasoning = {"mimo-v2.6-flash", "mimo-v2.6-pro", "muse-spark-1.3-contributor",
                      "claude-haiku-5-5", "deepseek-v4.1-flash"}
    got = {d["model_name"].split("@")[0] for d in ml
           if (d.get("model_info") or {}).get("supports_reasoning") is True}
    ok("推理模型已登记 model_info.supports_reasoning", got == want_reasoning,
       sorted(got))
    ok("非推理模型不得误标推理", all(
        (d.get("model_info") or {}).get("supports_reasoning") is not True
        for d in ml if d["model_name"].split("@")[0] not in want_reasoning))
except ImportError:
    print("  SKIP  PyYAML 不可用，跳过真实 YAML 解析")

print("\nartifacts: %s" % tmp)
if fails:
    print("FAILED %d: %s" % (len(fails), fails))
    sys.exit(1)
print("ALL PASS")
