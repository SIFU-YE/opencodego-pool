#!/bin/bash
# 协议支持矩阵探测：判定「mimo 系模型不支持 Responses 协议」是否为既有问题（与树键/标签无关）
set -u
MASTER=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')
S=http://127.0.0.1:4001

probe() { # $1=path $2=body $3=label
  code=$(curl -sS -m 90 -o /tmp/probe.out -w '%{http_code}' -X POST "$S$1" \
    -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' -d "$2")
  printf '%-52s http=%s  %s\n' "$3" "$code" "$(head -c 150 /tmp/probe.out | tr -d '\n')"
}

echo "=== Responses 协议（/v1/responses）==="
for m in mimo-v2.6-flash mimo-v2.6-pro muse-spark-1.3-contributor claude-haiku-5-5 deepseek-v4.1-flash; do
  probe /v1/responses "{\"model\":\"$m\",\"input\":\"ping\",\"max_output_tokens\":16}" "responses + $m"
done

echo
echo "=== Chat Completions 协议（/v1/chat/completions）==="
for m in mimo-v2.6-flash mimo-v2.6-pro muse-spark-1.3-contributor claude-haiku-5-5 deepseek-v4.1-flash; do
  probe /v1/chat/completions "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":8}" "chat + $m"
done
