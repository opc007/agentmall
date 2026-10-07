"""支付网关抽象：接口 + 模拟实现。

合规红线（开发任务书 §7 / Phase2任务书 §7）：checkout hands off——
智能体只建单，付款永远是用户本人在浏览器里点确认，绝不自动扣款。
`MockPaymentGateway` 只是把"用户点确认"这一步搬到网页上，
真实网关上线时只换实现类，上层调用点不用改。
"""
import json
import os
import time

MOCK_BASE = os.environ.get(
    "AGENTMALL_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")


class PaymentGateway:
    """支付网关接口。Phase 2 真实网关（微信/支付宝）实现本接口即可。"""

    name = "base"

    def create_payment(self, order_id: str, total: float,
                       subject: str = "") -> dict:
        """为订单生成收银台链接。返回 {pay_url, gateway, is_demo}。"""
        raise NotImplementedError

    def verify(self, order_id: str, token: str | None = None) -> dict:
        """校验支付结果。本 MVP 只做模拟确认。"""
        raise NotImplementedError


class MockPaymentGateway(PaymentGateway):
    """演示用模拟网关：不碰任何真实资金通道，页面上标注"演示环境"。

    pay_url 指向本地收银台页（带二维码），用户点"确认支付"才置为已支付。
    """

    name = "mock"

    def create_payment(self, order_id: str, total: float,
                       subject: str = "") -> dict:
        token = f"demo_{order_id}_{int(time.time())}"
        return {
            "pay_url": f"{MOCK_BASE}/pay/{order_id}?t={token}",
            "gateway": self.name,
            "is_demo": True,
            "token": token,
            "total": total,
            "subject": subject or f"AgentMall 订单 {order_id}",
        }

    def verify(self, order_id: str, token: str | None = None) -> dict:
        """模拟网关不做真实校验，确认即通过（演示环境）。"""
        return {"verified": True, "gateway": self.name, "is_demo": True,
                "order_id": order_id}


gateway: PaymentGateway = MockPaymentGateway()


def qr_png_bytes(content: str) -> bytes:
    """把支付确认页 URL 渲染成二维码 PNG（演示用，不含任何敏感信息）。"""
    import io

    import qrcode

    img = qrcode.make(content)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
