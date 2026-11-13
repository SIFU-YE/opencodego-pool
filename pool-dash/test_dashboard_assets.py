#!/usr/bin/env python3
"""号池看板「页面 + 静态资源」契约自测：离线运行、不联网、不启动任何服务。

运行方式（仓库根目录或 pool-dash/ 目录下均可）：

    python pool-dash/test_dashboard_assets.py

覆盖维护者要求的 5 项：
1. vendor/echarts.min.js 存在且 > 500KB，内容含 echarts 标记（本地 vendored，零 CDN）；
2. 内嵌 HTML 不含任何「绝对根路径」资源引用：src="/…" / href="/…" / fetch("/…"
   （页面经 https://<域名>/works/pool/ 反代前缀访问，绝对路径必然 404）；
3. 内嵌 HTML 含相对路径 vendor/echarts.min.js，且 6 个既有 JSON 接口字符串齐全
   （api/summary、api/account-sessions 为 GET）；2026-10-31 起
     api/teams-config / api/models-config / api/sync-models / api/save-teams-config
     随「DSH 配置 / Agent Teams」死配置下线，断言其不得再出现。
4. <script> 与 </script> 配对；echarts.init 出现次数 ≥ 图表容器数（且 ≥ 12）；
5. dashboard.py py_compile 通过，且在临时 POOL_HOME 下 `dashboard.py --selftest` 返回 0。

另附少量护栏：无右上角 `href="admin"` 冗余入口、无 CDN 域名、路由表已注册 /vendor/echarts.min.js 且带长缓存头、
脚本里所有 $(id)/getElementById 引用的 id 都能在页面里找到对应元素（防悬空引用）。
全部为静态断言 + 只读子进程；子进程的 POOL_HOME/PYTHONPYCACHEPREFIX 都指向系统 temp 临时目录，
不在仓库内落任何文件。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:  # 控制台编码不可编码中文时降级为替换字符，绝不因输出编码而失败
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.join(HERE, "dashboard.py")
ECHARTS = os.path.join(HERE, "vendor", "echarts.min.js")
MIN_ECHARTS_BYTES = 500 * 1024
MIN_CHARTS = 10   # 2026-10-20 瘦身：删掉 4 个稀疏/重复图表（状态码环/账号会话数/缓存/并发）

FAILED = []
PASSED = []


def check(cond, name, detail=""):
    if cond:
        PASSED.append(name)
        print("  ok   %s" % name)
    else:
        FAILED.append((name, detail))
        print("  FAIL %s%s" % (name, ("  <- " + detail) if detail else ""))
    return bool(cond)


def read_text(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def html_block(src):
    """取 dashboard.py 内嵌的 HTML = \"\"\"…\"\"\" 整块（含引号）。"""
    start = src.index('HTML = """')
    end = src.index('"""', start + len('HTML = """')) + 3
    return src[start:end]


def main():
    print("[1] vendored ECharts")
    exists = check(os.path.isfile(ECHARTS), "vendor/echarts.min.js 存在", ECHARTS)
    size = os.path.getsize(ECHARTS) if exists else 0
    check(size > MIN_ECHARTS_BYTES, "体积 > 500KB", "实际 %d 字节" % size)
    if exists:
        head = read_text(ECHARTS)
        check("echarts" in head.lower(), "内容含 echarts 标记")
        m = re.search(r'version:"([0-9]+\.[0-9]+\.[0-9]+)"', head)
        check(bool(m) and m.group(1).startswith("5."), "版本为 ECharts 5.x", m.group(1) if m else "未识别版本")

    print("[2] 页面契约（dashboard.py 内嵌 HTML）")
    src = read_text(DASH)
    html = html_block(src)
    check(len(html) > 20000, "HTML 块可解析且非空", "%d 字符" % len(html))

    abs_pats = [
        ("src=\"/", re.compile(r"""src\s*=\s*["']/""")),
        ("href=\"/", re.compile(r"""href\s*=\s*["']/""")),
        ("fetch(\"/", re.compile(r"""fetch\s*\(\s*["']/""")),
    ]
    for label, pat in abs_pats:
        hit = pat.search(html)
        check(hit is None, "无绝对根路径 %s" % label,
              ("命中 " + repr(hit.group(0))) if hit else "")

    print("[3] 相对路径资源与 6 个接口")
    check('src="vendor/echarts.min.js"' in html, "以相对路径引入 vendor/echarts.min.js")
    # 2026-10-20：控制台已是页签，右上角冗余入口已删除
    check('href="admin"' not in html, '顶部无冗余控制台入口（已合并为页签）')
    for host in ("cdn.jsdelivr.net", "unpkg.com", "npmmirror", "cdnjs."):
        check(host not in html, "无 CDN 引用 %s" % host)

    apis = [
        ("api/summary", 'fetch("api/summary'),  # 2026-10-31：summary 已带 ?range= 参数
        ("api/account-sessions", 'fetch("api/account-sessions"'),
    ]
    for name, needle in apis:
        check(needle in html, "接口 %s 相对路径调用" % name, needle)
    # 2026-10-31 弃用：DSH 配置同步 / Agent Teams 死配置下线后，这些接口不得再被前端调用
    for dead in ("api/teams-config", "api/models-config", "api/sync-models",
                 "api/save-teams-config"):
        check(("fetch(\"" + dead) not in html, "已下线接口不再出现 %s" % dead)

    print("[4] 脚本配对与图表数量")
    n_open = html.count("<script")
    n_close = html.count("</script>")
    check(n_open == n_close, "<script> 与 </script> 配对", "%d != %d" % (n_open, n_close))
    # 2026-10-20：模板内共 3 段可执行脚本 —— ①主题前置(防闪白) ②vendor ECharts
    # ③主页面脚本；管理控制台的 adminBoot 是运行期注入的 JSON（非可执行），不计入。
    # 原断言写于 dashfix 之前（当时只有 2 段），属过期断言。
    n_exec = len(re.findall(r'<script(?![^>]*application/json)', html))
    check(n_exec == 3, "恰好 3 段可执行脚本（主题前置 + vendored echarts + 主脚本）",
          "实际 %d" % n_exec)

    charts = re.findall(r'<div class="[^"]*\bchart\b[^"]*"\s+id="(chart-[a-z0-9-]+)"[^>]*data-chart="1"', html)
    n_charts = len(charts)
    n_init = len(re.findall(r"echarts\.init", html))
    check(n_charts >= MIN_CHARTS, "图表容器数 >= %d" % MIN_CHARTS, "实际 %d" % n_charts)
    check(n_init >= n_charts, "echarts.init 次数 >= 图表数", "%d < %d" % (n_init, n_charts))
    check(len(set(charts)) == len(charts), "图表容器 id 唯一", ",".join(charts))
    # 2026-10-20 回归：折叠功能删除后不得残留任何调用（boot() 里漏删会导致整页不加载）
    check("applyFold(" not in html and "toggleFold(" not in html,
          "无已删除折叠函数的残留调用", "")
    for cid in charts:
        check(('"%s"' % cid) in html, "图表 %s 在脚本中被初始化/引用" % cid)

    print("[5] 编译与非联机自测")
    env = dict(os.environ)
    tmp = tempfile.mkdtemp(prefix="pool-dash-assets-")
    env["POOL_HOME"] = tmp
    env["POOL_ADMIN_HOME"] = tmp
    env["PYTHONPYCACHEPREFIX"] = os.path.join(tmp, "pycache")  # 不在仓库里落 .pyc
    try:
        rc = subprocess.call([sys.executable, "-m", "py_compile", DASH], env=env)
        check(rc == 0, "py_compile dashboard.py", "rc=%d" % rc)
        p = subprocess.run([sys.executable, DASH, "--selftest"],
                           cwd=HERE, env=env, capture_output=True, text=True, timeout=180)
        out = (p.stdout or "") + (p.stderr or "")
        check(p.returncode == 0, "dashboard.py --selftest rc=0", "rc=%d %s" % (p.returncode, out.strip()[-200:]))
        check("selftest-p3-ok" in out, "自测输出 selftest-p3-ok", out.strip()[-120:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("[6] /vendor/echarts.min.js 路由（本地静态资源，长缓存）")
    check('"/vendor/echarts.min.js"' in src, "路由表新增 GET /vendor/echarts.min.js")
    check("os.path.join(os.path.dirname(os.path.abspath(__file__))" in src,
          "从 dashboard.py 同目录解析 vendor 路径")
    check('"vendor", "echarts.min.js"' in src, "读取 vendor/echarts.min.js")
    check("application/javascript" in src, "Content-Type: application/javascript")
    check("max-age=" in src, "带长缓存 Cache-Control")
    check("import json, os" in src, "os 模块沿用原 import 行（未新增 import）")

    print("[7] 脚本引用的 DOM id 均存在于页面")
    defined = set(re.findall(r'id="([A-Za-z][-A-Za-z0-9_]*)"', html))
    used = set(re.findall(r'\$\("([A-Za-z][-A-Za-z0-9_]*)"\)', html))
    used |= set(re.findall(r'getElementById\("([A-Za-z][-A-Za-z0-9_]*)"\)', html))
    missing = sorted(used - defined)
    check(len(used) >= 30, "脚本引用的 id 数量合理", "实际 %d" % len(used))
    check(not missing, "无悬空 id 引用（含悬浮指标卡 #ktip）", "缺失 " + ",".join(missing))

    print()
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name, detail in FAILED:
            print("  FAIL %s %s" % (name, detail))
        return 1
    print("test_dashboard_assets: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
