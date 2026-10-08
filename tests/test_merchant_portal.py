"""商户门户 + 自助注册端到端验收（Phase A / A5）。

覆盖：注册（成功/重名/密码不一致/太短）、登录（成功/错密码/种子商户）、
登出、未登录拦截、MCP 接入点可复制且 key 与注册一致、
**新商户 key 真能 resolve 出 merchant 角色 + 正确 merchant_id**（最关键的一条）、
跨商户数据不泄漏、挂路径前缀（AGENTMALL_MERCHANT_BASE_PATH）时链接正确。

测试库：/tmp/test_merchant_<pid>.db（**import agentmall.db 之前**设 AGENTMALL_DB），
测完连 -wal/-shm 一起删掉，不碰仓库里的 data/agentmall.db。

运行： .venv/bin/python tests/test_merchant_portal.py
"""
import html
import http.client
import http.cookiejar
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

# ---- 必须在 import agentmall.* 之前设好（db.py 在 import 时读 env）--------
DB = os.path.join(tempfile.gettempdir(), f"test_merchant_{os.getpid()}.db")
os.environ["AGENTMALL_DB"] = DB

from fastapi.testclient import TestClient  # noqa: E402

from agentmall import auth, db  # noqa: E402
from agentmall.roles import merchant_service  # noqa: E402
from agentmall.store import get_store  # noqa: E402
from agentmall.web import merchant_app  # noqa: E402

SEED_NAMES = {"日用优选生活馆", "洁美家居日杂", "个人护理专营店"}
checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> bool:
    checks.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return bool(ok)


def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def rm_db(path: str) -> None:
    for sfx in ("", "-wal", "-shm"):
        try:
            os.remove(path + sfx)
        except OSError:
            pass


def merchant_row(mid: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM merchants WHERE id=?", (mid,)).fetchone()
    return dict(row) if row else None


def merchant_by_name(name: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM merchants WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def mcp_json_of(page: str) -> dict:
    """从门户页面的 <pre id="mcp-cfg"> 里抠出 MCP 接入点 JSON。"""
    m = re.search(r'<pre id="mcp-cfg"[^>]*>(.*?)</pre>', page, re.S)
    if not m:
        return {}
    return json.loads(html.unescape(m.group(1)))


# ---------------------------------------------------------------- 主体用例
def test_portal():
    print("\n" + "=" * 64)
    print("商户门户 + 自助注册验收")
    print("=" * 64)

    rm_db(DB)
    db.init_db()
    with TestClient(merchant_app.app) as client:
        # ---------------- 1. 健康检查 ----------------
        r = client.get("/healthz")
        check(r.status_code == 200 and r.json() == {"ok": True, "service": "agentmall-merchant"},
              f"GET /healthz → {r.status_code} {r.json()}")

        # ---------------- 2. 未登录拦截 ----------------
        r = client.get("/", follow_redirects=False)
        check(r.status_code == 303 and r.headers.get("location") == "/login",
              f"未登录访问 / → 303 {r.headers.get('location')}")
        c = TestClient(merchant_app.app)
        r = c.get("/register")
        check(r.status_code == 200 and "商户自助注册" in r.text, "注册页可打开")
        r = c.get("/login")
        check(r.status_code == 200 and "商户登录" in r.text, "登录页可打开")
        check("演示环境" in c.get("/login").text, "登录页标注演示环境")

        # ---------------- 3. 注册校验（先跑失败分支）----------------
        r = c.post("/register", data={"name": "x", "password": "merchant123",
                                      "confirm": "merchant123"})
        check(r.status_code == 400 and "店名" in r.text,
              f"店名太短被拒（{r.status_code}）")
        r = c.post("/register", data={"name": "店" * 31, "password": "merchant123",
                                      "confirm": "merchant123"})
        check(r.status_code == 400 and "30" in r.text,
              f"店名超 30 字被拒（{r.status_code}）")
        r = c.post("/register", data={"name": "南宁好物日杂", "password": "short",
                                      "confirm": "short"})
        check(r.status_code == 400 and "8 位" in r.text,
              f"密码少于 8 位被拒（{r.status_code}）")
        r = c.post("/register", data={"name": "南宁好物日杂", "password": "merchant123",
                                      "confirm": "merchant999"})
        check(r.status_code == 400 and "不一致" in r.text,
              f"两次密码不一致被拒（{r.status_code}）")
        check(merchant_by_name("南宁好物日杂") is None, "校验失败时没有落库")

        # ---------------- 4. 注册成功 ----------------
        r = c.post("/register", data={"name": "南宁好物日杂", "password": "merchant123",
                                      "confirm": "merchant123"},
                   follow_redirects=False)
        check(r.status_code == 303, f"注册成功 → 303 {r.headers.get('location')}")
        row = merchant_by_name("南宁好物日杂")
        check(row is not None, "注册商户已落库")
        mid, key = row["id"], row["api_key"]
        check(mid.startswith("M") and mid not in ("M001", "M002", "M003"),
              f"merchant_id 合法且不撞种子（{mid}）")
        check(key.startswith("mk_"), f"api_key 前缀是 mk_（{key[:12]}…）")
        check(row["password_hash"].startswith("pbkdf2_sha256$"),
              "password_hash 是 pbkdf2 存储，不是明文")
        check("merchant123" not in json.dumps(row, ensure_ascii=False),
              "库里没有明文口令")
        check(row["status"] == "active", f"注册即可用（status={row['status']}）")
        check(len(c.cookies) > 0, "注册成功即写登录态 cookie")

        # 重名
        c2 = TestClient(merchant_app.app)
        r = c2.post("/register", data={"name": "南宁好物日杂", "password": "another123",
                                       "confirm": "another123"})
        check(r.status_code == 400 and "已被注册" in r.text,
              f"店名重复明确报错（{r.status_code}）")
        again = merchant_by_name("南宁好物日杂")
        check(again["id"] == mid and again["api_key"] == key
              and again["password_hash"] == row["password_hash"],
              "重名时**没有**静默覆盖老商户")

        # ---------------- 5. key 真能连 MCP（最关键）----------------
        actor = auth.resolve_key(key)
        check(bool(actor), "auth.resolve_key(新 key) 能解析")
        check(bool(actor) and actor["role"] == "merchant",
              f"role=merchant（实际 {actor and actor['role']}）")
        check(bool(actor) and actor["merchant_id"] == mid and actor["actor_id"] == mid,
              f"merchant_id 归属正确（{actor and actor['merchant_id']} == {mid}）")
        merchant_tools = auth.ROLE_TOOLS["merchant"]
        check(all(t in merchant_tools for t in (
            "merchant_publish_product", "merchant_update_product",
            "merchant_list_products", "merchant_list_orders",
            "merchant_fulfill_order")),
            f"merchant 角色的 5 个 tool 已开放（{len(merchant_tools)} 个）")
        check(auth.resolve_key("mk_不存在的key") is None, "无效 key 解析为 None")
        check(auth.resolve_key(auth.new_api_key("mk")) is None, "乱猜的 key 解析为 None")

        # ---------------- 6. 门户首页 ----------------
        r = c.get("/")
        page = r.text
        check(r.status_code == 200, f"登录后门户首页 200（{r.status_code}）")
        check("演示环境" in page and "不发生任何真实交易" in page, "门户醒目标注演示环境")
        check(mid in page, "门户显示自己的 merchant_id")
        check("南宁好物日杂" in page, "门户显示店名")
        check(key in page, "门户明文展示自己的 api_key")
        cfg = mcp_json_of(page)
        srv = cfg.get("mcpServers", {}).get("agentmall-merchant", {})
        check(bool(srv), "门户给出可直接复制的 MCP 接入点 JSON")
        check(srv.get("headers", {}).get("Authorization") == f"Bearer {key}",
              "接入点 JSON 里的 Bearer key 与注册时一致")
        check(srv.get("url", "").endswith("/mcp"), f"接入点 url 指向 MCP（{srv.get('url')}）")
        check(re.search(r"/mcp[\s\S]{0,400}" + re.escape(key), page) is not None,
              "页面上接入点 JSON 与 key 出现在同一块可复制区域")
        for tool in ("merchant_publish_product", "merchant_update_product",
                     "merchant_list_products", "merchant_list_orders",
                     "merchant_fulfill_order"):
            check(tool in page, f"能力指引含 {tool}")
        check("待审核" in page and "管理员" in page, "能力指引讲清「待审核」这件事")
        check("不传就不改" in page, "能力指引讲清 price/stock 不传就不改")
        check("演示环境 · 演示数据 · 模拟支付" in page, "横幅文案与约定一致")

        # 店铺信息四件套
        check(re.search(r"商品数", page) and re.search(r"订单数", page)
              and re.search(r"注册时间", page), "门户显示 merchant_id/店名/注册时间/商品数/订单数")

        # ---------------- 7. 跨商户数据隔离 ----------------
        store = get_store()
        seed_products = [p for p in store.search(keyword="抽纸", limit=5)
                         if p.get("merchant_id") == "M001"]
        if seed_products:
            o = store.create_order(seed_products[0]["id"], 1, "广西南宁市西乡塘区某路1号")
            oid = o.get("order_id") or o.get("id")
            check(bool(oid), f"造了一单 M001 的订单用于泄漏检测（{oid}）")
            r = c.get("/")
            page2 = r.text
            check(oid not in page2, "别家（M001）订单号不出现在新商户门户")
            check(re.search(r"订单数\s*</div>\s*<div class=\"v\">\s*0", page2) is not None,
                  "新商户订单数=0（别人的单不算他的）")
            mine = merchant_service.list_orders(
                {"role": "merchant", "actor_id": mid, "merchant_id": mid})
            check(all(o2.get("error") or o2.get("order_id") != oid for o2 in mine),
                  "MerchantService 也没把 M001 的订单给新商户")
        for nm in SEED_NAMES:
            row_s = merchant_by_name(nm)
            if row_s:
                check(row_s["api_key"] not in c.get("/").text,
                      f"种子商户 {row_s['id']} 的 api_key 不出现在新商户门户")

        # 商户自己上架一件 → 商品数 +1（待审核）
        pub = merchant_service.publish_product(
            {"role": "merchant", "actor_id": mid, "merchant_id": mid},
            "南宁柠檬茶原料包", "食品", 19.9, 200, "件", "演示")
        check(pub.get("status") == "待审核",
              f"商户上架商品进待审核（{pub.get('status')}）")
        pid = pub.get("product_id")
        check(bool(pid), f"拿到 product_id（{pid}）")
        with db.connect() as conn:
            owner = conn.execute("SELECT merchant_id FROM products WHERE id=?",
                                 (pid,)).fetchone()["merchant_id"]
        check(owner == mid, f"商品归属本商户（{owner} == {mid}）")
        page3 = c.get("/").text
        check(re.search(r"商品数\s*</div>\s*<div class=\"v\">\s*1", page3) is not None,
              "本商户上架 1 件后，门户商品数=1")

        # ---------------- 8. 登录 ----------------
        c3 = TestClient(merchant_app.app)
        r = c3.get("/", follow_redirects=False)
        check(r.status_code == 303 and r.headers.get("location") == "/login",
              "未登录访问 / 落回 /login")
        r = c3.post("/login", data={"name": "南宁好物日杂", "password": "wrong-password"},
                    follow_redirects=False)
        check(r.status_code == 401 and "错误" in r.text,
              f"错误密码不通过（{r.status_code}）")
        r = c3.get("/", follow_redirects=False)
        check(r.status_code == 303, "登录失败后仍被挡在门外")
        r = c3.post("/login", data={"name": "不存在的店", "password": "whatever123"},
                    follow_redirects=False)
        check(r.status_code == 401 and "错误" in r.text, "不存在的店名也给同样提示（不枚举店名）")
        r = c3.post("/login", data={"name": "南宁好物日杂", "password": "merchant123"},
                    follow_redirects=False)
        check(r.status_code == 303 and r.headers.get("location") == "/",
              f"登录成功 → 303 {r.headers.get('location')}")
        page4 = c3.get("/").text
        check(key in page4 and mcp_json_of(page4)["mcpServers"]["agentmall-merchant"]
              ["headers"]["Authorization"] == f"Bearer {key}",
              "登录后门户的 MCP 接入点 key 与注册时一致")
        check("南宁好物日杂" in page4, "登录后门户显示本店信息")

        # ---------------- 9. 种子商户登录：友好提示，不是 500 ----------------
        c4 = TestClient(merchant_app.app)
        r = c4.post("/login", data={"name": "日用优选生活馆", "password": "whatever123"},
                    follow_redirects=False)
        seed_ok = check(r.status_code == 401 and "API key" in r.text
                        and "种子" in r.text,
                        f"M001（password_hash 为空）登录给出友好提示（{r.status_code}）")
        check(seed_ok and "Traceback" not in r.text
              and "Internal Server Error" not in r.text, "种子商户登录不是 500 裸错误")
        check(len(c4.cookies) == 0, "种子商户登录失败时没有拿到登录态")
        r = c4.get("/", follow_redirects=False)
        check(r.status_code == 303, "种子商户拿不到门户内容")
        # 种子商户的 password_hash 确实为空（别被人偷偷塞了口令）
        check(merchant_by_name("日用优选生活馆")["password_hash"] == "",
              "种子商户 password_hash 仍为空（没被注册流程改写）")

        # ---------------- 10. 登出 ----------------
        r = c.post("/logout", follow_redirects=False)
        check(r.status_code == 303 and r.headers.get("location") == "/login",
              f"登出 → 303 {r.headers.get('location')}")
        r = c.get("/", follow_redirects=False)
        check(r.status_code == 303 and r.headers.get("location") == "/login",
              "登出后访问门户又被挡回登录页")

        # ---------------- 11. 404 不是裸 JSON ----------------
        r = c.get("/no-such-page")
        check(r.status_code == 404 and "页面不存在" in r.text and "Internal Server Error"
              not in r.text, "404 返回友好页面而不是裸 JSON")
        r = c.get("/no-such-page", headers={"accept": "application/json"})
        check(r.status_code == 404, "未知路径仍然 404")

    # ---------------- 12. 已登录后再访问 /register /login 会回工作台 -------
    c5 = TestClient(merchant_app.app)
    c5.post("/login", data={"name": "南宁好物日杂", "password": "merchant123"},
            follow_redirects=False)
    r = c5.get("/register", follow_redirects=False)
    check(r.status_code == 303 and r.headers.get("location") == "/",
          "已登录访问 /register 回工作台（不重复开店）")


# ---------------------------------------------------------------- 路径前缀
class _StripProxy(BaseHTTPRequestHandler):
    """模拟 nginx：``location /shop/ { proxy_pass http://merchant:8003/; }``

    浏览器看到的是 /shop/xxx，转给应用时前缀被剥掉。这个小代理把真实链路补上，
    否则测试只能「假装」剥前缀——cookie 的 Path=/shop 就会暴露问题（见下）。
    """

    upstream = ("127.0.0.1", 0)
    prefix = "/shop"

    def _proxy(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        path = self.path[len(self.prefix):] or "/"
        conn = http.client.HTTPConnection(*self.upstream, timeout=10)
        try:
            conn.request(method, path, body=body, headers={
                k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "content-length")})
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "content-length", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        finally:
            conn.close()

    def do_GET(self):  # noqa: N802
        self._proxy("GET")

    def do_POST(self):  # noqa: N802
        self._proxy("POST")

    def log_message(self, *a):  # 静音，别把测试输出冲掉
        pass


def test_base_path():
    """挂到 /shop 前缀下时，内部链接必须带前缀、且不能双前缀。

    背景：用户面/管理后台/商户门户**共用 /login、/logout 这些路由名**。
    nginx 的 proxy_pass 剥掉前缀再转发，应用若还生成根绝对链接，
    商户点「退出」就会跳到买家那边的退出路由。这里锁死这个回归。
    """
    print("\n" + "-" * 64)
    print("路径前缀回归（AGENTMALL_MERCHANT_BASE_PATH=/shop + nginx 剥前缀）")
    print("-" * 64)
    app_port = free_port()
    proxy_port = free_port()
    direct = f"http://127.0.0.1:{app_port}"
    base = f"http://127.0.0.1:{proxy_port}/shop"   # 浏览器视角（带前缀）
    db_path = os.path.join(tempfile.gettempdir(),
                           f"test_merchant_bp_{os.getpid()}.db")
    rm_db(db_path)
    env = dict(os.environ, AGENTMALL_DB=db_path, PYTHONPATH=ROOT,
               AGENTMALL_MERCHANT_BASE_PATH="/shop")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agentmall.web.merchant_app:app",
         "--port", str(app_port)], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    server = None
    try:
        up = False
        for _ in range(80):
            try:
                urllib.request.urlopen(f"{direct}/healthz", timeout=1).read()
                up = True
                break
            except Exception:
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
        if not check(up, "带前缀的商户门户启动成功"):
            return

        _StripProxy.upstream = ("127.0.0.1", app_port)
        server = ThreadingHTTPServer(("127.0.0.1", proxy_port), _StripProxy)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def go(path, data=None):
            body = urlencode(data).encode() if data else None
            req = urllib.request.Request(base + path, data=body,
                                         method="POST" if data else "GET")
            try:
                resp = op.open(req, timeout=10)
            except urllib.error.HTTPError as e:
                resp = e
            return resp.status, resp.read().decode("utf-8", "ignore"), resp.geturl()

        # 1. 未登录 → 落到带前缀的登录页
        st, body, url = go("/")
        check(url.endswith("/shop/login"), f"未登录 → /shop/login（{url}）")
        check("商户登录" in body, "带前缀下登录页可打开")

        # 2. 注册页链接带前缀、无双前缀
        st, body, url = go("/register")
        links = set(re.findall(r'(?:href|action)="([^"]*)"', body))
        shop_links = sorted(l for l in links if l.startswith("/"))
        check(shop_links and all(l.startswith("/shop/") for l in shop_links),
              f"注册页链接全部带 /shop 前缀（{shop_links}）")
        check("/shop/shop" not in body, "无双前缀（不会与 nginx sub_filter 叠加炸掉）")

        # 3. 注册（Cookie 的 Path 必须也是 /shop，否则浏览器根本不会回传，
        #    表现就是「注册完又被弹回登录页」——真实部署最常见的坑）
        st, body, url = go("/register", {"name": "前缀测试店",
                                         "password": "merchant123",
                                         "confirm": "merchant123"})
        check(url.split("?")[0].endswith("/shop/"),
              f"注册后落回带前缀的门户（{url.split('?')[0]}）")
        check(len(jar) > 0, "带前缀部署时登录 cookie 也写上了")
        check(all(c.path == "/shop" for c in jar),
              f"cookie 的 Path 跟着前缀走（{[c.path for c in jar]}）")

        # 4. 门户页面：链接/接入点/退出表单
        st, body, url = go("/")
        check(st == 200 and "mcpServers" in body,
              f"带前缀下门户首页可打开（{st}）")
        links = set(re.findall(r'(?:href|action)="([^"]*)"', body))
        shop_links = sorted(l for l in links if l.startswith("/"))
        check(shop_links and all(l.startswith("/shop/") for l in shop_links),
              f"门户链接全部带 /shop 前缀（{shop_links}）")
        check("/shop/shop" not in body, "门户无双前缀")
        check("agentmall-merchant" in body and "Bearer mk_" in body,
              "带前缀下 MCP 接入点照常展示")
        logout_action = re.findall(r'<form method="post" action="([^"]+)"', body)
        check(bool(logout_action) and all(a.startswith("/shop/") for a in logout_action),
              f"退出表单 action 带前缀（{logout_action}）")

        # 5. 登出也走带前缀的路由
        st, body, url = go("/logout", {})
        check(url.endswith("/shop/login"), f"带前缀下登出回登录页（{url}）")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        rm_db(db_path)


def main() -> int:
    try:
        test_portal()
        test_base_path()
    finally:
        rm_db(DB)
        rm_db(os.path.join(tempfile.gettempdir(),
                            f"test_merchant_bp_{os.getpid()}.db"))

    passed = sum(1 for ok, _ in checks if ok)
    print("\n" + "=" * 64)
    print(f"商户门户 {passed}/{len(checks)} 项通过")
    for ok, label in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    print("=" * 64)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
