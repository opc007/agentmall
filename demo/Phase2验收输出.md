# AgentMall Phase 2 验收输出记录

> 八套测试 + 路演彩排的真实执行输出，用于审核留痕。复现命令见文末。
> 生成时间：2026-10-07 21:04 · Linux / Python 3.11 / mcp 1.30.0 / fastapi 0.142.2 / uvicorn 0.54.0
>
> **演示声明：商品为演示数据（真实公开价格，整理自网络），支付为 MockPaymentGateway
> 模拟，不接任何真实支付网关，不发生任何真实资金交易，演示订单不触发发货。**

## 0. 三个服务

```bash
pip install -r requirements.txt

uvicorn agentmall.web.app:app --port 8000        # 用户面：注册/登录/个人中心/订单/收银台
python -m agentmall.http_server                   # MCP Server（streamable-http，:8001/mcp）
uvicorn agentmall.web.admin_app:app --port 8002   # 管理后台（独立进程）
```

## 1. 八套测试完整输出

```text
════════════════════════════════════════════════════════════
▶ tests/test_readonly.py
════════════════════════════════════════════════════════════
READONLY GUARD PASSED

════════════════════════════════════════════════════════════
▶ tests/demo_chain.py
════════════════════════════════════════════════════════════
Processing request of type ListToolsRequest
Processing request of type ListToolsRequest
Processing request of type CallToolRequest
Processing request of type CallToolRequest
Processing request of type CallToolRequest
Processing request of type CallToolRequest
Processing request of type CallToolRequest
Processing request of type CallToolRequest
Processing request of type CallToolRequest
Processing request of type CallToolRequest
Processing request of type CallToolRequest
Processing request of type CallToolRequest

[准备] MCP Server 已连接，工具注册表：
  - search_products: 搜索日用商品，按价格升序返回（只返回在售商品）。
  - get_product: 查看商品详情。product_id 如 "P001"。
  - create_order: 创建订单（辅助支付）。
  - get_order: 查询订单状态。order_id 如 "AMXXXXXXXXXX"。
  [PASS] 用户角色恰好 4 个 tool（实际 4 个：['search_products', 'get_product', 'create_order', 'get_order']）

==================================================================
第 1 步：搜索
用户：帮我找最便宜的抽纸
------------------------------------------------------------------
  P112  洁云 雅致生活 抽纸 3层100抽*27包        13.9 元/包
  P109  清风 抽纸 原木纯品 100抽 面巾纸         15.04 元/包
  P108  清风 四叶草系列 抽纸 3层100抽 S码24包    16.75 元/包
  P103  维达 细韧系列 抽纸 3层100抽*24包       16.93 元/包
  P104  维达 细韧抽纸 S码 3层100抽*20包 箱装    17.53 元/包
  P105  洁柔 Face 抽纸 缤纷系列 3层100抽*20包   17.7 元/包
  P110  心相印 抽纸 茶语丝享 110抽 3层S码10包    17.71 元/包
  P111  心相印 云感 悬挂式抽纸 4层320抽*4提      21.23 元/提
  P101  维达 超韧系列 抽纸 3层100抽*30包(195*  24.94 元/包
  [PASS] 搜索'纸巾'有结果（9 条）
  [PASS] 按价格升序
  [PASS] 只返回在售商品
  [PASS] max_price 过滤生效

==================================================================
第 2 步：比价
用户：P112 和另一个划算吗？
------------------------------------------------------------------
  P112  洁云 雅致生活 抽纸 3层100抽*27包      13.9 元/包 (原价 19.9)
  P101  维达 超韧系列 抽纸 3层100抽*30包(195* 24.94 元/包 (原价 29.8)
  [PASS] get_product 返回详情
  [PASS] 划线原价 >= 售价

==================================================================
第 3 步：下单
用户：买 2 份 P112，送到广西南宁市朝阳广场
------------------------------------------------------------------
{
  "order_id": "AMB33946EBE8",
  "items": [
    {
      "product_id": "P112",
      "merchant_id": "M001",
      "name": "洁云 雅致生活 抽纸 3层100抽*27包",
      "price": 13.9,
      "quantity": 2
    }
  ],
  "total": 27.8,
  "address": "广西南宁市朝阳广场",
  "note": "",
  "status": "待支付",
  "pay_url": "http://127.0.0.1:8000/pay/AMB33946EBE8?t=demo_AMB33946EBE8_1791378242",
  "is_demo": true,
  "payment_gateway": "mock",
  "created_at": 1791378242
}
  [PASS] 订单号 AM+10 位大写（AMB33946EBE8）
  [PASS] total=27.8（实际 27.8）
  [PASS] status=待支付（实际 待支付）
  [PASS] pay_url 非空
  [PASS] is_demo=1 标记

==================================================================
第 4 步：查单
用户：查一下刚才那个订单的状态
------------------------------------------------------------------
{
  "id": "AMB33946EBE8",
  "user_id": null,
  "total": 27.8,
  "address": "广西南宁市朝阳广场",
  "note": "",
  "status": "待支付",
  "pay_url": "http://127.0.0.1:8000/pay/AMB33946EBE8?t=demo_AMB33946EBE8_1791378242",
  "is_demo": true,
  "tracking_no": "",
  "created_at": 1791378242,
  "items": [
    {
      "product_id": "P112",
      "merchant_id": "M001",
      "name": "洁云 雅致生活 抽纸 3层100抽*27包",
      "price": 13.9,
      "quantity": 2
    }
  ],
  "order_id": "AMB33946EBE8"
}
  [PASS] 查回同一订单，状态待支付

==================================================================
异常演示
用户：搜个不存在的词 + 查不存在的单
------------------------------------------------------------------
  搜索'跑车' → []
  [PASS] 搜索无结果返回空列表，不崩溃
  get_order(AM0000000000) → {'error': '找不到订单 AM0000000000'}
  [PASS] 订单不存在返回 error 而非崩溃
  get_product(P99999) → {'error': '找不到商品 P99999'}
  [PASS] 商品不存在返回 error 而非崩溃
  超量下单 → {'error': '库存不足', 'available': 318, 'requested': 999999}
  [PASS] 超卖被拒绝，返回库存不足

==================================================================
演示链路 17/17 项通过
  ✅ 用户角色恰好 4 个 tool（实际 4 个：['search_products', 'get_product', 'create_order', 'get_order']）
  ✅ 搜索'纸巾'有结果（9 条）
  ✅ 按价格升序
  ✅ 只返回在售商品
  ✅ max_price 过滤生效
  ✅ get_product 返回详情
  ✅ 划线原价 >= 售价
  ✅ 订单号 AM+10 位大写（AMB33946EBE8）
  ✅ total=27.8（实际 27.8）
  ✅ status=待支付（实际 待支付）
  ✅ pay_url 非空
  ✅ is_demo=1 标记
  ✅ 查回同一订单，状态待支付
  ✅ 搜索无结果返回空列表，不崩溃
  ✅ 订单不存在返回 error 而非崩溃
  ✅ 商品不存在返回 error 而非崩溃
  ✅ 超卖被拒绝，返回库存不足
==================================================================

════════════════════════════════════════════════════════════
▶ tests/test_roles.py
════════════════════════════════════════════════════════════

▶ test_mask_address_only_city_level
  mask_address 用例通过

▶ test_publish_product_pending_review
  上架待审核 + 参数校验 通过

▶ test_review_approve_and_reject
  审核通过/驳回必填 reason 通过

▶ test_takedown_hides_from_user_search
  下架后用户不可见 通过（P101）

▶ test_merchant_cannot_touch_other_merchant_product
  商户数据隔离 通过

▶ test_merchant_list_orders_masks_address
  买家地址脱敏 + 订单隔离 通过

▶ test_update_and_fulfill_flow
  改价改库存/发货链路 通过

▶ test_admin_list_merchants_and_stats
  商户列表/平台看板 通过

▶ test_user_role_cannot_call_admin_tools
  用户/商户越权调用 admin tool 通过

▶ test_audit_log_is_append_only
  审计日志只追加 通过

▶ test_restart_persistence_and_role_matrix
  持久化/角色矩阵 通过

▶ test_demo_paid_order_counts_into_gmv
  演示单计入 GMV（真实链路）通过

============================================================
ROLES TESTS: 12/12 passed
ROLES TESTS PASSED ✅

════════════════════════════════════════════════════════════
▶ tests/test_http_auth.py
════════════════════════════════════════════════════════════
[准备] MCP HTTP 服务已起：http://127.0.0.1:37363/mcp

  [PASS] GET /healthz 返回 ok
  [PASS] user key 解析为 user 角色
  [PASS] admin key 解析为 admin 角色
  [PASS] merchant key 解析为 merchant 角色
  [PASS] 无效 key 被 401 拒绝（实际 401）
  [PASS] 用户 key 走 HTTP 能搜索（3 条）
  [PASS] 用户 key 调 admin_stats 被拒（{'error': '越权：user 角色无权调用 admin_stats', 'denied': True}）
  [PASS] 用户 key 调 merchant tool 被拒
  [PASS] 越权调用已记入 audit_log
  [PASS] 管理员 key 可访问 admin_stats（GMV=0.0）

============================================================
HTTP 鉴权 10/10 项通过
  ✅ GET /healthz 返回 ok
  ✅ user key 解析为 user 角色
  ✅ admin key 解析为 admin 角色
  ✅ merchant key 解析为 merchant 角色
  ✅ 无效 key 被 401 拒绝（实际 401）
  ✅ 用户 key 走 HTTP 能搜索（3 条）
  ✅ 用户 key 调 admin_stats 被拒（{'error': '越权：user 角色无权调用 admin_stats', 'denied': True}）
  ✅ 用户 key 调 merchant tool 被拒
  ✅ 越权调用已记入 audit_log
  ✅ 管理员 key 可访问 admin_stats（GMV=0.0）
============================================================

════════════════════════════════════════════════════════════
▶ tests/test_roadshow.py
════════════════════════════════════════════════════════════

================================================================
路演 7 步端到端验收（web=41099 mcp=51845）
================================================================
  [PASS] 第1步 网页可打开（HTTP 200）
  [PASS] 首页标注了演示数据/演示环境
  [PASS] 注册成功（HTTP 200）
  [PASS] 登录成功（HTTP 200）
  [PASS] 登录态 cookie 已下发
  [PASS] 第2步 个人中心可打开（HTTP 200）
  [PASS] 个人中心展示了用户 API key
  [PASS] 个人中心给出 MCP 接入点配置

--- 智能体（workbuddy）执行 ---
  [PASS] 智能体搜索到商品：洁云 雅致生活 抽纸 3层100抽*27包
  [PASS] 第5步 建单成功：AM43978A1AB3 total=27.8
  [PASS] 订单初始状态=待支付
  [PASS] pay_url 非空：http://127.0.0.1:41099/pay/AM43978A1AB3?t=demo_AM43978A1AB3_1791378257
  [PASS] 第6步 订单列表可打开（HTTP 200）
  [PASS] 订单列表里能看到刚下的单
  [PASS] 收银台页可打开（HTTP 200）
  [PASS] 收银台标注了「演示环境」
  [PASS] 收银台显示金额
  [PASS] 收银台含二维码
  [PASS] 二维码 PNG 可下载（HTTP 200，730 字节）
  [PASS] 确认支付成功（HTTP 200）
  [PASS] 订单状态变为「已完成」
  [PASS] 商品 P112 仍在售

================================================================
路演链路 22/22 项通过
  ✅ 第1步 网页可打开（HTTP 200）
  ✅ 首页标注了演示数据/演示环境
  ✅ 注册成功（HTTP 200）
  ✅ 登录成功（HTTP 200）
  ✅ 登录态 cookie 已下发
  ✅ 第2步 个人中心可打开（HTTP 200）
  ✅ 个人中心展示了用户 API key
  ✅ 个人中心给出 MCP 接入点配置
  ✅ 智能体搜索到商品：洁云 雅致生活 抽纸 3层100抽*27包
  ✅ 第5步 建单成功：AM43978A1AB3 total=27.8
  ✅ 订单初始状态=待支付
  ✅ pay_url 非空：http://127.0.0.1:41099/pay/AM43978A1AB3?t=demo_AM43978A1AB3_1791378257
  ✅ 第6步 订单列表可打开（HTTP 200）
  ✅ 订单列表里能看到刚下的单
  ✅ 收银台页可打开（HTTP 200）
  ✅ 收银台标注了「演示环境」
  ✅ 收银台显示金额
  ✅ 收银台含二维码
  ✅ 二维码 PNG 可下载（HTTP 200，730 字节）
  ✅ 确认支付成功（HTTP 200）
  ✅ 订单状态变为「已完成」
  ✅ 商品 P112 仍在售
================================================================

════════════════════════════════════════════════════════════
▶ tests/test_admin_console.py
════════════════════════════════════════════════════════════

============================================================
管理后台验收（:{port}）
============================================================
  [PASS] 未登录访问被挡回登录页（最终 200 http://127.0.0.1:47459/login）
  [PASS] 登录页标注演示环境
  [PASS] 登录页标题正确
  [PASS] 用户 key 不能进管理后台（%AE%A1%E7%90%86%E5%91%98%20key&level=err）
  [PASS] 管理员登录态已建立
  [PASS] 页面 / 可打开且内容正确（200）
  [PASS] 页面 /orders 可打开且内容正确（200）
  [PASS] 页面 /products 可打开且内容正确（200）
  [PASS] 页面 /audit 可打开且内容正确（200）
  [PASS] 看板订单数已 +1（1）
  [PASS] GMV 可读（0.0）
  [PASS] 全平台订单页看得到用户刚下的单
  [PASS] 管理员看到的买家地址已脱敏到市级
  [PASS] 门牌号未泄露
  [PASS] 下架不填 reason 被拒（F%85%E9%A1%BB%E5%A1%AB%E5%86%99%20reason&level=err）
  [PASS] 被拒后商品仍在售
  [PASS] 下架成功（%B7%B2%E4%B8%8B%E6%9E%B6%20P112&level=ok）
  [PASS] 商品状态变为下架
  [PASS] 验收标准5：下架后用户搜索不再返回该商品
  [PASS] 下架操作已记入审计日志并在页面可见
  [PASS] audit_log 里有下架记录（共 1 条）
  [PASS] 未登录重定向到带前缀的登录页（http://127.0.0.1:34527/admin/login）
  [PASS] 登录后回到带前缀的首页（http://127.0.0.1:34527/admin/）
  [PASS] 后台链接全部带 /admin 前缀（['/admin/', '/admin/audit', '/admin/logout', '/admin/orders', '/admin/products']）
  [PASS] 无双前缀（不会与 nginx sub_filter 叠加炸掉）
  [PASS] 带前缀下订单页仍可打开
  [PASS] audit_log 表禁 DELETE/UPDATE（触发器生效）

============================================================
管理后台 27/27 项通过
  ✅ 未登录访问被挡回登录页（最终 200 http://127.0.0.1:47459/login）
  ✅ 登录页标注演示环境
  ✅ 登录页标题正确
  ✅ 用户 key 不能进管理后台（%AE%A1%E7%90%86%E5%91%98%20key&level=err）
  ✅ 管理员登录态已建立
  ✅ 页面 / 可打开且内容正确（200）
  ✅ 页面 /orders 可打开且内容正确（200）
  ✅ 页面 /products 可打开且内容正确（200）
  ✅ 页面 /audit 可打开且内容正确（200）
  ✅ 看板订单数已 +1（1）
  ✅ GMV 可读（0.0）
  ✅ 全平台订单页看得到用户刚下的单
  ✅ 管理员看到的买家地址已脱敏到市级
  ✅ 门牌号未泄露
  ✅ 下架不填 reason 被拒（F%85%E9%A1%BB%E5%A1%AB%E5%86%99%20reason&level=err）
  ✅ 被拒后商品仍在售
  ✅ 下架成功（%B7%B2%E4%B8%8B%E6%9E%B6%20P112&level=ok）
  ✅ 商品状态变为下架
  ✅ 验收标准5：下架后用户搜索不再返回该商品
  ✅ 下架操作已记入审计日志并在页面可见
  ✅ audit_log 里有下架记录（共 1 条）
  ✅ 未登录重定向到带前缀的登录页（http://127.0.0.1:34527/admin/login）
  ✅ 登录后回到带前缀的首页（http://127.0.0.1:34527/admin/）
  ✅ 后台链接全部带 /admin 前缀（['/admin/', '/admin/audit', '/admin/logout', '/admin/orders', '/admin/products']）
  ✅ 无双前缀（不会与 nginx sub_filter 叠加炸掉）
  ✅ 带前缀下订单页仍可打开
  ✅ audit_log 表禁 DELETE/UPDATE（触发器生效）
============================================================

════════════════════════════════════════════════════════════
▶ tests/test_sourcing.py
════════════════════════════════════════════════════════════

【默认关闭：不出网】
  [PASS] 未启用时 sync_products 抛 SourceDisabled
  [PASS] 未启用时 get_agent_price 抛 SourceDisabled
  [PASS] 未启用时 create_order 抛 SourceDisabled
  [PASS] 未启用时 trace_logistics 抛 SourceDisabled
  [PASS] 未启用时 apply_refund 抛 SourceDisabled
  [PASS] 未启用时零网络调用（实际 0 次）
  [PASS] build_source 未启用时拒绝构造

【签名】
  [PASS] 签名 32 位大写十六进制（554154611089…）
  [PASS] 签名与参数顺序无关（按 key 升序）

【坑 3：token 有效期与 401 重放】
  [PASS] 首次取到 token（tok-1）
  [PASS] 未到期不重复取 token
  [PASS] 到期前自动续期并重新请求
  [PASS] 401 后重放一次成功，拿到 2 条商品

【坑 4：限流与退避重试】
  [PASS] 令牌桶限速生效（6 次耗用 0.003s）
  [PASS] 429 后退避重试成功（拿到 2 条）

【同步入库：待审核，用户搜不到】
  [PASS] sync_into 写入 2 条
  [PASS] 1688 同步的商品状态为待审核（['待审核', '待审核']）
  [PASS] list_all() 只返回在售，1688 新品不在其中

【坑 2：取价必须走 agent price】
  [PASS] 分销价取到 16.80（展示价 17.70，差 0.90）
  [PASS] 起订量一并返回
  [PASS] 确实调的是 agent price 接口而非商品接口（实际 ['alibaba.agent.price.get']）
  [PASS] 取价链路里没有误用普通商品接口（批发价陷阱）

【坑 1：代销关系白名单】
  [PASS] 白名单内可下单（{'ok': True, 'order_id': '1688ORDER001'}）
  [PASS] 白名单外被拦：以下 SKU 尚未建立代销关系，不能下单：['999']

【下单参数校验】
  [PASS] 空 SKU 被拒
  [PASS] 超过 50 个 SKU 被拒（接口上限）
  [PASS] 数量 0 被拒
  [PASS] 空地址被拒
  [PASS] 售后原因为空被拒

【物流与售后】
  [PASS] 物流轨迹可取
  [PASS] 售后申请可提交

============================================================
1688 适配层 31/31 项通过
  ✅ 未启用时 sync_products 抛 SourceDisabled
  ✅ 未启用时 get_agent_price 抛 SourceDisabled
  ✅ 未启用时 create_order 抛 SourceDisabled
  ✅ 未启用时 trace_logistics 抛 SourceDisabled
  ✅ 未启用时 apply_refund 抛 SourceDisabled
  ✅ 未启用时零网络调用（实际 0 次）
  ✅ build_source 未启用时拒绝构造
  ✅ 签名 32 位大写十六进制（554154611089…）
  ✅ 签名与参数顺序无关（按 key 升序）
  ✅ 首次取到 token（tok-1）
  ✅ 未到期不重复取 token
  ✅ 到期前自动续期并重新请求
  ✅ 401 后重放一次成功，拿到 2 条商品
  ✅ 令牌桶限速生效（6 次耗用 0.003s）
  ✅ 429 后退避重试成功（拿到 2 条）
  ✅ sync_into 写入 2 条
  ✅ 1688 同步的商品状态为待审核（['待审核', '待审核']）
  ✅ list_all() 只返回在售，1688 新品不在其中
  ✅ 分销价取到 16.80（展示价 17.70，差 0.90）
  ✅ 起订量一并返回
  ✅ 确实调的是 agent price 接口而非商品接口（实际 ['alibaba.agent.price.get']）
  ✅ 取价链路里没有误用普通商品接口（批发价陷阱）
  ✅ 白名单内可下单（{'ok': True, 'order_id': '1688ORDER001'}）
  ✅ 白名单外被拦：以下 SKU 尚未建立代销关系，不能下单：['999']
  ✅ 空 SKU 被拒
  ✅ 超过 50 个 SKU 被拒（接口上限）
  ✅ 数量 0 被拒
  ✅ 空地址被拒
  ✅ 售后原因为空被拒
  ✅ 物流轨迹可取
  ✅ 售后申请可提交
============================================================

════════════════════════════════════════════════════════════
▶ tests/test_restart.py
════════════════════════════════════════════════════════════

================================================================
崩溃重启留存（web=51461 mcp=34199）
================================================================
  [PASS] 注册用户 restartuser
  [PASS] 拿到该用户的 API key
  [PASS] 演示商品库里有在售商品
  [PASS] 建单成功 AM8CEE11114F total=49.88
  [PASS] 确认支付 HTTP 200
  [PASS] 管理员下架成功（记审计）：{'ok': True, 'product_id': 'P101', 'status': '下架', 'previous_status': '在售', 'reason': '崩溃重启留存测试'}
  [PASS] 库存已扣减 320 → 318（-2）
  [PASS] 审计日志有 1 条
  [PASS] 两个进程已 SIGKILL（不是优雅退出）
  [PASS] 库文件在进程全死后仍是可读且含该订单（WAL 未损坏）
  [PASS] 重启成功，两个服务都活过来了
  [PASS] orders 重启前后一致（1 → 1）
  [PASS] products 重启前后一致（59 → 59）
  [PASS] users 重启前后一致（2 → 2）
  [PASS] audit 重启前后一致（1 → 1）
  [PASS] 订单行完整：{'id': 'AM8CEE11114F', 'status': '已完成', 'total': 49.88}
  [PASS] 商品状态仍是「下架」，没被 seed 冲回在售
  [PASS] 库存没被二次扣减（仍为 318）
  [PASS] 崩溃前注册的用户仍能登录（HTTP 200）
  [PASS] 该用户的 api_key 仍有效，/me 能取到接入点
  [PASS] 订单列表里能看到崩溃前下的单
  [PASS] 审计禁删改触发器随库留存（2 个）
  [PASS] 重启后审计日志仍然删不掉
  [PASS] 重启后审计日志仍然改不掉
  [PASS] 重启后 admin_stats 正常：订单 1 · GMV ¥49.88
  [PASS] 重启后 GMV 仍为 ¥49.88（崩溃前付的款没丢）

================================================================
崩溃重启留存 26/26 项通过
  ✅ 注册用户 restartuser
  ✅ 拿到该用户的 API key
  ✅ 演示商品库里有在售商品
  ✅ 建单成功 AM8CEE11114F total=49.88
  ✅ 确认支付 HTTP 200
  ✅ 管理员下架成功（记审计）：{'ok': True, 'product_id': 'P101', 'status': '下架', 'previous_status': '在售', 'reason': '崩溃重启留存测试'}
  ✅ 库存已扣减 320 → 318（-2）
  ✅ 审计日志有 1 条
  ✅ 两个进程已 SIGKILL（不是优雅退出）
  ✅ 库文件在进程全死后仍是可读且含该订单（WAL 未损坏）
  ✅ 重启成功，两个服务都活过来了
  ✅ orders 重启前后一致（1 → 1）
  ✅ products 重启前后一致（59 → 59）
  ✅ users 重启前后一致（2 → 2）
  ✅ audit 重启前后一致（1 → 1）
  ✅ 订单行完整：{'id': 'AM8CEE11114F', 'status': '已完成', 'total': 49.88}
  ✅ 商品状态仍是「下架」，没被 seed 冲回在售
  ✅ 库存没被二次扣减（仍为 318）
  ✅ 崩溃前注册的用户仍能登录（HTTP 200）
  ✅ 该用户的 api_key 仍有效，/me 能取到接入点
  ✅ 订单列表里能看到崩溃前下的单
  ✅ 审计禁删改触发器随库留存（2 个）
  ✅ 重启后审计日志仍然删不掉
  ✅ 重启后审计日志仍然改不掉
  ✅ 重启后 admin_stats 正常：订单 1 · GMV ¥49.88
  ✅ 重启后 GMV 仍为 ¥49.88（崩溃前付的款没丢）
================================================================
```

## 2. 一键闸门 `./scripts/start_demo.sh --check`（实际输出）

起完三个服务后依次跑八套测试 + 路演彩排，总退出码 0。

```text
▶ 启动用户面 Web (:8000)…
▶ 启动 MCP Server (:8001/mcp)…
▶ 启动管理后台 (:8002)…

════════════════════════════════════════════════════════════
  ✅ AgentMall 已就绪（演示环境 · 不发生任何真实交易）
════════════════════════════════════════════════════════════
  用户面      http://127.0.0.1:8000
  管理后台    http://127.0.0.1:8002   （管理员 key: ak_demo_admin_secret）
  MCP 接入点  http://127.0.0.1:8001/mcp

  路演：注册 → 个人中心复制接入点 → 配给 AI 客户端
        → 说「找最便宜的抽纸，30 元以内」→ 网页订单点「去支付」→ 扫码确认
        → 切管理后台看订单数 +1、GMV 变化

  日志：/tmp/agentmall_{web,mcp,admin}.log
════════════════════════════════════════════════════════════

▶ 跑八套测试…
  test_readonly        PASSED
  demo_chain           链路 17/17 项通过
  test_roles           PASSED ✅
  test_http_auth       鉴权 10/10 项通过
  test_roadshow        链路 22/22 项通过
  test_admin_console   管理后台 27/27 项通过
  test_sourcing        适配层 31/31 项通过
  test_restart         26/26 项通过

▶ 路演彩排（对着刚起的这套服务跑 9 步）…
    ✅ 第 9 步 看板数字：订单数 +1、GMV 增加  [2m订单数 0 → 1（+1）· GMV ¥0.00 → ¥27.80（+¥27.80）[0m
  [32m[1m9 步全通，路演可以上[0m

完整彩排（含每步话术）：/tmp/agentmall_rehearse.log

🛑 关闭服务…
```

## 3. 结果汇总

| 测试 | 结果 | 覆盖 |
|---|---|---|
| `test_readonly.py` | READONLY GUARD PASSED | 只读边界守卫 + 超卖防护 |
| `demo_chain.py` | 演示链路 17/17 | MCP stdio 真实协议，搜索→比价→下单→查单+异常 |
| `test_roles.py` | ROLES TESTS PASSED ✅（12 项） | 三角色权限/审核/履约/审计 + **演示单计入 GMV 回归** |
| `test_http_auth.py` | HTTP 鉴权 10/10 | streamable-http 接入 + 越权拒绝 + 审计 |
| `test_roadshow.py` | 路演链路 22/22 | 路演 7 步端到端（真起 Web+MCP 两个进程） |
| `test_admin_console.py` | 管理后台 27/27 | 管理后台 + 验收标准 5 + `/admin` 路径前缀回归 |
| `test_sourcing.py` | 1688 适配层 31/31 | 默认禁用零外呼 + 签名/限流/代销白名单（全程 FakeTransport） |
| `test_restart.py` | 崩溃重启留存 26/26 | **验收标准 4：SIGKILL 砸掉后订单/商品/审计/触发器全在** |
| 路演彩排 | 9/9 | 9 步全链路 + 每步话术与救场预案 |

## 4. 对照 Phase2任务书 §6 验收标准

| # | 验收标准 | 落在哪套 | 结果 |
|---|---|---|---|
| 1 | 三个 server 跑起来；用户 key 调 merchant tool 被拒并记日志 | `test_http_auth.py` | ✅ |
| 2 | 全链路：下单扣库存 → 商户看到 → 商户发货 → 看板变化 | `test_roles.py` `test_roadshow.py` | ✅ |
| 3 | 商户 A 改不了商户 B 的商品；新上架待审核用户搜不到 | `test_roles.py` | ✅ |
| 4 | **kill 重启后订单/商品/审计都在** | `test_restart.py` | ✅ 26/26（此前无真实覆盖） |
| 5 | 管理员下架后用户搜索不再返回 | `test_admin_console.py` | ✅ |

## 5. 复现

```bash
git clone https://github.com/opc007/agentmall.git && cd agentmall
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 逐套
for t in test_readonly demo_chain test_roles test_http_auth test_roadshow \
         test_admin_console test_sourcing test_restart; do
  python tests/$t.py
done

# 或一键（八套 + 彩排）
./scripts/start_demo.sh --check

# 只想看路演话术
.venv/bin/python scripts/rehearse.py
```

预置演示 key：用户 `uk_demo_user_secret` / 管理员 `ak_demo_admin_secret` / 商户 `mk_demo_m00{1,2,3}_secret`
