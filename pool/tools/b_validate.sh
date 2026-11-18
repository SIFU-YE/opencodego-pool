#!/bin/bash
# B 方案前置验证（全部直连上游，绕开号池一切组件，单变量）
#   B-2 前缀缓存是否与 x-opencode-session 绑定？（决定换 session 派生规则会不会丢缓存）
#   B-1 一个「新 session」被连续使用后会不会掉速？（决定「每树一串」能撑多久）
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:?请先 export LITELLM_MASTER_KEY}"
export OCG_KEY_6="${OCG_KEY_6:?请先 export OCG_KEY_6=<账号6的key>}"
cd /tmp || exit 1

# 生成一段固定长文本（~8k token），用于前缀缓存实验（必须逐字节相同）
python3 - <<'PY'
import random
random.seed(20261024)
lines = []
for i in range(1200):
    lines.append("%04d  %s" % (i, "".join(random.choice("abcdef0123456789") for _ in range(28))))
text = "下面是系统导出的清单，请记住它：\n" + "\n".join(lines) + "\n\n问题：上面清单里一共有多少行？只回答数字。"
open("/tmp/b_prompt.txt", "w", encoding="utf-8").write(text)
print("prompt_chars=%d" % len(text))
PY

echo
echo "===== B-2 前缀缓存 vs session ====="
echo "[1/4] session=A 首次（预期 cached 低）"
PROBE_SESSION=probe-cache-A PROBE_PROMPT_FILE=/tmp/b_prompt.txt PROBE_MAXTOK=20 PROBE_USAGE=1 \
  PROBE_MAXTOK=20 timeout 200 python3 /tmp/sse_probe.py up6+np 1
echo "[2/4] session=A 二次（预期 cached 高）"
PROBE_SESSION=probe-cache-A PROBE_PROMPT_FILE=/tmp/b_prompt.txt PROBE_MAXTOK=20 PROBE_USAGE=1 \
  timeout 200 python3 /tmp/sse_probe.py up6+np 1
echo "[3/4] session=B 换成完全不同的 session（若 cached 仍高 ⇒ 缓存是内容键，换串无损）"
PROBE_SESSION=probe-cache-B PROBE_PROMPT_FILE=/tmp/b_prompt.txt PROBE_MAXTOK=20 PROBE_USAGE=1 \
  timeout 200 python3 /tmp/sse_probe.py up6+np 1
echo "[4/4] session=C 再换一个"
PROBE_SESSION=probe-cache-C PROBE_PROMPT_FILE=/tmp/b_prompt.txt PROBE_MAXTOK=20 PROBE_USAGE=1 \
  timeout 200 python3 /tmp/sse_probe.py up6+np 1

echo
echo "===== B-1 新 session 连续使用 8 次（800 token/次）====="
S=probe-verify-$(date +%H%M%S)
echo "session=$S"
for i in 1 2 3 4 5 6 7 8; do
  PROBE_SESSION="$S" PROBE_MAXTOK=800 PROBE_USAGE=1 timeout 300 python3 /tmp/sse_probe.py up6+np 1
done

echo
echo "===== 对照：现役热桶号 dsh-acct-6-s1 ×2 ====="
for i in 1 2; do
  PROBE_SESSION=dsh-acct-6-s1 PROBE_MAXTOK=800 PROBE_USAGE=1 timeout 300 python3 /tmp/sse_probe.py up6+np 1
done


