"""
信号引擎 · 回调买法（Pullback）

设计原则：
1. 纯标准库，不依赖 numpy/pandas（服务器可能没装）
2. 严格区分「日线状态」与「5m 触发」——日线每天算一次，5m 每次轮询算
3. 每个条件都带原因说明，方便日志追溯与事后归因

⚠️ 首版简化（必须知道）：
- 盘中触发用轮询近似（间隔 60s），不是真正的 tick 级「过去就买」，最多延迟一个轮询周期
- 支撑体系暂用 EMA9/21/50 + 前高 + 前低，**未实现 AVWAP**（需额外锚点逻辑）
- 未实现 E2「回调低点抬高」（需要跨回调记录上次低点，留给下一版）
- 未实现板块共振（加密板块轮动极快，且 OKX 无内置分类）
"""

import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ============ 指标（纯 Python）============
def ema_series(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def atr_series(bars, period):
    """bars: [[o,h,l,c], ...] 正序
    返回与 bars **等长**的 ATR 序列（首根用 H-L 近似 TR），
    便于与收盘价、EMA 按下标对齐。
    """
    if not bars:
        return []
    trs = [bars[0][1] - bars[0][2]]
    for i in range(1, len(bars)):
        h, l, pc = bars[i][1], bars[i][2], bars[i - 1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    out = []
    for i in range(len(trs)):
        s = max(0, i - period + 1)
        seg = trs[s:i + 1]
        out.append(sum(seg) / len(seg))
    return out


def adr_pct(bars, period=20):
    """ADR% = mean((H-L)/C) * 100"""
    seg = bars[-period:] if len(bars) >= period else bars
    if not seg:
        return 0.0
    return sum((b[1] - b[2]) / b[3] * 100 for b in seg) / len(seg)


def to_bars(raw):
    """OKX K线（倒序、含字符串）-> 正序 [[o,h,l,c,v], ...] 浮点"""
    rows = []
    for k in raw:
        rows.append([float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])])
    rows.reverse()  # OKX 返回最新在前
    return rows


# ============ 数据结构 ============
@dataclass
class DailyState:
    """日线级状态：决定这个标的今天是否在候选池"""
    inst_id: str
    ema9: float
    ema21: float
    ema50: float
    atr_daily_pct: float
    adr_pct: float
    close: float
    trend_ok: bool
    early_ok: bool
    days_since_breakout: int
    breakout_ok: bool
    reasons: list
    ema21_dev_pct: float = 0.0      # 偏离 EMA21 的百分比
    weekly_streak: int = 0          # 连涨周数

    @property
    def passed(self):
        return self.trend_ok and self.early_ok and self.breakout_ok


@dataclass
class Signal:
    inst_id: str
    entry_px: float          # 参考入场价（最新成交价）
    stop_px: float           # 止损价
    s_pct: float             # 止损宽度 %
    atr_daily_pct: float
    atr_5m_pct: float
    stop_source: str         # day_low / swing_low / trigger_bar_low / prev_bar_low
    reasons: list            # 详细（日志用）
    logic: list = field(default_factory=list)   # 简洁（飞书卡片用）


# 日线状态缓存：{inst_id: (utc_date_str, DailyState)}
# 日线指标用「已收盘」K 线计算，一天内稳定，故可缓存一整天。
_DAILY_CACHE = {}
# 日线原始 K 线缓存：{inst_id: (utc_date_str, bars)}
# 多个买法共用同一份 bars，避免重复拉取
_BARS_CACHE = {}


def get_daily_bars(client, inst_id, use_cache=True):
    """已收盘日线（剔除当天未收盘那根），按 UTC 日期缓存"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if use_cache:
        hit = _BARS_CACHE.get(inst_id)
        if hit and hit[0] == today:
            return hit[1]
    raw = client.get_candles(inst_id, "1D", 200)
    bars_all = to_bars(raw)
    bars = bars_all[:-1] if len(bars_all) >= 2 else bars_all
    if use_cache:
        _BARS_CACHE[inst_id] = (today, bars)
    return bars


class Config:
    """信号阈值（从 config.yaml 读，这里给默认值便于单测）"""
    def __init__(self, cfg: dict):
        s = cfg.get("stoploss", {})
        self.s_max_atr_mult = s.get("s_max", {}).get("atr_daily_mult", 0.5)
        self.s_max_adr_ratio = s.get("s_max", {}).get("adr_ratio", 0.5)
        self.s_max_cap = s.get("s_max", {}).get("hard_cap_pct", 5.0)
        self.s_min_floor = s.get("s_min", {}).get("floor_pct", 0.5)
        self.s_min_atr5m_mult = s.get("s_min", {}).get("atr_5m_mult", 3.0)
        sig = cfg.get("signal", {}).get("pullback", {})
        self.e1_days_max = sig.get("e1_days_max", 30)
        self.e1_relaxed_dev_pct = sig.get("e1_relaxed_dev_pct", 5.0)
        self.e3_ema21_dev_max = sig.get("e3_ema21_dev_max", 15.0)
        self.e3_weekly_up_max = sig.get("e3_weekly_up_max", 4)
        self.support_near_pct = sig.get("support_distance_max_pct", 1.0)


# ============ 日线状态 ============
def build_daily_state(client, inst_id, ccfg: Config, use_cache: bool = True) -> Optional[DailyState]:
    """
    日线状态。指标只用「已收盘」的日线（剔除最后一根未收盘），
    因此一天内数值稳定，可按 UTC 日期缓存，避免每分钟重复拉取。
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if use_cache:
        hit = _DAILY_CACHE.get(inst_id)
        if hit and hit[0] == today:
            return hit[1]

    bars = get_daily_bars(client, inst_id, use_cache)
    if len(bars) < 60:
        return None

    closes = [b[3] for b in bars]
    e9 = ema_series(closes, 9)
    e21 = ema_series(closes, 21)
    e50 = ema_series(closes, 50)
    atr_d = atr_series(bars, 14)
    if not atr_d:
        return None

    i = len(bars) - 1
    close = closes[i]
    atr_pct = atr_d[-1] / close * 100
    adr = adr_pct(bars, 20)
    reasons = []

    # A · 趋势：多头排列 + 三线斜率向上（用近 3 日比较）
    order_ok = e9[i] > e21[i] > e50[i]
    slope_ok = (i >= 3 and e9[i] > e9[i - 3] and e21[i] > e21[i - 3] and e50[i] > e50[i - 3])
    trend_ok = order_ok and slope_ok
    reasons.append(f"A趋势={'✓' if trend_ok else '✗'}(排列{order_ok}/斜率{slope_ok})")

    # E3 · 位置早：偏离 EMA21 < 15% + 连涨周数 < 4
    dev = (close - e21[i]) / e21[i] * 100
    dev_ok = dev < ccfg.e3_ema21_dev_max
    # 连涨周数：按 7 天分组取周末收盘
    weekly = closes[::7]
    streak = 0
    for j in range(len(weekly) - 1, 0, -1):
        if weekly[j] > weekly[j - 1]:
            streak += 1
        else:
            break
    streak_ok = streak < ccfg.e3_weekly_up_max
    early_ok = dev_ok and streak_ok
    reasons.append(f"E3位置={'✓' if early_ok else '✗'}(偏离{dev:.1f}%/连涨{streak}周)")

    # E1 · 距最近突破日 <= N 天
    bos_idx = None
    for j in range(len(bars) - 1, 20, -1):
        prior_high = max(b[1] for b in bars[j - 20:j])
        prev_prior_high = max(b[1] for b in bars[j - 21:j - 1]) if j >= 21 else prior_high
        if closes[j] > prior_high and closes[j - 1] <= prev_prior_high:
            bos_idx = j
            break
    days_since = (len(bars) - 1 - bos_idx) if bos_idx is not None else 9999
    # E1 双通道：距突破 ≤N 天  或  位置仍很靠近 EMA21（说明没追高，突破多久不重要）
    e1_strict = days_since <= ccfg.e1_days_max
    e1_relaxed = abs(dev) < ccfg.e1_relaxed_dev_pct
    breakout_ok = e1_strict or e1_relaxed
    if e1_strict:
        e1_tag = f"{days_since}天✓"
    elif e1_relaxed:
        e1_tag = f"{days_since}天→放宽(偏离{dev:+.1f}%)✓"
    else:
        e1_tag = f"{days_since}天✗(偏离{dev:+.1f}%)"
    reasons.append(f"E1距突破={e1_tag}")

    _st = DailyState(
        inst_id=inst_id, ema9=e9[i], ema21=e21[i], ema50=e50[i],
        atr_daily_pct=atr_pct, adr_pct=adr, close=close,
        trend_ok=trend_ok, early_ok=early_ok,
        days_since_breakout=days_since, breakout_ok=breakout_ok,
        reasons=reasons,
        ema21_dev_pct=dev, weekly_streak=streak,
    )
    if use_cache:
        _DAILY_CACHE[inst_id] = (today, _st)
        return _st
    return _st


# ============ 5m 触发 ============
def check_trigger(client, st: DailyState, ccfg: Config) -> Optional[Signal]:
    """
    5m 级触发检查。返回 Signal 或 None。

    条件：
      C  · 回调到位：最近若干根 5m 的最低价 <= 日线 EMA9
      V2 · V 型速度：从回调低点算，<=3 根 5m 内收复 EMA9
      触发 · 最新价 > 前一根 5m 的最高价（盘中过就买，轮询近似）
      止损 · 当日最低点 -> 触发K线最低 -> 前一根最低，取第一个落在 [s_min, s_max] 的
    """
    raw5 = client.get_candles(st.inst_id, "5m", 100)
    bars5 = to_bars(raw5)
    if len(bars5) < 30:
        return None

    n = len(bars5)
    last = bars5[-1]
    prev = bars5[-2]

    # ---- 实时位置校验（E3 必须用现价，不能用日线收盘）----
    # 日线 E3 用的是「已收盘」价格，判的是昨天位置早不早；
    # 但一天内可能已暴涨，此时位置早就晚了。这里用现价再卡一次。
    cur_px = float(client.get_ticker(st.inst_id)["last"])
    dev_now = (cur_px - st.ema21) / st.ema21 * 100 if st.ema21 else 0
    dev_ema9_now = (cur_px - st.ema9) / st.ema9 * 100 if st.ema9 else 0
    if dev_now >= ccfg.e3_ema21_dev_max:
        return None
    # 价格已远离 EMA9 时也不追（防止暴涨后追高）
    if dev_ema9_now >= ccfg.e3_ema21_dev_max:
        return None

    # 当日区间（用最近 288 根以内的同日数据近似：取最近 24 小时）
    day_window = bars5[-min(n, 288):]
    day_low = min(b[2] for b in day_window)

    # C · 回调到位：最近 12 根（1 小时）内最低价触及日线 EMA9
    recent = bars5[-12:]
    touched = any(b[2] <= st.ema9 for b in recent)
    if not touched:
        return None

    # 找回调低点及其位置
    low_val = min(b[2] for b in recent)
    low_idx = max(i for i, b in enumerate(recent) if b[2] == low_val)
    bars_since_low = len(recent) - 1 - low_idx

    # V2 · <= 3 根内收复 EMA9（用收盘价）
    if bars_since_low > 3:
        return None
    if not any(b[3] >= st.ema9 for b in recent[low_idx:]):
        return None

    # 触发：最新价 > 前一根高点
    if cur_px <= prev[1]:
        return None

    # 止损候选
    # 注：原规格只有 day_low / trigger_bar_low / prev_bar_low 三级。
    # 实盘实测发现这三级的宽度是「断层」的：day_low 常在 1.9%~4.3%（过宽），
    # 而 trigger_bar_low / prev_bar_low 常在 0.02%~0.10%（过窄），
    # 中间 0.5%~1.5% 这个最合理的区间反而没有候选 -> 大量信号被误杀。
    # 因此补一级 swing_low（回调波段低点），它正是「回调的最低点」，
    # 语义上也更贴近「止损放在支撑结构下方」。
    swing_low = min(b[2] for b in recent)
    cands = [
        ("day_low", day_low),
        ("swing_low", swing_low),
        ("trigger_bar_low", last[2]),
        ("prev_bar_low", prev[2]),
    ]
    # 去重（swing_low 有时等于 day_low）
    seen = set()
    cands = [c for c in cands if not (round(c[1], 10) in seen or seen.add(round(c[1], 10)))]
    atr5 = atr_series(bars5, 14)
    atr5_pct = (atr5[-1] / cur_px * 100) if atr5 else 0.0

    s_max = min(st.atr_daily_pct * ccfg.s_max_atr_mult,
                st.adr_pct * ccfg.s_max_adr_ratio,
                ccfg.s_max_cap)
    s_min = max(ccfg.s_min_floor, atr5_pct * ccfg.s_min_atr5m_mult)

    chosen = None
    for name, px in cands:
        if px <= 0 or px >= cur_px:
            continue
        s = (cur_px - px) / cur_px * 100
        if s_min <= s <= s_max:
            chosen = (name, px, s)
            break
    if chosen is None:
        return None

    name, stop_px, s_pct = chosen
    logic = [
        "日线趋势：EMA9 > EMA21 > EMA50，三线向上",
        f"位置：偏离 EMA21 {st.ema21_dev_pct:+.1f}%，连涨 {st.weekly_streak} 周",
        f"距突破日：{st.days_since_breakout} 天",
        f"回调：低点 {low_val:.6g} 触及日线 EMA9 {st.ema9:.6g}",
        f"V 型：{bars_since_low} 根 5m 内收复 EMA9",
        f"触发：现价 {cur_px:.6g} > 前一根高点 {prev[1]:.6g}",
        f"实时位置：偏离 EMA21 {dev_now:+.1f}%，偏离 EMA9 {dev_ema9_now:+.1f}%",
    ]
    reasons = st.reasons + [
        f"C回调到位✓(低点{low_val:.6g}<=EMA9 {st.ema9:.6g})",
        f"V2速度✓({bars_since_low}根内收复)",
        f"触发✓(现价{cur_px:.6g}>前高{prev[1]:.6g})",
        f"止损={name} s={s_pct:.2f}% [允许{s_min:.2f}~{s_max:.2f}]",
    ]
    return Signal(
        inst_id=st.inst_id, entry_px=cur_px, stop_px=stop_px, s_pct=s_pct,
        atr_daily_pct=st.atr_daily_pct, atr_5m_pct=atr5_pct,
        stop_source=name, reasons=reasons, logic=logic,
    )


def _avg_tr_pct(bars):
    """区间内平均真实波幅占收盘的百分比"""
    if len(bars) < 2:
        return 0.0
    tot = 0.0
    for i in range(1, len(bars)):
        h, l, pc = bars[i][1], bars[i][2], bars[i - 1][3]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tot += tr / bars[i][3] * 100
    return tot / (len(bars) - 1)


# ============ 突破买法 ============
def check_breakout_trigger(client, st: DailyState, ccfg: "Config", cfg: dict):
    """
    突破买法（v1.1 九步规则）：
      ① 趋势（复用 st.trend_ok，由调用方保证）
      ② 整理期 8-13 个交易日（|收盘/EMA9−1| ≤ 2%）
      ③ 整理深度 ≤ 8%
      ④ 波动收窄：后半 ATR% / 前半 ATR% < 0.85
      ⑤ AVWAP 位置：未实现，跳过
      ⑥ 整理前涨幅：记录不设门槛
      ⑦ 突破日前一日不能是 inside day
      ⑧ 触发：现价 > 昨日最高价（盘中即成交）
      ⑨ 止损：当日最低点，宽度上限 5%
    """
    bcfg = cfg.get("signal", {}).get("breakout", {})
    bars = get_daily_bars(client, st.inst_id)
    n = len(bars)
    if n < 30:
        return None

    closes = [b[3] for b in bars]
    e9 = ema_series(closes, 9)
    tol = bcfg.get("base_tol_pct", 2.0) / 100.0

    # ---- ② 整理期：从倒数第 2 根（昨日）往前数连续贴近 EMA9 的天数 ----
    K = 0
    j = n - 2
    while j >= 0 and e9[j] > 0 and abs(closes[j] / e9[j] - 1) <= tol:
        K += 1
        j -= 1
    dmin = bcfg.get("base_days_min", 8)
    dmax = bcfg.get("base_days_max", 13)
    if K < dmin or K > dmax:
        return None

    # ---- ③ 整理深度 ----
    seg = bars[n - K - 1:n - 1]
    if len(seg) < 3:
        return None
    hi = max(b[1] for b in seg)
    lo = min(b[2] for b in seg)
    if lo <= 0:
        return None
    depth = (hi - lo) / lo * 100
    if depth > bcfg.get("base_depth_max_pct", 8.0):
        return None

    # ---- ④ 波动收窄 ----
    half = max(2, K // 2)
    a1 = _avg_tr_pct(seg[:half])
    a2 = _avg_tr_pct(seg[-half:])
    if a1 <= 0:
        return None
    ratio = a2 / a1
    if ratio >= bcfg.get("atr_compress_ratio_max", 0.85):
        return None

    # ---- ⑦ 昨日不能是 inside day ----
    if bcfg.get("exclude_inside_day", True):
        prev2, prev1 = bars[n - 2], bars[n - 1]
        if prev1[1] <= prev2[1] and prev1[2] >= prev2[2]:
            return None

    # ---- ⑧ 触发：现价 > 昨日最高价 ----
    cur_px = float(client.get_ticker(st.inst_id)["last"])
    yday_high = bars[n - 1][1]
    if cur_px <= yday_high:
        return None

    # ---- ⑨ 止损：当日最低点，宽度上限 ----
    raw5 = client.get_candles(st.inst_id, "5m", 100)
    b5 = to_bars(raw5)
    if len(b5) < 20:
        return None
    day_low = min(b[2] for b in b5[-min(len(b5), 288):])
    if day_low <= 0 or day_low >= cur_px:
        return None
    s_pct = (cur_px - day_low) / cur_px * 100
    cap = bcfg.get("stop_cap_pct", 5.0)
    s_min = max(ccfg.s_min_floor, 0.5)
    if s_pct < s_min or s_pct > min(cap, ccfg.s_max_cap):
        return None

    # 整理前涨幅（记录）
    pre_gain = 0.0
    if n - K - 11 >= 0:
        base0 = closes[n - K - 1]
        if base0 > 0:
            pre_gain = (base0 - closes[n - K - 11]) / closes[n - K - 11] * 100

    logic = [
        "突破买法：整理末期 + 盘中突破昨日高点",
        f"整理期 {K} 天（要求 {dmin}-{dmax}），深度 {depth:.1f}%（≤{bcfg.get('base_depth_max_pct',8)}%）",
        f"波动收窄：后半 ATR {a2:.2f}% / 前半 {a1:.2f}% = {ratio:.2f}（<{bcfg.get('atr_compress_ratio_max',0.85)}）",
        f"整理前涨幅 {pre_gain:+.1f}%",
        f"触发：现价 {cur_px:.6g} > 昨日高 {yday_high:.6g}",
        f"止损：当日最低 {day_low:.6g}，宽度 {s_pct:.2f}%",
    ]
    reasons = [
        f"A趋势=✓", f"②整理{K}天✓", f"③深度{depth:.1f}%✓",
        f"④收窄{ratio:.2f}✓", f"⑧触发✓", f"⑨止损{s_pct:.2f}%",
    ]
    return Signal(
        inst_id=st.inst_id, entry_px=cur_px, stop_px=day_low, s_pct=s_pct,
        atr_daily_pct=st.atr_daily_pct,
        atr_5m_pct=(cur_px - day_low) / cur_px * 100,
        stop_source="breakout_day_low", reasons=reasons, logic=logic,
    )


# ============ 对外入口 ============
def scan(client, symbols, cfg, log=print, exclude=None):
    """
    扫描全部标的，返回 (状态列表, 信号列表)。

    exclude: 已持仓的标的集合，直接跳过（既防重复开仓，也省 API 请求）

    每标的按买法优先级依次尝试，最多产出一个信号：
      1) 回调买法（权重>0 且日线完整前置通过）
      2) 突破买法（权重>0 且趋势通过即可 —— 它本身就是突破日，不需要 E1/E3）
    """
    ccfg = Config(cfg)
    w = cfg.get("signal", {}).get("weights", {})
    w_pb = w.get("pullback", 0.8)
    w_bo = w.get("breakout", 0.15)
    states, signals = [], []
    skip = set(exclude or ())

    for inst in symbols:
        if inst in skip:
            continue          # 已持仓，不重复扫描
        try:
            st = build_daily_state(client, inst, ccfg)
            if st is None:
                log(f"  {inst:<20} 日线数据不足，跳过")
                continue
            states.append(st)

            # ---- 1) 回调买法 ----
            if w_pb > 0 and st.passed:
                sg = check_trigger(client, st, ccfg)
                if sg:
                    log(f"  {inst:<20} ★ 回调买法触发 {sg.stop_source} s={sg.s_pct:.2f}%")
                    signals.append(sg)
                    continue
                log(f"  {inst:<20} 日线通过，5m 未触发 → 试突破买法")

            # ---- 2) 突破买法（只需趋势）----
            if w_bo > 0 and st.trend_ok:
                sg2 = check_breakout_trigger(client, st, ccfg, cfg)
                if sg2:
                    log(f"  {inst:<20} ★ 突破买法触发 s={sg2.s_pct:.2f}%")
                    signals.append(sg2)
                    continue

            # ---- 都没触发：记录原因 ----
            if not st.trend_ok:
                log(f"  {inst:<20} 日线未过(A趋势✗) {' '.join(st.reasons)}")
            elif st.passed:
                log(f"  {inst:<20} 两种买法均未触发")
            else:
                log(f"  {inst:<20} 日线未过 {' '.join(st.reasons)}")
        except Exception as e:
            log(f"  {inst:<20} 异常 {type(e).__name__}: {str(e)[:80]}")

    return states, signals


def is_extended(client, inst_id, cfg):
    """
    判定「1 小时图上已经离均线很远」—— L1 首批减仓的必要条件之一。
    条件：价格 > EMA21 + mult×ATR14，且连续 N 根 1H 收盘在 EMA21 上方。
    """
    e = cfg["exit"]["first_trim"]["extension_check"]
    bar = _OKX_BAR.get(e.get("timeframe", "1h"), "1H")
    need = max(e["ema_period"], e["atr_period"]) + e["consecutive_closes"] + 5
    raw = client.get_candles(inst_id, bar, min(max(need, 60), 300))
    bars = to_bars(raw)
    if len(bars) < max(e["ema_period"], e["atr_period"]) + 2:
        return False, 0.0
    closes = [b[3] for b in bars]
    ema = ema_series(closes, e["ema_period"])
    atr = atr_series(bars, e["atr_period"])
    cur = closes[-1]
    if atr[-1] <= 0:
        return False, 0.0
    dev_atr = (cur - ema[-1]) / atr[-1]
    if dev_atr <= e["atr_mult"]:
        return False, dev_atr
    n = e["consecutive_closes"]
    above = all(closes[-i] > ema[-i] for i in range(1, n + 1))
    return above, dev_atr


def clear_daily_cache():
    """手动清缓存（跨日自动失效，一般不需要调）"""
    _DAILY_CACHE.clear()
    _BARS_CACHE.clear()
