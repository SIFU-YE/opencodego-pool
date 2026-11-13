#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""列出号池各账号的身份与状态（不打印任何密钥值，只打印邮箱标识与配额状态）。"""
import json

env = {}
for line in open("/opt/litellm/.env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k] = v.strip().strip('"').strip("'")

share = {}
try:
    doc = json.load(open("/opt/litellm/routing-share.json", encoding="utf-8"))
    print(f"份额引擎: mode={doc.get('mode')} status={doc.get('status')} generated_at={doc.get('generated_at')}\n")
    for a in doc.get("accounts") or []:
        share[a.get("key_ref")] = a
except Exception as e:
    print("读取 routing-share.json 失败:", e)

try:
    cfg = open("/opt/litellm/config.yaml", encoding="utf-8").read()
except Exception:
    cfg = ""

lat = {"OCG_KEY_2": "11.8s / 10.5s", "OCG_KEY_4": "9.7s / 2.1s",
       "OCG_KEY_5": "55.0s / 33.1s  ← 慢", "OCG_KEY_6": "7.4s / 2.8s"}

print(f"{'账号':<12}{'邮箱标识':<30}{'在池':<6}{'份额':<9}{'余量最紧窗口':<26}{'实测延迟(2 次)'}")
for i in range(1, 7):
    kid = f"OCG_KEY_{i}"
    email = env.get(f"OCG_EMAIL_{i}", "(未配置)")
    a = share.get(kid)
    in_pool = "是" if f"os.environ/{kid}" in cfg else "否"
    if a:
        w = a.get("windows") or {}
        perm = a.get("share")
        perm_s = f"{perm:.3f}" if isinstance(perm, (int, float)) else "-"
        tight = "-"
        worst = 0.0
        for name, win in w.items():
            pct = (win or {}).get("percent")
            if isinstance(pct, (int, float)) and pct >= worst:
                worst = pct
                tight = f"{name}={pct}%({(win or {}).get('status')})"
    else:
        perm_s, tight = "-", "-"
    print(f"{kid:<12}{email:<30}{in_pool:<6}{perm_s:<9}{tight:<26}{lat.get(kid, '未测')}")

print("\n别名对照：mimo-v2.6-flash@a5s3  →  仅 OCG_KEY_5 的一个会话桶 deployment（钉号时按树键选到它就整条链路走它）")
print("会话标签：该账号在上游请求里带 x-opencode-session: dsh-acct-5（兜底）或 dsh-acct-5-s<K>（会话桶）")
