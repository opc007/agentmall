"""路演 7 步端到端验收（docs/路演Demo流程.md）。

这是今晚最该被反复跑的那个脚本：真实起 Web + MCP HTTP 两个进程，
走完「注册 → 登录 → 拿接入点 → 智能体下单 → 订单列表 → 扫码支付 → 订单完成」。

与 demo_chain.py 的区别：那个只验 MCP tool 层（用户闭环 17 项），
这个验**用户 + 网页 + 支付**整条路演链路。

运行： .venv/bin/python tests/test_roadshow.py
"""
import asyncio
import http.cookiejar
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

DB = os.path.join(tempfile.gettempdir(), "agentmall_test_roadshow.db")
checks: list[tuple[bool, str]] = []
USER = {"username": "roadshow", "password": "pass123456"}
ADDR = "广西南宁市朝阳广场"


def check(ok: bool, label: str) -> None:
    checks.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def free_ports(n: int = 2) -> list[int]:
    """一次占住 n 个端口再释放，避免两次 bind 拿到同一个端口（OS 会立刻复用刚释放的端口）。"""
    socks, ports = [], []
    try:
        for _ in range(n):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            socks.append(s)
            ports.append(s.getsockname()[1])
    finally:
        for s in socks:
            s.close()
    return ports


class Client:
    """cookie 会话。用标准 CookieJar——login 是 303 重定向，
    Set-Cookie 挂在重定向响应上，手动抓 headers 会漏。"""

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, path: str, data: dict | None = None, method: str | None = None):
        url = self.base + path
        body = urllib.parse.urlencode(data).encode() if data else None
        r = urllib.request.Request(
            url, data=body, method=method or ("POST" if data else "GET"))
        try:
            resp = self.opener.open(r, timeout=15)
        except urllib.error.HTTPError as e:
            resp = e
        return resp.status, resp.read(), resp.geturl()

    @property
    def has_cookie(self) -> bool:
        return len(self.jar) > 0


def wait_up(url: str, proc, tries: int = 80) -> bool:
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except Exception:
            if proc.poll() is not None:
                return False
            time.sleep(0.5)
    return False


async def mcp_order(base: str, api_key: str, keyword: str) -> dict:
    """模拟智能体（workbuddy）：按接入点连 MCP，搜索并下单。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(f"{base}/mcp",
                                     headers={"Authorization": f"Bearer {api_key}"}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "search_products", {"keyword": keyword, "limit": 3})
            items = _unwrap(res)
            if not items:
                return {"error": "搜索无结果"}
            p = items[0]
            res2 = await session.call_tool("create_order", {
                "product_id": p["id"], "quantity": 2, "address": ADDR})
            return {"product": p, "order": _unwrap(res2)}


def _unwrap(res):
    import json
    sc = getattr(res, "structuredContent", None)
    if sc is not None and "result" in sc:
        return sc["result"]
    out = []
    for b in res.content:
        t = getattr(b, "text", None)
        if t:
            try:
                out.append(json.loads(t))
            except json.JSONDecodeError:
                out.append(t)
    return out[0] if len(out) == 1 else out


async def main() -> int:
    web_port, mcp_port = free_ports(2)
    web, mcp_base = f"http://127.0.0.1:{web_port}", f"http://127.0.0.1:{mcp_port}"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(DB + suffix):
            os.remove(DB + suffix)
    env = dict(os.environ, AGENTMALL_DB=DB, PYTHONPATH=ROOT,
               AGENTMALL_PUBLIC_URL=web, AGENTMALL_MCP_PORT=str(mcp_port))

    procs = [
        subprocess.Popen([sys.executable, "-m", "uvicorn", "agentmall.web.app:app",
                          "--port", str(web_port)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([sys.executable, "-m", "agentmall.http_server"],
                         cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    try:
        # 第 1 步：打开网页
        print(f"\n{'=' * 64}\n路演 7 步端到端验收（web={web_port} mcp={mcp_port}）\n{'=' * 64}")
        if not wait_up(f"{web}/healthz", procs[0]):
            print("❌ Web 服务启动失败"); return 1
        if not wait_up(f"{mcp_base}/healthz", procs[1]):
            print("❌ MCP HTTP 服务启动失败"); return 1
        c = Client(web)
        st, body, _ = c.req("/")
        check(st == 200, f"第1步 网页可打开（HTTP {st}）")
        check("演示" in body.decode("utf-8", "ignore"),
              "首页标注了演示数据/演示环境")

        # 注册 + 登录
        st, _, url = c.req("/register", USER)
        check(st in (200, 303), f"注册成功（HTTP {st}）")
        c2 = Client(web)
        st, _, url = c2.req("/login", USER)
        check(st in (200, 303), f"登录成功（HTTP {st}）")
        check(c2.has_cookie, "登录态 cookie 已下发")

        # 第 2 步：个人中心拿 MCP 接入点
        st, body, _ = c2.req("/me")
        html = body.decode("utf-8", "ignore")
        check(st == 200, f"第2步 个人中心可打开（HTTP {st}）")
        m = re.search(r"uk_[A-Za-z0-9_]+", html)
        check(bool(m), "个人中心展示了用户 API key")
        key = m.group(0) if m else ""
        check("mcpServers" in html or "mcp" in html.lower(),
              "个人中心给出 MCP 接入点配置")

        # 第 3-5 步：智能体按接入点搜索 + 下单
        print("\n--- 智能体（workbuddy）执行 ---")
        r = await mcp_order(mcp_base, key, "纸巾")
        check("error" not in r, f"智能体搜索到商品：{r.get('product', {}).get('name', '?')}")
        order = r.get("order", {})
        oid = order.get("order_id", "")
        check(bool(re.fullmatch(r"AM[0-9A-F]{10}", oid)),
              f"第5步 建单成功：{oid} total={order.get('total')}")
        check(order.get("status") == "待支付", "订单初始状态=待支付")
        check(bool(order.get("pay_url")), f"pay_url 非空：{order.get('pay_url')}")

        # 第 6 步：订单列表出现 → 去支付 → 二维码
        st, body, _ = c2.req("/orders")
        check(st == 200, f"第6步 订单列表可打开（HTTP {st}）")
        check(oid in body.decode("utf-8", "ignore"), "订单列表里能看到刚下的单")

        st, body, _ = c2.req(f"/pay/{oid}")
        pay_html = body.decode("utf-8", "ignore")
        check(st == 200, f"收银台页可打开（HTTP {st}）")
        check("演示环境" in pay_html, "收银台标注了「演示环境」")
        check(re.search(r"¥\s*\d", pay_html) is not None, "收银台显示金额")
        check("qr.png" in pay_html, "收银台含二维码")

        st, png, _ = c2.req(f"/pay/{oid}/qr.png")
        check(st == 200 and png[:4] == b"\x89PNG",
              f"二维码 PNG 可下载（HTTP {st}，{len(png)} 字节）")

        # 第 7 步：确认支付 → 订单完成
        st, body, url = c2.req(f"/pay/{oid}/confirm", method="POST")
        check(st in (200, 303), f"确认支付成功（HTTP {st}）")

        st, body, _ = c2.req("/orders")
        check("已完成" in body.decode("utf-8", "ignore"), "订单状态变为「已完成」")

        # 库存确实扣了
        sys.path.insert(0, ROOT)
        prev = dict(os.environ)
        os.environ["AGENTMALL_DB"] = DB
        from agentmall.store import get_store
        pid = r["product"]["id"]
        final = get_store().get(pid)
        os.environ.clear()
        os.environ.update(prev)
        check(final["status"] == "在售", f"商品 {pid} 仍在售")
    finally:
        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    passed = sum(1 for ok, _ in checks if ok)
    print(f"\n{'=' * 64}\n路演链路 {passed}/{len(checks)} 项通过")
    for ok, label in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    print("=" * 64)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(DB + suffix):
            try:
                os.remove(DB + suffix)
            except OSError:
                pass
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
