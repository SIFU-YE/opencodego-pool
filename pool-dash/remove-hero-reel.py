#!/usr/bin/env python3
"""删除个人主页 hero-film 轮播视频：6 个 video、轮播 UI、状态标签、轮播脚本。
保留 .hero-film 容器（自带径向渐变背景）与 .hero-film__veil 遮罩，布局与视觉质感不变。"""
import shutil

F = "/www/wwwroot/yesifu-home/index.html"
shutil.copy(F, F + ".bak-noreel-20260925")
html = open(F, encoding="utf-8").read()
orig_len = len(html)

# --- 1. 删除 6 个 <video class="hero-film__clip">...</video> ---
count = 0
while True:
    s = html.find('<video class="hero-film__clip')
    if s == -1:
        break
    e = html.find("</video>", s)
    assert e != -1
    e += len("</video>")
    # 吃掉前面的缩进空白与后面的换行
    ls = html.rfind("\n", 0, s) + 1
    if html[ls:s].strip() == "":
        s = ls
    html = html[:s] + html[e:]
    count += 1
assert count == 6, f"expected 6 videos, removed {count}"

# --- 2. 删除 hero-film__ui 整块（含 counter/nav/tools） ---
s = html.find('<div class="hero-film__ui">')
assert s != -1, "ui block not found"
e = html.find('<span class="hero-film__status"', s)
assert e != -1
html = html[:s] + html[e:]

# --- 3. 删除 hero-film__status 状态标签 ---
s = html.find('<span class="hero-film__status"')
assert s != -1, "status span not found"
e = html.find("</span>", s)
assert e != -1
e += len("</span>")
html = html[:s] + html[e:]

# --- 4. 删除轮播 <script> 块（含 heroFilm 引用的 IIFE） ---
anchor = html.find("var film=document.getElementById('heroFilm')")
assert anchor != -1, "reel script not found"
s = html.rfind("<script>", 0, anchor)
e = html.find("</script>", anchor)
assert s != -1 and e != -1
e += len("</script>")
html = html[:s] + html[e:]

open(F, "w", encoding="utf-8").write(html)
print("videos removed :", count)
print("size           :", orig_len, "->", len(html), f"(-{orig_len - len(html)})")
print("clip refs left :", html.count("hero-film__clip"))
print("reel refs left :", html.count("data-reel"))
print("OK")
