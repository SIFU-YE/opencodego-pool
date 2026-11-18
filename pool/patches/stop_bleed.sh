#!/bin/bash
# 止血：把 pool-affinity 切回 shadow（停止注入标签），并验证不再有标签注入
set -u
python3 - <<'PY'
import json
p = "/opt/litellm/pool-affinity.json"
d = json.load(open(p))
d["mode"] = "shadow"
d["updated"] = "2026-10-12 shadow (tag 路由实测不可靠，止血)"
json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
print("pool-affinity.json ->", json.dumps(d, ensure_ascii=False))
PY
sleep 7   # 等 sidecar 热读（TTL 5s）

MASTER=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')
S=http://127.0.0.1:4001

echo "=== 带树键请求（shadow 下应正常 200，且不再注入标签）==="
for p in /v1/chat/completions /v1/responses; do
  if [ "$p" = "/v1/chat/completions" ]; then
    b='{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"ping [pool-tree: shadow-check-01]"}],"max_tokens":5}'
  else
    b='{"model":"deepseek-v4.1-flash","input":"ping [pool-tree: shadow-check-01]","max_output_tokens":16}'
  fi
  c=$(curl -sS -m 60 -o /dev/null -w '%{http_code}' -X POST "$S$p" -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' -d "$b")
  echo "  $p -> http=$c"
done

echo
echo "=== 决策日志（injected 应为 false）==="
tail -3 /opt/litellm/pool-affinity-decisions.jsonl

echo
echo "=== 对照：手工带标签仍会 401（说明标签路由本身不可靠，与 sidecar 无关）==="
c=$(curl -sS -m 45 -o /dev/null -w '%{http_code}' -X POST "$S/v1/chat/completions" -H "Authorization: Bearer $MASTER" \
  -H 'Content-Type: application/json' -H 'x-litellm-tags: acct:OCG_KEY_6' \
  -d '{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":5}')
echo "  chat + acct:OCG_KEY_6 -> http=$c（401=标签路由不可靠；本轮已停止注入）"
