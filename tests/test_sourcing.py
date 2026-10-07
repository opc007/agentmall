"""1688 适配层离线单测（不发出任何真实网络请求）。

覆盖 `docs/货源接口选型.md` 列的 4 个业务坑：
  1. 代销关系逐商家申请 → create_order 白名单拦截
  2. agent_price ≠ 批发价 → get_agent_price 是独立取价入口
  3. token 有效期 2–10 小时 → TokenManager 自动续期 + 401 重放
  4. 限流 5–10 次/秒 → RateLimiter 令牌桶 + 退避重试

运行： .venv/bin/python tests/test_sourcing.py
"""
import os
import sys
import tempfile
import time

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

os.environ["AGENTMALL_DB"] = os.path.join(tempfile.gettempdir(), "agentmall_test_sourcing.db")
os.environ["AGENTMALL_1688_ENABLED"] = "1"
os.environ["AGENTMALL_1688_APP_KEY"] = "test_key"
os.environ["AGENTMALL_1688_APP_SECRET"] = "test_secret"

from agentmall import db, sourcing  # noqa: E402
from agentmall.sourcing import (Alibaba1688Source, ApiError, OrderDraft,  # noqa: E402
                                ProductRow, RateLimiter, SourceDisabled,
                                TokenManager, _sign)

checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    checks.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


class FakeTransport:
    """模拟 1688 网关，统计调用次数，绝不出网。"""

    def __init__(self, token_expires_in: int = 7200):
        self.calls: list[tuple[str, dict]] = []
        self.token_expires_in = token_expires_in
        self.token_seq = 0
        self.fail_once: dict[str, int] = {}

    def post(self, path: str, params: dict) -> dict:
        self.calls.append((path, dict(params)))
        method = params.get("method", "")
        if path.endswith("/token"):
            self.token_seq += 1
            return {"result": {"accessToken": f"tok-{self.token_seq}",
                               "expiresIn": self.token_expires_in}}
        left = self.fail_once.get(method, 0)
        if left:
            self.fail_once[method] = left - 1
            raise ApiError("429", "flow control")
        if method == "alibaba.product.search":
            return {"result": {"data": [
                {"productId": "123456", "subject": "洁柔抽纸 3层100抽*24包",
                 "categoryName": "纸巾", "price": "17.70", "originalPrice": "24.60",
                 "amountOnSale": 480, "unit": "包", "attributes": "3层/100抽",
                 "image": {"url": "https://img.example/123456.jpg"},
                 "supplierId": "S001", "isSupportAgencyOrder": True},
                {"productId": "789", "subject": "维达抽纸 整箱", "categoryName": "纸巾",
                 "price": "59.90", "stock": 150, "supplierId": "S002"},
            ]}}
        if method == "alibaba.agent.price.get":
            return {"result": {"data": [
                {"productId": "123456", "agentPrice": "16.80", "moq": 2,
                 "currency": "CNY"}]}}
        if method == "alibaba.trade.createOrder":
            return {"result": {"orderId": "1688ORDER001"}}
        if method == "alibaba.logistics.trace":
            return {"result": {"trace": [{"time": "2026-10-08 10:00",
                                          "desc": "已揽收"}]}}
        if method == "alibaba.trade.refund.apply":
            return {"result": {"id": "R001"}}
        raise AssertionError(f"未预期的接口：{method}")


def fresh_source(transport=None, **kw):
    t = transport or FakeTransport()
    return Alibaba1688Source(transport=t, limiter=RateLimiter(rate=1000, capacity=1000),
                             **kw), t


def main() -> int:
    for sfx in ("", "-wal", "-shm"):
        p = os.environ["AGENTMALL_DB"] + sfx
        if os.path.exists(p):
            os.remove(p)
    db.init_db()

    print("\n【默认关闭：不出网】")
    os.environ["AGENTMALL_1688_ENABLED"] = "0"
    blocked_t = FakeTransport()
    blocked = Alibaba1688Source(transport=blocked_t)
    for name, fn in [("sync_products", lambda: blocked.sync_products()),
                     ("get_agent_price", lambda: blocked.get_agent_price(["1"])),
                     ("create_order", lambda: blocked.create_order(OrderDraft(["1"], 1, "x"))),
                     ("trace_logistics", lambda: blocked.trace_logistics("1")),
                     ("apply_refund", lambda: blocked.apply_refund("1", "r"))]:
        try:
            fn()
            check(False, f"未启用时 {name} 应抛 SourceDisabled")
        except SourceDisabled:
            check(True, f"未启用时 {name} 抛 SourceDisabled")
    check(len(blocked_t.calls) == 0,
          f"未启用时零网络调用（实际 {len(blocked_t.calls)} 次）")
    try:
        sourcing.build_source(enabled=False)
        check(False, "build_source 未启用时应拒绝")
    except SourceDisabled:
        check(True, "build_source 未启用时拒绝构造")
    os.environ["AGENTMALL_1688_ENABLED"] = "1"

    print("\n【签名】")
    sig = _sign({"b": "2", "a": "1", "sign": "x", "empty": ""}, "SECRET")
    check(sig == sig.upper() and len(sig) == 32 and sig.isalnum(),
          f"签名 32 位大写十六进制（{sig[:12]}…）")
    check(_sign({"a": "1", "b": "2"}, "S") == _sign({"b": "2", "a": "1"}, "S"),
          "签名与参数顺序无关（按 key 升序）")

    print("\n【坑 3：token 有效期与 401 重放】")
    t = FakeTransport(token_expires_in=7200)
    src, t = fresh_source(t)
    src.sync_products()
    first = src.tokens.get()
    check(first == "tok-1", f"首次取到 token（{first}）")
    check(src.tokens.get() == "tok-1", "未到期不重复取 token")
    t2 = FakeTransport(token_expires_in=7200)
    src2, _ = fresh_source(t2)
    src2.tokens = TokenManager(src2._fetch_token, skew=10000)  # 强制立即过期
    before = len(t2.calls)
    src2.sync_products()
    check(len(t2.calls) > before, "到期前自动续期并重新请求")

    t3 = FakeTransport()
    src3, t3 = fresh_source(t3)
    src3.tokens._expires_at = time.time() + 99999
    t3.fail_once["alibaba.product.search"] = 0
    # 构造一次 401
    class T401(FakeTransport):
        def post(self, path, params):
            r = super().post(path, params)
            if params.get("method") == "alibaba.product.search" and \
                    getattr(self, "_401_done", False) is False:
                self._401_done = True
                raise ApiError("401", "token expired")
            return r
    t401 = T401()
    src4, _ = fresh_source(t401)
    ok = src4.sync_products()
    check(len(ok) == 2, "401 后重放一次成功，拿到 2 条商品")

    print("\n【坑 4：限流与退避重试】")
    rl = RateLimiter(rate=1000, capacity=3)
    t0 = time.monotonic()
    for _ in range(6):
        rl.acquire()
    el = time.monotonic() - t0
    check(el < 0.5, f"令牌桶限速生效（6 次耗用 {el:.3f}s）")
    t5 = FakeTransport()
    t5.fail_once["alibaba.product.search"] = 1  # 第一次 429
    src5, _ = fresh_source(t5)
    rows = src5.sync_products()
    check(len(rows) == 2, f"429 后退避重试成功（拿到 {len(rows)} 条）")

    print("\n【同步入库：待审核，用户搜不到】")
    t6 = FakeTransport()
    src6, _ = fresh_source(t6)
    n = src6.sync_into(limit=50)
    check(n == 2, f"sync_into 写入 {n} 条")
    conn = db.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id,name,status FROM products WHERE id LIKE 'P%' AND name LIKE '%洁柔抽纸%' "
            "OR name LIKE '%维达抽纸%'").fetchall()]
    finally:
        conn.close()
    check(bool(rows) and all(r["status"] == "待审核" for r in rows),
          f"1688 同步的商品状态为待审核（{[r['status'] for r in rows]}）")
    pid = rows[0]["id"] if rows else None
    ids = [r["id"] for r in src6.list_all()]
    check(pid not in ids, "list_all() 只返回在售，1688 新品不在其中")

    print("\n【坑 2：取价必须走 agent price】")
    t7 = FakeTransport()
    src7, t7 = fresh_source(t7)
    pr = src7.get_agent_price(["123456"])
    check(pr["123456"]["agent_price"] == 16.8,
          f"分销价取到 16.80（展示价 17.70，差 {17.70 - 16.8:.2f}）")
    check(pr["123456"]["moq"] == 2, "起订量一并返回")
    methods = [c[1].get("method") for c in t7.calls if c[1].get("method")]
    check(methods and methods[-1] == "alibaba.agent.price.get",
          f"确实调的是 agent price 接口而非商品接口（实际 {methods}）")
    check("alibaba.product.get" not in methods,
          "取价链路里没有误用普通商品接口（批发价陷阱）")

    print("\n【坑 1：代销关系白名单】")
    src8, _ = fresh_source(FakeTransport())
    d_ok = OrderDraft(["123456"], 2, "浙江金华义乌某仓库", allowed_merchant_ids={"123456"})
    r = src8.create_order(d_ok)
    check(r.get("ok") and r.get("order_id") == "1688ORDER001", f"白名单内可下单（{r}）")
    d_bad = OrderDraft(["999"], 1, "地址", allowed_merchant_ids={"123456"})
    r2 = src8.create_order(d_bad)
    check("代销关系" in str(r2.get("error", "")), f"白名单外被拦：{r2.get('error')}")

    print("\n【下单参数校验】")
    check(src8.create_order(OrderDraft([], 1, "a")).get("error"),
          "空 SKU 被拒")
    check(src8.create_order(OrderDraft([str(i) for i in range(51)], 1, "a")).get("error"),
          "超过 50 个 SKU 被拒（接口上限）")
    check(src8.create_order(OrderDraft(["1"], 0, "a")).get("error"), "数量 0 被拒")
    check(src8.create_order(OrderDraft(["1"], 1, "  ")).get("error"), "空地址被拒")
    check(src8.apply_refund("1", "").get("error"), "售后原因为空被拒")

    print("\n【物流与售后】")
    src9, _ = fresh_source(FakeTransport())
    check(len(src9.trace_logistics("1688ORDER001")) == 1, "物流轨迹可取")
    check(src9.apply_refund("1688ORDER001", "买家少收了一件").get("ok"), "售后申请可提交")

    passed = sum(1 for ok, _ in checks if ok)
    print(f"\n{'=' * 60}\n1688 适配层 {passed}/{len(checks)} 项通过")
    for ok, label in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    print("=" * 60)
    for sfx in ("", "-wal", "-shm"):
        p = os.environ["AGENTMALL_DB"] + sfx
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
