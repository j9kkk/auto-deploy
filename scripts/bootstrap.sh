#!/usr/bin/env bash
# AutoDeploy 一键部署 / 升级脚本（GHCR 预构建镜像 + Docker Compose）。
#
# 流程：检测系统环境并安装缺失的 Docker 组件 → 创建安装目录 /opt/auto-deploy
# → 下载 docker-compose.yml → 拉取 GitHub Actions 构建的官方镜像并启动。
# 无需克隆仓库，无需本地构建。
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/j9kkk/auto-deploy/main/scripts/bootstrap.sh | bash
#   重复执行同一条命令即升级到最新镜像（任务数据不受影响）。
#
# 可用环境变量：
#   AUTODEPLOY_VERSION       部署版本：main（默认，跟随 latest 镜像）或版本 tag
#                            （如 v0.2.0，compose 文件与镜像 tag 同步锁定）
#   AUTODEPLOY_INSTALL_DIR   安装目录（默认 /opt/auto-deploy）
#   AUTODEPLOY_PORT          服务端口（默认 8770）
#   AUTODEPLOY_IMAGE         镜像地址（默认 ghcr.io/j9kkk/auto-deploy，可换成镜像仓库）
#   AUTODEPLOY_COMPOSE_FILE  本地 docker-compose.yml 路径（离线安装用，跳过在线下载）

set -euo pipefail

RAW_BASE="https://raw.githubusercontent.com/j9kkk/auto-deploy"
VERSION="${AUTODEPLOY_VERSION:-main}"
INSTALL_DIR="${AUTODEPLOY_INSTALL_DIR:-/opt/auto-deploy}"
PORT="${AUTODEPLOY_PORT:-}"

log()  { printf '\033[32m[auto-deploy]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[auto-deploy]\033[0m %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || fail "缺少 curl，请先安装（apt/dnf/yum install curl）"

# 系统级改动逐条显式 sudo；绝不落盘自提权（bash 管道增量读 stdin，落盘副本会截断）。
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || fail "当前非 root 且未安装 sudo，请改用 root 执行"
  SUDO="sudo"
fi

# ---- Docker 引擎与 compose 插件：缺失则自动安装 ----
if ! command -v docker >/dev/null 2>&1; then
  log "未检测到 Docker，正在通过官方脚本安装（需要几分钟）…"
  curl -fsSL https://get.docker.com | ${SUDO} sh \
    || fail "Docker 自动安装失败，请手动安装：https://docs.docker.com/engine/install/"
  command -v docker >/dev/null 2>&1 || fail "Docker 已安装但当前会话找不到命令，请重开终端后重试"
fi

if ! docker compose version >/dev/null 2>&1; then
  fail "缺少 Docker Compose 插件（v2），请先安装 docker-compose-plugin 后重试"
fi

if docker info >/dev/null 2>&1; then
  DOCKER="docker"
else
  DOCKER="${SUDO} docker"   # 非 root 且不在 docker 组：统一走 sudo
fi

# ---- 安装目录 ----
# 旧版脚本曾把仓库克隆到安装目录；克隆残留与镜像部署混放会互相覆盖，直接拒绝。
if [ -d "${INSTALL_DIR}/.git" ]; then
  fail "目录 ${INSTALL_DIR} 是旧版克隆安装的残留，请先备份其中的 .env 后删除该目录再重试"
fi
if ! mkdir -p "${INSTALL_DIR}" 2>/dev/null; then
  ${SUDO} mkdir -p "${INSTALL_DIR}"
fi
cd "${INSTALL_DIR}"
if [ -w . ]; then
  SUDO=""   # 目录当前用户可写：后续文件操作无需提权
fi

# ---- docker-compose.yml：在线下载（自动重试），或离线指定本地文件 ----
if [ -n "${AUTODEPLOY_COMPOSE_FILE:-}" ]; then
  [ -f "${AUTODEPLOY_COMPOSE_FILE}" ] || fail "离线文件不存在：${AUTODEPLOY_COMPOSE_FILE}"
  ${SUDO} install -m 644 "${AUTODEPLOY_COMPOSE_FILE}" ./docker-compose.yml
  log "已使用本地 compose 文件（离线安装）"
else
  log "下载 docker-compose.yml（${VERSION}）…"
  TMP_COMPOSE="$(mktemp)"
  DOWNLOADED=0
  for attempt in 1 2 3; do
    if curl -fsSL "${RAW_BASE}/${VERSION}/docker-compose.yml" -o "${TMP_COMPOSE}"; then
      DOWNLOADED=1
      break
    fi
    log "下载失败，3 秒后重试（第 ${attempt} 次）…"
    sleep 3
  done
  [ "${DOWNLOADED}" -eq 1 ] || fail "compose 文件下载失败：请确认能访问 GitHub 后重试（受限网络可先 export https_proxy=代理地址 再执行）"
  grep -q "^services:" "${TMP_COMPOSE}" || fail "下载内容不是有效的 compose 文件，已中止"
  ${SUDO} install -m 644 "${TMP_COMPOSE}" ./docker-compose.yml
  rm -f "${TMP_COMPOSE}"
fi

# ---- 端口 / 镜像：写入 .env 供 compose 读取；已有 .env 时尊重现有配置 ----
if [ ! -f .env ] || [ -n "${PORT}" ] || [ -n "${AUTODEPLOY_IMAGE+x}" ] \
   || { [ -n "${AUTODEPLOY_VERSION+x}" ] && [ "${VERSION}" != "main" ]; }; then
  {
    printf 'AUTODEPLOY_PORT=%s\n' "${PORT:-8770}"
    if [ -n "${AUTODEPLOY_VERSION+x}" ] && [ "${VERSION}" != "main" ]; then
      # 锁定版本时同步锁定镜像 tag（GHCR 的语义化标签不含 v 前缀）
      printf 'AUTODEPLOY_IMAGE_TAG=%s\n' "${VERSION#v}"
    fi
    if [ -n "${AUTODEPLOY_IMAGE+x}" ]; then
      printf 'AUTODEPLOY_IMAGE=%s\n' "${AUTODEPLOY_IMAGE}"
    fi
  } | ${SUDO} tee .env >/dev/null
fi
SERVICE_PORT="$(grep -E '^AUTODEPLOY_PORT=' .env 2>/dev/null | tail -n 1 | cut -d= -f2 || true)"
SERVICE_PORT="${SERVICE_PORT:-8770}"

# ---- 拉取镜像并启动 ----
log "拉取官方镜像并启动（首次需要下载几百 MB）…"
if ! ${DOCKER} compose pull; then
  log "镜像拉取失败；若离线环境已通过 docker load 导入镜像，将使用本地镜像继续"
fi
${DOCKER} compose up -d --no-build

# ---- 健康检查 ----
log "等待服务就绪（最长 90 秒）…"
READY=0
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:${SERVICE_PORT}/api/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done
if [ "${READY}" -ne 1 ]; then
  fail "服务未就绪，请查看日志：cd ${INSTALL_DIR} && ${DOCKER} compose logs autodeploy"
fi

# ---- 初始密码（仅首次启动打印一次）----
if ${DOCKER} compose logs autodeploy 2>&1 | grep -q "首次启动"; then
  echo
  log "管理员账号已创建，初始密码如下（仅显示这一次，请立即登录并修改）："
  ${DOCKER} compose logs autodeploy 2>&1 | grep -A 3 "首次启动" || true
else
  log "服务已就绪。重复执行本脚本即可升级到最新镜像，任务数据不受影响"
fi

echo
log "部署完成：http://$(hostname -I 2>/dev/null | awk '{print $1}' || true):${SERVICE_PORT}/"
log "常用命令（在 ${INSTALL_DIR} 下执行）："
echo "  查看日志:  docker compose logs -f autodeploy"
echo "  停止服务:  docker compose down        （数据保留）"
echo "  彻底删除:  docker compose down -v     （连数据卷一起删除）"
log "升级方式：重新执行本脚本，拉取最新镜像并滚动重启，任务数据不受影响"
