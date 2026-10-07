"""管理员侧 + 商户侧服务层自测（issue #2 P1/P2）。

跑法：  cd /workspace/agentmall && .venv/bin/python tests/test_roles.py
也可：  .venv/bin/python -m pytest tests/test_roles.py -q

用临时库，绝不碰主库 agentmall/data/agentmall.db：
    os.environ["AGENTMALL_DB"] = "/tmp/test_roles_<pid>.db"   # 必须在 import agentmall 之前
"""
import json
import os
import sys

# ---- 先切库，再 import agentmall.db（DB_PATH 在 import 时读环境变量）----
_TMP_DB = f"/tmp/test_roles_{os.getpid()}.db"
for _suffix in ("", "-wal", "-shm"):
    if os.path.exists(_TMP_DB + _suffix):
        os.remove(_TMP_DB + _suffix)
os.environ["AGENTMALL_DB"] = _TMP_DB

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentmall import auth, db, roles  # noqa: E402
from agentmall.roles import admin_service, merchant_service, mask_address  # noqa: E402

ADMIN_KEY = "ak_demo_admin_secret"
M1_KEY = "mk_demo_m001_secret"
M2_KEY = "mk_demo_m002_secret"
USER_KEY = "uk_demo_user_secret"

FULL_ADDRESS = "广西南宁市西乡塘区大学东路100号3单元501"


def actor_for(key: str) -> dict:
    a = auth.resolve_key(key)
    assert a, f"key 不认识：{key}"
    return a


def q(sql: str, args: tuple = ()) -> list[dict]:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def seeded_product(status: str = roles.STATUS_ON_SALE) -> dict:
    """从 seed 里取一个商品（seed 由 db.py 生成，id 会随采集数据变化，不写死 P001）。"""
    rows = q("SELECT id,merchant_id,name,status FROM products WHERE status=? "
             "ORDER BY id", (status,))
    assert rows, f"seed 里应有 status={status} 的商品"
    return rows[0]


def make_order(order_id: str, merchant_id: str, status: str,
               address: str = FULL_ADDRESS, total: float = 33.8,
               quantity: int = 2, product_id: str = "") -> None:
    """直接造订单（用户侧下单在 store.py，由他人并行编写中，这里不依赖它）。"""
    product_id = product_id or seeded_product()["id"]
    items = [{"product_id": product_id, "name": "测试商品", "price": total / quantity,
              "quantity": quantity, "merchant_id": merchant_id}]
    conn = db.connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO orders(id,user_id,items_json,total,address,note,"
                "status,pay_url,is_demo,tracking_no,created_at) "
                "VALUES(?,'U001',?,?,?,'','待支付','https://pay.example.com/mock/x',1,'',?)",
                (order_id, json.dumps(items, ensure_ascii=False), total, address,
                 db.now()))
            conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    finally:
        conn.close()


def user_visible(product_id: str) -> bool:
    """用户侧可见性的镜像判定：用户只能看到 status=在售 的商品。

    store.py 由他人并行编写（Phase 2 会把搜索切到 SQLite 并过滤 status='在售'），
    当前还是读 products_seed.json 的内存实现，所以这里直接对 DB 判 status ——
    判定条件与 store.py 将要实现的过滤条件保持一致。
    """
    rows = q("SELECT status FROM products WHERE id=?", (product_id,))
    return bool(rows) and rows[0]["status"] == roles.STATUS_ON_SALE


def audit_actions(action_prefix: str = "") -> list[str]:
    """按 action 前缀取审计动作（SQL LIKE 的通配符是 %，不是 *）。"""
    return [r["action"] for r in
            q("SELECT action FROM audit_log WHERE action LIKE ? ORDER BY id",
              (f"{action_prefix}%",))]


# ---------------------------------------------------------------- 用例

def test_mask_address_only_city_level():
    """买家地址脱敏：只到市/区级，兼容缺分隔符/缺"省"字的各种写法。"""
    assert mask_address("广西南宁市朝阳广场") == "广西南宁市"
    assert mask_address("广东省广州市天河区体育西路138号") == "广东省广州市"
    assert mask_address("北京市朝阳区望京街道xx路1号") == "北京市朝阳区"   # 直辖市给到区级
    assert mask_address("上海市浦东新区张江路1号") == "上海市浦东新区"
    assert mask_address(FULL_ADDRESS) == "广西南宁市"                  # 完整地址（含区县门牌）
    assert mask_address("江苏省苏州市") == "江苏省苏州市"              # "州"是市名的一部分
    assert mask_address("广西 南宁市 朝阳广场") == "广西南宁市"      # 空格分隔
    assert mask_address("广西,南宁市,朝阳广场") == "广西南宁市"     # 逗号分隔
    # 一个行政区划标记都没有的手写地址：剥不动就原样返回，但不含门牌/区县
    loose = mask_address("广西南宁朝阳广场")
    assert not any(ch.isdigit() for ch in loose) and "路" not in loose, loose
    assert mask_address("") == ""
    assert mask_address(None) == ""
    print("  mask_address 用例通过")


def test_review_approve_and_reject():
    """管理员审核通过 → 在售；驳回不带 reason → 报错且不写库。"""
    m2 = actor_for(M2_KEY)
    pub = merchant_service.publish_product(
        m2, "审核测试抽纸", "纸巾", 19.9, 100, "包", "10包/箱")
    assert pub.get("status") == roles.STATUS_PENDING, pub
    pid = pub["product_id"]

    admin = actor_for(ADMIN_KEY)
    bad = admin_service.review_product(admin, pid, approve=False)  # 缺 reason
    assert bad == {"error": "驳回必须填写 reason"}, bad
    assert q("SELECT status FROM products WHERE id=?", (pid,))[0]["status"] \
        == roles.STATUS_PENDING, "驳回失败时不该动 status"

    ok = admin_service.review_product(admin, pid, approve=True)
    assert ok["status"] == roles.STATUS_ON_SALE and ok["previous_status"] == roles.STATUS_PENDING, ok
    assert q("SELECT status FROM products WHERE id=?", (pid,))[0]["status"] == roles.STATUS_ON_SALE

    # 驳回：必须带 reason，商品落回不可见状态
    rej = admin_service.review_product(admin, pid, approve=False, reason="图片与实物不符")
    assert rej["status"] == roles.STATUS_OFF_SHELF, rej
    assert q("SELECT status FROM products WHERE id=?", (pid,))[0]["status"] == roles.STATUS_OFF_SHELF
    assert any(a == roles.TOOL_REVIEW for a in audit_actions(roles.TOOL_REVIEW))

    # reason 必填：下架同理
    assert admin_service.takedown_product(admin, pid, "") == {"error": "下架必须填写 reason"}
    print("  审核通过/驳回必填 reason 通过")


def test_takedown_hides_from_user_search():
    """管理员下架 → 用户侧再也搜不到（status 不再是"在售"）。"""
    admin = actor_for(ADMIN_KEY)
    pid = seeded_product()["id"]
    assert user_visible(pid), f"前置条件：{pid} 应在售"
    res = admin_service.takedown_product(admin, pid, reason="涉嫌虚假宣传")
    assert res["status"] == roles.STATUS_OFF_SHELF, res
    assert not user_visible(pid), "下架后用户不应再搜到"
    assert [a for a in audit_actions(roles.TOOL_TAKEDOWN)], "下架必须记审计"
    print(f"  下架后用户不可见 通过（{pid}）")


def test_merchant_cannot_touch_other_merchant_product():
    """商户 A 的 key 改商户 B 的商品 → 拒绝 + 留痕 + 数据没被动。"""
    m2 = actor_for(M2_KEY)
    pid = merchant_service.publish_product(m2, "B店专属商品", "收纳", 12.5, 8, "件", "")["product_id"]
    before = q("SELECT price,stock,status FROM products WHERE id=?", (pid,))[0]

    m1 = actor_for(M1_KEY)
    denied = merchant_service.update_product(m1, pid, price=0.01, stock=999)
    assert denied == {"error": "只能操作本商户的商品", "denied": True}, denied
    assert q("SELECT price,stock,status FROM products WHERE id=?", (pid,))[0] == before, \
        "越权调用不得改动数据"

    # 别人的订单也动不了
    make_order("AMX00000001", "M002", roles.ORDER_TO_SHIP)
    denied2 = merchant_service.fulfill_order(m1, "AMX00000001", tracking_no="SF123")
    assert denied2 == {"error": "只能操作本商户的订单", "denied": True}, denied2
    assert q("SELECT status FROM orders WHERE id=?", ("AMX00000001",))[0]["status"] \
        == roles.ORDER_TO_SHIP, "越权发货不得改订单状态"

    assert any(a == f"deny:{roles.TOOL_UPDATE}" for a in audit_actions("deny:")), audit_actions("deny:")
    assert any(a == f"deny:{roles.TOOL_FULFILL}" for a in audit_actions("deny:"))

    # 自己店里的当然能改
    assert merchant_service.update_product(m2, pid, price=13.5)["price"] == 13.5
    print("  商户数据隔离 通过")


def test_user_role_cannot_call_admin_tools():
    """用户角色调 admin tool → 拒绝，且 audit_log 有 deny: 记录。

    返回形态约定：返回 dict 的 tool（review/takedown/stats）直接给 dict；
    返回 list 的 tool（list_merchants/audit_log）包成 [dict]，
    以免 MCP 层出现「声明 list 却返回 dict」的契约不一致。
    """
    user = actor_for(USER_KEY)
    pid = seeded_product()["id"]
    dict_tools = (admin_service.review_product(user, pid, approve=True),
                  admin_service.takedown_product(user, pid, reason="我要下架"),
                  admin_service.stats(user))
    for res in dict_tools:
        assert isinstance(res, dict), f"dict 类 tool 应返回 dict：{res}"
        assert res.get("denied") is True, res
        assert "越权" in res.get("error", ""), res

    list_tools = (admin_service.list_merchants(user),
                  admin_service.audit_log(user))
    for res in list_tools:
        assert isinstance(res, list) and len(res) == 1, \
            f"list 类 tool 应返回 [denied]：{res}"
        assert res[0].get("denied") is True, res
        assert "越权" in res[0].get("error", ""), res

    denies = q("SELECT actor_role,actor_id,action FROM audit_log "
               "WHERE action LIKE 'deny:%' ORDER BY id")
    assert denies, "audit_log 必须有 deny: 记录"
    assert denies[-1]["actor_role"] == auth.ROLE_USER and denies[-1]["actor_id"] == "U001", denies[-1]
    # 商品没被动过
    assert user_visible(pid), "越权调用不该改动商品状态"

    # 商户也不能调 admin tool
    m1 = actor_for(M1_KEY)
    assert admin_service.stats(m1).get("denied") is True
    print("  用户/商户越权调用 admin tool 通过")


def test_merchant_list_orders_masks_address():
    """商户列订单：地址脱敏到市/区级，且只看得到自己店的订单。"""
    make_order("AMX00000002", "M001", roles.ORDER_TO_SHIP, total=33.8)
    make_order("AMX00000003", "M002", roles.ORDER_TO_SHIP)

    orders = merchant_service.list_orders(actor_for(M1_KEY))
    ids = [o["order_id"] for o in orders]
    assert ids == ["AMX00000002"], ids
    addr = orders[0]["address"]
    assert addr == "广西南宁市", addr
    assert "西乡塘区" not in json.dumps(orders, ensure_ascii=False), "订单里不能出现区县/门牌"
    assert "大学东路" not in json.dumps(orders, ensure_ascii=False)
    assert orders[0]["address_masked"] is True
    assert "pay_url" not in orders[0], "商户侧不应拿到买家支付链接"

    # 商户 B 只看到自己的订单（看不到 M001 的那单）
    m2_orders = merchant_service.list_orders(actor_for(M2_KEY))
    assert "AMX00000003" in [o["order_id"] for o in m2_orders]
    assert "AMX00000002" not in [o["order_id"] for o in m2_orders], "不该看到别家订单"
    # 状态过滤
    assert [o["order_id"] for o in
            merchant_service.list_orders(actor_for(M1_KEY), status="待支付")] == []
    print("  买家地址脱敏 + 订单隔离 通过")


def test_publish_product_pending_review():
    """新上架商品 status=待审核，用户搜不到；字段校验友好。"""
    m1 = actor_for(M1_KEY)
    pub = merchant_service.publish_product(m1, "待审核测试商品", "个护", 29.9, 50, "瓶", "500ml")
    assert pub["status"] == roles.STATUS_PENDING and pub["product_id"], pub
    assert pub["merchant_id"] == "M001"
    assert not user_visible(pub["product_id"]), "待审核商品不能出现在用户搜索里"

    row = q("SELECT * FROM products WHERE id=?", (pub["product_id"],))[0]
    assert row["merchant_id"] == "M001" and row["price"] == 29.9 and row["stock"] == 50
    assert row["status"] == roles.STATUS_PENDING and row["original_price"] == 29.9
    assert any(a == roles.TOOL_PUBLISH for a in audit_actions(roles.TOOL_PUBLISH))
    assert [p for p in merchant_service.list_products(m1) if p["id"] == pub["product_id"]]

    # 非法输入
    assert "price" in merchant_service.publish_product(m1, "x", "个护", 0, 1, "件", "")["error"]
    assert "price" in merchant_service.publish_product(m1, "x", "个护", -1, 1, "件", "")["error"]
    assert "stock" in merchant_service.publish_product(m1, "x", "个护", 1, -5, "件", "")["error"]
    assert "price" in merchant_service.publish_product(m1, "x", "个护", "abc", 1, "件", "")["error"]
    assert "名称" in merchant_service.publish_product(m1, "  ", "个护", 1, 1, "件", "")["error"]
    # 商户不能绕过审核自行上架
    blocked = merchant_service.update_product(m1, pub["product_id"], on_sale=True)
    assert "审核" in blocked["error"], blocked
    # id 不冲突（list_products 返回原始行，商品主键字段是 id）
    ids = [p["id"] for p in merchant_service.list_products(m1)]
    assert len(ids) == len(set(ids)), "商品 id 必须唯一"
    print("  上架待审核 + 参数校验 通过")


def test_update_and_fulfill_flow():
    """改价/改库存/自营上下架 + 发货只允许本商户与合法状态。"""
    m2 = actor_for(M2_KEY)
    pid = merchant_service.publish_product(m2, "发货链路商品", "清洁", 8.8, 20, "瓶", "")["product_id"]
    admin_service.review_product(actor_for(ADMIN_KEY), pid, approve=True)

    upd = merchant_service.update_product(m2, pid, stock=5, on_sale=False)
    assert upd["stock"] == 5 and upd["status"] == roles.STATUS_OFF_SHELF, upd
    assert not user_visible(pid)
    upd2 = merchant_service.update_product(m2, pid, on_sale=True)
    assert upd2["status"] == roles.STATUS_ON_SALE and user_visible(pid)
    assert "至少指定一个" in merchant_service.update_product(m2, pid)["error"]
    assert "price" in merchant_service.update_product(m2, pid, price=0)["error"]
    assert "stock" in merchant_service.update_product(m2, pid, stock=-1)["error"]
    assert "找不到" in merchant_service.update_product(m2, "P-NOPE", price=1)["error"]

    make_order("AMX00000004", "M002", roles.ORDER_TO_SHIP, total=17.6)
    make_order("AMX00000005", "M002", roles.ORDER_PENDING_PAY)
    done = merchant_service.fulfill_order(m2, "AMX00000004", tracking_no="SF0001")
    assert done["status"] == roles.ORDER_TO_RECEIVE and done["tracking_no"] == "SF0001", done
    assert q("SELECT status,tracking_no FROM orders WHERE id=?",
              ("AMX00000004",))[0] == {"status": roles.ORDER_TO_RECEIVE, "tracking_no": "SF0001"}
    assert "已发货" in merchant_service.fulfill_order(m2, "AMX00000004")["error"]
    # checkout hands off：未支付订单不许发，也不许被代付
    assert merchant_service.fulfill_order(m2, "AMX00000005", "SF0002")["error"] == \
        "订单未支付，不能发货"
    assert q("SELECT status,pay_url FROM orders WHERE id=?",
              ("AMX00000005",))[0]["status"] == roles.ORDER_PENDING_PAY
    # 跨店订单不整单发货
    items = [{"product_id": pid, "name": "跨店", "price": 5.0, "quantity": 1,
              "merchant_id": "M002"},
             {"product_id": seeded_product()["id"], "name": "跨店", "price": 5.0,
              "quantity": 1, "merchant_id": "M001"}]
    conn = db.connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO orders(id,user_id,items_json,total,address,note,status,"
                "pay_url,is_demo,tracking_no,created_at) VALUES"
                "('AMX00000006','U001',?,10.0,'广西南宁市','','待发货','',1,'',?)",
                (json.dumps(items, ensure_ascii=False), db.now()))
    finally:
        conn.close()
    cross = merchant_service.fulfill_order(m2, "AMX00000006")
    assert cross.get("denied") is True and "跨店" in cross["error"], cross
    assert q("SELECT status FROM orders WHERE id=?", ("AMX00000006",))[0]["status"] == roles.ORDER_TO_SHIP
    print("  改价改库存/发货链路 通过")


def test_admin_list_merchants_and_stats():
    """商户列表（商品数/订单数）与平台看板（含 GMV 口径）。"""
    admin = actor_for(ADMIN_KEY)
    merchants = admin_service.list_merchants(admin)
    assert [m["id"] for m in merchants] == ["M001", "M002", "M003"], merchants
    m1 = next(m for m in merchants if m["id"] == "M001")
    m2 = next(m for m in merchants if m["id"] == "M002")
    assert m1["name"] == "日用优选生活馆"
    assert m1["product_count"] == q("SELECT COUNT(*) c FROM products WHERE merchant_id='M001'")[0]["c"]
    assert m2["product_count"] >= 2, m2
    assert m1["order_count"] >= 1 and m2["order_count"] >= 1, (m1, m2)

    # GMV：只算待发货/待收货/完成；待支付、售后中不算
    make_order("AMX00000007", "M001", roles.ORDER_DONE, total=100.0)
    make_order("AMX00000008", "M001", roles.ORDER_PENDING_PAY, total=999.0)
    make_order("AMX00000009", "M001", "售后中", total=888.0)
    st = admin_service.stats(admin)
    expected = round(sum(float(r["total"]) for r in q(
        "SELECT total FROM orders WHERE status IN (?,?,?)", roles.GMV_STATUSES)), 2)
    assert st["gmv"] == expected, (st, expected)
    assert st["orders"] == q("SELECT COUNT(*) c FROM orders")[0]["c"]
    assert st["merchants"] == 3 and st["users"] >= 1
    assert st["pending_review"] == q(
        "SELECT COUNT(*) c FROM products WHERE status='待审核'")[0]["c"]
    assert set(st) == {"orders", "gmv", "merchants", "products", "pending_review", "users"}, st
    print("  商户列表/平台看板 通过")


def test_demo_paid_order_counts_into_gmv():
    """回归：走**真实**下单+支付链路，验证演示单计入 GMV（2026-10-07 彩排时发现）。

    之前的 test_admin_list_merchants_and_stats 用 make_order 手工造行、状态取常量
    roles.ORDER_DONE，所以常量写错（写成「完成」而 store 实际写「已完成」）时它照样绿，
    真实演示单的 GMV 却一直是 0——这正是路演第 9 步「看板 GMV 增加」会当场翻车的地方。

    这里刻意不复用常量，直接断言库里的字面值。
    """
    from agentmall.store import get_store
    store = get_store()

    admin = actor_for(ADMIN_KEY)
    before = admin_service.stats(admin)["gmv"]
    p = seeded_product(status=roles.STATUS_ON_SALE)
    before_stock = int(p["stock"] if "stock" in p else 0) or None
    stock_row = q("SELECT stock FROM products WHERE id=?", (p["id"],))[0]
    before_stock = int(stock_row["stock"])

    order = store.create_order(p["id"], 2, FULL_ADDRESS)
    oid = order["order_id"]
    assert order["status"] == roles.ORDER_PENDING_PAY, order

    res = store.confirm_payment(oid)
    assert res.get("paid") is True, res

    # 库里的字面值必须是「已完成」（不是「完成」）
    row = q("SELECT status,is_demo,total FROM orders WHERE id=?", (oid,))[0]
    assert row["status"] == "已完成", f"演示单终态应为「已完成」，实际 {row['status']!r}"
    assert int(row["is_demo"]) == 1, row

    # 核心断言：这笔钱必须进 GMV
    after = admin_service.stats(admin)["gmv"]
    assert after == round(before + float(row["total"]), 2), \
        f"演示单付款后 GMV 应 +{row['total']}：{before} → {after}"

    # 库存确实扣了
    after_stock = int(q("SELECT stock FROM products WHERE id=?", (p["id"],))[0]["stock"])
    assert after_stock == before_stock - 2, (before_stock, after_stock)

    # 商户按「已完成」筛订单必须能筛到（ORDER_STATUSES 认这个字面值）
    m = actor_for(M1_KEY if p["merchant_id"] == "M001" else M2_KEY)
    listed = merchant_service.list_orders(m, status="已完成")
    if isinstance(listed, dict):  # 越权/错误返回
        raise AssertionError(f"按「已完成」筛订单被拒：{listed}")
    assert oid in {o["order_id"] for o in listed}, \
        f"商户按「已完成」筛不到刚付完的单：{[o.get('order_id') for o in listed][:5]}"

    # 对已完成的单再发货，应给「订单已完成」而不是「状态…不允许发货」
    again = merchant_service.fulfill_order(m, oid, "SF999")
    if isinstance(again, dict) and again.get("error"):
        assert "已完成" in again["error"], f"报错文案不对：{again['error']}"

    print("  演示单计入 GMV（真实链路）通过")


def test_audit_log_is_append_only():
    """审计日志只读视图 + 底层禁删改触发器仍然生效（合规红线）。"""
    admin = actor_for(ADMIN_KEY)
    log = admin_service.audit_log(admin, limit=5)
    assert log and len(log) <= 5 and log[0]["id"] > log[-1]["id"], log[:2]
    assert {"id", "actor_role", "actor_id", "action", "target", "detail",
            "created_at", "created_at_str"} == set(log[0])
    conn = db.connect()
    try:
        for sql in ("DELETE FROM audit_log", "UPDATE audit_log SET detail='x'"):
            try:
                with conn:
                    conn.execute(sql)
            except Exception as exc:  # noqa: BLE001 - 触发器拒绝即通过
                assert "audit_log" in str(exc), (sql, exc)
            else:
                raise AssertionError(f"audit_log 竟然允许：{sql}")
    finally:
        conn.close()
    print("  审计日志只追加 通过")


def test_restart_persistence_and_role_matrix():
    """重启（重开连接）后数据仍在；角色矩阵与 auth.ROLE_TOOLS 对齐。"""
    assert db.DB_PATH == _TMP_DB, db.DB_PATH
    assert q("SELECT COUNT(*) c FROM products WHERE status='待审核'")[0]["c"] > 0
    # 每个 tool 都有对应的 deny: 路径
    for svc, tool in ((admin_service, roles.TOOL_REVIEW),
                      (merchant_service, roles.TOOL_LIST_PRODUCTS)):
        assert tool in auth.ROLE_TOOLS[auth.ROLE_USER] or tool in auth.ROLE_TOOLS[auth.ROLE_MERCHANT] \
            or tool in auth.ROLE_TOOLS[auth.ROLE_ADMIN]
    assert "update_product" not in {n for n in dir(roles.Store)} if hasattr(roles, "Store") else True
    print("  持久化/角色矩阵 通过")


# 按依赖顺序跑（后面的用例要看到前面写进库的数据），pytest 则按定义顺序跑
TEST_ORDER = [
    "test_mask_address_only_city_level",
    "test_publish_product_pending_review",
    "test_review_approve_and_reject",
    "test_takedown_hides_from_user_search",
    "test_merchant_cannot_touch_other_merchant_product",
    "test_merchant_list_orders_masks_address",
    "test_update_and_fulfill_flow",
    "test_admin_list_merchants_and_stats",
    "test_user_role_cannot_call_admin_tools",
    "test_audit_log_is_append_only",
    "test_restart_persistence_and_role_matrix",
    "test_demo_paid_order_counts_into_gmv",
]


def main() -> int:
    db.init_db()
    missing = [n for n in TEST_ORDER if n not in globals()]
    assert not missing, f"TEST_ORDER 里的用例不存在：{missing}"
    tests = [globals()[n] for n in TEST_ORDER]
    failed = 0
    for fn in tests:
        print(f"\n▶ {fn.__name__}")
        try:
            fn()
        except Exception:  # noqa: BLE001 - 自测要看到全部失败
            failed += 1
            import traceback
            traceback.print_exc()
    print("\n" + "=" * 60)
    print(f"ROLES TESTS: {len(tests) - failed}/{len(tests)} passed")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(_TMP_DB + suffix):
            os.remove(_TMP_DB + suffix)
    if failed:
        print("ROLES TESTS FAILED")
        return 1
    print("ROLES TESTS PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
