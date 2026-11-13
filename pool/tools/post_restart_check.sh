#!/bin/bash
# 重启 DSH 后的一键验证：① 子代理是否上溯到队长会话树键 ② 该树是否落在同一账号 ③ 协议修复是否生效
# 用法：先派一个子代理（或跑一次团队），再执行本脚本。
set -u
LEADER="session-4e7011c7-2246-4e22-8f91-3ca4f93e6295"
SHORT="session-4e7011c7"          # 埋点 pool-requests.jsonl 的 sess 会被截断到 18 字符
LOG=/opt/litellm/pool-affinity-decisions.jsonl
REQ=/opt/litellm/pool-requests.jsonl

echo "=== ① 子代理是否上溯到队长会话（$LEADER）==="
n=$(grep -c "$LEADER" "$LOG" 2>/dev/null || echo 0)
if [ "$n" -gt 0 ]; then
  echo "  PASS  决策日志出现队长树键 $n 次"
  grep "$LEADER" "$LOG" | tail -3
else
  echo "  FAIL  未出现队长树键（说明插件仍是 v1.0.0：子代理标记的是自身 id）"
  echo "        最近决策："
  tail -3 "$LOG"
fi

echo
echo "=== ② 该树是否落在同一账号 ==="
python3 - "$LEADER" "$SHORT" <<'PY'
import collections, json, re, sys
full, short = sys.argv[1], sys.argv[2]
rows = []
for line in open('/opt/litellm/pool-requests.jsonl', encoding='utf-8', errors='replace'):
    try: r = json.loads(line)
    except Exception: continue
    if r.get('sess') in (full, short):
        rows.append(r)
if not rows:
    print("  WARN  该树暂无成功的请求记录（需要先跑通一次真实委派）")
    raise SystemExit
accts = collections.Counter()
for r in rows:
    m = re.search(r'@a(\d+)(?:s(\d+))?', str(r.get('model') or ''))
    if m:
        accts["a" + m.group(1)] += 1
    elif str(r.get('key_want') or '').startswith('OCG_KEY_'):
        accts["a" + r['key_want'].split('_')[-1]] += 1
    else:
        accts['unknown'] += 1
print(f"  请求={len(rows)}  账号分布={dict(accts)}")
known = {k for k in accts if k != 'unknown'}
print("  判定:", "PASS 同树同账号" if len(known) == 1 else f"FAIL 出现多个账号 {sorted(known)}")
PY

echo
echo "=== ③ 协议修复是否生效（mimo 走 completions / muse 走 responses）==="
MASTER=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')
for m in mimo-v2.6-flash mimo-v2.6-pro; do
  c=$(curl -sS -m 60 -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:4001/v1/chat/completions \
    -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":5}")
  echo "  chat + $m -> http=$c（期望 200）"
done
c=$(curl -sS -m 60 -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:4001/v1/responses \
  -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' \
  -d '{"model":"muse-spark-1.3-contributor","input":"ping","max_output_tokens":16}')
echo "  responses + muse-spark -> http=$c（期望 200）"
