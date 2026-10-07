"""streamable-http 接入 + per-agent key 鉴权测试（Phase2任务书 §6 验收标准 1）。

验的是路演真正要走的那条路：AI 客户端按「URL + Bearer key」接入，
用户 key 调不到管理员 tool，且越权被记进审计日志。

运行： .venv/bin/python tests/test_http_auth.py
"""
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

DB = os.path.join(tempfile.gettempdir(), "agentmall_test_http.db")
PORT = int(os.environ.get("TEST_MCP_PORT", "8931"))
BASE = f"http://127.0.0.1:{PORT}"
KEYS = {
    "user": "uk_demo_user_secret",
    "admin": "ak_demo_admin_secret",
    "merchant": "mk_demo_m001_secret",
}

checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    checks.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def call(base: str, key: str, tool: str, args: dict):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with streamablehttp_client(f"{base}/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            if tool is None:
                return tools, None
            res = await session.call_tool(tool, args)
            return tools, res


def unwrap(res):
    sc = getattr(res, "structuredContent", None)
    if sc is not None and "result" in sc:
        return sc["result"]
    for b in res.content:
        t = getattr(b, "text", None)
        if t:
            import json
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return t
    return res.content


async def main() -> int:
    global PORT
    PORT = _free_port()
    BASE = f"http://127.0.0.1:{PORT}"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(DB + suffix):
            os.remove(DB + suffix)

    env = dict(os.environ, AGENTMALL_DB=DB, PYTHONPATH=ROOT,
               AGENTMALL_MCP_PORT=str(PORT))
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentmall.http_server"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # 等端口起来
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{BASE}/healthz", timeout=1).read()
                break
            except Exception:
                if proc.poll() is not None:
                    print("服务提前退出：\n", proc.stdout.read())
                    return 1
                time.sleep(0.5)
        else:
            print("服务启动超时")
            return 1

        print(f"[准备] MCP HTTP 服务已起：{BASE}/mcp\n")

        # 1. 健康检查
        health = urllib.request.urlopen(f"{BASE}/healthz", timeout=5).read()
        check(b'"ok"' in health, "GET /healthz 返回 ok")

        # 2. whoami：key → 角色
        for role, key in KEYS.items():
            req = urllib.request.Request(
                f"{BASE}/whoami", headers={"Authorization": f"Bearer {key}"})
            body = urllib.request.urlopen(req, timeout=5).read().decode()
            check(f'"{role}"' in body, f"{role} key 解析为 {role} 角色")

        # 3. 无效 key 被 401
        try:
            req = urllib.request.Request(
                f"{BASE}/whoami", headers={"Authorization": "Bearer bad_key"})
            urllib.request.urlopen(req, timeout=5)
            check(False, "无效 key 应被 401 拒绝")
        except urllib.error.HTTPError as e:
            check(e.code == 401, f"无效 key 被 401 拒绝（实际 {e.code}）")

        # 4. 用户 key 正常搜索
        _, res = await call(BASE, KEYS["user"], "search_products",
                            {"keyword": "纸巾", "limit": 3})
        items = unwrap(res)
        check(isinstance(items, list) and len(items) > 0,
              f"用户 key 走 HTTP 能搜索（{len(items) if isinstance(items, list) else '?'} 条）")

        # 5. 越权：用户 key 调管理员 tool → 拒绝 + 记审计
        _, res = await call(BASE, KEYS["user"], "admin_stats", {})
        out = unwrap(res)
        denied = isinstance(out, dict) and out.get("denied") is True
        check(denied, f"用户 key 调 admin_stats 被拒（{out}）")

        _, res = await call(BASE, KEYS["user"], "merchant_list_orders", {})
        out2 = unwrap(res)
        check(isinstance(out2, list) and out2 and out2[0].get("denied") is True,
              "用户 key 调 merchant tool 被拒")

        # 6. 审计日志里有 deny 记录
        _, res = await call(BASE, KEYS["admin"], "admin_audit_log", {"limit": 20})
        logs = unwrap(res)
        has_deny = isinstance(logs, list) and any(
            str(e.get("action", "")).startswith("deny:") for e in logs)
        check(has_deny, "越权调用已记入 audit_log")

        # 7. 管理员看板可访问
        _, res = await call(BASE, KEYS["admin"], "admin_stats", {})
        stats = unwrap(res)
        check(isinstance(stats, dict) and "gmv" in stats,
              f"管理员 key 可访问 admin_stats（GMV={stats.get('gmv') if isinstance(stats, dict) else '?'}）")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    passed = sum(1 for ok, _ in checks if ok)
    print(f"\n{'=' * 60}\nHTTP 鉴权 {passed}/{len(checks)} 项通过")
    for ok, label in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    print("=" * 60)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(DB + suffix):
            try:
                os.remove(DB + suffix)
            except OSError:
                pass
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
