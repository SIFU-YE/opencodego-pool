#!/bin/bash
# B-3：被"用热"的 session 是否会恢复？（决定「每树一串」在长会话里能不能反复吃到快档）
# 目标 session = probe-verify-110601（11:06 连打 8 次后掉到 ~100 t/s）
export OCG_KEY_6="${OCG_KEY_6:?请先 export OCG_KEY_6=<账号6的key>}"
cd /tmp || exit 1
S=probe-verify-110601

for wait in 0 300 600 1200; do
  if [ "$wait" != "0" ]; then sleep "$wait"; fi
  echo "=== T+${wait}s ==="
  PROBE_SESSION="$S" PROBE_MAXTOK=400 PROBE_USAGE=1 timeout 300 python3 /tmp/sse_probe.py up6+np 1
done


