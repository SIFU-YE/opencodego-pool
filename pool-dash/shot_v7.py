#!/usr/bin/env python3
"""号池看板 v7 截图验证：账号·会话分配面板"""
import re, base64, urllib.request, urllib.parse, http.cookiejar
import os
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_der_public_key
from playwright.sync_api import sync_playwright

BASE = "https://<pool-host>"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.addheaders = [("User-Agent", "Mozilla/5.0 Chrome/126")]
html = op.open(BASE + "/login", timeout=30).read().decode()
csrf = re.search(r'name="_csrf"\s+value="([^"]+)"', html).group(1)
pub = load_der_public_key(base64.b64decode(re.search(r'const publicKey = "([^"]+)"', html).group(1).replace("\\/", "/")))
enc = base64.b64encode(pub.encrypt(os.environ.get("HALO_PASS","").encode(), padding.PKCS1v15())).decode()
data = urllib.parse.urlencode({"_csrf": csrf, "username": os.environ.get("HALO_USER",""), "password": enc}).encode()
class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
op2 = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NR())
try: op2.open(urllib.request.Request(BASE + "/login", data=data, method="POST"), timeout=30)
except Exception: pass
halo_cookies = [{"name": c.name, "value": c.value, "domain": "<pool-host>", "path": "/"} for c in cj]

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_cookies(halo_cookies)
    pg = ctx.new_page()
    pg.goto(BASE + "/works/pool/", wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(5000)
    # 滚到「账号 · 会话分配」区块
    el = pg.locator("text=账号 · 会话分配").first
    el.scroll_into_view_if_needed()
    pg.wait_for_timeout(1500)
    pg.screenshot(path="C:/tmp/pool-v7-acctsess.png")
    body = pg.evaluate("document.body.innerText")
    print("面板标题存在:", "账号 · 会话分配" in body)
    print("OCG_KEY_1 展示:", "OCG_KEY_1" in body)
    print("请求计数出现:", "请求 5" in body or "请求 4" in body)
    b.close()
