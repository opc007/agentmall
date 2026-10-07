"""管理员侧 + 商户侧服务层（issue #2 的 P1 / P2）。

对应 Phase2任务书 §3 的 10 个 tool，方法名去掉 ``admin_`` / ``merchant_`` 前缀：

    AdminService.review_product   ← admin_review_product
    AdminService.takedown_product ← admin_takedown_product
    AdminService.list_merchants   ← admin_list_merchants
    AdminService.stats            ← admin_stats
    AdminService.audit_log        ← admin_audit_log

    MerchantService.publish_product ← merchant_publish_product
    MerchantService.update_product  ← merchant_update_product
    MerchantService.list_products   ← merchant_list_products
    MerchantService.list_orders     ← merchant_list_orders
    MerchantService.fulfill_order   ← merchant_fulfill_order

铁律（docs/角色架构.md + Phase2任务书 §6/§7）：

1. **角色即边界**：每个方法第一件事校验 ``actor["role"]``，不对就
   ``auth.deny_unauthorized()``（内部已记 ``deny:`` 审计，不要重复记）。
2. **商户数据隔离**：商户方法除角色外还要校验 ``merchant_id`` 归属，
   改别人商品/动别人订单一律拒绝并留痕。
3. **上架走审核**：商户新建商品 status=待审核，只有管理员审核通过才在售；
   用户侧 ``Store.search`` 只返回 status=在售，所以待审核商品天然搜不到。
4. **写操作全部留痕**：review / takedown / publish / update / fulfill 都调
   ``db.log_audit()``，且与业务写在同一事务里提交（audit_log 有禁删改触发器，
   不做任何 UPDATE/DELETE）。
5. **checkout hands off**：本模块不碰支付、不改订单的支付状态；
   待支付订单不允许发货，商户侧也拿不到买家的 pay_url。

只读用户侧（``store.py``）不在本文件内，也没有在这里给它加任何写方法。
"""
import json
import re
import secrets
import time
from contextlib import contextmanager

from . import auth, db

# ---- 与 db.py SCHEMA 一致的状态词 ----
STATUS_PENDING = "待审核"
STATUS_ON_SALE = "在售"
STATUS_OFF_SHELF = "下架"
PRODUCT_STATUSES = (STATUS_PENDING, STATUS_ON_SALE, STATUS_OFF_SHELF)

ORDER_PENDING_PAY = "待支付"
ORDER_TO_SHIP = "待发货"
ORDER_TO_RECEIVE = "待收货"
ORDER_DONE = "完成"
ORDER_STATUSES = (ORDER_PENDING_PAY, ORDER_TO_SHIP, ORDER_TO_RECEIVE, ORDER_DONE,
                  "售后中")

# GMV 口径：只算已付款且未回退的订单（待发货 / 待收货 / 完成）。
# 待支付订单可能永远不付款，计入会虚高；售后中说明交易正在回退，暂不计入。
GMV_STATUSES = (ORDER_TO_SHIP, ORDER_TO_RECEIVE, ORDER_DONE)

# tool 名（用于越权拒绝与审计 action），与 auth.ROLE_TOOLS 的键保持一致
TOOL_REVIEW = "admin_review_product"
TOOL_TAKEDOWN = "admin_takedown_product"
TOOL_LIST_MERCHANTS = "admin_list_merchants"
TOOL_STATS = "admin_stats"
TOOL_AUDIT_LOG = "admin_audit_log"
TOOL_PUBLISH = "merchant_publish_product"
TOOL_UPDATE = "merchant_update_product"
TOOL_LIST_PRODUCTS = "merchant_list_products"
TOOL_LIST_ORDERS = "merchant_list_orders"
TOOL_FULFILL = "merchant_fulfill_order"


# ---------------------------------------------------------------- 基础设施

@contextmanager
def _session():
    """短连接 + 事务。正常退出 commit，抛异常 rollback，退出时 close。"""
    db.init_db()
    conn = db.connect()
    try:
        with conn:            # __exit__ 无异常则 commit，有异常则 rollback
            yield conn
    finally:
        conn.close()


def _coerce_float(raw, field: str) -> float:
    """把外部输入转 float，失败/非有限数抛 ValueError（调用方转成友好 error）。"""
    try:
        num = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须是数字") from None
    if num != num or num in (float("inf"), float("-inf")):  # NaN / inf
        raise ValueError(f"{field} 必须是有限数字")
    return num


def _coerce_int(raw, field: str) -> int:
    try:
        num = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必须是数字") from None
    if num != num or num in (float("inf"), float("-inf")) or abs(num - round(num)) > 1e-9:
        raise ValueError(f"{field} 必须是整数")
    return int(round(num))


def _loads_items(raw: str) -> list[dict]:
    """orders.items_json → list[dict]。脏数据不炸，退化成空列表。"""
    try:
        items = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []


def _fmt_ts(ts: int | float | None) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts or 0)))
    except (TypeError, ValueError, OSError):
        return ""


def _as_bool(value) -> bool:
    """宽松布尔解析：MCP/HTTP 传进来的可能是 "false" / "0" 这类字符串。

    审核、下架这类开关一旦被误判成 True，商品就直接上架了，宁可严格一点。
    """
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "none", "否", "驳回")
    return bool(value)


# ------------------------------------------------------------ 买家地址脱敏

# 只按行政区划切：省/市/州/盟/县/区/旗 + 多字后缀。
# 刻意不收 乡/镇/村/路/号 这些细粒度标记 —— 它们常出现在地名内部
# （"西乡塘区"里的"乡"、"南京路"里的"路"），切错段会把地址截得更难看。
_ADMIN_MARKS = ("特别行政区", "自治区", "自治州", "省", "市", "州", "盟", "县", "区", "旗")
# 细粒度标记只在"按行政区划切不出 level 段"时兜底用
# （例："广西南宁朝阳广场"没有"市"字，只能靠细粒度标记把"南宁"切出来）。
_DETAIL_MARKS = _ADMIN_MARKS + (
    "街道", "大厦", "广场", "小区", "单元", "镇", "乡", "村", "组", "苑",
    "园", "街", "路", "号", "栋", "幢", "层", "室", "弄", "巷", "条", "楼",
)


def _mark_re(marks) -> re.Pattern:
    """多字后缀排前面，避免 "特别行政区" 被单字 "区" 先吃掉。"""
    return re.compile("(" + "|".join(
        sorted((re.escape(m) for m in marks), key=len, reverse=True)) + ")")


_ADMIN_RE = _mark_re(_ADMIN_MARKS)
_DETAIL_RE = _mark_re(_DETAIL_MARKS)
# 地址里常见的分隔符，脱敏前先压掉（"广西 南宁市 朝阳广场" / "广西,南宁市,朝阳广场"）
_SEP_RE = re.compile(r"[\s,，;；、|/\\\-—_·]+")
# 行政区划名主体至少要有这么多字，否则不把标记当切点（见 _split_by）
_MIN_UNIT_BODY = 2


def _split_by(text: str, pattern: re.Pattern) -> list[str]:
    """按标记切段，标记留在前一段尾部：广东省广州市… → ['广东省','广州市',…]。

    切点要求标记前面至少有 ``_MIN_UNIT_BODY`` 个字的地名主体，否则一律不切 ——
    "广州市"里的"州"、"江苏"里的"市"是**市名的一部分**，不是行政区划后缀。
    """
    parts: list[str] = []
    pos = 0
    for m in pattern.finditer(text):
        if m.start() - pos >= _MIN_UNIT_BODY:   # 标记前面要有足够的地名主体
            parts.append(text[pos:m.end()])     # 标记留在前一段尾部
            pos = m.end()
    if pos < len(text):
        parts.append(text[pos:])
    return parts or [text]


def _admin_ended(part: str) -> bool:
    return any(part.endswith(mark) for mark in _ADMIN_MARKS)


# 直辖市 / 特别行政区：市名本身就是一级行政区，再带一个"区"才算区级
_MUNICIPALITIES = ("北京", "上海", "天津", "重庆", "香港", "澳门")
# 市级标记：切在第一个市级标记之后 = 正好把区县/街道/门牌都甩掉
_CITY_MARKS = ("自治州", "市", "州", "盟")


def mask_address(address: str, level: int = 2) -> str:
    """买家收货地址脱敏（Phase2任务书 §7 合规红线：商户只看得到区级）。

        广西南宁市朝阳广场              → 广西南宁市
        广西南宁市西乡塘区大学东路100号   → 广西南宁市
        广东省广州市天河区体育西路138号  → 广东省广州市
        北京市朝阳区望京街道xx路1号     → 北京市朝阳区   （直辖市给到区级）
        广西 南宁市,朝阳广场             → 广西南宁市   （分隔符无关）

    规则：**切在第一个市级标记（市/州/盟/自治州）之后**，正好把区县、街道、
    门牌全甩掉；直辖市例外，"北京市"本身是一级行政区，再带上一个"区"才算区级。

    不依赖空格/逗号等分隔符（整串挤在一起照样能切），也不依赖"省"字
    （"广西南宁市"这种省名不带"省"的写法很常见）。一个行政区划标记都没有的
    手写地址（"广西南宁朝阳广场"）只能按细粒度标记剥，剥不动就原样返回 ——
    这种串里本来也没有区县/门牌可泄露。
    """
    text = _SEP_RE.sub("", address or "")
    if not text:
        return ""
    level = max(1, int(level or 1))
    parts = _split_by(text, _ADMIN_RE)
    city_idx = next((i for i, p in enumerate(parts)
                     if any(p.endswith(m) for m in _CITY_MARKS)), None)
    if city_idx is None:
        # 没有"市/州/盟"字：港澳台（省/特别行政区级）或"广西南宁朝阳广场"这种
        # 手写地址。首段若是省级就到此为止，否则退回细粒度标记切前 level 段。
        if _admin_ended(parts[0]):
            keep = 1 + (1 if text.startswith(_MUNICIPALITIES) and len(parts) > 1 else 0)
        else:
            parts = _split_by(text, _DETAIL_RE)
            keep = level
    else:
        keep = city_idx + 1
        if city_idx == 0 and text.startswith(_MUNICIPALITIES) and len(parts) > 1:
            keep += 1  # 直辖市：市 + 区 才算区级
    return "".join(parts[:keep])


# -------------------------------------------------------------- 管理员侧

class AdminService:
    """管理员智能体服务层。只有 admin key 能进；所有写操作记审计。"""

    # ---- 商品审核 ----

    def review_product(self, actor: dict, product_id: str, approve: bool,
                       reason: str = "") -> dict:
        """审核上架：通过 → 在售；驳回 → 下架（驳回原因必填，记审计）。"""
        if (actor or {}).get("role") != auth.ROLE_ADMIN:
            return auth.deny_unauthorized(actor, TOOL_REVIEW)
        pid = (product_id or "").strip()
        if not pid:
            return {"error": "product_id 不能为空"}
        reason = (reason or "").strip()
        approve = _as_bool(approve)
        if not approve and not reason:
            # 驳回不给理由，商户根本不知道要改什么 —— 不写库直接打回
            return {"error": "驳回必须填写 reason"}

        target_status = STATUS_ON_SALE if approve else STATUS_OFF_SHELF
        action = "approve" if approve else "reject"
        try:
            with _session() as conn:
                row = conn.execute(
                    "SELECT id,name,status,merchant_id FROM products WHERE id=?",
                    (pid,)).fetchone()
                if row is None:
                    return {"error": f"找不到商品 {pid}"}
                conn.execute("UPDATE products SET status=? WHERE id=?",
                             (target_status, pid))
                db.log_audit(
                    conn, actor["role"], actor["actor_id"], TOOL_REVIEW, pid,
                    json.dumps({"action": action, "from": row["status"],
                                "to": target_status, "name": row["name"],
                                "reason": reason}, ensure_ascii=False))
                prev_status = row["status"]
        except Exception as exc:
            return {"error": f"审核失败：{exc}"}
        return {"ok": True, "product_id": pid, "approved": approve,
                "status": target_status, "previous_status": prev_status,
                "reason": reason}

    def takedown_product(self, actor: dict, product_id: str, reason: str) -> dict:
        """强制下架（处罚 / 合规处置）。reason 必填，记审计。"""
        if (actor or {}).get("role") != auth.ROLE_ADMIN:
            return auth.deny_unauthorized(actor, TOOL_TAKEDOWN)
        pid = (product_id or "").strip()
        if not pid:
            return {"error": "product_id 不能为空"}
        reason = (reason or "").strip()
        if not reason:
            return {"error": "下架必须填写 reason"}
        try:
            with _session() as conn:
                row = conn.execute(
                    "SELECT id,name,status FROM products WHERE id=?", (pid,)).fetchone()
                if row is None:
                    return {"error": f"找不到商品 {pid}"}
                if row["status"] == STATUS_OFF_SHELF:
                    return {"error": f"商品 {pid} 已是下架状态"}
                conn.execute("UPDATE products SET status=? WHERE id=?",
                             (STATUS_OFF_SHELF, pid))
                db.log_audit(
                    conn, actor["role"], actor["actor_id"], TOOL_TAKEDOWN, pid,
                    json.dumps({"from": row["status"], "to": STATUS_OFF_SHELF,
                                "name": row["name"], "reason": reason},
                               ensure_ascii=False))
                prev_status = row["status"]
        except Exception as exc:
            return {"error": f"下架失败：{exc}"}
        return {"ok": True, "product_id": pid, "status": STATUS_OFF_SHELF,
                "previous_status": prev_status, "reason": reason}

    # ---- 平台看板 ----

    def list_merchants(self, actor: dict) -> list[dict]:
        """商户列表：id / 名称 / 商品数 / 订单数（含本商户商品的订单）。"""
        if (actor or {}).get("role") != auth.ROLE_ADMIN:
            return [auth.deny_unauthorized(actor, TOOL_LIST_MERCHANTS)]
        try:
            with _session() as conn:
                merchants = conn.execute(
                    "SELECT id,name FROM merchants ORDER BY id").fetchall()
                counts: dict[str, int] = {}
                for row in conn.execute(
                        "SELECT merchant_id,COUNT(*) c FROM products "
                        "GROUP BY merchant_id"):
                    counts[row["merchant_id"]] = row["c"]
                # 订单按商户统计要解 items_json（一个订单可能含多个商户的商品）
                order_counts: dict[str, int] = {}
                for row in conn.execute("SELECT items_json FROM orders"):
                    for mid in {str(it.get("merchant_id") or "")
                                for it in _loads_items(row["items_json"])}:
                        if mid:
                            order_counts[mid] = order_counts.get(mid, 0) + 1
                return [{"id": r["id"], "name": r["name"],
                         "product_count": counts.get(r["id"], 0),
                         "order_count": order_counts.get(r["id"], 0)}
                        for r in merchants]
        except Exception as exc:
            return [{"error": f"查询商户列表失败：{exc}"}]

    def stats(self, actor: dict) -> dict:
        """平台看板。GMV 口径见函数末尾注释：只算已付款且未回退的订单。"""
        if (actor or {}).get("role") != auth.ROLE_ADMIN:
            return auth.deny_unauthorized(actor, TOOL_STATS)
        try:
            with _session() as conn:
                orders = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
                gmv = conn.execute(
                    "SELECT COALESCE(SUM(total),0) g FROM orders WHERE status IN (%s)"
                    % ",".join("?" * len(GMV_STATUSES)), GMV_STATUSES).fetchone()["g"]
                merchants = conn.execute(
                    "SELECT COUNT(*) c FROM merchants").fetchone()["c"]
                products = conn.execute(
                    "SELECT COUNT(*) c FROM products").fetchone()["c"]
                pending = conn.execute(
                    "SELECT COUNT(*) c FROM products WHERE status=?",
                    (STATUS_PENDING,)).fetchone()["c"]
                users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        except Exception as exc:
            return {"error": f"查询平台数据失败：{exc}"}
        # GMV 口径注释：待支付可能永不付款，售后中说明在回退，都不计入；
        # 待发货/待收货/完成 = 已付款且未回退的成交额。
        return {"orders": int(orders), "gmv": round(float(gmv), 2),
                "merchants": int(merchants), "products": int(products),
                "pending_review": int(pending), "users": int(users)}

    def audit_log(self, actor: dict, limit: int = 50) -> list[dict]:
        """审计日志（只读）。最新在前；audit_log 上有禁删改触发器，本模块只 INSERT。"""
        if (actor or {}).get("role") != auth.ROLE_ADMIN:
            return [auth.deny_unauthorized(actor, TOOL_AUDIT_LOG)]
        try:
            limit_i = _coerce_int(limit, "limit")
        except ValueError as exc:
            return {"error": str(exc)}
        limit_i = max(1, min(limit_i, 500))
        try:
            with _session() as conn:
                rows = conn.execute(
                    "SELECT id,actor_role,actor_id,action,target,detail,created_at "
                    "FROM audit_log ORDER BY id DESC LIMIT ?", (limit_i,)).fetchall()
        except Exception as exc:
            return [{"error": f"查询审计日志失败：{exc}"}]
        return [{"id": r["id"], "actor_role": r["actor_role"],
                 "actor_id": r["actor_id"], "action": r["action"],
                 "target": r["target"], "detail": r["detail"],
                 "created_at": r["created_at"],
                 "created_at_str": _fmt_ts(r["created_at"])} for r in rows]


# ---------------------------------------------------------------- 商户侧

class MerchantService:
    """商户智能体服务层。需要 merchant key，且只能动自己 merchant_id 的数据。"""

    @staticmethod
    def _merchant_id(actor: dict | None, tool: str) -> tuple[str | None, dict | None]:
        """取 actor 的 merchant_id。取不到说明 key 有问题，按越权处理。"""
        mid = (actor or {}).get("merchant_id")
        if not mid:
            return None, auth.deny_unauthorized(actor, tool)
        return str(mid), None

    # ---- 商品 ----

    def publish_product(self, actor: dict, name, category, price, stock,
                        unit, specs) -> dict:
        """提交上架申请。新商品一律 status=待审核，管理员审过才在售。"""
        if (actor or {}).get("role") != auth.ROLE_MERCHANT:
            return auth.deny_unauthorized(actor, TOOL_PUBLISH)
        mid, denied = self._merchant_id(actor, TOOL_PUBLISH)
        if denied:
            return denied
        name = (name or "").strip()
        if not name:
            return {"error": "商品名称不能为空"}
        category = (category or "").strip() or "未分类"
        unit = (unit or "").strip() or "件"
        specs = (specs or "").strip() if isinstance(specs, str) else str(specs or "")
        try:
            price_f = _coerce_float(price, "price")
            stock_i = _coerce_int(stock, "stock")
        except ValueError as exc:
            return {"error": str(exc)}
        if price_f <= 0:
            return {"error": "price 必须大于 0"}
        if stock_i < 0:
            return {"error": "stock 不能为负数"}
        try:
            with _session() as conn:
                # id 不冲突：4 字节随机 + 事务内查重，最多试 5 次
                pid = ""
                for _ in range(5):
                    cand = "P" + secrets.token_hex(4).upper()
                    if not conn.execute("SELECT 1 FROM products WHERE id=?",
                                        (cand,)).fetchone():
                        pid = cand
                        break
                if not pid:
                    return {"error": "生成商品 ID 失败，请重试"}
                # original_price 存原价；没有真实划线价就等于售价，
                # 不编造虚假折扣（合规）。
                conn.execute(
                    "INSERT INTO products(id,merchant_id,name,category,price,"
                    "original_price,stock,unit,specs,image_url,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'',?,?)",
                    (pid, mid, name, category, price_f, price_f, stock_i, unit,
                     specs, STATUS_PENDING, db.now()))
                db.log_audit(
                    conn, actor["role"], actor["actor_id"], TOOL_PUBLISH, pid,
                    json.dumps({"name": name, "category": category,
                                "price": price_f, "stock": stock_i,
                                "status": STATUS_PENDING}, ensure_ascii=False))
        except Exception as exc:
            return {"error": f"提交上架失败：{exc}"}
        return {"product_id": pid, "status": STATUS_PENDING, "merchant_id": mid,
                "name": name, "message": "已提交，等待管理员审核"}

    def update_product(self, actor: dict, product_id, price=None, stock=None,
                       on_sale=None) -> dict:
        """改价 / 改库存 / 自家上下架。只能改本商户商品；待审核商品不能自行上架。"""
        if (actor or {}).get("role") != auth.ROLE_MERCHANT:
            return auth.deny_unauthorized(actor, TOOL_UPDATE)
        mid, denied = self._merchant_id(actor, TOOL_UPDATE)
        if denied:
            return denied
        pid = (product_id or "").strip()
        if not pid:
            return {"error": "product_id 不能为空"}
        if price is None and stock is None and on_sale is None:
            return {"error": "price / stock / on_sale 至少指定一个"}

        sets: list[str] = []
        args: list = []
        changes: dict = {}
        if price is not None:
            try:
                price_f = _coerce_float(price, "price")
            except ValueError as exc:
                return {"error": str(exc)}
            if price_f <= 0:
                return {"error": "price 必须大于 0"}
            sets.append("price=?")
            args.append(price_f)
            changes["price"] = price_f
        if stock is not None:
            try:
                stock_i = _coerce_int(stock, "stock")
            except ValueError as exc:
                return {"error": str(exc)}
            if stock_i < 0:
                return {"error": "stock 不能为负数"}
            sets.append("stock=?")
            args.append(stock_i)
            changes["stock"] = stock_i

        try:
            with _session() as conn:
                row = conn.execute(
                    "SELECT id,name,status,merchant_id,price,stock FROM products "
                    "WHERE id=?", (pid,)).fetchone()
                if row is None:
                    return {"error": f"找不到商品 {pid}"}
                if row["merchant_id"] != mid:
                    # 越权隔离：改别人店的商品 —— 拒绝 + 留痕（deny_unauthorized
                    # 不适用：角色是对的，错的是数据归属，所以这里自己记 deny:）
                    db.log_audit(
                        conn, actor["role"], actor["actor_id"],
                        f"deny:{TOOL_UPDATE}", pid,
                        json.dumps({"error": "只能操作本商户的商品",
                                    "owner": row["merchant_id"],
                                    "actor": mid}, ensure_ascii=False))
                    return {"error": "只能操作本商户的商品", "denied": True}
                if on_sale is not None:
                    want_on_sale = _as_bool(on_sale)
                    if want_on_sale and row["status"] == STATUS_PENDING:
                        # 商户不能绕过管理员审核自行上架（Phase2任务书 §3）
                        return {"error": "待审核商品需管理员审核通过后才能上架"}
                    target = STATUS_ON_SALE if want_on_sale else STATUS_OFF_SHELF
                    if target != row["status"]:
                        sets.append("status=?")
                        args.append(target)
                        changes["status"] = {"from": row["status"], "to": target}
                if not sets:
                    return {"ok": True, "product_id": pid, "status": row["status"],
                            "price": row["price"], "stock": row["stock"],
                            "message": "没有需要修改的字段"}
                args.append(pid)
                conn.execute(f"UPDATE products SET {','.join(sets)} WHERE id=?", args)
                db.log_audit(
                    conn, actor["role"], actor["actor_id"], TOOL_UPDATE, pid,
                    json.dumps({"changes": changes}, ensure_ascii=False))
                after = conn.execute(
                    "SELECT status,price,stock FROM products WHERE id=?",
                    (pid,)).fetchone()
        except Exception as exc:
            return {"error": f"更新商品失败：{exc}"}
        return {"ok": True, "product_id": pid, "status": after["status"],
                "price": after["price"], "stock": after["stock"],
                "changes": changes}

    def list_products(self, actor: dict) -> list[dict]:
        """本商户全部商品（含待审核 / 下架）。"""
        if (actor or {}).get("role") != auth.ROLE_MERCHANT:
            return [auth.deny_unauthorized(actor, TOOL_LIST_PRODUCTS)]
        mid, denied = self._merchant_id(actor, TOOL_LIST_PRODUCTS)
        if denied:
            return [denied]
        try:
            with _session() as conn:
                rows = conn.execute(
                    "SELECT * FROM products WHERE merchant_id=? "
                    "ORDER BY created_at DESC, id DESC", (mid,)).fetchall()
        except Exception as exc:
            return [{"error": f"查询商品失败：{exc}"}]
        # 补 product_id 别名：publish_product 返回的是 product_id，
        # 列表里只有 id 的话调用方拼不出来（与订单 order_id 同样的处理）
        out = []
        for r in rows:
            d = dict(r)
            d["product_id"] = d.get("id", "")
            out.append(d)
        return out

    # ---- 订单 ----

    def list_orders(self, actor: dict, status: str = "") -> list[dict]:
        """本店铺订单。买家地址只给到市/区级（合规红线）；不回传 pay_url。"""
        if (actor or {}).get("role") != auth.ROLE_MERCHANT:
            return [auth.deny_unauthorized(actor, TOOL_LIST_ORDERS)]
        mid, denied = self._merchant_id(actor, TOOL_LIST_ORDERS)
        if denied:
            return [denied]
        status = (status or "").strip()
        if status and status not in ORDER_STATUSES:
            return [{"error": f"未知订单状态 {status}，可选：{'/'.join(ORDER_STATUSES)}"}]
        try:
            with _session() as conn:
                rows = conn.execute(
                    "SELECT id,user_id,items_json,total,address,note,status,"
                    "is_demo,tracking_no,created_at FROM orders "
                    "ORDER BY created_at DESC, id DESC").fetchall()
        except Exception as exc:
            return [{"error": f"查询订单失败：{exc}"}]
        out: list[dict] = []
        for r in rows:
            items = _loads_items(r["items_json"])
            mine = [it for it in items if str(it.get("merchant_id") or "") == mid]
            if not mine:
                continue
            if status and r["status"] != status:
                continue
            out.append({
                "order_id": r["id"],
                "user_id": r["user_id"],
                "items": mine,
                "total": round(float(r["total"]), 2),
                # 脱敏：商户不该拿到买家精确到门牌的地址
                "address": mask_address(r["address"]),
                "address_masked": True,
                "note": r["note"],
                "status": r["status"],
                "tracking_no": r["tracking_no"],
                "is_demo": bool(r["is_demo"]),
                "created_at": r["created_at"],
            })
        return out

    def fulfill_order(self, actor: dict, order_id, tracking_no="") -> dict:
        """发货：待发货 → 待收货。只发本商户的订单；不碰支付，不改待支付订单。"""
        if (actor or {}).get("role") != auth.ROLE_MERCHANT:
            return auth.deny_unauthorized(actor, TOOL_FULFILL)
        mid, denied = self._merchant_id(actor, TOOL_FULFILL)
        if denied:
            return denied
        oid = (order_id or "").strip()
        if not oid:
            return {"error": "order_id 不能为空"}
        tracking_no = (tracking_no or "").strip()
        try:
            with _session() as conn:
                row = conn.execute(
                    "SELECT id,items_json,status,address FROM orders WHERE id=?",
                    (oid,)).fetchone()
                if row is None:
                    return {"error": f"找不到订单 {oid}"}
                items = _loads_items(row["items_json"])
                others = sorted({str(it.get("merchant_id") or "")
                                 for it in items
                                 if str(it.get("merchant_id") or "") != mid} - {""})
                if not any(str(it.get("merchant_id") or "") == mid for it in items):
                    db.log_audit(
                        conn, actor["role"], actor["actor_id"], f"deny:{TOOL_FULFILL}",
                        oid, json.dumps({"error": "只能操作本商户的订单",
                                         "actor": mid}, ensure_ascii=False))
                    return {"error": "只能操作本商户的订单", "denied": True}
                if others:
                    # 一个订单里混了别家商品，整单改状态会动到别人的数据。
                    # 跨店拆单是 Phase 3 的活，这里宁可不发。
                    db.log_audit(
                        conn, actor["role"], actor["actor_id"], f"deny:{TOOL_FULFILL}",
                        oid, json.dumps({"error": "订单含其他商户商品，暂不支持跨店发货",
                                         "others": others}, ensure_ascii=False))
                    return {"error": "订单含其他商户商品，暂不支持跨店发货",
                            "denied": True}
                if row["status"] == ORDER_PENDING_PAY:
                    # 不代买家付款，不改支付状态（checkout hands off）
                    return {"error": "订单未支付，不能发货"}
                if row["status"] == ORDER_TO_RECEIVE:
                    return {"error": "订单已发货"}
                if row["status"] == ORDER_DONE:
                    return {"error": "订单已完成"}
                if row["status"] != ORDER_TO_SHIP:
                    return {"error": f"订单状态 {row['status']} 不允许发货"}

                conn.execute(
                    "UPDATE orders SET status=?,tracking_no=? WHERE id=?",
                    (ORDER_TO_RECEIVE, tracking_no or row["tracking_no"] or "", oid))
                db.log_audit(
                    conn, actor["role"], actor["actor_id"], TOOL_FULFILL, oid,
                    json.dumps({"from": row["status"], "to": ORDER_TO_RECEIVE,
                                "tracking_no": tracking_no,
                                "address_masked": mask_address(row["address"])},
                               ensure_ascii=False))
        except Exception as exc:
            return {"error": f"发货失败：{exc}"}
        return {"ok": True, "order_id": oid, "status": ORDER_TO_RECEIVE,
                "tracking_no": tracking_no}


# 模块级单例（server.py / http_server.py 按需 import）
admin_service = AdminService()
merchant_service = MerchantService()
