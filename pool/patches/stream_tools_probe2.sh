#!/bin/bash
# 修正版双协议流式+tools 探测：Responses 用扁平 tools 结构；带 SSE Accept 头
set -u
MASTER=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')
S=http://127.0.0.1:4001
CHAT_TOOLS='"tools":[{"type":"function","function":{"name":"get_weather","description":"查天气","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]'
RESP_TOOLS='"tools":[{"type":"function","name":"get_weather","description":"查天气","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}]'

echo "=== A) chat/completions 流式 + tools + mimo-v2.6-flash ==="
code=$(curl -sS -N -m 90 -o /tmp/se1.txt -w '%{http_code}' -X POST "$S/v1/chat/completions" \
  -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d "{\"model\":\"mimo-v2.6-flash\",\"stream\":true,\"max_tokens\":48,\"messages\":[{\"role\":\"user\",\"content\":\"查北京天气\"}],$CHAT_TOOLS}")
echo "  http=$code  frames=$(grep -c '^data: ' /tmp/se1.txt 2>/dev/null)  tool_calls=$(grep -c tool_calls /tmp/se1.txt 2>/dev/null)  done=$(grep -c '\[DONE\]' /tmp/se1.txt 2>/dev/null)"

echo
echo "=== B) responses 流式 + tools（扁平结构）+ deepseek-v4.1-flash（现状生产路径）==="
code=$(curl -sS -N -m 90 -o /tmp/se2.txt -w '%{http_code}' -X POST "$S/v1/responses" \
  -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d "{\"model\":\"deepseek-v4.1-flash\",\"stream\":true,\"max_output_tokens\":48,\"input\":\"查北京天气\",$RESP_TOOLS}")
echo "  http=$code  data帧=$(grep -c '^data: ' /tmp/se2.txt 2>/dev/null)  function_call=$(grep -c function_call /tmp/se2.txt 2>/dev/null)"
echo "  头部 160 字节: $(head -c 160 /tmp/se2.txt | tr -d '\n')"

echo
echo "=== C) 带树键的两协议并发（同树同账号的前置条件）==="
for p in /v1/chat/completions /v1/responses; do
  if [ "$p" = "/v1/chat/completions" ]; then
    b='{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"ping [pool-tree: probe-dual-01]"}],"max_tokens":8}'
  else
    b='{"model":"deepseek-v4.1-flash","input":"ping [pool-tree: probe-dual-01]","max_output_tokens":16}'
  fi
  c=$(curl -sS -m 60 -o /dev/null -w '%{http_code}' -X POST "$S$p" -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' -d "$b")
  echo "  $p -> http=$c"
done
echo "  决策记录:"; grep 'probe-dual-01' /opt/litellm/pool-affinity-decisions.jsonl | tail -2
echo "  实际落号:"; python3 - <<'PY'
import json
for line in open('/opt/litellm/pool-requests.jsonl', encoding='utf-8', errors='replace'):
    try: r = json.loads(line)
    except Exception: continue
    if r.get('sess') == 'probe-dual-01':
        print('   ', r.get('model'), (r.get('keyRef') or r.get('key') or '-')[-8:])
PY
