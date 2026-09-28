#!/usr/bin/env bash
# 卸载 AutoDeploy 的 systemd 安装。
#
# 默认行为是安全的：停止并删除服务本身，但保留数据目录与账号，
# 以免误删任务配置、运行历史与已部署的站点。
# 确认不再需要时，用 --purge 一并删除：
#   sudo ./scripts/uninstall.sh --purge
#
# 预览模式（不改动任何东西）：
#   sudo ./scripts/uninstall.sh --dry-run
#
# 安装时用过非默认路径的，通过同样的环境变量指定：
#   sudo AUTODEPLOY_INSTALL_DIR=/srv/autodeploy AUTODEPLOY_DATA_DIR=/data/autodeploy \
#        AUTODEPLOY_SERVICE_NAME=autodeploy ./scripts/uninstall.sh --purge

set -euo pipefail

SERVICE_NAME="${AUTODEPLOY_SERVICE_NAME:-autodeploy}"
INSTALL_DIR="${AUTODEPLOY_INSTALL_DIR:-/opt/autodeploy}"
DATA_DIR="${AUTODEPLOY_DATA_DIR:-/var/lib/autodeploy}"
RUN_USER="${AUTODEPLOY_USER:-autodeploy}"
# systemd 单元与 sudoers 目录。真实部署固定为 /etc 下的标准路径；
# 留出环境变量仅为测试与特殊容器环境使用。
UNIT_DIR="${AUTODEPLOY_UNIT_DIR:-/etc/systemd/system}"
SUDOERS_DIR="${AUTODEPLOY_SUDOERS_DIR:-/etc/sudoers.d}"

PURGE=0
DRY_RUN=0

log()  { printf '\033[36m[uninstall]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[uninstall]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[uninstall]\033[0m %s\n' "$*" >&2; exit 1; }

for arg in "$@"; do
  case "$arg" in
    --purge)   PURGE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      fail "未知参数: ${arg}（可用 --purge / --dry-run / --help）" ;;
  esac
done

if [ "$(id -u 2>/dev/null || echo 1)" != "0" ]; then
  fail "请以 root 运行（sudo $0）"
fi
command -v systemctl >/dev/null 2>&1 || fail "未检测到 systemd；此脚本仅用于 systemd 安装的卸载"

run() {
  # 统一的执行入口：--dry-run 下只打印将要执行的命令，不产生任何改动。
  if [ "$DRY_RUN" -eq 1 ]; then
    log "[预览] $*"
  else
    log "$ $*"
    "$@"
  fi
}

done_msg() {
  # run() 真正执行后的完成提示；dry-run 模式下不打印，避免误导。
  [ "$DRY_RUN" -eq 1 ] || log "$*"
}

# ---------------------------------------------------------------------------
# 0. 提示哪些「目标目录」不会被卸载触碰
#
# 任务把构建产物发布到 target_dir（如 /var/www/blog），这些站点由 nginx 等
# 直接服务，删除它们超出了卸载服务的职责——但必须显式说出来，否则操作者
# 可能误以为站点也会被清理。
# ---------------------------------------------------------------------------
list_deploy_targets() {
  local db="$DATA_DIR/autodeploy.db"
  [ -f "$db" ] || return 0
  if command -v sqlite3 >/dev/null 2>&1; then
    # 数据库损坏或表结构变化都不能中止卸载，这里显式吞掉错误。
    sqlite3 "$db" "SELECT DISTINCT target_dir FROM tasks WHERE trim(target_dir) != '';" 2>/dev/null || true
  elif [ "$PURGE" -eq 1 ] && [ -x "$INSTALL_DIR/.venv/bin/python" ]; then
    # 没有 sqlite3 CLI 时退回服务自带的 Python 读库（只在 --purge 时需要）。
    "$INSTALL_DIR/.venv/bin/python" - "$db" <<'PY' 2>/dev/null || true
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
for (target,) in conn.execute(
    "SELECT DISTINCT target_dir FROM tasks WHERE trim(target_dir) != ''"
):
    print(target)
PY
  fi
}

TARGETS="$(list_deploy_targets)"
if [ -n "$TARGETS" ]; then
  warn "以下目录是任务发布过的站点，本脚本不会改动它们："
  while IFS= read -r target; do
    [ -n "$target" ] && warn "    $target"
  done <<< "$TARGETS"
  if [ "$PURGE" -eq 1 ]; then
    warn "如需一并清理，请确认无服务依赖后自行删除。"
  fi
fi

# ---------------------------------------------------------------------------
# 1. 停止并禁用服务
# ---------------------------------------------------------------------------
if systemctl list-unit-files --type=service 2>/dev/null | grep -q "^$SERVICE_NAME"; then
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    log "停止服务 ${SERVICE_NAME}"
    run systemctl stop "$SERVICE_NAME"
  else
    log "服务未在运行，跳过 stop"
  fi
  if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    log "禁用开机自启"
    run systemctl disable "$SERVICE_NAME"
  fi
else
  log "未找到服务 ${SERVICE_NAME}，跳过 stop/disable"
fi

# 正在执行的部署子进程属于独立进程组，stop 只发主进程；等待片刻让
# TimeoutStopSec 的收尾逻辑跑完，再进入删除阶段。
if [ "$DRY_RUN" -eq 0 ] && systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE_NAME"; then
  for _ in 1 2 3 4 5 6; do
    systemctl is-active --quiet "$SERVICE_NAME" || break
    sleep 1
  done
fi

# ---------------------------------------------------------------------------
# 2. 删除 systemd 单元与 sudoers 授权
# ---------------------------------------------------------------------------
UNIT_FILE="$UNIT_DIR/$SERVICE_NAME.service"
if [ -f "$UNIT_FILE" ]; then
  run rm -f "$UNIT_FILE"
  run systemctl daemon-reload
  run systemctl reset-failed "$SERVICE_NAME"
  done_msg "已删除 ${UNIT_FILE}"
else
  log "${UNIT_FILE} 不存在，跳过"
fi

SUDOERS_FILE="$SUDOERS_DIR/$SERVICE_NAME"
if [ -f "$SUDOERS_FILE" ]; then
  run rm -f "$SUDOERS_FILE"
  done_msg "已删除 sudoers 授权 ${SUDOERS_FILE}"
else
  log "${SUDOERS_FILE} 不存在，跳过"
fi

# ---------------------------------------------------------------------------
# 3. 删除程序目录
# ---------------------------------------------------------------------------
if [ -d "$INSTALL_DIR" ]; then
  run rm -rf "$INSTALL_DIR"
  done_msg "已删除程序目录 ${INSTALL_DIR}"
else
  log "${INSTALL_DIR} 不存在，跳过"
fi

# ---------------------------------------------------------------------------
# 4. 数据目录与账号：默认保留，--purge 才删除
# ---------------------------------------------------------------------------
if [ "$PURGE" -eq 0 ]; then
  warn "数据目录已保留: ${DATA_DIR}"
  warn "服务账号已保留: ${RUN_USER}"
  warn "任务配置、运行历史与打包产物都在其中；确认不再需要时重新运行并加 --purge"
  log "卸载完成"
  exit 0
fi

# --- --purge：删除数据目录与账号 -------------------------------------------
if [ "$DRY_RUN" -eq 0 ] && [ -d "$DATA_DIR" ] && [ "${AUTODEPLOY_PURGE_CONFIRMED:-0}" != "1" ]; then
  warn "即将永久删除数据目录及其全部内容（任务配置、数据库、运行历史、发布与产物）："
  warn "    ${DATA_DIR}  （$(du -sh "${DATA_DIR}" 2>/dev/null | cut -f1)）"
  printf '确认请输入 YES：'
  read -r answer
  [ "$answer" = "YES" ] || fail "已取消，未做任何删除"
fi

if [ -d "$DATA_DIR" ]; then
  run rm -rf "$DATA_DIR"
  done_msg "已删除数据目录 ${DATA_DIR}"
else
  log "${DATA_DIR} 不存在，跳过"
fi

# 只删除本安装创建的账号：install.sh 在账号已存在时会跳过创建，
# 卸载时同样不能误删一个预先存在的账号。
if id -u "$RUN_USER" >/dev/null 2>&1; then
  run userdel "$RUN_USER"
  done_msg "已删除服务账号 ${RUN_USER}"
else
  log "账号 ${RUN_USER} 不存在，跳过"
fi

log "卸载完成（已清除全部数据）"
