#!/usr/bin/env bash
# AutoDeploy 一键部署 / 升级脚本（Docker Compose 版）。
#
# 用法：
#   远程一键部署（自动安装 Docker、克隆源码、构建并启动）：
#     curl -fsSL https://raw.githubusercontent.com/j9kkk/auto-deploy/main/scripts/bootstrap.sh | bash
#   已克隆仓库时本地执行；重复执行即升级到最新代码：
#     ./scripts/bootstrap.sh
#
# 可用环境变量：
#   AUTODEPLOY_REPO_URL     代码仓库地址（默认官方仓库）
#   AUTODEPLOY_BRANCH       分支或 tag（默认 main）
#   AUTODEPLOY_INSTALL_DIR  源码目录（默认 /opt/auto-deploy）
#   AUTODEPLOY_PORT         服务端口（默认 8770）
#
# 任务数据保存在 Docker 数据卷 auto-deploy_autodeploy-data 中，升级不影响数据。

set -euo pipefail

REPO_URL="${AUTODEPLOY_REPO_URL:-https://github.com/j9kkk/auto-deploy.git}"
BRANCH="${AUTODEPLOY_BRANCH:-main}"
INSTALL_DIR="${AUTODEPLOY_INSTALL_DIR:-/opt/auto-deploy}"
PORT="${AUTODEPLOY_PORT:-}"

log()  { printf '\033[32m[auto-deploy]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[auto-deploy]\033[0m %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || fail "缺少 curl，请先安装（apt/dnf/yum install curl）"

# 系统级改动（安装 Docker、写 /opt）逐条显式 sudo。
# 绝不把本脚本落盘后再提权重跑：bash 从管道增量读 stdin，落盘副本会截断。
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || fail "当前非 root 且未安装 sudo，请改用 root 执行"
  SUDO="sudo"
fi

# ---- Docker 引擎与 compose 插件 ----
if ! command -v docker >/dev/null 2>&1; then
  log "未检测到 Docker，正在通过官方脚本安装（需要几分钟）…"
  curl -fsSL https://get.docker.com | ${SUDO} sh \
    || fail "Docker 自动安装失败，请手动安装：https://docs.docker.com/engine/install/"
  command -v docker >/dev/null 2>&1 || fail "Docker 已安装但当前会话找不到命令，请重开终端后重试"
fi

if ! docker compose version >/dev/null 2>&1; then
  fail "缺少 Docker Compose 插件（v2），请先安装 docker-compose-plugin 后重试"
fi

# 非 root 且不在 docker 组时，docker 命令统一走 sudo（组成员需重新登录才生效）
if docker info >/dev/null 2>&1; then
  DOCKER="docker"
else
  DOCKER="${SUDO} docker"
fi

# ---- git ----
if ! command -v git >/dev/null 2>&1; then
  log "未检测到 git，尝试用系统包管理器安装…"
  if command -v apt-get >/dev/null 2>&1; then
    ${SUDO} apt-get update -y || true
    ${SUDO} apt-get install -y git || true
  elif command -v dnf >/dev/null 2>&1; then
    ${SUDO} dnf install -y git || true
  elif command -v yum >/dev/null 2>&1; then
    ${SUDO} yum install -y git || true
  elif command -v apk >/dev/null 2>&1; then
    ${SUDO} apk add git || true
  fi
  command -v git >/dev/null 2>&1 || fail "请先安装 git 后重试"
fi

# ---- 源码：首次克隆，已有安装则原地更新 ----
# GitHub 偶发抖动，失败自动重试三次。
if [ -d "${INSTALL_DIR}" ] && [ ! -d "${INSTALL_DIR}/.git" ] \
   && [ -n "$(ls -A "${INSTALL_DIR}" 2>/dev/null)" ]; then
  fail "目录 ${INSTALL_DIR} 已存在且不是本仓库的工作副本，请更换 AUTODEPLOY_INSTALL_DIR 或手动清理"
fi

UPDATED=0
for attempt in 1 2 3; do
  if [ -d "${INSTALL_DIR}/.git" ]; then
    if ${SUDO} git -C "${INSTALL_DIR}" fetch --depth 1 origin "${BRANCH}" \
       && ${SUDO} git -C "${INSTALL_DIR}" reset --hard FETCH_HEAD \
       && ${SUDO} git -C "${INSTALL_DIR}" clean -fdx; then
      UPDATED=1
      break
    fi
  else
    if ${SUDO} git clone --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"; then
      break
    fi
    # 半途失败的克隆清掉重来，避免残留目录卡死后续重试
    ${SUDO} rm -rf "${INSTALL_DIR}"
  fi
  log "网络抖动，3 秒后重试（第 ${attempt} 次）…"
  if [ "${attempt}" -eq 3 ]; then
    fail "源码获取失败：请确认能访问 GitHub（必要时配置代理）后重试"
  fi
  sleep 3
done

cd "${INSTALL_DIR}"

# ---- 端口：写入 .env 供 compose 读取；已有 .env 时尊重现有配置 ----
if [ ! -f .env ] || [ -n "${PORT}" ]; then
  printf 'AUTODEPLOY_PORT=%s\n' "${PORT:-8770}" | ${SUDO} tee .env >/dev/null
fi
SERVICE_PORT="$(grep -E '^AUTODEPLOY_PORT=' .env 2>/dev/null | tail -n 1 | cut -d= -f2 || true)"
SERVICE_PORT="${SERVICE_PORT:-8770}"

# ---- 构建并启动 ----
log "构建并启动服务（首次构建需要几分钟）…"
${DOCKER} compose up -d --build

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
elif [ "${UPDATED}" -eq 1 ]; then
  log "已升级到最新代码，任务数据不受影响"
fi

echo
log "部署完成：http://$(hostname -I 2>/dev/null | awk '{print $1}' || true):${SERVICE_PORT}/"
log "常用命令（在 ${INSTALL_DIR} 下执行）："
echo "  查看日志:  docker compose logs -f autodeploy"
echo "  停止服务:  docker compose down        （数据保留）"
echo "  彻底删除:  docker compose down -v     （连数据卷一起删除）"
log "升级方式：重新执行本脚本，拉取最新代码并重新构建，任务数据不受影响"
