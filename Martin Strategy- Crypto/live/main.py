#!/usr/bin/env python3
"""
主循环 · OKX 100 USDT 实盘

运行：
    cd ~/martin-live && set -a && . ./.env && set +a && python3 main.py

三种运行模式（由 config.yaml 控制）：
    runtime.dry_run=true  只算信号、打印，不下单        <- 第一次跑用这个
    runtime.dry_run=false + account.demo=true  真实下单但打模拟盘
    runtime.dry_run=false + account.demo=false 真钱

退出：Ctrl+C 或 kill（会先保存 state 再退）
"""

import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

import yaml

from okx_client import OKXClient, OKXError
import strategy
import executor
import universe
from notifier import FeishuNotifier

STATE_FILE = "state.json"
LOG_FILE = None
_STOP = {"flag": False}


def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    ts = now_utc().strftime("%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if LOG_FILE:
        LOG_FILE.write(line + "\n")
        LOG_FILE.flush()


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_window(now, window):
    cur = now.hour * 60 + now.minute
    return _hm(window[0]) <= cur < _hm(window[1])


def session_allows_entry(now, cfg):
    """当前是否允许开新仓（时段过滤）"""
    s = cfg["session"]
    if in_window(now, s["avoid_utc"]):
        return False, "回避窗口"
    if in_window(now, s["primary_utc"]):
        return True, "主窗口"
    if s.get("allow_secondary") and in_window(now, s["secondary_utc"]):
        return True, "次窗口"
    # 放宽模式：除回避窗口外全部允许（原逻辑这里是 return False, "非交易时段"）
    if s.get("mode", "all_except_avoid") == "all_except_avoid":
        return True, "非主窗口"
    return False, "非交易时段"


def load_cfg(path="config.yaml"):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "positions": {},
        "day": "",
        "day_start_equity": 0.0,
        "week": "",
        "week_start_equity": 0.0,
        "week_risk_pct": 0.0,
        "consecutive_stops": 0,
        "pause_until": 0,
        "history": [],
    }


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def get_instrument(client, inst_id):
    ins = client.get_instruments("SWAP", inst_id)
    return ins[0] if ins else None


def reconcile(client, state, cfg, notifier, log=print):
    """
    启动时对账：以交易所为准，修正 state 并补齐止损单。

    处理的四种不一致：
      a) state 有 / 交易所无  -> 已平仓（止损或手动），从 state 移除并记入 history
      b) state 无 / 交易所有  -> state 丢失，用 avgPx 反推止损接管（否则无法管理）
      c) 两边都有但数量不符   -> 以交易所为准，同步 lots 并改小止损单
      d) 有持仓但无止损单     -> 裸仓，立即补挂

    返回 True 表示对账完成可以正常交易；False 表示存在需人工处理的问题，
    调用方应禁止开新仓。
    """
    dry = cfg["runtime"]["dry_run"]
    margin_mode = cfg["account"]["margin_mode"]
    pos_side = cfg["account"].get("pos_side", "long")

    try:
        live = {}
        for p in client.get_positions():
            try:
                qty = float(p.get("pos") or 0)
            except (TypeError, ValueError):
                continue
            if qty != 0:
                live[p["instId"]] = {
                    "pos": qty,
                    "avgPx": float(p.get("avgPx") or 0),
                    "upl": float(p.get("upl") or 0),
                }
        algos = {}
        for a in client.get_pending_algos("conditional"):
            algos.setdefault(a["instId"], []).append(a)
    except Exception as e:
        log(f"❌ 对账失败（无法读取交易所持仓）: {str(e)[:100]}")
        log("   → 本次启动将禁止开新仓，只管理已有持仓")
        return False

    problems = []
    st_pos = state["positions"]

    # ---- a) state 有、交易所无 ----
    for inst in list(st_pos.keys()):
        if inst not in live:
            tr = st_pos[inst]
            log(f"  [对账] {inst}: state 有记录但交易所已无持仓 → 已平仓，移出 state")
            state["history"].append({
                "inst_id": inst,
                "entry_px": tr.get("entry_px"),
                "stop_px": tr.get("stop_px"),
                "opened_at": tr.get("opened_at"),
                "closed_at": int(time.time()),
                "close_reason": "对账发现交易所已无持仓（止损或手动平仓）",
                "inferred": True,
            })
            del st_pos[inst]

    # ---- b) state 无、交易所有 ----
    for inst, info in live.items():
        if inst in st_pos:
            continue
        avg = info["avgPx"]
        if avg <= 0:
            log(f"  [对账] {inst}: 有持仓但均价为 0，无法接管，需人工处理")
            problems.append(f"{inst}: 有持仓但均价异常")
            continue
        stop_px = round(avg * 0.985, 8)
        log(f"  [对账] ⚠️ {inst}: 交易所有 {info['pos']} 张但 state 无记录 → 用均价反推接管")
        st_pos[inst] = {
            "inst_id": inst,
            "side": "long",
            "lots": str(info["pos"]),
            "entry_px": avg,
            "stop_px": stop_px,
            "init_stop_px": stop_px,
            "algo_id": None,
            "s_pct": 1.5,
            "r_value": avg - stop_px,
            "risk_usdt": 0.0,
            "notional": 0.0,
            "opened_at": int(time.time()),
            "trimmed": False,
            "stop_source": "reconcile_inferred",
            "inferred": True,
        }
        problems.append(f"{inst}: state 无记录，已按均价 {avg:.6g} 反推接管")

    # ---- c) 数量不符 ----
    for inst in set(st_pos.keys()) & set(live.keys()):
        state_lots = float(st_pos[inst].get("lots") or 0)
        live_lots = live[inst]["pos"]
        if abs(state_lots - live_lots) > 1e-9:
            log(f"  [对账] {inst}: 数量不符 state={state_lots} vs 交易所={live_lots} → 以交易所为准")
            st_pos[inst]["lots"] = str(live_lots)
            problems.append(f"{inst}: 数量已从 {state_lots} 校正为 {live_lots}")

    # ---- d) 止损单检查 ----
    for inst, info in live.items():
        tr = st_pos.get(inst)
        if not tr:
            continue
        mine = algos.get(inst, [])
        if len(mine) == 0:
            log(f"  [对账] ❌ {inst} 裸仓（无止损单）→ 立即补挂")
            if not dry:
                try:
                    ins = get_instrument(client, inst)
                    if ins:
                        tid = client.place_stop_loss(
                            inst, fmt_step(info["pos"], ins["lotSz"]),
                            tr["stop_px"], td_mode=margin_mode,
                            pos_side=pos_side, side="sell")
                        tr["algo_id"] = tid
                        log(f"      已补挂 algoId={tid} @ {tr['stop_px']}")
                    else:
                        problems.append(f"{inst}: 合约信息缺失，无法补挂止损")
                except Exception as e:
                    log(f"      补挂失败: {str(e)[:100]}")
                    problems.append(f"{inst}: 补挂止损失败")
            else:
                log(f"      [dry_run] 跳过补挂")
        elif len(mine) > 1:
            log(f"  [对账] ⚠️ {inst} 有 {len(mine)} 个止损单（重复）→ 保留 1 个，撤其余")
            for a in mine[1:]:
                try:
                    client.cancel_algo(inst, a["algoId"])
                    log(f"      已撤重复单 {a['algoId']}")
                except Exception:
                    pass
            if not tr.get("algo_id") or tr["algo_id"] != mine[0]["algoId"]:
                tr["algo_id"] = mine[0]["algoId"]
        else:
            # 单一止损单，核对数量是否匹配
            try:
                a_sz = float(mine[0].get("sz") or 0)
                if abs(a_sz - info["pos"]) > 1e-9:
                    log(f"  [对账] {inst} 止损单数量 {a_sz} ≠ 持仓 {info['pos']} → 修正")
                    client.amend_stop_loss(inst, mine[0]["algoId"], tr["stop_px"],
                                           sz=str(info["pos"]))
            except Exception as e:
                log(f"      止损单数量核对失败: {str(e)[:80]}")
            if tr.get("algo_id") != mine[0]["algoId"]:
                tr["algo_id"] = mine[0]["algoId"]

    if problems:
        log(f"  [对账] 发现 {len(problems)} 处需关注：")
        for x in problems:
            log(f"         · {x}")
        try:
            notifier.notify_risk(
                "启动对账发现不一致",
                ["**已自动修正可修的部分**"] + [f"· {x}" for x in problems[:6]] +
                (["（仅显示前 6 条）"] if len(problems) > 6 else []),
                level="warn")
        except Exception:
            pass
        return len([x for x in problems if "反推接管" in x or "无法" in x]) == 0

    log("  [对账] state 与交易所一致，止损单齐全")
    return True


def handle_exit(signum, frame):
    _STOP["flag"] = True
    log(f"收到退出信号 {signum}，保存状态后退出…")


def main():
    global LOG_FILE

    cfg = load_cfg()
    os.makedirs("logs", exist_ok=True)
    LOG_FILE = open(cfg["runtime"]["log_file"], "a", encoding="utf-8")

    dry = cfg["runtime"]["dry_run"]
    demo = cfg["account"]["demo"]
    log("=" * 68)
    log(f"Martin Strategy · Crypto · Live 启动")
    log(f"  模式: {'dry_run（不下单）' if dry else '真实下单'} | "
        f"{'模拟盘' if demo else '⚠️ 实盘真钱'}")
    log(f"  标的: {', '.join(cfg['universe']['symbols'])}")
    log(f"  风险/笔: {cfg['position']['risk_per_trade_pct']}% | "
        f"最大持仓: {cfg['position']['max_positions']}")
    log(f"  时段: 主 {cfg['session']['primary_utc']} | "
        f"回避 {cfg['session']['avoid_utc']} (UTC)")
    log("=" * 68)

    client = OKXClient.from_env(demo=demo)
    notifier = FeishuNotifier.from_env(enabled=cfg.get("notify", {}).get("feishu", True))
    log(f"飞书通知: {'已启用' if notifier.enabled else '未启用（缺 FEISHU_WEBHOOK）'}")
    state = load_state()

    def calc_equity():
        """仓位计算用权益 = min(API 实际权益, 配置上限)"""
        raw = client.get_usdt_equity()
        cap = cfg["account"].get("equity_cap_usdt") or 0
        return min(raw, cap) if cap > 0 else raw, raw

    # 启动前先确认真实权益
    try:
        equity, equity_raw = calc_equity()
        log(f"账户权益: {equity_raw:.4f} USDT（仓位按 {equity:.2f} 计算，上限 {cfg['account'].get('equity_cap_usdt')}）")
        if equity < 10:
            log("⚠️ 权益低于 10 USDT，请确认账户是否正确")
    except OKXError as e:
        log(f"❌ 无法读取账户权益: {e}")
        log("   请检查 .env 里的 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE")
        return 1

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    poll = cfg["runtime"]["poll_seconds"]

    # ---- 启动对账：以交易所为准修正 state，并补齐止损单 ----
    log("启动对账…")
    reconcile_ok = reconcile(client, state, cfg, notifier, log=log)
    save_state(state)
    if not reconcile_ok:
        log("⚠️ 对账存在需人工处理的问题，本次启动不开新仓")

    # ---- 标的池：优先用 state 里今天已建好的，否则重建 ----
    if not state.get("pool"):
        log("首次启动，构建标的池…")
        p0 = universe.build_pool(client, cfg, log=log)
        if p0:
            state["pool"] = p0
            state["pool_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        else:
            state["pool"] = list(cfg["universe"]["symbols"])
            log(f"建池失败，回退到 config 手工列表（{len(state['pool'])} 个）")
    log(f"当前标的池：{len(state['pool'])} 个")

    while not _STOP["flag"]:
        loop_start = time.time()
        try:
            now = now_utc()
            today = now.strftime("%Y-%m-%d")
            # 风控与风险档位（提前定义，避免各 continue 分支后未定义）
            cb = cfg["risk"]["circuit_breaker"]
            eff_risk_pct = cfg["position"]["risk_per_trade_pct"]

            # ---- 日切重置 ----
            if state["day"] != today:
                state["day"] = today
                state["day_start_equity"] = calc_equity()[0]
                log(f"—— 新交易日 {today}，起始权益 {state['day_start_equity']:.4f} ——")

            # ---- 每小时定期对账（防止运行中出现 state 与交易所不一致）----
            if time.time() - state.get("last_reconcile_ts", 0) > 3600:
                try:
                    ok_r = reconcile(client, state, cfg, notifier, log=log)
                    state["last_reconcile_ts"] = int(time.time())
                    if not ok_r:
                        log("⚠️ 定期对账发现问题，暂停开新仓")
                        reconcile_ok = False
                except Exception as e:
                    log(f"定期对账异常: {str(e)[:80]}")

            # ---- 标的池每日重建（默认 UTC 08:00）----
            if universe.should_rebuild(now, state,
                                       cfg["runtime"].get("daily_rebuild_hour_utc", 8)):
                new_pool = universe.build_pool(client, cfg, log=log)
                if new_pool:
                    added, removed = universe.pool_diff(state.get("pool"), new_pool)
                    state["pool"] = new_pool
                    state["pool_date"] = today
                    log(f"—— 标的池已重建：{len(new_pool)} 个"
                        f"（新增 {len(added)}，移除 {len(removed)}）——")
                    if added or removed:
                        try:
                            lines = [f"**当前池** {len(new_pool)} 个"]
                            if added:
                                lines.append(f"**新增** {', '.join(a.split('-')[0] for a in added[:8])}")
                            if removed:
                                lines.append(f"**移除** {', '.join(r.split('-')[0] for r in removed[:8])}")
                            notifier.notify_risk("标的池已更新", lines, level="info")
                        except Exception:
                            pass
                else:
                    log("标的池重建失败，沿用旧池")

            # ---- 周切重置（ISO 周）----
            week_key = now.strftime("%G-W%V")
            if state.get("week") != week_key:
                state["week"] = week_key
                state["week_start_equity"] = calc_equity()[0]
                state["week_risk_pct"] = 0.0     # 周初恢复正常风险
                log(f"—— 新交易周 {week_key}，起始权益 {state['week_start_equity']:.4f} ——")

            equity, _ = calc_equity()

            # ---- 熔断 ----
            if state.get("pause_until", 0) > time.time():
                left = int(state["pause_until"] - time.time())
                log(f"熔断中，剩余 {left//3600}h{left%3600//60}m，跳过本轮")
                _sleep_rest(loop_start, poll)
                continue
                _sleep_rest(loop_start, poll)
                continue

            day_pnl_pct = 0.0
            if state["day_start_equity"] > 0:
                day_pnl_pct = (equity - state["day_start_equity"]) / state["day_start_equity"] * 100
            # ---- 周亏降级（不停止交易，只降风险）----
            week_base = state.get("week_start_equity") or 0
            if week_base > 0:
                week_pnl_pct = (equity - week_base) / week_base * 100
                if week_pnl_pct <= -cb["weekly_loss_pct"]:
                    eff_risk_pct = state.get("week_risk_pct") or 0.25
                    state["week_risk_pct"] = eff_risk_pct
                    log(f"⚠️ 本周亏损 {week_pnl_pct:.2f}% 触及 {cb['weekly_loss_pct']}% "
                        f"→ 风险降至 {eff_risk_pct}%")

            if day_pnl_pct <= -cfg["risk"]["circuit_breaker"]["daily_loss_pct"]:
                log(f"⛔ 当日亏损 {day_pnl_pct:.2f}% 触及熔断线，今日停止开新仓")
                flag = f"cb_notified_{today}"
                if not state.get(flag):
                    state[flag] = True
                    try:
                        notifier.notify_risk(
                            "日亏熔断触发",
                            [f"**当日盈亏** {day_pnl_pct:.2f}%",
                             f"**当前权益** {equity:.2f} USDT",
                             "今日停止开新仓，已有持仓继续按规则管理"],
                            level="stop")
                    except Exception:
                        pass
                _sleep_rest(loop_start, poll)
                continue

            # ---- 管现有持仓 ----
            if state["positions"]:
                log(f"持仓 {len(state['positions'])} 笔，逐笔检查：")
                for inst in list(state["positions"].keys()):
                    tr = state["positions"][inst]
                    try:
                        ins = get_instrument(client, inst)
                        st_daily = strategy.build_daily_state(
                            client, inst, strategy.Config(cfg))
                        action, detail = executor.manage_position(
                            client, tr, cfg, ins, st_daily, log=log)
                        if action == "trim":
                            try:
                                cur = float(client.get_ticker(inst)["last"])
                            except Exception:
                                cur = tr["entry_px"]
                            rr = tr["r_value"]
                            pnl_r = (cur - tr["entry_px"]) / rr if rr > 0 else 0
                            log(f"    {inst} {detail}")
                            try:
                                eq = client.get_usdt_equity()
                            except Exception:
                                eq = None
                            hd = (time.time() - tr.get("opened_at", time.time())) / 86400
                            adv = tr.get("last_advisor") or {}
                            reason = adv.get("reason") or "浮盈达 R 门槛 + 1h 延伸过度"
                            try:
                                notifier.notify_trim(inst, detail, tr["entry_px"],
                                                     cur, pnl_r, reason,
                                                     equity=eq, hold_days=hd)
                            except Exception:
                                pass
                        elif action == "closed_externally":
                            log(f"    {inst} {detail} → 从 state 移除并记录")
                            state["history"].append({
                                "inst_id": inst,
                                "entry_px": tr.get("entry_px"),
                                "stop_px": tr.get("stop_px"),
                                "opened_at": tr.get("opened_at"),
                                "closed_at": int(time.time()),
                                "close_reason": f"外部平仓：{detail}",
                                "inferred": True,
                            })
                            del state["positions"][inst]
                            try:
                                notifier.notify_risk(
                                    "持仓已在交易所平掉",
                                    [f"标的 `{inst}`", f"原因：{detail}",
                                     "**已从 state 移除**（可能是止损触发）"],
                                    level="info")
                            except Exception:
                                pass
                        elif action == "close":
                            try:
                                exit_px = float(client.get_ticker(inst)["last"])
                            except Exception:
                                exit_px = None
                            ok = executor.close_position(
                                client, tr, cfg, detail, log=log,
                                notifier=notifier, exit_px=exit_px)
                            if ok and exit_px:
                                rr = tr["r_value"]
                                pnl_r = (exit_px - tr["entry_px"]) / rr if rr > 0 else 0
                                if pnl_r < 0:
                                    state["consecutive_stops"] = state.get("consecutive_stops", 0) + 1
                                    log(f"    连亏计数: {state['consecutive_stops']} 笔")
                                else:
                                    state["consecutive_stops"] = 0
                                    log("    连亏计数已清零")
                                # 连亏 N 笔 -> 暂停
                                if state["consecutive_stops"] >= cb["consecutive_stops_pause"]:
                                    pause_h = cb.get("pause_hours", 24)
                                    state["pause_until"] = time.time() + pause_h * 3600
                                    log(f"⛔ 连亏 {state['consecutive_stops']} 笔，暂停交易 {pause_h} 小时")
                                    try:
                                        notifier.notify_risk(
                                            f"连亏 {state['consecutive_stops']} 笔，暂停交易",
                                            [f"暂停 {pause_h} 小时",
                                             f"当前权益 {equity:.2f} USDT"],
                                            level="stop")
                                    except Exception:
                                        pass
                            if ok:
                                state["history"].append({
                                    **{k: v for k, v in tr.items() if k != "algo_id"},
                                    "closed_at": int(time.time()),
                                    "close_reason": detail,
                                })
                                del state["positions"][inst]
                        else:
                            log(f"    {inst} {detail}")
                    except Exception as e:
                        log(f"    ❌ {inst} 管理异常: {type(e).__name__}: {str(e)[:120]}")
                        log(traceback.format_exc()[:300])

            # ---- 扫新信号 ----
            allow, why = session_allows_entry(now, cfg)
            slots = cfg["position"]["max_positions"] - len(state["positions"])
            if not allow:
                log(f"不开新仓：{why}")
            elif slots <= 0:
                log(f"不开新仓：持仓已满（{cfg['position']['max_positions']} 笔）")
            else:
                log(f"扫描信号（{why}，剩余 {slots} 个名额）…")
                if not reconcile_ok:
                    log("不开新仓：启动对账存在未解决问题")
                    _sleep_rest(loop_start, poll)
                    continue
                scan_list = state.get("pool") or cfg["universe"]["symbols"]
                held = set(state["positions"].keys())
                if held:
                    log(f"  已持仓 {len(held)} 个标的将被跳过：{', '.join(x.split('-')[0] for x in held)}")
                states, signals = strategy.scan(client, scan_list, cfg, log=log, exclude=held)
                for sg in signals:
                    if slots <= 0:
                        log("  名额已满，忽略后续信号")
                        break
                    # 二次确认：state 里没有，但交易所可能有（防 state 丢失/不一致）
                    if sg.inst_id in state["positions"]:
                        log(f"  {sg.inst_id} 已在持仓中，跳过")
                        continue
                    try:
                        live_pos = [p for p in client.get_positions(sg.inst_id)
                                    if float(p.get("pos") or 0) != 0]
                        if live_pos:
                            log(f"  ⚠️ {sg.inst_id} 交易所有持仓但 state 未记录，跳过开仓")
                            try:
                                notifier.notify_risk(
                                    "发现 state 与交易所不一致",
                                    [f"标的 `{sg.inst_id}`",
                                     f"交易所持仓 {live_pos[0].get('pos')} 张",
                                     "**已跳过开仓**，请人工核对 state.json"],
                                    level="stop")
                            except Exception:
                                pass
                            continue
                    except Exception as e:
                        log(f"  {sg.inst_id} 持仓核对失败，跳过以防重复: {str(e)[:60]}")
                        continue

                    ins = get_instrument(client, sg.inst_id)
                    if not ins:
                        continue
                    tr = executor.open_position(
                        client, sg, cfg, state, ins, equity, log=log,
                        notifier=notifier, risk_pct=eff_risk_pct)
                    if tr:
                        state["positions"][sg.inst_id] = tr
                        slots -= 1

            save_state(state)

        except OKXError as e:
            log(f"❌ OKX 接口错误: {e}")
        except Exception as e:
            log(f"❌ 主循环异常: {type(e).__name__}: {str(e)[:150]}")
            log(traceback.format_exc()[:500])

        _sleep_rest(loop_start, poll)

    save_state(state)
    log("已保存状态，退出。")
    return 0


def _sleep_rest(loop_start, poll):
    elapsed = time.time() - loop_start
    remain = max(0.0, poll - elapsed)
    # 分片 sleep，方便及时响应退出信号
    while remain > 0 and not _STOP["flag"]:
        chunk = min(1.0, remain)
        time.sleep(chunk)
        remain -= chunk


if __name__ == "__main__":
    sys.exit(main())
