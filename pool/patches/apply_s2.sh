#!/bin/bash
# S2 上生产 + 端到端验证（合成树键）：重启 litellm、冒烟、切 full、验证同树同账号
set -u
MASTER=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')
API=http://127.0.0.1:4001/v1/chat/completions          # 经 sidecar

echo "=== 1) 重启 litellm 加载带 tags 的配置 ==="
docker restart litellm >/dev/null
ready=0
for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:4000/health/liveliness 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then ready=1; echo "litellm ready after ${i}x3s"; break; fi
  sleep 3
done
[ "$ready" = "1" ] || { echo "!! litellm 未就绪"; docker logs litellm 2>&1 | tail -20; exit 1; }

echo
echo "=== 2) 生产冒烟：未带标签（主流量形态）==="
c=$(curl -sS -m 120 -o /tmp/s2a.out -w '%{http_code}' -X POST "$API" -H "Authorization: Bearer $MASTER" \
  -H 'Content-Type: application/json' -d '{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":5}')
echo "http=$c"; head -c 160 /tmp/s2a.out; echo

echo
echo "=== 3) 切换 pool-affinity.json → full ==="
python3 - <<'PY'
import json
p = "/opt/litellm/pool-affinity.json"
d = json.load(open(p))
d["mode"] = "full"
d["updated"] = "2026-10-12 full"
json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
print(json.dumps(d, ensure_ascii=False))
PY
sleep 7   # 等 sidecar 热读（TTL 5s）

echo
echo "=== 4) 合成树键请求：synth-tree-01（3 个不同模型，应落同一账号）==="
for m in mimo-v2.6-flash mimo-v2.6-pro deepseek-v4.1-flash; do
  c=$(curl -sS -m 120 -o /tmp/s2b.out -w '%{http_code}' -X POST "$API" -H "Authorization: Bearer $MASTER" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"ping [pool-tree: synth-tree-01]\"}],\"max_tokens\":5}")
  echo "  $m -> http=$c"
done
echo "=== 4b) 对照树键：synth-tree-02 ==="
for m in mimo-v2.6-flash mimo-v2.6-pro; do
  c=$(curl -sS -m 120 -o /tmp/s2c.out -w '%{http_code}' -X POST "$API" -H "Authorization: Bearer $MASTER" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"ping [pool-tree: synth-tree-02]\"}],\"max_tokens\":5}")
  echo "  $m -> http=$c"
done

echo
echo "=== 5) sidecar 决策（tree-affinity 行）==="
journalctl -u litellm-session-sidecar --since '-3min' --no-pager 2>/dev/null | grep -E 'tree-affinity|tree-shadow' | tail -8
echo
echo "=== 6) 决策文件 ==="
tail -8 /opt/litellm/pool-affinity-decisions.jsonl 2>/dev/null
echo
echo "=== 7) pool-requests 最近 6 条（sess=树键 与 账号字段）==="
tail -6 /opt/litellm/pool-requests.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        r = json.loads(line)
    except Exception:
        continue
    print('sess=%-22s model=%-24s key=%s keyRef=%s tt=%s' % (r.get('sess'), r.get('model'), r.get('key') or '-', r.get('keyRef') or '-', r.get('tt')))
"
echo
echo "=== 8) 与 dry-run 预测比对 ==="
python3 /tmp/patch_sidecar_s1.py --dry-run 2>&1 | tail -8
