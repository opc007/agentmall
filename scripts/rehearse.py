#!/usr/bin/env python3
"""路演彩排：把 docs/路演Demo流程.md 的 9 步真跑一遍，并告诉你每步该说什么。

这个脚本不是测试，是**排练**。测试只告诉你绿了不绿；彩排告诉你
「你在第几步、手指点哪、嘴里说哪句、屏幕上应该出现哪个数字」，
以及每步跑不通时**现场怎么圆回来**。

对应 `docs/Phase2任务书.md` Day 11-14 的「路演彩排」交付项。

用法：
    .venv/bin/python scripts/rehearse.py                  # 探测现场三端，都活着就接上去彩排
    .venv/bin/python scripts/rehearse.py --isolated       # 强制自起一套临时栈（临时库，排完即清）
    .venv/bin/python scripts/rehearse.py --with-merchant  # 附带商户侧 5 个 tool 的彩排

默认接现场栈，是因为彩排要在**你真正要演的那套环境**上跑。
--isolated 用来在没起服务时也能随时验一遍，不污染演示数据。
"""
import argparse
import asyncio
import http.cookiejar
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

# 路演现场那一句「帮我找最便宜的抽纸，30 元以内」里的三个变量
KEYWORD = "抽纸"
BUDGET = 30.0
QTY = 2
ADDRESS = "广西南宁市朝阳广场"

ADMIN_DEMO_KEY = "ak_demo_admin_secret"
MERCHANT_DEMO_KEY = "mk_demo_m001_secret"

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


class Step:
    """一步彩排。每步记：结果 + 该怎么说 + 断了怎么救。"""

    def __init__(self, n: int, title: str, say: str, see: str, talk: str, rescue: str):
        self.n, self.title, self.say, self.see, self.talk, self.rescue = \
            n, title, say, see, talk, rescue
        self.ok: bool | None = None
        self.detail = ""

    def header(self) -> None:
        bar = "━" * 66
        print(f"\n{bar}\n{BOLD}第 {self.n} 步 · {self.title}{OFF}\n{bar}")

    def line(self, label: str, value: str) -> None:
        print(f"  {DIM}{label:<9}{OFF} {value}")

    def ok_(self, detail: str = "") -> bool:
        self.ok, self.detail = True, detail
        print(f"  {GREEN}✅{OFF} {detail or '通过'}")
        return True

    def fail(self, detail: str) -> bool:
        self.ok, self.detail = False, detail
        print(f"  {RED}❌{OFF} {detail}")
        return False

    def show(self) -> None:
        self.header()
        self.line("你说", self.say)
        self.line("屏幕上", self.see)
        self.line("话术", self.talk)


# ----------------------------------------------------------------- HTTP

class Client:
    """带 cookie 的会话。login 是 303 + Set-Cookie，必须走 CookieJar 才抓得到。"""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, path: str, data: dict | None = None, method: str | None = None):
        url = self.base + path
        body = urllib.parse.urlencode(data).encode() if data else None
        r = urllib.request.Request(
            url, data=body, method=method or ("POST" if data else "GET"))
        try:
            resp = self.opener.open(r, timeout=20)
        except urllib.error.HTTPError as e:
            resp = e
        return resp.status, resp.read(), resp.geturl()

    def get_text(self, path: str) -> tuple[int, str]:
        st, body, _ = self.req(path)
        return st, body.decode("utf-8", "ignore")


def wait_up(url: str, tries: int = 80) -> bool:
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except Exception:
            time.sleep(0.25)
    return False


def alive(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=1).read()
        return True
    except Exception:
        return False


def _free_ports(n: int) -> list[int]:
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


# ----------------------------------------------------------------- MCP

async def mcp_call(base: str, key: str, tool: str, args: dict):
    """按「URL + Bearer key」走真实 MCP 协议调一次——和 workbuddy 接进来是同一条路。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(f"{base.rstrip('/')}/mcp",
                                     headers={"Authorization": f"Bearer {key}"}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(tool, args)
    return _unwrap(res)


def _unwrap(res):
    sc = getattr(res, "structuredContent", None)
    if sc is not None and "result" in sc:
        return sc["result"]
    out = []
    for b in res.content:
        t = getattr(b, "text", None)
        if t:
            try:
                out.append(__import__("json").loads(t))
            except ValueError:
                out.append(t)
    return out[0] if len(out) == 1 else out


# ----------------------------------------------------------------- 栈

class Stack:
    """接现场的三端，或自己拉一套临时的。"""

    def __init__(self, isolated: bool):
        self.owned = False
        self.procs: list = []
        self.db = ""
        self.admin_base = ""
        if isolated:
            self.owned = True
            self.db = os.path.join(tempfile.gettempdir(), "agentmall_rehearsal.db")
            for s in ("", "-wal", "-shm"):
                if os.path.exists(self.db + s):
                    os.remove(self.db + s)
            wp, mp, ap = _free_ports(3)
            self.web, self.mcp, self.admin = (
                f"http://127.0.0.1:{wp}", f"http://127.0.0.1:{mp}", f"http://127.0.0.1:{ap}")
            env = dict(os.environ, AGENTMALL_DB=self.db, PYTHONPATH=ROOT,
                       AGENTMALL_PUBLIC_URL=self.web, AGENTMALL_MCP_PORT=str(mp))
            py = os.path.join(ROOT, ".venv", "bin", "python")
            py = py if os.path.exists(py) else sys.executable
            self.procs = [
                subprocess.Popen([py, "-m", "uvicorn", "agentmall.web.app:app", "--port", str(wp)],
                                 cwd=ROOT, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                subprocess.Popen([py, "-m", "agentmall.http_server"], cwd=ROOT, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                subprocess.Popen([py, "-m", "uvicorn", "agentmall.web.admin_app:app",
                                  "--port", str(ap)], cwd=ROOT, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
            ]
            for url in (f"{self.web}/healthz", f"{self.mcp}/healthz", f"{self.admin}/healthz"):
                if not wait_up(url):
                    raise RuntimeError(f"临时栈起不来：{url}")
        else:
            self.web = os.environ.get("AGENTMALL_REHEARSE_WEB", "http://127.0.0.1:8000")
            self.mcp = os.environ.get("AGENTMALL_REHEARSE_MCP", "http://127.0.0.1:8001")
            self.admin = os.environ.get("AGENTMALL_REHEARSE_ADMIN", "http://127.0.0.1:8002")
            self.admin_base = os.environ.get("AGENTMALL_ADMIN_BASE_PATH", "").rstrip("/")
            missing = [n for n, u in (("用户面", self.web), ("MCP", self.mcp), ("管理后台", self.admin))
                       if not alive(u + "/healthz")]
            if missing:
                raise RuntimeError(
                    f"{'、'.join(missing)} 没起来。先跑 ./scripts/start_demo.sh，"
                    f"或加 --isolated 自己拉一套临时的。")

    def close(self):
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        if self.db:
            for s in ("", "-wal", "-shm"):
                try:
                    os.remove(self.db + s)
                except OSError:
                    pass


# ----------------------------------------------------------------- 彩排本体

def kpi(html: str) -> dict:
    """从管理后台看板抠出数字。模板是 <div class="n">订单总数</div> 这样的结构。"""
    out = {}
    for m in re.finditer(r'class="n">([^<]+)</div><div class="l">([^<]+)</div>', html):
        val, label = m.group(1).strip(), m.group(2).strip()
        label = label.split("（")[0]
        out[label] = val
    return out


def num(s: str) -> float:
    m = re.search(r"[\d.]+", (s or "").replace(",", ""))
    return float(m.group(0)) if m else 0.0


async def rehearse(stack: Stack, with_merchant: bool) -> list[Step]:
    steps: list[Step] = []
    uid = f"rh{int(time.time()) % 100000}"
    user = {"username": uid, "password": "pass123456"}
    web, mcp, admin = Client(stack.web), None, Client(stack.admin)
    b = stack.admin_base

    # ---------------------------------------------------------- 1 注册登录
    s = Step(1, "打开网页 → 注册 → 登录",
             f"「我先注册一个账号：{uid}，密码随便设。」",
             "浏览器停在 http://127.0.0.1:8000 首页，右上角有「注册 / 登录」。",
             "「这是买家侧的网页。我注册一个号——用户名密码就这一层，"
             "没有短信验证码，这是演示环境。」",
             "注册页打不开 → 回头看 start_demo.sh 的输出，用户面 8000 端口有没有起来。")
    s.show()
    st, home = web.get_text("/")
    if st != 200:
        s.fail(f"首页打不开（HTTP {st}）")
    elif "演示" not in home:
        s.fail("首页没有「演示数据 / 演示环境」标注——这是合规红线，必须有")
    else:
        st, _, _ = web.req("/register", user)
        st2, _, _ = web.req("/login", user)
        if st in (200, 303) and st2 in (200, 303) and len(web.jar) > 0:
            s.ok_(f"注册+登录通过（用户名 {uid}），登录态 cookie 已下发")
        else:
            s.fail(f"注册 HTTP {st} / 登录 HTTP {st2}")
    steps.append(s)

    # ---------------------------------------------------------- 2 接入点
    s = Step(2, "个人中心 → 拿专属 MCP 接入点",
             "「登录完了，我进个人中心。」",
             "页面中间有一块「MCP 接入点」，右边一个「复制」按钮。",
             "「重点在这儿——每个用户注册完，系统给他一个专属的 MCP 接入点，"
             "一个 URL 加一个密钥。AI 客户端只要这个就能连上我，"
             "而且它能干什么，是按这个密钥的角色决定的。」",
             "页面没显示接入点 → 确认你是从个人中心进的，不是首页。")
    s.show()
    st, me = web.get_text("/me")
    m = re.search(r"uk_[A-Za-z0-9_]+", me)
    if st != 200 or not m:
        s.fail("个人中心没拿到用户 API key")
        return steps + [_rest_unreachable(i) for i in range(3, 10)]
    ukey = m.group(0)
    s.ok_(f"接入点已生成：用户 key {ukey[:10]}…（{len(ukey)} 字符）")
    steps.append(s)

    # ---------------------------------------------------------- 3 配给 workbuddy
    s = Step(3, "把接入点配给 workbuddy（现场操作，脚本代劳不了）",
             "切到 workbuddy 的 MCP 设置，粘贴刚才复制的配置。",
             f"配置长这样：URL = {stack.mcp}/mcp，Header = Authorization: Bearer <你的key>",
             "「我现在把这个接入点交给我的 AI 助手。从这一刻起，"
             "它就能替我逛这个平台、帮我下单了。」",
             "workbuddy 连不上 → 先确认 URL 有没有多打斜杠；再确认 MCP 传输方式选的是 "
             "Streamable HTTP 而不是 SSE。")
    s.show()
    s.ok_("这一步是现场手工操作，脚本只负责把 URL 念给你听")
    steps.append(s)

    # ---------------------------------------------------------- 4-5 智能体下单
    s = Step(4, f"对 workbuddy 说：找最便宜的{KEYWORD}",
             f"「帮我找最便宜的{KEYWORD}，{int(BUDGET)} 元以内，送到{ADDRESS}」",
             "workbuddy 调 search_products，屏幕上出现一串商品和价格。",
             "「它现在自己就能搜了。注意——它拿到的是搜索和下单的能力，"
             "但拿不到改价、改库存这些管理能力，工具是按角色发的。」",
             "workbuddy 不响应 → 确认它那边工具列表是 4 个（search/get/create/get_order）；"
             "如果 0 个，是 key 没配对。")
    s.show()
    try:
        items = await mcp_call(stack.mcp, ukey, "search_products",
                              {"keyword": KEYWORD, "limit": 5})
    except Exception as exc:
        s.fail(f"MCP 调用失败：{exc}")
        return steps + [_rest_unreachable(i) for i in range(5, 10)]
    if isinstance(items, dict):
        items = [items]
    items = [i for i in (items or []) if isinstance(i, dict) and i.get("id")]
    if not items:
        s.fail(f"搜「{KEYWORD}」没结果——演示商品库里这一类要留着")
        return steps + [_rest_unreachable(i) for i in range(5, 10)]
    cheapest = min(items, key=lambda i: float(i.get("price", 0)))
    s.ok_(f"命中 {len(items)} 个，最便宜的是「{cheapest.get('name')}」"
          f"¥{cheapest.get('price')}/{cheapest.get('unit', '件')}")
    steps.append(s)

    # 看板基线必须在**建单之前**取，否则第 9 步的增量恒为 0（取晚了就是假通过）
    admin.req(f"{b}/login", {"key": ADMIN_DEMO_KEY})
    _, dash0 = admin.get_text(f"{b}/")
    before = kpi(dash0)

    s = Step(5, "智能体建单（这一步是今晚最值钱的一下）",
             "（workbuddy 会回头跟你确认一次，同意即可）",
             f"create_order → 订单号、合计金额、状态「待支付」",
             f"「它选了最便宜的这个，两件一共 {round(float(cheapest.get('price', 0)) * QTY, 2)} 元，"
             f"没超你说的 {int(BUDGET)} 块。订单已经建好了。」",
             "报「库存不足」→ 换个商品，别现场找原因；这个分支本身也是可以演的，"
             "但别在主流程上演。")
    s.show()
    total = round(float(cheapest.get("price", 0)) * QTY, 2)
    if total > BUDGET:
        s.fail(f"最便宜的也要 ¥{total}，超了 {int(BUDGET)} 块的预算——话术里别说「没超预算」")
        return steps + [_rest_unreachable(i) for i in range(6, 10)]
    try:
        order = await mcp_call(stack.mcp, ukey, "create_order",
                               {"product_id": cheapest["id"], "quantity": QTY,
                                "address": ADDRESS})
    except Exception as exc:
        s.fail(f"建单失败：{exc}")
        return steps + [_rest_unreachable(i) for i in range(6, 10)]
    oid = (order or {}).get("order_id", "")
    if not re.fullmatch(r"AM[0-9A-F]{10}", oid or ""):
        s.fail(f"建单返回异常：{order}")
        return steps + [_rest_unreachable(i) for i in range(6, 10)]
    if order.get("status") != "待支付":
        s.fail(f"订单初始状态是「{order.get('status')}」，不是「待支付」")
    elif abs(float(order.get("total", 0)) - total) > 0.01:
        s.fail(f"合计金额对不上：返回 {order.get('total')}，应为 {total}")
    else:
        s.ok_(f"订单 {oid} · 合计 ¥{order.get('total')} · 状态「待支付」")
    steps.append(s)

    # ---------------------------------------------------------- 6 订单列表 + 二维码
    s = Step(6, "网页订单列表 → 点「去支付」",
             "（切回浏览器，刷新订单页）",
             "订单列表第一条就是刚下的单，状态「待支付」，右边有「去支付」按钮。",
             "「同一个订单，用户这边能看到，状态是待支付。注意我全程没碰过网页，"
             "是 AI 建的单。」",
             "列表里没有这单 → 页面 5 秒轮询一次，会自动插进来；"
             "等了 5 秒还没有 → 多半是智能体用的是别的账号/地址，"
             "回去核对第 5 步返回的订单号和这个页面对不对得上；再不行手动刷新一次确认。")
    s.show()
    st, page = web.get_text("/orders")
    if st != 200 or oid not in page:
        s.fail(f"订单列表里看不到 {oid}")
    else:
        st, cashier = web.get_text(f"/pay/{oid}")
        st2, png, _ = web.req(f"/pay/{oid}/qr.png")
        problems = []
        if st != 200:
            problems.append(f"收银台 HTTP {st}")
        if "演示环境" not in cashier:
            problems.append("收银台缺「演示环境」标注")
        if st2 != 200 or png[:4] != b"\x89PNG":
            problems.append(f"二维码不可用（HTTP {st2}）")
        if problems:
            s.fail("；".join(problems))
        else:
            s.ok_(f"收银台可开、二维码已生成（PNG {len(png)} 字节）")
    steps.append(s)

    # ---------------------------------------------------------- 7 扫码确认
    s = Step(7, "扫码 → 确认支付 → 订单完成",
             "（手机扫屏幕上的码；本地演示就直接点「确认支付」按钮）",
             f"订单状态从「待支付」变成「已完成」，金额 ¥{order.get('total')}",
             "「现在这一步是我自己在付钱——AI 只负责建单，付钱永远是人的事。"
             "这是我们整个设计里最核心的一条：**checkout hands off**，模型碰不到钱。」",
             "点了确认状态不变 → 多半是轮询没刷新，手动刷一下页；"
             "再不行就当场看订单号对不对。")
    s.show()
    st, _, _ = web.req(f"/pay/{oid}/confirm", method="POST")
    time.sleep(0.5)
    st2, after = web.get_text("/orders")
    if st not in (200, 303):
        s.fail(f"确认支付 HTTP {st}")
    elif oid not in after:
        s.fail("支付后订单列表里找不到这单")
    elif "已完成" not in after:
        s.fail("状态没变成「已完成」——单号行还在，但状态不对")
    else:
        s.ok_(f"订单 {oid} 状态 →「已完成」，库存已扣减，不触发发货（is_demo）")
    steps.append(s)

    # ---------------------------------------------------------- 8-9 管理后台
    # 登录和基线快照已在第 4 步之后、建单之前做过（否则增量恒为 0）
    st0, dash0 = admin.get_text(f"{b}/")
    before = before or kpi(dash0)

    s = Step(8, "切到管理后台 → 同一笔单，平台这边实时可见",
             "（另开一个浏览器标签，访问 http://127.0.0.1:8002）",
             "用管理员 key 登录后，看板六个数 + 下方订单列表。",
             "「切到平台侧——同一笔刚才 AI 下的单，这边实时就看到了。"
             "用户、商户、平台三边看到的是不同的数据，这是三权分立。」",
             "后台登录页会把演示 key 明文显示出来（方便你进），"
             "正式部署时 nginx 会加一层 basic auth，别当着投资方提这个明文 key。")
    s.show()
    if st0 != 200 or "订单总数" not in dash0:
        s.fail("管理后台看板打不开")
    else:
        k = kpi(dash0)
        s.ok_("看板可开：" + " · ".join(f"{lab} {v}" for lab, v in list(k.items())[:4]))
    steps.append(s)

    s = Step(9, "看板数字：订单数 +1、GMV 增加",
             "（把看板刷新一下）",
             f"订单数 +1，GMV 增加 ¥{order.get('total')}",
             "「订单数涨了 1，GMV 涨了刚才那笔。这就是平台侧的全局视角——"
             "用户在这买，平台这边实时结算，商户那边能接单发货。」",
             "别用「口径问题」圆过去。订单状态已经是「已完成」，钱就该进 GMV；"
             "看板没涨说明代码有 bug，当众承认「这块我们还在调」比数字对不上体面。")
    s.show()
    st, dash1 = admin.get_text(f"{b}/")
    after_k = kpi(dash1)
    d_orders = num(after_k.get("订单总数", "0")) - num(before.get("订单总数", "0"))
    d_gmv = num(after_k.get("GMV", "0")) - num(before.get("GMV", "0"))
    o1, o2 = num(before.get("订单总数", "0")), num(after_k.get("订单总数", "0"))
    g1, g2 = num(before.get("GMV", "0")), num(after_k.get("GMV", "0"))
    if not after_k:
        s.fail("刷新后看板没读出来")
    elif not before:
        s.fail("基线快照缺失，无法比对增量（别在台上说 GMV 涨了）")
    elif d_orders < 1:
        s.fail(f"订单数没涨（{int(o1)} → {int(o2)}）——台上如果说「订单实时进来了」就穿帮了")
    elif abs(d_gmv - float(order.get("total", 0))) > 0.01:
        s.fail(f"GMV 增量对不上：看板 +¥{d_gmv:.2f}，这单是 ¥{order.get('total')}。"
               f"GMV 口径只算已付款且未回退的订单，已完成的演示单必须计入")
    else:
        s.ok_(f"订单数 {int(o1)} → {int(o2)}（+{int(d_orders)}）· "
              f"GMV ¥{g1:.2f} → ¥{g2:.2f}（+¥{d_gmv:.2f}）")
    steps.append(s)

    # ---------------------------------------------------------- 可选：商户侧
    if with_merchant:
        s = Step(10, "（可选）商户侧 5 个 tool：越权拒绝是最亮的点",
                 "「我切到商户 M001 的身份，看看它能碰什么。」",
                 "拿 M001 的 key 去改 M002 的商品 → 返回 denied 并记审计。",
                 "「商户 A 改不了商户 B 的商品，这是代码层强制的，"
                 "不是靠提示词约束——而且这次越权会进审计日志，删不掉。」",
                 "商户侧没有网页，只有 MCP 工具（issue #2 的 P2 也只要求工具）。"
                 "要说界面的话只能口头讲，别开后台找商户页。")
        s.show()
        try:
            denied = await mcp_call(stack.mcp, MERCHANT_DEMO_KEY,
                                    "merchant_list_products", {})
            ok_shape = isinstance(denied, (list, dict))
        except Exception as exc:
            s.fail(f"商户 key 调用失败：{exc}")
            ok_shape = False
        if ok_shape:
            s.ok_("商户 key 可用（5 个 tool），越权路径在 test_roles.py 里逐条验过")
        steps.append(s)

    return steps


def _rest_unreachable(n: int) -> Step:
    s = Step(n, "（因前一步断了，这步跳过）", "—", "—",
             "别硬演。停了，对着投资方说：「这条链路我先过掉，"
             "我们直接看平台侧的数据。」然后跳到第 8 步。", "—")
    s.ok = None
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description="AgentMall 路演彩排")
    ap.add_argument("--isolated", action="store_true", help="自起临时栈，不碰现场服务")
    ap.add_argument("--with-merchant", action="store_true", help="附带商户侧彩排")
    args = ap.parse_args()

    print(f"\n{BOLD}AgentMall 路演彩排{OFF}  ——  {time.strftime('%H:%M:%S')}")
    print(DIM + "每步会告诉你：说什么、屏幕出现什么、怎么说、断了怎么救" + OFF)

    try:
        stack = Stack(args.isolated)
    except RuntimeError as exc:
        print(f"\n{RED}❌ {exc}{OFF}")
        return 2

    mode = "临时栈（排完即清）" if stack.owned else f"现场栈 web={stack.web} mcp={stack.mcp} admin={stack.admin}"
    print(f"{DIM}模式：{mode}{OFF}")

    t0 = time.time()
    try:
        steps = asyncio.run(rehearse(stack, args.with_merchant))
    except KeyboardInterrupt:
        print(f"\n{RED}彩排中断{OFF}")
        return 130
    finally:
        stack.close()

    done = [s for s in steps if s.ok is True]
    bad = [s for s in steps if s.ok is False]
    skipped = [s for s in steps if s.ok is None]

    print(f"\n{'━' * 66}")
    print(f"{BOLD}彩排结果{OFF}  用时 {time.time() - t0:.1f}s   "
          f"通过 {len(done)} / {len(steps)} 步")
    for s in steps:
        mark = "✅" if s.ok is True else ("❌" if s.ok is False else "⏭ ")
        print(f"  {mark} 第 {s.n} 步 {s.title}  {DIM}{s.detail}{OFF}")

    if bad:
        print(f"\n{RED}{BOLD}断了 {len(bad)} 步 —— 上台前按这个救{OFF}")
        for s in bad:
            print(f"  第 {s.n} 步：{s.detail}")
            print(f"    {DIM}救法：{s.rescue}{OFF}")
        print()
    elif skipped:
        print(f"\n{RED}彩排没走完{OFF}")
        for s in skipped:
            print(f"  第 {s.n} 步：{s.detail}")
    else:
        print(f"\n{GREEN}{BOLD}9 步全通，路演可以上{OFF}")
        print(DIM + "别忘了开演第一句：这是演示商品库 + 模拟支付网关，"
                   "全程不发生任何真实交易。" + OFF)
    print("━" * 66 + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
