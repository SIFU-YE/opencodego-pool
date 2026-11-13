#!/usr/bin/env python3
"""同步个人主页传送门：删死链卡片（reasonix / deepseek-web2api-free），插入号池看板卡片，更新描述。"""
import shutil

F = "/www/wwwroot/yesifu-home/index.html"
shutil.copy(F, F + ".bak-pool-20260925")
html = open(F, encoding="utf-8").read()

def find(s, sub, start=0):
    i = html.find(sub, start)
    assert i != -1, f"anchor not found: {sub[:60]}"
    return i

# --- 1. 删除 PT—02 OpenCode (reasonix, 已下线) 卡片：整行 ---
s = find(html, '\n          <a class="portal-card reveal" href="https://reasonix.<pool-host>/"')
e = find(html, "</a>", s) + len("</a>")
html = html[:s] + html[e:]

# --- 2. 删除 PT—03 DeepSeek Web2API Free (已删除) 卡片：同行内嵌 ---
s = find(html, '<a class="portal-card reveal" href="/works/deepseek-web2api-free/webui/"')
e = find(html, "</a>", s) + len("</a>")
html = html[:s] + html[e:]

# --- 3. 在 PT—10 DSH 卡片后插入 PT—11 号池看板 ---
s = find(html, '<a class="portal-card reveal" href="/works/dsh/"')
e = find(html, "</a>", s) + len("</a>")
new_card = ('\n          <a class="portal-card reveal" href="/works/pool/" aria-label="使用 Halo 登录后进入号池看板" target="_blank" rel="noopener">'
            '\n            <div class="portal-card__top"><span>PT\u201411 / LITELLM POOL</span><span class="portal-card__state">\u25c6 HALO LOGIN</span></div>'
            '\n            <h3>号池看板</h3><p>使用当前 Halo 登录会话查看 OpenCode Go 账号池与 LiteLLM 网关的模型、账号与实时请求统计，未登录时自动进入 Halo 登录页。</p>'
            '\n            <div class="portal-card__foot"><span>/WORKS/POOL<br>HALO SESSION ONLY</span><b>\u21d7</b></div>'
            '\n          </a>')
html = html[:e] + new_card + html[e:]

# --- 4. 更新区块描述 ---
old_desc = "Agent Team、OpenCode、DeepSeek dsh 与 DeepSeek Web2API Free 均通过统一登录认证后访问。"
new_desc = "Agent Team、DeepSeek dsh 与号池看板均通过统一登录认证后访问。"
assert old_desc in html, "desc anchor not found"
html = html.replace(old_desc, new_desc)

open(F, "w", encoding="utf-8").write(html)
print("portal-card count =", html.count('class="portal-card'))
print("works/pool links   =", html.count('href="/works/pool/"'))
print("reasonix left      =", html.count("reasonix.<pool-host>"))
print("web2api left       =", html.count("deepseek-web2api-free"))
print("OK")
