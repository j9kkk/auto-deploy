#!/usr/bin/env bash
# AutoDeploy 发布前真实升级 E2E 验收（由 .github/workflows/docker.yml 的 e2e job 调用）。
#
# 验收路径（全部在 CI 环境里真实执行，不是 mock）：
#   1. 启动本地 registry（localhost:5000 是宿主 daemon 默认信任的非 TLS 地址；
#      镜像拉取/推送全部由宿主 daemon 完成，容器内不直连 registry）；
#   2. 用 scripts/bootstrap.sh 安装上一发布版本（PREV_TAG，镜像来自本地 registry）；
#   3. 宿主驱动迁移：改安装目录 .env 的镜像 tag 到候选版本并重建
#      （验证「旧版本实例能升级到新版本」且配置数据保留）；
#   4. 面板内升级：POST /api/system/self-update 升到补丁版本（新架构核心验收），
#      断言 stage=done、confirmed_operation_id、from/to 镜像引用与数据保留；
#   5. 面板内回退：POST /api/system/self-update/rollback 回到候选版本；
#   6. 每一跳都断言 settings.update_repo 跨升级保留（数据保留探针）。
#
# 环境变量：
#   CANDIDATE_IMAGE  候选镜像引用（如 ghcr.io/j9kkk/auto-deploy:e2e-<sha>）
#   PREV_TAG         上一发布版本 tag（如 v0.3.4）
#   E2E_PORT         面板端口（默认 18770）
#
# 诊断与密码脱敏：任何失败路径都把 .env、self-update 状态/历史、docker ps、
# 容器与 compose 日志写入 .e2e-run/logs/；初始密码只出现在
# .e2e-run/bootstrap.log（故意放在 logs/ 之外，不会被上传为工件），
# 容器日志里的密码横幅落盘前一律脱敏。

set -euo pipefail

# ---------- 输入 ----------
: "${CANDIDATE_IMAGE:?缺少环境变量 CANDIDATE_IMAGE（候选镜像引用，如 ghcr.io/j9kkk/auto-deploy:e2e-<sha>）}"
: "${PREV_TAG:?缺少环境变量 PREV_TAG（上一发布版本 tag，如 v0.3.4）}"
E2E_PORT="${E2E_PORT:-18770}"
PREV_VER="${PREV_TAG#v}"
UPDATE_REPO_EXPECT="file:///e2e-mirror"
PANEL_UID=1000   # 镜像内面板进程用户（Dockerfile useradd --uid 1000）

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
E2E="$ROOT/.e2e-run"
LOGS="$E2E/logs"
COOKIES="$E2E/cookies.txt"
BASE_URL="http://127.0.0.1:${E2E_PORT}"
REGISTRY_URL="http://127.0.0.1:5000"

log()  { printf '\033[32m[e2e]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[e2e]\033[0m %s\n' "$*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi

# ---------- 诊断落盘与清理 ----------
# 密码横幅只出现在容器/compose 日志与 bootstrap.log 中；落盘日志一律脱敏，
# bootstrap.log 本身留在 .e2e-run/ 根目录、绝不复制进 logs/。
sanitize() { sed -E 's/(初始密码: ).*/\1<已脱敏>/'; }

dump_diagnostics() {
  log "收集诊断信息到 ${LOGS}/（密码横幅已脱敏；bootstrap.log 不上传）"
  if [ -f "$E2E/install/.env" ]; then
    cp "$E2E/install/.env" "$LOGS/env.txt" 2>/dev/null || true
  fi
  docker ps -a >"$LOGS/docker-ps.txt" 2>&1 || true
  curl -sS -b "$COOKIES" "${BASE_URL}/api/system/self-update/status" \
    >"$LOGS/self-update-status.json" 2>/dev/null || true
  curl -sS -b "$COOKIES" "${BASE_URL}/api/system/self-update/history" \
    >"$LOGS/self-update-history.json" 2>/dev/null || true
  local name
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    docker logs "$name" 2>&1 | sanitize >"$LOGS/container-${name}.log" || true
  done < <(docker ps -a --format '{{.Names}}' | grep -E '^auto-deploy' || true)
  if [ -f "$E2E/install/docker-compose.yml" ]; then
    (cd "$E2E/install" && docker compose logs --tail 200 2>&1 | sanitize >"$LOGS/compose.log") || true
  fi
}

cleanup() {
  local rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ]; then
    dump_diagnostics || true
  fi
  if [ -f "$E2E/install/docker-compose.yml" ]; then
    (cd "$E2E/install" && docker compose down -v --remove-orphans) >/dev/null 2>&1 || true
  fi
  docker rm -f e2e-registry >/dev/null 2>&1 || true
  if [ -n "${PATCH_DIR:-}" ] && [ -d "${PATCH_DIR}" ]; then
    rm -rf "${PATCH_DIR}"
  fi
  exit "$rc"
}

# ---------- API 帮助函数 ----------
api() { # api METHOD PATH [JSON_BODY] → 响应体；HTTP 非 2xx 直接失败
  local method="$1" path="$2" body="${3:-}"
  local args=(-fsS -b "$COOKIES" -c "$COOKIES" -X "$method")
  if [ -n "$body" ]; then
    args+=(-H 'Content-Type: application/json' -d "$body")
  fi
  curl "${args[@]}" "${BASE_URL}${path}"
}

do_login() { # 成功返回 0，失败返回 1（供轮询循环静默重试）
  rm -f "$COOKIES"
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' -c "$COOKIES" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"admin\",\"password\":\"${INIT_PW}\"}" \
    "${BASE_URL}/api/auth/login" 2>/dev/null)" || code="000"
  [ "$code" = "200" ]
}

login() {
  if do_login; then
    log "登录成功（admin）"
  else
    fail "登录失败（POST /api/auth/login 返回非 200）"
  fi
}

jget() { # jget KEY < JSON 对象 → 字段值（缺失/解析失败输出空串）
  python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
v = d.get(sys.argv[1], "")
print("" if v is None else v)' "$1"
}

health_version() {
  curl -fsS "${BASE_URL}/api/health" 2>/dev/null | jget version || true
}

current_update_repo() {
  api GET /api/settings | python3 -c 'import json,sys
d = json.load(sys.stdin)
print(d.get("settings", {}).get("update_repo", ""))'
}

expect_update_repo() { # 数据保留探针：settings.update_repo 必须跨升级保留
  local got
  got="$(current_update_repo)" || fail "读取 /api/settings 失败"
  if [ "$got" != "$UPDATE_REPO_EXPECT" ]; then
    fail "数据保留断言失败：settings.update_repo=${got:-<空>}（期望 ${UPDATE_REPO_EXPECT}）"
  fi
  log "数据保留探针通过：settings.update_repo=${got}"
}

wait_health_version() { # $1 期望版本 $2 最长秒数 $3 场景说明
  local want="$1" budget="$2" what="$3" got="" i
  log "等待面板就绪：${what}（期望 version=${want}，最长 ${budget}s）"
  for i in $(seq 1 "$budget"); do
    got="$(health_version)"
    if [ "$got" = "$want" ]; then
      log "面板版本已切换：version=${got}"
      return 0
    fi
    sleep 1
  done
  fail "${what}：${budget}s 内 version 未达到 ${want}（最后读到：${got:-无响应}）"
}

wait_self_update_done() { # $1 最长秒数 $2 说明 $3 期望 to_image $4 期望 from_image（空=不校验）
  local budget="$1" what="$2" want_to="$3" want_from="$4"
  local status="" stage="" op confirmed to_image from_image down=0
  log "轮询面板自升级状态：${what}（最长 ${budget}s，每 5s 一次）"
  local deadline=$(( SECONDS + budget ))
  while [ "$SECONDS" -lt "$deadline" ]; do
    status="$(curl -fsS -b "$COOKIES" "${BASE_URL}/api/system/self-update/status" 2>/dev/null)" || status=""
    if [ -n "$status" ]; then
      down=0
      stage="$(printf '%s' "$status" | jget stage)"
      if [ "$stage" = "done" ]; then
        op="$(printf '%s' "$status" | jget operation_id)"
        confirmed="$(printf '%s' "$status" | jget confirmed_operation_id)"
        to_image="$(printf '%s' "$status" | jget to_image_ref)"
        [ -n "$to_image" ] || to_image="$(printf '%s' "$status" | jget to_image)"
        from_image="$(printf '%s' "$status" | jget from_image_ref)"
        [ -n "$from_image" ] || from_image="$(printf '%s' "$status" | jget from_image)"
        [ -n "$confirmed" ] || fail "${what}：stage=done 但 confirmed_operation_id 为空"
        [ "$confirmed" = "$op" ] || fail "${what}：confirmed_operation_id=${confirmed} 与 operation_id=${op} 不一致"
        [ "$to_image" = "$want_to" ] || fail "${what}：to_image=${to_image}（期望 ${want_to}）"
        if [ -n "$want_from" ] && [ "$from_image" != "$want_from" ]; then
          fail "${what}：from_image=${from_image}（期望 ${want_from}）"
        fi
        log "${what} 完成：from_image=${from_image} → to_image=${to_image}"
        return 0
      fi
      case "$stage" in
        failed|rolled_back|attention|recovery_required|unverified)
          printf '%s' "$status" >"$LOGS/self-update-final-state.json"
          fail "${what}：自升级落到失败终态 stage=${stage}（state 全文见 ${LOGS}/self-update-final-state.json）"
          ;;
      esac
    else
      # 容器重建期间接口短暂不可用；连续多次失败时静默重登一次兜底
      # （会话令牌哈希随数据卷持久化，正常情况 Cookie 应保持有效）。
      down=$(( down + 1 ))
      if [ "$down" -ge 3 ]; then
        down=0
        do_login >/dev/null 2>&1 || true
      fi
    fi
    sleep 5
  done
  printf '%s' "$status" >"$LOGS/self-update-final-state.json" 2>/dev/null || true
  fail "${what}：${budget}s 内未到达终态 done（最后 stage=${stage:-未知}）"
}

assert_history_latest() { # $1 期望 to_image
  local raw stage to_image
  raw="$(api GET /api/system/self-update/history)" || fail "读取 /api/system/self-update/history 失败"
  stage="$(printf '%s' "$raw" | python3 -c 'import json,sys
entries = json.load(sys.stdin).get("entries", [])
print(entries[0].get("stage", "") if entries else "")')"
  to_image="$(printf '%s' "$raw" | python3 -c 'import json,sys
entries = json.load(sys.stdin).get("entries", [])
print(entries[0].get("to_image", "") if entries else "")')"
  [ "$stage" = "done" ] || fail "升级历史最新条目 stage=${stage:-<空>}（期望 done）"
  [ "$to_image" = "$1" ] || fail "升级历史最新条目 to_image=${to_image:-<空>}（期望 $1）"
  log "升级历史校验通过：最新条目 stage=done to_image=${to_image}"
}

# ---------- 主流程 ----------
rm -rf "$E2E"
mkdir -p "$LOGS"
docker rm -f e2e-registry >/dev/null 2>&1 || true
trap cleanup EXIT

# 1. 本地 registry：镜像经宿主 daemon 推拉，容器内不直连。
log "启动本地 registry（localhost:5000，宿主 daemon 默认信任的非 TLS 地址）"
if ! docker run -d --name e2e-registry -p 5000:5000 registry:2 >/dev/null; then
  fail "本地 registry 启动失败（docker run registry:2）"
fi
REGISTRY_OK=0
for _ in $(seq 1 30); do
  if curl -fsS "${REGISTRY_URL}/v2/" >/dev/null 2>&1; then
    REGISTRY_OK=1
    break
  fi
  sleep 1
done
[ "$REGISTRY_OK" -eq 1 ] || fail "本地 registry 30 秒内未就绪（${REGISTRY_URL}/v2/ 不通），可查看 docker logs e2e-registry"
log "本地 registry 就绪"

# 2. 准备三个版本：上一发布版本、候选版本、候选补丁版本。
log "拉取上一版本镜像：ghcr.io/j9kkk/auto-deploy:${PREV_VER}"
if ! docker pull "ghcr.io/j9kkk/auto-deploy:${PREV_VER}"; then
  fail "拉取上一版本镜像失败：ghcr.io/j9kkk/auto-deploy:${PREV_VER}（该版本是否已发布？）"
fi
docker tag "ghcr.io/j9kkk/auto-deploy:${PREV_VER}" "localhost:5000/auto-deploy:${PREV_VER}"
if ! docker push "localhost:5000/auto-deploy:${PREV_VER}" >/dev/null; then
  fail "推送上一版本镜像到本地 registry 失败（tag ${PREV_VER}）"
fi

log "拉取候选镜像：${CANDIDATE_IMAGE}"
if ! docker pull "$CANDIDATE_IMAGE"; then
  fail "拉取候选镜像失败：${CANDIDATE_IMAGE}"
fi
log "读取候选镜像版本号（from app import __version__）"
B_VER="$(docker run --rm --entrypoint python "$CANDIDATE_IMAGE" \
  -c "from app import __version__; print(__version__)" | tr -d '[:space:]')"
if [ -z "$B_VER" ]; then
  fail "候选镜像版本号为空（docker run --entrypoint python 读取 app.__version__ 失败）"
fi
if ! printf '%s' "$B_VER" | grep -Eq '^[0-9]+(\.[0-9]+)*$'; then
  fail "候选镜像版本号异常：${B_VER}"
fi
docker tag "$CANDIDATE_IMAGE" "localhost:5000/auto-deploy:${B_VER}"
if ! docker push "localhost:5000/auto-deploy:${B_VER}" >/dev/null; then
  fail "推送候选镜像到本地 registry 失败（tag ${B_VER}）"
fi
log "候选镜像已就绪：localhost:5000/auto-deploy:${B_VER}"

B_PATCH="$(awk -F. '{printf "%d.%d.%d", $1,$2,$3+1}' <<<"$B_VER")"
log "构造补丁版本 ${B_PATCH}（复制候选镜像内容并改写 __version__，避免全量源码构建）"
PATCH_DIR="$(mktemp -d)"
CID="$(docker create "$CANDIDATE_IMAGE")"
if ! docker cp "${CID}:/app/app" "$PATCH_DIR/app" \
   || ! docker cp "${CID}:/app/web" "$PATCH_DIR/web" \
   || ! docker cp "${CID}:/app/requirements.txt" "$PATCH_DIR/requirements.txt"; then
  docker rm -f "$CID" >/dev/null 2>&1 || true
  fail "从候选镜像拷贝 /app 内容失败"
fi
docker rm "$CID" >/dev/null
sed -i.bak -E "s/^__version__ = .*/__version__ = \"${B_PATCH}\"/" "$PATCH_DIR/app/__init__.py"
rm -f "$PATCH_DIR/app/__init__.py.bak"
if ! grep -Fqx "__version__ = \"${B_PATCH}\"" "$PATCH_DIR/app/__init__.py"; then
  fail "补丁版本号改写失败（app/__init__.py 未变为 ${B_PATCH}）"
fi
cat > "$PATCH_DIR/Dockerfile" <<EOF
FROM ${CANDIDATE_IMAGE}
COPY app /app/app
COPY web /app/web
EOF
if ! docker build -t "localhost:5000/auto-deploy:${B_PATCH}" "$PATCH_DIR"; then
  fail "补丁版本镜像构建失败（${B_PATCH}）"
fi
if ! docker push "localhost:5000/auto-deploy:${B_PATCH}" >/dev/null; then
  fail "推送补丁镜像到本地 registry 失败（tag ${B_PATCH}）"
fi
log "补丁镜像已就绪：localhost:5000/auto-deploy:${B_PATCH}"

# 3. git 发现源：本地裸仓库（挂进容器只读），打上 v$B_VER 与 v$B_PATCH。
#    面板的 check() 对 file:// 源走 git ls-remote，只认 v<纯数字版本> 形式。
log "构造 git 发现源：${E2E}/mirror.git"
if ! git clone --bare "$ROOT" "$E2E/mirror.git" >/dev/null; then
  fail "git clone --bare ${ROOT} 失败"
fi
git -C "$E2E/mirror.git" tag -f "v${B_VER}" HEAD >/dev/null || fail "打 tag v${B_VER} 失败"
git -C "$E2E/mirror.git" tag -f "v${B_PATCH}" HEAD >/dev/null || fail "打 tag v${B_PATCH} 失败"
# 面板以容器内 uid 1000 执行 git ls-remote：属主不同会触发 git 的
# dubious ownership 拒绝，统一交给面板用户（容器内只读挂载，不改内容）。
$SUDO chown -R "${PANEL_UID}:${PANEL_UID}" "$E2E/mirror.git"

# 4. 自定义 override：bootstrap 看到 override 不含「AutoDeploy 自动生成」标记
#    就会原样保留，从而把 socket、安装目录（同路径）与发现源挂进容器。
log "写入自定义 docker-compose.override.yml（socket + 安装目录 + 只读发现源）"
mkdir -p "$E2E/install"
SOCKET_GID="$(stat -c %g /var/run/docker.sock 2>/dev/null || true)"
if [ -z "$SOCKET_GID" ]; then
  SOCKET_GID="$(getent group docker 2>/dev/null | cut -d: -f3 || true)"
fi
[ -n "$SOCKET_GID" ] || fail "无法确定 /var/run/docker.sock 的组 GID"
cat > "$E2E/install/docker-compose.override.yml" <<EOF
# scripts/ci/e2e-upgrade.sh 写入的 E2E 专用 override（非一键脚本代管）
services:
  autodeploy:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ${E2E}/install:${E2E}/install
      - ${E2E}/mirror.git:/e2e-mirror:ro
    group_add:
      - "${SOCKET_GID}"
EOF

# 5. 安装上一发布版本。
log "安装上一版本 ${PREV_TAG}（bootstrap.sh → 本地 registry）"
if ! AUTODEPLOY_VERSION="$PREV_TAG" \
     AUTODEPLOY_IMAGE="localhost:5000/auto-deploy" \
     AUTODEPLOY_PORT="$E2E_PORT" \
     AUTODEPLOY_INSTALL_DIR="$E2E/install" \
     bash "$ROOT/scripts/bootstrap.sh" 2>&1 | tee "$E2E/bootstrap.log"; then
  fail "bootstrap 安装 ${PREV_TAG} 失败（完整日志：${E2E}/bootstrap.log）"
fi
# 安装目录属主交给容器面板用户（面板自升级要改写其中 .env），
# 同时放开读写让宿主侧 sed/读取也能进行（双保险，bootstrap 也会处理）。
$SUDO chown -R "${PANEL_UID}:${PANEL_UID}" "$E2E/install"
$SUDO chmod -R a+rwX "$E2E/install"

# 6. 初始密码：只在 bootstrap.log 与本变量中出现，绝不回显、绝不进 logs/。
INIT_PW="$(grep -A3 '首次启动' "$E2E/bootstrap.log" | sed -n 's/.*初始密码: *//p' | tail -n 1 | tr -d '\r')"
if [ -z "$INIT_PW" ]; then
  fail "未能从 bootstrap 日志解析初始密码（grep '首次启动' 后未找到 '初始密码:' 行）"
fi
log "已解析初始密码（不回显）"

# 7. 登录并写入跨升级数据保留探针。
login
api PUT /api/settings "{\"update_repo\":\"${UPDATE_REPO_EXPECT}\"}" >/dev/null \
  || fail "写入 settings.update_repo 失败（PUT /api/settings）"
expect_update_repo

# 8. 第一跳：宿主驱动迁移 PREV → 候选版本（验证旧版本实例能升级到新版本）。
log "第一跳：宿主驱动迁移 ${PREV_VER} → ${B_VER}"
(
  cd "$E2E/install"
  if grep -q '^AUTODEPLOY_IMAGE_TAG=' .env; then
    sed -i.bak "s/^AUTODEPLOY_IMAGE_TAG=.*/AUTODEPLOY_IMAGE_TAG=${B_VER}/" .env && rm -f .env.bak
  else
    printf 'AUTODEPLOY_IMAGE_TAG=%s\n' "$B_VER" >> .env
  fi
  grep -Fxq "AUTODEPLOY_IMAGE_TAG=${B_VER}" .env || { echo "[e2e] .env 未切换到 ${B_VER}" >&2; exit 1; }
  echo "[e2e] docker compose pull && up -d（localhost:5000/auto-deploy:${B_VER}）"
  docker compose pull
  docker compose up -d --no-build
) || fail "第一跳迁移失败（compose 重建 ${B_VER}）"
wait_health_version "$B_VER" 240 "第一跳迁移后"
login
expect_update_repo

# 9. 第二跳：面板内升级 B_VER → B_PATCH（新架构核心验收）。
log "第二跳：面板内升级 ${B_VER} → ${B_PATCH}"
api POST /api/system/self-update "{\"target_version\":\"v${B_PATCH}\"}" >/dev/null \
  || fail "发起面板自升级失败（POST /api/system/self-update target=v${B_PATCH}）"
wait_self_update_done 1200 "面板升级到 ${B_PATCH}" \
  "localhost:5000/auto-deploy:${B_PATCH}" "localhost:5000/auto-deploy:${B_VER}"
wait_health_version "$B_PATCH" 180 "面板升级后"
login
expect_update_repo
assert_history_latest "localhost:5000/auto-deploy:${B_PATCH}"

# 10. 第三跳：面板内回退到 B_VER。
log "第三跳：面板内回退 → ${B_VER}"
api POST /api/system/self-update/rollback "{\"target_image\":\"localhost:5000/auto-deploy:${B_VER}\"}" >/dev/null \
  || fail "发起面板回退失败（POST /api/system/self-update/rollback target_image=localhost:5000/auto-deploy:${B_VER}）"
wait_self_update_done 900 "面板回退到 ${B_VER}" \
  "localhost:5000/auto-deploy:${B_VER}" ""
wait_health_version "$B_VER" 180 "面板回退后"
login
expect_update_repo

log "E2E 通过：安装 ${PREV_TAG} → 迁移 ${B_VER} → 面板升级 ${B_PATCH} → 面板回退 ${B_VER}"
