"""用户智能体只读边界断言（CI 用）。

设计参考：shopify-mcp 的 assert-free-only（CI 扫描注册表，出现写操作就失败）。
规则：Store 对用户侧只允许读操作 + 建单；绝不允许改价/改库存/上下架/删单。

⚠️ Phase 2 语义变更（issue #2 / P0）：
建单**会扣减库存**（防超卖，Phase2任务书 §3 明确要求），所以 Phase 1 那条
"下单不得修改商品"的断言已经过时——现改为：

  下单**可以**扣库存，但**绝不允许**改价格、改上下架状态、改商户归属。

Phase 1 的原断言是 test_create_order_does_not_touch_catalog，语义比现在更严：
Phase 1 认为下单是"只写订单、不碰商品目录"；Phase 2 按 issue #2 明确放宽为
"可扣库存，其余商品字段只读"。价格/上下架的禁写边界没有放宽。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 用临时库，别污染开发数据
os.environ.setdefault("AGENTMALL_DB", os.path.join(
    tempfile.gettempdir(), "agentmall_test_readonly.db"))

from agentmall import db  # noqa: E402
from agentmall.store import Store  # noqa: E402

# 禁止出现在 Store 上的方法名：改价 / 改库存 / 上下架 / 删改商品与订单
FORBIDDEN = [
    "update_price", "set_price",
    "update_stock", "set_stock",
    "publish", "unpublish",
    "update_product", "set_product", "delete_product",
    "delete_order", "update_order",
    "takedown", "review_product", "takedown_product",
]


def test_no_forbidden_write_methods():
    for name in FORBIDDEN:
        assert not hasattr(Store, name), f"Store 不允许出现 {name}（用户智能体禁区）"


def test_create_order_does_not_touch_catalog():
    """下单只允许扣库存，不允许改价 / 改上下架 / 改商户归属。"""
    s = Store()
    p = s.search(keyword="纸巾", limit=1)[0]
    pid = p["id"]

    before = s.get(pid)
    stock_before = before["stock"]
    order = s.create_order(pid, 2, "测试地址")
    assert "error" not in order, f"下单应成功：{order}"
    after = s.get(pid)

    # 允许的：库存扣减
    assert after["stock"] == stock_before - 2, "下单应扣减库存"
    # 禁止的：价格 / 划线价 / 上下架 / 商户归属 / 名称
    for field in ("price", "original_price", "status", "merchant_id", "name"):
        assert before[field] == after[field], f"下单不得修改商品 {field}"


def test_create_order_rejects_oversell():
    """Phase 2 新增：超卖防护。"""
    s = Store()
    p = s.search(keyword="纸巾", limit=1)[0]
    order = s.create_order(p["id"], p["stock"] + 1, "测试地址")
    assert order.get("error") == "库存不足", f"超卖应被拒绝：{order}"
    # 拒绝后库存不能变
    assert s.get(p["id"])["stock"] == p["stock"], "超卖失败不得扣库存"


if __name__ == "__main__":
    test_no_forbidden_write_methods()
    test_create_order_does_not_touch_catalog()
    test_create_order_rejects_oversell()
    print("READONLY GUARD PASSED")
