"""商品搜索 + 订单内存存储。

Phase 2 在此接入 1688：新增 Alibaba1688Source(ProductSource) 即可，
上层 search/get/create_order 不用改。
"""
import json
import os
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))


class ProductSource:
    """商品来源适配器。MVP 用本地 JSON；Phase 2 实现 1688 版。"""

    def list_all(self) -> list:
        raise NotImplementedError

    # TODO(Phase 2): 实现 Alibaba1688Source(ProductSource)，
    # 对接 1688 开放平台 API 做商品/库存/价格同步。


class LocalJsonSource(ProductSource):
    def __init__(self, path: str = "") -> None:
        path = path or os.path.join(BASE, "data", "products_seed.json")
        with open(path, encoding="utf-8") as f:
            self.products = json.load(f)

    def list_all(self) -> list:
        return self.products


class Store:
    def __init__(self, source: ProductSource | None = None) -> None:
        self.source = source or LocalJsonSource()
        self.orders: dict = {}

    # ---- 商品 ----
    def search(self, keyword: str = "", category: str | None = None,
               max_price: float | None = None, limit: int = 10) -> list:
        kw = (keyword or "").strip()
        res = [
            p for p in self.source.list_all()
            if (not kw or kw in p["name"] or kw in p.get("category", ""))
            and (not category or p["category"] == category)
            and (not max_price or p["price"] <= max_price)
        ]
        res.sort(key=lambda p: p["price"])
        return res[:max(limit, 1)]

    def get(self, product_id: str) -> dict:
        for p in self.source.list_all():
            if p["id"] == product_id:
                return p
        return {"error": f"找不到商品 {product_id}"}

    # ---- 订单（辅助支付：只建单，不扣款） ----
    def create_order(self, product_id: str, quantity: int,
                     address: str, note: str = "") -> dict:
        p = self.get(product_id)
        if "error" in p:
            return p
        if quantity < 1:
            return {"error": "数量必须 >= 1"}
        if not (address or "").strip():
            return {"error": "收货地址不能为空"}
        order_id = "AM" + uuid.uuid4().hex[:10].upper()
        total = round(p["price"] * quantity, 2)
        order = {
            "order_id": order_id,
            "items": [{
                "product_id": p["id"],
                "name": p["name"],
                "price": p["price"],
                "quantity": quantity,
            }],
            "total": total,
            "address": address.strip(),
            "note": note,
            "status": "待支付",
            # TODO(Phase 2): 接入真实支付网关后替换为真实收银台链接。
            # 现阶段为占位，绝不得伪装成真实支付。
            "pay_url": f"https://pay.example.com/mock/{order_id}",
            "created_at": int(time.time()),
        }
        self.orders[order_id] = order
        return order

    def get_order(self, order_id: str) -> dict:
        return self.orders.get(order_id, {"error": f"找不到订单 {order_id}"})


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store
