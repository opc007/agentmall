"""AgentMall Web 前端（P0）。

FastAPI + Jinja2 纯 HTML，**刻意不引任何前端框架**（无 React/Vue/构建步骤），
CSS 全部本地化（static/style.css），沙箱无外网也能正常渲染。

合规红线（每个页面都由 templates/base.html 统一兜底，见 base.html 里的
`.demo-banner` 横幅与 `.compliance` 声明块）：
1. 商品数据为**演示数据**，非真实货源实时库存；
2. 支付是**模拟支付**（MockPaymentGateway），不接任何真实支付网关，不自动扣款；
3. checkout hands off：智能体只建单，**付钱永远是用户自己点**；
4. 演示订单 is_demo=1，支付后不触发发货（未对接真实履约）。

启动：uvicorn agentmall.web.app:app --port 8000
"""
