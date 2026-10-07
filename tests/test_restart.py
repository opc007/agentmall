"""真·崩溃重启后的数据留存（Phase2任务书 §6 验收标准 4）。

    「kill 重启后订单/商品/审计都在」

这个验收标准之前**没有被真正测过**：
- test_roles 的 test_restart_persistence 只重开了一次连接、查了个 COUNT>0
- test_roadshow / test_admin_console / test_http_auth 里的 kill() 全是测试收尾，
  杀完就退，没人看数据活没活下来

所以「重启持久化」这条验收标准此前是靠「SQLite 嘛，肯定在」这句话支撑的。
今晚补一个真跑：起进程 → 干真活 → **SIGKILL 砸掉** → 重新起 → 逐项对账。

额外盯两件容易被忽略的：
1. 审计日志的禁删改触发器，杀进程重启后**还在**（它是 schema 的一部分，
   该随库文件持久化；如果哪天有人改成 init 时重建，这里会红）
2. 下单的库存扣减不会被重复应用（扣减与建单同事务，重启不能二次扣）

运行： .venv/bin/python tests/test_restart.py
"""
import asyncio
import http.cookiejar
import os
import re
import signal
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

DB = os.path.join(tempfile.gettempdir(), "agentmall_test_restart.db")
ADMIN_KEY = "ak_demo_admin_secret"
USER = {"username": "restartuser", "password": "pass123456"}

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


def wait_up(url: str, proc=None, tries: int = 80) -> bool:
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except Exception:
            if proc is not None and proc.poll() is not None:
                return False
            time.sleep(0.25)
    return False


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, path: str, data: dict | None = None, method: str | None = None):
        body = urllib.parse.urlencode(data).encode() if data else None
        r = urllib.request.Request(self.base + path, data=body,
                                   method=method or ("POST" if data else "GET"))
        try:
            resp = self.opener.open(r, timeout=15)
        except urllib.error.HTTPError as e:
            resp = e
        return resp.status, resp.read().decode("utf-8", "ignore"), resp.geturl()


def spawn(web_port: int, mcp_port: int) -> list:
    env = dict(os.environ, AGENTMALL_DB=DB, PYTHONPATH=ROOT,
               AGENTMALL_PUBLIC_URL=f"http://127.0.0.1:{web_port}",
               AGENTMALL_MCP_PORT=str(mcp_port))
    return [
        subprocess.Popen([sys.executable, "-m", "uvicorn", "agentmall.web.app:app",
                          "--port", str(web_port)], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([sys.executable, "-m", "agentmall.http_server"],
                         cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]


def hard_kill(procs: list) -> None:
    """SIGKILL，不是 terminate——模拟进程被砸掉，不给任何优雅退出的机会。"""
    for p in procs:
        try:
            os.kill(p.pid, signal.SIGKILL)
        except (ProcessLookupError, TypeError):
            pass
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


async def mcp(base: str, key: str, tool: str, args: dict):
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
    out = []
    for b in res.content:
        if getattr(b, "text", None):
            try:
                out.append(__import__("json").loads(b.text))
            except ValueError:
                out.append(b.text)
    return out[0] if len(out) == 1 else out


def rows(sql: str, args: tuple = ()) -> list[dict]:
    """绕开项目自己的 db 层，用裸 sqlite3 读——避免「用被测代码验被测代码」。"""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def main() -> int:
    for s in ("", "-wal", "-shm"):
        if os.path.exists(DB + s):
            os.remove(DB + s)

    web_port, mcp_port = free_ports(2)
    web, mcp_base = f"http://127.0.0.1:{web_port}", f"http://127.0.0.1:{mcp_port}"

    print(f"\n{'=' * 64}\n崩溃重启留存（web={web_port} mcp={mcp_port}）\n{'=' * 64}")

    # ---------------------------------------------------------- 第一段：干真活
    procs = spawn(web_port, mcp_port)
    try:
        if not (wait_up(f"{web}/healthz", procs[0]) and wait_up(f"{mcp_base}/healthz", procs[1])):
            print("❌ 服务起不来"); return 1

        c = Client(web)
        st, _, _ = c.req("/register", USER)
        if st != 200:   # 用户已存在（重跑），换名字
            USER["username"] = USER["username"] + str(int(time.time()) % 10000)
            st, _, _ = c.req("/register", USER)
        check(st == 200, f"注册用户 {USER['username']}")

        st, me, _ = c.req("/me")
        m = re.search(r"uk_[A-Za-z0-9_]+", me)
        check(st == 200 and bool(m), "拿到该用户的 API key")
        ukey = m.group(0) if m else ""

        # 挑一个有库存的商品
        cand = [p for p in rows("SELECT id,price,stock FROM products "
                                "WHERE status='在售' AND stock>=2 ORDER BY id")]
        check(bool(cand), "演示商品库里有在售商品")
        pick = cand[0]
        stock_before = int(pick["stock"])

        order = asyncio.run(mcp(mcp_base, ukey, "create_order",
                                {"product_id": pick["id"], "quantity": 2,
                                 "address": "广西南宁市朝阳广场"}))
        oid = (order or {}).get("order_id", "")
        check(bool(re.fullmatch(r"AM[0-9A-F]{10}", oid or "")),
              f"建单成功 {oid} total={order.get('total')}")

        # 支付确认走真实 HTTP 路径
        st, _, _ = c.req(f"/pay/{oid}/confirm", method="POST")
        check(st in (200, 303), f"确认支付 HTTP {st}")

        # 一次带审计的管理员写操作（下架，reason 必填）
        gone = asyncio.run(mcp(mcp_base, ADMIN_KEY, "admin_takedown_product",
                               {"product_id": pick["id"], "reason": "崩溃重启留存测试"}))
        check(isinstance(gone, dict) and "error" not in gone,
              f"管理员下架成功（记审计）：{gone}")

        before = {
            "orders": rows("SELECT COUNT(*) c FROM orders")[0]["c"],
            "products": rows("SELECT COUNT(*) c FROM products")[0]["c"],
            "users": rows("SELECT COUNT(*) c FROM users")[0]["c"],
            "audit": rows("SELECT COUNT(*) c FROM audit_log")[0]["c"],
            "stock": int(rows("SELECT stock FROM products WHERE id=?", (pick["id"],))[0]["stock"]),
            "status": rows("SELECT status FROM products WHERE id=?", (pick["id"],))[0]["status"],
            "order_row": rows("SELECT id,status,total FROM orders WHERE id=?", (oid,))[0],
        }
        check(before["stock"] == stock_before - 2,
              f"库存已扣减 {stock_before} → {before['stock']}（-2）")
        check(before["audit"] > 0, f"审计日志有 {before['audit']} 条")
    finally:
        hard_kill(procs)

    # ---------------------------------------------------------- 砸掉之后
    check(True, f"两个进程已 SIGKILL（不是优雅退出）")
    snap = rows("SELECT id,status FROM orders WHERE id=?", (oid,))
    check(bool(snap), "库文件在进程全死后仍是可读且含该订单（WAL 未损坏）")

    # ---------------------------------------------------------- 重启
    procs2 = spawn(web_port, mcp_port)
    try:
        if not (wait_up(f"{web}/healthz", procs2[0]) and wait_up(f"{mcp_base}/healthz", procs2[1])):
            print("❌ 重启失败"); return 1
        check(True, "重启成功，两个服务都活过来了")

        after = {
            "orders": rows("SELECT COUNT(*) c FROM orders")[0]["c"],
            "products": rows("SELECT COUNT(*) c FROM products")[0]["c"],
            "users": rows("SELECT COUNT(*) c FROM users")[0]["c"],
            "audit": rows("SELECT COUNT(*) c FROM audit_log")[0]["c"],
            "stock": int(rows("SELECT stock FROM products WHERE id=?", (pick["id"],))[0]["stock"]),
            "status": rows("SELECT status FROM products WHERE id=?", (pick["id"],))[0]["status"],
            "order_row": rows("SELECT id,status,total FROM orders WHERE id=?", (oid,))[0],
        }

        for k in ("orders", "products", "users", "audit"):
            check(after[k] == before[k],
                  f"{k} 重启前后一致（{before[k]} → {after[k]}）")
        check(after["order_row"] == before["order_row"],
              f"订单行完整：{after['order_row']}")
        check(after["status"] == "下架",
              f"商品状态仍是「{after['status']}」，没被 seed 冲回在售")
        check(after["stock"] == before["stock"],
              f"库存没被二次扣减（仍为 {after['stock']}）")

        # 崩溃前的用户还能登录
        c2 = Client(web)
        st, _, _ = c2.req("/login", USER)
        check(st in (200, 303), f"崩溃前注册的用户仍能登录（HTTP {st}）")
        st, me2, _ = c2.req("/me")
        check(ukey in me2, "该用户的 api_key 仍有效，/me 能取到接入点")
        st, orders_html, _ = c2.req("/orders")
        check(oid in orders_html, "订单列表里能看到崩溃前下的单")

        # 审计触发器必须还在
        trg = rows("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_log'")
        check(len(trg) >= 2, f"审计禁删改触发器随库留存（{len(trg)} 个）")
        conn = sqlite3.connect(DB)
        try:
            for sql, label in (("DELETE FROM audit_log", "删"),
                               ("UPDATE audit_log SET action='x'", "改")):
                try:
                    conn.execute(sql)
                    ok = False
                except sqlite3.Error:
                    ok = True
                check(ok, f"重启后审计日志仍然{label}不掉")
        finally:
            conn.rollback()
            conn.close()

        # 重启后业务仍能继续跑（不是只读残骸）
        again = asyncio.run(mcp(mcp_base, ADMIN_KEY, "admin_stats", {}))
        check(isinstance(again, dict) and "error" not in again,
              f"重启后 admin_stats 正常：订单 {again.get('orders')} · GMV ¥{again.get('gmv')}")
        check(float(again.get("gmv", 0)) > 0,
              f"重启后 GMV 仍为 ¥{again.get('gmv')}（崩溃前付的款没丢）")
    finally:
        hard_kill(procs2)

    passed = sum(1 for ok, _ in checks if ok)
    print(f"\n{'=' * 64}\n崩溃重启留存 {passed}/{len(checks)} 项通过")
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
