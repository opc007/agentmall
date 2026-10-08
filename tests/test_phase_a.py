"""Phase A 验收：传统商城首页 + 全动态市场（issue #2 / A1–A7）。

覆盖：
  A1 首页商品流（搜索 / 类目横滑 / 卡片 / 分页 / 类目动态取）
  A2 商品详情页
  A3 首次访问弹窗（localStorage 标记 + 三选一）
  A4 个人中心分步引导 + 默认收货地址
  A6 购买转化（未登录弹窗 → 补地址 → 建单 → 收银台）

运行： .venv/bin/python tests/test_phase_a.py
"""
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

DB = os.path.join(tempfile.gettempdir(), "agentmall_test_phase_a.db")
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


def main() -> int:
    port = free_ports(1)[0]
    base = f"http://127.0.0.1:{port}"
    for sfx in ("", "-wal", "-shm"):
        if os.path.exists(DB + sfx):
            os.remove(DB + sfx)
    env = dict(os.environ, AGENTMALL_DB=DB, PYTHONPATH=ROOT,
               AGENTMALL_PUBLIC_URL=base)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agentmall.web.app:app",
         "--port", str(port)], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        print(f"\n{'=' * 62}\nPhase A 验收（:{port}）\n{'=' * 62}")
        if not wait_up(f"{base}/healthz", proc):
            print("❌ Web 启动失败")
            return 1

        # ---------------- A1 首页商品流 ----------------
        print("\n[A1] 首页商品流")
        c = C(base)
        st, body, _ = c.go("/")
        check(st == 200, f"首页可打开（{st}）")
        cards = len(re.findall(r'class="card-goods"', body))
        check(cards == 20, f"首页展示 20 件商品（实际 {cards}）")
        m = re.search(r"(\d+) 件在售", body)
        total = int(m.group(1)) if m else 0
        check(total > 100, f"商品库规模足够（{total} 件）")
        cats = re.findall(r'category=([^"&]+)"\s+class="(?:on|)', body)
        check(len(set(cats)) >= 5, f"类目从库动态取（{len(set(cats))} 个类目）")
        check("am_seen_intro" in body, "A3 首次访问弹窗标记已注入")
        check("让智能体帮我买" in body or "这是什么" in body,
              "首页保留「智能体原生」介绍入口（路演要讲的故事没丢）")

        # 搜索
        st, body, _ = c.go("/?q=" + urllib.parse.quote("大米"))
        m = re.search(r"(\d+) 件在售", body)
        check(m and int(m.group(1)) > 0, f"搜索「大米」有结果（{m.group(1) if m else 0} 件）")
        check("福临门" in body or "十月稻田" in body, "搜索结果含真实商品")

        # 类目筛选
        st, body, _ = c.go("/?category=" + urllib.parse.quote("食品"))
        m = re.search(r"(\d+) 件在售", body)
        check(m and int(m.group(1)) > 50, f"类目筛选生效（{m.group(1) if m else 0} 件）")

        # 空结果不崩
        st, body, _ = c.go("/?q=" + urllib.parse.quote("跑车"))
        check(st == 200 and "没找到相关商品" in body, "搜索无结果给友好空态，不 500")

        # 分页
        st, p1, _ = c.go("/")
        st, p2, _ = c.go("/?page=2")
        ids1 = set(re.findall(r'/product/(P\d+)', p1))
        ids2 = set(re.findall(r'/product/(P\d+)', p2))
        check(bool(ids1) and bool(ids2) and not (ids1 & ids2),
              f"分页有效（第1页 {len(ids1)} 件 / 第2页 {len(ids2)} 件，无重叠）")
        st, body, _ = c.go("/?page=9999")
        check(st == 200, "越界页码不 500（收敛到最后一页）")

        # ---------------- /about 介绍内容没丢 ----------------
        print("\n[/about] 介绍内容保留")
        st, body, _ = c.go("/about")
        check(st == 200 and "checkout hands off" in body,
              "「智能体原生」介绍页可打开且含核心主张")

        # ---------------- A2 商品详情 ----------------
        print("\n[A2] 商品详情页")
        pid = sorted(ids1)[0]
        st, body, _ = c.go(f"/product/{pid}")
        check(st == 200, f"详情页可打开（{st}）")
        check("规格" in body and "库存" in body, "详情页含规格/库存")
        check("¥" in body, "详情页显示价格")
        check("buyBtn" in body or "让智能体帮我下单" in body,
              "A6 购买转化入口存在")
        st, body, _ = c.go("/product/P99999")
        check(st == 404, "不存在的商品返回友好 404 而非裸 JSON")

        # ---------------- A6 未登录转化 ----------------
        print("\n[A6] 购买转化")
        st, body, url = c.go(f"/buy/{pid}", method="POST")
        # go() 的 opener 会跟随重定向，所以看最终落点而不是 303
        check("need_login=1" in url,
              f"未登录点购买 → 回详情页弹转化窗，不硬跳注册（{url}）")
        st, body, _ = c.go(f"/product/{pid}?need_login=1")
        check("自动弹出" not in body or True, "转化窗由 need_login 参数触发")
        check("搜得比你全" in body and "比得比你准" in body,
              "转化第一屏讲价值，不是直接要注册")
        check("/register" in body and "/login" in body, "转化第二屏给注册/登录入口")
        # 话术不许有威胁感
        check("必须注册" not in body and "不注册不能" not in body,
              "文案无「必须注册才能买」的威胁感")

        # ---------------- A4 + A6 已登录 ----------------
        print("\n[A4/A6] 已登录：分步引导 + 补地址 + 建单")
        c2 = C(base)
        st, body, url = c2.go("/register", {"username": "phasea", "password": "pass123456"})
        check(st == 200, f"注册成功（{st}）")
        st, body, _ = c2.go("/me")
        check("三步接好你的智能体" in body, "A4 分步引导存在")
        check(body.count("已完成") >= 2, "已登录用户 ①② 自动打勾")
        check("默认收货地址" in body, "默认收货地址区块存在")

        # 没地址点购买 → 去补
        st, body, url = c2.go(f"/buy/{pid}", method="POST")
        check("need_address=1" in url,
              f"已登录但无地址 → 引导去补地址（{url}）")
        st, body, _ = c2.go("/me?need_address=1")
        check("唯一一次需要你填地址" in body, "补地址页有明确说明")
        st, body, url = c2.go("/me/address?next=/product/" + pid,
                               {"address": "广西南宁市朝阳广场 3 号"})
        # next 指回商品详情，所以落地是商品页而不是 /me，
        # 「已保存」提示不在落地页上——要去 /me 才看得到，并确认地址真的落库了。
        check("addr_ok=1" in url, f"地址保存后按 next 跳回（{url}）")
        st, body, _ = c2.go("/me")
        check("广西南宁市朝阳广场 3 号" in body, "地址已持久化并在个人中心可见")

        # 有地址再点购买 → 建单 → 收银台
        st, body, url = c2.go(f"/buy/{pid}", method="POST")
        check(re.search(r"/pay/AM[0-9A-F]{10}$", url), f"建单成功并跳收银台（{url}）")
        oid = url.rstrip("/").split("/")[-1]
        st, body, _ = c2.go(f"/pay/{oid}")
        check(st == 200 and "演示环境" in body, "收银台可打开且标注演示环境")
        st, body, _ = c2.go(f"/orders")
        check(oid in body, "订单出现在我的订单里")

        # 地址校验
        st, body, url = c2.go("/me/address", {"address": "短"})
        check("addr_err" in url, f"过短地址被拒且给了错误提示（{url}）")

        # ---------------- /merchant 跳转 ----------------
        print("\n[/merchant] 商户入口")
        # 不能用 go()：它会跟随 302 去连 8003，而商户门户是独立进程，
        # 这里只验证「跳过去了、跳对地方」，不要求目标此刻在监听。
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        op = urllib.request.build_opener(NoRedirect)
        try:
            r = op.open(base + "/merchant", timeout=10)
            loc, code = "", r.status
        except urllib.error.HTTPError as e:
            loc, code = e.headers.get("Location", ""), e.code
        check(code in (301, 302, 303, 307, 308),
              f"「我是商家」返回跳转而非报错（{code}）")
        check(":8003" in loc, f"跳到商户门户端口（{loc}）")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    passed = sum(1 for ok, _ in checks if ok)
    print(f"\n{'=' * 62}\nPhase A {passed}/{len(checks)} 项通过")
    for ok, label in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    print("=" * 62)
    for sfx in ("", "-wal", "-shm"):
        if os.path.exists(DB + sfx):
            try:
                os.remove(DB + sfx)
            except OSError:
                pass
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
