# syntax=docker/dockerfile:1
#
# AgentMall 镜像（用户面 / MCP / 管理后台 三合一，compose 里用不同 command 起三个进程）
#
# 构建：docker build -t agentmall:latest .
# 运行：docker run --rm -p 8000:8000 -v ./data:/data agentmall:latest
#
# 说明：本文件属于「部署物料」，不改动 agentmall/ 下任何 Python 代码。
#       三个服务的差异**只在启动命令**，镜像本身完全共用。
FROM python:3.11-slim

LABEL org.opencontainers.image.title="AgentMall" \
      org.opencontainers.image.description="AgentMall 演示环境：演示商品数据 + Mock 支付网关，不发生任何真实交易" \
      org.opencontainers.image.licenses="MIT"

# ---------------------------------------------------------------------------
# pip 源：默认走清华镜像（国内拉 PyPI 全靠它，慢 10 倍不止）。
# 海外服务器/内网环境请在 build 时换掉：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple .
#   docker build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple .
# 留空则用 pip 默认源：
#   docker build --build-arg PIP_INDEX_URL="" .
# 注意：走 https 源不需要 --trusted-host（那是降级到 http 源才用的，不要加）。
# ---------------------------------------------------------------------------
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# 非 root 运行用的账号。UID/GID 固定成 10001，方便宿主机 bind mount 时对齐属主：
#   sudo chown -R 10001:10001 ./data      # 见 docs/部署文档.md「数据目录权限」
ARG APP_USER=agentmall
ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    AGENTMALL_DB=/data/agentmall.db

# sqlite3 CLI：约 1.5MB，专门为「备份/排查 SQL」装的，
# 让 docs/部署文档.md 里的 `sqlite3 xxx.db ".backup"` 在容器内也能直接用。
# 追求镜像体积可以删掉这一行，代价是排查时只能进 Python shell。
RUN apt-get update \
 && apt-get install -y --no-install-recommends sqlite3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -g "${APP_GID}" "${APP_USER}" \
 && useradd -u "${APP_UID}" -g "${APP_GID}" -m -d "/home/${APP_USER}" \
            -s /sbin/nologin -c "AgentMall service account" "${APP_USER}"

WORKDIR /app

# 依赖单独一层：改业务代码不会让 pip 层缓存失效。
# 依赖清单里锁了 mcp<2（v2 把 FastMCP 改名成 MCPServer，server.py 按 v1 API 写的），
# 不要在镜像里"顺手升级"，会直接把 `python -m agentmall.http_server` 升崩。
COPY requirements.txt ./
RUN pip install --index-url "${PIP_INDEX_URL}" -r requirements.txt

# 只拷运行必需的东西：业务包 + 演示商品种子 + 数据目录占位。
# data/products_real.json 必须带上：db._seed_products() 优先读
# <repo_root>/data/products_real.json（即 /app/data/products_real.json）来灌演示商品，
# 缺了它会静默退回 30 条老种子，页面上商品全变样。
COPY agentmall/ ./agentmall/
COPY data/ ./data/
# 同步仓库根目录下开箱即用的文档（发布说明/演示脚本），不进镜像也能跑，
# 留着方便在服务器上直接 cat 给客户/同事看。
COPY README.md LICENSE ./
COPY docs/ ./docs/

# /data 是 SQLite 的 bind mount 落点；即使不挂卷（本地跑 docker run）也能起来。
# /app/data 也要属主归 agentmall，否则回落到默认 DB 路径时会因只读而建表失败。
RUN mkdir -p /data /app/data \
 && chown -R "${APP_UID}:${APP_GID}" /app /data

USER ${APP_UID}:${APP_GID}
WORKDIR /app

EXPOSE 8000 8001 8002

# 健康检查打 /healthz：三个服务都有这个路由（web/mcp/admin 各返回自己的 service 名）。
# 镜像默认面向「用户面 8000」；compose 里每个 service 都单独覆盖成自己的端口。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)" || exit 1

# 默认起用户面。
# ⚠️ 这里必须用 uvicorn 直接起 ASGI app，不能写 `python -m agentmall.web.app`——
#    app.py 全文没有 `if __name__ == "__main__"` 块，那样跑会**秒退且退出码为 0**，
#    在 systemd（Restart=always）下会变成无限静默重启，最难查的一类故障。
#    对比：http_server.py / admin_app.py 都有 main()，可以 `-m` 直接起。
# --proxy-headers：让应用侧能看到真实客户端 IP / X-Forwarded-Proto（nginx 在前面时必须开）。
CMD ["python", "-m", "uvicorn", "agentmall.web.app:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
