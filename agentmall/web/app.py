"""AgentMall Web App（P0 前端）：FastAPI + Jinja2 纯 HTML，无前端框架。

启动：uvicorn agentmall.web.app:app --port 8000

本文件只做「展示层」：所有业务逻辑一律转发给已经写好的
`db` / `auth` / `payment` / `store` 四个模块，本文件不改它们一个字节。

---- 登录态说明（demo 简化，生产必须换掉）----------------------------
本 demo 把**用户自己的 api_key 直接放进 cookie**（cookie 名 `agentmall_key`），
`current_user()` 拿 cookie 值去问 `auth.resolve_key()`，由 key 反查角色和 user_id。

生产环境**必须**换成正式的 session / JWT 方案，理由：
  * 明文 key 落 cookie 一旦被 XSS 读到，攻击者就等于拿到了该用户的 MCP 身份；
  * cookie 无法主动吊销（用户改密码 / key 泄漏后无法让旧 cookie 立即失效）；
  * 没有 CSRF token，跨站请求可带着 cookie 打 POST。
演示环境为了零依赖跑通全链路才这么简化，注释在此留档，避免被当成范例照抄。
------------------------------------------------------------------------
"""
from __future__ import annotations

import json
import logging
import struct
import time
import zlib
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import auth, db, payment
from ..store import get_store

BASE = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE / "templates"
STATIC_DIR = BASE / "static"

logger = logging.getLogger("agentmall.web")

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# ---- cookie / 会话（demo 简化，见模块 docstring 的生产替换说明）----
COOKIE_NAME = "agentmall_key"
COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 天


# ---------------------------------------------------------------- 工具函数
def money(value) -> str:
    """金额格式化：¥12.30。模板里用 |money。"""
    try:
        return f"{float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def fmt_time(ts) -> str:
    """unix 秒 → '2026-10-07 19:30:00'。模板里用 |dt。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return "-"


templates.env.filters["money"] = money
templates.env.filters["dt"] = fmt_time


def order_id_of(order: dict) -> str:
    """取订单号。

    注意契约不一致：任务书写 list_orders 返回项含 `order_id`，但 store.py 的
    `_order_dict()` 直接吐 `SELECT *` 的行，主键字段名其实是 `id`。
    store.py 不归我改，所以两个 key 都认，上游哪天改名也不会把前端打挂。
    """
    return str(order.get("order_id") or order.get("id") or "")


def order_user_id(order: dict) -> str:
    return str(order.get("user_id") or "")


def items_brief(items) -> str:
    """['抽纸 x2', '垃圾袋 x1'] —— 表格里一列显示完。"""
    if not isinstance(items, list):
        return ""
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or it.get("product_id") or "商品"
        qty = it.get("quantity", 1)
        out.append(f"{name} ×{qty}")
    return "、".join(out)


def items_total_qty(items) -> int:
    if not isinstance(items, list):
        return 0
    total = 0
    for it in items:
        try:
            total += int(it.get("quantity", 0))
        except (TypeError, ValueError, AttributeError):
            continue
    return total


def view_order(order: dict) -> dict:
    """把 store 的订单 dict 整成「模板友好」的视图对象。

    必须整这一层的两个原因：
    1. **Jinja 坑**：模板里写 `order.items` 拿到的是 **dict 的 items 方法**
       （Jinja 先 getattr 再取 key），会直接抛
       `TypeError: 'builtin_function_or_method' object is not iterable`。
       所以视图里商品列表的 key 叫 `lines`，不叫 `items`，模板只吃这里的字段，
       永远不直接访问 store 的原始 dict。
    2. **契约不一致**：store._order_dict 吐的主键字段是 `id`，任务书写的是
       `order_id`，这里统一成 `order_id`。
    """
    items = order.get("items")
    if not isinstance(items, list):
        items = []
    lines = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            price = float(it.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        try:
            qty = int(it.get("quantity", 1) or 1)
        except (TypeError, ValueError):
            qty = 1
        lines.append({
            "product_id": it.get("product_id", ""),
            "name": it.get("name") or it.get("product_id") or "商品",
            "price": price,
            "quantity": qty,
            "subtotal": round(price * qty, 2),
        })
    status = str(order.get("status") or "")
    try:
        total = float(order.get("total") or 0)
    except (TypeError, ValueError):
        total = 0.0
    return {
        "order_id": order_id_of(order),
        "status": status,
        "payable": status == "待支付",
        "total": total,
        "address": order.get("address", ""),
        "created_at": order.get("created_at", 0),
        "is_demo": bool(order.get("is_demo", True)),
        "pay_url": order.get("pay_url", ""),
        "lines": lines,
        "items_brief": items_brief(items),
        "qty": items_total_qty(items),
    }


# ---------------------------------------------------------------- 二维码
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _matrix_to_png(matrix, scale: int = 8) -> bytes:
    """纯标准库把二维码矩阵编码成 PNG（8 位灰度，行 filter=None，0=黑/255=白）。

    matrix 是 qrcode 的 get_matrix()，**已经自带 2 模块的静区**，所以这里不再加边距。
    注意纵向也要放大：每个矩阵行要重复 scale 次，否则行数对不上就成了坏图。
    """
    side = len(matrix) * scale
    blank = b"\xff" * scale
    rows = []
    for row in matrix:
        line = bytearray(b"\x00")  # 每行第一个字节是 filter type(0=None)
        for cell in row:
            line += b"\x00" * scale if cell else blank
        rows.extend([bytes(line)] * scale)
    ihdr = struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + _png_chunk(b"IEND", b""))


def qr_png(content: str) -> bytes:
    """收银台二维码 PNG。

    主路径是 `payment.qr_png_bytes()`（payment.py 属于契约文件，不改）。
    兜底：`qrcode.make()` 内部要 `PIL` 才能存图，而本机 .venv 没装 Pillow
    （requirements.txt 里也没有），直接调会 ModuleNotFoundError → 500。
    为了让二维码在沙箱/新机器上照样能扫，这里用 qrcode 的纯矩阵 API
    + 手写 PNG 编码兜底。**装了 Pillow 之后会自动走回主路径。**
    """
    try:
        return payment.qr_png_bytes(content)
    except Exception as exc:  # noqa: BLE001 - 任何失败都不该让收银台 500
        logger.warning("payment.qr_png_bytes 失败(%s)，回退到无 PIL 的 PNG 编码", exc)
        import qrcode

        code = qrcode.QRCode(border=2, box_size=1)
        code.add_data(content)
        code.make(fit=True)
        return _matrix_to_png(code.get_matrix())


# ---------------------------------------------------------------- 登录态
def current_user(request: Request) -> dict | None:
    """当前登录用户；未登录返回 None。

    demo 简化：cookie 里直接放用户自己的 api_key，交给 `auth.resolve_key()`
    反查角色与 user_id。**生产环境请换成正式 session/JWT**（见模块 docstring）。
    """
    key = request.cookies.get(COOKIE_NAME, "")
    if not key:
        return None
    actor = auth.resolve_key(key)
    if not actor:
        return None
    # 补上 username / api_key：resolve_key 只回角色和 actor_id，
    # 个人中心要显示用户名和给智能体用的 key 明文。
    user = {
        "role": actor.get("role", ""),
        "user_id": actor.get("actor_id", ""),
        "merchant_id": actor.get("merchant_id"),
        "username": actor.get("actor_id", ""),
        "api_key": key,
    }
    if user["role"] == auth.ROLE_USER and user["user_id"]:
        with closing(db.connect()) as conn:  # 短连接，用完立刻关
            row = conn.execute("SELECT username, api_key FROM users WHERE id=?",
                               (user["user_id"],)).fetchone()
        if row is None:
            return None  # key 有效但用户已被删
        user["username"] = row["username"]
        user["api_key"] = row["api_key"]
    return user


def require_user(request: Request) -> dict:
    """未登录 → 302 到 /login（带上 next，方便登录后跳回来）。"""
    user = current_user(request)
    if not user or user["role"] != auth.ROLE_USER:
        raise _Redirect(f"/login?next={quote(request.url.path)}")
    return user


class _Redirect(Exception):
    """内部用：把 require_user 的重定向包成异常，避免每个 handler 写 if。"""

    def __init__(self, url: str) -> None:
        self.url = url


def safe_next(raw: str | None, fallback: str = "/me") -> str:
    """只允许站内相对路径，防开放重定向。"""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return fallback
    return raw


def set_login_cookie(response: Response, api_key: str) -> None:
    # secure=False：演示跑在 http://127.0.0.1:8000 上，设 True 浏览器就不回传了。
    # 生产走 https 时必须改成 secure=True。
    response.set_cookie(
        COOKIE_NAME, api_key, max_age=COOKIE_MAX_AGE,
        httponly=True, samesite="lax", secure=False, path="/")


def mcp_config(base_url: str, api_key: str) -> dict:
    """个人中心展示的 MCP 接入点配置（可直接粘进 AI 客户端）。

    base_url 用当前请求的 host:port，这样手机连同一局域网时扫码/复制也是对的。
    """
    return {
        "mcpServers": {
            "agentmall": {
                "url": f"{base_url.rstrip('/')}/mcp",
                "headers": {"Authorization": f"Bearer {api_key}"},
            }
        }
    }


# ---------------------------------------------------------------- 订单可见性
def _user_count() -> int:
    try:
        conn = db.connect()
        try:
            return int(conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"])
        finally:
            conn.close()
    except Exception:
        return 999  # 读不出来就按「多人」处理，宁可不给看


def orders_for_user(user_id: str, limit: int = 50) -> tuple[list[dict], bool]:
    """返回 (订单列表, 是否走了「未绑定订单池」兜底)。

    正常情况就是 `store.list_orders(user_id=...)`。

    兜底只保留给**单用户**的老演示用法：stdio 模式没带 key 时建的订单 user_id
    是 NULL，网页上会一条都看不到。这种情况下全系统往往只有一个用户，把这批未绑定
    订单当作「智能体刚建的」展示出来，并在页面标注。

    **2026-10-07 安全修复**：这个池子原先对所有登录用户无条件可见可付，实测另一个
    真人注册后能看到**并且付款**别人的订单——直接破掉「用户只能看自己的单」这条
    合规红线。现在加了前提：全系统只有一个用户时才允许兜底，多用户一律不给看。
    根因（MCP 建单不传 user_id）已在 server.py 修掉，正常链路不再产生未绑定订单。
    """
    store = get_store()
    own = store.list_orders(user_id=user_id, limit=limit)
    if own:
        return own, False
    if _user_count() > 1:
        return [], False   # 多人环境：未绑定的订单不属于我，不给看
    pool = [o for o in store.list_orders(limit=limit) if not order_user_id(o)]
    return pool, bool(pool)


def can_view_order(order: dict, user_id: str) -> bool:
    """能不能看/付这一单。本人的单可以；未绑定的单仅在单用户演示时放行。"""
    uid = order_user_id(order)
    if uid:
        return uid == user_id
    return _user_count() <= 1


# ---------------------------------------------------------------- 渲染helper
def render(request: Request, name: str, user: dict | None = None,
           status_code: int = 200, **ctx):
    return templates.TemplateResponse(
        request=request, name=name,
        context={"user": user, "request": request, **ctx},
        status_code=status_code)


# ---------------------------------------------------------------- 路由
router = APIRouter()


@router.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@router.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", cat: str = "", page: int = 1):
    """商城首页：传统货架式商品流（搜索 / 类目 / 分页）。老介绍页移到 /about。"""
    page = max(int(page or 1), 1)
    per_page = 24
    st = get_store()
    # 类目列表（去重，按商品数倒序）
    with closing(db.connect()) as conn:
        cats = [r[0] for r in conn.execute(
            "SELECT category, COUNT(*) c FROM products "
            "WHERE status='在售' GROUP BY category ORDER BY c DESC")]
    # 取全部再在内存分页（演示规模，简单可靠）
    items = st.search(keyword=q, category=cat or None, limit=5000)
    total = len(items)
    pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, pages)
    chunk = items[(page - 1) * per_page: page * per_page]
    return render(request, "shop.html", current_user(request),
                  products=chunk, categories=cats, q=q, cat=cat,
                  page=page, pages=pages, total=total)


@router.get("/about", response_class=HTMLResponse)
def about(request: Request):
    """项目介绍（原首页内容）：智能体原生商城说明 + 演示边界。"""
    return render(request, "about.html", current_user(request))


@router.get("/product/{product_id}", response_class=HTMLResponse)
def product_detail(request: Request, product_id: str):
    """商品详情页。"""
    p = get_store().get(product_id)
    if p.get("error"):
        raise HTTPException(status_code=404, detail=p["error"])
    return render(request, "product.html", current_user(request), p=p)


@router.post("/buy/{product_id}", response_class=HTMLResponse)
def buy_now(request: Request, product_id: str,
            quantity: int = Form(1)):
    """网页直接购买：登录用户建单 → 跳收银台；未登录 → 去登录（带回跳）。

    演示环境：收货地址用占位（智能体下单时填真实地址），不做传统地址表单。
    """
    try:
        user = require_user(request)
    except _Redirect:
        # 回跳到商品详情页（buy 是 POST，回跳到它会 405）
        return RedirectResponse(f"/login?next={quote('/product/' + product_id)}",
                                status_code=303)
    st = get_store()
    order = st.create_order(
        product_id, quantity,
        address="演示地址（智能体下单时填写真实收货地址）",
        user_id=user["user_id"], is_demo=True)
    if order.get("error"):
        p = st.get(product_id)
        return render(request, "product.html", user, p=p,
                      error=order["error"], status_code=400)
    return RedirectResponse(f"/pay/{order_id_of(order)}", status_code=303)


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request, error: str = "", next: str = "/me"):
    return render(request, "register.html", current_user(request),
                  error=error, next_url=safe_next(next, "/me"))


@router.post("/register", response_class=HTMLResponse)
def register_submit(request: Request,
                    username: str = Form(""),
                    password: str = Form(""),
                    next: str = Form("")):
    ok, msg = auth.register(username, password)
    if not ok:
        # msg 是失败原因（用户名太短/已存在/密码太短），直接回显
        return render(request, "register.html", current_user(request),
                      error=msg, next_url=safe_next(next, "/me"), status_code=400)
    # 注册成功即视为登录：再 login 一次拿 api_key 写 cookie
    user = auth.login(username, password)
    if not user:
        return render(request, "register.html", current_user(request),
                      error="注册成功但登录失败，请手动登录", status_code=500)
    resp = RedirectResponse(safe_next(next, "/me"), status_code=303)
    set_login_cookie(resp, user["api_key"])
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = "", next: str = "/me"):
    return render(request, "login.html", current_user(request),
                  error=error, next_url=safe_next(next, "/me"))


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request,
                 username: str = Form(""),
                 password: str = Form(""),
                 next: str = Form("")):
    user = auth.login(username, password)
    if not user:
        return render(request, "login.html", current_user(request),
                      error="用户名或密码错误", next_url=safe_next(next, "/me"),
                      status_code=401)
    resp = RedirectResponse(safe_next(next, "/me"), status_code=303)
    set_login_cookie(resp, user["api_key"])
    return resp


@router.api_route("/logout", methods=["GET", "POST"])
def logout(request: Request):
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@router.get("/me", response_class=HTMLResponse)
def me(request: Request):
    """个人中心：MCP 接入点（一键复制）+ 本人的 api_key 明文。"""
    try:
        user = require_user(request)
    except _Redirect as r:
        return RedirectResponse(r.url, status_code=303)

    base_url = str(request.base_url).rstrip("/")
    cfg = mcp_config(base_url, user["api_key"])
    cfg_text = json.dumps(cfg, ensure_ascii=False, indent=2)
    orders, from_pool = orders_for_user(user["user_id"], limit=10)
    return render(request, "me.html", user,
                  mcp_cfg=cfg, mcp_cfg_text=cfg_text, base_url=base_url,
                  mcp_endpoint=f"{base_url}/mcp",
                  recent_orders=[view_order(o) for o in orders],
                  from_pool=from_pool)


@router.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request):
    try:
        user = require_user(request)
    except _Redirect as r:
        return RedirectResponse(r.url, status_code=303)
    orders, from_pool = orders_for_user(user["user_id"], limit=100)
    return render(request, "orders.html", user,
                  orders=[view_order(o) for o in orders], from_pool=from_pool)


@router.get("/api/orders")
def api_orders(request: Request, limit: int = 50):
    """给前端 5 秒轮询用的 JSON 快照（Cache-Control: no-store）。"""
    user = current_user(request)
    if not user or user["role"] != auth.ROLE_USER:
        return JSONResponse({"ok": False, "error": "未登录", "orders": []},
                            status_code=401,
                            headers={"Cache-Control": "no-store"})
    rows, from_pool = orders_for_user(user["user_id"], limit=limit)
    payload = [view_order(o) for o in rows]
    return JSONResponse(
        {"ok": True, "count": len(payload), "from_pool": from_pool,
         "orders": payload},
        headers={"Cache-Control": "no-store"})


def _load_order_or_404(request: Request, order_id: str, user: dict):
    """取订单；不存在 / 不属于当前用户 → 渲染错误页。"""
    order = get_store().get_order(order_id)
    if not order or order.get("error") or not order_id_of(order):
        return None, render(request, "pay.html", user, error="订单不存在",
                            order=None, paid_now=False, just_paid=False,
                            status_code=404)
    if not can_view_order(order, user["user_id"]):
        return None, render(request, "pay.html", user, error="无权查看他人订单",
                            order=None, paid_now=False, just_paid=False,
                            status_code=403)
    return view_order(order), None


@router.get("/pay/{order_id}", response_class=HTMLResponse)
def pay_page(request: Request, order_id: str, paid: int = 0):
    try:
        user = require_user(request)
    except _Redirect as r:
        return RedirectResponse(r.url, status_code=303)
    order, err = _load_order_or_404(request, order_id, user)
    if err is not None:
        return err
    return render(request, "pay.html", user, order=order,
                  oid=order["order_id"], paid_now=not order["payable"],
                  just_paid=bool(paid))


@router.get("/pay/{order_id}/qr.png")
def pay_qr(request: Request, order_id: str):
    """收银台二维码：内容 = **收银台页 URL**，演示用，不含敏感信息。

    注意不能直接用 `request.url`：本路由自己的 URL 是 `/pay/{id}/qr.png`，
    直接编码的话扫出来是个 PNG 而不是收银台页。这里把 path 换回收银台页、
    query（含 ?t= 演示 token）原样保留。
    """
    cashier_url = str(request.url.replace(path=f"/pay/{order_id}"))
    if cashier_url.endswith("?"):
        cashier_url = cashier_url[:-1]
    return Response(content=qr_png(cashier_url), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.post("/pay/{order_id}/confirm", response_class=HTMLResponse)
def pay_confirm(request: Request, order_id: str):
    """用户本人在浏览器里点「确认支付」。

    checkout hands off：智能体不参与这一步，也不会自动扣款。
    """
    try:
        user = require_user(request)
    except _Redirect as r:
        return RedirectResponse(r.url, status_code=303)
    order, err = _load_order_or_404(request, order_id, user)
    if err is not None:
        return err

    result = get_store().confirm_payment(order_id)
    if result.get("error"):
        return render(request, "pay.html", user, error=result["error"],
                      order=order, oid=order["order_id"],
                      paid_now=False, just_paid=False, status_code=400)
    # POST-redirect-GET：刷新收银台不会重复扣（这里是模拟的，但习惯要保持）
    return RedirectResponse(f"/pay/{order_id}?paid=1", status_code=303)


# ---------------------------------------------------------------- 启动
@asynccontextmanager
async def lifespan(_app):
    """建表 + seed（db.init_db 内部有锁和幂等保护，重复调用安全）。"""
    db.init_db()
    get_store()  # 预热 Store（内部同样会 init_db）
    yield


# 启动命令：uvicorn agentmall.web.app:app --port 8000
app = FastAPI(
    title="AgentMall Web",
    description="AgentMall 演示前端（演示数据 + 模拟支付，不构成真实交易）",
    version="0.1.0-mvp",
    lifespan=lifespan,
)
app.include_router(router)
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------- 兜底错误页
# 真实用户会输错网址、点到过期的链接。以前这里直接吐 FastAPI 的裸 JSON
# {"detail":"Not Found"}，看着像坏了而不是像站点。改成带导航的友好页面。
@app.exception_handler(404)
async def _not_found(request: Request, exc):
    if request.url.path.startswith(("/api/", "/static/")):
        return JSONResponse({"ok": False, "error": "资源不存在"}, status_code=404)
    try:
        return templates.TemplateResponse(
            request, "error_404.html",
            {"request": request, "user": _safe_user(request), "base": ""},
            status_code=404)
    except Exception:
        return HTMLResponse(
            '<meta charset="utf-8"><h1>404 · 页面不存在</h1>'
            '<p><a href="/">回首页</a></p>', status_code=404)


@app.exception_handler(500)
async def _server_error(request: Request, exc):
    """别把栈信息甩给用户——演示现场崩了要能圆回来。"""
    try:
        return templates.TemplateResponse(
            request, "error_500.html",
            {"request": request, "user": _safe_user(request), "base": ""},
            status_code=500)
    except Exception:
        return HTMLResponse(
            '<meta charset="utf-8"><h1>500 · 服务出错了</h1>'
            '<p><a href="/">回首页</a></p>', status_code=500)


def _safe_user(request: Request):
    try:
        return current_user(request)
    except Exception:
        return None
