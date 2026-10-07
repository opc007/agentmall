"""用户智能体只读边界断言（CI 用）。

设计参考：shopify-mcp 的 assert-free-only（CI 扫描注册表，出现写操作就失败）。
规则：Store 对用户侧只允许读操作 + 建单；绝不允许改价/改库存/上下架/删单。
建单是业务必需的写操作，但只写订单、不碰商品目录（test_create_order_does_not_touch_catalog 保证）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentmall.store import Store

# 禁止出现在 Store 上的方法名：改价 / 改库存 / 上下架 / 删改商品与订单
FORBIDDEN = [
    "update_price", "set_price",
    "update_stock", "set_stock",
    "publish", "unpublish",
    "update_product", "set_product", "delete_product",
    "delete_order", "update_order",
]


def test_no_forbidden_write_methods():
    for name in FORBIDDEN:
        assert not hasattr(Store, name), f"Store 不允许出现 {name}（用户智能体禁区）"


def test_create_order_does_not_touch_catalog():
    s = Store()
    before = [(p["id"], p["price"], p["stock"]) for p in s.source.list_all()]
    s.create_order("P001", 2, "测试地址")
    after = [(p["id"], p["price"], p["stock"]) for p in s.source.list_all()]
    assert before == after, "下单不得修改商品价格/库存"


if __name__ == "__main__":
    test_no_forbidden_write_methods()
    test_create_order_does_not_touch_catalog()
    print("READONLY GUARD PASSED")
