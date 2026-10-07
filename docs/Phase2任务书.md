# AgentMall Phase 2 任务书：三角色全可用

**目标**：路演级——用户 / 商户 / 管理员三个智能体都能对接、真实可用。
路演现场：任意 AI 客户端配个 URL 连上，完整演示"用户下单 → 商户发货 → 管理员看板"全链路。

**读者**：MiniMax（开发）。Phase 1（用户侧 4 tool）已验收通过，在此基础上扩展，不推倒。

> **今晚（10-07）冲刺**：12 点前做多少算多少，优先级见 issue #2。
> 本任务书是完整版 spec，1–2 周节奏；今晚只按 P0→P1→P2 往前赶。

---

## 1. 做什么 / 不做什么

✅ **做**：
1. 数据持久化：SQLite（商品/商户/订单/审计日志），重启不丢
2. 公网可连：MCP server 支持 streamable-http，部署到公网服务器（服务器由用户提供， MiniMax 先本地开发，不阻塞）
3. 商户智能体：5 个 tool（下架/改价/改库存/看自己订单/发货）
4. 管理员智能体：5 个 tool（审核上架/下架/商户列表/平台看板/审计日志）
5. per-agent key 鉴权：key → 角色 → 可用 tool 过滤，越权调用直接拒绝
6. 用户侧升级：下单扣减库存（防超卖）、订单入库
7. 三角色演示脚本 v2 + 部署文档

🚫 **不做**（Phase 3）：
- 1688 真实对接（`Alibaba1688Source`，见 docs/货源接口选型.md）
- 真实支付网关（继续 checkout hands off 占位）
- 写操作"提案队列"（人审批）：Phase 2 商户改自己店的东西直接执行，管理员写操作记审计日志即足够；提案队列放 Phase 3
- 前端网页（路演用 MCP 直连演示，不做网页）

## 2. 架构

```
                    ┌─ shop MCP server (port 8001) ── 用户 key
公网服务器 ──────────┼─ merchant MCP server (port 8002) ── 商户 key → merchant_id
(streamable-http)   └─ admin MCP server (port 8003) ── 管理员 key
                              │
                         SQLite（共用）
              products / merchants / orders / audit_log
```

- 三个独立 server，共用 SQLite 数据层；tool 按前缀天然隔离
  （`shop_*` 其实就是现有 4 个 tool，Phase 2 可改名或保持，MiniMax 定，保持向后兼容优先）。
- 鉴权契约：每个请求带 `Authorization: Bearer <key>`；
  key 在 `merchants`/`admin_keys` 表里查角色，server 只暴露该角色的 tool。
  实现方式（中间件/网关）MiniMax 定，契约不变即可。
- Seed：预置 2–3 个测试商户（含 key）+ 30 个 SKU 迁入 SQLite。

## 3. Tool 定义

### 商户智能体（需 merchant key，只能操作自己的 merchant_id）

| tool | 参数 | 返回 |
|------|------|------|
| `merchant_publish_product` | name, category, price, stock, unit, specs | `{product_id, status: "待审核"}`（上架先待审核，管理员审过才在售） |
| `merchant_update_product` | product_id, price?, stock?, on_sale? | ok / error（只能改自己的，改别人的直接拒绝） |
| `merchant_list_products` | — | 自己商户的全部商品（含待审核/下架） |
| `merchant_list_orders` | status? | 自己店铺的订单（买家地址脱敏：只给区级） |
| `merchant_fulfill_order` | order_id, tracking_no? | 发货，订单状态 → 待收货 |

### 管理员智能体（需 admin key，所有写操作记审计日志）

| tool | 参数 | 返回 |
|------|------|------|
| `admin_review_product` | product_id, approve: bool, reason? | 审核通过 → 在售；驳回 → reason 必填 |
| `admin_takedown_product` | product_id, reason | 下架（reason 必填，记审计） |
| `admin_list_merchants` | — | 商户列表（id/名称/商品数/订单数） |
| `admin_stats` | — | `{orders, gmv, merchants, products, 待审核数}` |
| `admin_audit_log` | limit=50 | 审计记录（who/when/做了什么） |

### 用户智能体（现有 4 个，保持兼容）

- `search_products` 只返回 `status=在售` 的商品
- `create_order` 新增：校验 `quantity <= stock`，下单成功扣减库存；
  超卖返回 `{"error": "库存不足"}`

## 4. 数据模型（SQLite）

```sql
products(id, merchant_id, name, category, price, stock, unit, specs,
         status,              -- 待审核 / 在售 / 下架
         created_at)
merchants(id, name, api_key, created_at)
admin_keys(id, name, api_key, created_at)
orders(id, items_json, total, address, status, pay_url, created_at)
         -- status: 待支付 / 待发货 / 待收货 / 完成 / 售后中
audit_log(id, actor_role, actor_id, action, target, detail, created_at)
```

## 5. 里程碑（14 天）

| 时间 | 任务 |
|------|------|
| Day 1–3 | SQLite 数据层 + seed 迁移；用户侧持久化（订单入库、下单扣库存） |
| Day 4–7 | 三 server 拆分 + per-agent key 鉴权；商户侧 5 tool |
| Day 8–10 | 管理员侧 5 tool + audit log；三角色演示脚本 v2 |
| Day 11–14 | 公网部署（等服务器）+ 三端联调 + 路演彩排 |

## 6. 验收标准

1. 三个 server 在公网（或本地）各自跑起来；拿 user key 调 merchant tool 被拒绝并记日志
2. 全链路：用户下单（库存扣减）→ 商户看到订单 → 商户发货 → 管理员看板 GMV/订单数变化
3. 商户 A 的 key 改不了商户 B 的商品；新上架商品待审核，用户搜不到
4. kill 重启后订单/商品/审计都在
5. 管理员下架商品后，用户搜索不再返回

## 7. 合规红线（沿用，不重复）

- checkout hands off：智能体只建单，支付永远用户自己完成，不做自动扣款
- 商户只能看自己店铺订单，买家地址脱敏
- 管理员所有写操作记审计日志，不可删改

## 8. 需要用户准备的

- **公网服务器一台**（MiniMax 先本地开发，不阻塞；部署时需要）。
  建议：复用 AI电影梦 现有服务器，或新开一台轻量云服务器。
