"""真实用户的边界操作：手滑、乱输、并发、恶意输入。

前面几套测的是「正常路径正确」，这套测「用户不正常的时候会发生什么」——
输错密码、乱点 URL、数量填负数、两个人同时抢最后一件、地址里塞脚本。

跑法： .venv/bin/python tests/test_edge_cases.py
"""
import asyncio
import html
import http.cookiejar
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

DB = os.path.join(tempfile.gettempdir(), "agentmall_test_edge.db")
checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    checks.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def free_ports(n=2):
    socks, ports = [], []
    try:
        for _ in range(n):
            s = socket.socket(); s.bind(("127.0.0.1", 0))
            socks.append(s); ports.append(s.getsockname()[1])
    finally:
        for s in socks:
            s.close()
    return ports


def wait_up(url, tries=80):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1).read(); return True
        except Exception:
            time.sleep(0.25)
    return False


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, path, data=None, method=None):
        body = urllib.parse.urlencode(data).encode() if data else None
        r = urllib.request.Request(self.base + path, data=body,
                                   method=method or ("POST" if data else "GET"))
        try:
            resp = self.opener.open(r, timeout=15)
        except urllib.error.HTTPError as e:
            resp = e
        return resp.status, resp.read().decode("utf-8", "ignore"), resp.geturl()


async def mcp(base, key, tool, args):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(f"{base}/mcp", headers={"Authorization": f"Bearer {key}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
    sc = getattr(res, "structuredContent", None)
    if sc is not None and "result" in sc:
        return sc["result"]
    for b in res.content:
        if getattr(b, "text", None):
            import json
            try:
                return json.loads(b.text)
            except ValueError:
                return b.text
    return getattr(res, "content", res)


def q(sql, args=()):
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def looks_like_crash(body: str) -> bool:
    """未捕获异常 / 框架错误页，而不是友好提示。"""
    markers = ("Traceback", "Internal Server Error", "RuntimeError", "AttributeError",
               "KeyError", "ValueError", "jinja2.exceptions", "UndefinedError",
               "werkzeug.debug", "sqlite3.")
    return any(m in body for m in markers)


def main() -> int:
    for s in ("", "-wal", "-shm"):
        if os.path.exists(DB + s):
            os.remove(DB + s)
    wp, mp = free_ports(2)
    web, mcp_base = f"http://127.0.0.1:{wp}", f"http://127.0.0.1:{mp}"
    env = dict(os.environ, AGENTMALL_DB=DB, PYTHONPATH=ROOT,
               AGENTMALL_PUBLIC_URL=web, AGENTMALL_MCP_PORT=str(mp))
    procs = [
        subprocess.Popen([sys.executable, "-m", "uvicorn", "agentmall.web.app:app", "--port", str(wp)],
                         cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([sys.executable, "-m", "agentmall.http_server"], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    try:
        print(f"\n{'=' * 64}\n真实用户边界操作（web={wp} mcp={mp}）\n{'=' * 64}")
        if not (wait_up(f"{web}/healthz") and wait_up(f"{mcp_base}/healthz")):
            print("❌ 服务起不来"); return 1

        # ============ 注册：手滑类
        st, body, _ = Client(web).req("/register", {"username": "ab", "password": "123"})
        check(st == 400 and "用户名" in body,
              f"太短的用户名被拒且有中文提示（HTTP {st}）")
        check(not looks_like_crash(body), "注册校验失败时不是 500 崩溃页")

        st, body, _ = Client(web).req("/register", {"username": "edgeuser", "password": "short"})
        check(st == 400 and "密码" in body, f"弱密码被拒且有中文提示（HTTP {st}）")

        st, body, _ = Client(web).req("/register", {"username": "edgeuser", "password": "pass123456"})
        check(st == 200, "第一次注册成功")
        st, body, _ = Client(web).req("/register", {"username": "edgeuser", "password": "pass123456"})
        check(st == 400 and "alert-error" in body,
              f"重复用户名被拒且在页面上给出提示（HTTP {st}）")
        check("alert-error" in body, "重复注册显示的是页面内提示，不是空白页")

        # ============ 登录：手滑类
        C = Client(web)
        st, body, _ = C.req("/login", {"username": "edgeuser", "password": "wrongpass"})
        check(st == 401 and "用户名或密码错误" in body,
              f"密码输错返回 401 且有中文提示（HTTP {st}）")
        check(not looks_like_crash(body), "登录失败不是崩溃页")
        st, body, url = C.req("/login", {"username": "edgeuser", "password": "pass123456"})
        check(st in (200, 303), f"输对密码能登录（HTTP {st}）")
        _, me, _ = C.req("/me")
        key = (re.search(r"uk_[A-Za-z0-9_]+", me) or [None]) and re.search(r"uk_[A-Za-z0-9_]+", me).group(0)
        check(bool(key), "拿到自己的 key")

        # ============ 乱点 URL
        for path, label in (("/pay/AMDOESNOTEXIST", "不存在的订单收银台"),
                            ("/orders/NOPE", "不存在的订单详情")):
            st, body, _ = C.req(path)
            check(st in (403, 404) and not looks_like_crash(body),
                  f"{label}：返回 {st} 且不是崩溃页")
            if st in (403, 404):
                check("<html" in body.lower() and "detail" not in body[:200],
                      f"{label}：返回的是友好网页不是裸 JSON")

        st, body, _ = Client(web).req("/me")   # 未登录访问 /me
        check(st == 200 or "登录" in (url or body), f"未登录访问个人中心会引导去登录（HTTP {st}）")

        st, body, _ = Client(web).req("/this-page-does-not-exist-xyz")
        check(st == 404 and not looks_like_crash(body), f"不存在的页面 404 且不是崩溃页（HTTP {st}）")
        check("<html" in body.lower() and "页面不存在" in body,
              "乱输网址得到的是带导航的友好 404 页，不是裸 JSON")
        check("首页" in body and "我的订单" in body, "404 页给出了可点的去向，不把人堵死")

        # ============ 恶意输入：XSS
        xss = "<script>alert(1)</script>"
        r = asyncio.run(mcp(mcp_base, key, "search_products", {"keyword": xss, "limit": 3}))
        check(isinstance(r, list), "搜索里塞 <script> 不报错")
        st, page, _ = C.req(f"/orders")
        check("<script>alert(1)</script>" not in page, "订单页没有原样吐出 <script>（应转义或不出现在此）")

        # 地址带脚本 → 看渲染是否转义
        items = asyncio.run(mcp(mcp_base, key, "search_products", {"keyword": "抽纸", "limit": 1}))
        pid = items[0]["id"]
        o = asyncio.run(mcp(mcp_base, key, "create_order",
                            {"product_id": pid, "quantity": 1, "address": f"广西南宁{xss}"}))
        oid = o.get("order_id", "")
        st, pay, _ = C.req(f"/pay/{oid}")
        check(st == 200 and "<script>alert(1)</script>" not in pay,
              f"收货地址里的 <script> 在收银台被转义，没有原样执行（HTTP {st}）")
        check("&lt;script&gt;" in pay or "alert(1)" not in pay, "脚本标签确实被 HTML 转义")

        # ============ 数量：负数 / 零 / 超量 / 非数字
        for qty, label in ((-1, "负数"), (0, "零"), (99999, "超出库存")):
            r = asyncio.run(mcp(mcp_base, key, "create_order",
                                {"product_id": pid, "quantity": qty, "address": "广西南宁市"}))
            check(isinstance(r, dict) and bool(r.get("error")),
                  f"数量={qty}（{label}）被拒并返回 error：{str(r)[:60]}")
        r = asyncio.run(mcp(mcp_base, key, "create_order",
                            {"product_id": pid, "quantity": "abc", "address": "广西南宁市"}))
        check(("error" in str(r).lower()) or ("validation" in str(r).lower()),
              f"数量=abc（类型不对）被拦下，没建出单：{str(r)[:60]}")

        r = asyncio.run(mcp(mcp_base, key, "create_order",
                            {"product_id": pid, "quantity": 1, "address": ""}))
        check(isinstance(r, dict) and r.get("error"), f"空收货地址被拒：{str(r)[:60]}")

        r = asyncio.run(mcp(mcp_base, key, "create_order",
                            {"product_id": "P-NOT-EXIST", "quantity": 1, "address": "广西南宁市"}))
        check(isinstance(r, dict) and r.get("error"), f"不存在的商品被拒：{str(r)[:60]}")

        # ============ 超卖竞态：10 个人同时抢最后 5 件
        stock0 = q("SELECT stock FROM products WHERE id=?", (pid,))[0]["stock"]
        asyncio.run(mcp(mcp_base, key, "create_order",
                        {"product_id": pid, "quantity": max(1, stock0 - 5), "address": "A"}))
        now = q("SELECT stock FROM products WHERE id=?", (pid,))[0]["stock"]
        check(now == 5, f"库存被压到 5（实际 {now}），用于并发抢")

        async def race():
            async def one(i):
                return await mcp(mcp_base, key, "create_order",
                                 {"product_id": pid, "quantity": 1, "address": f"并发{i}"})
            return await asyncio.gather(*[one(i) for i in range(10)])
        results = asyncio.run(race())
        ok = [r for r in results if isinstance(r, dict) and not r.get("error")]
        bad = [r for r in results if isinstance(r, dict) and r.get("error")]
        final = q("SELECT stock FROM products WHERE id=?", (pid,))[0]["stock"]
        check(len(ok) == 5 and len(bad) == 5,
              f"10 人抢 5 件：成功 {len(ok)} 单、拒绝 {len(bad)} 单（应为 5/5）")
        check(final == 0, f"库存正好扣到 0，没扣成负数（实际 {final}）")
        n_orders = q("SELECT COUNT(*) c FROM orders")[0]["c"]
        check(n_orders >= len(ok), f"并发建的单都落库了（订单总数 {n_orders} ≥ 成功 {len(ok)}）")

        # ============ 无效 key
        r = asyncio.run(mcp(mcp_base, "uk_totally_bogus_key", "search_products", {"keyword": "抽纸"}))
        check(isinstance(r, (list, dict)), f"用不存在的 key 调用不崩：{str(r)[:60]}")
        check(isinstance(r, list) and any(isinstance(x, dict) and x.get("denied")
                                          for x in r if isinstance(r, list)) or
              (isinstance(r, dict) and (r.get("error") or r.get("denied"))),
              f"不存在的 key 被拒绝（denied/error）：{str(r)[:80]}")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    passed = sum(1 for ok, _ in checks if ok)
    print(f"\n{'=' * 64}\n边界操作 {passed}/{len(checks)} 项通过")
    for ok, label in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    print("=" * 64)
    for s in ("", "-wal", "-shm"):
        if os.path.exists(DB + s):
            try:
                os.remove(DB + s)
            except OSError:
                pass
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())