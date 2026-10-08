#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_mcp_public.py — 公网演示用 MCP 启动器
=========================================================
背景：`mcp` 库的 FastMCP 在 host=127.0.0.1 时自动开启 DNS 重绑定保护，
`allowed_hosts` 只有 localhost 变体，公网域名（tunnel / Codespaces 转发域名）
调 `/mcp` 会被 421 "Invalid Host header" 拒绝。

本脚本在 `agentmall.http_server` import 之前关闭该保护（仅演示环境）。
正式修复（`AGENTMALL_MCP_PUBLIC_HOST` 环境变量 → `allowed_hosts`）见 issue #2
Phase A 的 A7，由主线实现后替换本脚本。

用法：
    python scripts/run_mcp_public.py
    环境变量 AGENTMALL_MCP_PORT 生效（默认 8001）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentmall.server import mcp  # noqa: E402

mcp.settings.transport_security = None  # 关闭 DNS 重绑定保护（演示用）

import uvicorn  # noqa: E402
from agentmall.http_server import PORT, app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
