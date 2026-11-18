#!/usr/bin/env bash
# OpencodeLitellmPool · 独立部署引导脚本（幂等，可重复执行）
#
# 一条命令起步（全新 Linux 主机）：
#   sudo ./deploy/bootstrap.sh
#
# 步骤（全部幂等）：
#   1. 预检：Linux / root / docker(+compose v2) / python3(>=3.10)
#   2. 准备 POOL_HOME（默认 /opt/litellm）并平铺安装各组件
#   3. 从仓库根 .env.example 生成 ${POOL_HOME}/.env（600）；缺 key 时交互式安全录入
#   4. 跑一次 gen_config.py 生成 config.yaml
#   5. docker compose up -d 启动 LiteLLM 并等待健康检查
#   6. 安装/启动 systemd 服务：sidecar / dashboard / reconciler（无 systemd 时 nohup 兜底）
#   7. 冒烟验证并打印下一步
#
# 安全纪律：
#   · 绝不 echo / tee / set -x 任何密钥；密钥只经管道写入 .env（600），终端不回显；
#   · 不安装 gen_config 的 cron（运行期热变更由 pool_reconciler.py 负责，避免双时间表重启竞争）；
#   · 默认只监听 127.0.0.1，不碰防火墙、不装 Halo、不配管理员账号。
#
# 用法：sudo ./deploy/bootstrap.sh [--check] [--no-systemd] [--non-interactive]
#                                 [--allow-snapshot] [--pull] [-h|--help]
# 退出码：0 成功；非 0 失败并打印原因。

set -Eeuo pipefail
umask 022

# ---------------------------------------------------------------------------
# 路径与默认值
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
DEPLOY_DIR="$SCRIPT_DIR"
DEPLOY_ENV="$DEPLOY_DIR/.env"
DEPLOY_ENV_EXAMPLE="$DEPLOY_DIR/.env.example"
POOL_ENV_EXAMPLE="$REPO_ROOT/.env.example"
SERVICE_ENV_FILE="/etc/opencodelitelm-pool.env"
COMPOSE_PROJECT="opencodelitelm-pool"

# 调用方显式传入的部署变量优先级最高（其次是 deploy/.env，最后是下面的默认值）
declare -A ENV_OVERRIDES=()
for _v in POOL_HOME LITELLM_IMAGE LITELLM_PORT DASHBOARD_PORT RECONCILE_INTERVAL; do
  if [[ -n "${!_v:-}" ]]; then ENV_OVERRIDES["$_v"]="${!_v}"; fi
done
unset _v

# 可被 deploy/.env 覆盖，也可被环境变量覆盖
POOL_HOME="${POOL_HOME:-/opt/litellm}"
LITELLM_IMAGE="${LITELLM_IMAGE:-ghcr.io/berriai/litellm:main-stable}"
LITELLM_PORT="${LITELLM_PORT:-4000}"
DASHBOARD_PORT="${DASHBOARD_PORT:-4010}"
RECONCILE_INTERVAL="${RECONCILE_INTERVAL:-60}"

# 运行时状态（不是用户配置）
CHECK_ONLY=0
SYSTEMD_MODE="auto"   # auto | yes | no
USE_SYSTEMD=0
NON_INTERACTIVE=0
ALLOW_SNAPSHOT=0
PULL=0
FIRST_INSTALL=0
CONFIG_CHANGED=0
ENV_FILE=""           # 在 prepare_home 里确定

# ---------------------------------------------------------------------------
# 日志 / 错误处理
# ---------------------------------------------------------------------------
step()  { printf '\n[bootstrap] ── %s ──\n' "$*"; }
info()  { printf '[bootstrap] %s\n' "$*"; }
warn()  { printf '[bootstrap] WARN: %s\n' "$*" >&2; }
die()   { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

on_err() {
  local rc=$?
  printf '[bootstrap] ERROR: 命令失败（rc=%s，行 %s）：%s\n' \
    "$rc" "$LINENO" "$BASH_COMMAND" >&2
  printf '[bootstrap] 提示：修复后可直接重跑本脚本（幂等）；带 --check 只做预检。\n' >&2
}
trap on_err ERR

usage() {
  cat <<'EOF'
OpencodeLitellmPool 一键部署（幂等）

用法：
  sudo ./deploy/bootstrap.sh [选项]

选项：
  --check            只做预检，不修改任何文件/容器/服务
  --no-systemd       不安装 systemd 服务，改用 nohup 后台启动（调试/无 systemd 主机）
  --systemd          强制使用 systemd（没有则报错退出）
  --non-interactive  非交互：缺 key 时直接报错，不弹出录入提示
  --allow-snapshot   gen_config.py 未能生成 config.yaml 时，回退使用仓库快照启动
  --pull             强制拉取最新 LiteLLM 镜像（默认仅在本机无镜像时拉取）
  -h, --help         显示本帮助

前置：Linux + Docker(Compose v2) + python3 >= 3.10 + root；至少 1 把 OpenCode Go 上游 key。
部署参数（路径/端口/镜像/间隔）改 deploy/.env，不写死在脚本里。
EOF
}

# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
parse_args() {
  while (( $# )); do
    case "$1" in
      --check)           CHECK_ONLY=1 ;;
      --no-systemd)      SYSTEMD_MODE="no" ;;
      --systemd)         SYSTEMD_MODE="yes" ;;
      --non-interactive) NON_INTERACTIVE=1 ;;
      --allow-snapshot)  ALLOW_SNAPSHOT=1 ;;
      --pull)            PULL=1 ;;
      -h|--help)         usage; exit 0 ;;
      *)                 die "未知参数：$1（--help 查看用法）" ;;
    esac
    shift
  done
}

# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------
systemd_usable() {
  command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]
}

check_linux() {
  [[ "$(uname -s)" == "Linux" ]] || die "仅支持 Linux（当前 $(uname -s)）。Windows/macOS 请用 WSL2/虚拟机。"
}

check_root() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "请用 root 运行：sudo $0（需要写 $POOL_HOME、安装 systemd 服务、操作 docker）"
}

check_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令 $1：$2"
}

check_python() {
  check_cmd python3 "安装 Python 3.10+（Debian/Ubuntu: apt install python3；RHEL: dnf install python3）"
  python3 - <<'PY' || die "python3 版本过低：需要 >= 3.10（当前 $(python3 -V 2>&1)）"
import sys
if sys.version_info < (3, 10):
    sys.exit(1)
PY
}

check_docker() {
  check_cmd docker "安装 Docker Engine：https://docs.docker.com/engine/install/"
  docker info >/dev/null 2>&1 || die "docker 守护进程不可用（systemctl enable --now docker）"
  docker compose version >/dev/null 2>&1 \
    || die "需要 Docker Compose v2（命令为 'docker compose'；旧的 docker-compose 不支持）"
}

port_in_use() {
  python3 - "$1" <<'PY'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(0)   # 被占用
finally:
    s.close()
sys.exit(1)
PY
}

check_ports() {
  (( FIRST_INSTALL == 1 )) || return 0
  if port_in_use "$LITELLM_PORT" && ! docker inspect litellm >/dev/null 2>&1; then
    warn "端口 ${LITELLM_PORT} 已被占用（LiteLLM 启动可能失败）：ss -ltnp | grep :${LITELLM_PORT}"
  fi
  if port_in_use "$DASHBOARD_PORT" && ! systemctl is-active --quiet opencodelitelm-pool-dashboard 2>/dev/null; then
    warn "端口 ${DASHBOARD_PORT} 已被占用（看板启动可能失败）：ss -ltnp | grep :${DASHBOARD_PORT}"
  fi
}

preflight() {
  step "预检"
  check_linux
  check_root
  check_cmd install
  check_python
  check_docker

  case "$SYSTEMD_MODE" in
    yes)
      systemd_usable || die "--systemd 指定使用 systemd，但本机没有可用的 systemd"
      USE_SYSTEMD=1 ;;
    no)
      USE_SYSTEMD=0 ;;
    auto)
      if systemd_usable; then USE_SYSTEMD=1
      else
        USE_SYSTEMD=0
        warn "未检测到可用的 systemd，将用 nohup 后台启动（生产环境建议用 systemd）"
      fi ;;
  esac

  FIRST_INSTALL=0
  [[ -f "$POOL_HOME/.env" ]] || FIRST_INSTALL=1
  check_ports

  info "Linux / root / docker compose v2 / python3 检查通过"
  info "docker: $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?')；python3: $(python3 -V 2>&1)"
  info "服务管理方式：$( ((USE_SYSTEMD == 1)) && echo systemd || echo nohup )"
}

# ---------------------------------------------------------------------------
# deploy/.env 加载
# ---------------------------------------------------------------------------
load_deploy_env() {
  if [[ -f "$DEPLOY_ENV" ]]; then
    # shellcheck disable=SC1090
    set -a
    . "$DEPLOY_ENV" || die "deploy/.env 解析失败，请检查语法（$DEPLOY_ENV）"
    set +a
  fi
  # 显式环境变量优先于 deploy/.env（例如 sudo POOL_HOME=/data/litellm ./bootstrap.sh）
  local name
  for name in "${!ENV_OVERRIDES[@]}"; do
    printf -v "$name" '%s' "${ENV_OVERRIDES[$name]}"
    export "${name?}"
  done
}

ensure_deploy_env() {
  [[ -f "$DEPLOY_ENV" ]] && { chmod 600 "$DEPLOY_ENV" 2>/dev/null || true; return 0; }
  [[ -f "$DEPLOY_ENV_EXAMPLE" ]] || die "缺少 deploy/.env.example"
  install -m 600 "$DEPLOY_ENV_EXAMPLE" "$DEPLOY_ENV"
  info "已生成 deploy/.env（部署参数模板；可改路径/端口后重跑）"
}

# ---------------------------------------------------------------------------
# POOL_HOME 与组件安装
# ---------------------------------------------------------------------------
ensure_compat_symlink() {
  local real_home
  real_home="$(readlink -f "$POOL_HOME")"
  [[ "$real_home" == "/opt/litellm" ]] && return 0

  if [[ -L /opt/litellm ]]; then
    if [[ "$(readlink -f /opt/litellm)" == "$real_home" ]]; then
      return 0
    fi
    die "/opt/litellm 已是指向别处的软链（-> $(readlink -f /opt/litellm)），与 POOL_HOME=$real_home 冲突"
  elif [[ -e /opt/litellm ]]; then
    die "POOL_HOME 取非默认值（$real_home）时，需要 /opt/litellm 作为兼容软链，但它已是真实目录。
     处置：① 用默认 POOL_HOME=/opt/litellm；或 ② 迁移旧文件后 mv /opt/litellm /opt/litellm.bak 再重跑。
     原因：gen_config.py 与 dashboard.py 目前仍硬编码 /opt/litellm（原生支持是后续项）。"
  fi
  ln -s "$real_home" /opt/litellm
  info "已创建兼容软链 /opt/litellm -> $real_home（gen_config.py / dashboard.py 仍硬编码 /opt/litellm）"
}

prepare_home() {
  step "准备 POOL_HOME：$POOL_HOME"
  mkdir -p "$POOL_HOME/logs" "$POOL_HOME/run"
  chmod 750 "$POOL_HOME"
  chmod 750 "$POOL_HOME/logs" "$POOL_HOME/run"
  ensure_compat_symlink
}

find_source() {
  local name=$1 cand
  for cand in "$REPO_ROOT/pool/$name" "$REPO_ROOT/pool-dash/$name" "$REPO_ROOT/$name"; do
    if [[ -f "$cand" ]]; then
      printf '%s\n' "$cand"
      return 0
    fi
  done
  return 1
}

install_component() {
  local src=$1 dest=$2
  if [[ -f "$dest" ]] && cmp -s "$src" "$dest"; then
    info "组件已最新：$(basename "$dest")"
  else
    install -m 644 "$src" "$dest"
    info "已安装：$(basename "$dest")"
  fi
}

install_components() {
  step "安装组件到 $POOL_HOME"
  local required=(session-sidecar.py gen_config.py upstream_policy.py pool_logger.py toolrepair.py share-engine.py dashboard.py)
  local optional=(pool_control.py pool_reconciler.py pool_admin.py pool_admin_ui.py model-allowlist.json)
  local name src

  for name in "${required[@]}"; do
    src="$(find_source "$name")" || die "仓库缺少必需组件 $name（期望在 pool/ 或 pool-dash/ 下）"
    install_component "$src" "$POOL_HOME/$name"
  done

  for name in "${optional[@]}"; do
    if src="$(find_source "$name")"; then
      install_component "$src" "$POOL_HOME/$name"
    fi
  done

  if [[ -d "$REPO_ROOT/pool/tools" ]]; then
    mkdir -p "$POOL_HOME/tools"
    cp -a "$REPO_ROOT/pool/tools/." "$POOL_HOME/tools/" 2>/dev/null \
      || warn "复制 pool/tools 失败（不影响主流程）"
    find "$POOL_HOME/tools" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
  fi

  [[ -f "$DEPLOY_DIR/README.md" ]] && install -m 644 "$DEPLOY_DIR/README.md" "$POOL_HOME/deploy-README.md"
}

# ---------------------------------------------------------------------------
# .env（密钥）处理 —— 全程不打印值
# ---------------------------------------------------------------------------
read -r -d '' ENV_UPSERT_PY <<'PY' || true
import os
import sys
path, key = sys.argv[1], sys.argv[2]
val = sys.stdin.read().strip()
lines, found = [], False
try:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith(key + "="):
                lines.append(key + "=" + val + "\n")
                found = True
            else:
                lines.append(line)
except FileNotFoundError:
    pass
if not found:
    lines.append(key + "=" + val + "\n")
tmp = path + ".tmp-bootstrap"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.writelines(lines)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY

upsert_env() {
  local key=$1 value=$2
  printf '%s' "$value" | python3 -c "$ENV_UPSERT_PY" "$ENV_FILE" "$key"
}

env_get() {
  local name=$1 line val
  [[ -f "$ENV_FILE" ]] || return 1
  line="$(grep -E "^${name}=" "$ENV_FILE" | tail -n 1 || true)"
  [[ -n "$line" ]] || return 1
  val="${line#*=}"
  val="${val%$'\r'}"
  val="${val#\"}"; val="${val%\"}"
  val="${val#\'}"; val="${val%\'}"
  printf '%s' "$val"
}

is_placeholder() {
  local v=$1
  [[ -z "$v" ]] && return 0
  [[ "$v" == "replace-me" || "$v" == "REPLACE_ME" || "$v" == "REPLACE-ME" ]] && return 0
  [[ "$v" == *"<pool-"* || "$v" == "sk-pool-…" || "$v" == "sk-pool-..." ]] && return 0
  return 1
}

valid_secret() {
  local v=$1
  (( ${#v} >= 16 )) && [[ "$v" != *[[:space:]]* ]]
}

count_valid_keys() {
  local n=0 line val
  [[ -f "$ENV_FILE" ]] || { printf '0'; return 0; }
  while IFS= read -r line; do
    val="${line#*=}"
    val="${val%$'\r'}"
    val="${val#\"}"; val="${val%\"}"
    val="${val#\'}"; val="${val%\'}"
    if ! is_placeholder "$val" && valid_secret "$val"; then
      n=$((n + 1))
    fi
  done < <(grep -E '^OCG_KEY_[0-9]+=' "$ENV_FILE" 2>/dev/null || true)
  printf '%s' "$n"
}

ensure_pool_env_file() {
  ENV_FILE="$POOL_HOME/.env"
  if [[ ! -f "$ENV_FILE" ]]; then
    [[ -f "$POOL_ENV_EXAMPLE" ]] || die "缺少仓库根 .env.example"
    install -m 600 "$POOL_ENV_EXAMPLE" "$ENV_FILE"
    info "已从 .env.example 生成 ${ENV_FILE}（600；尚未填 key）"
  else
    chmod 600 "$ENV_FILE" 2>/dev/null || true
  fi
}

ensure_master_key() {
  local cur gen
  cur="$(env_get LITELLM_MASTER_KEY || true)"
  if is_placeholder "$cur"; then
    gen="$(python3 - <<'PY'
import secrets
print("sk-pool-master-" + secrets.token_hex(24))
PY
)"
    upsert_env LITELLM_MASTER_KEY "$gen"
    unset gen
    info "已自动生成 LITELLM_MASTER_KEY（内部主键，不打印；客户端用 sk-pool-* 实例键）"
  else
    info "LITELLM_MASTER_KEY 已配置"
  fi
}

prompt_for_keys() {
  local existing after i k
  existing="$(count_valid_keys)"
  if (( existing > 0 )); then
    info "已配置 ${existing} 把上游 key（OCG_KEY_n；值不打印）"
    return 0
  fi
  if (( NON_INTERACTIVE == 1 )); then
    die "未配置上游 key 且指定了 --non-interactive。请编辑 ${ENV_FILE} 填 OCG_KEY_1=…（600）后重跑"
  fi
  if [[ ! -t 0 ]]; then
    die "未配置上游 key 且当前不是交互终端。请编辑 ${ENV_FILE} 填 OCG_KEY_1=…（600）后重跑"
  fi

  printf '\n[bootstrap] 未检测到可用的上游账号 key（OCG_KEY_n）。\n' >&2
  printf '[bootstrap] 现在安静录入：输入不回显、不写终端日志；直接回车结束。\n' >&2
  i=1
  while (( i <= 12 )); do
    read -r -s -p "  第 ${i} 把 OpenCode Go 上游 key（回车结束）: " k || break
    printf '\n' >&2
    [[ -z "$k" ]] && break
    if is_placeholder "$k" || ! valid_secret "$k"; then
      warn "key 形态不对（至少 16 字符且不含空白/占位符），请重新输入第 ${i} 把"
      continue
    fi
    upsert_env "OCG_KEY_$i" "$k"
    unset k
    i=$((i + 1))
  done

  after="$(count_valid_keys)"
  if (( after == 0 )); then
    die "仍未配置上游 key。请编辑 ${ENV_FILE} 填 OCG_KEY_1=…（权限 600）后重跑本脚本"
  fi
  info "已写入 ${after} 把上游 key（OCG_KEY_n，值不打印）"
}

ensure_instance_keys() {
  local f="$POOL_HOME/dsh-instance-keys.json"
  if [[ -s "$f" ]]; then
    chmod 600 "$f" 2>/dev/null || true
    info "客户端实例键文件已存在（不覆盖）"
    return 0
  fi
  python3 - "$f" <<'PY'
import json
import os
import secrets
import sys

path = sys.argv[1]
key = "sk-pool-" + secrets.token_hex(16)
tmp = path + ".tmp-bootstrap"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump({key: "bootstrap-first-key"}, fh, ensure_ascii=False, indent=1)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
  info "已签发首个客户端实例键 → $f（不打印；也可在看板「管理控制台」页签追加/吊销）"
}

prepare_runtime_files() {
  [[ -e "$POOL_HOME/pool-requests.jsonl" ]] || install -m 600 /dev/null "$POOL_HOME/pool-requests.jsonl"
  [[ -e "$POOL_HOME/logs/bootstrap.log" ]] || install -m 600 /dev/null "$POOL_HOME/logs/bootstrap.log"
}

load_runtime_env() {
  local buckets admin
  buckets="$(env_get POOL_SESSION_BUCKETS || true)"
  [[ -n "$buckets" ]] || buckets=8
  [[ "$buckets" =~ ^[0-9]+$ ]] || die "POOL_SESSION_BUCKETS 必须是整数（当前：$buckets）"
  export POOL_SESSION_BUCKETS="$buckets"
  admin="$(env_get POOL_ADMIN_HOSTS || true)"
  [[ -n "$admin" ]] || admin="$(env_get POOL_ADMIN_ALLOW || true)"
  if [[ -n "$admin" ]]; then
    export POOL_ADMIN_HOSTS="$admin"
    export POOL_ADMIN_ALLOW="$admin"
  fi
  if [[ "$LITELLM_PORT" != "4000" ]]; then
    warn "LITELLM_PORT=${LITELLM_PORT}：sidecar / dashboard / reconciler 默认都连 127.0.0.1:4000（组件内固定），
     改端口会让链路断开。除非你同步改了这些组件，否则请保持 LITELLM_PORT=4000。"
  fi
  export POOL_HOME LITELLM_IMAGE LITELLM_PORT DASHBOARD_PORT RECONCILE_INTERVAL
}

# ---------------------------------------------------------------------------
# .env 规整 + 控制面初始化
# ---------------------------------------------------------------------------
prune_empty_key_lines() {
  python3 - "$ENV_FILE" <<'PY'
import os
import sys

path = sys.argv[1]
kept, removed = [], []
with open(path, encoding="utf-8") as fh:
    for line in fh:
        stripped = line.strip()
        if stripped.startswith("OCG_KEY_") and "=" in stripped:
            name, _, val = stripped.partition("=")
            val = val.strip().strip('"').strip("'")
            if not val or val.upper().startswith("REPLACE") or "<pool-" in val:
                removed.append(name.strip())
                continue
        kept.append(line)
if removed:
    tmp = path + ".tmp-bootstrap"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(kept)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
print("[bootstrap] 已清理空/占位账号行：%s" % (", ".join(removed) if removed else "无"))
PY
}

seed_control_plane() {
  [[ -f "$POOL_HOME/pool_control.py" ]] || {
    warn "未找到 pool_control.py：跳过控制面初始化（管理控制台页签热更新不可用）"
    return 0
  }
  step "初始化控制面（pool-config.json，仅首次）"
  if ! python3 - "$POOL_HOME" <<'PY'
import json
import os
import re
import sys

home = sys.argv[1]
os.environ["POOL_HOME"] = home
sys.path.insert(0, home)
import pool_control

# 已初始化过 → 绝不覆盖（看板「管理控制台」页签是之后的事实源）
try:
    if pool_control.load_config(force=True) is not None:
        print("[bootstrap] 控制面已初始化，跳过（不覆盖看板「管理控制台」写入的配置）")
        sys.exit(0)
except Exception:
    pass

allow, seen = [], set()
try:
    with open(os.path.join(home, "config.yaml"), encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"\s*-\s*model_name:\s*(\S+)\s*$", line)
            if not m:
                continue
            name = m.group(1)
            if "@" in name or name in seen:   # 池内别名 <model>@a<N> 不外泄
                continue
            seen.add(name)
            allow.append(name)
except FileNotFoundError:
    pass

accounts = []
with open(os.path.join(home, ".env"), encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line.startswith("OCG_KEY_") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        slot = name.rsplit("_", 1)[-1]
        if not slot.isdigit():
            continue
        val = val.strip().strip('"').strip("'")
        if not val or val.upper().startswith("REPLACE") or "<pool-" in val:
            continue
        accounts.append({"slot": int(slot), "key_var": name, "enabled": True})
accounts.sort(key=lambda a: a["slot"])

if not allow or not accounts:
    print("[bootstrap] WARN: allowlist/账号为空，跳过控制面初始化（首次在「管理控制台」页签写入时会创建）")
    sys.exit(0)

pool_control.update_models(allow, actor="bootstrap")
pool_control.update_accounts(accounts, actor="bootstrap")
print("[bootstrap] 控制面已初始化：models=%d，accounts=%d（之后请在「管理控制台」页签管理）"
      % (len(allow), len(accounts)))
PY
  then
    warn "控制面初始化失败（不影响数据面；可在「管理控制台」页签重新写入，或手工排查 pool_control.py）"
  fi
}

# ---------------------------------------------------------------------------
# 生成 config.yaml
# ---------------------------------------------------------------------------
generate_config() {
  step "生成 LiteLLM 配置（gen_config.py，只写文件不重启）"
  local cfg="$POOL_HOME/config.yaml" before="" after="" count=0 buckets
  buckets="$POOL_SESSION_BUCKETS"

  [[ -f "$cfg" ]] && before="$(sha256sum "$cfg" | awk '{print $1}')"

  if ( cd "$POOL_HOME" \
        && POOL_HOME="$POOL_HOME" POOL_SKIP_RESTART=1 POOL_SESSION_BUCKETS="$buckets" \
           python3 gen_config.py ) 2>&1 | tee -a "$POOL_HOME/logs/bootstrap.log"; then
    :
  else
    die "gen_config.py 执行失败（见上方输出）。常见原因：上游不可达 / key 无效 / 通道策略拒绝写盘"
  fi

  [[ -f "$cfg" ]] && after="$(sha256sum "$cfg" | awk '{print $1}')"
  count="$(grep -c 'model_name:' "$cfg" 2>/dev/null || true)"

  if [[ "${count:-0}" -eq 0 ]]; then
    if (( ALLOW_SNAPSHOT == 1 )); then
      [[ -f "$REPO_ROOT/pool/config.yaml" ]] || die "gen_config 未生成 config.yaml，且仓库无 pool/config.yaml 快照"
      install -m 644 "$REPO_ROOT/pool/config.yaml" "$cfg"
      # 快照可能引用 .env 里没有的 OCG_KEY_n；缺任何一个都会让 LiteLLM 启动失败，先拦住
      local missing="" name val
      while IFS= read -r name; do
        val="$(env_get "$name" || true)"
        if is_placeholder "$val"; then missing="$missing $name"; fi
      done < <(grep -oE 'os\.environ/OCG_KEY_[0-9]+' "$cfg" | sed 's#os\.environ/##' | sort -u)
      if [[ -n "$missing" ]]; then
        die "仓库快照引用了未配置的账号：$missing
     --allow-snapshot 只适合「账号集合与快照一致」的应急场景；请先在 ${ENV_FILE} 补齐这些 key 再试"
      fi
      warn "gen_config 未写出有效 config.yaml，已回退仓库快照启动（请尽快在「管理控制台」页签修正账号/模型）"
      CONFIG_CHANGED=1
    else
      die "gen_config.py 未生成有效 config.yaml（常见原因见 $POOL_HOME/deploy-README.md「常见故障」）。
     可加 --allow-snapshot 先用仓库快照启动；或检查 ${ENV_FILE} 里的 key 与到 opencode.ai 的网络。"
    fi
  else
    info "config.yaml 就绪：${count} 个 model_name"
    if [[ -n "$before" && "$before" == "$after" ]]; then
      CONFIG_CHANGED=0
      info "config.yaml 内容未变化"
    else
      CONFIG_CHANGED=1
      info "config.yaml 已更新（部署期稍后按需重启容器加载；运行期变更由 reconciler 热应用）"
    fi
  fi
}

# ---------------------------------------------------------------------------
# 容器生命周期
# ---------------------------------------------------------------------------
check_legacy_container() {
  if docker inspect litellm >/dev/null 2>&1; then
    local proj
    proj="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' litellm 2>/dev/null || true)"
    if [[ -z "$proj" || "$proj" != "$COMPOSE_PROJECT" ]]; then
      die "已存在同名容器 litellm（来源：${proj:-docker run / 其它 compose 项目}）。
     这是旧部署（docker run）或别的 compose 项目占用。升级请先在旧机备份，
     执行 docker rm -f litellm 后重跑本脚本（容器内无持久数据：config/.env 都在宿主 $POOL_HOME）。"
    fi
  fi
}

container_created_epoch() {
  local created
  created="$(docker inspect -f '{{.Created}}' litellm 2>/dev/null)" || return 1
  date -d "$created" +%s 2>/dev/null || return 1
}

start_container() {
  step "启动 LiteLLM 容器（docker compose up -d）"
  if (( PULL == 1 )) || ! docker image inspect "$LITELLM_IMAGE" >/dev/null 2>&1; then
    info "拉取镜像：$LITELLM_IMAGE"
    docker pull "$LITELLM_IMAGE" || die "镜像拉取失败（检查网络/镜像源）：$LITELLM_IMAGE"
  fi

  local existed=0 old_id="" new_id="" need_recreate=0
  old_id="$(docker inspect -f '{{.Id}}' litellm 2>/dev/null || true)"
  [[ -n "$old_id" ]] && existed=1

  if (( existed == 1 )); then
    local cepoch env_mtime
    cepoch="$(container_created_epoch || echo 0)"
    env_mtime="$(stat -c %Y "$ENV_FILE" 2>/dev/null || echo 0)"
    if (( env_mtime > cepoch )); then
      need_recreate=1
      info "检测到 .env 比容器新 → 将重建容器（restart 不会重读 env-file）"
    fi
  fi

  if (( need_recreate == 1 )); then
    ( cd "$DEPLOY_DIR" && docker compose -p "$COMPOSE_PROJECT" up -d --force-recreate ) \
      || die "docker compose up --force-recreate 失败（见上方输出）"
  else
    ( cd "$DEPLOY_DIR" && docker compose -p "$COMPOSE_PROJECT" up -d ) \
      || die "docker compose up 失败（见上方输出）"
  fi

  new_id="$(docker inspect -f '{{.Id}}' litellm 2>/dev/null || true)"
  if (( existed == 1 )) && [[ -n "$new_id" && "$old_id" == "$new_id" ]] && (( CONFIG_CHANGED == 1 )); then
    docker restart litellm >/dev/null || die "docker restart litellm 失败"
    info "config.yaml 有变化，已重启容器加载（部署期操作；运行期热变更走 reconciler，不需要重启）"
  fi
}

wait_http() {
  python3 - "$1" "$2" <<'PY'
import sys
import time
import urllib.request

url, timeout = sys.argv[1], float(sys.argv[2])
deadline = time.time() + timeout
last = ""
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            if 200 <= resp.status < 500:
                sys.exit(0)
    except Exception as exc:  # noqa: BLE001 - 探测循环，任何错误都继续重试
        last = str(exc)[:160]
    time.sleep(2)
sys.stderr.write("最后一次错误：%s\n" % last)
sys.exit(1)
PY
}

wait_litellm() {
  step "等待 LiteLLM 健康检查"
  if wait_http "http://127.0.0.1:${LITELLM_PORT}/health/liveliness" 180; then
    info "LiteLLM 健康检查通过：127.0.0.1:${LITELLM_PORT}"
    return 0
  fi
  ( cd "$DEPLOY_DIR" && docker compose -p "$COMPOSE_PROJECT" logs --tail=80 litellm ) 2>&1 || true
  die "LiteLLM 在 180s 内未就绪（容器日志见上）。
     常见原因：config.yaml 引用了未注入的 os.environ、端口 ${LITELLM_PORT} 被占用、镜像/网络问题。
     排查：docker ps -a | grep litellm；docker logs --tail=100 litellm"
}

check_reconciler() {
  [[ -f "$POOL_HOME/pool_reconciler.py" ]] || return 0
  step "reconciler 预检（--check）"
  if ( cd "$POOL_HOME" && POOL_HOME="$POOL_HOME" python3 pool_reconciler.py --check ) \
       2>&1 | tee -a "$POOL_HOME/logs/bootstrap.log"; then
    info "reconciler 预检通过"
  else
    warn "reconciler --check 未通过（运行期热应用可能不可用；数据面不受影响，详见部署 README）"
  fi
}

# ---------------------------------------------------------------------------
# 服务启动：systemd 优先，否则 nohup 兜底
# ---------------------------------------------------------------------------
write_service_env_file() {
  local buckets admin_hosts admin_allow
  buckets="$(env_get POOL_SESSION_BUCKETS || true)"
  [[ -n "$buckets" ]] || buckets=8
  admin_hosts="$(env_get POOL_ADMIN_HOSTS || true)"
  admin_allow="$(env_get POOL_ADMIN_ALLOW || true)"
  umask 077
  {
    printf '# 由 deploy/bootstrap.sh 生成；重跑会覆盖，勿手改。\n'
    printf '# 只放路径/端口/间隔等非密钥参数；上游 key 在 %s/.env。\n' "$POOL_HOME"
    printf 'POOL_HOME=%s\n' "$POOL_HOME"
    printf 'POOL_DASHBOARD_PORT=%s\n' "$DASHBOARD_PORT"
    printf 'POOL_RECONCILE_INTERVAL=%s\n' "$RECONCILE_INTERVAL"
    printf 'POOL_SESSION_BUCKETS=%s\n' "$buckets"
    if [[ -n "$admin_hosts" ]]; then
      printf 'POOL_ADMIN_HOSTS=%s\n' "$admin_hosts"
    fi
    if [[ -n "$admin_allow" ]]; then
      printf 'POOL_ADMIN_ALLOW=%s\n' "$admin_allow"
    fi
  } > "$SERVICE_ENV_FILE"
  chmod 600 "$SERVICE_ENV_FILE"
  umask 022
}

restart_unit() {
  local unit=$1
  if ! systemctl restart "$unit"; then
    systemctl status "$unit" --no-pager -l 2>&1 | tail -n 20 >&2 || true
    die "服务 $unit 启动失败：journalctl -u $unit -n 100 --no-pager"
  fi
}

stop_nohup_services() {
  local pidf pid
  for pidf in "$POOL_HOME"/run/*.pid; do
    [[ -f "$pidf" ]] || continue
    pid="$(cat "$pidf" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      info "已停止 nohup 模式进程 $(basename "$pidf" .pid)（pid $pid），改由 systemd 接管"
    fi
    rm -f "$pidf"
  done
}

install_services_systemd() {
  step "安装并启动 systemd 服务"
  stop_nohup_services
  write_service_env_file

  local units=(opencodelitelm-pool-sidecar.service opencodelitelm-pool-dashboard.service \
               opencodelitelm-pool-reconciler.service opencodelitelm-pool-share-engine.service \
               opencodelitelm-pool-share-engine.timer)
  local unit
  for unit in "${units[@]}"; do
    [[ -f "$DEPLOY_DIR/systemd/$unit" ]] || die "缺少 unit 文件 deploy/systemd/$unit"
    install -m 644 "$DEPLOY_DIR/systemd/$unit" "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload

  systemctl enable opencodelitelm-pool-sidecar >/dev/null 2>&1 || true
  systemctl enable opencodelitelm-pool-dashboard >/dev/null 2>&1 || true
  restart_unit opencodelitelm-pool-sidecar
  restart_unit opencodelitelm-pool-dashboard
  info "sidecar（127.0.0.1:4001）与 dashboard（127.0.0.1:${DASHBOARD_PORT}）已启动"

  if [[ -f "$POOL_HOME/pool_reconciler.py" ]]; then
    systemctl enable opencodelitelm-pool-reconciler >/dev/null 2>&1 || true
    restart_unit opencodelitelm-pool-reconciler
    info "reconciler 已启动（--watch --interval ${RECONCILE_INTERVAL}s，热应用 .env/allowlist 变更）"
  else
    warn "未找到 $POOL_HOME/pool_reconciler.py：跳过 reconciler（运行期热应用不可用；装好后重跑本脚本）"
  fi

  info "share-engine unit 已安装但未启用（可选）：systemctl enable --now opencodelitelm-pool-share-engine.timer"
  info "日志：journalctl -u opencodelitelm-pool-sidecar -f"
}

start_background_services() {
  step "使用 nohup 后台启动（无 systemd 模式）"
  local name pidf logf old pids=()

  if systemd_usable && systemctl is-active --quiet opencodelitelm-pool-sidecar 2>/dev/null; then
    warn "systemd 单元 opencodelitelm-pool-* 仍在运行；请先
     sudo systemctl disable --now opencodelitelm-pool-sidecar opencodelitelm-pool-dashboard opencodelitelm-pool-reconciler
     再使用 --no-systemd，否则端口冲突（4001/4010）"
  fi

  start_bg() {
    name=$1; shift
    pidf="$POOL_HOME/run/$name.pid"
    logf="$POOL_HOME/logs/$name.log"
    if [[ -f "$pidf" ]]; then
      old="$(cat "$pidf" 2>/dev/null || true)"
      if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
        kill "$old" 2>/dev/null || true
        sleep 1
      fi
      rm -f "$pidf"
    fi
    nohup "$@" >> "$logf" 2>&1 &
    printf '%s\n' "$!" > "$pidf"
    chmod 600 "$pidf"
    sleep 1
    if kill -0 "$(cat "$pidf")" 2>/dev/null; then
      info "已后台启动 $name（pid $(cat "$pidf")，日志 $logf）"
    else
      die "$name 启动失败，见 $logf"
    fi
  }

  start_bg sidecar python3 "$POOL_HOME/session-sidecar.py"
  start_bg dashboard python3 "$POOL_HOME/dashboard.py" --listen "127.0.0.1:${DASHBOARD_PORT}"
  if [[ -f "$POOL_HOME/pool_reconciler.py" ]]; then
    start_bg reconciler python3 "$POOL_HOME/pool_reconciler.py" --watch --interval "$RECONCILE_INTERVAL"
  else
    warn "未找到 $POOL_HOME/pool_reconciler.py：跳过 reconciler"
  fi
  info "停止方式：kill \$(cat $POOL_HOME/run/*.pid)"
}

# ---------------------------------------------------------------------------
# 冒烟验证
# ---------------------------------------------------------------------------
verify_deployment() {
  step "冒烟验证"
  local rc=0
  python3 - "$POOL_HOME" "$DASHBOARD_PORT" <<'PY' || rc=$?
import json
import os
import sys
import time
import urllib.error
import urllib.request

home, dash_port = sys.argv[1], sys.argv[2]

def get(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也是有效响应（如 Host 白名单 403）
        return exc.code, exc.read()

# ---- 1) sidecar /v1/models（用实例键，不打印键值）----
instance_key = ""
try:
    with open(os.path.join(home, "dsh-instance-keys.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    for k in data:
        if not str(k).startswith("_"):
            instance_key = str(k)
            break
except Exception:
    pass

if not instance_key:
    print("[verify] WARN: 未找到客户端实例键，跳过 /v1/models 检查", file=sys.stderr)
else:
    deadline = time.time() + 45
    last = ""
    while time.time() < deadline:
        try:
            status, body = get("http://127.0.0.1:4001/v1/models",
                               {"Authorization": "Bearer " + instance_key})
            if status == 200:
                try:
                    ids = [m.get("id") for m in json.loads(body).get("data", [])]
                except Exception:
                    ids = []
                print("[verify] sidecar /v1/models: HTTP 200, %d 个模型" % len(ids))
                if ids:
                    print("[verify] 模型示例: " + ", ".join(str(i) for i in ids[:5]))
                last = ""
                break
            last = "HTTP %s" % status
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:160]
        time.sleep(2)
    if last:
        print("[verify] ERROR: sidecar /v1/models 不可用（%s）" % last, file=sys.stderr)
        sys.exit(2)

# ---- 2) 看板首页 + 管理面接口 ----
deadline = time.time() + 30
last = ""
status = None
while time.time() < deadline:
    try:
        status, _ = get("http://127.0.0.1:%s/" % dash_port)
        break
    except Exception as exc:  # noqa: BLE001
        last = str(exc)[:160]
    time.sleep(2)
if status is None:
    print("[verify] ERROR: 看板端口 %s 不可达（%s）" % (dash_port, last), file=sys.stderr)
    sys.exit(3)
if status == 200:
    print("[verify] 看板首页 /: HTTP 200")
else:
    print("[verify] WARN: 看板首页 / 返回 HTTP %s" % status)

# 2026-10-17 起管理控制台是看板内的一个页签，独立页 /admin 已下线；
# 因此冒烟验证改打管理面接口本身（Host 白名单只作用于 /api/admin/*）。
try:
    astatus, _abody = get("http://127.0.0.1:%s/api/admin/status" % dash_port)
except Exception as exc:  # noqa: BLE001
    astatus = None
    print("[verify] WARN: 管理面 /api/admin/status 不可达（%s）" % str(exc)[:160])
if astatus == 200:
    print("[verify] 管理面 /api/admin/status: HTTP 200（看板「管理控制台」页签可用）")
elif astatus in (401, 403):
    print("[verify] WARN: 管理面 /api/admin/status 返回 HTTP %s（Host 白名单 POOL_ADMIN_HOSTS 已生效）" % astatus)
elif astatus is not None:
    print("[verify] WARN: 管理面 /api/admin/status 返回 HTTP %s（pool_admin*/pool_admin_ui 缺失时看板降级只读，主页面仍可用）" % astatus)
PY

  case "$rc" in
    0) ;;
    2) die "sidecar 冒烟失败。排查：systemctl status opencodelitelm-pool-sidecar；journalctl -u opencodelitelm-pool-sidecar -n 100" ;;
    3) die "看板端口不可达。排查：systemctl status opencodelitelm-pool-dashboard；journalctl -u opencodelitelm-pool-dashboard -n 100" ;;
    *) die "冒烟验证失败（rc=$rc）" ;;
  esac
}

# ---------------------------------------------------------------------------
# 完成摘要
# ---------------------------------------------------------------------------
print_summary() {
  cat <<EOF

[bootstrap] 部署完成 ✅
  POOL_HOME          : $POOL_HOME
  LiteLLM（容器）    : 127.0.0.1:${LITELLM_PORT}（容器名 litellm，env 来自 $POOL_HOME/.env）
  sidecar（客户端）  : 127.0.0.1:4001
  看板（含管理页签） : http://127.0.0.1:${DASHBOARD_PORT}/  ，管理控制台直达 http://127.0.0.1:${DASHBOARD_PORT}/#admin
  服务管理           : $( ((USE_SYSTEMD == 1)) && echo "systemctl status opencodelitelm-pool-{sidecar,dashboard,reconciler}" || echo "$POOL_HOME/run/*.pid + $POOL_HOME/logs/*.log" )

下一步：
  1. 客户端 baseURL：http://127.0.0.1:4001/v1（本机）；远程经反代：https://<pool-host>/pool/v1
  2. 客户端实例键：$POOL_HOME/dsh-instance-keys.json（首个已自动签发，仅存文件；也可在「管理控制台」页签管理）
  3. 改上游账号密钥 / 模型 allowlist：打开看板 →「管理控制台」页签（热生效，不需要重启容器）
  4. 验证：curl -s -H "Authorization: Bearer sk-pool-…" http://127.0.0.1:4001/v1/models
  5. 本机默认只监听 127.0.0.1；远程访问请自备 VPN / 反代鉴权（本项目不内置账号体系）
  6. 完整说明：$POOL_HOME/deploy-README.md（仓库内 deploy/README.md）

EOF
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
main() {
  parse_args "$@"

  if [[ ! -f "$DEPLOY_ENV" ]]; then
    if (( CHECK_ONLY == 1 )); then
      info "deploy/.env 尚不存在（--check 不生成，使用默认参数）"
    else
      ensure_deploy_env
    fi
  fi
  load_deploy_env

  export POOL_HOME LITELLM_IMAGE LITELLM_PORT DASHBOARD_PORT RECONCILE_INTERVAL

  preflight
  if (( CHECK_ONLY == 1 )); then
    info "预检通过（--check：未做任何修改）"
    exit 0
  fi

  prepare_home
  install_components
  ensure_pool_env_file
  load_runtime_env
  ensure_master_key
  prompt_for_keys
  prune_empty_key_lines
  ensure_instance_keys
  prepare_runtime_files
  generate_config
  seed_control_plane
  check_legacy_container
  start_container
  wait_litellm
  check_reconciler

  if (( USE_SYSTEMD == 1 )); then
    install_services_systemd
  else
    start_background_services
  fi

  verify_deployment
  print_summary
}

main "$@"
