#!/bin/bash
# 正式端到端验收：同一会话树 × 3 模型 × 2 协议 → 必须同一账号
set -u
MASTER=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')
S=http://127.0.0.1:4001
TREE="acc-final-$(date +%H%M%S)"

echo "树键 = $TREE"
post() { # path body label
  code=$(curl -sS -m 90 -o /dev/null -w '%{http_code}' -X POST "$S$1" \
    -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' -d "$2")
  printf '  %-34s http=%s\n' "$3" "$code"
}
post /v1/chat/completions "{\"model\":\"mimo-v2.6-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"ping [pool-tree: $TREE]\"}],\"max_tokens\":5}" "chat + mimo-v2.6-flash"
post /v1/chat/completions "{\"model\":\"mimo-v2.6-pro\",\"messages\":[{\"role\":\"user\",\"content\":\"ping [pool-tree: $TREE]\"}],\"max_tokens\":5}" "chat + mimo-v2.6-pro"
post /v1/chat/completions "{\"model\":\"deepseek-v4.1-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"ping [pool-tree: $TREE]\"}],\"max_tokens\":5}" "chat + dsv4.1-flash"
post /v1/responses "{\"model\":\"deepseek-v4.1-flash\",\"input\":\"ping [pool-tree: $TREE]\",\"max_output_tokens\":16}" "responses + dsv4.1-flash"
post /v1/responses "{\"model\":\"mimo-v2.6-flash\",\"input\":\"ping [pool-tree: $TREE]\",\"max_output_tokens\":16}" "responses + mimo-v2.6-flash"

echo
echo "=== 归属分析（model 别名 → key_want → keyRef 三级口径）==="
python3 - "$TREE" <<'PY'
import json, re, sys, hashlib
tree = sys.argv[1]
rows = []
for line in open('/opt/litellm/pool-requests.jsonl', encoding='utf-8', errors='replace'):
    try: r = json.loads(line)
    except Exception: continue
    if r.get('sess') != tree:
        continue
    model = str(r.get('model') or '')
    m = re.search(r'@a(\d+)(?:s(\d+))?', model)
    kw = str(r.get('key_want') or '')
    if m:
        acct = f"a{m.group(1)}（别名）"
    elif kw.startswith('OCG_KEY_'):
        acct = f"a{kw.split('_')[-1]}（key_want）"
    else:
        raw = r.get('keyRef') or r.get('key') or ''
        acct = ("acct#" + hashlib.sha256(raw.encode()).hexdigest()[:8]) if raw else "未知"
    rows.append((model, acct, r))
for model, acct, _ in rows:
    print(f"  {model:28s} -> {acct}")
uniq = {a.split('（')[0] for _, a, _ in rows}
print(f"\n  请求数={len(rows)}  唯一账号数={len(uniq)}  验收目标=1  ->", "PASS" if len(uniq) == 1 else "FAIL")
resp_rows = [r for m, a, r in rows if '未知' in a]
if resp_rows:
    print("  未归属原始记录样本:", json.dumps(resp_rows[0], ensure_ascii=False)[:260])
PY
