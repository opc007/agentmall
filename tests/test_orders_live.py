"""订单页「0 订单 → 智能体下单 → 订单自动出现」回归（2026-10-07 审核指出）。

审核原话：orders.html 的 poll() 只刷新已有行，整个脚本还被 `{% if orders %} 包住，
0 订单用户停在页面上时脚本都不渲染，新订单永远出不来
—— 正是路演第 5→6 步「让智能体下单，网页自动出现订单」的现场。

本测试不跑浏览器（跑不动），但把这三件事钉死：
1. 0 订单用户的页面里**必须**有轮询脚本（原来没有）
2. 表格的 tbody 必须常驻（原来被 {% if %} 整个包掉）
3. poll() 的 JS 里必须有「插入未知订单」的逻辑（原来只更新已知行）
   —— 并且它插进去的字段必须是 /api/orders 真有的字段
"""
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

TPL = os.path.join(ROOT, "agentmall", "web", "templates", "orders.html")
checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    checks.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def main() -> int:
    print(f"\n{'=' * 64}\n订单页实时插单 静态回归\n{'=' * 64}")
    src = open(TPL, encoding="utf-8").read()

    # ---------- 1. 0 订单时脚本也必须渲染 ----------
    # 先剥掉 HTML/Jinja 注释再判，否则我自己在注释里写的
    # 「整个脚本还被 {% if orders %} 包住」这句会把自己判成 FAIL
    script = src[src.find("{% block head %}"):]
    script = re.sub(r"<!--.*?-->", "", script, flags=re.S)
    script = re.sub(r"\{\#.*?#\}", "", script, flags=re.S)
    script_nc = re.sub(r"//.*", "", script)
    check("{% if orders %}" not in script_nc,
          "轮询脚本不再被 {% if orders %} 包住（0 订单用户也在轮询）")
    check("setInterval(poll" in script, "脚本里有 setInterval 轮询")
    check("var POLL_MS" in script, "脚本里有轮询间隔常量")

    # ---------- 2. tbody 常驻 ----------
    check('id="orders-body"' in src, "tbody#orders-body 常驻")
    tbl = src[src.find('<table class="table table-orders">'):]
    tbl = tbl[:tbl.find("</table>")]
    check("{% if orders %}" not in tbl,
          "订单表格本身不被 {% if %} 包住（0 订单时也在 DOM 里）")
    check('id="orders-empty"' in src, "0 订单的空状态用独立元素承载")
    check('id="orders-table"' in src, "表格容器有 id，脚本能显隐它")

    # ---------- 3. 必须有插入新订单的逻辑 ----------
    check("insertBefore" in src or "insertAdjacentHTML" in src,
          "脚本里有「插入新行」的逻辑（原来只有 paint，没有插入）")
    check("buildRow" in src, "脚本能构造新订单行")
    check("known[" in src, "脚本做已存在/未存在的差集判断")
    check("is-hidden" in src, "有订单时隐藏空状态、0 订单时显示空状态")

    # ---------- 4. 插入用的字段必须真实存在 ----------
    sys.path.insert(0, ROOT)
    from agentmall.web.app import view_order
    keys = set(view_order({
        "id": "AM0000000001", "status": "待支付", "total": 11.8,
        "address": "广西南宁市朝阳广场", "created_at": 1789000000,
        "is_demo": True, "pay_url": "", "items": [],
    }).keys())
    used = set(re.findall(r"\bo\.([a-z_]+)\b", src))
    missing = {u for u in used if u not in keys}
    check(not missing,
          f"脚本用到的字段 /api/orders 都有（多了：{missing or '无'}）")

    # ---------- 5. 转义：插入的是 innerHTML，必须转义 ----------
    check("function esc(" in src, "有 esc() 转义函数")
    check(".innerHTML" in src, "确实用 innerHTML 建行（所以转义是必需的）")
    body = src[src.find("function buildRow"):src.find("function showTable")]
    check("esc(o.order_id)" in body and "esc(o.address)" in body,
          "订单号/地址都过了转义（防止地址里的 HTML 被注入）")

    passed = sum(1 for ok, _ in checks if ok)
    print(f"\n{'=' * 64}\n订单页实时插单 {passed}/{len(checks)} 项通过")
    for ok, label in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    print("=" * 64)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
