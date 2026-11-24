#!/usr/bin/env bash
# opencodego-pool 热更新脚本：只替换 Python 组件并重启 reconciler，不中断数据面。
#
# 适用场景（默认 systemd 部署）：
#   - 更新 pool_store.py / pool_reconciler.py 两个运行时组件；
#   - LiteLLM 容器、session-sidecar 全程不停止；
#   - 只重启 opencodelitelm-pool-reconciler 和 opencodelitelm-pool-dashboard
#     （两者都不接客户端 /v1/* 数据面；dashboard 是管理控制台/看板面）；
#   - 数据面 4000/4001 的健康检查在更新前后都必须通过。
#
# 在号池服务器上以 root 运行：
#   sudo bash deploy/hot-update-pool.sh --dry-run
#   sudo bash deploy/hot-update-pool.sh
#
# 可选：
#   --home /opt/litellm          POOL_HOME
#   --repo /path/to/repo         仓库根目录（含 pool/pool_store.py）
#   --no-reconcile               更新后不主动跑 --once（仅重启服务）
#   --skip-dashboard             不重启 dashboard（不推荐：管理控制台进程仍加载旧 pool_store）
#   --skip-health                跳过 HTTP 健康检查（不推荐）
#   --service <name>             systemd reconciler 服务名
#
# 退出码：0=成功；1=更新/回滚失败；2=更新完成但 --once 失败（服务仍健康，需人工看日志）。
set -Eeuo pipefail

HOME_DIR="${POOL_HOME:-/opt/litellm}"
REPO_ROOT=""
SERVICE_NAME="opencodelitelm-pool-reconciler"
DASHBOARD_SERVICE="opencodelitelm-pool-dashboard"
DASHBOARD_PORT="${POOL_DASHBOARD_PORT:-${DASHBOARD_PORT:-4010}}"
DO_RECONCILE=1
DO_HEALTH=1
DO_DASHBOARD=1
DRY_RUN=0
LOCK_FILE="${HOME_DIR}/.hot-update.lock"
BACKUP_ROOT="${POOL_HOT_UPDATE_BACKUP_DIR:-/var/backups/opencodego-pool}"
TS="$(date +%Y%m%d-%H%M%S)"
FILES=(pool_store.py pool_reconciler.py)
BACKUP_DIR=""
FILES_INSTALLED=0

usage() {
  awk 'NR > 1 { if ($0 ~ /^#/) { sub(/^# ?/, ""); print } else { exit } }' "$0"
}

log()  { printf '[hot-update] %s\n' "$*"; }
warn() { printf '[hot-update][WARN] %s\n' "$*" >&2; }
die()  { printf '[hot-update][FAIL] %s\n' "$*" >&2; exit 1; }

while (($#)); do
  case "$1" in
    --home) HOME_DIR="${2:?}"; shift 2 ;;
    --repo) REPO_ROOT="${2:?}"; shift 2 ;;
    --service) SERVICE_NAME="${2:?}"; shift 2 ;;
    --no-reconcile) DO_RECONCILE=0; shift ;;
    --skip-dashboard) DO_DASHBOARD=0; shift ;;
    --skip-health) DO_HEALTH=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

[[ "${EUID:-$(id -u)}" -eq 0 ]] || die "请用 root 运行：sudo bash $0"
[[ -f "${HOME_DIR}/pool_store.py" ]] || die "找不到 ${HOME_DIR}/pool_store.py；--home 是否正确？"

if [[ -z "$REPO_ROOT" ]]; then
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
[[ -f "$REPO_ROOT/pool/pool_store.py" ]] || die "仓库缺少 pool/pool_store.py：$REPO_ROOT"
[[ -f "$REPO_ROOT/pool/pool_reconciler.py" ]] || die "仓库缺少 pool/pool_reconciler.py：$REPO_ROOT"

# 同一时间只允许一个热更新；避免两个进程同时换文件/重启服务
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "另一个 hot-update 正在运行（lock: $LOCK_FILE）"
else
  mkdir "$LOCK_FILE.d" 2>/dev/null || die "另一个 hot-update 正在运行（lock: $LOCK_FILE.d）"
  trap 'rmdir "$LOCK_FILE.d" 2>/dev/null || true' EXIT
fi

# ---------------------------------------------------------------- 健康检查
tcp_ok() {
  local port="$1"
  python3 - "$port" <<'PY'
import socket, sys
port = int(sys.argv[1])
s = socket.socket()
s.settimeout(2)
try:
    s.connect(("127.0.0.1", port))
except Exception:
    sys.exit(1)
finally:
    s.close()
PY
}

http_ok() {
  local url="$1"
  python3 - "$url" <<'PY'
import sys, urllib.request
url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=5) as r:
        sys.exit(0 if 200 <= r.status < 500 else 1)
except Exception:
    sys.exit(1)
PY
}

check_data_plane() {
  local label="$1"
  tcp_ok 4000 || { warn "$label: LiteLLM 4000 不可达"; return 1; }
  tcp_ok 4001 || { warn "$label: sidecar 4001 不可达"; return 1; }
  if (( DO_HEALTH == 1 )); then
    http_ok "http://127.0.0.1:4000/health/liveliness" \
      || { warn "$label: LiteLLM health/liveliness 失败"; return 1; }
  fi
  return 0
}

check_dashboard() {
  local label="$1"
  (( DO_DASHBOARD == 1 )) || return 0
  (( DO_HEALTH == 1 )) || return 0
  if ! unit_exists "$DASHBOARD_SERVICE" || ! unit_active "$DASHBOARD_SERVICE"; then
    return 0
  fi
  http_ok "http://127.0.0.1:${DASHBOARD_PORT}/" \
    || { warn "$label: dashboard :${DASHBOARD_PORT} 不可达"; return 1; }
  http_ok "http://127.0.0.1:${DASHBOARD_PORT}/api/admin/status" \
    || warn "$label: dashboard /api/admin/status 非 2xx（可能 Host 白名单，继续）"
  return 0
}

# ---------------------------------------------------------------- 服务控制
unit_exists() {
  local unit="$1"
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl list-unit-files --no-pager --no-legend "${unit}.service" 2>/dev/null | grep -q "${unit}.service"
}

unit_active() {
  systemctl is-active --quiet "$1"
}

restart_unit() {
  local unit="$1" label="$2"
  if ! unit_exists "$unit"; then
    warn "未找到 systemd unit ${unit}.service，跳过 $label 重启"
    return 2
  fi
  systemctl restart "$unit"
  for _ in $(seq 1 30); do
    unit_active "$unit" && return 0
    sleep 0.5
  done
  return 1
}

restart_reconciler() {
  if unit_exists "$SERVICE_NAME"; then
    restart_unit "$SERVICE_NAME" "reconciler"
    return $?
  fi
  # nohup 兼容模式：只动 reconciler，不动 sidecar/dashboard
  local pidfile="${HOME_DIR}/run/reconciler.pid"
  if [[ -f "$pidfile" ]]; then
    kill "$(cat "$pidfile")" 2>/dev/null || true
    sleep 1
  fi
  mkdir -p "${HOME_DIR}/run" "${HOME_DIR}/logs"
  ( cd "$HOME_DIR" && \
    set -a && [[ -f /etc/opencodelitelm-pool.env ]] && . /etc/opencodelitelm-pool.env && set +a; \
    nohup python3 "${HOME_DIR}/pool_reconciler.py" --watch --interval "${POOL_RECONCILE_INTERVAL:-60}" \
      >>"${HOME_DIR}/logs/reconciler.log" 2>&1 & echo $! >"$pidfile" ) || return 1
  sleep 1
  kill -0 "$(cat "$pidfile")" 2>/dev/null
}

restart_dashboard() {
  if (( DO_DASHBOARD == 0 )); then
    warn "按 --skip-dashboard 跳过 dashboard 重启；管理控制台仍可能加载旧 pool_store"
    return 0
  fi
  if unit_exists "$DASHBOARD_SERVICE"; then
    restart_unit "$DASHBOARD_SERVICE" "dashboard" || return 1
    return 0
  fi
  warn "未找到 ${DASHBOARD_SERVICE}.service；跳过 dashboard 重启（请确认管理面如何部署）"
  return 0
}

# ---------------------------------------------------------------- 回滚
rollback() {
  local reason="${1:-未知错误}"
  (( FILES_INSTALLED == 1 )) || return 0
  warn "开始回滚（$reason）"
  for f in "${FILES[@]}"; do
    [[ -f "$BACKUP_DIR/$f" ]] && install -m "$(stat -c '%a' "$BACKUP_DIR/$f")" \
      -o "$(stat -c '%u' "$BACKUP_DIR/$f")" -g "$(stat -c '%g' "$BACKUP_DIR/$f")" \
      "$BACKUP_DIR/$f" "$HOME_DIR/$f" || true
  done
  FILES_INSTALLED=0
  restart_dashboard || warn "回滚后 dashboard 重启失败，请人工检查 systemctl status $DASHBOARD_SERVICE"
  restart_reconciler || warn "回滚后 reconciler 重启失败，请人工检查 systemctl status $SERVICE_NAME"
  check_data_plane "回滚后" || warn "回滚后数据面健康检查失败"
  check_dashboard "回滚后" || warn "回滚后 dashboard 健康检查失败"
  die "已回滚：$reason"
}

on_error() {
  local rc=$?
  set +e
  warn "第 $LINENO 行失败（rc=$rc），尝试自动回滚"
  rollback "脚本执行失败（第 $LINENO 行）"
}
trap on_error ERR

# ---------------------------------------------------------------- 预检
log "POOL_HOME=$HOME_DIR"
log "REPO_ROOT=$REPO_ROOT"
log "SERVICE=$SERVICE_NAME  DASHBOARD=$DASHBOARD_SERVICE"
check_data_plane "更新前" || die "更新前数据面不健康，拒绝变更（先排查 LiteLLM/sidecar）"
check_dashboard "更新前" || die "更新前 dashboard 不健康，拒绝变更"

for f in "${FILES[@]}"; do
  python3 -m py_compile "$REPO_ROOT/pool/$f" || die "新文件语法错误：pool/$f"
done
if unit_exists "$SERVICE_NAME" && ! unit_active "$SERVICE_NAME"; then
  warn "reconciler 当前不是 active；仍会安装并尝试启动"
fi
if (( DO_DASHBOARD == 1 )) && ! unit_exists "$DASHBOARD_SERVICE"; then
  warn "未找到 ${DASHBOARD_SERVICE}.service；dashboard 不会被打断，但管理控制台可能仍加载旧代码"
fi

# 记录旧文件
mkdir -p "$BACKUP_ROOT"
BACKUP_DIR="${BACKUP_ROOT}/${TS}"
mkdir -p "$BACKUP_DIR"
for f in "${FILES[@]}"; do
  cp -p "$HOME_DIR/$f" "$BACKUP_DIR/$f"
  sha256sum "$HOME_DIR/$f" >"$BACKUP_DIR/$f.sha256"
done
log "备份：$BACKUP_DIR"

if (( DRY_RUN == 1 )); then
  log "dry-run：以下文件会原子替换，并重启 $DASHBOARD_SERVICE + $SERVICE_NAME"
  for f in "${FILES[@]}"; do
    printf '  %s -> %s\n' "$REPO_ROOT/pool/$f" "$HOME_DIR/$f"
  done
  log "dry-run 完成，未改动任何文件"
  exit 0
fi

# ---------------------------------------------------------------- 原子替换
for f in "${FILES[@]}"; do
  src="$REPO_ROOT/pool/$f"
  dst="$HOME_DIR/$f"
  mode="$(stat -c '%a' "$dst")"
  uid="$(stat -c '%u' "$dst")"
  gid="$(stat -c '%g' "$dst")"
  tmp="$(mktemp "${HOME_DIR}/.${f}.new.XXXXXX")"
  install -m "$mode" -o "$uid" -g "$gid" "$src" "$tmp"
  mv -f "$tmp" "$dst"
  FILES_INSTALLED=1
  log "已替换：$dst"
done

# 先重启 dashboard：管理控制台进程也加载 pool_control/pool_store，必须换新代码
# 数据面 LiteLLM/sidecar 不受影响；dashboard 只有控制面短暂重启。
set +e
restart_dashboard
dash_rc=$?
set -e
if (( dash_rc != 0 )); then
  rollback "dashboard 重启失败"
fi

# 再重启 reconciler（数据面不停）
set +e
restart_reconciler
restart_rc=$?
set -e
if (( restart_rc != 0 )); then
  rollback "reconciler 重启失败"
fi

check_data_plane "更新后" || rollback "更新后数据面健康检查失败"
check_dashboard "更新后" || rollback "更新后 dashboard 健康检查失败"

if unit_exists "$SERVICE_NAME"; then
  log "reconciler 状态：$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo unknown)"
fi
if unit_exists "$DASHBOARD_SERVICE"; then
  log "dashboard 状态：$(systemctl is-active "$DASHBOARD_SERVICE" 2>/dev/null || echo unknown)"
fi

# ---------------------------------------------------------------- 主动收敛 + 修复看板表
if (( DO_RECONCILE == 1 )); then
  log "执行一次 reconciler --once（补 pool.db deployments 表）"
  cd "$HOME_DIR"
  set -a
  [[ -f /etc/opencodelitelm-pool.env ]] && . /etc/opencodelitelm-pool.env
  set +a
  once_rc=1
  once_out=""
  for attempt in 1 2 3 4 5; do
    once_out="$(python3 "$HOME_DIR/pool_reconciler.py" --once 2>&1)" && once_rc=0 || once_rc=$?
    # 另一个 watch 进程持锁时会返回 skipped=locked；退避后重试
    if printf '%s' "$once_out" | grep -q '"skipped": "locked"'; then
      log "reconciler 被 watch 锁占用，第 $attempt 次重试…"
      sleep 2
      once_rc=1
      continue
    fi
    break
  done
  printf '%s\n' "$once_out" | tail -n 30
  if (( once_rc != 0 )); then
    warn "--once 失败（rc=$once_rc）。服务仍健康，代码不回滚；请查看 journalctl -u $SERVICE_NAME。"
    exit 2
  fi
fi

# 校验 pool.db deployments（看板「无部署」的直接数据源）
set +e
python3 - "$HOME_DIR/pool.db" "$HOME_DIR/pool-config.json" <<'PY'
import json, sqlite3, sys
db, cfg = sys.argv[1], sys.argv[2]
try:
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
    rows = conn.execute("SELECT slot, COUNT(*) FROM deployments GROUP BY slot").fetchall()
    total = conn.execute("SELECT COUNT(*) FROM deployments").fetchone()[0]
    conn.close()
except Exception as exc:
    print("WARN: pool.db 读取失败：%s" % exc)
    raise SystemExit(0)
enabled = []
try:
    doc = json.load(open(cfg, encoding="utf-8"))
    enabled = [a.get("slot") for a in (doc.get("accounts") or []) if a.get("enabled")]
except Exception:
    pass
print("deployments total=%d by_slot=%s enabled_slots=%s" % (total, rows, enabled))
if total == 0 and enabled:
    print("WARN: deployments 表仍为 0；若 reconciler --once 未补齐，请检查热路径或执行 gen_config.py")
    raise SystemExit(3)
PY
db_check_rc=$?
set -e
if (( db_check_rc != 0 )); then
  warn "deployments 表校验未通过（rc=$db_check_rc），服务本身仍健康；请人工确认"
  exit 2
fi

log "完成：代码已更新，reconciler 已重启，数据面健康，deployments 表已同步"
log "回滚备份保留在：$BACKUP_DIR"
