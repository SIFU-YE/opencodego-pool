#!/bin/bash
# 上生产：重启 litellm 加载别名配置 → 切 mode=full → 端到端验收（同树同账号）
set -u
MASTER=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')
S=http://127.0.0.1:4001
TREE="acc-alias-01"

echo "=== 1) 重启 litellm（加载别名配置；配置已无 tags/enable_tag_filtering）==="
docker restart litellm >/dev/null
ready=0
for i in $(seq 1 40); do
  c=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:4000/health/liveliness 2>/dev/null || echo 000)
  [ "$c" = "200" ] && { ready=1; echo "  ready after ${i}x3s"; break; }
  sleep 3
done
[ "$ready" = "1" ] || { echo "!! litellm 未就绪"; docker logs litellm 2>&1 | tail -15; exit 1; }

echo
echo "=== 2) GET /v1/models 仍应只有 5 个正式名（别名不外泄）==="
curl -sS -m 30 -o /tmp/am.json http://127.0.0.1:4001/v1/models -H "Authorization: Bearer $MASTER"
python3 -c "
import json
d = json.load(open('/tmp/am.json'))
ids = [m['id'] for m in d.get('data', [])]
print('  模型数:', len(ids))
print('  ', ids)
print('  含别名:', any('@a' in i for i in ids))
"

echo
echo "=== 3) 别名直连（经 sidecar）==="
for a in mimo-v2.6-flash@a5 deepseek-v4.1-flash@a2; do
  c=$(curl -sS -m 60 -o /dev/null -w '%{http_code}' -X POST "$S/v1/chat/completions" \
    -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$a\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":5}")
  echo "  $a -> http=$c"
done

echo
echo "=== 4) 切 mode=full（启用别名改写）==="
python3 - <<'PY'
import json
p = "/opt/litellm/pool-affinity.json"
d = json.load(open(p)); d["mode"] = "full"; d["updated"] = "2026-10-12 full(alias)"
json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
print("  mode =", d["mode"])
PY
sleep 8

echo
echo "=== 5) 同一会话树：chat+mimo-flash / responses+dsv4.1 / chat+mimo-pro ==="
post() { # path body label
  code=$(curl -sS -m 90 -o /tmp/a5.json -w '%{http_code}' -X POST "$S$1" \
    -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' -d "$2")
  model=$(python3 -c "
import json
try:
    print(json.load(open('/tmp/a5.json')).get('model', '?'))
except Exception:
    print('(非JSON)')
")
  printf '  %-28s http=%s  响应model=%s\n' "$3" "$code" "$model"
}
post /v1/chat/completions "{\"model\":\"mimo-v2.6-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"ping [pool-tree: $TREE]\"}],\"max_tokens\":5}" "chat + mimo-v2.6-flash"
post /v1/responses "{\"model\":\"deepseek-v4.1-flash\",\"input\":\"ping [pool-tree: $TREE]\",\"max_output_tokens\":16}" "responses + dsv4.1-flash"
post /v1/chat/completions "{\"model\":\"mimo-v2.6-pro\",\"messages\":[{\"role\":\"user\",\"content\":\"ping [pool-tree: $TREE]\"}],\"max_tokens\":5}" "chat + mimo-v2.6-pro"

echo
echo "=== 6) 决策与实际落号（model 字段里的 @aN 即账号）==="
grep "$TREE" /opt/litellm/pool-affinity-decisions.jsonl | tail -3
python3 - <<PY
import json, collections
tree = "$TREE"
accts = collections.Counter()
for line in open('/opt/litellm/pool-requests.jsonl', encoding='utf-8', errors='replace'):
    try: r = json.loads(line)
    except Exception: continue
    if r.get('sess') == tree:
        m = str(r.get('model') or '')
        accts[m] += 1
for m, n in accts.items():
    print(f"  model={m:30s} 请求={n}")
print("  唯一账号数:", len(accts), "（目标 1）")
PY
