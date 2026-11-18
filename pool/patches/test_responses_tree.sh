#!/bin/bash
# 验证：/v1/responses（DSH 实际协议）也能识别会话树键
set -u
MASTER=$(grep '^LITELLM_MASTER_KEY=' /opt/litellm/.env | head -1 | cut -d= -f2- | tr -d '"')
S=http://127.0.0.1:4001

probe() { # path body label
  code=$(curl -sS -m 90 -o /tmp/rtree.out -w '%{http_code}' -X POST "$S$1" \
    -H "Authorization: Bearer $MASTER" -H 'Content-Type: application/json' -d "$2")
  printf '%-56s http=%s %s\n' "$3" "$code" "$(head -c 110 /tmp/rtree.out | tr -d '\n')"
}

echo "=== Responses 协议 + 树键（可用的 dsv4.1-flash）==="
probe /v1/responses '{"model":"deepseek-v4.1-flash","input":"ping [pool-tree: synth-resp-01]","max_output_tokens":16}' "responses + tree synth-resp-01"
probe /v1/responses '{"model":"deepseek-v4.1-flash","input":"ping [pool-tree: synth-resp-02]","max_output_tokens":16}' "responses + tree synth-resp-02"

echo
echo "=== Chat Completions + 树键（回归确认未破坏）==="
probe /v1/chat/completions '{"model":"mimo-v2.6-flash","messages":[{"role":"user","content":"ping [pool-tree: synth-tree-03]"}],"max_tokens":8}' "chat + tree synth-tree-03"

echo
echo "=== 决策日志（应出现上述三个树键）==="
tail -5 /opt/litellm/pool-affinity-decisions.jsonl
echo
echo "=== sidecar 日志 ==="
journalctl -u litellm-session-sidecar --since '-2min' --no-pager 2>/dev/null | grep -E 'tree-affinity' | tail -5
