"""AgentMall 演示链路：真实 MCP stdio client 联调 tool。

任务书 §5 允许"用 MCP Inspector 或手写 client 脚本逐个调通"——本脚本就是后者。
用途：一键复现 demo/演示脚本.md 的全链路，并留下可截图的输出。

运行：  .venv/bin/python tests/demo_chain.py

设计说明：断言刻意写成**数据无关**的（不硬编码 P001/16.9 这类具体 SKU），
因为 Phase 2 商品库会换成爬取的真实商品，硬编码的期望值会随数据源漂移。
改为校验不变量：数量、升序、订单号格式、total == 单价×数量、状态流转正确。
"""
import asyncio
import json
import os
import re
import sys
import tempfile

os.environ.setdefault("AGENTMALL_DB", os.path.join(
    tempfile.gettempdir(), "agentmall_demo_chain.db"))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ADDR = "广西南宁市朝阳广场"
checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    checks.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def unwrap(res) -> object:
    """兼容三种返回形态：structuredContent / 单个文本 JSON / 多个文本 JSON。"""
    sc = getattr(res, "structuredContent", None)
    if sc is not None and "result" in sc:
        return sc["result"]
    parsed = []
    for block in res.content:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            parsed.append(json.loads(text))
        except json.JSONDecodeError:
            parsed.append(text)
    if not parsed:
        return res.content
    return parsed[0] if len(parsed) == 1 else parsed


def banner(step: str, say: str) -> None:
    print(f"\n{'=' * 66}\n{step}\n用户：{say}\n{'-' * 66}")


async def run_chain(session: ClientSession) -> None:
    # ---- 工具注册表（用户角色）----
    print("\n[准备] MCP Server 已连接，工具注册表：")
    names = [t.name for t in (await session.list_tools()).tools]
    for t in (await session.list_tools()).tools:
        print(f"  - {t.name}: {(t.description or '').splitlines()[0][:40]}")
    check(set(names) == {"search_products", "get_product",
                         "create_order", "get_order"},
          f"用户角色恰好 4 个 tool（实际 {len(names)} 个：{names}）")

    # ---- 第 1 步：搜索 ----
    banner("第 1 步：搜索", "帮我找最便宜的抽纸")
    res = unwrap(await session.call_tool(
        "search_products", {"keyword": "纸巾", "max_price": 30}))
    for p in res:
        print(f"  {p['id']}  {p['name'][:26]:<26} {p['price']:>6} 元/{p['unit']}")
    prices = [p["price"] for p in res]
    check(len(res) > 0, f"搜索'纸巾'有结果（{len(res)} 条）")
    check(prices == sorted(prices), "按价格升序")
    check(all(p["status"] == "在售" for p in res), "只返回在售商品")
    check(all(p["price"] <= 30 for p in res), "max_price 过滤生效")
    cheapest = res[0]

    # ---- 第 2 步：比价 + 详情 ----
    banner("第 2 步：比价", f"{cheapest['id']} 和另一个划算吗？")
    if len(res) > 1:
        other = res[-1]
        for pid in (cheapest["id"], other["id"]):
            d = unwrap(await session.call_tool("get_product", {"product_id": pid}))
            print(f"  {pid}  {d['name'][:26]:<26} {d['price']} 元/{d['unit']} "
                  f"(原价 {d.get('original_price')})")
        detail = unwrap(await session.call_tool(
            "get_product", {"product_id": cheapest["id"]}))
        check(detail["id"] == cheapest["id"], "get_product 返回详情")
        check(detail["original_price"] >= detail["price"],
              "划线原价 >= 售价")
    else:
        check(False, "结果不足 2 条，无法做比价演示")

    # ---- 第 3 步：下单 ----
    banner("第 3 步：下单", f"买 2 份 {cheapest['id']}，送到{ADDR}")
    order = unwrap(await session.call_tool("create_order", {
        "product_id": cheapest["id"], "quantity": 2, "address": ADDR}))
    print(json.dumps(order, ensure_ascii=False, indent=2))
    oid = order.get("order_id", "")
    expect_total = round(cheapest["price"] * 2, 2)
    check(bool(re.fullmatch(r"AM[0-9A-F]{10}", oid)), f"订单号 AM+10 位大写（{oid}）")
    check(order.get("total") == expect_total,
          f"total={expect_total}（实际 {order.get('total')}）")
    check(order.get("status") == "待支付", f"status=待支付（实际 {order.get('status')}）")
    check(bool(order.get("pay_url")), "pay_url 非空")
    check(order.get("is_demo") is True, "is_demo=1 标记")

    # ---- 第 4 步：查单 ----
    banner("第 4 步：查单", "查一下刚才那个订单的状态")
    got = unwrap(await session.call_tool("get_order", {"order_id": oid}))
    print(json.dumps(got, ensure_ascii=False, indent=2))
    check(got.get("order_id") == oid and got.get("status") == "待支付",
          "查回同一订单，状态待支付")

    # ---- 异常演示 ----
    banner("异常演示", "搜个不存在的词 + 查不存在的单")
    empty = unwrap(await session.call_tool("search_products", {"keyword": "跑车"}))
    print(f"  搜索'跑车' → {empty}")
    check(empty == [], "搜索无结果返回空列表，不崩溃")

    miss_o = unwrap(await session.call_tool(
        "get_order", {"order_id": "AM0000000000"}))
    print(f"  get_order(AM0000000000) → {miss_o}")
    check("error" in miss_o, "订单不存在返回 error 而非崩溃")

    miss_p = unwrap(await session.call_tool(
        "get_product", {"product_id": "P99999"}))
    print(f"  get_product(P99999) → {miss_p}")
    check("error" in miss_p, "商品不存在返回 error 而非崩溃")

    over = unwrap(await session.call_tool("create_order", {
        "product_id": cheapest["id"], "quantity": 999999, "address": ADDR}))
    print(f"  超量下单 → {over}")
    check(over.get("error") == "库存不足", "超卖被拒绝，返回库存不足")


async def main() -> int:
    env = dict(os.environ,
               PYTHONPATH=os.path.join(os.path.dirname(__file__), ".."),
               AGENTMALL_API_KEY="")  # 空 = 用户角色无鉴权演示模式
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "agentmall.server"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await run_chain(session)

    passed = sum(1 for ok, _ in checks if ok)
    print(f"\n{'=' * 66}\n演示链路 {passed}/{len(checks)} 项通过")
    for ok, label in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    print("=" * 66)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
