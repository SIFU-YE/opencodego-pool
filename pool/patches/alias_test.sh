#!/bin/bash
# 临时实例验证「池内别名 → 账号」的确定性命中（不碰生产进程/生产日志）
set -u
IMG=$(docker inspect -f '{{.Config.Image}}' litellm)
cp /opt/litellm/config.yaml /tmp/config-alias.yaml
: > /tmp/alias-requests.jsonl

docker rm -f litellm-alias >/dev/null 2>&1
docker run -d --name litellm-alias -p 127.0.0.1:4100:4000 \
  --env-file /opt/litellm/.env \
  -v /tmp/config-alias.yaml:/app/config.yaml:ro \
  -v /opt/litellm/pool_logger.py:/app/pool_logger.py:ro \
  -v /tmp/alias-requests.jsonl:/app/pool-requests.jsonl \
  "$IMG" --config /app/config.yaml --port 4000 >/dev/null

echo "--- 等待就绪 ---"
ready=0
for i in $(seq 1 40); do
  c=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:4100/health/liveliness 2>/dev/null || echo 000)
  [ "$c" = "200" ] && { ready=1; echo "  ready after ${i}x3s"; break; }
  sleep 3
done
if [ "$ready" != "1" ]; then
  echo "!! 未就绪"; docker logs litellm-alias 2>&1 | tail -20; docker rm -f litellm-alias >/dev/null; exit 1
fi

MASTER=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')

echo
echo "--- 正式模型名（未带别名）---"
for m in mimo-v2.6-flash deepseek-v4.1-flash; do
  c=$(curl -sS -m 60 -o /tmp/al.out -w '%{http_code}' -X POST http://127.0.0.1:4100/v1/chat/completions \
    -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":5}")
  echo "  $m -> http=$c"
done

echo
echo "--- 别名（每个别名必须命中对应账号）---"
for a in mimo-v2.6-flash@a2 mimo-v2.6-flash@a5 deepseek-v4.1-flash@a6 deepseek-v4.1-flash@a4; do
  c=$(curl -sS -m 60 -o /tmp/al.out -w '%{http_code}' -X POST http://127.0.0.1:4100/v1/chat/completions \
    -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$a\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":5}")
  echo "  $a -> http=$c"
done

echo
echo "--- 临时埋点：每个请求实际用的账号（应别名=aN 一致；正式名=均衡）---"
python3 - <<'PY'
import json
try:
    lines = open('/tmp/alias-requests.jsonl', encoding='utf-8', errors='replace').read().splitlines()
except FileNotFoundError:
    lines = []
print(f"  记录数={len(lines)}")
for line in lines:
    try:
        r = json.loads(line)
    except Exception:
        continue
    acct = (r.get('keyRef') or r.get('key') or '-')
    print(f"    model={str(r.get('model')):28s} acct尾6={acct[-6:]}")
PY

echo
echo "--- 清理 ---"
docker rm -f litellm-alias >/dev/null && echo "  removed"
