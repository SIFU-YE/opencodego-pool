# -*- coding: utf-8 -*-
"""上游通道策略：**仅使用 OpenCode Go 订阅（opencodego），不得使用 Zen / Console（付费）**。

背景（2026-10-16 实测）：
  · Go 订阅通道：`https://opencode.ai/inference/go/...`（规范路径），
    旧路径 `https://opencode.ai/zen/go/...` 是它的别名（同名 Go 模型表，逐字节相同）；
  · Console（pay-as-you-go / 含 *-free）通道：`https://opencode.ai/inference/...`
    与 `https://opencode.ai/zen/v1...` —— **本仓库禁止使用**。
  两者极易混淆：路径里只差一个 `/go/` 段，而 Console 通道按量计费。

本模块把该策略做成**可执行的断言**：gen_config 在写 config.yaml 之前校验
所有 api_base，命中非 Go 通道立即拒绝写盘与重启（fail-closed，退出码 4）。
除模块加载时读一次环境变量（用于推导直连反代白名单）外，不读文件、不发网络请求，
供生成器与测试共用。
"""
import os

GO_BASE_PREFIXES = (
    "https://opencode.ai/inference/go/",   # 规范 Go 通道
    "https://opencode.ai/zen/go/",         # 同一 Go 订阅通道的旧路径别名
)

# 2026-10-28：本机直连反代（nginx -> opencode.ai Go 通道，不经 sing-box）。
# 路径仍是 /inference/go/，只是换成本机/部署机 host；仅为「非 muse 模型绕开日本出口」用。
# 仓库里不写死任何内网地址：默认只放行本机 loopback；若反代在 Docker 网关或另一台主机，
# 部署方用 POOL_DIRECT_OPENAI / POOL_DIRECT_ANTHROPIC（与 gen_config.py 同名）显式提供
# api_base，这里从这两个变量推导出「协议 + 主机 + 端口 + /inference/go/」前缀。
_RELAY_PATH = "/inference/go/"
_DEFAULT_RELAY_HOSTS = ("127.0.0.1", "localhost")
_RELAY_PORT = 4002


def _relay_prefixes():
    prefixes = ["http://%s:%d%s" % (host, _RELAY_PORT, _RELAY_PATH)
                for host in _DEFAULT_RELAY_HOSTS]
    for name in ("POOL_DIRECT_OPENAI", "POOL_DIRECT_ANTHROPIC"):
        value = (os.environ.get(name) or "").strip()
        if not value.startswith(("http://", "https://")):
            continue
        idx = value.find(_RELAY_PATH)
        if idx > 0:
            prefixes.append(value[:idx + len(_RELAY_PATH)])
    # 去重且保持顺序
    return tuple(dict.fromkeys(prefixes))


RELAY_BASE_PREFIXES = _relay_prefixes()

# Console / 付费通道特征（注意：不能误伤 `.../inference/go/openai/v1`）
FORBIDDEN_MARKERS = (
    "opencode.ai/zen/v1",             # Console 模型表（与 /inference/v1 同表）
    "opencode.ai/inference/v1",       # Console 模型表
    "opencode.ai/inference/openai",   # Console OpenAI 协议端点
    "opencode.ai/inference/anthropic",
    "opencode.ai/inference/google",
    "opencode.ai/inference/systemone",
    "/works/pool/zen",                # 号池侧若出现的 zen 专用入口
)


def classify(base):
    """返回 (是否合规, 原因)。base 为 None/空 → 视为不合规。"""
    if not isinstance(base, str) or not base.strip():
        return False, "api_base 为空"
    b = base.strip()
    for m in FORBIDDEN_MARKERS:
        if m in b:
            return False, "命中 Console/Zen 付费通道特征 %r" % m
    for p in GO_BASE_PREFIXES:
        if b.startswith(p):
            return True, "ok(go)"
    for p in RELAY_BASE_PREFIXES:
        if b.startswith(p):
            return True, "ok(go-relay)"
    return False, "不在 Go 订阅白名单前缀内（%s）" % " / ".join(GO_BASE_PREFIXES)


def extract_bases(lines):
    """从生成器产出的行里抽出所有 api_base 值。"""
    out = []
    for ln in lines or []:
        s = (ln or "").strip()
        if s.startswith("api_base:"):
            out.append(s.split(":", 1)[1].strip())
    return out


def check_lines(lines):
    """校验生成器产出的全部 api_base。返回违规列表 [(base, reason)]，空=全部合规。"""
    bad = []
    for b in extract_bases(lines):
        okc, why = classify(b)
        if not okc:
            bad.append((b, why))
    return bad


def assert_lines(lines, on_error=None):
    """不合规则抛出 RuntimeError（调用方可捕获后自行退出）。返回合规的 base 数。"""
    bad = check_lines(lines)
    if bad:
        detail = "; ".join("%s → %s" % (b, r) for b, r in bad)
        if on_error:
            on_error(detail)
        raise RuntimeError("上游通道策略违规：%s" % detail)
    return len(extract_bases(lines))
