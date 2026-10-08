"""AgentMall MCP Server（Phase 2：用户 / 管理员 / 商户三角色）。

启动（stdio，供 Claude Code / Cursor / workbuddy 等客户端）：
    python -m agentmall.server

per-agent key：把该角色的 key 放进环境变量 `AGENTMALL_API_KEY`，
Server 据此只注册该角色的 tool（鉴权契约见 docs/Phase2任务书.md §2）。
未设置 `AGENTMALL_API_KEY` 时按"用户角色、无鉴权"运行（Phase 1 演示兼容）。

预置 key（仅演示用，见 db.py）：
    用户    uk_demo_user_secret
    商户    mk_demo_m001_secret / mk_demo_m002_secret / mk_demo_m003_secret
    管理员  ak_demo_admin_secret
"""
import os

from mcp.server.fastmcp import FastMCP

from . import auth, db
from .store import get_store


def _services():
    """惰性加载角色服务。

    故意不在模块顶层 import：roles.py 里的商户/管理员逻辑一旦有问题，
    不应该把用户侧 4 个 tool 一起带死（用户闭环优先级最高）。
    """
    try:
        from .roles import admin_service, merchant_service
        return admin_service, merchant_service
    except Exception as exc:
        return None, {"error": f"角色服务未就绪：{exc}"}


mcp = FastMCP("agentmall")
store = get_store()


def _actor() -> dict | None:
    """解析当前调用方角色。

    stdio 模式从 AGENTMALL_API_KEY 环境变量取；
    streamable-http 模式由中间件把请求头的 Bearer key 放进 ContextVar
    （并发下比 env var 安全）。
    """
    key = (auth.get_request_key() or os.environ.get("AGENTMALL_API_KEY", "")).strip()
    if not key:
        return {"role": auth.ROLE_USER, "actor_id": "anonymous", "merchant_id": None}
    return auth.resolve_key(key)


def _role() -> str:
    a = _actor()
    return a["role"] if a else ""


def _enabled(tool: str) -> bool:
    """该角色能否看到/调用这个 tool。"""
    return tool in auth.ROLE_TOOLS.get(_role(), set())


# 多角色模式（streamable-http 单进程服务所有角色）：此时全部注册，
# 隔离由调用时守卫 + 审计兜底。stdio 模式一进程一角色，直接按角色裁剪注册表。
_MULTI_ROLE = os.environ.get("AGENTMALL_MULTI_ROLE", "") == "1"


def _reg(tool: str):
    """按角色决定是否把这个 tool 注册进 MCP 工具表。"""
    def deco(fn):
        if _MULTI_ROLE or tool in auth.ROLE_TOOLS.get(_role(), set()):
            mcp.tool(name=tool)(fn)
        return fn
    return deco


def _guarded(tool: str):
    """写操作前的兜底校验。返回 (actor, None) 或 (None, error_dict)。"""
    actor = _actor()
    if actor is None or tool not in auth.ROLE_TOOLS.get(actor["role"], set()):
        return None, auth.deny_unauthorized(actor, tool)
    return actor, None


# ============================================================ 用户智能体（4 tool）
# 名称保持 Phase 1 不变，向后兼容（Phase2任务书 §2）

@_reg("search_products")
def search_products(keyword: str, category: str = "", max_price: float = 0,
                    limit: int = 10) -> list[dict]:
    """搜索日用商品，按价格升序返回（只返回在售商品）。
    keyword: 关键词，如"纸巾"；category: 纸巾/垃圾袋/清洁/收纳/个护，空=不限；
    max_price: 最高价，0=不限；limit: 最多返回条数。"""
    if not _enabled("search_products"):
        return [auth.deny_unauthorized(_actor(), "search_products")]
    return store.search(keyword=keyword, category=category or None,
                        max_price=max_price or None, limit=limit)


@_reg("get_product")
def get_product(product_id: str) -> dict:
    """查看商品详情。product_id 如 "P001"。"""
    if not _enabled("get_product"):
        return auth.deny_unauthorized(_actor(), "get_product")
    return store.get(product_id)


def _receiver_of(user_id: str) -> tuple[str, str]:
    """从用户档案取收货人姓名 / 电话。档案没填就返回空串，不猜、不编。"""
    if not user_id:
        return "", ""
    try:
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT receiver_name, receiver_phone FROM users WHERE id=?",
                (user_id,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return "", ""
    if row is None:
        return "", ""
    return (row["receiver_name"] or ""), (row["receiver_phone"] or "")


@_reg("create_order")
def create_order(product_id: str, quantity: int, address: str,
                 note: str = "") -> dict:
    """创建订单（辅助支付）。
    校验库存并扣减，返回订单号与收银台链接；
    用户需自行在浏览器点击完成付款，智能体不自动扣款。"""
    actor = _actor()
    if not _enabled("create_order"):
        return auth.deny_unauthorized(actor, "create_order")
    # 订单必须归属到调用它的那个用户（2026-10-07 修）。
    # 之前这里没传 user_id → 智能体建的单 user_id 全是 NULL，网页层只能退而用
    # 「未绑定订单池」兜底，而那个池子对**所有**登录用户可见可付——
    # 实测：另一个真人注册后能看到并付款别人的订单，直接破掉「用户只能看自己单」
    # 这条合规红线。store.create_order 早就接受 user_id 参数，只是没人传。
    uid = ""
    if actor and actor.get("actor_id") not in (None, "", "anonymous"):
        uid = str(actor["actor_id"])
    # Phase A P0：智能体代用户下单时，把用户档案里的收货人姓名/电话带上，
    # 否则订单只有一段地址文本、没有联系人，下单流程走不通。
    # **地址参数仍以调用方传的为准**（智能体可以按用户当次口述临时改地址），
    # 但姓名/电话这类用户不会在对话里重复报的东西，从档案补。
    rname, rphone = _receiver_of(uid)
    return store.create_order(product_id, quantity, address, note, user_id=uid,
                              receiver_name=rname, receiver_phone=rphone)


@_reg("get_order")
def get_order(order_id: str) -> dict:
    """查询订单状态。order_id 如 "AMXXXXXXXXXX"。"""
    if not _enabled("get_order"):
        return auth.deny_unauthorized(_actor(), "get_order")
    return store.get_order(order_id)


# ======================================================= 管理员智能体（5 tool）
# 所有写操作记 audit_log

@_reg("admin_review_product")
def admin_review_product(product_id: str, approve: bool, reason: str = "") -> dict:
    """审核商户上架申请。通过→在售；驳回→reason 必填。"""
    actor, err = _guarded("admin_review_product")
    if err:
        return err
    admin, missing = _services()
    return missing if isinstance(missing, str) else admin.review_product(
        actor, product_id, approve, reason)


@_reg("admin_takedown_product")
def admin_takedown_product(product_id: str, reason: str) -> dict:
    """下架商品。reason 必填，操作记审计日志。"""
    actor, err = _guarded("admin_takedown_product")
    if err:
        return err
    admin, missing = _services()
    return missing if isinstance(missing, str) else admin.takedown_product(
        actor, product_id, reason)


@_reg("admin_list_merchants")
def admin_list_merchants() -> list[dict]:
    """商户列表（id/名称/商品数/订单数）。"""
    actor, err = _guarded("admin_list_merchants")
    if err:
        return [err]
    admin, missing = _services()
    return [missing] if isinstance(missing, str) else admin.list_merchants(actor)


@_reg("admin_stats")
def admin_stats() -> dict:
    """平台看板：订单数/GMV/商户数/商品数/待审核数/用户数。"""
    actor, err = _guarded("admin_stats")
    if err:
        return err
    admin, missing = _services()
    return missing if isinstance(missing, str) else admin.stats(actor)


@_reg("admin_audit_log")
def admin_audit_log(limit: int = 50) -> list[dict]:
    """审计记录（who/when/做了什么），只读。"""
    actor, err = _guarded("admin_audit_log")
    if err:
        return [err]
    admin, missing = _services()
    return [missing] if isinstance(missing, str) else admin.audit_log(actor, limit)


# ========================================================= 商户智能体（5 tool）
# 只能操作自己 merchant_id 下的数据

@_reg("merchant_publish_product")
def merchant_publish_product(name: str, category: str, price: float,
                             stock: int, unit: str = "件",
                             specs: str = "") -> dict:
    """上架商品。提交后状态为"待审核"，管理员审核通过才在售。"""
    actor, err = _guarded("merchant_publish_product")
    if err:
        return err
    _, merch = _services()
    if isinstance(merch, str):
        return merch
    return merch.publish_product(actor, name, category, price, stock, unit, specs)


@_reg("merchant_update_product")
def merchant_update_product(product_id: str, price: float = 0, stock: int = -1,
                            on_sale: bool = None) -> dict:
    """改价/改库存/上下架（仅本商户商品）。
    price<=0 表示不改价；stock<0 表示不改库存；**on_sale 不传表示不改上下架**。

    on_sale 的默认值必须是 None 而不是 True（2026-10-07 审核复现后修）：
    默认 True 会让「商户只想改个价」也被当成「要把它上架」，
    对待审核商品直接被 roles 层拒掉——而商户压根没提过上架这事。
    None 与 False/True 可区分，才能让 roles 层判断「是不是真的在改状态」。
    """
    actor, err = _guarded("merchant_update_product")
    if err:
        return err
    _, merch = _services()
    if isinstance(merch, str):
        return merch
    return merch.update_product(
        actor, product_id,
        price=price if price and price > 0 else None,
        stock=stock if stock is not None and stock >= 0 else None,
        on_sale=on_sale)


@_reg("merchant_list_products")
def merchant_list_products() -> list[dict]:
    """本商户全部商品（含待审核/下架）。"""
    actor, err = _guarded("merchant_list_products")
    if err:
        return [err]
    _, merch = _services()
    return [merch] if isinstance(merch, str) else merch.list_products(actor)


@_reg("merchant_list_orders")
def merchant_list_orders(status: str = "") -> list[dict]:
    """本店铺订单。买家地址已脱敏到市级。status 可选过滤。"""
    actor, err = _guarded("merchant_list_orders")
    if err:
        return [err]
    _, merch = _services()
    return [merch] if isinstance(merch, str) else merch.list_orders(actor, status)


@_reg("merchant_fulfill_order")
def merchant_fulfill_order(order_id: str, tracking_no: str = "") -> dict:
    """发货，订单状态 → 待收货。仅本店铺订单。"""
    actor, err = _guarded("merchant_fulfill_order")
    if err:
        return err
    _, merch = _services()
    if isinstance(merch, str):
        return merch
    return merch.fulfill_order(actor, order_id, tracking_no)


def main() -> None:
    db.init_db()
    mcp.run()


if __name__ == "__main__":
    main()
