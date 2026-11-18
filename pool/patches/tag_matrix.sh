#!/bin/bash
# 隔离变量：为什么 responses + 有效标签在手工测试里 401，而 sidecar 注入同标签却 200
set -u
MASTER=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')
IKEY=$(python3 - <<'PY'
import json
d = json.load(open('/opt/litellm/dsh-instance-keys.json'))
if isinstance(d.get('keys'), dict):
    print(next(iter(d['keys']), ""))
else:
    print(next((k for k in d if isinstance(k, str) and k.startswith('sk-')), ""))
PY
)
S=http://127.0.0.1:4001

show() { # path label auth header_args...
  local path="$1" label="$2" auth="$3"; shift 3
  code=$(curl -sS -m 60 -o /tmp/mx.out -w '%{http_code}' -X POST "$S$path" \
    -H "Authorization: Bearer $auth" -H 'Content-Type: application/json' "$@")
  printf '  %-52s http=%s\n' "$label" "$code"
  [ "$code" != "200" ] && printf '        %s\n' "$(head -c 200 /tmp/mx.out | tr -d '\n')"
}

CHAT_BODY='{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":5}'
RESP_BODY='{"model":"deepseek-v4.1-flash","input":"ping","max_output_tokens":16}'

echo "=== 用主键（master）==="
show /v1/chat/completions   "chat  + 无标签"                       "$MASTER" -d "$CHAT_BODY"
show /v1/chat/completions   "chat  + 头 acct:OCG_KEY_6"            "$MASTER" -H 'x-litellm-tags: acct:OCG_KEY_6' -d "$CHAT_BODY"
show /v1/responses          "resp  + 无标签"                       "$MASTER" -d "$RESP_BODY"
show /v1/responses          "resp  + 头 acct:OCG_KEY_6"            "$MASTER" -H 'x-litellm-tags: acct:OCG_KEY_6' -d "$RESP_BODY"

echo
echo "=== 用实例键（生产形态 sk-pool-*）==="
show /v1/responses          "resp  + 无标签"                       "$IKEY" -d "$RESP_BODY"
show /v1/responses          "resp  + 头 acct:OCG_KEY_6"            "$IKEY" -H 'x-litellm-tags: acct:OCG_KEY_6' -d "$RESP_BODY"
show /v1/chat/completions   "chat  + 头 acct:OCG_KEY_6"            "$IKEY" -H 'x-litellm-tags: acct:OCG_KEY_6' -d "$CHAT_BODY"

echo
echo "=== 试其他账号标签（排除「KEY_6 恰好不可用」）==="
for k in OCG_KEY_2 OCG_KEY_4 OCG_KEY_5; do
  show /v1/responses "resp + 头 acct:$k" "$MASTER" -H "x-litellm-tags: acct:$k" -d "$RESP_BODY"
done

echo
echo "=== 试 body metadata.tags（Responses 的等价路径）==="
show /v1/responses "resp + body metadata.tags=[acct:OCG_KEY_6]" "$MASTER" \
  -d '{"model":"deepseek-v4.1-flash","input":"ping","max_output_tokens":16,"metadata":{"tags":["acct:OCG_KEY_6"]}}'

echo
echo "=== 对照：标签大小写/引号变体 ==="
show /v1/chat/completions "chat + 头 \"acct:OCG_KEY_6\"（带引号）" "$MASTER" -H 'x-litellm-tags: "acct:OCG_KEY_6"' -d "$CHAT_BODY"
