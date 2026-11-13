#!/usr/bin/env python3
"""DSH NEXT 2.0.14-next 反代认证 cookie 续期（每月执行）。
2026-09-25 起服务器运行 DSH NEXT（社区版桌面壳 NEXT 通道，内嵌官方 Harness v0.1.7-rc.1）。
NEXT 与 stable 差异（逆向 lib/desktop-browser-access + token 流实测验证）：
  1. authority 固定为 127.0.0.1（nginx 反代时 proxy_set_header Host "127.0.0.1"，
     回环恒受 connection 信任，绕过 NEXT 每次启动用 DSH_NEXT_TRUSTED_HOSTS
     覆盖 connection.patch 的行为——该 patch 只含 IPv4 字面量，无法加域名）；
  2. 签名消息不含 "v1." 前缀：sig = HMAC-SHA256(decoded_secret, payload_b64)，
     stable 旧方案是 HMAC(decoded_secret, "v1." + payload_b64)，勿混用；
  3. cookie 名 marker = "dsh-auth-" + b64url(sha256("127.0.0.1"))；
  4. payload 结构不变：{"version":1,"authority","issuedAt","expiresAt"}，窗口 ≤30 天。
脚本同时替换 nginx yesifu.conf 中全部 dsh-auth 注入行，新 cookie 前置。
NEXT 偏好（browserAccess 等）在 /root/.dsh/desktop-preferences.json，与本脚本无关。"""
import hmac, hashlib, json, base64, time, re

AUTHORITY = "127.0.0.1"
NGINX_CONF = "/www/server/nginx/conf/vhost/yesifu.conf"
CREDENTIALS = "/root/.dsh/.credentials.yaml"
COOKIE_FILE = "/root/.dsh/browser-cookie.txt"

def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

# 1. marker 按 authority 动态计算（不硬编码，authority 改动自动跟随）
cookie_marker = "dsh-auth-" + b64u(hashlib.sha256(AUTHORITY.encode()).digest())

# 2. 读取 secret（base64url 文本 -> 解码为 32 字节，HMAC key 必须用解码后的形式）
creds = open(CREDENTIALS).read()
i = creds.find("secret: ") + len("secret: ")
secret_txt = creds[i:].split("\n", 1)[0].strip()
secret = base64.urlsafe_b64decode(secret_txt + "=" * (-len(secret_txt) % 4))

# 3. 生成 30 天窗口签名 cookie（NEXT 方案：HMAC 消息不含 "v1." 前缀）
now = int(time.time() * 1000)
payload = {"version": 1, "authority": AUTHORITY, "issuedAt": now, "expiresAt": now + 30 * 86400000}
body = b64u(json.dumps(payload, separators=(",", ":")).encode())
sig = b64u(hmac.new(secret, body.encode(), hashlib.sha256).digest())
cookie = "v1.%s.%s" % (body, sig)
open(COOKIE_FILE, "w").write(cookie)
print("cookie issued for authority", AUTHORITY, "valid 30 days, len:", len(cookie))

# 4. 替换 nginx 全部注入行：新 cookie 前置 + 保留浏览器原 cookie
# 2026-09-25 起同时覆盖 private/dsh-root-proxy.conf（/sidebar、/open-in-app 等根路径
# location 共享 include——NEXT 升级时曾遗漏导致 sidebar_open 开关保存 403 forbidden）
NGINX_FILES = [NGINX_CONF, "/www/server/nginx/conf/vhost/private/dsh-root-proxy.conf"]
new_line = 'proxy_set_header Cookie "' + cookie_marker + "=" + cookie + '; $http_cookie";'
pattern = re.compile(r'proxy_set_header Cookie "[^"]*dsh-auth-[^"]*";')
total = 0
for conf in NGINX_FILES:
    try:
        src = open(conf).read()
    except FileNotFoundError:
        print("skip (not found):", conf)
        continue
    src, n = pattern.subn(new_line, src)
    if n:
        open(conf, "w").write(src)
    print(conf, "lines replaced:", n)
    total += n
assert total >= 1, "no cookie injection lines found"
leftover = 0
for conf in NGINX_FILES:
    try:
        leftover += len(re.findall(r'proxy_set_header Cookie "\$http_cookie; dsh-auth-', open(conf).read()))
    except FileNotFoundError:
        pass
print("old-style append lines left:", leftover)
print("OK")
