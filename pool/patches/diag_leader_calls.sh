#!/bin/bash
# 诊断：为什么"我这个会话树键"的池内调用没进成功埋点
set -u
T="session-4e7011c7-2246-4e22-8f91-3ca4f93e6295"
echo "=== litellm 近期 4xx/5xx（40 分钟，尾部）==="
docker logs litellm --since 40m 2>&1 | grep -E ' (400|401|403|422|429|500|502) ' | tail -6
echo
echo "=== 决策数 vs 成功埋点数（该树键）==="
printf '  决策: %s\n' "$(grep -c "$T" /opt/litellm/pool-affinity-decisions.jsonl)"
printf '  埋点: %s\n' "$(grep -c "$T" /opt/litellm/pool-requests.jsonl)"
echo
echo "=== 该树键最近决策（含 peer 信息）==="
grep "$T" /opt/litellm/pool-affinity-decisions.jsonl | tail -3
echo
echo "=== 埋点里的该树键（若有）==="
grep "$T" /opt/litellm/pool-requests.jsonl | tail -2
