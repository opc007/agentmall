#!/usr/bin/env bash
# 把演示库重置回「刚 seed 完」的状态——上台前跑一次。
#
#   ./scripts/reset_demo.sh            重置（先自动备份到 /tmp）
#   ./scripts/reset_demo.sh --force    连备份都不留（不建议）
#
# 为什么需要：路演彩排（scripts/rehearse.py）和手动试玩都会往演示库里写
# 订单和注册用户。上台前如果看板订单数是 5、里面混着彩排的单，说服力会打折。
#
# 建议顺序：start_demo.sh --check（先验）→ reset_demo.sh（再清）→ start_demo.sh（开演）
#
# 旧库会先备份到 /tmp/agentmall_demo_backup_<时间戳>.db，重置错了可以拷回来。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

# db.py 里 DB_PATH 的位置，别在这里写死第二份
DB="$("$PY" -c 'import sys; sys.path.insert(0,"."); from agentmall import db; print(db.DB_PATH)')"
[ -n "$DB" ] && [ -f "$DB" ] || { echo "ℹ️  演示库还不存在（$DB），直接 seed 一次即可"; exit 0; }

if [ "${1:-}" = "--force" ]; then
  echo "▶ --force：不备份，直接删 $DB"
  rm -f "$DB" "$DB-wal" "$DB-shm"
else
  BK="/tmp/agentmall_demo_backup_$(date +%Y%m%d_%H%M%S).db"
  cp "$DB" "$BK"
  echo "▶ 已备份到 $BK"
  rm -f "$DB" "$DB-wal" "$DB-shm"
fi

"$PY" -c '
import sys; sys.path.insert(0, ".")
from agentmall import db
db.init_db()
c = db.connect()
n = {t: c.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
     for t in ("products", "merchants", "users", "orders")}
print("✅ 已重建：" + " · ".join(f"{k} {v}" for k, v in n.items()))
print("   库位置：", db.DB_PATH)
'
echo "   下一步：./scripts/start_demo.sh   （起服务，正式开演）"
