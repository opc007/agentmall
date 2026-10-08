"""MCP Server 的 streamable-http 入口（路演用：AI 客户端按 URL 接入）。

stdio 模式（`python -m agentmall.server`）适合本地 Claude Code / Cursor；
但路演场景里用户要把「MCP 接入点」复制给自己的 AI（workbuddy），
需要按 URL + Bearer key 接入，所以这里把同一个 `mcp` 实例挂到 HTTP 上。

启动：
    .venv/bin/python -m agentmall.http_server        # 端口 8001
    # 或 uvicorn agentmall.http_server:app --port 8001

鉴权：每个请求 `Authorization: Bearer <key>`，key 决定角色，
中间件把它放进 ContextVar，tool 内部据此过滤可见范围（见 server.py `_actor`）。
无 key 时按"用户角色"放行（单机演示用）；生产环境应强制要求 key。
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# 单进程服务所有角色：注册全部 tool，隔离靠调用时守卫 + 审计兜底。
# **必须在 import server 之前设好**——server.py 在 import 时就决定注册哪些 tool。
os.environ.setdefault("AGENTMALL_MULTI_ROLE", "1")

from . import auth, db  # noqa: E402
from .server import mcp  # noqa: E402

PORT = int(os.environ.get("AGENTMALL_MCP_PORT", "8001"))
MCP_PATH = "/mcp"


# ---------------------------------------------------------------------------
# Host / Origin 白名单（mcp 库的 DNS 重绑定保护）
# ---------------------------------------------------------------------------
# 背景：`mcp` 1.30 的 FastMCP 在 host 是 127.0.0.1/localhost/::1 时，会**自动**
# 打开 DNS 重绑定保护（见 mcp/server/fastmcp/server.py:191），allowed_hosts 只有
# `127.0.0.1:*` / `localhost:*` / `[::1]:*`。于是经 nginx / tunnel 用公网域名
# （deploy/nginx.conf 把 /mcp 反代到 127.0.0.1:8001，Host 头是公网域名）访问时，
# TransportSecurityMiddleware 会回 421 "Invalid Host header"。
# 之前的演示环境靠启动包装器把 mcp.settings.transport_security 置空绕过去，
# 那是 hack——等于把防护整个关掉。这里改成正式配置：把公网域名显式加进白名单。
#
# ⚠️ 安全提醒：DNS 重绑定保护本身是在防 DNS rebinding 攻击——攻击者诱导受害者
#    浏览器向 127.0.0.1 发请求，用伪造的 Host / Origin 骗过只监听本机的服务，
#    从而偷走会话或调用本地 tool。
#    · 把保护整个关掉（enable_dns_rebinding_protection=False）
#    · 或把白名单写成 `*`（本实现里 `*` 只能靠关掉保护来实现，见下）
#    等价于把 /mcp 暴露给**任意** Host 头：任何能连到这个端口的人都能用你的
#    MCP tool。生产环境请显式列出真实域名
#    （AGENTMALL_MCP_PUBLIC_HOST=mall.example.com），不要图省事写 `*`。
#
# 环境变量：
#   AGENTMALL_MCP_PUBLIC_HOST     逗号分隔的公网域名；`*` = 放行全部（危险！）
#   AGENTMALL_MCP_ALLOWED_ORIGINS 逗号分隔的 Origin；同样支持 `*`（危险！）
# 两个都不设 → 完全保持 mcp 库默认行为（只放行 localhost），本地开发不受影响。

WILDCARD = "*"

# 本地回环永远保留：否则本地开发、tests/test_http_auth.py 等走 127.0.0.1 的用例会全挂。
# 这三条与 mcp 库自身的默认值保持一致（server.py:193）。
LOCAL_HOST_PATTERNS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOCAL_ORIGIN_PATTERNS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]

_DANGER_NOTE = (
    "危险配置：任意 Host / Origin 头都能访问 /mcp，MCP tool 对所有能连到该端口的人开放。"
    "生产环境请显式列出真实域名（AGENTMALL_MCP_PUBLIC_HOST=mall.example.com），不要写 '*'。"
)


def _split_env(name: str) -> list[str]:
    """按逗号切分环境变量，去空白与空项。"""
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def _has_port(value: str) -> bool:
    """`example.com` / `https://example.com` 没端口；`example.com:8443` / `https://a:443` 有。"""
    if "://" in value:  # Origin 自带 scheme，先剥掉，否则 https: 的冒号会被当成端口
        value = value.split("://", 1)[1]
    if value.startswith("["):  # IPv6: [::1]:8001
        return ":" in value.split("]", 1)[1]
    return value.count(":") == 1


def _expand_patterns(values: list[str]) -> list[str]:
    """补齐 `host:*` 形式。

    mcp 1.30 的 TransportSecurityMiddleware 只支持两种匹配：完全相等，
    或白名单项以 `:*` 结尾做「前缀 + 端口」匹配（transport_security.py:_validate_host）。
    反代 / tunnel 的 Host 头常常带端口（`mall.example.com:8443`），所以裸域名
    额外补一条 `mall.example.com:*`，否则换个端口就又 421 了。
    已经写了端口或已经带 `*` 的保持原样。
    """
    out: list[str] = []
    for value in values:
        for item in ([value] if ("*" in value or _has_port(value))
                     else [value, f"{value}:*"]):
            if item not in out:
                out.append(item)
    return out


def configure_mcp_hosts(mcp_server) -> None:
    """把 AGENTMALL_MCP_PUBLIC_HOST / _ALLOWED_ORIGINS 写进 mcp 的 transport security。

    必须在本进程第一次调用 `mcp_server.streamable_http_app()` **之前**执行：
    那是 StreamableHTTPSessionManager 快照 security_settings 的时刻
    （streamable_http_app → security_settings=self.settings.transport_security），
    之后再改 `settings.transport_security` 不会生效。
    """
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = _split_env("AGENTMALL_MCP_PUBLIC_HOST")
    origins = _split_env("AGENTMALL_MCP_ALLOWED_ORIGINS")

    # mcp 1.30 的 _validate_host 没有任何 glob 支持（`*` 不会匹配 `mall.example.com`），
    # 要真正「放行全部」只能关掉 DNS 重绑定保护——所以 `*` 必须显式警告。
    wildcard = WILDCARD in hosts or WILDCARD in origins
    # `*.example.com` 这类子域通配在 mcp 1.30 里同样不生效（不会 glob 匹配），
    # 与其静默失效不如在启动日志里点破，让人改成逐个列域名。
    bogus_glob = [v for v in hosts + origins if "*" in v and v != WILDCARD]

    if not hosts and not origins:
        # 没配 → 一个字都不动，保持 mcp 库默认（DNS 重绑定保护 + 仅 localhost 白名单）。
        _report(mcp_server, settings=mcp_server.settings.transport_security,
                sources="", danger=False)
        return

    if wildcard:
        mcp_server.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False, allowed_hosts=[], allowed_origins=[])
    else:
        mcp_server.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            # 本地回环必须始终保留，否则本地开发和现有测试会全挂。
            allowed_hosts=_expand_patterns([*LOCAL_HOST_PATTERNS, *hosts]),
            allowed_origins=_expand_patterns([*LOCAL_ORIGIN_PATTERNS, *origins]),
        )

    sources = ", ".join(
        f"{k}={os.environ[k]}"
        for k in ("AGENTMALL_MCP_PUBLIC_HOST", "AGENTMALL_MCP_ALLOWED_ORIGINS")
        if os.environ.get(k))
    _report(mcp_server, settings=mcp_server.settings.transport_security,
            sources=sources, danger=wildcard)

    for value in bogus_glob:
        print(f"  ⚠️ 「{value}」里的 * 不会生效：mcp 1.30 只支持精确匹配和 `host:*` 端口通配，"
              f"请逐个列出域名。")


def _report(mcp_server, *, settings, sources: str, danger: bool) -> None:
    """启动时把最终生效的白名单打出来：哪个进程在跑、允许哪些 Host。"""
    print(f"[MCP Host 白名单] pid={os.getpid()} port={PORT} path={MCP_PATH}")
    if settings is None or not getattr(settings, "enable_dns_rebinding_protection", False):
        print("  DNS 重绑定保护 : 关闭（任意 Host 头都放行）")
        print("  允许的 Host    : *")
        print("  允许的 Origin  : *")
    else:
        print("  DNS 重绑定保护 : 开启")
        print(f"  允许的 Host    : {', '.join(settings.allowed_hosts)}")
        print(f"  允许的 Origin  : {', '.join(settings.allowed_origins)}")
    if sources:
        print(f"  配置来源       : {sources}")
    else:
        print("  配置来源       : 未设置 AGENTMALL_MCP_PUBLIC_HOST（mcp 库默认：仅本地回环）")
    if danger:
        print(f"  ⚠️ {_DANGER_NOTE}")


class BearerMiddleware(BaseHTTPMiddleware):
    """把 Authorization: Bearer <key> 注入 ContextVar。"""

    async def dispatch(self, request: Request, call_next):
        token = auth.set_request_key(
            auth.key_from_header(request.headers.get("authorization")))
        try:
            return await call_next(request)
        finally:
            auth.reset_request_key(token)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # 关键：把 MCP 的 session manager 跑在自己的 task group 里。
    # 挂载进 FastAPI 后子应用的 lifespan 不会被执行，不显式 run() 的话
    # 每个请求都会抛 "Task group is not initialized. Make sure to use run()."
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="AgentMall MCP Server", lifespan=lifespan)
app.add_middleware(BearerMiddleware)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "agentmall-mcp", "path": MCP_PATH}


@app.get("/whoami")
async def whoami(request: Request) -> JSONResponse:
    """调试用：看当前 key 被解析成什么角色。"""
    actor = auth.resolve_key(
        auth.key_from_header(request.headers.get("authorization")))
    if not actor:
        return JSONResponse({"ok": False, "error": "无效或缺失的 API key"}, 401)
    return JSONResponse({"ok": True, "role": actor["role"],
                         "actor_id": actor["actor_id"],
                         "merchant_id": actor["merchant_id"],
                         "tools": sorted(auth.ROLE_TOOLS.get(actor["role"], set()))})


# 挂载 MCP streamable-http。
# 注意：必须挂在 "/"，因为 FastMCP 的 ASGI app 内部自带 `/mcp` 路由；
# 若挂到 app.mount("/mcp", ...)，实际路径会变成 /mcp/mcp，客户端 307 重定向。
# /healthz 与 /whoami 在挂载之前声明，会优先匹配，不受影响。
#
# configure_mcp_hosts 必须在上面这行**之前**跑：streamable_http_app() 会把
# settings.transport_security 快照进 StreamableHTTPSessionManager，晚一步就白改。
configure_mcp_hosts(mcp)
app.mount("/", mcp.streamable_http_app())


def main() -> None:
    import uvicorn

    print(f"AgentMall MCP Server (streamable-http) → http://127.0.0.1:{PORT}{MCP_PATH}")
    print("演示 key：")
    print("  用户    uk_demo_user_secret")
    print("  管理员  ak_demo_admin_secret")
    print("  商户    mk_demo_m001_secret / mk_demo_m002_secret / mk_demo_m003_secret")
    public_hosts = _split_env("AGENTMALL_MCP_PUBLIC_HOST")
    if public_hosts:
        print("公网接入点（Host 头已在白名单内，不会再 421）：")
        for host in public_hosts:
            print(f"  {host}{MCP_PATH}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
