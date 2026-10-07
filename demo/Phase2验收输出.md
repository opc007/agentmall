# AgentMall Phase 2 验收输出记录

> 六套测试的真实执行输出，用于审核留痕。复现命令见文末。
> 生成时间：2026-10-07 · Linux / Python 3.11 / mcp 1.30.0 / fastapi 0.142
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

## 1. 六套测试完整输出

```text
════════════════════════════════════════════════════════════
▶ tests/test_readonly.py
════════════════════════════════════════════════════════════
READONLY GUARD PASSED

════════════════════════════════════════════════════════════
▶ tests/demo_chain.py
════════════════════════════════════════════════════════════

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
  "order_id": "AM1A3C6CF102",
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
  "pay_url": "http://127.0.0.1:8000/pay/AM1A3C6CF102?t=demo_AM1A3C6CF102_1791372901",
  "is_demo": true,
  "payment_gateway": "mock",
  "created_at": 1791372901
}
  [PASS] 订单号 AM+10 位大写（AM1A3C6CF102）
  [PASS] total=27.8（实际 27.8）
  [PASS] status=待支付（实际 待支付）
  [PASS] pay_url 非空
  [PASS] is_demo=1 标记

==================================================================
第 4 步：查单
用户：查一下刚才那个订单的状态
------------------------------------------------------------------
{
  "id": "AM1A3C6CF102",
  "user_id": null,
  "total": 27.8,
  "address": "广西南宁市朝阳广场",
  "note": "",
  "status": "待支付",
  "pay_url": "http://127.0.0.1:8000/pay/AM1A3C6CF102?t=demo_AM1A3C6CF102_1791372901",
  "is_demo": true,
  "tracking_no": "",
  "created_at": 1791372901,
  "items": [
    {
      "product_id": "P112",
      "merchant_id": "M001",
      "name": "洁云 雅致生活 抽纸 3层100抽*27包",
      "price": 13.9,
      "quantity": 2
    }
  ],
  "order_id": "AM1A3C6CF102"
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
  超量下单 → {'error': '库存不足', 'available': 328, 'requested': 999999}
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
  ✅ 订单号 AM+10 位大写（AM1A3C6CF102）
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

============================================================
ROLES TESTS: 11/11 passed
ROLES TESTS PASSED ✅

════════════════════════════════════════════════════════════
▶ tests/test_http_auth.py
════════════════════════════════════════════════════════════
[准备] MCP HTTP 服务已起：http://127.0.0.1:36073/mcp

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
路演 7 步端到端验收（web=58615 mcp=42913）
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
  [PASS] 第5步 建单成功：AM7A1C206E26 total=27.8
  [PASS] 订单初始状态=待支付
  [PASS] pay_url 非空：http://127.0.0.1:58615/pay/AM7A1C206E26?t=demo_AM7A1C206E26_1791372915
  [PASS] 第6步 订单列表可打开（HTTP 200）
  [PASS] 订单列表里能看到刚下的单
  [PASS] 收银台页可打开（HTTP 200）
  [PASS] 收银台标注了「演示环境」
  [PASS] 收银台显示金额
  [PASS] 收银台含二维码
  [PASS] 二维码 PNG 可下载（HTTP 200，718 字节）
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
  ✅ 第5步 建单成功：AM7A1C206E26 total=27.8
  ✅ 订单初始状态=待支付
  ✅ pay_url 非空：http://127.0.0.1:58615/pay/AM7A1C206E26?t=demo_AM7A1C206E26_1791372915
  ✅ 第6步 订单列表可打开（HTTP 200）
  ✅ 订单列表里能看到刚下的单
  ✅ 收银台页可打开（HTTP 200）
  ✅ 收银台标注了「演示环境」
  ✅ 收银台显示金额
  ✅ 收银台含二维码
  ✅ 二维码 PNG 可下载（HTTP 200，718 字节）
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
  [PASS] 未登录访问被挡回登录页（最终 200 http://127.0.0.1:47347/login）
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
  [PASS] audit_log 表禁 DELETE/UPDATE（触发器生效）

============================================================
管理后台 22/22 项通过
  ✅ 未登录访问被挡回登录页（最终 200 http://127.0.0.1:47347/login）
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
  ✅ audit_log 表禁 DELETE/UPDATE（触发器生效）
============================================================

```

---

## 2. 干净克隆复现证明

审核可直接照跑，验证仓库从零可复现（不依赖开发机任何残留状态）：

```bash
git clone https://github.com/opc007/agentmall.git && cd agentmall
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
for t in test_readonly demo_chain test_roles test_http_auth test_roadshow test_admin_console; do
  python tests/$t.py
done
```

实测结果（全新克隆 + 全新 venv，`mcp` 自动解析到 1.30.0）：

| 测试 | 结果 |
|---|---|
| test_readonly | READONLY GUARD PASSED |
| demo_chain | 演示链路 17/17 项通过 |
| test_roles | ROLES TESTS PASSED ✅ |
| test_http_auth | HTTP 鉴权 10/10 项通过 |
| test_roadshow | 路演链路 22/22 项通过 |
| test_admin_console | 管理后台 22/22 项通过 |

或更简单：`./scripts/start_demo.sh --check`（一键起三个服务并跑完六套测试）。
