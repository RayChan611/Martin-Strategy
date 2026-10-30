"""
标的池构建 · 每日重建

筛选条件（对应 config.universe）：
  1. settleCcy = USDT（USDT 本位永续）
  2. state = live（在市交易）
  3. instCategory = 1（纯加密；3=股票 4=商品，必须排除）
  4. 24h 成交额 ≥ filters.adtv_min_usd（默认 1 亿美元）

为什么要每日重建：成交额天天变，手工维护的列表几周后就过时了
（某些币跌出 1 亿、新币冲进来，池子不会自动跟上）。
"""

from datetime import datetime, timezone

from okx_client import OKXClient


def build_pool(client: OKXClient, cfg: dict, log=print):
    """
    返回按成交额降序的 instId 列表。
    失败时返回空列表（调用方应回退到 config 里的手工列表）。
    """
    u = cfg.get("universe", {})
    min_vol = u.get("filters", {}).get("adtv_min_usd", 100_000_000)
    want_cat = str(u.get("inst_category", 1))
    max_size = u.get("max_size", 50)

    try:
        instruments = client.get_instruments("SWAP")
        tickers = {t["instId"]: t for t in client.get_tickers("SWAP")}
    except Exception as e:
        log(f"  [pool] 拉取合约列表失败: {str(e)[:80]}")
        return []

    rows = []
    skipped_cat = 0
    for x in instruments:
        if x.get("settleCcy") != "USDT":
            continue
        if x.get("state") != "live":
            continue
        if str(x.get("instCategory")) != want_cat:
            skipped_cat += 1
            continue
        t = tickers.get(x["instId"])
        if not t:
            continue
        try:
            vol = float(t.get("volCcy24h") or 0) * float(t.get("last") or 0)
        except (TypeError, ValueError):
            continue
        if vol < min_vol:
            continue
        rows.append((x["instId"], vol))

    rows.sort(key=lambda r: -r[1])
    pool = [s for s, _ in rows[:max_size]]
    log(f"  [pool] 建成 {len(pool)} 个标的"
        f"（排除非加密 {skipped_cat} 个，成交额门槛 {min_vol/1e8:.0f} 亿）")
    if pool:
        log(f"  [pool] 前三: {', '.join(pool[:3])}")
    return pool


def pool_diff(old, new):
    """返回 (新增列表, 移除列表)"""
    o, n = set(old or []), set(new or [])
    return sorted(n - o), sorted(o - n)


def should_rebuild(now, state, rebuild_hour_utc=8):
    """判断今天是否还没重建过，且已过重建时点"""
    today = now.strftime("%Y-%m-%d")
    if not state.get("pool"):
        return True                      # 首次启动
    if state.get("pool_date") == today:
        return False                     # 今天已建
    return now.hour >= rebuild_hour_utc   # 到点才建
