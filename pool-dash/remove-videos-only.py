#!/usr/bin/env python3
"""仅删除 hero-film 轮播的 6 个 <video> 元素，UI/计数器/圆点/脚本代码全部保留。
为防止空 clip 导致 JS 报错，在轮播脚本初始化处加一行守卫（可逆，代码仍在）。"""
import shutil

F = "/www/wwwroot/yesifu-home/index.html"
# 1. 从最近备份恢复（含 UI 与脚本的完整版本）
shutil.copy(F + ".bak-noreel-20260925", F)
html = open(F, encoding="utf-8").read()
orig_len = len(html)

# 2. 仅删除 6 个 <video class="hero-film__clip">...</video>
count = 0
while True:
    s = html.find('<video class="hero-film__clip')
    if s == -1:
        break
    e = html.find("</video>", s)
    assert e != -1
    e += len("</video>")
    ls = html.rfind("\n", 0, s) + 1
    if html[ls:s].strip() == "":
        s = ls
    html = html[:s] + html[e:]
    count += 1
assert count == 6, f"expected 6 videos, removed {count}"

# 3. 轮播脚本空 clip 守卫（一行，其余代码不动）
anchor = "var index=0,timer=0,userPaused=reduced;"
assert anchor in html, "script anchor not found"
html = html.replace(anchor, anchor + "\n      if(!clips.length){return;}", 1)

open(F, "w", encoding="utf-8").write(html)
print("videos removed :", count)
print("size           :", orig_len, "->", len(html), f"(-{orig_len - len(html)})")
print("ui/script kept : data-reel =", html.count("data-reel"), ", heroFilm script =", html.count("getElementById('heroFilm')"))
print("OK")
