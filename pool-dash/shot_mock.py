#!/usr/bin/env python3
"""Mock 看板视觉验证：全页截图 + console 错误捕获"""
import sys
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:4011/"
OUT = sys.argv[2] if len(sys.argv) > 2 else "C:/Users/<user>/Desktop/system/deploy/opencodego-pool/pool-dash/shots"
import pathlib; pathlib.Path(OUT).mkdir(parents=True, exist_ok=True)

errors = []
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", args=["--no-sandbox"])
    pg = b.new_context(viewport={"width": 1440, "height": 900}).new_page()
    pg.on("console", lambda m: errors.append(m.text) if m.type in ("error",) else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(URL, wait_until="networkidle", timeout=45000)
    pg.wait_for_timeout(2500)
    pg.screenshot(path=f"{OUT}/v8-1-top.png")
    pg.locator("text=账号 · 会话分配").first.scroll_into_view_if_needed()
    pg.wait_for_timeout(900)
    pg.screenshot(path=f"{OUT}/v8-2-sessions.png")
    pg.locator("text=模型选择").first.scroll_into_view_if_needed()
    pg.wait_for_timeout(900)
    pg.screenshot(path=f"{OUT}/v8-3-providers.png")
    b.close()
print("console errors:", errors if errors else "无")
print("done ->", OUT)
