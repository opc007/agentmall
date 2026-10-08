"""MCP Host/Origin 白名单回归测试（公网域名 421 "Invalid Host header" 的正式修复）。

背景：`mcp` 1.30 的 FastMCP 在 host=127.0.0.1 时自动开启 DNS 重绑定保护，
allowed_hosts 只有 localhost，于是经 nginx / tunnel 用公网域名访问 /mcp 会被
TransportSecurityMiddleware 回 421。现在 `agentmall/http_server.py` 读
AGENTMALL_MCP_PUBLIC_HOST / AGENTMALL_MCP_ALLOWED_ORIGINS 把域名写进白名单。

本测试**真的起子进程服务、发真实 HTTP 请求**验证状态码，不只断言内部变量：
- 不设环境变量 → 127.0.0.1 正常连，且未列出的 Host 仍被拒（现有行为不变）
- 设 AGENTMALL_MCP_PUBLIC_HOST=mall.example.com → Host: mall.example.com 不再 421
- 设了公网域名 → Host: 127.0.0.1:PORT 仍可用（本地没被误伤）
- 配了白名单 → 未列出的任意 Host 仍被拒（安全边界）
- AGENTMALL_MCP_PUBLIC_HOST=* → 放行全部，且启动日志里明确提示是危险配置

每个场景用独立临时库（/tmp/agentmall_test_mcp_hosts_<pid>_<场景>.db），互不干扰。
运行： .venv/bin/python tests/test_mcp_hosts.py
"""
import asyncio
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

PUBLIC_HOST = "mall.example.com"
FOREIGN_HOST = "evil.example.org"

INIT_BODY = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "mcp-hosts-test", "version": "1.0"}},
}).encode()

checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    checks.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _db_path(tag: str) -> str:
    return os.path.join(tempfile.gettempdir(),
                        f"agentmall_test_mcp_hosts_{os.getpid()}_{tag}.db")


def _drop_db(path: str) -> None:
    for suffix in ("", "-wal", "-shm", ".log"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


class Server:
    """真起一个 agentmall.http_server 子进程，日志落临时文件便于断言启动白名单。"""

    def __init__(self, tag: str, **env_extra: str):
        self.port = _free_port()
        self.db = _db_path(tag)
        self.log = self.db + ".log"
        _drop_db(self.db)
        # 继承调用方环境（含 PYTHONPATH），只覆盖本场景关心的变量；
        # 场景之间不共享任何状态，每个都是全新进程。
        env = {k: v for k, v in os.environ.items()
               if k not in ("AGENTMALL_MCP_PUBLIC_HOST", "AGENTMALL_MCP_ALLOWED_ORIGINS")}
        env.update(AGENTMALL_DB=self.db, PYTHONPATH=ROOT,
                   AGENTMALL_MCP_PORT=str(self.port), **env_extra)
        self._fh = open(self.log, "w+", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "agentmall.http_server"],
            cwd=ROOT, env=env, stdout=self._fh, stderr=subprocess.STDOUT, text=True)

    @property
    def log_text(self) -> str:
        self._fh.flush()
        with open(self.log, encoding="utf-8") as f:
            return f.read()

    def wait_ready(self, timeout: float = 40.0) -> bool:
        import urllib.request

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                print("服务提前退出：\n", self.log_text)
                return False
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz",
                                       timeout=1).read()
                return True
            except Exception:
                time.sleep(0.3)
        return False

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self._fh.close()
        _drop_db(self.db)

    def __enter__(self) -> "Server":
        if not self.wait_ready():
            raise RuntimeError(f"服务启动失败，日志：\n{self.log_text}")
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---- 真实请求 -------------------------------------------------------

    def post_mcp(self, host_header: str, origin: str | None = None) -> int:
        """对 /mcp 发一个真实的 MCP initialize POST，返回 HTTP 状态码。

        只读状态码就断开：成功时响应是 SSE 流，不适合整段读完。
        """
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "Host": host_header}
        if origin:
            headers["Origin"] = origin
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/mcp", body=INIT_BODY, headers=headers)
            return conn.getresponse().status
        finally:
            conn.close()


async def mcp_session_ok(port: int, host_header: str) -> tuple[bool, str]:
    """走真正的 MCP 客户端握手：能不能 initialize + 列出 tool。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    try:
        async with streamablehttp_client(
                f"http://127.0.0.1:{port}/mcp",
                headers={"Host": host_header}) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools = [t.name for t in (await session.list_tools()).tools]
        return bool(tools), f"{len(tools)} 个 tool"
    except Exception as e:  # noqa: BLE001 — 测试里只关心失败原因
        return False, f"{type(e).__name__}: {e}"


async def main() -> int:
    # ---------------- 场景 1：不设环境变量，现有行为不变 ----------------
    print("\n[场景 1] 不设 AGENTMALL_MCP_PUBLIC_HOST（默认行为不得改变）")
    with Server("default") as srv:
        ok, detail = await mcp_session_ok(srv.port, f"127.0.0.1:{srv.port}")
        check(ok, f"127.0.0.1 完整 MCP 会话正常（initialize + list_tools，{detail}）")
        check(srv.post_mcp(f"127.0.0.1:{srv.port}") != 421,
              "Host: 127.0.0.1:PORT 不返回 421")
        check(srv.post_mcp(PUBLIC_HOST) == 421,
              f"未列出的 Host（{PUBLIC_HOST}）仍被 421 拒绝——默认没被放宽")
        log = srv.log_text
        check("[MCP Host 白名单] pid=" in log and "允许的 Host" in log,
              "启动日志打印了 pid 与最终 Host 白名单")
        check("[::1]:*" in log, "默认白名单保留 localhost/127.0.0.1/::1")

    # ---------------- 场景 2：配置公网域名 ----------------
    print("\n[场景 2] AGENTMALL_MCP_PUBLIC_HOST=mall.example.com")
    with Server("public", AGENTMALL_MCP_PUBLIC_HOST=PUBLIC_HOST) as srv:
        status = srv.post_mcp(PUBLIC_HOST)
        check(status != 421, f"Host: {PUBLIC_HOST} 不再 421（实际 {status}）")
        ok, detail = await mcp_session_ok(srv.port, PUBLIC_HOST)
        check(ok, f"带 Host: {PUBLIC_HOST} 的完整 MCP 会话能列出 tool（{detail}）")

        check(srv.post_mcp(f"127.0.0.1:{srv.port}") != 421,
              "本地 Host: 127.0.0.1:PORT 未被误伤")
        ok, detail = await mcp_session_ok(srv.port, f"127.0.0.1:{srv.port}")
        check(ok, f"本地完整 MCP 会话仍正常（{detail}）")
        check(srv.post_mcp(f"localhost:{srv.port}") != 421,
              "本地 Host: localhost:PORT 仍在白名单内")

        # 安全边界：白名单外的任意 Host 必须继续被拒
        check(srv.post_mcp(FOREIGN_HOST) == 421,
              f"白名单外的 Host（{FOREIGN_HOST}）仍被 421 拒绝")
        check(srv.post_mcp("mall.example.com.evil.test") == 421,
              "相似域名 mall.example.com.evil.test 也被拒")
        check(PUBLIC_HOST in srv.log_text,
              "启动日志里能看到生效的公网域名")

    # ---------------- 场景 3：逗号分隔多个域名 ----------------
    print("\n[场景 3] AGENTMALL_MCP_PUBLIC_HOST=a.example.com, b.example.com")
    with Server("multi",
                AGENTMALL_MCP_PUBLIC_HOST="a.example.com, b.example.com") as srv:
        check(srv.post_mcp("a.example.com") != 421, "第一个域名可用")
        check(srv.post_mcp("b.example.com") != 421, "第二个域名可用")
        check(srv.post_mcp(FOREIGN_HOST) == 421, "没列的域名仍被 421 拒绝")

    # ---------------- 场景 4：ALLOWED_ORIGINS ----------------
    print("\n[场景 4] AGENTMALL_MCP_ALLOWED_ORIGINS")
    with Server("origin",
                AGENTMALL_MCP_PUBLIC_HOST=PUBLIC_HOST,
                AGENTMALL_MCP_ALLOWED_ORIGINS="https://mall.example.com") as srv:
        check(srv.post_mcp(PUBLIC_HOST, origin="https://mall.example.com") != 421,
              "白名单内的 Origin 不被 403")
        check(srv.post_mcp(PUBLIC_HOST, origin="https://mall.example.com:8443") != 421,
              "白名单 Origin 带端口也能过（自动补了 `origin:*`）")
        check(srv.post_mcp(PUBLIC_HOST, origin="https://evil.example.org") == 403,
              "白名单外的 Origin 被 403 拒绝")

    # ---------------- 场景 5：`*` 放行全部（危险配置） ----------------
    print("\n[场景 5] AGENTMALL_MCP_PUBLIC_HOST=*（危险配置）")
    with Server("wildcard", AGENTMALL_MCP_PUBLIC_HOST="*") as srv:
        check(srv.post_mcp(FOREIGN_HOST) != 421,
              f"Host: {FOREIGN_HOST} 被放行（*=放行全部，符合预期）")
        check(srv.post_mcp(PUBLIC_HOST) != 421, "Host: mall.example.com 被放行")
        log = srv.log_text
        check("⚠️" in log and "危险配置" in log,
              "启动输出明确提示这是危险配置（关闭了 DNS 重绑定保护）")
        check("生产环境请显式列出真实域名" in log,
              "启动输出提醒生产环境显式列域名、不要写 '*'")

    # ---------------- 场景 6：公网域名没配时，公网 Host 必须仍是 421 ----------------
    print("\n[场景 6] 不设变量时公网 Host 依然 421（防止'顺手全放开'）")
    with Server("nofix") as srv:
        check(srv.post_mcp(PUBLIC_HOST) == 421,
              f"未配置时 Host: {PUBLIC_HOST} 返回 421（修复前就是这个行为）")

    passed = sum(1 for ok, _ in checks if ok)
    print(f"\n{'=' * 68}\nMCP Host 白名单 {passed}/{len(checks)} 项通过")
    for ok, label in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    print("=" * 68)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))