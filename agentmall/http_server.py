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
app.mount("/", mcp.streamable_http_app())


def main() -> None:
    import uvicorn

    print(f"AgentMall MCP Server (streamable-http) → http://127.0.0.1:{PORT}{MCP_PATH}")
    print("演示 key：")
    print("  用户    uk_demo_user_secret")
    print("  管理员  ak_demo_admin_secret")
    print("  商户    mk_demo_m001_secret / mk_demo_m002_secret / mk_demo_m003_secret")
    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
