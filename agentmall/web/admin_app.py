"""AgentMall 管理后台（管理员视角，独立进程）。

与用户面 Web（agentmall/web/app.py，:8000）**物理隔离**，独立跑在 :8002。
这不是图省事——docs/角色架构.md 定死三权分立：买的人、卖的人、管平台的人
看到的数据和能做的操作完全不同。管理面单独一个 app，避免与用户面互相牵连。

启动： .venv/bin/python -m uvicorn agentmall.web.admin_app:app --port 8002

鉴权：登录页输入管理员 key（演示用预置 key）。key 即身份，存在 cookie 里，
所有写操作都带 actor 走 AdminService，越权由服务层拦 + 记审计。
"""
import os
import sys
from contextlib import asynccontextmanager, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from agentmall import auth, db
from agentmall.roles import admin_service

TPL = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TPL))
DEMO_ADMIN_KEY = "ak_demo_admin_secret"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="AgentMall 管理后台", lifespan=lifespan)
ADMIN_KEY_COOKIE = "agentmall_admin_key"


def _actor(request: Request) -> dict | None:
    key = request.cookies.get(ADMIN_KEY_COOKIE, "")
    actor = auth.resolve_key(key) if key else None
    if actor and actor["role"] == auth.ROLE_ADMIN:
        return actor
    return None


def _guard(request: Request):
    """未登录或非管理员 → 重定向到登录页。"""
    if _actor(request) is None:
        return RedirectResponse("/login", status_code=303)
    return None


def _flash(request: Request, msg: str = "", level: str = "ok") -> dict:
    return {"msg": msg, "level": level}


def list_platform_orders(limit: int = 100, status: str = "") -> list[dict]:
    """全平台订单（管理员视角）。

    AdminService 的 5 个 tool 里没有"列全平台订单"（那是给 AI 智能体用的），
    但管理后台页面必须有。这里走只读 SQL 直查，不经过服务层。
    买家地址在页面上脱敏到市级（合规红线：管理员看到的用户隐私字段需脱敏）。
    """
    import json

    sql = "SELECT * FROM orders"
    args: list = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(int(limit), 1))
    with closing(db.connect()) as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["items"] = json.loads(d.get("items_json") or "[]")
        except json.JSONDecodeError:
            d["items"] = []
        d.pop("items_json", None)
        d["order_id"] = d.get("id", "")
        # 地址脱敏：只留到市级
        d["address_masked"] = _mask_city(d.get("address", ""))
        out.append(d)
    return out


def _mask_city(addr: str) -> str:
    import re
    text = re.sub(r"[\s,，、#\-—]+", "", addr or "")
    for mark in ("市", "州", "盟"):
        idx = text.find(mark)
        if idx != -1:
            return text[:idx + 1]
    return text[:6]


def list_products_for_admin(limit: int = 200) -> list[dict]:
    sql = ("SELECT id, merchant_id, name, category, price, stock, status, created_at "
           "FROM products ORDER BY created_at DESC LIMIT ?")
    with closing(db.connect()) as conn:
        return [dict(r) for r in conn.execute(sql, (limit,)).fetchall()]


# ------------------------------------------------------------------ 页面

@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "agentmall-admin"}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, msg: str = "", level: str = "err"):
    return templates.TemplateResponse(request, "admin_login.html", {
"msg": msg, "level": level,
        "demo_key": DEMO_ADMIN_KEY})


@app.post("/login")
async def login(request: Request, key: str = Form(...)):
    actor = auth.resolve_key(key.strip())
    if not actor or actor["role"] != auth.ROLE_ADMIN:
        return RedirectResponse(
            f"/login?msg={'不是管理员 key' if actor else 'key 无效'}&level=err",
            status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(ADMIN_KEY_COOKIE, key.strip(), httponly=True, samesite="lax")
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(ADMIN_KEY_COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if (g := _guard(request)):
        return g
    actor = _actor(request)
    stats = admin_service.stats(actor)
    return templates.TemplateResponse(request, "admin_dashboard.html", {
"actor": actor, "stats": stats,
        "orders": list_platform_orders(limit=20),
        "pending": [p for p in list_products_for_admin() if p["status"] == "待审核"][:20],
        **_flash(request, request.query_params.get("msg", ""),
                 request.query_params.get("level", "ok"))})


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, status: str = ""):
    if (g := _guard(request)):
        return g
    return templates.TemplateResponse(request, "admin_orders.html", {
"actor": _actor(request),
        "orders": list_platform_orders(status=status), "status": status,
        **_flash(request, request.query_params.get("msg", ""),
                 request.query_params.get("level", "ok"))})


@app.get("/products", response_class=HTMLResponse)
async def products_page(request: Request):
    if (g := _guard(request)):
        return g
    return templates.TemplateResponse(request, "admin_products.html", {
"actor": _actor(request),
        "products": list_products_for_admin(),
        **_flash(request, request.query_params.get("msg", ""),
                 request.query_params.get("level", "ok"))})


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, limit: int = 100):
    if (g := _guard(request)):
        return g
    logs = admin_service.audit_log(_actor(request), limit)
    return templates.TemplateResponse(request, "admin_audit.html", {
"actor": _actor(request), "logs": logs,
        **_flash(request, request.query_params.get("msg", ""),
                 request.query_params.get("level", "ok"))})


@app.post("/products/{product_id}/takedown")
async def takedown(request: Request, product_id: str, reason: str = Form("")):
    if (g := _guard(request)):
        return g
    res = admin_service.takedown_product(_actor(request), product_id, reason)
    ok = "error" not in res
    return RedirectResponse(
        f"/products?msg={'已下架 ' + product_id if ok else res['error']}"
        f"&level={'ok' if ok else 'err'}", status_code=303)


@app.post("/products/{product_id}/review")
async def review(request: Request, product_id: str, approve: str = Form("")):
    if (g := _guard(request)):
        return g
    res = admin_service.review_product(
        _actor(request), product_id, approve == "1", request.query_params.get("reason", ""))
    ok = "error" not in res
    return RedirectResponse(
        f"/products?msg={'已通过 ' + product_id if ok else res['error']}"
        f"&level={'ok' if ok else 'err'}", status_code=303)


def main() -> None:  # pragma: no cover
    import uvicorn

    port = int(os.environ.get("AGENTMALL_ADMIN_PORT", "8002"))
    print(f"AgentMall 管理后台 → http://127.0.0.1:{port}  (管理员 key: {DEMO_ADMIN_KEY})")
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
