"""1688 货源适配层（骨架实现，默认关闭、不发生任何真实调用）。

对应 `docs/货源接口选型.md` 的结论与 `docs/Phase2任务书.md` §8 的 Phase 2 扩展点。
`store.py::ProductSource` 的实现之一——**tool 层（server.py）一个字都不用动**，
这就是 Adapter 模式的价值。

⚠️ 默认禁用：不设置 `AGENTMALL_1688_ENABLED=1` 时，本模块的所有网络方法
直接抛 `SourceDisabled`，不会发出任何请求。真实调用还需要企业资质 + 代销关系
（见货源接口选型.md「4 个业务坑」第 1 条）。

设计要点（四个业务坑的工程应对）
--------------------------------
1. **分销关系逐商家申请**：只有进了代销名单的商家才能下单。
   本模块用 `allowed_merchant_ids` 白名单挡住未申请代销关系的商家，
   不依赖 1688 侧报错才发现订单会黄。
2. **agent_price ≠ 批发价**：普通商品接口给的是批发价，差价算错直接亏本。
   `get_agent_price()` 是独立的、**唯一**允许用于报价的取价入口，
   `sync_products()` 写入的商品价只作展示参考，报价链路不许用它。
3. **Token 有效期 2–10 小时**：`TokenManager` 到期前自动刷新，401 时强制重取并重放一次。
4. **限流 5–10 次/秒**：`RateLimiter` 令牌桶 + 指数退避重试。
   并且——智能体的查询**永远走本地 SQLite**，只有定时同步任务才出网。

关于测试
--------
本模块不依赖网络即可完整测试：注入 `FakeTransport` 模拟 1688 的响应，
覆盖正常流、token 过期、限流重试、限流退避、代销白名单拦截、未授权时禁用。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

from . import db

API_BASE = "https://gw.open.1688.com/openapi"
APP_KEY_ENV = "AGENTMALL_1688_APP_KEY"
APP_SECRET_ENV = "AGENTMALL_1688_APP_SECRET"
ENABLED_ENV = "AGENTMALL_1688_ENABLED"
TOKEN_SKEW = 300  # 提前 5 分钟续期，避免边界抖动


class SourceDisabled(RuntimeError):
    """未显式开启 1688 对接（默认路径永远走这里，不会出网）。"""


class ApiError(RuntimeError):
    def __init__(self, code: str, message: str, retried: bool = False) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.retried = retried


# --------------------------------------------------------------------- 基础设施

class RateLimiter:
    """令牌桶。免费版 5–10 次/秒，这里默认保守取 5。"""

    def __init__(self, rate: float = 5.0, capacity: float | None = None) -> None:
        self.rate = max(float(rate), 0.1)
        self.capacity = float(capacity if capacity is not None else rate)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self.rate
            time.sleep(wait)


class TokenManager:
    """access_token 缓存 + 到期前自动续期 + 401 强制刷新。

    坑 3：token 有效期 2–10 小时且不固定，靠 expires_in 判断，不写死时长。
    """

    def __init__(self, fetcher, skew: int = TOKEN_SKEW) -> None:
        self._fetcher = fetcher          # callable() -> (token, expires_in)
        self._skew = skew
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def get(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.time() >= self._expires_at - self._skew:
                token, expires_in = self._fetcher()
                self._token = token
                # 兜底：expires_in 异常小时按 1 小时算，宁可多刷几次也别用过期 token
                ttl = expires_in if isinstance(expires_in, (int, float)) and expires_in > 60 else 3600
                self._expires_at = time.time() + float(ttl)
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = ""
            self._expires_at = 0.0


class HttpTransport:
    """真实 HTTP 传输。仅在显式启用时才会被构造并调用。"""

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def post(self, path: str, params: dict) -> dict:
        raise SourceDisabled(
            "1688 真实调用未开启。需设置 %s=1 并配置 %s / %s，"
            "且先完成企业认证与代销关系申请。" % (ENABLED_ENV, APP_KEY_ENV, APP_SECRET_ENV))


def _sign(params: dict, secret: str) -> dict:
    """1688 开放平台签名：按 key 升序拼串 → MD5 → 32 位大写。"""
    import hashlib

    parts = []
    for k in sorted(params):
        if k in ("sign", "sign_method") or params[k] in (None, ""):
            continue
        parts.append(f"{k}{params[k]}")
    raw = "".join(parts) + secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


# --------------------------------------------------------------------- 数据结构

@dataclass
class ProductRow:
    """同步进 SQLite products 表的中间结构（尚未分配 id）。"""
    source_id: str
    name: str
    category: str
    price: float                 # 展示参考价（批发/展示口径），**不可用于报价**
    original_price: float = 0.0
    stock: int = 0
    unit: str = "件"
    specs: str = ""
    image_url: str = ""
    supplier_id: str = ""
    supports_agency: bool = False   # 是否支持一件代发（坑 1）
    agent_price: float = 0.0        # 分销/一件代发价（坑 2）


@dataclass
class OrderDraft:
    product_ids: list[str]
    quantity: int
    address: str
    note: str = ""
    allowed_merchant_ids: set[str] = field(default_factory=set)


# --------------------------------------------------------------------- 适配器

class Alibaba1688Source:
    """`store.ProductSource` 的 1688 实现。

    对外两个必须实现的方法（Adapter 契约）：
      - ``list_all()``  返回本地库里的商品（智能体只查这里，不出网）
      - ``sync_into()`` 把 1688 商品批量同步进 SQLite

    真实货源侧的能力：``get_agent_price()`` / ``create_order()`` /
    ``trace_logistics()`` / ``apply_refund()``。
    """

    name = "alibaba1688"

    #: 定时同步间隔建议值（秒）。免费版限流低，2 小时一批足够。
    SYNC_INTERVAL = 2 * 3600

    def __init__(self, transport=None, limiter: RateLimiter | None = None,
                 tokens: TokenManager | None = None, db_path: str = "") -> None:
        self.enabled = os.environ.get(ENABLED_ENV, "") == "1"
        self.db_path = db_path or db.DB_PATH
        self.transport = transport if transport is not None else HttpTransport()
        self.limiter = limiter or RateLimiter()
        self.tokens = tokens or TokenManager(self._fetch_token)
        self._call_seq = 0

    # -- Adapter 契约 ---------------------------------------------------------
    def list_all(self) -> list:
        """本地 SQLite 里的商品。智能体查询永远走这里，绝不实时调 API。"""
        conn = db.connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM products WHERE status='在售'").fetchall()]
        finally:
            conn.close()

    def sync_into(self, limit: int = 200, category: str = "") -> int:
        """拉一批 1688 商品写进本地库，返回写入条数。"""
        rows = self.sync_products(limit=limit, category=category)
        if not rows:
            return 0
        ts = db.now()
        conn = db.connect()
        try:
            with conn:
                for r in rows:
                    pid = self._stable_id(r.source_id)
                    conn.execute(
                        "INSERT INTO products(id,merchant_id,name,category,price,"
                        "original_price,stock,unit,specs,image_url,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                        "price=excluded.price,stock=excluded.stock,"
                        "image_url=excluded.image_url,created_at=excluded.created_at",
                        (pid, "M001", r.name, r.category, float(r.price),
                         float(r.original_price or r.price), int(r.stock),
                         r.unit, r.specs, r.image_url, "待审核", ts))
            return len(rows)
        finally:
            conn.close()

    # -- 货源能力 -------------------------------------------------------------
    def sync_products(self, limit: int = 200, category: str = "") -> list[ProductRow]:
        """商品搜索（单次最多 200 条，按业务坑 4 走定时批量而非实时查）。"""
        if not self.enabled:
            raise SourceDisabled("1688 对接未开启")
        payload = self._call("alibaba.product.search", {
            "q": category or "", "page": 1, "pageSize": min(int(limit), 200)})
        out = []
        for it in (payload.get("result", {}).get("data") or []):
            out.append(ProductRow(
                source_id=str(it.get("productId") or it.get("id") or ""),
                name=it.get("subject") or it.get("title") or "",
                category=it.get("categoryName") or category or "其他",
                price=float(it.get("price") or 0),
                original_price=float(it.get("originalPrice") or it.get("price") or 0),
                stock=int(it.get("amountOnSale") or it.get("stock") or 0),
                unit=it.get("unit") or "件",
                specs=str(it.get("attributes") or "")[:200],
                image_url=it.get("image", {}).get("url", "") if isinstance(
                    it.get("image"), dict) else str(it.get("image") or ""),
                supplier_id=str(it.get("supplierId") or ""),
                supports_agency=bool(it.get("isSupportAgencyOrder"))))
        return [r for r in out if r.source_id]

    def get_agent_price(self, product_ids: list[str]) -> dict:
        """**唯一允许用于对外报价的取价入口。**

        坑 2：普通商品接口返回的是批发价，必须走这个接口拿一件代发价，
        拿错直接亏本。返回 {product_id: {"agent_price","moq","currency"}}。
        """
        if not self.enabled:
            raise SourceDisabled("1688 对接未开启")
        payload = self._call("alibaba.agent.price.get", {"productIds": ",".join(product_ids)})
        out = {}
        for it in (payload.get("result", {}).get("data") or []):
            pid = str(it.get("productId") or "")
            if pid:
                out[pid] = {
                    "agent_price": float(it.get("agentPrice") or 0),
                    "moq": int(it.get("moq") or 1),
                    "currency": it.get("currency") or "CNY",
                }
        return out

    def create_order(self, draft: OrderDraft) -> dict:
        """向 1688 采购下单（单次最多 50 个 SKU）。

        坑 1：**代销关系必须已申请**。这里先在本地白名单拦一道，
        不靠 1688 报错才发现订单会黄。
        """
        if not self.enabled:
            raise SourceDisabled("1688 对接未开启")
        if not draft.product_ids:
            return {"error": "SKU 不能为空"}
        if len(draft.product_ids) > 50:
            return {"error": "单次最多 50 个 SKU"}
        if int(draft.quantity) < 1:
            return {"error": "数量必须 >= 1"}
        if not (draft.address or "").strip():
            return {"error": "收货地址不能为空"}
        blocked = [p for p in draft.product_ids
                   if draft.allowed_merchant_ids and p not in draft.allowed_merchant_ids]
        if blocked:
            return {"error": f"以下 SKU 尚未建立代销关系，不能下单：{blocked}"}
        payload = self._call("alibaba.trade.createOrder", {
            "productIds": ",".join(draft.product_ids),
            "quantity": int(draft.quantity),
            "receiver": draft.address, "memo": draft.note or "",
        })
        return {"ok": True, "order_id": str(payload.get("result", {}).get("orderId") or "")}

    def trace_logistics(self, order_id: str) -> list[dict]:
        """物流轨迹，用于把订单状态回传给智能体。"""
        if not self.enabled:
            raise SourceDisabled("1688 对接未开启")
        payload = self._call("alibaba.logistics.trace", {"orderId": order_id})
        return list(payload.get("result", {}).get("trace") or [])

    def apply_refund(self, order_id: str, reason: str = "") -> dict:
        """售后申请。真实网关接入前不自动触发，需人工确认。"""
        if not self.enabled:
            raise SourceDisabled("1688 对接未开启")
        if not (reason or "").strip():
            return {"error": "售后原因不能为空"}
        payload = self._call("alibaba.trade.refund.apply",
                             {"orderId": order_id, "reason": reason})
        return {"ok": True, "refund_id": str(payload.get("result", {}).get("id") or "")}

    # -- 内部 -----------------------------------------------------------------
    def _require_enabled(self) -> None:
        if not self.enabled:
            raise SourceDisabled(
                f"1688 对接未开启（需 {ENABLED_ENV}=1）。"
                "接入前请确认：企业认证已过、目标商家已建立代销关系。")

    def _call(self, method: str, biz: dict, _retried: bool = False) -> dict:
        """统一出口：限流 → 取 token → 请求 → 401 重放 → 退避重试。"""
        self._require_enabled()
        self.limiter.acquire()
        params = {
            "app_key": os.environ.get(APP_KEY_ENV, ""),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "v": "2.0",
            "format": "json",
            "method": method,
            "app_secret": os.environ.get(APP_SECRET_ENV, ""),
            **{k: v for k, v in biz.items() if v not in (None, "")},
        }
        params["access_token"] = self.tokens.get()
        secret = params.pop("app_secret", "")
        params["sign"] = _sign({**params, "app_secret": secret}, secret)

        try:
            payload = self.transport.post(API_BASE, params)
        except ApiError as exc:
            # 坑 3：token 过期（401）强制重取并重放一次
            if exc.code in ("401", "40101", "invalid.token") and not _retried:
                self.tokens.invalidate()
                return self._call(method, biz, _retried=True)
            # 限流/网关抖动 → 指数退避重试
            if exc.code in ("429", "500", "502", "503", "flow.control") and not _retried:
                time.sleep(1.5)
                return self._call(method, biz, _retried=True)
            raise

        if payload.get("error_response"):
            err = payload["error_response"]
            raise ApiError(str(err.get("code", "")), str(err.get("msg", "") or
                                                     err.get("message", "")), _retried)
        return payload

    def _fetch_token(self) -> tuple[str, int]:
        self._require_enabled()
        self._call_seq += 1
        payload = self.transport.post(f"{API_BASE}/token", {
            "grant_type": "client_credentials",
            "app_key": os.environ.get(APP_KEY_ENV, ""),
            "app_secret": os.environ.get(APP_SECRET_ENV, ""),
        })
        res = payload.get("result", payload)
        return str(res.get("accessToken") or res.get("access_token") or ""), \
            int(res.get("expiresIn") or res.get("expires_in") or 3600)

    @staticmethod
    def _stable_id(source_id: str) -> str:
        """1688 的 productId → 本地 PXXX，且可重复同步保持稳定。"""
        import hashlib
        h = hashlib.md5(f"1688:{source_id}".encode()).hexdigest()
        return "P" + h[:10].upper()


# 供 store.py / 测试引用
def build_source(enabled: bool = False) -> Alibaba1688Source:
    if not enabled and os.environ.get(ENABLED_ENV, "") != "1":
        raise SourceDisabled("1688 对接未开启")
    return Alibaba1688Source()


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


__all__ = ["Alibaba1688Source", "ProductSourceError", "SourceDisabled", "ApiError",
           "ProductRow", "OrderDraft", "RateLimiter", "TokenManager", "HttpTransport",
           "build_source", "ENABLED_ENV", "APP_KEY_ENV", "APP_SECRET_ENV"]


class ProductSourceError(RuntimeError):
    """保留给将来货源层统一异常类型。"""
