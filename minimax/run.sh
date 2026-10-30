#!/usr/bin/env bash
# MiniMax SDD 上下文模块 — 端到端验证样例驱动脚本。
# 从干净环境执行: bash minimax/run.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE_DIR="$ROOT_DIR/minimax"
STATE_DIR="$MODULE_DIR/state"
ENV_FILE="$MODULE_DIR/.env"
PORT="${POWERCONTEXT_PORT:-8600}"
SERVER_PID=""

command -v uv >/dev/null 2>&1 || { echo "需要 uv (https://docs.astral.sh/uv/); 请先安装" >&2; exit 1; }

log() { printf '\n==== %s ====\n' "$*"; }

cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

start_server() {
  mkdir -p "$STATE_DIR"
  cat > "$ENV_FILE" <<EOF
POWERCONTEXT_SERVER_HTTP_HOST=127.0.0.1
POWERCONTEXT_SERVER_HTTP_PORT=$PORT
POWERCONTEXT_SERVER_ACCESS_MODE=disabled
POWERCONTEXT_SERVER_DASHBOARD_ENABLED=false
POWERCONTEXT_SERVER_DATABASE_KIND=sqlite
POWERCONTEXT_SERVER_DATABASE_URL=sqlite+aiosqlite:///$STATE_DIR/powercontext.db
POWERCONTEXT_SERVER_RUNTIME_ARTIFACT_PROCESSING_ROLE=all
POWERCONTEXT_SERVER_RUNTIME_DREAM_ENABLED=false
POWERCONTEXT_SERVER_LOGGING_LEVEL=WARNING
POWERCONTEXT_CLIENT_SERVER_URL=http://127.0.0.1:$PORT
EOF
  log "启动 PowerContext 服务 (端口 $PORT)"
  (cd "$ROOT_DIR" && uv run --extra seekdb powercontext server run --env-file "$ENV_FILE" > "$STATE_DIR/server.log" 2>&1) &
  SERVER_PID=$!
  for _ in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:$PORT/health/live" >/dev/null 2>&1; then
      log "PowerContext 服务就绪"
      return 0
    fi
    sleep 1
  done
  echo "PowerContext 服务启动超时，日志见 $STATE_DIR/server.log" >&2
  exit 1
}

stop_server() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    SERVER_PID=""
  fi
}

cli() { (cd "$MODULE_DIR" && uv run --extra seekdb python -m sdd_context.cli "$@"); }

start_server

log "步骤 1/9 初始化 (seekdb 资料库 + PowerContext 作用域)"
cli init

log "步骤 2/9 导入 v1 规格并查看上下文 (基线)"
cp "$MODULE_DIR/demo_project/specs/catalog-spec.v1.md" "$MODULE_DIR/demo_project/specs/catalog-spec.md"
cli ingest
cli context

log "步骤 3/9 更新规格为 v2 并重新导入 (上下文必须使用更新内容)"
cp "$MODULE_DIR/demo_project/specs/catalog-spec.v2.md" "$MODULE_DIR/demo_project/specs/catalog-spec.md"
cli ingest
cli context

log "步骤 4/9 修改前验证 (目标测试因功能未实现而失败，回归测试通过)"
cli verify --phase before

log "步骤 5/9 MiniMax 生成代码修改并应用"
cli fix

log "步骤 6/9 修改后验证 (功能测试与回归测试全部通过)"
cli verify --phase after

log "步骤 7/9 保存任务状态到 PowerContext"
cli save

log "步骤 8/9 重启上下文模块 (重启 PowerContext 服务)"
stop_server
start_server

log "步骤 9/9 重启后读取并复用任务状态"
cli resume

log "验证样例全部完成"
echo "产物:"
echo "  - 代码差异:         minimax/demo_project/snapshots/catalog.diff"
echo "  - 修改前/后快照:    minimax/demo_project/snapshots/catalog.before.py / catalog.after.py"
echo "  - MiniMax 开发记录: minimax/devlog/minimax_dev_record.md"
echo "  - 测试日志:         minimax/devlog/pytest_before.log / pytest_after.log"
