"""注册/登录、口令 hash、per-agent key 鉴权。

鉴权契约（Phase2任务书 §2）：每个请求带 `Authorization: Bearer <key>`，
key 决定角色，server 只暴露该角色的 tool，越权直接拒绝并记审计。
"""
import hashlib
import hmac
import os
import secrets
from contextvars import ContextVar

from . import db

# streamable-http 模式下，每个请求的 Bearer key 存在 ContextVar 里，
# 这样并发请求互不串味（env var 是全局的，并发下会串）。
_request_key: ContextVar[str] = ContextVar("agentmall_request_key", default="")

_PBKDF2_ROUNDS = 120_000


def set_request_key(api_key: str):
    """中间件里调用。返回 token，调用方负责 reset。"""
    return _request_key.set(api_key or "")


def get_request_key() -> str:
    return _request_key.get()


def reset_request_key(token) -> None:
    _request_key.reset(token)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(expected.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def new_api_key(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


# ---- 用户注册/登录 ----

def register(username: str, password: str) -> tuple[bool, str]:
    username = (username or "").strip()
    if len(username) < 2:
        return False, "用户名至少 2 个字符"
    if len(password or "") < 6:
        return False, "密码至少 6 位"
    db.init_db()
    conn = db.connect()
    try:
        if conn.execute("SELECT 1 FROM users WHERE username=?",
                        (username,)).fetchone():
            return False, "用户名已被注册"
        uid = "U" + secrets.token_hex(6).upper()
        conn.execute(
            "INSERT INTO users(id,username,password_hash,api_key,created_at) "
            "VALUES(?,?,?,?,?)",
            (uid, username, hash_password(password), new_api_key("uk"), db.now()))
        conn.commit()
        return True, uid
    except Exception:
        return False, "注册失败，请稍后重试"
    finally:
        conn.close()


def login(username: str, password: str) -> dict | None:
    db.init_db()
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE username=?",
                           ((username or "").strip(),)).fetchone()
        if not row or not verify_password(password or "", row["password_hash"]):
            return None
        return {"user_id": row["id"], "username": row["username"],
                "api_key": row["api_key"]}
    finally:
        conn.close()


# ---- per-agent key → 角色 ----

ROLE_USER = "user"
ROLE_MERCHANT = "merchant"
ROLE_ADMIN = "admin"

# 角色 → 允许的 tool 集合。未列出的 tool 一律拒绝（越权）。
ROLE_TOOLS: dict[str, set[str]] = {
    ROLE_USER: {"search_products", "get_product", "create_order", "get_order"},
    ROLE_ADMIN: {"admin_review_product", "admin_takedown_product",
                 "admin_list_merchants", "admin_stats", "admin_audit_log"},
}
# 商户侧是动态的（见 merchant_tool_allowed），先放开名单再按归属校验
ROLE_TOOLS[ROLE_MERCHANT] = {"merchant_publish_product", "merchant_update_product",
                             "merchant_list_products", "merchant_list_orders",
                             "merchant_fulfill_order"}


def resolve_key(api_key: str) -> dict | None:
    """key → {role, actor_id, merchant_id?}。无效 key 返回 None。"""
    api_key = (api_key or "").strip()
    if not api_key:
        return None
    db.init_db()
    conn = db.connect()
    try:
        row = conn.execute("SELECT id FROM users WHERE api_key=?",
                           (api_key,)).fetchone()
        if row:
            return {"role": ROLE_USER, "actor_id": row["id"], "merchant_id": None}
        row = conn.execute("SELECT id,api_key FROM merchants WHERE api_key=?",
                           (api_key,)).fetchone()
        if row:
            return {"role": ROLE_MERCHANT, "actor_id": row["id"],
                    "merchant_id": row["id"]}
        row = conn.execute("SELECT id FROM admin_keys WHERE api_key=?",
                           (api_key,)).fetchone()
        if row:
            return {"role": ROLE_ADMIN, "actor_id": row["id"], "merchant_id": None}
        return None
    finally:
        conn.close()


def key_from_header(authorization: str | None) -> str:
    """从 `Authorization: Bearer <key>` 抽出 key。"""
    if not authorization:
        return ""
    parts = authorization.split(None, 1)
    return parts[1].strip() if len(parts) == 2 else ""


def deny_unauthorized(actor: dict | None, tool: str, target: str = "") -> dict:
    """越权拒绝并记审计（Phase2任务书 §6 验收标准 1）。"""
    db.init_db()
    conn = db.connect()
    try:
        role = actor["role"] if actor else "anonymous"
        actor_id = actor["actor_id"] if actor else "-"
        db.log_audit(conn, role, actor_id, f"deny:{tool}", target,
                     "越权调用被拒绝")
        conn.commit()
    finally:
        conn.close()
    return {"error": f"越权：{role} 角色无权调用 {tool}", "denied": True}
