#!/usr/bin/env bash
# UBISmart Linux 控制面 + Agent Server 安装脚本（Phase D4）。
#
#   sudo packaging/systemd/install.sh \
#       --control-dir dist/UBISmartControl \
#       --agent-dir dist/UBISmartAgent
#
# 或从源码 venv 运行：
#   sudo packaging/systemd/install.sh --python /opt/coding-agent/venv/bin/python
#
# 只安装 A2/A3 与 systemd 单元；不安装 MySQL。首次安装会生成
# /etc/coding-agent/install-secrets.txt，请保存后删除。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALL_ROOT=/opt/coding-agent
DATA_ROOT=/var/lib/coding-agent
CONFIG_DIR=/etc/coding-agent
WORKSPACE_ROOT=/srv/coding-agent/workspaces
USER_NAME=coding-agent

CONTROL_DIR=""
AGENT_DIR=""
PYTHON=""
START=1
UNINSTALL=0

usage() {
  cat <<'USAGE'
用法：install.sh [选项]
  --control-dir PATH  PyInstaller 控制面目录（默认 dist/UBISmartControl）
  --agent-dir PATH    PyInstaller Agent 目录（默认 dist/UBISmartAgent）
  --python PATH       源码 venv 的 Python（与 --*-dir 二选一）
  --no-start          安装后不启动服务
  --uninstall         停止并移除程序与 systemd 单元（保留配置与数据）
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --control-dir) CONTROL_DIR="${2:-}"; shift 2 ;;
    --agent-dir) AGENT_DIR="${2:-}"; shift 2 ;;
    --python) PYTHON="${2:-}"; shift 2 ;;
    --no-start) START=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行（sudo）" >&2
  exit 2
fi

if [ "$UNINSTALL" -eq 1 ]; then
  systemctl disable --now coding-agent-control.service \
    coding-agent-agent.service coding-agent-backup.timer 2>/dev/null || true
  rm -f /etc/systemd/system/coding-agent-control.service \
    /etc/systemd/system/coding-agent-agent.service \
    /etc/systemd/system/coding-agent-backup.service \
    /etc/systemd/system/coding-agent-backup.timer
  systemctl daemon-reload
  rm -rf "$INSTALL_ROOT"
  echo "已移除程序与 systemd 单元。"
  echo "保留配置 $CONFIG_DIR 与数据 $DATA_ROOT（含 checkpoint/审计/备份）。"
  exit 0
fi

if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_ROOT" \
    --shell /usr/sbin/nologin --user-group "$USER_NAME"
fi
GROUP="$(id -gn "$USER_NAME")"

install -d -m 0755 -o root -g root "$INSTALL_ROOT" "$INSTALL_ROOT/bin"
install -d -m 0700 -o "$USER_NAME" -g "$GROUP" \
  "$DATA_ROOT" "$DATA_ROOT/backups"
install -d -m 0755 -o root -g root /srv/coding-agent
install -d -m 0700 -o "$USER_NAME" -g "$GROUP" "$WORKSPACE_ROOT"
install -d -m 0750 -o root -g "$GROUP" "$CONFIG_DIR"

random_token() {
  head -c 48 /dev/urandom | base64 | tr -d '=+/' | cut -c1-48
}

write_wrapper() {
  local name="$1" module="$2"
  cat > "$INSTALL_ROOT/bin/$name" <<EOF
#!/bin/sh
exec "$PYTHON" -m $module "\$@"
EOF
  chmod 0755 "$INSTALL_ROOT/bin/$name"
}

install_binaries() {
  if [ -n "$PYTHON" ]; then
    if [ ! -x "$PYTHON" ]; then
      echo "Python 不可执行：$PYTHON" >&2
      exit 2
    fi
    write_wrapper UBISmartControl ubismart.servers.control_server
    write_wrapper UBISmartAgent ubismart.servers.agent_server
    return
  fi

  CONTROL_DIR="${CONTROL_DIR:-$ROOT/dist/UBISmartControl}"
  AGENT_DIR="${AGENT_DIR:-$ROOT/dist/UBISmartAgent}"
  if [ ! -x "$CONTROL_DIR/UBISmartControl" ]; then
    echo "未找到 $CONTROL_DIR/UBISmartControl；请先运行 scripts/build_services.py" >&2
    exit 2
  fi
  if [ ! -x "$AGENT_DIR/UBISmartAgent" ]; then
    echo "未找到 $AGENT_DIR/UBISmartAgent；请先运行 scripts/build_services.py" >&2
    exit 2
  fi
  rm -rf "$INSTALL_ROOT/control" "$INSTALL_ROOT/agent"
  cp -a "$CONTROL_DIR" "$INSTALL_ROOT/control"
  cp -a "$AGENT_DIR" "$INSTALL_ROOT/agent"
  ln -sfn "$INSTALL_ROOT/control/UBISmartControl" \
    "$INSTALL_ROOT/bin/UBISmartControl"
  ln -sfn "$INSTALL_ROOT/agent/UBISmartAgent" \
    "$INSTALL_ROOT/bin/UBISmartAgent"
}

write_env_files() {
  if [ -e "$CONFIG_DIR/agent.env" ] || [ -e "$CONFIG_DIR/control.env" ]; then
    echo "已存在 $CONFIG_DIR/*.env，跳过生成。"
    echo "请确认两侧 AGENT_INTERNAL_TOKEN 一致。"
    return
  fi

  local internal web
  internal="$(random_token)"
  web="$(random_token)"

  umask 077
  cat > "$CONFIG_DIR/agent.env" <<EOF
AGENT_INTERNAL_TOKEN=$internal
AGENT_AGENT_DATA_ROOT=$DATA_ROOT/agent
AGENT_AUDIT_DIR=$DATA_ROOT/audit
AGENT_CHECKPOINT_PATH=$DATA_ROOT/checkpoints.sqlite
AGENT_STEERING_DIR=$DATA_ROOT/steering
AGENT_EXECUTION_BACKEND=local
EOF
  cat > "$CONFIG_DIR/control.env" <<EOF
AGENT_INTERNAL_URL=http://127.0.0.1:8790
AGENT_INTERNAL_TOKEN=$internal
AGENT_CONTROL_DB_AUTO_CREATE=false
AGENT_CONTROL_BACKUP_DIR=$DATA_ROOT/backups
AGENT_WEB_AUTH_TOKENS='{"$web":"admin"}'
# 配置 MySQL 后取消下一行注释，并重启 coding-agent-control。
# AGENT_CONTROL_DB_URL='mysql+pymysql://coding_agent:change-me@127.0.0.1:3306/coding_agent?charset=utf8mb4'
EOF
  chown root:"$GROUP" "$CONFIG_DIR/agent.env" "$CONFIG_DIR/control.env"
  chmod 0640 "$CONFIG_DIR/agent.env" "$CONFIG_DIR/control.env"

  cat > "$CONFIG_DIR/install-secrets.txt" <<EOF
AGENT_INTERNAL_TOKEN=$internal
AGENT_WEB_TOKEN=$web
EOF
  chown root:root "$CONFIG_DIR/install-secrets.txt"
  chmod 0600 "$CONFIG_DIR/install-secrets.txt"
}

install_units() {
  install -m 0644 "$ROOT/packaging/systemd/coding-agent-agent.service" \
    /etc/systemd/system/coding-agent-agent.service
  install -m 0644 "$ROOT/packaging/systemd/coding-agent-control.service" \
    /etc/systemd/system/coding-agent-control.service
  install -m 0644 "$ROOT/packaging/systemd/coding-agent-backup.service" \
    /etc/systemd/system/coding-agent-backup.service
  install -m 0644 "$ROOT/packaging/systemd/coding-agent-backup.timer" \
    /etc/systemd/system/coding-agent-backup.timer
  systemctl daemon-reload
  systemctl enable coding-agent-agent.service coding-agent-control.service
  # 备份 timer 默认启用；未配置 AGENT_CONTROL_DB_URL 时 service 会因
  # ExecCondition 直接跳过，不会每天失败。
  systemctl enable --now coding-agent-backup.timer
}

wait_health() {
  local url="$1"
  local _
  for _ in $(seq 1 20); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

verify() {
  if ! command -v curl >/dev/null 2>&1; then
    echo "未安装 curl，跳过健康检查"
    return
  fi
  echo "--- A3 healthz ---"
  if wait_health http://127.0.0.1:8790/healthz; then
    curl -fsS http://127.0.0.1:8790/healthz
  else
    echo "A3 未就绪，查看：journalctl -u coding-agent-agent -n 80"
  fi
  echo
  echo "--- A2 healthz ---"
  if wait_health http://127.0.0.1:8780/healthz; then
    curl -fsS http://127.0.0.1:8780/healthz
  else
    echo "A2 未就绪，查看：journalctl -u coding-agent-control -n 80"
  fi
  echo
  echo "查看日志：journalctl -u coding-agent-agent -u coding-agent-control -f"
}

install_binaries
write_env_files
install_units

if [ "$START" -eq 1 ]; then
  systemctl restart coding-agent-agent.service
  systemctl restart coding-agent-control.service
  verify
else
  echo "已安装但未启动；执行：systemctl start coding-agent-agent coding-agent-control"
fi

echo
echo "安装完成。"
echo "前端 token 与内部 token：$CONFIG_DIR/install-secrets.txt（保存后请删除）"
echo "工作区根：$WORKSPACE_ROOT"
