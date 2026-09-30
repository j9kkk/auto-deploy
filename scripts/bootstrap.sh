#!/usr/bin/env bash
# 一键安装 / 升级 AutoDeploy（systemd 部署）。
#
# 不需要先克隆仓库，在目标服务器上执行一条命令即可：
#   curl -fsSL https://raw.githubusercontent.com/j9kkk/git-deploy/main/scripts/bootstrap.sh | sudo bash
# 以文件方式执行且非 root 时（克隆仓库或下载后 bash bootstrap.sh），会自动通过
# sudo 提权；管道模式 bash 增量读取 stdin，无法落盘自提权，因此要求显式 sudo。
#
# 重复执行同一条命令即升级到最新发布版本：
#   - 数据目录不会被改动（任务配置、运行历史与已发布的站点都保留）；
#   - 安装时用环境变量自定义过的服务名/目录/端口，会从现有 systemd 单元
#     读取并沿用，升级不会把它们冲回默认值；
#   - systemd 单元与 sudoers 授权始终由当前版本的 install.sh 重写，
#     后续版本新增的授权或加固随升级自动生效。
#
# 环境变量（与 scripts/install.sh 一致，sudo 提权时会自动透传）：
#   AUTODEPLOY_SERVICE_NAME   systemd 服务名    （默认 autodeploy）
#   AUTODEPLOY_INSTALL_DIR    安装目录          （默认 /opt/autodeploy）
#   AUTODEPLOY_DATA_DIR       数据目录          （默认 /var/lib/autodeploy）
#   AUTODEPLOY_USER           服务账号          （默认 autodeploy）
#   AUTODEPLOY_PORT           监听端口          （默认 8770）
#   AUTODEPLOY_HOST           监听地址          （默认 0.0.0.0）
#   AUTODEPLOY_PYTHON         Python 解释器     （默认 python3，需 3.10+）
#   AUTODEPLOY_REF            安装版本          （默认最新发布 tag，可指定分支/标签）
#   AUTODEPLOY_REPO_URL       仓库地址          （默认本项目，fork 部署时可改写）
#   AUTODEPLOY_UNIT_DIR       systemd 单元目录  （默认 /etc/systemd/system，测试用）
#
# 需要代理时先 export http_proxy / https_proxy，再执行上面的命令。

set -euo pipefail

DEFAULT_REPO_URL="https://github.com/j9kkk/git-deploy"
REPO_URL="${AUTODEPLOY_REPO_URL:-$DEFAULT_REPO_URL}"
UNIT_DIR="${AUTODEPLOY_UNIT_DIR:-/etc/systemd/system}"
ONE_LINER="curl -fsSL https://raw.githubusercontent.com/j9kkk/git-deploy/main/scripts/bootstrap.sh | sudo bash"

log()  { printf '\033[36m[一键部署]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[一键部署]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[一键部署]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 前置检查（放在提权之前：环境不满足时先失败，避免无谓地索取 sudo 密码） --
command -v systemctl >/dev/null 2>&1 \
  || fail "未检测到 systemd：本脚本用于 systemd 部署，其它环境请参考 README 手动运行"

# 从既有 systemd 单元继承安装参数（环境变量显式指定的优先）。
inherit_unit_settings() {
  local unit="$1" val key
  for key in AUTODEPLOY_DATA_DIR AUTODEPLOY_PORT AUTODEPLOY_HOST; do
    if [ -z "${!key:-}" ]; then
      val="$(sed -n "s/^Environment=$key=//p" "$unit" | tail -n1 | tr -d '\r')"
      val="${val%\"}"; val="${val#\"}"
      if [ -n "$val" ]; then export "$key=$val"; fi
    fi
  done
  if [ -z "${AUTODEPLOY_INSTALL_DIR:-}" ]; then
    val="$(sed -n 's/^WorkingDirectory=//p' "$unit" | tail -n1 | tr -d '\r')"
    if [ -n "$val" ]; then export AUTODEPLOY_INSTALL_DIR="$val"; fi
  fi
  if [ -z "${AUTODEPLOY_USER:-}" ]; then
    val="$(sed -n 's/^User=//p' "$unit" | tail -n1 | tr -d '\r')"
    if [ -n "$val" ]; then export AUTODEPLOY_USER="$val"; fi
  fi
}

# --- 提权：安装环节需要 root ------------------------------------------------
# 以文件方式执行（克隆仓库或下载后 bash bootstrap.sh）且非 root 时，
# 自动通过 sudo 重新执行，环境变量经 sudo env 显式透传（sudo 默认会重置环境）。
# 管道执行（curl … | bash）时不做落盘自提权：bash 是增量读取 stdin 的，
# 执行到落盘语句时脚本开头已被消费，落盘副本是截断的——此时直接提示
# 使用带 sudo 的一行命令。
if [ "$(id -u)" != "0" ]; then
  if [ "${AUTODEPLOY_ESCALATED:-0}" = "1" ]; then
    fail "通过 sudo 重新执行后仍不是 root：请检查 sudo 配置，或改用 root 执行"
  fi
  self="${BASH_SOURCE[0]:-$0}"
  if [ ! -f "$self" ] || [ ! -r "$self" ]; then
    fail "管道执行时请使用带 sudo 的一行命令：${ONE_LINER}（或先把脚本保存为文件再执行）"
  fi
  command -v sudo >/dev/null 2>&1 \
    || fail "安装需要 root 权限：请用 sudo 重新执行（sudo bash ${self}）"
  extra_env=()
  for var in AUTODEPLOY_SERVICE_NAME AUTODEPLOY_INSTALL_DIR AUTODEPLOY_DATA_DIR \
             AUTODEPLOY_USER AUTODEPLOY_PORT AUTODEPLOY_HOST AUTODEPLOY_PYTHON \
             AUTODEPLOY_REF AUTODEPLOY_REPO_URL AUTODEPLOY_UNIT_DIR; do
    if [ -n "${!var:-}" ]; then extra_env+=("$var=${!var}"); fi
  done
  log "需要 root 权限，正在通过 sudo 重新执行…"
  exec sudo env AUTODEPLOY_ESCALATED=1 ${extra_env[@]+"${extra_env[@]}"} bash "$self" "$@"
fi

# --- 基本依赖 ---------------------------------------------------------------
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
  fail "未找到 curl 或 wget：请先安装其一（如 apt install curl）"
fi

fetch() {  # fetch <url> <输出文件>
  # 连接阶段限时 15 秒：网络不通时快速失败并走候选地址/明确的报错提示，
  # 而不是按系统默认等上一分多钟。
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --connect-timeout 15 "$1" -o "$2"
  else
    wget -qO "$2" --timeout=15 "$1"
  fi
}

# --- 识别已有安装 ------------------------------------------------------------
SERVICE_NAME="${AUTODEPLOY_SERVICE_NAME:-}"
EXISTING_UNIT=""
if [ -z "$SERVICE_NAME" ] && [ -f "$UNIT_DIR/autodeploy.service" ]; then
  SERVICE_NAME="autodeploy"
fi
if [ -z "$SERVICE_NAME" ]; then
  # 兼容用自定义服务名安装过的场景：本项目写出的单元带有 AUTODEPLOY_ 标记。
  for unit_file in "$UNIT_DIR"/*.service; do
    if [ -f "$unit_file" ] && grep -q "AUTODEPLOY_SERVICE_NAME=" "$unit_file" 2>/dev/null; then
      SERVICE_NAME="$(basename "$unit_file" .service)"
      break
    fi
  done
fi
if [ -n "$SERVICE_NAME" ] && [ -f "$UNIT_DIR/$SERVICE_NAME.service" ]; then
  EXISTING_UNIT="$UNIT_DIR/$SERVICE_NAME.service"
  export AUTODEPLOY_SERVICE_NAME="$SERVICE_NAME"
  inherit_unit_settings "$EXISTING_UNIT"
  log "检测到已有安装：${EXISTING_UNIT}（沿用其服务名/目录/端口等配置）"
fi

# --- 决定安装版本：与应用内「版本更新」一致，默认最新发布 tag -----------------
REF="${AUTODEPLOY_REF:-}"
if [ -n "$REF" ]; then
  log "安装指定版本: $REF"
elif command -v curl >/dev/null 2>&1; then
  # 跟随 releases/latest 的重定向拿到最新发布 tag（不走 API，无速率限制）。
  latest_url="$(curl -fsSLI --connect-timeout 15 -o /dev/null -w '%{url_effective}' "$REPO_URL/releases/latest" 2>/dev/null || true)"
  case "$latest_url" in
    */releases/tag/*)
      REF="${latest_url%%\?*}"   # 去掉可能的查询串
      REF="${REF##*/}"
      ;;
  esac
  if [ -n "$REF" ]; then
    log "最新发布版本: $REF"
  else
    REF="main"
    warn "无法解析最新发布版本，改用 main 分支"
  fi
else
  REF="main"
  warn "未安装 curl，跳过版本探测，改用 main 分支"
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/autodeploy-bootstrap.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT
SRC="$WORK_DIR/src"

download_tree() {  # download_tree <tarball 地址>
  local url="$1" tgz="$WORK_DIR/src.tar.gz"
  rm -rf "$SRC"
  log "下载代码: $url"
  if ! fetch "$url" "$tgz"; then return 1; fi
  if ! tar -tzf "$tgz" >/dev/null 2>&1; then
    warn "下载内容不是有效的压缩包"
    return 1
  fi
  mkdir -p "$SRC"
  tar -xzf "$tgz" -C "$SRC" --strip-components=1
}

candidates=()
if [ -n "${AUTODEPLOY_REF:-}" ]; then
  # 用户指定版本：可能是 tag 也可能是分支，两者都试。
  candidates+=("$REPO_URL/archive/refs/tags/$REF.tar.gz" "$REPO_URL/archive/refs/heads/$REF.tar.gz")
elif [ "$REF" = "main" ]; then
  candidates+=("$REPO_URL/archive/refs/heads/main.tar.gz")
else
  candidates+=("$REPO_URL/archive/refs/tags/$REF.tar.gz" "$REPO_URL/archive/refs/heads/main.tar.gz")
fi
downloaded=0
for url in "${candidates[@]}"; do
  if download_tree "$url"; then downloaded=1; break; fi
  warn "下载失败，尝试下一个来源"
done
if [ "$downloaded" != "1" ]; then
  fail "下载失败：请检查服务器能否访问 ${REPO_URL}（需要代理时先 export https_proxy=… 再重试）"
fi

# --- 版本对比 ----------------------------------------------------------------
read_version() {  # read_version <源码目录>
  sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$1/app/__init__.py" 2>/dev/null | tail -n1
}
NEW_VERSION="$(read_version "$SRC")"
if [ -z "$NEW_VERSION" ]; then NEW_VERSION="未知"; fi
CURRENT_VERSION=""
if [ -n "${AUTODEPLOY_INSTALL_DIR:-}" ] && [ -f "$AUTODEPLOY_INSTALL_DIR/app/__init__.py" ]; then
  CURRENT_VERSION="$(read_version "$AUTODEPLOY_INSTALL_DIR")"
fi
if [ -n "$CURRENT_VERSION" ]; then
  if [ "$CURRENT_VERSION" = "$NEW_VERSION" ]; then
    log "当前已是 v${CURRENT_VERSION}，将进行修复式重装（同步单元/授权并重置程序文件）"
  else
    log "检测到已有安装 v${CURRENT_VERSION}，升级到 v${NEW_VERSION}"
  fi
else
  log "准备全新安装 v$NEW_VERSION"
fi

# --- 全部系统改动交给当前版本的 install.sh -----------------------------------
# 用户创建、sudoers、单元文件、虚拟环境都由 install.sh 负责：未来版本的安装
# 逻辑变化（新增授权、加固单元等）无需改动本脚本即自动生效。
log "开始安装…"
bash "$SRC/scripts/install.sh"

if [ "$REPO_URL" = "$DEFAULT_REPO_URL" ]; then
  log "以后升级或修复，重新执行同一条命令即可："
  log "    $ONE_LINER"
fi
