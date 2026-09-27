#!/usr/bin/env bash
# AutoDeploy launcher.
#
# Creates the virtualenv on first run, then starts the service.  Arguments are
# passed straight through to the Python entry point, so
#   ./run.sh --port 9000 --no-browser
# works as expected.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${AUTODEPLOY_PYTHON:-python3}"
VENV_DIR="$ROOT_DIR/.venv"
REQUIREMENTS="$ROOT_DIR/requirements.txt"

log()  { printf '\033[36m[autodeploy]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[autodeploy]\033[0m %s\n' "$*" >&2; exit 1; }

command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "找不到 $PYTHON_BIN，请先安装 Python 3.10+"

# Python 3.10+ is required for the type syntax used throughout the codebase.
"$PYTHON_BIN" - <<'PY' || fail "需要 Python 3.10 或更高版本"
import sys
if sys.version_info < (3, 10):
    sys.exit(1)
PY

if [ ! -x "$VENV_DIR/bin/python" ]; then
  log "首次运行：正在创建虚拟环境 $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR" || fail "创建虚拟环境失败"
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
fi

# Reinstall only when the requirements file is newer than the marker, so a
# normal start does not pay for a dependency check.
MARKER="$VENV_DIR/.requirements-installed"
if [ ! -f "$MARKER" ] || [ "$REQUIREMENTS" -nt "$MARKER" ]; then
  log "正在安装依赖…"
  "$VENV_DIR/bin/pip" install --quiet --disable-pip-version-check -r "$REQUIREMENTS" \
    || fail "依赖安装失败，请检查网络或 pip 源"
  touch "$MARKER"
fi

export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec "$VENV_DIR/bin/python" -m app.main "$@"
