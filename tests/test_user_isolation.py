"""真实用户视角验收：两个真人各自注册、各自下单、互相看不见对方的东西。

这条不是接口层断言，是把「A 下了单，B 会不会看见、能不能替他付款」当真人一样走一遍。
2026-10-07 实测发现过：MCP 建单不传 user_id → 订单 user_id 为 NULL → 网页层的
「未绑定订单池」对所有登录用户可见可付，B 能看到并付款 A 的订单。本脚本锁死这个回归。

运行： .venv/bin/python tests/test_user_isolation.py
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

DB = os.path.join(tempfile.gettempdir(), "agentmall_test_isolation.db")
checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    checks.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def free_ports(n: int = 2) -> list[int]:
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


def wait_up(url: str, tries: int = 80) -> bool:
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except Exception:
            time.sleep(0.25)
    return False


class Client:
    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

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
    async with streamablehttp_client(f"{base}/mcp",
                                     headers={"Authorization": f"Bearer {key}"}) as (r, w, _):
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
    return res.content


async def main() -> int:
    for s in ("", "-wal", "-shm"):
        if os.path.exists(DB + s):
            os.remove(DB + s)
    wp, mp = free_ports(2)
    web, mcp_base = f"http://127.0.0.1:{wp}", f"http://127.0.0.1:{mp}"
    env = dict(os.environ, AGENTMALL_DB=DB, PYTHONPATH=ROOT,
               AGENTMALL_PUBLIC_URL=web, AGENTMALL_MCP_PORT=str(mp))
    procs = [
        subprocess.Popen([sys.executable, "-m", "uvicorn", "agentmall.web.app:app",
                          "--port", str(wp)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([sys.executable, "-m", "agentmall.http_server"],
                         cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    try:
        print(f"\n{'=' * 64}\n真实用户视角：订单跨用户隔离（web={wp} mcp={mp}）\n{'=' * 64}")
        if not (wait_up(f"{web}/healthz") and wait_up(f"{mcp_base}/healthz")):
            print("❌ 服务起不来")
            return 1

        def signup(base, name):
            c = Client(base)
            st, _, _ = c.req("/register", {"username": name, "password": "pass123456"})
            if st != 200:
                c.req("/login", {"username": name, "password": "pass123456"})
            _, me, _ = c.req("/me")
            m = re.search(r"uk_[A-Za-z0-9_]+", me)
            return c, (m.group(0) if m else "")

        # ---- 用户 A：小李，用自己的 key 让智能体下单
        A, ka = signup(web, "xiaoli")
        check(bool(ka), f"用户 A（小李）注册成功，拿到自己的 key {ka[:10]}…")
        items = await mcp(mcp_base, ka, "search_products", {"keyword": "抽纸", "limit": 3})
        pid = items[0]["id"]
        order = await mcp(mcp_base, ka, "create_order",
                          {"product_id": pid, "quantity": 1,
                           "address": "广西南宁市朝阳广场"})
        oid = order["order_id"]
        check(bool(oid), f"A 的智能体建单：{oid} ¥{order['total']}")

        # 根因断言：订单必须真的归属到 A
        import sqlite3
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT user_id,status FROM orders WHERE id=?", (oid,)).fetchone())
        conn.close()
        ua = re.search(r"uk_[A-Za-z0-9_]+", A.req("/me")[1]).group(0)
        uid_a = None
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        uid_a = conn.execute("SELECT id FROM users WHERE api_key=?", (ua,)).fetchone()["id"]
        conn.close()
        check(row["user_id"] == uid_a,
              f"订单已归属到 A 本人（user_id={row['user_id']}，应为 {uid_a}）——不是 NULL")

        st, html_a, _ = A.req("/orders")
        check(oid in html_a, "A 自己的订单页里能看到这一单")

        # ---- 用户 B：完全无关的另一个人
        B, kb = signup(web, "xiaowang")
        check(bool(kb), f"用户 B（小王）注册成功，拿到自己的 key {kb[:10]}…")

        st, html_b, _ = B.req("/orders")
        check(oid not in html_b, ">>> B 的订单页里看不到 A 的单（跨用户不可见）")

        st, _pay, _ = B.req(f"/pay/{oid}")
        check(st != 200, f">>> B 直接打开 A 的收银台被拒（HTTP {st}）")

        st, _conf, _ = B.req(f"/pay/{oid}/confirm", method="POST")
        check(st != 200, f">>> B 强行确认支付 A 的单被拒（HTTP {st}）")

        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        after = dict(conn.execute(
            "SELECT status FROM orders WHERE id=?", (oid,)).fetchone())
        conn.close()
        check(after["status"] == "待支付",
              f">>> A 的单仍是「{after['status']}」，没被 B 付掉")

        # ---- B 自己也能正常下单，互不串号
        order_b = await mcp(mcp_base, kb, "create_order",
                            {"product_id": pid, "quantity": 1,
                             "address": "广东省广州市天河区"})
        oid_b = order_b["order_id"]
        check(bool(oid_b) and oid_b != oid, f"B 也能正常建单：{oid_b}")
        st, html_b2, _ = B.req("/orders")
        check(oid_b in html_b2 and oid not in html_b2,
              "B 的订单页只有自己那一单")
        st, html_a2, _ = A.req("/orders")
        check(oid in html_a2 and oid_b not in html_a2,
              "A 的订单页只有自己那一单")

        # ---- A 本人当然能正常付款（别把正常路径也堵死）
        st, _p, _ = A.req(f"/pay/{oid}")
        check(st == 200, "A 本人打开自己的收银台正常")
        st, _c, _ = A.req(f"/pay/{oid}/confirm", method="POST")
        check(st in (200, 303), f"A 自己付款成功（HTTP {st}）")
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        paid = dict(conn.execute(
            "SELECT status FROM orders WHERE id=?", (oid,)).fetchone())
        conn.close()
        check(paid["status"] == "已完成",
              f"A 付完之后状态为「{paid['status']}」——正常路径没被误伤")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    passed = sum(1 for ok, _ in checks if ok)
    print(f"\n{'=' * 64}\n跨用户隔离 {passed}/{len(checks)} 项通过")
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
    sys.exit(asyncio.run(main()))
