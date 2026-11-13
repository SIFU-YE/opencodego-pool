#!/usr/bin/env python3
"""号池看板 v8 线上验证：Halo 登录 → 图表区截图 + console 错误捕获"""
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

OUT = "C:/Users/<user>/Desktop/system/deploy/opencodego-pool/pool-dash/shots"
errors = []
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_cookies(halo_cookies)
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(BASE + "/works/pool/", wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(6000)
    pg.screenshot(path=f"{OUT}/v8-online-1.png")
    pg.locator("text=按模型请求与").first.scroll_into_view_if_needed()
    pg.wait_for_timeout(1200)
    pg.screenshot(path=f"{OUT}/v8-online-2.png")
    body = pg.evaluate("document.body.innerText")
    print("环形仪表:", body.count("Rolling") >= 1 and "Monthly" in body)
    print("趋势图说明:", "个小时桶" in body)
    print("受限账号数显示:", "受限" in body)
    print("pageerrors:", errors if errors else "无")
    b.close()
