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

from dataclasses import dataclass, field
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
        self.e1_days_max = 30
        self.e3_ema21_dev_max = 15.0
        self.support_near_pct = 1.0


# ============ 日线状态 ============
def build_daily_state(client, inst_id, ccfg: Config) -> Optional[DailyState]:
    raw = client.get_candles(inst_id, "1D", 200)
    bars = to_bars(raw)
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
    streak_ok = streak < 4
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
    breakout_ok = days_since <= ccfg.e1_days_max
    reasons.append(f"E1距突破={days_since}天{'✓' if breakout_ok else '✗'}")

    return DailyState(
        inst_id=inst_id, ema9=e9[i], ema21=e21[i], ema50=e50[i],
        atr_daily_pct=atr_pct, adr_pct=adr, close=close,
        trend_ok=trend_ok, early_ok=early_ok,
        days_since_breakout=days_since, breakout_ok=breakout_ok,
        reasons=reasons,
        ema21_dev_pct=dev, weekly_streak=streak,
    )


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
    cur_px = float(client.get_ticker(st.inst_id)["last"])
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


# ============ 对外入口 ============
def scan(client, symbols, cfg, log=print):
    """扫描全部标的，返回 (候选状态列表, 触发的信号列表)"""
    ccfg = Config(cfg)
    states, signals = [], []
    for inst in symbols:
        try:
            st = build_daily_state(client, inst, ccfg)
            if st is None:
                log(f"  {inst:<18} 日线数据不足，跳过")
                continue
            states.append(st)
            if not st.passed:
                log(f"  {inst:<18} 日线未过 {' '.join(st.reasons)}")
                continue
            log(f"  {inst:<18} 日线通过，检查 5m 触发")
            sg = check_trigger(client, st, ccfg)
            if sg:
                signals.append(sg)
                log(f"  {inst:<18} ★ 触发 {sg.stop_source} s={sg.s_pct:.2f}%")
        except Exception as e:
            log(f"  {inst:<18} 异常 {type(e).__name__}: {str(e)[:80]}")
    return states, signals
