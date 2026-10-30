"""
OKX v5 API 客户端（仅标准库，无第三方依赖）

设计要点：
1. 显式禁用系统代理 —— 东京服务器上有 HTTP_PROXY 环境变量，不清会连不上
2. 支持模拟盘（x-simulated-trading: 1），接口与实盘完全一致
3. 所有价格/数量用字符串传，避免浮点精度问题（OKX 要求）

用法：
    c = OKXClient.from_env()          # 从环境变量读密钥
    c = OKXClient.from_env(demo=True) # 模拟盘
    bal = c.get_balance()
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


class OKXError(Exception):
    """OKX 业务错误（code != 0）"""

    def __init__(self, code, msg, payload=None):
        self.code = code
        self.msg = msg
        self.payload = payload
        super().__init__(f"OKX [{code}] {msg}")


def _now_iso() -> str:
    """OKX 要求 ISO8601 带毫秒、UTC、Z 结尾，例：2020-12-08T09:08:57.715Z"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class OKXClient:
    BASE = "https://www.okx.com"

    def __init__(self, api_key="", secret="", passphrase="", demo=False, timeout=20):
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase
        self.demo = demo
        self.timeout = timeout
        # 显式禁用代理：环境变量里的 HTTP_PROXY 会让请求走不通
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ---------- 构造 ----------
    @classmethod
    def from_env(cls, demo=None):
        demo_env = os.getenv("OKX_DEMO", "1") == "1"
        return cls(
            api_key=os.getenv("OKX_API_KEY", ""),
            secret=os.getenv("OKX_SECRET_KEY", ""),
            passphrase=os.getenv("OKX_PASSPHRASE", ""),
            demo=demo_env if demo is None else demo,
        )

    # ---------- 底层请求 ----------
    def _sign(self, ts, method, path, body):
        msg = f"{ts}{method.upper()}{path}{body}"
        mac = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _request(self, method, path, params=None, body_obj=None, private=False):
        if params:
            path = path + "?" + urllib.parse.urlencode(params)
        body = json.dumps(body_obj, separators=(",", ":")) if body_obj else ""

        headers = {
            "Content-Type": "application/json",
            # OKX 会拦截 Python-urllib 默认 UA（403），必须伪装成常规客户端
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        }
        if private:
            if not (self.api_key and self.secret and self.passphrase):
                raise OKXError("NO_KEY", "缺少 API 凭证，请设置 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE")
            ts = _now_iso()
            headers["OK-ACCESS-KEY"] = self.api_key
            headers["OK-ACCESS-SIGN"] = self._sign(ts, method, path, body)
            headers["OK-ACCESS-TIMESTAMP"] = ts
            headers["OK-ACCESS-PASSPHRASE"] = self.passphrase
            if self.demo:
                headers["x-simulated-trading"] = "1"

        req = urllib.request.Request(
            self.BASE + path,
            data=body.encode() if body else None,
            headers=headers,
            method=method.upper(),
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                payload = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            raise OKXError(f"HTTP{e.code}", raw[:300]) from e

        if payload.get("code") != "0":
            # 批量下单时 code 在主结构，明细在 data
            detail = payload.get("data") or []
            if detail and isinstance(detail, list) and detail[0].get("sCode") not in (None, "0"):
                raise OKXError(detail[0].get("sCode"), detail[0].get("sMsg", ""), payload)
            raise OKXError(payload.get("code"), payload.get("msg", ""), payload)
        return payload.get("data", [])

    # ================= 公共接口 =================
    def get_instruments(self, inst_type="SWAP", inst_id=None):
        p = {"instType": inst_type}
        if inst_id:
            p["instId"] = inst_id
        return self._request("GET", "/api/v5/public/instruments", params=p)

    def get_candles(self, inst_id, bar="5m", limit=300, after=None):
        """
        返回 [[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm], ...]，**时间倒序**
        bar: 1m/3m/5m/15m/1H/4H/1D ...
        after: 取该时间戳之前的数据（用于翻页）
        """
        p = {"instId": inst_id, "bar": bar, "limit": str(min(limit, 300))}
        if after:
            p["after"] = str(after)
        return self._request("GET", "/api/v5/market/candles", params=p)

    def get_ticker(self, inst_id):
        d = self._request("GET", "/api/v5/market/ticker", params={"instId": inst_id})
        return d[0] if d else None

    def get_tickers(self, inst_type="SWAP"):
        return self._request("GET", "/api/v5/market/tickers", params={"instType": inst_type})

    def get_funding_rate(self, inst_id, limit=100):
        return self._request(
            "GET", "/api/v5/public/funding-rate-history",
            params={"instId": inst_id, "limit": str(limit)},
        )

    # ================= 账户 =================
    def get_balance(self, ccy=None):
        p = {}
        if ccy:
            p["ccy"] = ccy
        return self._request("GET", "/api/v5/account/balance", params=p, private=True)

    def get_usdt_equity(self):
        """账户 USDT 权益（含未实现盈亏）"""
        data = self.get_balance("USDT")
        if not data:
            return 0.0
        for d in data[0].get("details", []):
            if d["ccy"] == "USDT":
                return float(d.get("eq") or 0)
        return 0.0

    def get_positions(self, inst_id=None):
        p = {"instType": "SWAP"}
        if inst_id:
            p["instId"] = inst_id
        return self._request("GET", "/api/v5/account/positions", params=p, private=True)

    def set_leverage(self, inst_id, lever, mgn_mode="cross"):
        return self._request(
            "POST", "/api/v5/account/set-leverage",
            body_obj={"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode},
            private=True,
        )

    # ================= 交易 =================
    def place_order(self, inst_id, side, sz, px=None, td_mode="cross",
                    pos_side="net", ord_type="market", cl_ord_id=None, reduce_only=False):
        """
        side: buy / sell
        sz:   张数（字符串）
        px:   限价单价格（market 时传 None）
        ord_type: market / limit
        """
        body = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "posSide": pos_side,
            "ordType": ord_type,
            "sz": str(sz),
        }
        if ord_type == "limit":
            if px is None:
                raise ValueError("限价单必须传 px")
            body["px"] = str(px)
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id[:32]
        if reduce_only:
            body["reduceOnly"] = True
        d = self._request("POST", "/api/v5/trade/order", body_obj=body, private=True)
        return d[0] if d else None

    def place_stop_loss(self, inst_id, sz, sl_trigger_px, td_mode="cross",
                        pos_side="net", side="sell"):
        """
        挂条件止损单（algo order）。slOrdPx="-1" 表示触发后市价成交。
        返回 algoId
        """
        body = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "posSide": pos_side,
            "ordType": "conditional",
            "sz": str(sz),
            "slTriggerPx": str(sl_trigger_px),
            "slOrdPx": "-1",
        }
        d = self._request("POST", "/api/v5/trade/order-algo", body_obj=body, private=True)
        return d[0].get("algoId") if d else None

    def amend_stop_loss(self, inst_id, algo_id, new_trigger_px, sz=None):
        """修改止损触发价（移动止损用）"""
        body = {
            "instId": inst_id,
            "algoId": algo_id,
            "newSlTriggerPx": str(new_trigger_px),
        }
        if sz is not None:
            body["newSz"] = str(sz)
        d = self._request("POST", "/api/v5/trade/amend-algos", body_obj=body, private=True)
        return d[0] if d else None

    def cancel_algo(self, inst_id, algo_id):
        return self._request(
            "POST", "/api/v5/trade/cancel-algos",
            body_obj=[{"instId": inst_id, "algoId": algo_id}], private=True,
        )

    def get_pending_algos(self, ord_type="conditional"):
        return self._request(
            "GET", "/api/v5/trade/orders-algo-pending",
            params={"ordType": ord_type}, private=True,
        )

    def get_order(self, inst_id, ord_id):
        d = self._request(
            "GET", "/api/v5/trade/order",
            params={"instId": inst_id, "ordId": ord_id}, private=True,
        )
        return d[0] if d else None

    def close_position(self, inst_id, pos_side="net", mgn_mode="cross", sz=None):
        """市价平仓"""
        body = {"instId": inst_id, "mgnMode": mgn_mode}
        if pos_side and pos_side != "net":
            body["posSide"] = pos_side
        if sz:
            body["sz"] = str(sz)
        d = self._request("POST", "/api/v5/trade/close-position", body_obj=body, private=True)
        return d


# ---------------- 精度工具 ----------------
def round_to_step(value, step_str):
    """
    按交易所步长取整。OKX 的 tickSz / lotSz 是字符串，如 "0.01"、"0.0001"
    向下取整，避免超出可下单精度。
    """
    step = float(step_str)
    if step <= 0:
        return value
    n = int(value / step + 1e-9)
    return round(n * step, 12)


def fmt_step(value, step_str):
    """格式化成交易所要求的字符串（不带科学计数法）"""
    step = float(step_str)
    if step >= 1:
        return str(int(round_to_step(value, step_str)))
    decimals = len(step_str.split(".")[1]) if "." in step_str else 0
    return f"{round_to_step(value, step_str):.{decimals}f}"


def calc_lots(notional_usdt, price, ct_val, lot_sz, min_sz="0.01"):
    """
    名义金额 -> 可下单张数（按 lotSz 向下取整，不足 minSz 返回 0）

    这是仓位计算的关键一步：OKX 按「张」下单，1 张 = ctVal 个币。
    例：BTC ctVal=0.01，价 78000 -> 每张 780 USDT；
        目标名义 33.3 USDT -> 0.0427 张 -> 取整 0.04 张 -> 实际 31.2 USDT
    """
    per_lot = ct_val * price
    if per_lot <= 0:
        return 0.0
    lots = round_to_step(notional_usdt / per_lot, lot_sz)
    if lots < float(min_sz):
        return 0.0
    return lots


def actual_notional(lots, price, ct_val):
    """实际下单名义金额"""
    return lots * ct_val * price


if __name__ == "__main__":
    # 自检：公共接口不需要密钥
    c = OKXClient()
    t = c.get_ticker("BTC-USDT-SWAP")
    print("BTC 最新价:", t["last"], " 24h量:", t.get("volCcy24h"))
    ks = c.get_candles("BTC-USDT-SWAP", "5m", 3)
    print("最近 3 根 5m（倒序）:")
    for k in ks:
        print("  ", k[:5])
    ins = c.get_instruments("SWAP", "BTC-USDT-SWAP")[0]
    print("精度: tickSz=%s lotSz=%s minSz=%s ctVal=%s" % (
        ins["tickSz"], ins["lotSz"], ins["minSz"], ins["ctVal"]))
    # 张数计算自检：目标名义 33.3 USDT
    cv = float(ins["ctVal"]); ls = ins["lotSz"]; ms = ins["minSz"]
    px = float(t["last"])
    lots = calc_lots(33.3, px, cv, ls, ms)
    print("张数计算自检: 目标 33.3 USDT -> %s 张（每张 %.2f USDT）-> 实际 %.2f USDT" % (
        fmt_step(lots, ls), cv * px, actual_notional(lots, px, cv)))
