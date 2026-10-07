"""AgentMall MVP 演示链路：真实 MCP stdio client 联调 4 个 tool。

任务书 §5 允许"用 MCP Inspector 或手写 client 脚本逐个调通"——本脚本就是后者，
用途是 CI/本地一键复现 demo/演示脚本.md 的全链路，并留下可截图的输出。

运行：  .venv/bin/python tests/demo_chain.py
"""
import asyncio
import json
import os
import re
import sys

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


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agentmall.server"],
        env=dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..")),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ---- 工具注册表 ----
            print("\n[准备] MCP Server 已连接，工具注册表：")
            names = []
            for t in (await session.list_tools()).tools:
                names.append(t.name)
                print(f"  - {t.name}: {(t.description or '').splitlines()[0][:40]}")
            check(set(names) == {"search_products", "get_product",
                                 "create_order", "get_order"},
                  f"恰好注册 4 个 tool（实际 {len(names)} 个）")

            # ---- 第 1 步：搜索 ----
            banner("第 1 步：搜索", "帮我找最便宜的抽纸")
            res = unwrap(await session.call_tool(
                "search_products", {"keyword": "纸巾"}))
            for p in res:
                print(f"  {p['id']}  {p['name']:<28} {p['price']:>6} 元/{p['unit']}")
            prices = [p["price"] for p in res]
            check(len(res) == 8, f"返回 8 个纸巾 SKU（实际 {len(res)}）")
            check(prices == sorted(prices), "按价格升序")
            check(res[0]["id"] == "P008" and res[0]["price"] == 9.9,
                  f"最便宜为 P008 9.9 元（实际 {res[0]['id']} {res[0]['price']}）")

            # ---- 第 2 步：比价 + 详情 ----
            banner("第 2 步：比价", "P001 和 P003 哪个划算？")
            detail = {}
            for pid in ("P001", "P003"):
                detail[pid] = unwrap(await session.call_tool(
                    "get_product", {"product_id": pid}))
                print(f"  {pid}  {detail[pid]['name']:<28} "
                      f"{detail[pid]['price']} 元/{detail[pid]['unit']} "
                      f"(原价 {detail[pid]['original_price']})")
            check(detail["P001"]["price"] == 16.9 and detail["P003"]["price"] == 22.9,
                  "P001=16.9/提、P003=22.9/包，详情与演示脚本一致")

            # ---- 第 3 步：下单 ----
            banner("第 3 步：下单", "买 2 提 P001，送到广西南宁市朝阳广场")
            order = unwrap(await session.call_tool("create_order", {
                "product_id": "P001", "quantity": 2, "address": ADDR}))
            print(json.dumps(order, ensure_ascii=False, indent=2))
            oid = order["order_id"]
            check(bool(re.fullmatch(r"AM[0-9A-F]{10}", oid)),
                  f"订单号 AM+10 位大写（{oid}）")
            check(order["total"] == 33.8, f"total=33.8（实际 {order['total']}）")
            check(order["status"] == "待支付", f"status=待支付（实际 {order['status']}）")
            check(bool(order["pay_url"]), f"pay_url 非空（占位 {order['pay_url']}）")
            check("example.com" in order["pay_url"],
                  "pay_url 为 example.com 占位，未伪装真实支付")

            # ---- 第 4 步：查单 ----
            banner("第 4 步：查单", "查一下刚才那个订单的状态")
            got = unwrap(await session.call_tool("get_order", {"order_id": oid}))
            print(json.dumps(got, ensure_ascii=False, indent=2))
            check(got.get("order_id") == oid and got.get("status") == "待支付",
                  "查回同一订单，状态待支付")

            # ---- 异常演示（演示脚本加分项）----
            banner("异常演示", "搜个不存在的词 + 查不存在的单")
            empty = unwrap(await session.call_tool(
                "search_products", {"keyword": "跑车"}))
            print(f"  搜索'跑车' → {empty}")
            check(empty == [], "搜索无结果返回空列表，不崩溃")

            miss_o = unwrap(await session.call_tool(
                "get_order", {"order_id": "AM0000000000"}))
            print(f"  get_order(AM0000000000) → {miss_o}")
            check("error" in miss_o, "订单不存在返回 error 而非崩溃")

            miss_p = unwrap(await session.call_tool(
                "get_product", {"product_id": "P999"}))
            print(f"  get_product(P999) → {miss_p}")
            check("error" in miss_p, "商品不存在返回 error 而非崩溃")

    # ---- 汇总 ----
    passed = sum(1 for ok, _ in checks if ok)
    print(f"\n{'=' * 66}\n演示链路 {passed}/{len(checks)} 项通过")
    for ok, label in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    print("=" * 66)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
