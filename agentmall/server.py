"""AgentMall MCP Server（6 小时 MVP）.

启动：python -m agentmall.server
"""
from mcp.server.fastmcp import FastMCP

from .store import get_store

mcp = FastMCP("agentmall")
store = get_store()


@mcp.tool()
def search_products(keyword: str, category: str = "", max_price: float = 0,
                    limit: int = 10) -> list[dict]:
    """搜索日用商品，按价格升序返回。
    keyword: 关键词，如"纸巾"；category: 纸巾/垃圾袋/清洁/收纳/个护，空=不限；
    max_price: 最高价，0=不限；limit: 最多返回条数。"""
    return store.search(keyword=keyword,
                        category=category or None,
                        max_price=max_price or None,
                        limit=limit)


@mcp.tool()
def get_product(product_id: str) -> dict:
    """查看商品详情。product_id 如 "P001"。"""
    return store.get(product_id)


@mcp.tool()
def create_order(product_id: str, quantity: int, address: str,
                 note: str = "") -> dict:
    """创建订单（辅助支付）。
    返回订单号与占位支付链接，用户需自行点击完成付款，智能体不自动扣款。"""
    return store.create_order(product_id, quantity, address, note)


@mcp.tool()
def get_order(order_id: str) -> dict:
    """查询订单状态。order_id 如 "AMXXXXXXXXXX"。"""
    return store.get_order(order_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
