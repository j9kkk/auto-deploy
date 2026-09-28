#!/usr/bin/env bash
# Install AutoDeploy as a systemd service.
#
# Run as root from the project directory:
#   sudo ./scripts/install.sh
#
# Creates a dedicated system user, installs the service and starts it.  The
# admin password is generated on first start and printed to the journal.

set -euo pipefail

SERVICE_NAME="${AUTODEPLOY_SERVICE_NAME:-autodeploy}"
INSTALL_DIR="${AUTODEPLOY_INSTALL_DIR:-/opt/autodeploy}"
DATA_DIR="${AUTODEPLOY_DATA_DIR:-/var/lib/autodeploy}"
RUN_USER="${AUTODEPLOY_USER:-autodeploy}"
PORT="${AUTODEPLOY_PORT:-8770}"
HOST="${AUTODEPLOY_HOST:-0.0.0.0}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\033[36m[install]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "请以 root 运行（sudo $0）"
command -v systemctl >/dev/null 2>&1 || fail "未检测到 systemd，请手动部署或使用 docker"

log "安装目录: $INSTALL_DIR"
log "数据目录: $DATA_DIR"
log "运行用户: $RUN_USER"

# --- dependencies ----------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
  log "正在安装 git…"
  if command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -y -qq git
  elif command -v dnf >/dev/null 2>&1; then dnf install -y -q git
  elif command -v yum >/dev/null 2>&1; then yum install -y -q git
  else fail "未找到 git，请手动安装后重试"
  fi
fi

PYTHON_BIN="$(command -v python3 || true)"
[ -n "$PYTHON_BIN" ] || fail "未找到 python3，请先安装 Python 3.10+"

# --- user ------------------------------------------------------------------
if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  log "创建系统用户 $RUN_USER"
  useradd --system --create-home --home-dir "$DATA_DIR" --shell /bin/bash "$RUN_USER" \
    || fail "创建用户失败"
else
  log "用户 $RUN_USER 已存在，跳过创建"
fi

# The service user must be able to stop/restart services it deploys.
log "检查 sudo 权限（用于 systemd 部署方式）"
if [ ! -f "/etc/sudoers.d/$SERVICE_NAME" ]; then
  cat > "/etc/sudoers.d/$SERVICE_NAME" <<EOF
# Allow AutoDeploy to restart units it deploys.
$RUN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart *, /usr/bin/systemctl start *, /usr/bin/systemctl stop *, /usr/bin/systemctl is-active *, /usr/bin/systemctl daemon-reload
EOF
  chmod 0440 "/etc/sudoers.d/$SERVICE_NAME"
  visudo -c -f "/etc/sudoers.d/$SERVICE_NAME" >/dev/null 2>&1 || {
    rm -f "/etc/sudoers.d/$SERVICE_NAME"
    log "警告: sudoers 校验失败，已跳过；如需 systemd 部署请手动配置"
  }
fi

# --- files -----------------------------------------------------------------
log "复制程序文件到 $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
for item in app web requirements.txt run.sh; do
  rm -rf "${INSTALL_DIR:?}/$item"
  cp -R "$ROOT_DIR/$item" "$INSTALL_DIR/"
done
chmod +x "$INSTALL_DIR/run.sh"

mkdir -p "$DATA_DIR"
chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR" "$DATA_DIR"

log "创建虚拟环境并安装依赖"
if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
  sudo -u "$RUN_USER" "$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
fi
sudo -u "$RUN_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
sudo -u "$RUN_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check \
  -r "$INSTALL_DIR/requirements.txt" || fail "依赖安装失败"

# --- service ---------------------------------------------------------------
log "写入 systemd 单元 /etc/systemd/system/$SERVICE_NAME.service"
cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=AutoDeploy - GitHub 自动拉取与部署服务
Documentation=file://$INSTALL_DIR/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$INSTALL_DIR
Environment=AUTODEPLOY_DATA_DIR=$DATA_DIR
Environment=AUTODEPLOY_HOST=$HOST
Environment=AUTODEPLOY_PORT=$PORT
Environment=PYTHONUNBUFFERED=1
# The service executes user-provided deploy scripts; keep it in its own
# cgroup so a runaway build cannot starve the host.
ExecStart=$INSTALL_DIR/.venv/bin/python -m app.main --no-browser
Restart=always
RestartSec=5
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME
# Hardening: the deploy directories are the only places that must be writable.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
systemctl restart "$SERVICE_NAME"

log "等待服务启动…"
sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
  log "服务已启动 ✓"
else
  fail "服务启动失败，请查看：journalctl -u $SERVICE_NAME -n 50 --no-pager"
fi

cat <<EOF

======================================================================
  AutoDeploy 安装完成
======================================================================
  服务名     : $SERVICE_NAME
  访问地址   : http://<服务器IP>:$PORT/
  数据目录   : $DATA_DIR
  安装目录   : $INSTALL_DIR

  初始管理员密码在首次启动时生成，查看方式：
      journalctl -u $SERVICE_NAME -n 80 --no-pager | grep -A3 首次启动

  常用命令：
      systemctl status $SERVICE_NAME
      systemctl restart $SERVICE_NAME
      journalctl -u $SERVICE_NAME -f

  卸载：sudo $ROOT_DIR/scripts/uninstall.sh          （保留数据）
        sudo $ROOT_DIR/scripts/uninstall.sh --purge  （连数据一起删）

  如任务要使用 Docker 部署方式（docker build / docker compose），
  需要把服务账号加入 docker 组，否则会报
  "permission denied ... /var/run/docker.sock"：

      sudo usermod -aG docker $RUN_USER
      sudo systemctl restart $SERVICE_NAME

  注意：docker 组权限等价于 root，且本服务可执行任意部署脚本，
  请勿将控制台直接暴露到公网。

  如需公网访问，建议在前面加一层 Nginx 并启用 HTTPS，
  参考 deploy/nginx.conf.example，并记得在「设置」中开启
  「仅通过 HTTPS 发送会话 Cookie」。
======================================================================
EOF
