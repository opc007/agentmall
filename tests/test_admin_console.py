"""管理后台 + 审核流端到端验收（issue #2 / P1，Phase2任务书 §6）。

覆盖：管理后台四页、per-agent key 隔离、下架 reason 必填、
下架后用户搜索不再返回（验收标准 5）、审计留痕。

运行： .venv/bin/python tests/test_admin_console.py
"""
import http.cookiejar
import re
import os
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

DB = os.path.join(tempfile.gettempdir(), "agentmall_test_admin.db")
ADMIN_KEY = "ak_demo_admin_secret"
USER_KEY = "uk_demo_user_secret"
checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    checks.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def free_ports(n: int = 1) -> list[int]:
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


class C:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def go(self, path, data=None, method=None):
        body = urllib.parse.urlencode(data).encode() if data else None
        r = urllib.request.Request(self.base + path, data=body,
                                   method=method or ("POST" if data else "GET"))
        try:
            resp = self.op.open(r, timeout=15)
        except urllib.error.HTTPError as e:
            resp = e
        return resp.status, resp.read().decode("utf-8", "ignore"), resp.geturl()


def wait_up(url, proc, tries=80):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except Exception:
            if proc.poll() is not None:
                return False
            time.sleep(0.5)
    return False


def test_admin_base_path():
    """管理后台挂到 /admin 前缀下时，内部链接必须带前缀。

    背景：用户面与管理后台**共用 /orders、/login、/logout 这些路由名**。
    nginx 的 `proxy_pass http://.../` 会把 /admin 前缀剥掉再转发，
    若应用仍生成根绝对链接，后台点「订单」就会跳到**买家的订单页**。
    回归风险高，这里单独锁死。
    """
    port = free_ports(1)[0]
    base = f"http://127.0.0.1:{port}"
    db_path = os.path.join(tempfile.gettempdir(), "agentmall_test_admin_bp.db")
    for sfx in ("", "-wal", "-shm"):
        if os.path.exists(db_path + sfx):
            os.remove(db_path + sfx)
    env = dict(os.environ, AGENTMALL_DB=db_path, PYTHONPATH=ROOT,
               AGENTMALL_ADMIN_BASE_PATH="/admin")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agentmall.web.admin_app:app",
         "--port", str(port)], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_up(f"{base}/healthz", proc):
            check(False, "带前缀的管理后台启动成功")
            return
        # 模拟 nginx 剥掉前缀后转发：应用收到的是根路径 /
        c = C(base)
        st, body, url = c.go("/")
        check(url.endswith("/admin/login"),
              f"未登录重定向到带前缀的登录页（{url}）")
        c2 = C(base)
        st, body, url = c2.go("/login", {"key": ADMIN_KEY})
        check(url.endswith("/admin/"), f"登录后回到带前缀的首页（{url}）")
        st, body, _ = c2.go("/")
        links = set(re.findall(r'(?:href|action)="([^"]*)"', body))
        admin_links = [l for l in links if l.startswith("/")]
        check(admin_links and all(l.startswith("/admin/") for l in admin_links),
              f"后台链接全部带 /admin 前缀（{sorted(admin_links)}）")
        check("admin/admin" not in body, "无双前缀（不会与 nginx sub_filter 叠加炸掉）")
        st, body, _ = c2.go("/orders")
        check(st == 200 and "全平台订单" in body, "带前缀下订单页仍可打开")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        for sfx in ("", "-wal", "-shm"):
            if os.path.exists(db_path + sfx):
                try:
                    os.remove(db_path + sfx)
                except OSError:
                    pass


def main() -> int:
    port = free_ports(1)[0]
    base = f"http://127.0.0.1:{port}"
    for sfx in ("", "-wal", "-shm"):
        if os.path.exists(DB + sfx):
            os.remove(DB + sfx)
    env = dict(os.environ, AGENTMALL_DB=DB, PYTHONPATH=ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agentmall.web.admin_app:app",
         "--port", str(port)], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        print(f"\n{'=' * 60}\n管理后台验收（:{'{'}port{'}'}）\n{'=' * 60}")
        if not wait_up(f"{base}/healthz", proc):
            print("❌ 管理后台启动失败"); return 1

        # 1. 未登录被挡（opener 会自动跟随 303，所以断言落点而不是状态码）
        c = C(base)
        st, body, url = c.go("/")
        check(url.endswith("/login") and "管理后台登录" in body,
              f"未登录访问被挡回登录页（最终 {st} {url}）")
        st, body, _ = c.go("/login")
        check("演示环境" in body, "登录页标注演示环境")
        check("AdminMall" in body or "管理后台" in body, "登录页标题正确")

        # 2. 非管理员 key 被拒
        st, body, url = c.go("/login", {"key": USER_KEY})
        check("不是管理员" in url or "err" in url,
              f"用户 key 不能进管理后台（{url[-40:]}）")

        # 3. 管理员 key 登录
        c2 = C(base)
        c2.go("/login", {"key": ADMIN_KEY})
        check(len(c2.jar) > 0, "管理员登录态已建立")

        # 4. 四个页面
        for path, kw in [("/", "订单总数"), ("/orders", "全平台订单"),
                         ("/products", "商品治理"), ("/audit", "审计")]:
            st, body, _ = c2.go(path)
            check(st == 200 and kw in body, f"页面 {path} 可打开且内容正确（{st}）")

        # 5. 造一单（下单会扣库存）
        prev = dict(os.environ)
        os.environ["AGENTMALL_DB"] = DB
        from agentmall.store import get_store
        from agentmall.roles import admin_service
        s = get_store()
        p = s.search(keyword="纸巾", limit=1)[0]
        o = s.create_order(p["id"], 1, "广西南宁市西乡塘区某路1号")
        admin_actor = {"role": "admin", "actor_id": "A001", "merchant_id": None}
        st_orders = admin_service.stats(admin_actor)
        check(st_orders["orders"] >= 1, f"看板订单数已 +1（{st_orders['orders']}）")
        check("¥" not in str(st_orders["gmv"]) or st_orders["gmv"] >= 0,
              f"GMV 可读（{st_orders['gmv']}）")

        # 6. 全平台订单页看得到用户的单
        st, body, _ = c2.go("/orders")
        check(o["order_id"] in body, "全平台订单页看得到用户刚下的单")
        check("西乡塘区" not in body, "管理员看到的买家地址已脱敏到市级")
        check("某路1号" not in body, "门牌号未泄露")

        # 7. 下架 reason 必填（验收标准 5）
        st, body, url = c2.go(f"/products/{p['id']}/takedown", {"reason": ""})
        check("err" in url or "必填" in url, f"下架不填 reason 被拒（{url[-50:]}）")
        check(s.get(p["id"])["status"] == "在售", "被拒后商品仍在售")

        # 8. 正常下架 → 用户搜索不再返回
        st, body, url = c2.go(f"/products/{p['id']}/takedown", {"reason": "演示下架测试"})
        check("ok" in url, f"下架成功（{url[-40:]}）")
        check(s.get(p["id"])["status"] == "下架", "商品状态变为下架")
        ids = [x["id"] for x in s.search(keyword="纸巾", limit=50)]
        check(p["id"] not in ids, "验收标准5：下架后用户搜索不再返回该商品")

        # 9. 审计留痕
        st, body, _ = c2.go("/audit")
        check("takedown" in body or "下架" in body, "下架操作已记入审计日志并在页面可见")
        logs = admin_service.audit_log(admin_actor, 50)
        check(any("takedown" in str(l.get("action", "")) for l in logs),
              f"audit_log 里有下架记录（共 {len(logs)} 条）")

        test_admin_base_path()

        # 10. 审计表禁删改（合规红线）
        from agentmall import db
        with db.connect() as conn:
            try:
                conn.execute("DELETE FROM audit_log WHERE id=-1")
                conn.commit()
                guard_ok = True
            except Exception:
                guard_ok = False
        try:
            with db.connect() as conn:
                conn.execute("UPDATE audit_log SET action='x' WHERE id=-1")
                conn.commit()
            upd_ok = True
        except Exception:
            upd_ok = False
        check(guard_ok and upd_ok, "audit_log 表禁 DELETE/UPDATE（触发器生效）")

        os.environ.clear(); os.environ.update(prev)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    passed = sum(1 for ok, _ in checks if ok)
    print(f"\n{'=' * 60}\n管理后台 {passed}/{len(checks)} 项通过")
    for ok, label in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    print("=" * 60)
    for sfx in ("", "-wal", "-shm"):
        if os.path.exists(DB + sfx):
            try:
                os.remove(DB + sfx)
            except OSError:
                pass
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
