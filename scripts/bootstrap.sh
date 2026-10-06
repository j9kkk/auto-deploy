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
# 规范化：去掉尾部多余的 /（根目录 / 保持原样，属边界情况）。
while [ "${INSTALL_DIR}" != "/" ] && [ "${INSTALL_DIR%/}" != "${INSTALL_DIR}" ]; do
  INSTALL_DIR="${INSTALL_DIR%/}"
done
# compose 挂载写作「宿主路径:容器路径」，面板升级也依赖同一绝对路径映射：
# 相对路径会导致两处路径不一致，冒号 / 换行会破坏挂载语法，均直接拒绝。
case "${INSTALL_DIR}" in
  /*) ;;
  *) fail "安装目录必须使用绝对路径（compose 挂载与面板升级依赖同路径映射）" ;;
esac
case "${INSTALL_DIR}" in
  *:*)     fail "安装目录不能包含冒号 :（会破坏 compose 挂载语法），请更换目录" ;;
  *$'\n'*) fail "安装目录不能包含换行符（会破坏 compose 挂载语法），请更换目录" ;;
esac
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

# ---- upsert_env：.env 增量更新（只增删改指定键，其余行、注释与顺序原样保留）----
# 之前在指定端口 / 镜像 / 版本时整份重写 .env，会把用户已有的镜像源、版本锁等
# 配置一并冲掉；改为增量更新后，重复执行脚本不再丢失既有配置。
# 临时文件必须建在安装目录内（与 .env 同一文件系统），install 的替换才能在该
# 文件系统上原子完成；落在 /tmp 等别处会跨设备，无法原子替换。
upsert_env() {
  # 用法：upsert_env KEY VALUE 设置键值；upsert_env --remove KEY 删除该键的所有行
  local ACTION="set" KEY VALUE MODE TMP RC
  if [ "${1:-}" = "--remove" ]; then
    ACTION="remove"
    shift
  fi
  KEY="${1:?upsert_env 缺少键名}"
  VALUE=""
  if [ "${ACTION}" = "set" ]; then
    VALUE="${2:?upsert_env 缺少键值}"
  fi
  # 无提权且现有 .env 不可读时直接中止，避免把旧配置清空（root 创建后普通用户重跑的场景）
  if [ -z "${SUDO}" ] && [ -f .env ] && [ ! -r .env ]; then
    fail "无法读取现有 .env（权限不足），已中止写入以保护原有配置；请用 sudo 重新执行本脚本"
  fi
  if [ -f .env ]; then
    MODE="$(stat -c %a .env 2>/dev/null || true)"
    [ -n "${MODE}" ] || MODE="600"   # 取不到权限位时按 600 处理
  else
    MODE="600"   # 新建 .env 固定 600，不对本机其他用户开放
  fi
  TMP="$(mktemp "${INSTALL_DIR}/.env.tmp.XXXXXX")"
  RC=0
  if [ -f .env ]; then
    # grep -v 无匹配行返回 1（属正常，需兜底避免 set -e 误退），返回 2 及以上才是读错误
    ${SUDO} grep -v "^${KEY}=" .env >"${TMP}" || RC=$?
    if [ "${RC}" -gt 1 ]; then
      rm -f "${TMP}"
      fail "读取现有 .env 失败，已中止写入以保护原有配置；请检查文件权限后重试"
    fi
  else
    : >"${TMP}"
  fi
  if [ "${ACTION}" = "set" ]; then
    printf '%s=%s\n' "${KEY}" "${VALUE}" >>"${TMP}"
  fi
  if ! ${SUDO} install -m "${MODE}" "${TMP}" ".env"; then
    rm -f "${TMP}"
    fail "写入 .env 失败，请检查安装目录权限后重试"
  fi
  rm -f "${TMP}"
}

# ---- 端口 / 镜像：按意图增量写入 .env；已有 .env 时只改动本次显式指定的键 ----
# 未显式给任何变量且 .env 已存在时什么都不改（保留现有行为：默认重跑不动已锁定的 tag）。
if [ ! -f .env ]; then
  upsert_env AUTODEPLOY_PORT "${PORT:-8770}"
elif [ -n "${PORT}" ]; then
  upsert_env AUTODEPLOY_PORT "$PORT"
fi
if [ -n "${AUTODEPLOY_VERSION+x}" ]; then
  if [ "${VERSION}" = "main" ]; then
    # 显式指定 main（最新渠道）时解除版本锁：删除 tag 锁定行，恢复跟随 latest 镜像
    upsert_env --remove AUTODEPLOY_IMAGE_TAG
  else
    # 锁定版本时同步锁定镜像 tag（GHCR 的语义化标签不含 v 前缀）
    upsert_env AUTODEPLOY_IMAGE_TAG "${VERSION#v}"
  fi
fi
if [ -n "${AUTODEPLOY_IMAGE+x}" ]; then
  upsert_env AUTODEPLOY_IMAGE "$AUTODEPLOY_IMAGE"
fi
SERVICE_PORT="$(grep -E '^AUTODEPLOY_PORT=' .env 2>/dev/null | tail -n 1 | cut -d= -f2 || true)"
SERVICE_PORT="${SERVICE_PORT:-8770}"

# ---- Docker socket 挂载：任务使用「Docker / Docker Compose」部署方式所需 ----
# 自动写入本目录的 docker-compose.override.yml（compose 自动合并，升级重写主文件不影响）。
# 默认开启；AUTODEPLOY_MOUNT_DOCKER_SOCKET=0 可关闭。docker 组权限等价 root，勿暴露公网。
MOUNT_SOCKET=1
case "${AUTODEPLOY_MOUNT_DOCKER_SOCKET:-1}" in
  0|false|FALSE|no|NO|off|OFF) MOUNT_SOCKET=0 ;;
esac

OVERRIDE_FILE="docker-compose.override.yml"
HAS_OVERRIDE=0
OURS_OVERRIDE=0
if [ -f "${OVERRIDE_FILE}" ]; then
  HAS_OVERRIDE=1
  if grep -q "AutoDeploy 自动生成" "${OVERRIDE_FILE}"; then
    OURS_OVERRIDE=1
  fi
fi

SOCKET_GID=""
if [ "${MOUNT_SOCKET}" -eq 1 ]; then
  if [ -S /var/run/docker.sock ]; then
    SOCKET_GID="$(stat -c %g /var/run/docker.sock 2>/dev/null || true)"
  fi
  if [ -z "${SOCKET_GID}" ]; then
    SOCKET_GID="$(getent group docker 2>/dev/null | cut -d: -f3 || true)"
  fi
fi

if [ "${MOUNT_SOCKET}" -eq 1 ] && [ -n "${SOCKET_GID}" ]; then
  if [ "${HAS_OVERRIDE}" -eq 1 ] && [ "${OURS_OVERRIDE}" -eq 0 ]; then
    log "检测到自定义的 docker-compose.override.yml，未改动；如需脚本代管挂载请移除该文件后重跑"
  else
    {
      echo "# AutoDeploy 自动生成（scripts/bootstrap.sh 管理，重新执行脚本会更新本文件）"
      echo "services:"
      echo "  autodeploy:"
      echo "    volumes:"
      echo "      - /var/run/docker.sock:/var/run/docker.sock"
      # 安装目录同路径挂载：面板内一键升级要在容器里改写宿主机 .env 并以
      # 该目录为工作目录执行 compose up，路径必须与 compose 标签一致。
      echo "      - ${INSTALL_DIR}:${INSTALL_DIR}"
      echo "    group_add:"
      echo "      - \"${SOCKET_GID}\""
    } | ${SUDO} tee "${OVERRIDE_FILE}" >/dev/null
    log "已挂载宿主机 Docker socket 与安装目录 ${INSTALL_DIR}（docker 组 GID ${SOCKET_GID}），任务的 Docker 部署方式与面板内一键升级都依赖这两个挂载"
  fi
elif [ "${MOUNT_SOCKET}" -eq 1 ]; then
  log "警告：未检测到 /var/run/docker.sock 的 docker 组 GID，本次不挂载 socket；Docker / Docker Compose 部署方式将不可用（其余部署方式不受影响）"
else
  if [ "${HAS_OVERRIDE}" -eq 1 ] && [ "${OURS_OVERRIDE}" -eq 1 ]; then
    ${SUDO} rm -f "${OVERRIDE_FILE}"
  fi
  log "已按 AUTODEPLOY_MOUNT_DOCKER_SOCKET=0 跳过 socket 挂载"
fi

# ---- 属主迁移：把安装目录交给面板容器运行用户（UID 1000）----
# 面板容器以 UID 1000 运行，旧版本面板（0.3.3/0.3.4）的面板内升级要在容器内以
# UID 1000 改写安装目录的 .env，属主不迁移会导致 Permission denied（0.3.4 的
# 真实事故）；新版执行器以 root 运行不依赖此项，但迁移路径需要。
# root 直跑时 SUDO 为空但文件同样归 root，故两种情况都执行；已属 1000 时
# chown 是无害的 no-op。自定义 override 非脚本代管，不动其属主。
if [ "$(id -u)" -eq 0 ] || [ -n "${SUDO}" ]; then
  ${SUDO} chown 1000:1000 "${INSTALL_DIR}" .env docker-compose.yml
  if [ "${OURS_OVERRIDE}" -eq 1 ]; then
    ${SUDO} chown 1000:1000 "${OVERRIDE_FILE}"
  fi
fi

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

# ---- 版本核对：报告运行中的服务版本；锁定版本时不一致即失败 ----
HEALTH_JSON="$(curl -fsS "http://127.0.0.1:${SERVICE_PORT}/api/health" 2>/dev/null || true)"
RUNNING_VERSION="$(printf '%s' "${HEALTH_JSON}" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
if [ -n "${RUNNING_VERSION}" ]; then
  log "服务版本: ${RUNNING_VERSION}"
else
  log "服务版本: 未知（未能从 /api/health 读取）"
fi
if [ -n "${AUTODEPLOY_VERSION+x}" ] && [ "${VERSION}" != "main" ]; then
  # 接口返回形如 0.3.5，锁定值可能带 v 前缀，比较前先统一去掉
  LOCKED_VERSION="${VERSION#v}"
  if [ "${RUNNING_VERSION#v}" != "${LOCKED_VERSION}" ]; then
    fail "服务版本 ${RUNNING_VERSION:-未知} 与锁定的 ${LOCKED_VERSION} 不一致，请查看 compose 日志排查"
  fi
fi

# ---- 初始密码（仅首次启动打印一次）----
# 健康检查通过时横幅可能尚未写入 docker 日志，短暂重试避免误报「未拿到密码」。
BANNER_FOUND=0
for _ in $(seq 1 10); do
  if ${DOCKER} compose logs autodeploy 2>&1 | grep -q "首次启动"; then
    BANNER_FOUND=1
    break
  fi
  sleep 1
done
if [ "${BANNER_FOUND}" -eq 1 ]; then
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
