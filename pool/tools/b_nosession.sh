#!/bin/bash
# B-4：三种 session 策略在同一时段交替对比（同 key、同 prompt、800 token）
#   none  = 完全不发 x-opencode-session（普通 OpenAI 客户端行为）
#   fresh = 每次新串
#   hot   = 现役热桶号 dsh-acct-6-s1
export OCG_KEY_6="${OCG_KEY_6:?请先 export OCG_KEY_6=<账号6的key>}"
cd /tmp || exit 1
for i in 1 2 3; do
  for mode in none fresh hot; do
    case $mode in
      none)  S="-" ;;
      fresh) S="probe-fresh-$(date +%H%M%S)-$i" ;;
      hot)   S="dsh-acct-6-s1" ;;
    esac
    echo "--- round $i mode=$mode ---"
    PROBE_SESSION="$S" PROBE_MAXTOK=800 PROBE_USAGE=1 timeout 300 python3 /tmp/sse_probe.py up6+np 1
  done
done


