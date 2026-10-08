"""AgentMall 商户门户（商户智能体自助注册 + 商户工作台），独立进程 :8003。

启动：uvicorn agentmall.web.merchant_app:app --port 8003

与用户面 Web（app.py，:8000）、管理后台（admin_app.py，:8002）**物理隔离**，
理由同 admin_app：三权分立是定死的架构约束，卖的人不该看到买家数据，
也不该跟买家共用一套登录态（`/login`、`/logout` 这些路由名三个 app 都占）。

本文件只做「展示层 + 商户账号的注册/登录」，商品/订单的业务动作一律走 MCP
（MerchantService），网页上不提供直接改价改库存的表单——商户智能体才是主力。

---- 登录态说明（demo 简化，生产必须换掉）----------------------------
和用户面一样，cookie 里直接放**商户自己的 api_key**（`agentmall_merchant_key`），
`current_merchant()` 把 cookie 交给 `auth.resolve_key()` 反查角色与 merchant_id。

生产环境**必须**换成正式 session / JWT，理由与 app.py docstring 同：
  * 明文 key 落 cookie，一旦被 XSS 读到，攻击者就等于拿到了该商户的 MCP 身份
    （商户 key 能改价、改库存、发货，比买家 key 危险得多）；
  * cookie 无法主动吊销（商户改密码 / key 泄漏后旧 cookie 立即失效做不到）；
  * 没有 CSRF token，跨站请求可带着 cookie 打 POST。
演示环境为了零依赖跑通「注册 → 拿 key → 智能体接进来」才这么简化，注释留档。
------------------------------------------------------------------------

---- 商户归属的唯一依据 ----------------------------------------------------
`auth.resolve_key()` 返回的 `merchant_id` 是唯一归属依据：门户里所有统计、
所有 MCP 能力都按它过滤，自己也**不再**额外读任何全局数据，
所以商户侧看不到别家商户的任何东西。
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import sqlite3
import secrets
import sys
import time
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agentmall import auth, db  # noqa: E402
from agentmall.roles import merchant_service  # noqa: E402

logger = logging.getLogger("agentmall.web.merchant")

TPL = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TPL))

# ---- 挂路径前缀（nginx 反代到 /shop 之类）---------------------------------
# 独立端口裸跑（:8003）时留空。所有内部链接一律走 `_u()`，
# **不要写根绝对路径**：用户面也占了 `/login`、`/logout`，
# 挂到子路径下还生成 /login 会跳到买家那边的同名路由（管理后台踩过这个坑，
# 见 test_admin_console.py::test_admin_base_path，这里锁同样的回归）。
BASE_PATH = os.environ.get("AGENTMALL_MERCHANT_BASE_PATH", "").rstrip("/")

COOKIE_NAME = "agentmall_merchant_key"
COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 天
# cookie path 跟 BASE_PATH 走：挂在 /shop 下时，cookie 只发给 /shop/*，
# 不会跟同域其他 app 的同名 cookie 打架。
COOKIE_PATH = BASE_PATH or "/"

NAME_MIN, NAME_MAX = 2, 30
PASSWORD_MIN = 8

# MCP server 是**另一个进程**（:8001），不在本门户的网关上，
# 所以不能拿 request.base_url 拼——那会指到 :8003 自己。
# 优先 AGENTMALL_MCP_PUBLIC_URL，其次按「同 host + MCP 端口」推：
# 手机连局域网时 host 跟着请求走，复制出去的配置在手机上也是对的。
MCP_PORT = os.environ.get("AGENTMALL_MCP_PORT", "8001")
MCP_PATH = "/mcp"


def _u(path: str) -> str:
    """把站内路径加上 BASE_PATH 前缀。"""
    return f"{BASE_PATH}{path if path.startswith('/') else '/' + path}"


# ---------------------------------------------------------------- 小工具
def fmt_time(ts) -> str:
    """unix 秒 → '2026-10-07 19:30:00'。模板里用 |dt。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return "-"


templates.env.filters["dt"] = fmt_time


def render(request: Request, tpl: str, /, status_code: int = 200, **ctx):
    """渲染模板。

    模板名 `tpl` 做成**位置专用**参数（`/`）：页面上下文里经常有
    `name=`（登录/注册表单回填店名）、`status=`（商户状态）这类同名字段，
    普通参数会被 **kwargs 撞成 `got multiple values for argument`。
    注意别把 `status_code` 也设成位置专用——位置专用参数**不能用关键字传值**，
    `render(..., status_code=400)` 会落进 **ctx，状态码静默变回 200。
    """
    return templates.TemplateResponse(request, tpl, {"request": request, **ctx},
                                      status_code=status_code)


def _flash(request: Request, param: str = "msg") -> tuple[str, str]:
    q = request.query_params
    return q.get(param, ""), q.get("level", "ok")


def fix_utf8_mojibake(text: str) -> str:
    """表单里非百分号编码的 UTF-8 字节，按 latin-1 解出来就是乱码，修回去。

    HTTP 规定表单字节流按 latin-1 解，所以 `curl -d "name=南宁好物"` 这种
    没做百分号编码的请求（路演现场脚本/临时命令行很容易这么写）会在库里留下
    `åå®å¥½...`。浏览器发的都是 %XX 形式的 UTF-8，压根走不到这里。

    只在「latin-1 字节恰好是合法 UTF-8」时才改写；改不动就原样返回，
    密码**不**走这里（动密码等于换了一把锁）。
    """
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


# ---------------------------------------------------------------- MCP 接入点
def mcp_base_url(request: Request) -> str:
    """MCP server 的对外基地址（不含 /mcp）。"""
    env = (os.environ.get("AGENTMALL_MCP_PUBLIC_URL") or "").strip()
    if env:
        return env.rstrip("/")
    host = request.url.hostname or "127.0.0.1"
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    if ":" in host:  # IPv6 字面量要加方括号
        host = f"[{host}]"
    return f"http://{host}:{MCP_PORT}"


def mcp_config(base: str, api_key: str) -> dict:
    """商户门户展示的 MCP 接入点配置（可直接粘进支持远程 MCP 的客户端）。"""
    return {
        "mcpServers": {
            "agentmall-merchant": {
                "url": f"{base.rstrip('/')}{MCP_PATH}",
                "headers": {"Authorization": f"Bearer {api_key}"},
            }
        }
    }


# ---------------------------------------------------------------- 账号
class RegisterError(Exception):
    """注册失败，message 直接展示给用户。"""


def _new_merchant_id(conn: sqlite3.Connection) -> str:
    """生成 'M' + 随机后缀的商户 ID。

    必须查库确认不与既有 id 撞（种子是 M001/M002/M003，未来别人手工插的也可能有
    各种形状），所以不靠「位数够长就不可能撞」这种运气假设。
    """
    for _ in range(20):
        cand = "M" + secrets.token_hex(4).upper()
        if not conn.execute("SELECT 1 FROM merchants WHERE id=?",
                            (cand,)).fetchone():
            return cand
    raise RegisterError("生成商户 ID 失败，请重试")


def register_merchant(name: str, password: str, confirm: str) -> dict:
    """商户自助注册。成功返回 {merchant_id, api_key, name}，失败抛 RegisterError。

    口径（已定）：注册完**立即可用**（status='active'），不再加商户注册审核；
    需要审核的是**商品上架**——新商品一律「待审核」，管理员审核通过才在售。
    """
    name = fix_utf8_mojibake((name or "").strip())
    password = password or ""
    if not NAME_MIN <= len(name) <= NAME_MAX:
        raise RegisterError(f"店名需要 {NAME_MIN}–{NAME_MAX} 个字符（当前 {len(name)} 个）")
    if len(password) < PASSWORD_MIN:
        raise RegisterError(f"密码至少 {PASSWORD_MIN} 位（当前 {len(password)} 位）")
    if password != (confirm or ""):
        raise RegisterError("两次输入的密码不一致")

    db.init_db()
    with closing(db.connect()) as conn:
        for _ in range(3):
            # 店名唯一是**应用层**约束（表上没有 UNIQUE），所以显式查：
            # 重复必须明确报错，不能静默覆盖老商户（覆盖 = 顶号 + 改归属）。
            dup = conn.execute("SELECT id FROM merchants WHERE name=?",
                               (name,)).fetchone()
            if dup is not None:
                raise RegisterError(
                    f"店名「{name}」已被注册（商户号 {dup['id']}），请换一个店名")
            mid = _new_merchant_id(conn)
            api_key = auth.new_api_key("mk")
            try:
                conn.execute(
                    "INSERT INTO merchants"
                    "(id,name,api_key,password_hash,status,created_at) "
                    "VALUES(?,?,?,?,'active',?)",
                    (mid, name, api_key, auth.hash_password(password), db.now()))
                db.log_audit(conn, auth.ROLE_MERCHANT, mid, "merchant_register",
                             mid, json.dumps({"name": name}, ensure_ascii=False))
                conn.commit()
                return {"merchant_id": mid, "api_key": api_key, "name": name}
            except sqlite3.IntegrityError:
                # 并发下 id / api_key 撞车（几乎不可能，但撞了就换一个重试）
                conn.rollback()
                continue
        raise RegisterError("注册失败，请稍后重试")


def login_merchant(name: str, password: str) -> dict:
    """店名 + 口令登录。成功返回商户视图（含 api_key），失败抛 RegisterError。"""
    name = fix_utf8_mojibake((name or "").strip())
    db.init_db()
    with closing(db.connect()) as conn:
        # 店名唯一是应用层约束，万一有历史重复行，取最早注册的那个
        row = conn.execute(
            "SELECT id,name,api_key,password_hash,status FROM merchants "
            "WHERE name=? ORDER BY created_at ASC, id ASC LIMIT 1", (name,)).fetchone()

    if row is None:
        # 不区分「店名不存在」和「密码错」，避免被拿去枚举店名
        raise RegisterError("店名或密码错误")
    if not (row["password_hash"] or "").strip():
        # 种子商户（M001/M002/M003）建库时就没口令，只走 api_key 鉴权。
        # 这里必须给人话提示，不能落到 verify_password 抛异常 → 500 裸 JSON。
        raise RegisterError(
            f"「{name}」是演示种子商户，没有网页口令，无法用密码登录。"
            "请用它的 API key 直接接入 MCP；网页登录请用自助注册的店名。")
    if not auth.verify_password(password or "", row["password_hash"]):
        raise RegisterError("店名或密码错误")
    if (row["status"] or "active") != "active":
        raise RegisterError("账号已被平台停用（suspended），请联系平台管理员")
    return {"merchant_id": row["id"], "name": row["name"], "api_key": row["api_key"],
            "status": row["status"]}


def current_merchant(request: Request) -> dict | None:
    """当前登录商户；未登录 / key 无效 / key 不是商户角色一律返回 None。"""
    key = request.cookies.get(COOKIE_NAME, "")
    if not key:
        return None
    actor = auth.resolve_key(key)
    if not actor or actor.get("role") != auth.ROLE_MERCHANT:
        return None
    # resolve_key 的 merchant_id 是唯一归属依据，后面所有统计都按它过滤
    mid = actor.get("merchant_id") or actor.get("actor_id")
    if not mid:
        return None
    with closing(db.connect()) as conn:  # 短连接，用完立刻关
        row = conn.execute(
            "SELECT id,name,api_key,status,created_at FROM merchants WHERE id=?",
            (mid,)).fetchone()
    if row is None:  # key 有效但商户已不存在
        return None
    return {"merchant_id": row["id"], "name": row["name"], "api_key": row["api_key"],
            "status": row["status"], "created_at": row["created_at"],
            "actor_id": actor.get("actor_id", mid)}


def _safe_merchant(request: Request) -> dict | None:
    """错误页里取当前商户；DB 抖了也不该让错误页自己再炸一次。"""
    try:
        return current_merchant(request)
    except Exception:  # noqa: BLE001
        return None


def guard(request: Request):
    """未登录 → 303 到登录页。首页等门户页面统一走它。"""
    if current_merchant(request) is None:
        return RedirectResponse(_u("/login"), status_code=303)
    return None


def set_login_cookie(response: Response, api_key: str) -> None:
    # secure=False：演示跑 http://127.0.0.1:8003 上，设 True 浏览器就不回传了。
    # 生产走 https 时必须改成 secure=True。
    response.set_cookie(COOKIE_NAME, api_key, max_age=COOKIE_MAX_AGE,
                        httponly=True, samesite="lax", secure=False,
                        path=COOKIE_PATH)


# ---------------------------------------------------------------- 能力指引
# 「5 个 tool 能干什么」不能凭记忆写——roles.py / server.py 是唯一事实来源。
# 这里在**运行时**把 docstring 和签名抠出来，页面上的描述不可能与服务层脱节。
_TOOL_SPECS = [
    ("merchant_publish_product", "publish_product"),
    ("merchant_update_product", "update_product"),
    ("merchant_list_products", "list_products"),
    ("merchant_list_orders", "list_orders"),
    ("merchant_fulfill_order", "fulfill_order"),
]

# 坑位说明是**我们自己写给商户看的**（docstring 里没有的部分）：
# (a) 新上架商品是待审核、用户搜不到；(b) update 的 price/stock 不传就不改。
_TOOL_NOTES = {
    "merchant_publish_product":
        "上架后商品状态是「待审核」，买家搜不到也买不了；"
        "需要管理员在管理后台审核通过才变成「在售」。"
        "商户自己不能把待审核商品直接上架（会返回「待审核商品需管理员审核通过后才能上架」）。",
    "merchant_update_product":
        "price / stock / on_sale **不传就不改**，传了才改——"
        "MCP 签名里 price<=0、stock<0 也算「不传」。三个都不传会报"
        "「price / stock / on_sale 至少指定一个」。只能改本商户的商品。",
    "merchant_list_products":
        "返回本商户全部商品，待审核 / 在售 / 下架都在里面，"
        "所以能看见「我提交了但还在等审核」的单子。",
    "merchant_list_orders":
        "只返回含本商户商品的订单；status 可选过滤（待支付/待发货/待收货/已完成/售后中）。"
        "买家地址只给到市/区级，拿不到门牌号，也拿不到支付链接。",
    "merchant_fulfill_order":
        "发货只做「待发货 → 待收货」。不碰支付：待支付的订单不能发货；"
        "订单里混了别家商品会整体拒发（跨店拆单不支持）。",
}


def _callable_params(fn) -> str:
    """渲染服务层签名（去掉 actor），让「默认 None = 不改」直接可见。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return ""
    parts = []
    for name, p in sig.parameters.items():
        if name in ("self", "actor"):
            continue
        parts.append(name if p.default is inspect.Parameter.empty
                     else f"{name}={p.default!r}")
    return ", ".join(parts)


def tool_guide() -> list[dict]:
    """给页面用的能力指引：名称 / 签名 / docstring 摘要 / 易踩说明。"""
    out = []
    for tool, method in _TOOL_SPECS:
        fn = getattr(merchant_service, method, None)
        doc = (inspect.getdoc(fn) or "").strip() if fn else ""
        head, _, tail = doc.partition("\n\n")
        out.append({
            "tool": tool,
            "params": _callable_params(fn) if fn else "",
            "summary": head.replace("\n", " "),
            "detail": " ".join(tail.split()),
            "notes": _TOOL_NOTES.get(tool, ""),
        })
    return out


def merchant_counts(merchant: dict) -> tuple[int, int]:
    """本店商品数 / 订单数。

    走 MerchantService 而不是自己写 SQL：服务层里已经按 merchant_id 过滤
    （订单还要按 items 里的 merchant_id 挑），门户复用同一套口径，
    也就不会有机会看到别家商户的数。
    """
    actor = {"role": auth.ROLE_MERCHANT, "actor_id": merchant["merchant_id"],
             "merchant_id": merchant["merchant_id"]}
    try:
        products = [p for p in merchant_service.list_products(actor)
                    if not p.get("error")]
    except Exception:  # noqa: BLE001 - 统计失败不该让整个门户挂掉
        logger.warning("统计商户商品数失败", exc_info=True)
        products = []
    try:
        orders = [o for o in merchant_service.list_orders(actor)
                  if not o.get("error")]
    except Exception:  # noqa: BLE001
        logger.warning("统计商户订单数失败", exc_info=True)
        orders = []
    return len(products), len(orders)


# ---------------------------------------------------------------- 启动
@asynccontextmanager
async def lifespan(_app):
    """建表 + seed（db.init_db 内部有锁和幂等保护，重复调用安全）。"""
    db.init_db()
    yield


app = FastAPI(
    title="AgentMall 商户门户",
    description="AgentMall 商户自助注册与商户工作台（演示数据 + 模拟支付，不构成真实交易）",
    version="0.1.0-mvp",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 路由
@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "agentmall-merchant"}


@app.get("/", response_class=HTMLResponse)
async def portal(request: Request):
    """商户门户首页：MCP 接入点 + api_key 明文 + 店铺信息 + 5 个 tool 的能力指引。"""
    if (g := guard(request)):
        return g
    merchant = current_merchant(request)
    base = mcp_base_url(request)
    cfg = mcp_config(base, merchant["api_key"])
    n_products, n_orders = merchant_counts(merchant)
    msg, level = _flash(request)
    return render(
        request, "merchant_portal.html",
        base=BASE_PATH, merchant=merchant,
        mcp_cfg=cfg,
        mcp_cfg_text=json.dumps(cfg, ensure_ascii=False, indent=2),
        mcp_endpoint=f"{base}{MCP_PATH}",
        n_products=n_products, n_orders=n_orders,
        tools=tool_guide(),
        msg=msg, level=level)


@app.get("/register", response_class=HTMLResponse)
async def register_form(request: Request, error: str = ""):
    if current_merchant(request):
        return RedirectResponse(_u("/"), status_code=303)
    return render(request, "merchant_register.html", base=BASE_PATH,
                  merchant=None, error=error, name="")


@app.post("/register", response_class=HTMLResponse)
async def register_submit(request: Request,
                          name: str = Form(""),
                          password: str = Form(""),
                          confirm: str = Form("")):
    try:
        info = register_merchant(name, password, confirm)
    except RegisterError as exc:
        return render(request, "merchant_register.html", base=BASE_PATH,
                      merchant=None, error=str(exc), name=(name or "").strip(),
                      status_code=400)
    except Exception:  # noqa: BLE001 - 注册异常不该吐裸 JSON
        logger.exception("商户注册失败")
        return render(request, "merchant_register.html", base=BASE_PATH,
                      merchant=None, error="注册失败，请稍后重试",
                      name=(name or "").strip(), status_code=500)
    # 注册成功即登录：直接把新 key 写进 cookie，省得再输一次密码
    resp = RedirectResponse(
        _u(f"/?msg={quote('注册成功，店铺已激活（商品上架仍需管理员审核）')}&level=ok"),
        status_code=303)
    set_login_cookie(resp, info["api_key"])
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str = "", name: str = ""):
    if current_merchant(request):
        return RedirectResponse(_u("/"), status_code=303)
    return render(request, "merchant_login.html", base=BASE_PATH, merchant=None,
                  error=error, name=name or "")


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, name: str = Form(""), password: str = Form("")):
    try:
        info = login_merchant(name, password)
    except RegisterError as exc:
        return render(request, "merchant_login.html", base=BASE_PATH, merchant=None,
                      error=str(exc), name=(name or "").strip(), status_code=401)
    except Exception:  # noqa: BLE001
        logger.exception("商户登录失败")
        return render(request, "merchant_login.html", base=BASE_PATH, merchant=None,
                      error="登录失败，请稍后重试", name=(name or "").strip(),
                      status_code=500)
    resp = RedirectResponse(_u("/"), status_code=303)
    set_login_cookie(resp, info["api_key"])
    return resp


# GET 也放行：路演现场手机端点链接比点按钮方便（demo 取舍，见 app.py 同款做法）
@app.api_route("/logout", methods=["GET", "POST"])
async def logout():
    resp = RedirectResponse(_u("/login"), status_code=303)
    # path 必须和 set_login_cookie 时一致，否则浏览器删不掉这个 cookie
    resp.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return resp


# ---------------------------------------------------------------- 兜底错误页
# 真实用户会输错网址、点到过期链接。别吐 FastAPI 的裸 JSON {"detail":"Not Found"}，
# 看着像坏了而不像站点（同 app.py 的做法，模板换成商户自己的一套）。
@app.exception_handler(404)
async def _not_found(request: Request, exc):
    if request.url.path.startswith(("/api/", "/static/")):
        return JSONResponse({"ok": False, "error": "资源不存在"}, status_code=404)
    try:
        return render(request, "merchant_error_404.html", status_code=404,
                      base=BASE_PATH, merchant=_safe_merchant(request))
    except Exception:  # noqa: BLE001
        return HTMLResponse(
            f'<meta charset="utf-8"><h1>404 · 页面不存在</h1>'
            f'<p><a href="{_u("/")}">回商户门户</a></p>', status_code=404)


@app.exception_handler(500)
async def _server_error(request: Request, exc):
    """别把栈信息甩给用户——演示现场崩了要能圆回来。"""
    try:
        return render(request, "merchant_error_500.html", status_code=500,
                      base=BASE_PATH, merchant=_safe_merchant(request))
    except Exception:  # noqa: BLE001
        return HTMLResponse(
            f'<meta charset="utf-8"><h1>500 · 服务出错了</h1>'
            f'<p><a href="{_u("/")}">回商户门户</a></p>', status_code=500)


def main() -> None:  # pragma: no cover
    import uvicorn

    port = int(os.environ.get("AGENTMALL_MERCHANT_PORT", "8003"))
    print(f"AgentMall 商户门户 → http://127.0.0.1:{port}"
          f"（MCP 接入点指向 :{MCP_PORT}{MCP_PATH}）")
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
