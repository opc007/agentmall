#!/usr/bin/env bash
# AgentMall 一键起全栈（路演用）。
#
#   ./scripts/start_demo.sh          起 3 个服务，前台等待 Ctrl-C 退出
#   ./scripts/start_demo.sh --check  起完自动跑七套测试 + 路演彩排再退出
#
# 三个服务：
#   :8000  用户面 Web   —— 注册/登录/个人中心/订单列表/模拟收银台
#   :8001  MCP Server   —— streamable-http，AI 客户端按「URL + Bearer key」接入
#   :8002  管理后台     —— 独立进程，与用户面物理隔离（三权分立）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WEB_PORT="${AGENTMALL_WEB_PORT:-8000}"
MCP_PORT="${AGENTMALL_MCP_PORT:-8001}"
ADMIN_PORT="${AGENTMALL_ADMIN_PORT:-8002}"
PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

PIDS=()
cleanup() {
  echo ""
  echo "🛑 关闭服务…"
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [ ! -d .venv ]; then
  echo "▶ 创建虚拟环境并装依赖…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi

export AGENTMALL_MCP_PORT="$MCP_PORT"
export AGENTMALL_ADMIN_PORT="$ADMIN_PORT"
export AGENTMALL_PUBLIC_URL="${AGENTMALL_PUBLIC_URL:-http://127.0.0.1:$WEB_PORT}"

wait_up() {  # url, 名字
  for _ in $(seq 1 60); do
    if curl -sf -m 1 "$1" >/dev/null 2>&1; then return 0; fi
    sleep 0.5
  done
  echo "❌ $2 启动超时"; return 1
}

echo "▶ 启动用户面 Web (:$WEB_PORT)…"
"$PY" -m uvicorn agentmall.web.app:app --port "$WEB_PORT" >/tmp/agentmall_web.log 2>&1 &
PIDS+=($!)

echo "▶ 启动 MCP Server (:$MCP_PORT/mcp)…"
"$PY" -m agentmall.http_server >/tmp/agentmall_mcp.log 2>&1 &
PIDS+=($!)

echo "▶ 启动管理后台 (:$ADMIN_PORT)…"
"$PY" -m uvicorn agentmall.web.admin_app:app --port "$ADMIN_PORT" >/tmp/agentmall_admin.log 2>&1 &
PIDS+=($!)

wait_up "http://127.0.0.1:$WEB_PORT/healthz"  "用户面"
wait_up "http://127.0.0.1:$MCP_PORT/healthz"  "MCP Server"
wait_up "http://127.0.0.1:$ADMIN_PORT/healthz" "管理后台"

cat <<BANNER

════════════════════════════════════════════════════════════
  ✅ AgentMall 已就绪（演示环境 · 不发生任何真实交易）
════════════════════════════════════════════════════════════
  用户面      http://127.0.0.1:$WEB_PORT
  管理后台    http://127.0.0.1:$ADMIN_PORT   （管理员 key: ak_demo_admin_secret）
  MCP 接入点  http://127.0.0.1:$MCP_PORT/mcp

  路演：注册 → 个人中心复制接入点 → 配给 AI 客户端
        → 说「找最便宜的抽纸，30 元以内」→ 网页订单点「去支付」→ 扫码确认
        → 切管理后台看订单数 +1、GMV 变化

  日志：/tmp/agentmall_{web,mcp,admin}.log
════════════════════════════════════════════════════════════

BANNER

if [ "${1:-}" = "--check" ]; then
  echo "▶ 跑七套测试…"
  FAILED=0
  for t in test_readonly demo_chain test_roles test_http_auth test_roadshow test_admin_console test_sourcing; do
    printf "  %-20s " "$t"
    if out="$("$PY" "tests/$t.py" 2>&1)"; then
      echo "$out" | grep -oE "PASSED.*|链路 [0-9]+/[0-9]+ 项通过|鉴权 [0-9]+/[0-9]+ 项通过|管理后台 [0-9]+/[0-9]+ 项通过|适配层 [0-9]+/[0-9]+ 项通过|[0-9]+/[0-9]+ 项通过" | tail -1
    else
      FAILED=1
      echo "❌ 失败"
      echo "$out" | tail -20
    fi
  done

  # 最后一道闸门：拿刚起的这套服务真跑一遍路演 9 步。
  # 单测全绿不等于台上能演——2026-10-07 就是彩排时才发现看板 GMV 恒为 0。
  echo ""
  echo "▶ 路演彩排（对着刚起的这套服务跑 9 步）…"
  if "$PY" scripts/rehearse.py >/tmp/agentmall_rehearse.log 2>&1; then
    grep -E "^  ✅ 第 9 步|9 步全通" /tmp/agentmall_rehearse.log | sed 's/^/  /'
  else
    FAILED=1
    echo "  ❌ 彩排没过——上台前必须先看这个："
    grep -E "^  (✅|❌|⏭) 第|^  第 [0-9]+ 步：|^    " /tmp/agentmall_rehearse.log | sed 's/^/  /' | tail -30
  fi
  echo ""
  echo "完整彩排（含每步话术）：/tmp/agentmall_rehearse.log"
  exit $FAILED
fi

echo "按 Ctrl-C 停止所有服务。"
wait
