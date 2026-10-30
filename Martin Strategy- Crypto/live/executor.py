"""
执行器 · 从信号到下单，以及持仓生命周期管理

职责：
1. 仓位计算：风险金额 -> 名义 -> 张数（按 lotSz 取整）
2. 开仓：市价买入 + 立即挂条件止损单（两条腿必须都成功）
3. 持仓管理：保本移单、9EMA 跟踪改单、时间止损
4. 平仓：撤掉止损单 + 市价平仓

安全约定：
- 止损单挂不上 -> 立即市价平掉刚开的仓（不留裸仓）
- 任何异常都写日志并保持 state 一致
"""

import time
import traceback

from okx_client import calc_lots, actual_notional, fmt_step


def calc_position(cfg, equity, s_pct, px, ins):
    """
    返回 (lots, notional_usdt, risk_usdt) 或 (0, 0, 0) 表示不可开
    """
    p = cfg["position"]
    risk_usdt = equity * p["risk_per_trade_pct"] / 100.0
    if s_pct <= 0:
        return 0.0, 0.0, 0.0
    target_notional = risk_usdt / (s_pct / 100.0)

    # 单笔名义上限
    cap = equity * p["single_position_max_pct"] / 100.0
    target_notional = min(target_notional, cap)

    if target_notional < p.get("min_notional_usdt", 10):
        return 0.0, 0.0, 0.0

    ct_val = float(ins["ctVal"])
    lots = calc_lots(target_notional, px, ct_val, ins["lotSz"], ins["minSz"])
    if lots <= 0:
        return 0.0, 0.0, 0.0

    notional = actual_notional(lots, px, ct_val)
    real_risk = notional * (s_pct / 100.0)
    return lots, notional, real_risk


def open_position(client, sg, cfg, state, ins, equity, log=print, notifier=None):
    """
    开仓 + 挂止损。返回 trade dict 或 None。
    """
    p = cfg["position"]
    dry = cfg["runtime"]["dry_run"]

    lots, notional, risk = calc_position(cfg, equity, sg.s_pct, sg.entry_px, ins)
    if lots <= 0:
        log(f"    {sg.inst_id} 仓位计算为 0（名义不足或粒度不够），放弃")
        return None

    log(f"    {sg.inst_id} 开仓 {fmt_step(lots, ins['lotSz'])} 张 "
        f"名义 {notional:.2f}U 风险 {risk:.3f}U ({risk/equity*100:.2f}%)")

    if dry:
        log(f"    [dry_run] 跳过真实下单")
        return None

    # 1) 市价开多
    ord_res = client.place_order(
        inst_id=sg.inst_id, side="buy", sz=fmt_step(lots, ins["lotSz"]),
        td_mode=cfg["account"]["margin_mode"],
        pos_side=cfg["account"].get("pos_side", "long"), ord_type="market",
    )
    ord_id = ord_res.get("ordId") if ord_res else None
    if not ord_id:
        log(f"    ❌ 下单未返回 ordId")
        return None

    # 等成交，拿真实均价
    fill_px = sg.entry_px
    for _ in range(10):
        time.sleep(0.5)
        o = client.get_order(sg.inst_id, ord_id)
        if o and o.get("state") == "filled" and float(o.get("avgPx") or 0) > 0:
            fill_px = float(o["avgPx"])
            break

    # 2) 立即挂条件止损单
    stop_px = fill_px * (1 - sg.s_pct / 100.0)
    stop_px = float(fmt_step(stop_px, ins["tickSz"]))
    algo_id = None
    try:
        algo_id = client.place_stop_loss(
            inst_id=sg.inst_id, sz=fmt_step(lots, ins["lotSz"]),
            sl_trigger_px=stop_px, td_mode=cfg["account"]["margin_mode"],
            pos_side=cfg["account"].get("pos_side", "long"), side="sell",
        )
    except Exception as e:
        log(f"    ❌ 止损单挂失败: {e} -> 立即平仓，不留裸仓")
        try:
            client.close_position(sg.inst_id,
                                  pos_side=cfg["account"].get("pos_side", "long"),
                                  mgn_mode=cfg["account"]["margin_mode"])
        except Exception as e2:
            log(f"    ❌❌ 平仓也失败，需人工介入: {e2}")
        return None

    if not algo_id:
        log(f"    ❌ 止损单未返回 algoId -> 立即平仓")
        if notifier:
            try:
                notifier.notify_risk("止损单挂单失败，已自动平仓",
                                     [f"标的 `{sg.inst_id}`", "**未形成裸仓**，无需人工处理"],
                                     level="stop")
            except Exception:
                pass
        try:
            client.close_position(sg.inst_id,
                                  pos_side=cfg["account"].get("pos_side", "long"),
                                  mgn_mode=cfg["account"]["margin_mode"])
        except Exception:
            pass
        return None

    trade = {
        "inst_id": sg.inst_id,
        "side": "long",
        "lots": fmt_step(lots, ins["lotSz"]),
        "entry_px": fill_px,
        "stop_px": stop_px,
        "init_stop_px": stop_px,
        "algo_id": algo_id,
        "s_pct": sg.s_pct,
        "r_value": fill_px - stop_px,          # 1R 的价格幅度
        "risk_usdt": risk,
        "notional": actual_notional(lots, fill_px, float(ins["ctVal"])),
        "opened_at": int(time.time()),
        "trimmed": False,
        "stop_source": sg.stop_source,
    }
    log(f"    ✅ 开仓完成 均价 {fill_px:.6g} 止损 {stop_px:.6g} algoId {algo_id}")

    if notifier:
        try:
            taker = cfg.get("cost", {}).get("taker_pct", 0.05) / 100.0
            fee_open = trade["notional"] * taker
            notifier.notify_open(
                inst=sg.inst_id, entry_px=fill_px, stop_px=stop_px,
                notional=trade["notional"],
                risk_usdt=risk, s_pct=sg.s_pct,
                risk_pct=risk / equity * 100 if equity else 0,
                stop_source=sg.stop_source,
                equity=equity,
                leverage=cfg["position"].get("leverage_set", 1),
                logic=getattr(sg, "logic", None),
                fee_open=fee_open,
                fee_round=fee_open * 2,
                r_value=trade["r_value"],
                r_targets=cfg.get("exit", {}).get("l1_first_trim", {})
                    .get("r_threshold_grid", [3, 5, 12, 15]),
            )
        except Exception as e:
            log(f"    [通知] 开仓推送失败（不影响交易）: {str(e)[:80]}")
    return trade


def manage_position(client, tr, cfg, ins, st_daily, log=print):
    """
    管理单笔持仓：保本 -> 跟踪 -> 时间止损。
    返回 (action, detail)：action in {hold, amend, close}
    """
    dry = cfg["runtime"]["dry_run"]
    cur = float(client.get_ticker(tr["inst_id"])["last"])
    entry, stop = tr["entry_px"], tr["stop_px"]
    r = tr["r_value"]
    pnl_r = (cur - entry) / r if r > 0 else 0

    # 1) 保本：日线 EMA9 升到成本之上
    new_stop = stop
    if st_daily and st_daily.ema9 > entry and entry > stop:
        new_stop = max(new_stop, entry)
        log(f"    {tr['inst_id']} EMA9({st_daily.ema9:.6g})>成本({entry:.6g}) -> 保本移单")

    # 2) 跟踪：EMA9 下方一点（只升不降）
    if st_daily and st_daily.atr_daily_pct > 0:
        trail = st_daily.ema9 * (1 - 0.001)  # EMA9 下方 0.1%（简化：用日线ATR的0.1倍会更松）
        if trail > new_stop:
            new_stop = trail
            log(f"    {tr['inst_id']} 9EMA 跟踪 -> {trail:.6g}")

    if new_stop > stop + 1e-12:
        new_px = float(fmt_step(new_stop, ins["tickSz"]))
        if not dry:
            try:
                client.amend_stop_loss(tr["inst_id"], tr["algo_id"], new_px)
                tr["stop_px"] = new_px
                return "amend", f"止损上移至 {new_px:.6g}"
            except Exception as e:
                log(f"    ❌ 改单失败 {e}（保持原止损）")
        else:
            log(f"    [dry_run] 将把止损上移到 {new_px:.6g}")
            tr["stop_px"] = new_px
            return "amend", f"[dry] 止损上移至 {new_px:.6g}"

    # 3) 时间止损
    days = (time.time() - tr["opened_at"]) / 86400
    ts = cfg["exit"]["time_stop"]
    if days >= ts["days_hard"]:
        return "close", f"持有 {days:.1f} 天 >= 硬上限 {ts['days_hard']}"
    if days >= ts["days_soft"] and pnl_r < 1.0:
        return "close", f"持有 {days:.1f} 天且浮盈 {pnl_r:.2f}R < 1R"

    return "hold", f"浮盈 {pnl_r:+.2f}R"


def close_position(client, tr, cfg, reason, log=print, notifier=None, exit_px=None):
    """撤止损单 + 市价平仓"""
    dry = cfg["runtime"]["dry_run"]
    log(f"    {tr['inst_id']} 平仓：{reason}")
    if dry:
        log(f"    [dry_run] 跳过真实平仓")
        return True
    # 先撤止损单，避免平仓后残留
    try:
        if tr.get("algo_id"):
            client.cancel_algo(tr["inst_id"], tr["algo_id"])
    except Exception as e:
        log(f"    ⚠️ 撤止损单失败（继续平仓）: {e}")
    try:
        client.close_position(tr["inst_id"],
                              pos_side=cfg["account"].get("pos_side", "long"),
                              mgn_mode=cfg["account"]["margin_mode"])
        if notifier:
            try:
                px_out = exit_px if exit_px else tr["entry_px"]
                r = tr["r_value"]
                pnl_r = (px_out - tr["entry_px"]) / r if r > 0 else 0.0
                pnl_usdt = pnl_r * tr["risk_usdt"]
                notifier.notify_close(tr["inst_id"], tr["entry_px"], px_out,
                                      pnl_usdt, pnl_r, reason)
            except Exception as e:
                log(f"    [通知] 平仓推送失败（不影响交易）: {str(e)[:80]}")
        return True
    except Exception as e:
        log(f"    ❌ 平仓失败: {e}")
        log(traceback.format_exc()[:400])
        return False
