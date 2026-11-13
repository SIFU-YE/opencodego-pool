#!/bin/bash
# 现场诊断：S5(key_want) 与 S5b(reason) 是否都在，埋点记录里字段如何
set -u
MASTER=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')
TREE="diag-$(date +%H%M%S)"
echo "树键 = $TREE"

echo "=== live 文件里的补丁痕迹 ==="
grep -n 'treepin-s5\b\|treepin-s5b\|md\["key_want"\]\|pool_alias' /opt/litellm/session-sidecar.py | head -8

echo
echo "=== 发一条带树键的 chat 请求 ==="
code=$(curl -sS -m 60 -o /tmp/dg.json -w '%{http_code}' -X POST http://127.0.0.1:4001/v1/chat/completions \
  -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' \
  -d "{\"model\":\"mimo-v2.6-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"ping [pool-tree: $TREE]\"}],\"max_tokens\":5}")
echo "  http=$code"
python3 -c "
import json
try: print('  响应 model =', json.load(open('/tmp/dg.json')).get('model'))
except Exception as e: print('  解析失败', e)
"

echo
echo "=== 决策日志（最后一条）==="
tail -1 /opt/litellm/pool-affinity-decisions.jsonl

echo
echo "=== 埋点记录（该树键，完整字段）==="
python3 - "$TREE" <<'PY'
import json, sys
tree = sys.argv[1]
rows = []
for line in open('/opt/litellm/pool-requests.jsonl', encoding='utf-8', errors='replace'):
    try: r = json.loads(line)
    except Exception: continue
    if r.get('sess') == tree:
        rows.append(r)
for r in rows[-2:]:
    print(" ", json.dumps(r, ensure_ascii=False)[:400])
if not rows:
    print("  （无记录）")
PY
