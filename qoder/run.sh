#!/usr/bin/env bash
# ReSearch 科研资料工作台 — 一键启动脚本。
# 前置：seekdb 已在本机运行（默认 127.0.0.1:2881，root/空密码，见 README）。
# 用法：bash qoder/run.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE_DIR="$ROOT_DIR/qoder"
STATE_DIR="$MODULE_DIR/app/state"
ENV_FILE="$STATE_DIR/powercontext.env"
PC_PORT="${POWERCONTEXT_PORT:-8600}"
APP_PORT="${APP_PORT:-8080}"
SERVER_PID=""
APP_PID=""

command -v uv >/dev/null 2>&1 || { echo "需要 uv (https://docs.astral.sh/uv/); 请先安装" >&2; exit 1; }

log() { printf '\n==== %s ====\n' "$*"; }

cleanup() {
  for pid in "$APP_PID" "$SERVER_PID"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT

start_powercontext() {
  mkdir -p "$STATE_DIR"
  cat > "$ENV_FILE" <<EOF
POWERCONTEXT_SERVER_HTTP_HOST=127.0.0.1
POWERCONTEXT_SERVER_HTTP_PORT=$PC_PORT
POWERCONTEXT_SERVER_ACCESS_MODE=disabled
POWERCONTEXT_SERVER_DASHBOARD_ENABLED=false
POWERCONTEXT_SERVER_DATABASE_KIND=sqlite
POWERCONTEXT_SERVER_DATABASE_URL=sqlite+aiosqlite:///$STATE_DIR/powercontext.db
POWERCONTEXT_SERVER_RUNTIME_ARTIFACT_PROCESSING_ROLE=all
POWERCONTEXT_SERVER_RUNTIME_DREAM_ENABLED=false
POWERCONTEXT_SERVER_LOGGING_LEVEL=WARNING
POWERCONTEXT_CLIENT_SERVER_URL=http://127.0.0.1:$PC_PORT
EOF
  log "启动 PowerContext 服务 (端口 $PC_PORT)"
  (cd "$ROOT_DIR" && uv run --extra seekdb powercontext server run --env-file "$ENV_FILE" > "$STATE_DIR/server.log" 2>&1) &
  SERVER_PID=$!
  for _ in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:$PC_PORT/health/live" >/dev/null 2>&1; then
      log "PowerContext 服务就绪"
      return 0
    fi
    sleep 1
  done
  echo "PowerContext 服务启动超时，日志见 $STATE_DIR/server.log" >&2
  exit 1
}

start_powercontext

log "启动 ReSearch 工作台 (端口 $APP_PORT)"
(cd "$MODULE_DIR/app" && uv run --with pymysql python app.py > "$STATE_DIR/app.log" 2>&1) &
APP_PID=$!
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:$APP_PORT/api/health" >/dev/null 2>&1; then
    log "工作台就绪: http://127.0.0.1:$APP_PORT"
    echo "按 Ctrl+C 退出（会自动关闭两个服务）"
    wait "$APP_PID"
    exit 0
  fi
  sleep 1
done
echo "工作台启动超时，日志见 $STATE_DIR/app.log" >&2
exit 1
