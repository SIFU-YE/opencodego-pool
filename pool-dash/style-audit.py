#!/usr/bin/env python3
"""号池驾驶舱样式自检（2026-10-19 dashfix 版）：多视口 × 7 页签 × 双主题跑
几何 / 裁切 / 图表渲染 / 控制台断言。退出码非零 = 有异常。

与 v12 单页面板版自检的差异（v12 检查全部保留，另**新增**页签层检查）：
- v12 检查保留：`.panel` 面板数 ≥8、`#kpis` KPI 卡 ≥6（`.fold` 折叠控件已于
  2026-10-20 看板瘦身中移除，改为断言**不得再出现**）、
  `#acctDetail .det` 账号卡 ≥1、`.chart` 容器有 canvas 且高度合理、
  无横向溢出、无越界元素、无文本被裁、表头不折行、无控制台错误；
- 新增页签层：6 个页签、逐页签点击后 `.tpane.on` 必须对上、每个页签内
  都不得横向溢出 / 不得有控制台错误、图表容器必须有 canvas（ECharts）；
- 新增双主题：`data-theme` 合法、`.t-dark`/`.t-light` **只显示一个**、
  切换后主题翻转且 localStorage 记忆、浅色主题下同样不溢出不报错；
- 新增管理控制台页签：`.adm` 片段已注入、`#adminBoot` bootstrap 在、
  `#capChips` 已由 adminInit 渲染出内容；
- 新增 canvas 缩放：ECharts canvas 的 CSS 宽度必须等于 `.chart` 容器宽度
  （取代 v12 手绘 SVG 的 `svg.trend` 缩放断言）。

用法:
    python3 style-audit.py [url] [--shot DIR]
"""
import json
import sys

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:4010/"
SHOTS = None
_args = sys.argv[1:]
_i = 0
while _i < len(_args):
    if _args[_i] == "--shot" and _i + 1 < len(_args):
        SHOTS = _args[_i + 1]
        _i += 2
    else:
        URL = _args[_i]
        _i += 1

# 验收基准（2560×1440 / 1920×1080）排最前，其后是常规档位与窄屏兜底
VIEWPORTS = [(2560, 1440), (1920, 1080), (1440, 1000), (1280, 900),
             (1024, 800), (768, 900), (390, 844)]

TABS = ["overview", "pool", "usage", "perf", "routing", "admin"]

READY_JS = """
() => {
  const ok = e => e && e.innerHTML.length > 40;
  const pill = document.getElementById('refresh');
  return !!document.querySelector('.tpane.on') &&
         !!document.querySelector('#tabs .tab') &&
         ok(document.getElementById('kpis')) &&
         ok(document.getElementById('acctDetail')) &&
         !!pill && /更新于|加载失败/.test(pill.textContent);
}
"""

CHARTS_READY_JS = """
() => {
  const on = document.querySelector('.tpane.on');
  if (!on) return false;
  const cs = [...on.querySelectorAll('.chart')].filter(c => c.getBoundingClientRect().width >= 40);
  if (!cs.length) return true;
  for (const c of cs) {
    const cv = c.querySelector('canvas');
    if (!cv) return false;
    if (Math.abs(cv.getBoundingClientRect().width - c.getBoundingClientRect().width) > 2) return false;
    if (c.getBoundingClientRect().height < 60) return false;
  }
  return true;
}
"""


def wait_charts_ready(pg, fallback_ms=750, budget_ms=5000):
    """等到「当前可见页签里的图表都画出来了」再量，替代纯固定睡眠。

    2026-10-19（flake 收尾）：逐页签那档原本固定 `wait_for_timeout(750)`，
    实测最坏就绪 343.5ms → 余量仅 2.18×，在极端 CPU 竞争（并发 3~4 个浏览器）下会被击穿，
    表现为偶发 FAIL（不是页面缺陷）。这里改成「等条件成立」，**判定标准一字未动**：
    等不到就按原样继续，由后面的 CHECK 判 FAIL（不掩盖真问题）。
    """
    try:
        pg.wait_for_function(CHARTS_READY_JS, timeout=budget_ms)
        pg.wait_for_timeout(150)      # 留一帧给 ECharts 收尾
    except Exception:                 # noqa: BLE001 —— 超时不是错误，交给 CHECK 判定
        pg.wait_for_timeout(fallback_ms)


ALERT_READY_JS = """
() => {
  const bar = document.getElementById('alertBar');
  if (!bar) return true;
  const t = (bar.textContent || '').trim();
  if (!t) return false;                          // 还是空的：renderRouting 没跑
  if (bar.querySelector('.chip')) return true;   // 有告警 chip —— 就是要量它
  return /未触发|数据未就绪/.test(t);             // 明确的终止态：无 chip 可量
}
"""


def wait_alert_ready(pg, budget_ms=6000):
    """等 routing 页签的 `#alertBar` 渲染完再量几何。

    2026-10-20（独立验收抓到）：`#alertBar` 的内容由 `renderRouting()` 异步填，
    固定等待下有时 chips 还没出来 → `越界元素` 这条断言**变成假绿**
    （实测 6 次里 2 次 chips=0 直接 PASS，其余 4 次报真实越界）。
    这里只等「就绪」，**阈值一字不动**：等不到照常量，由 CHECK 判 FAIL。
    返回 (ok, chips)：chips 用于在日志里证明「这次不是空跑」。
    """
    ok = True
    try:
        pg.wait_for_function(ALERT_READY_JS, timeout=budget_ms)
        pg.wait_for_timeout(120)      # 留一帧给布局
    except Exception:                 # noqa: BLE001
        ok = False
    try:
        chips = pg.evaluate("() => document.querySelectorAll('#alertBar .chip').length")
    except Exception:                 # noqa: BLE001
        chips = -1
    return ok, chips


CHECK = r"""
() => {
  const o = {notes: [], wide: [], clipped: [], charts: [], canvas: [],
             kpis: 0, accts: 0, panels: 0, folds: 0, tabs: 0, tpane: "",
             theme: "", labels: [], admin: false, overflow: 0};
  const vw = window.innerWidth;
  o.overflow = document.documentElement.scrollWidth - vw;
  o.tabs = document.querySelectorAll('#tabs .tab').length;
  const on = document.querySelector('.tpane.on');
  o.tpane = on ? on.id : "";
  o.theme = document.documentElement.getAttribute('data-theme') || "";
  ['t-dark', 't-light'].forEach(c => {
    const e = document.querySelector('#themeBtn .' + c);
    if (e && e.getClientRects().length) o.labels.push(c);
  });
  o.panels = document.querySelectorAll('.panel').length;
  o.folds = document.querySelectorAll('.fold, #foldAll').length;
  o.kpis = document.querySelectorAll('#kpis > *').length;
  o.accts = document.querySelectorAll('#acctDetail .det').length;
  // 管理面片段常驻 DOM，但只在 admin 页签可见 —— 这里只做记录，判定放在 admin 页签那一步
  o.admin = !!(on && on.querySelector('.adm')) && !!document.getElementById('adminBoot');

  if (!on) { o.notes.push('没有可见的页签面板（.tpane.on）'); return o; }

  // ---- 可见页签内的图表：必须有 ECharts canvas，且高度合理 ----
  on.querySelectorAll('.chart').forEach(c => {
    const r = c.getBoundingClientRect();
    if (r.width < 40) return;
    const hasCanvas = !!c.querySelector('canvas');
    const hasSvg = !!c.querySelector('svg');
    if (!hasCanvas && !hasSvg && (c.innerHTML || '').length < 40)
      o.charts.push('空图表 ' + (c.id || c.className).slice(0, 24));
    else if (r.height < 60)
      o.charts.push('图表过矮 ' + (c.id || c.className).slice(0, 24) + ' h=' + Math.round(r.height));
  });

  // ---- canvas 缩放：CSS 宽度必须等于容器宽度（取代 v12 的 svg.trend 缩放断言） ----
  on.querySelectorAll('.chart canvas').forEach(cv => {
    const r = cv.getBoundingClientRect();
    if (r.width < 40) return;
    const host = cv.closest('.chart');
    if (!host) return;
    const hw = host.getBoundingClientRect().width;
    if (Math.abs(r.width - hw) > 2)
      o.canvas.push((host.id || 'chart') + ' ' + Math.round(r.width) + '/' + Math.round(hw));
  });

  // ---- 几何：可见页签 + 页头里不得有越界 / 裁切 ----
  const roots = [document.querySelector('header'), document.querySelector('.tabs'), on].filter(Boolean);
  roots.forEach(root => root.querySelectorAll('*').forEach(el => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || s.position === 'fixed') return;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return;
    if (r.right > vw + 2) {
      let scrollable = false;
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
        const ps = getComputedStyle(p);
        if (ps.overflowX === 'auto' || ps.overflowX === 'scroll') { scrollable = true; break; }
      }
      if (!scrollable && o.wide.length < 5)
        o.wide.push((el.tagName + '.' + (el.className || '')).slice(0, 36) + ' w=' + Math.round(r.width));
    }
    if (el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 2 &&
        s.overflowX === 'visible' && el.children.length === 0 && o.clipped.length < 5)
      o.clipped.push((el.className || el.tagName).slice(0, 24) + ':' + (el.textContent || '').trim().slice(0, 16));
  }));

  const thTall = [];
  on.querySelectorAll('th').forEach(t => {
    const h = t.getBoundingClientRect().height;
    if (h > 40 && thTall.length < 3) thTall.push(t.textContent.trim().slice(0, 8) + ' h=' + Math.round(h));
  });
  if (thTall.length) o.notes.push('表头折行 ' + JSON.stringify(thTall));

  if (o.tabs !== 6) o.notes.push('页签数不等于 6（DSH 配置已并入控制台）: ' + o.tabs);
  if (o.theme !== 'dark' && o.theme !== 'light') o.notes.push('主题属性非法: ' + o.theme);
  if (o.labels.length !== 1) o.notes.push('主题按钮可见标签数 != 1: ' + JSON.stringify(o.labels));
  if (o.kpis < 6) o.notes.push('KPI 卡不足: ' + o.kpis);
  if (o.accts < 1) o.notes.push('账号卡未渲染');
  if (o.panels < 8) o.notes.push('面板数不足: ' + o.panels);
  if (o.folds > 0) o.notes.push('折叠控件应已移除（2026-10-20 瘦身）');
  if (o.charts.length) o.notes.push('图表问题 ' + JSON.stringify(o.charts.slice(0, 4)));
  if (o.canvas.length) o.notes.push('canvas 缩放异常 ' + JSON.stringify(o.canvas.slice(0, 3)));
  if (o.overflow > 2) o.notes.push('页面横向溢出 +' + o.overflow + 'px');
  if (o.wide.length) o.notes.push('越界元素 ' + JSON.stringify(o.wide));
  if (o.clipped.length) o.notes.push('文本被裁 ' + JSON.stringify(o.clipped));
  return o;
}
"""

ADMIN_JS = """
() => {
  const caps = document.getElementById('capChips');
  return {adm: !!document.querySelector('.tpane.on .adm'),
          boot: !!document.getElementById('adminBoot'),
          caps: caps ? caps.innerHTML.length : 0};
}
"""

fails = 0
checked = 0


def launch(pw):
    """优先用 playwright 自带 chromium；没有就退回系统 Chrome/Edge。

    服务器上装了 ms-playwright chromium；开发机（Windows）通常只有系统 Chrome，
    这条兜底能让自检在两边都跑得起来（并打印实际用的通道）。
    """
    last = ""
    for kw in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            b = pw.chromium.launch(headless=True, **kw)
            print("browser:", kw.get("channel") or "bundled-chromium")
            return b
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:160]
    raise SystemExit("无法启动任何浏览器：%s\n请先 `python3 -m playwright install chromium`" % last)


def report(tag, r, errs, extra=None):
    """把一次 CHECK 结果整理成问题清单；返回问题条数。"""
    problems = list(r["notes"]) + list(extra or [])
    if errs:
        problems.append("控制台错误 " + json.dumps(errs[:3], ensure_ascii=False))
    status = "PASS" if not problems else "FAIL"
    print(f"[{status}] {tag}" + ("" if not problems else "  " + " ; ".join(problems)))
    return 0 if not problems else 1


with sync_playwright() as pw:
    b = launch(pw)
    for W, H in VIEWPORTS:
        pg = b.new_page(viewport={"width": W, "height": H})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:180]))
        pg.on("console", lambda m: errs.append(m.text[:150]) if m.type == "error" else None)
        try:
            pg.goto(URL, wait_until="domcontentloaded", timeout=45000)
            try:
                pg.wait_for_function(READY_JS, timeout=60000)
            except Exception:  # noqa: BLE001
                print(f"[WARN] {W}x{H} 首屏数据 60s 内未就绪，按骨架判读")
            pg.wait_for_timeout(1200)
            wait_charts_ready(pg, fallback_ms=0)   # 首屏图表就绪再量（余量 12.77×，仍补一层保险）
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {W}x{H} 页面异常：{str(exc)[:140]}")
            fails += 1
            pg.close()
            continue

        # ---- 骨架（默认总览页签）----
        checked += 1
        r = pg.evaluate(CHECK)
        fails += report(f"{W}x{H} · 骨架/总览", r, errs)
        errs = []
        if SHOTS and W in (1920, 2560):
            pg.screenshot(path=f"{SHOTS}/dashfix-{W}x{H}-overview.png", full_page=False)

        # ---- 逐页签 ----
        for tab in TABS:
            checked += 1
            try:
                pg.click(f'.tab[data-tab="{tab}"]')
            except Exception as exc:  # noqa: BLE001
                print(f"[FAIL] {W}x{H} · {tab}  点不到页签: {str(exc)[:110]}")
                fails += 1
                continue
            pg.wait_for_timeout(750)
            wait_charts_ready(pg, fallback_ms=0)   # 只补「等就绪」，固定 750ms 已睡过
            if tab == "routing":
                # 2026-10-20：routing 的 #alertBar 是异步填的，不等就量会「假绿」
                ok_bar, chips = wait_alert_ready(pg)
                if not ok_bar:
                    print(f"[WARN] {W}x{H} · routing  #alertBar 6s 内未渲染完成，按当前状态判读")
                print(f"[INFO] {W}x{H} · routing  alertBar chips={chips}")
            r = pg.evaluate(CHECK)
            extra = []
            if r["tpane"] != "tab-" + tab:
                extra.append("页签切换未生效 %s != %s" % (r["tpane"], "tab-" + tab))
            fails += report(f"{W}x{H} · {tab}", r, errs, extra)
            errs = []
            if tab == "admin":
                a = pg.evaluate(ADMIN_JS)
                checked += 1
                probs = []
                if not a["adm"]:
                    probs.append("缺 .adm 片段")
                if not a["boot"]:
                    probs.append("缺 #adminBoot bootstrap")
                if a["caps"] < 20:
                    probs.append("能力徽标未渲染（adminInit 未触发？）len=%d" % a["caps"])
                status = "PASS" if not probs else "FAIL"
                if probs:
                    fails += 1
                print(f"[{status}] {W}x{H} · admin 片段挂载点" + ("" if not probs else "  " + " ; ".join(probs)))
            if SHOTS and W in (1920, 2560):
                pg.screenshot(path=f"{SHOTS}/dashfix-{W}x{H}-{tab}.png", full_page=False)

        # ---- 双主题：切换后必须翻转、只显示一个标签、不溢出不报错 ----
        checked += 1
        before = pg.evaluate("document.documentElement.getAttribute('data-theme')")
        pg.click("#themeBtn")
        pg.wait_for_timeout(800)
        r = pg.evaluate(CHECK)
        extra = []
        if r["theme"] == before:
            extra.append("主题未翻转（仍为 %s）" % r["theme"])
        # .t-dark = 深色主题下可见（文案「浅色」，点它切到浅色）；
        # .t-light = 浅色主题下可见（文案「深色」）。浅色下可见的必须是 .t-light。
        if r["labels"] != ["t-light"]:
            extra.append("浅色下可见标签异常 %s" % json.dumps(r["labels"]))
        fails += report(f"{W}x{H} · 浅色主题", r, errs, extra)
        errs = []
        if SHOTS and W in (1920, 2560):
            pg.screenshot(path=f"{SHOTS}/dashfix-{W}x{H}-light.png", full_page=False)

        # ---- 浅色主题下再逐页签扫一遍溢出（最密的 admin 页签也在内） ----
        for tab in TABS:
            checked += 1
            try:
                pg.click(f'.tab[data-tab="{tab}"]')
            except Exception as exc:  # noqa: BLE001
                print(f"[FAIL] {W}x{H} · 浅色/{tab}  点不到页签: {str(exc)[:110]}")
                fails += 1
                continue
            pg.wait_for_timeout(420)
            # 浅色档只量溢出/页签归属，但页签切换 + 重渲染同样需要「等就绪」而非固定睡
            try:
                pg.wait_for_function(
                    "()=>{const on=document.querySelector('.tpane.on');"
                    "return !!on && on.id==='tab-%s';}" % tab, timeout=3000)
            except Exception:  # noqa: BLE001 —— 超时交给下面的判定
                pass
            rr = pg.evaluate(
                "()=>({o:document.documentElement.scrollWidth-window.innerWidth,"
                "t:(document.querySelector('.tpane.on')||{}).id||''})")
            probs = []
            if rr["o"] > 2:
                probs.append("浅色下横向溢出 +%d" % rr["o"])
            if rr["t"] != "tab-" + tab:
                probs.append("页签切换未生效 " + rr["t"])
            if errs:
                probs.append("浅色下控制台错误 " + json.dumps(errs[:2], ensure_ascii=False))
                errs = []
            status = "PASS" if not probs else "FAIL"
            if probs:
                fails += 1
            print(f"[{status}] {W}x{H} · 浅色/{tab}" + ("" if not probs else "  " + " ; ".join(probs)))
        pg.close()
    b.close()

print(f"RESULT: {'ALL PASS' if not fails else str(fails) + ' check(s) FAILED'} · {checked} 项检查")
sys.exit(1 if fails else 0)
