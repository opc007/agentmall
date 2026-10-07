# AgentMall

**智能体原生的日用消费品采购 MCP Server（开源 MVP）**
*Agent-native daily-needs shopping via MCP — open source MVP.*

> 🚧 当前状态：Phase 2 路演雏形（用户侧闭环已通，管理员/商户侧进行中）
> 数据与支付均为**演示数据**：商品是整理/采集的公开价格，支付是模拟网关，不发生任何真实交易。

## 这是什么

用户不用打开 App，直接对自己的 AI 智能体说"帮我买最便宜的抽纸"，
智能体通过本项目的 MCP 接口完成搜索、比价、下单——买的是 1688 源头价的日用刚需品
（纸巾、垃圾袋、洗衣液、收纳等）。

- **MCP 优先**：为智能体设计接口，而非为人设计网页
- **源头价格**：对接 1688 一件代发，零库存、现金流正向
- **合规先行**：只做"辅助支付"（智能体下单、用户自己点链接付款），不做自动扣款

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate   # Linux/macOS
pip install -r requirements.txt

# 终端 A：Web 前端（注册/登录/个人中心/订单/模拟收银台）→ http://127.0.0.1:8000
uvicorn agentmall.web.app:app --port 8000

# 终端 B：MCP Server（给 AI 客户端接）→ http://127.0.0.1:8001/mcp
python -m agentmall.http_server
```

本地 AI 客户端（Claude Code / Cursor 等）也可以直接用 stdio：

```bash
python -m agentmall.server
```

> Windows 用 `.venv\Scripts\activate`。若系统 pip 报 `externally-managed-environment`
> （PEP 668），说明该 Python 禁止全局装包——用上面的 venv 即可，这是推荐做法。

> ⚠️ **必须装 `mcp<2`**：`requirements.txt` 已锁 `mcp>=1.8,<2`。
> mcp 2.x 把 `FastMCP` 更名为 `MCPServer`，`mcp.server.fastmcp` 会直接 ImportError，
> 导致 Server 无法启动。迁 v2 属后续事项。

## 三角色接入（per-agent key）

每个请求带 `Authorization: Bearer <key>`，key 决定角色，Server **只注册该角色的 tool**：

| 角色 | 工具数 | 演示 key |
|------|-------|---------|
| 用户 | 4 | `uk_demo_user_secret` |
| 管理员 | 5 | `ak_demo_admin_secret` |
| 商户 | 5 | `mk_demo_m001_secret` / `mk_demo_m002_secret` / `mk_demo_m003_secret` |

stdio 模式下用环境变量指定角色：`AGENTMALL_API_KEY=ak_demo_admin_secret python -m agentmall.server`
`GET /whoami` 可以随时查当前 key 被解析成什么角色、能看到哪些 tool。

## 一键验证（不用接客户端）

想快速确认整条链路是否正常，可直接跑内置的 MCP stdio client——它以真实 MCP 协议
逐个调用 4 个 tool，复现 [demo/演示脚本.md](demo/演示脚本.md) 的全链路：

```bash
python tests/demo_chain.py       # 用户侧全链路（stdio 真实协议），17 项断言
python tests/test_readonly.py    # 只读边界守卫
python tests/test_roles.py       # 管理员/商户权限与审核流
python tests/test_http_auth.py   # streamable-http 接入 + 越权拒绝 + 审计
```

最近一次完整输出见 [demo/演示输出.md](demo/演示输出.md)。

## MCP Tools（一期）

| Tool | 功能 |
|------|------|
| `search_products` | 关键词/品类/最高价搜索，按价格排序 |
| `get_product` | 查看商品详情 |
| `create_order` | 创建订单（辅助支付，返回占位支付链接） |
| `get_order` | 查询订单状态 |

## 演示

按 [demo/演示脚本.md](demo/演示脚本.md) 走一遍，即可在 2 分钟内看到完整链路：
搜索"纸巾" → 比价 → 下单 → 拿到订单号 → 查询"待支付"状态。

## 路线图

- **Phase 1（本仓库当前）**：本地商品库 + **用户智能体** MCP 接口（搜索/比价/下单/查单）+ 辅助支付占位，
  验证"智能体能帮人买日用品"
- **Phase 2**：接入 1688 开放平台真实商品/库存/价格；接入真实支付网关替换占位链接；
  用户授权与额度管理；**商户智能体**（上架/改价/订单履约）+ **管理员智能体**（审核/风控/仲裁）上线，
  三角色权限隔离（见 [docs/角色架构.md](docs/角色架构.md)）
- **Phase 3**：智能体间议价、集采拼单、订阅式自动补货（A2A）

> 三智能体（用户 / 商户 / 管理员）功能与权限边界已定死，详见
> [docs/角色架构.md](docs/角色架构.md)。Phase 1 只做用户侧，不许混。

## 合规声明

本项目采用 **"checkout hands off"（结账交接）** 范式——这是 Anthropic 官方
commerce-agents 蓝图确立的智能体电商安全模式，也是 northcinder
"Buying stays a separate decision"（购买是独立决策）理念的中文落地：
**下单是智能体的事，付钱永远是用户的事。智能体只建单，
支付发生在用户自己的浏览器/App 里，模型碰不到钱。**

一期仅实现"智能体辅助支付"，不实现、不鼓励任何形式的智能体自动扣款。
该设计同时符合中国支付清算协会《智能体支付应用自律公约》第一阶段
（辅助支付）的要求；真实支付能力上线前将完成合规评估与报备。

## 设计参考

本项目站在以下开源项目的基础上设计：
- [anthropics/commerce-agents](https://github.com/anthropics/commerce-agents) —— "checkout hands off" 范式与 start-small 方法论
- [cinderline/northcinder](https://github.com/cinderline/northcinder) —— human-in-the-loop 结账流程与 Adapter 货源模式
- [jackwangfeng/keel](https://github.com/jackwangfeng/keel) —— per-agent key 分级权限与写操作提案队列
- [shopmanagerai/shopify-mcp](https://github.com/shopmanagerai/shopify-mcp) —— CI 强制只读保证与 operation ledger

差异化：以上没有一个覆盖"1688 一件代发 + 用户/商户/管理员三角色 + 中国支付合规"
这个组合——这正是 AgentMall 的位置。

## License

MIT
