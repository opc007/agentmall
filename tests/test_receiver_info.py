"""Phase A P0-1 验收：收货信息补齐收货人姓名 + 联系电话。

背景：2026-10-08 用户路演实测反馈——`/me` 的「默认收货地址」只有地址文本，
**没有收货人和电话，下单流程走不通**。指挥官版 `buy_now` 当时用
`"演示地址（智能体下单时填写真实收货地址）"` 硬编码占位。

覆盖：
  1. 表单三字段（姓名 / 11 位手机号 / 地址）必填 + 校验
  2. 保存后个人中心回显完整信息
  3. 网页下单走真实收货信息，不再有占位符
  4. **智能体通过 MCP 下单时，订单也带姓名+电话**（需求 3）
  5. 订单详情/收银台能看到完整收货信息
  6. 老数据缺项不静默放过（需求 4）

运行： .venv/bin/python tests/test_receiver_info.py
"""
import http.cookiejar
import json
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

DB = os.path.join(tempfile.gettempdir(), "agentmall_test_receiver.db")
checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    checks.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    for sfx in ("", "-wal", "-shm"):
        if os.path.exists(DB + sfx):
            os.remove(DB + sfx)
    env = dict(os.environ, AGENTMALL_DB=DB, PYTHONPATH=ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agentmall.web.app:app",
         "--port", str(port)], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        print(f"\n{'=' * 62}\nP0-1 收货信息验收（:{port}）\n{'=' * 62}")
        for _ in range(80):
            try:
                urllib.request.urlopen(f"{base}/healthz", timeout=1).read()
                break
            except Exception:
                time.sleep(0.5)

        c = C(base)
        c.go("/register", {"username": "liufeng", "password": "pass123456"})

        # --- 1. 表单三字段都在，且是必填 ---
        st, body, _ = c.go("/me")
        for f in ("receiver_name", "receiver_phone", "address"):
            check(f'name="{f}"' in body, f"表单含字段 {f}")
        check(body.count("required") >= 3, "三个字段都是必填")

        # --- 2. 校验 ---
        bad_cases = [
            ("手机号位数不对", {"receiver_name": "刘峰", "receiver_phone": "12345",
                              "address": "广西南宁市朝阳广场 3 号"}, "11 位手机号"),
            ("手机号非数字开头", {"receiver_name": "刘峰", "receiver_phone": "23800138000",
                              "address": "广西南宁市朝阳广场 3 号"}, "11 位手机号"),
            ("姓名为空", {"receiver_name": "", "receiver_phone": "13800138000",
                        "address": "广西南宁市朝阳广场 3 号"}, "收货人姓名"),
            ("地址太短", {"receiver_name": "刘峰", "receiver_phone": "13800138000",
                        "address": "短"}, "收货地址"),
        ]
        for label, data, want in bad_cases:
            st, body, url = c.go("/me/address", data)
            check("addr_err" in url and want in urllib.parse.unquote(url),
                  f"{label} 被拒并给出提示")

        # --- 3. 正常保存 + 回显 ---
        st, body, url = c.go("/me/address?next=/me", {
            "receiver_name": "刘峰", "receiver_phone": "13800138000",
            "address": "广西南宁市朝阳广场 3 号"})
        check("addr_ok=1" in url, "合法信息保存成功")
        st, body, _ = c.go("/me")
        check("刘峰" in body and "13800138000" in body and "朝阳广场" in body,
              "个人中心回显「姓名 + 电话 + 地址」")

        # --- 4. 网页下单不再用占位符 ---
        st, body, url = c.go("/buy/P112", method="POST")
        check("/pay/AM" in url, f"下单跳收银台（{url}）")
        oid = url.rstrip("/").split("/")[-1]
        st, body, _ = c.go(f"/pay/{oid}")
        check("演示地址（智能体下单时填写真实收货地址）" not in body,
              "收银台不再出现占位地址（需求：下单流程走得通）")
        check("刘峰" in body and "13800138000" in body,
              "收银台显示完整收货信息")

        # --- 5. 订单列表 ---
        st, body, _ = c.go("/orders")
        check(oid in body and "刘峰" in body, "订单列表显示收货人")

        # --- 6. 库里快照 ---
        os.environ["AGENTMALL_DB"] = DB
        from agentmall import db
        with db.connect() as conn:
            row = conn.execute(
                "SELECT address, receiver_name, receiver_phone FROM orders "
                "WHERE id=?", (oid,)).fetchone()
        check(row is not None and row["receiver_name"] == "刘峰"
              and row["receiver_phone"] == "13800138000"
              and "朝阳广场" in row["address"],
              "订单快照落库（收货信息随单留存）")

        # --- 7. 缺项不静默放过（需求 4）---
        c2 = C(base)
        c2.go("/register", {"username": "nofill", "password": "pass123456"})
        st, body, url = c2.go("/buy/P112", method="POST")
        check("need_address=1" in url,
              f"新用户没填收货信息点下单 → 去补全而不是硬下单（{url}）")
        check("missing=" in url, "明确告诉用户缺哪几项")

        # --- 8. 智能体 MCP 下单也带姓名电话（需求 3）---
        from agentmall import auth
        u = auth.login("liufeng", "pass123456")
        ukey = u["api_key"]
        import asyncio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def mcp_order():
            # 环境变量**显式给全**：之前依赖 os.environ 的残留，子进程连到别的库，
            # 症状是返回空 content，排查绕了一圈。
            child_env = dict(os.environ)
            child_env.update(AGENTMALL_DB=DB, AGENTMALL_API_KEY=ukey,
                             PYTHONPATH=ROOT)
            p = StdioServerParameters(
                command=sys.executable, args=["-m", "agentmall.server"],
                env=child_env)
            async with stdio_client(p) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    res = await s.call_tool("create_order", {
                        "product_id": "P112", "quantity": 1,
                        "address": "智能体按用户口述临时改的地址"})
                    sc = res.structuredContent or {}
                    if "result" in sc:
                        return sc["result"]
                    texts = [b.text for b in res.content if getattr(b, "text", None)]
                    if not texts:
                        return {"error": "空返回", "is_error": res.isError}
                    return json.loads(texts[0])

        o2 = asyncio.run(mcp_order())
        check(o2.get("receiver_name") == "刘峰",
              f"MCP 建单带上了档案里的姓名（{o2.get('receiver_name')}）")
        check(o2.get("receiver_phone") == "13800138000",
              f"MCP 建单带上了档案里的电话（{o2.get('receiver_phone')}）")
        check("智能体按用户口述" in o2.get("address", ""),
              "地址仍以智能体/用户当次口述为准（不被档案覆盖）")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    passed = sum(1 for ok, _ in checks if ok)
    print(f"\n{'=' * 62}\nP0-1 收货信息 {passed}/{len(checks)} 项通过")
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
