#!/bin/bash
# 判定 LiteLLM 标签路由在两种协议上是否都生效：用「无效标签」当探针
#   若无效标签导致 401 → 该协议确实在读标签；若仍 200 → 该协议忽略标签
set -u
MASTER=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')
S=http://127.0.0.1:4001

probe() { # path body label extra_header
  local extra=()
  [ -n "${4:-}" ] && extra=(-H "$4")
  code=$(curl -sS -m 60 -o /tmp/tg.out -w '%{http_code}' -X POST "$S$1" \
    -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' "${extra[@]}" -d "$2")
  printf '  %-58s http=%s  %s\n' "$3" "$code" "$(head -c 110 /tmp/tg.out | tr -d '\n')"
}

echo "=== A) 头方式 x-litellm-tags: acct:NOPE（无效标签）==="
probe /v1/chat/completions '{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":5}' \
      "chat + 头无效标签（期望 401=标签生效）" 'x-litellm-tags: acct:NOPE'
probe /v1/responses '{"model":"deepseek-v4.1-flash","input":"ping","max_output_tokens":16}' \
      "responses + 头无效标签（401=生效 / 200=被忽略）" 'x-litellm-tags: acct:NOPE'

echo
echo "=== B) body 方式 metadata.tags（Responses 是否读 body 标签）==="
probe /v1/responses '{"model":"deepseek-v4.1-flash","input":"ping","max_output_tokens":16,"metadata":{"tags":["acct:NOPE"]}}' \
      "responses + body 无效标签（401=生效 / 200=被忽略）"

echo
echo "=== C) 对照：有效标签 ==="
probe /v1/responses '{"model":"deepseek-v4.1-flash","input":"ping","max_output_tokens":16}' \
      "responses + 头有效标签 acct:OCG_KEY_6" 'x-litellm-tags: acct:OCG_KEY_6'

echo
echo "=== D) 同树键连打两次 responses（看是否同账号）==="
for i in 1 2; do
  curl -sS -m 60 -o /dev/null -X POST "$S/v1/responses" -H "Authorization: Bearer $MASTER" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"deepseek-v4.1-flash\",\"input\":\"ping [pool-tree: resp-aff-01]\",\"max_output_tokens\":16}"
done
echo "=== E) 同树键连打两次 chat（对照）==="
for i in 1 2; do
  curl -sS -m 60 -o /dev/null -X POST "$S/v1/chat/completions" -H "Authorization: Bearer $MASTER" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"mimo-v2.6-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"ping [pool-tree: chat-aff-01]\"}],\"max_tokens\":5}"
done
echo
echo "=== F) 实际落号（按 sess 分组）==="
python3 - <<'PY'
import json, collections
trees = collections.defaultdict(collections.Counter)
for line in open('/opt/litellm/pool-requests.jsonl', encoding='utf-8', errors='replace'):
    try: r = json.loads(line)
    except Exception: continue
    s = str(r.get('sess') or '')
    if s in ('acc-check-01', 'resp-aff-01', 'chat-aff-01', 'probe-dual-01'):
        trees[s][(r.get('keyRef') or r.get('key') or '?')[-6:]] += 1
for s, c in trees.items():
    print(f"  {s:16s} 账号 {dict(c)}")
PY
