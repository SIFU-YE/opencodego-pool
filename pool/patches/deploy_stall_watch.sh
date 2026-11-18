#!/bin/bash
# 部署上游静默监控 + 每 5 分钟 cron（先备份 crontab）
set -u
cp /tmp/stall_watch.py /opt/litellm/tools/stall_watch.py
chmod 755 /opt/litellm/tools/stall_watch.py

echo "=== 首次运行 ==="
python3 /opt/litellm/tools/stall_watch.py 15 60; echo "退出码=$?"
echo
echo "=== 状态文件 ==="
cat /opt/litellm/upstream-stall.json
echo
echo "=== 现有 crontab（备份到 /opt/litellm/crontab.bak-stallwatch）==="
crontab -l > /opt/litellm/crontab.bak-stallwatch 2>/dev/null
cat /opt/litellm/crontab.bak-stallwatch
if crontab -l 2>/dev/null | grep -q 'stall_watch.py'; then
  echo "(cron 条目已存在，跳过)"
else
  { crontab -l 2>/dev/null; echo '*/5 * * * * /usr/bin/python3 /opt/litellm/tools/stall_watch.py 15 60 >> /opt/litellm/upstream-stall.cron.log 2>&1'; } | crontab -
  echo "已添加 cron 条目"
fi
echo
echo "=== 部署后的 crontab ==="
crontab -l
