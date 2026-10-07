"""商品搜索 + 订单存储（SQLite 持久化，Phase 2）。

Phase 1 是内存 dict，重启丢订单；Phase 2 落到 SQLite，订单/库存/审计重启不丢。

只读边界（tests/test_readonly.py 守卫）：**用户侧只允许读 + 建单**。
下单会扣减库存（防超卖），但绝不改价格、绝不上下架、绝不删改商品。
改价/改库存/审核类写操作全部在 `roles.py` 的 MerchantService / AdminService 里，
**故意不放在本类上**，避免用户侧 tool 被误调。

1688 对接（Phase 3）：实现下方 `Alibaba1688Source`，走 `ProductSource` 适配器
把 1688 商品同步进 SQLite，上层 tool 不用改。
"""
import json
import os
import secrets
import uuid
from contextlib import closing

from . import db, payment

BASE = os.path.dirname(os.path.abspath(__file__))

PRODUCT_FIELDS = ("id", "merchant_id", "name", "category", "price",
                  "original_price", "stock", "unit", "specs", "image_url",
                  "status", "created_at")


# ---------------------------------------------------------------- 商品来源适配器
class ProductSource:
    """商品来源适配器。MVP/Phase 2 用本地 SQLite；Phase 3 实现 1688 版。"""

    def list_all(self) -> list:
        raise NotImplementedError

    def sync_into(self) -> int:  # pragma: no cover - 抽象
        """把源数据同步进 SQLite，返回同步条数。"""
        raise NotImplementedError


class LocalJsonSource(ProductSource):
    """从 data/products_real.json（或 products_seed.json）初始化商品库。"""

    def __init__(self, path: str = "") -> None:
        self.path = path or os.path.join(BASE, "data", "products_real.json")
        if not os.path.exists(self.path):
            self.path = os.path.join(BASE, "data", "products_seed.json")

    def list_all(self) -> list:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("products", []) if isinstance(data, dict) else data


class Alibaba1688Source(ProductSource):
    """TODO(Phase 3)：对接 1688 开放平台，同步商品/库存/价格。
    选型依据见 docs/货源接口选型.md。实现时只需实现 list_all + sync_into。"""


# -------------------------------------------------------------------- Store
class Store:
    def __init__(self, source: ProductSource | None = None) -> None:
        self.source = source or LocalJsonSource()
        db.init_db()

    # ---- 商品 ----
    def search(self, keyword: str = "", category: str | None = None,
               max_price: float | None = None, limit: int = 10,
               only_on_sale: bool = True) -> list:
        """关键词模糊匹配 name/category，按价格升序。

        only_on_sale=True 时只返回在售商品（待审核/下架对用户不可见）。
        """
        kw = (keyword or "").strip()
        sql = ["SELECT * FROM products WHERE 1=1"]
        args: list = []
        if only_on_sale:
            sql.append("AND status = '在售'")
        if kw:
            sql.append("AND (name LIKE ? OR category LIKE ?)")
            args += [f"%{kw}%", f"%{kw}%"]
        if category:
            sql.append("AND category = ?")
            args.append(category)
        if max_price:
            sql.append("AND price <= ?")
            args.append(float(max_price))
        sql.append("ORDER BY price ASC LIMIT ?")
        args.append(max(int(limit), 1))
        with closing(db.connect()) as conn:
            rows = conn.execute(" ".join(sql), args).fetchall()
        return [dict(r) for r in rows]

    def get(self, product_id: str) -> dict:
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT * FROM products WHERE id=?",
                               ((product_id or "").strip(),)).fetchone()
        if row is None:
            return {"error": f"找不到商品 {product_id}"}
        return dict(row)

    # ---- 订单（辅助支付：只建单，不扣款） ----
    def create_order(self, product_id: str, quantity: int,
                     address: str, note: str = "", user_id: str = "") -> dict:
        """建单并扣库存。库存不足返回 error（防超卖）。"""
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            return {"error": "数量必须是整数"}
        if quantity < 1:
            return {"error": "数量必须 >= 1"}
        if not (address or "").strip():
            return {"error": "收货地址不能为空"}

        db.init_db()
        conn = db.connect()
        try:
            row = conn.execute("SELECT * FROM products WHERE id=?",
                               ((product_id or "").strip(),)).fetchone()
            if row is None:
                return {"error": f"找不到商品 {product_id}"}
            p = dict(row)
            if p["status"] != "在售":
                return {"error": f"商品 {product_id} 当前不可购买（{p['status']}）"}
            if quantity > p["stock"]:
                return {"error": "库存不足",
                        "available": p["stock"], "requested": quantity}

            order_id = "AM" + uuid.uuid4().hex[:10].upper()
            total = round(p["price"] * quantity, 2)
            charge = payment.gateway.create_payment(
                order_id, total, subject=f"{p['name']} x{quantity}")
            items = [{
                "product_id": p["id"],
                "merchant_id": p["merchant_id"],
                "name": p["name"],
                "price": p["price"],
                "quantity": quantity,
            }]
            ts = db.now()
            # 扣库存 + 建单在同一个事务里，避免并发超卖
            with conn:
                cur = conn.execute(
                    "UPDATE products SET stock = stock - ? "
                    "WHERE id = ? AND stock >= ?",
                    (quantity, p["id"], quantity))
                if cur.rowcount == 0:
                    return {"error": "库存不足"}
                conn.execute(
                    "INSERT INTO orders(id,user_id,items_json,total,address,note,"
                    "status,pay_url,is_demo,tracking_no,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,1,'',?)",
                    (order_id, user_id or None, json.dumps(items, ensure_ascii=False),
                     total, address.strip(), note or "", "待支付",
                     charge.get("pay_url", ""), ts))
            return {
                "order_id": order_id,
                "items": items,
                "total": total,
                "address": address.strip(),
                "note": note or "",
                "status": "待支付",
                # 占位收银台链接。演示环境，绝不伪装成真实支付；不做自动扣款。
                "pay_url": charge.get("pay_url", ""),
                "is_demo": True,
                "payment_gateway": charge.get("gateway", "mock"),
                "created_at": ts,
            }
        except Exception as exc:  # 不裸崩
            return {"error": f"下单失败：{exc}"}
        finally:
            conn.close()

    def get_order(self, order_id: str) -> dict:
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT * FROM orders WHERE id=?",
                               ((order_id or "").strip(),)).fetchone()
        if row is None:
            return {"error": f"找不到订单 {order_id}"}
        return self._order_dict(row)

    def list_orders(self, user_id: str | None = None, limit: int = 50) -> list:
        sql = "SELECT * FROM orders"
        args: list = []
        if user_id:
            sql += " WHERE user_id = ?"
            args.append(user_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(int(limit), 1))
        with closing(db.connect()) as conn:
            return [self._order_dict(r) for r in conn.execute(sql, args).fetchall()]

    # ---- 模拟支付确认（网页收银台，用户本人点） ----
    def confirm_payment(self, order_id: str) -> dict:
        """用户点"确认支付"后置为已支付。

        checkout hands off：这一步只发生在用户自己的浏览器里，智能体不参与。
        演示订单 is_demo=1，支付后不触发发货（无真实履约）。
        """
        db.init_db()
        conn = db.connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE orders SET status = '已完成' "
                    "WHERE id = ? AND status = '待支付'",
                    ((order_id or "").strip(),))
                if cur.rowcount == 0:
                    row = conn.execute("SELECT status FROM orders WHERE id=?",
                                       ((order_id or "").strip(),)).fetchone()
                    if row is None:
                        return {"error": f"找不到订单 {order_id}"}
                    return {"order_id": order_id, "status": row["status"],
                            "paid": row["status"] != "待支付"}
            row = conn.execute("SELECT * FROM orders WHERE id=?",
                               ((order_id or "").strip(),)).fetchone()
            return {"order_id": order_id, "status": "已完成", "paid": True,
                    "is_demo": True, "note": "演示订单，已完成但不触发发货"}
        except Exception as exc:
            return {"error": f"支付确认失败：{exc}"}
        finally:
            conn.close()

    @staticmethod
    def _order_dict(row) -> dict:
        if row is None:
            return {}
        d = dict(row)
        try:
            d["items"] = json.loads(d.pop("items_json", "[]"))
        except json.JSONDecodeError:
            d["items"] = []
        # 表里主键叫 id，对外契约叫 order_id（Phase 1 起就是这个口径）
        d["order_id"] = d.get("id", "")
        d["is_demo"] = bool(d.get("is_demo", 1))
        return d


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def new_product_id() -> str:
    return "P" + secrets.token_hex(4).upper()
