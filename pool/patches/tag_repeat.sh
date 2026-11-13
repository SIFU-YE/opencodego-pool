#!/bin/bash
# 追查 401 真实原因：重复测各标签 + 抓 LiteLLM 侧日志
set -u
MASTER=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')
S=http://127.0.0.1:4001
RESP='{"model":"deepseek-v4.1-flash","input":"ping","max_output_tokens":16}'

echo "=== 每个标签重复 3 次（responses）==="
for k in OCG_KEY_2 OCG_KEY_4 OCG_KEY_5 OCG_KEY_6; do
  line="  acct:$k ->"
  for i in 1 2 3; do
    c=$(curl -sS -m 45 -o /dev/null -w '%{http_code}' -X POST "$S/v1/responses" \
      -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' \
      -H "x-litellm-tags: acct:$k" -d "$RESP")
    line="$line $c"
  done
  echo "$line"
done

echo
echo "=== 每个标签重复 3 次（chat）==="
CHAT='{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":5}'
for k in OCG_KEY_2 OCG_KEY_4 OCG_KEY_5 OCG_KEY_6; do
  line="  acct:$k ->"
  for i in 1 2 3; do
    c=$(curl -sS -m 45 -o /dev/null -w '%{http_code}' -X POST "$S/v1/chat/completions" \
      -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' \
      -H "x-litellm-tags: acct:$k" -d "$CHAT")
    line="$line $c"
  done
  echo "$line"
done

echo
echo "=== LiteLLM 日志里与 tag 相关的行（最近 15 分钟）==="
docker logs litellm --since 15m 2>&1 | grep -iE 'tag' | tail -12

echo
echo "=== 配置里 mimo-v2.6-flash 的 deployment 标签（抽查）==="
python3 - <<'PY'
import re
cfg = open('/opt/litellm/config.yaml', encoding='utf-8').read()
blocks = re.findall(r'(?ms)^  - model_name: (\S+)\n(.*?)(?=^  - model_name: |\Z)', cfg)
for model, body in blocks:
    if model != 'deepseek-v4.1-flash':
        continue
    tag = re.search(r'acct:(OCG_KEY_\d+)', body)
    key = re.search(r'api_key: os\.environ/(OCG_KEY_\d+)', body)
    print(f"    {model}: tag={tag.group(1) if tag else None} key={key.group(1) if key else None}")
PY
