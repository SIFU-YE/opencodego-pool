#!/bin/bash
# 临时实例验证 tag 路由语义（不动生产进程）：带健康轮询与失败诊断
set -u
IMG=$(docker inspect -f '{{.Config.Image}}' litellm)
echo "image = $IMG"

python3 - <<'PY'
src = open('/opt/litellm/config.yaml').read()
out = "\n".join(l for l in src.splitlines() if 'callbacks:' not in l) + "\n"
open('/tmp/config-tagtest.yaml', 'w').write(out)
print("temp config lines =", len(out.splitlines()))
PY

docker rm -f litellm-tagtest >/dev/null 2>&1
docker run -d --name litellm-tagtest -p 127.0.0.1:4100:4000 \
  --env-file /opt/litellm/.env \
  -v /tmp/config-tagtest.yaml:/app/config.yaml:ro \
  "$IMG" --config /app/config.yaml --port 4000 >/dev/null

echo "--- 等待就绪（最多 150s）---"
ok=0
for i in $(seq 1 50); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:4100/health/liveliness 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then ok=1; echo "ready after ${i}x3s (http=$code)"; break; fi
  sleep 3
done

if [ "$ok" != "1" ]; then
  echo "!! 未就绪，诊断："
  docker ps -a --filter name=litellm-tagtest --format '{{.Status}}'
  echo "--- logs (tail 30) ---"
  docker logs litellm-tagtest 2>&1 | tail -30
  echo "--- state ---"
  docker inspect litellm-tagtest --format '{{.State.Status}} exit={{.State.ExitCode}} err={{.State.Error}}' 2>&1
  docker rm -f litellm-tagtest >/dev/null 2>&1
  exit 1
fi

KEY=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')
post() {
  curl -sS -m 120 -o /tmp/tt.out -w '%{http_code}' -X POST http://127.0.0.1:4100/v1/chat/completions \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$1"
}
run() { echo "--- $1 ---"; code=$(post "$2"); echo "http=$code"; head -c 260 /tmp/tt.out 2>/dev/null; echo; }

run "a) 未带标签（生产主流量形态，必须仍可路由）" '{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":5}'
run "b) metadata.tags=acct:OCG_KEY_5（应成功）" '{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":5,"metadata":{"tags":["acct:OCG_KEY_5"]}}'
run "c) metadata.tags=acct:NOPE（应失败=证明过滤生效）" '{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":5,"metadata":{"tags":["acct:NOPE"]}}'
run "d) metadata.tags=!acct:OCG_KEY_5（排他语义）" '{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":5,"metadata":{"tags":["!acct:OCG_KEY_5"]}}'

echo "--- e) 头方式 x-litellm-tags: acct:OCG_KEY_6（sidecar 实际采用）---"
code=$(curl -sS -m 120 -o /tmp/tt2.out -w '%{http_code}' -X POST http://127.0.0.1:4100/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -H 'x-litellm-tags: acct:OCG_KEY_6' \
  -d '{"model":"mimo-v2.6-pro","messages":[{"role":"user","content":"ping"}],"max_tokens":5}')
echo "http=$code"; head -c 260 /tmp/tt2.out 2>/dev/null; echo

echo "--- 清理 ---"; docker rm -f litellm-tagtest >/dev/null && echo removed
