#!/bin/bash
# 号池回归验证：实例键鉴权、模型发现、两种协议、anthropic 协议取证
set -u
MASTER=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')

IKEY=$(python3 - <<'PY'
import json
try:
    d = json.load(open('/opt/litellm/dsh-instance-keys.json'))
except Exception:
    print(""); raise SystemExit
if isinstance(d, dict):
    if isinstance(d.get('keys'), dict):
        print(next(iter(d['keys']), ""))
    else:
        for k in d:
            if isinstance(k, str) and k.startswith('sk-'):
                print(k); break
PY
)
echo "instance key resolved: $([ -n "${IKEY:-}" ] && echo yes || echo no)"

echo
echo "=== 1) 实例键 → 主键改写（生产鉴权路径，回归重点）==="
code=$(curl -sS -m 90 -o /tmp/rg1.out -w '%{http_code}' -X POST http://127.0.0.1:4001/v1/chat/completions \
  -H "Authorization: Bearer $IKEY" -H 'Content-Type: application/json' \
  -d '{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":5}')
echo "  http=$code  $(head -c 100 /tmp/rg1.out | tr -d '\n')"

echo
echo "=== 2) GET /v1/models 过滤（sidecar 只放行 5 个）==="
curl -sS -m 30 -o /tmp/rg2.out -w '  http=%{http_code}\n' http://127.0.0.1:4001/v1/models -H "Authorization: Bearer $MASTER"
python3 -c "
import json
try:
    d = json.load(open('/tmp/rg2.out'))
    print('  models:', [m['id'] for m in d.get('data', [])])
except Exception as e:
    print('  parse err', e)
"

echo
echo "=== 3) /v1/responses + 实例键（DSH 真实形态，未带树键）==="
code=$(curl -sS -m 90 -o /tmp/rg3.out -w '%{http_code}' -X POST http://127.0.0.1:4001/v1/responses \
  -H "Authorization: Bearer $IKEY" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","input":"ping","max_output_tokens":16}')
echo "  http=$code  $(head -c 100 /tmp/rg3.out | tr -d '\n')"

echo
echo "=== 4) anthropic 协议 /v1/messages + claude-haiku-5-5（协议修复选型取证）==="
code=$(curl -sS -m 60 -o /tmp/rg4.out -w '%{http_code}' -X POST http://127.0.0.1:4001/v1/messages \
  -H "x-api-key: $MASTER" -H 'anthropic-version: 2023-06-01' -H 'Content-Type: application/json' \
  -d '{"model":"claude-haiku-5-5","max_tokens":16,"messages":[{"role":"user","content":"ping"}]}')
echo "  http=$code  $(head -c 200 /tmp/rg4.out | tr -d '\n')"

echo
echo "=== 5) 同一会话锚（无树键）在两种协议下是否一致（S3 的副产品：DSH 流量有亲和在）==="
tail -4 /opt/litellm/pool-requests.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    try: r = json.loads(line)
    except Exception: continue
    print('  sess=%-24s model=%-22s tt=%s' % (str(r.get('sess'))[:24], r.get('model'), r.get('tt')))
"
