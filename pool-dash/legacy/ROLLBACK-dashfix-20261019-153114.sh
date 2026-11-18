#!/usr/bin/env bash
# 一键回滚 dashfix（20261019-153114）：还原 v12 孤本 dashboard.py + 旧 pool_admin_ui.py / style-audit.py
set -euo pipefail
D=/opt/pool-dash
cp -p "$D/dashboard.py.bak-dashfix-20261019-153114"     "$D/dashboard.py"
cp -p "$D/pool_admin_ui.py.bak-dashfix-20261019-153114" "$D/pool_admin_ui.py"
cp -p "$D/style-audit.py.bak-v12-20261019-153114"       "$D/style-audit.py"
find "$D/__pycache__" -maxdepth 1 -name 'dashboard*.pyc' -delete 2>/dev/null || true
find "$D/__pycache__" -maxdepth 1 -name 'pool_admin_ui*.pyc' -delete 2>/dev/null || true
systemctl restart pool-dash pool-dashboard
sleep 3
echo "--- 回滚后 md5（dashboard.py 应为 2d5bc72575d4e974303f7b28fb8c4227）---"
md5sum "$D/dashboard.py" "$D/pool_admin_ui.py" "$D/style-audit.py"
systemctl is-active pool-dash pool-dashboard
echo "rollback done (20261019-153114)"
