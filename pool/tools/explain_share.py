#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解释份额来源：份额 ∝ pace = min_w(rem/days)。打印各账号的 rem/days/pace/share。
（只读 routing-share.json，不打印密钥）"""
import json
from datetime import datetime, timezone

doc = json.load(open("/opt/litellm/routing-share.json", encoding="utf-8"))
now = datetime.now(timezone.utc)
print(f"份额引擎 mode={doc.get('mode')}  status={doc.get('status')}  生成于 {doc.get('generated_at')}")
print(f"参数: {json.dumps(doc.get('params'), ensure_ascii=False)}")
print()
print(f"{'账号':<12}{'share':<9}{'monthly 已用':<14}{'monthly 剩余':<14}{'距重置(天)':<12}{'pace=rem/days':<15}{'weekly':<22}")
rows = []
for a in doc.get("accounts") or []:
    w = a.get("windows") or {}
    m, wk = w.get("monthly") or {}, w.get("weekly") or {}
    mp = m.get("percent")
    rem = (100 - mp) if isinstance(mp, (int, float)) else None
    days = m.get("days")
    pace = (rem / days) if (rem is not None and days) else None
    rows.append((a.get("key_ref"), a.get("share"), mp, rem, days, pace,
                 f"{wk.get('percent')}% status={wk.get('status')}"))
for r in sorted(rows, key=lambda x: -(x[1] or 0)):
    k, sh, mp, rem, days, pace, wk = r
    f = lambda v, n=2: ("-" if v is None else f"{v:.{n}f}")
    print(f"{k:<12}{f(sh,3):<9}{f(mp,1)+'%':<14}{f(rem,1)+'%':<14}{f(days):<12}{f(pace,2):<15}{wk:<22}")

print("\n说明：share_i ∝ pace_i；pace 越大（剩余额度多 + 距重置近）→ 份额越高 → 引擎越想让它尽快烧额度。")
print("     算法里**没有任何延迟/成功率信号**，所以「慢账号拿最高份额」在设计上是可能的。")
