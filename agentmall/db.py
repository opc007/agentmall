"""SQLite 持久化数据层（三角色共用）。

Phase 1 用内存 dict，重启丢订单；Phase 2 换成 SQLite，订单/商品/审计重启不丢
（Phase2任务书 §4 数据模型）。

并发：MCP server（stdio，独立进程）与 Web（FastAPI）会同时访问同一个库，
所以开 WAL + busy_timeout，并统一走 connect() 拿短连接。
"""
import json
import os
import sqlite3
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("AGENTMALL_DB", os.path.join(BASE, "data", "agentmall.db"))

_init_lock = threading.Lock()
_initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS merchants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    api_key     TEXT NOT NULL UNIQUE,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_keys (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    api_key     TEXT NOT NULL UNIQUE,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    api_key       TEXT NOT NULL UNIQUE,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id             TEXT PRIMARY KEY,
    merchant_id    TEXT NOT NULL DEFAULT 'M001',
    name           TEXT NOT NULL,
    category       TEXT NOT NULL,
    price          REAL NOT NULL,
    original_price REAL NOT NULL DEFAULT 0,
    stock          INTEGER NOT NULL DEFAULT 0,
    unit           TEXT NOT NULL DEFAULT '件',
    specs          TEXT NOT NULL DEFAULT '',
    image_url      TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT '在售',   -- 待审核 / 在售 / 下架
    created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_status ON products(status);
CREATE INDEX IF NOT EXISTS idx_products_cat    ON products(category);

CREATE TABLE IF NOT EXISTS orders (
    id         TEXT PRIMARY KEY,
    user_id    TEXT,
    items_json TEXT NOT NULL,
    total      REAL NOT NULL,
    address    TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT '待支付',
    pay_url    TEXT NOT NULL DEFAULT '',
    is_demo    INTEGER NOT NULL DEFAULT 1,
    tracking_no TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_role  TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);

-- 写操作不可删改（合规红线：管理员审计日志不可篡改）
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log 不可删除'); END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log 不可修改'); END;
"""

# 演示用商户与管理员 key（Phase2任务书 §2：预置 2-3 个测试商户含 key）
SEED_MERCHANTS = [
    ("M001", "日用优选生活馆", "mk_demo_m001_secret"),
    ("M002", "洁美家居日杂", "mk_demo_m002_secret"),
    ("M003", "个人护理专营店", "mk_demo_m003_secret"),
]
SEED_ADMIN = ("A001", "平台管理员", "ak_demo_admin_secret")
SEED_USER = ("U001", "demo", "demo123", "uk_demo_user_secret")


def now() -> int:
    return int(time.time())


def connect() -> sqlite3.Connection:
    """短连接。调用方负责 close（或用 with closing(...)）。"""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
            _seed(conn)
        finally:
            conn.close()
        _initialized = True


def _seed(conn: sqlite3.Connection) -> None:
    from .auth import hash_password

    ts = now()
    for mid, name, key in SEED_MERCHANTS:
        conn.execute(
            "INSERT OR IGNORE INTO merchants(id,name,api_key,created_at) VALUES(?,?,?,?)",
            (mid, name, key, ts))
    conn.execute(
        "INSERT OR IGNORE INTO admin_keys(id,name,api_key,created_at) VALUES(?,?,?,?)",
        (SEED_ADMIN[0], SEED_ADMIN[1], SEED_ADMIN[2], ts))
    conn.execute(
        "INSERT OR IGNORE INTO users(id,username,password_hash,api_key,created_at) "
        "VALUES(?,?,?,?,?)",
        (SEED_USER[0], SEED_USER[1], hash_password(SEED_USER[2]), SEED_USER[3], ts))

    if conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"] == 0:
        _seed_products(conn, ts)
    conn.commit()


def _seed_products(conn: sqlite3.Connection, ts: int) -> None:
    """优先灌真实采集数据；没有就退回 30 SKU 演示种子。"""
    real_path = os.path.join(BASE, "data", "products_real.json")
    if os.path.exists(real_path):
        try:
            with open(real_path, encoding="utf-8") as f:
                payload = json.load(f)
            rows = payload.get("products", [])
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO products"
                    "(id,merchant_id,name,category,price,original_price,stock,"
                    "unit,specs,image_url,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'在售',?)",
                    [(r.get("id") or f"P{i:03d}", r.get("merchant_id", "M001"),
                      r["name"], r["category"], float(r["price"]),
                      float(r.get("original_price") or r["price"]),
                      int(r.get("stock", 100)), r.get("unit", "件"),
                      r.get("specs", ""), r.get("image_url", ""), ts)
                     for i, r in enumerate(rows, start=101)])
                return
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            pass  # 采集文件有问题就退回演示种子，不阻塞启动

    legacy = os.path.join(BASE, "data", "products_seed.json")
    if not os.path.exists(legacy):
        return
    with open(legacy, encoding="utf-8") as f:
        rows = json.load(f)
    conn.executemany(
        "INSERT OR REPLACE INTO products"
        "(id,merchant_id,name,category,price,original_price,stock,unit,specs,"
        "image_url,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,'','在售',?)",
        [(p["id"], "M001", p["name"], p["category"], float(p["price"]),
          float(p.get("original_price", p["price"])), int(p.get("stock", 0)),
          p.get("unit", "件"), p.get("specs", ""), ts) for p in rows])


def log_audit(conn: sqlite3.Connection, actor_role: str, actor_id: str,
              action: str, target: str = "", detail: str = "") -> None:
    """审计日志只能追加（表上有禁删改触发器）。"""
    conn.execute(
        "INSERT INTO audit_log(actor_role,actor_id,action,target,detail,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (actor_role, actor_id, action, target, detail, now()))


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
