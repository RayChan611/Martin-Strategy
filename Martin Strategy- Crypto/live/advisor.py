"""
止盈判断器 · 内置 LLM 综合判断

定位：把「止盈要不要卖、卖多少」从固定规则交给模型判断，规则只负责
      在合适的时点把账单和盘面递上去。

链路：
    浮盈达触发档 -> collect_context 收集盘面 -> ask_llm 判断 -> 解析成指令 -> 执行

安全边界（重要）：
1. LLM 超时 / 报错 / 返回非法 JSON -> 一律 hold（不动作），绝不猜
2. 单次减仓比例上限由 config 控制（max_trim_pct_per_call）
3. 同一笔交易两次判断有最小间隔（min_interval_hours），防刷
4. 不允许 LLM 加仓，只能 hold / trim / close
"""

import json
import time
import urllib.request

import strategy


# ============ 盘面数据收集 ============
def collect_context(client, tr, cfg, st_daily=None):
    """把判断需要的所有信息收集成一个 dict"""
    inst = tr["inst_id"]
    cur = float(client.get_ticker(inst)["last"])
    entry, stop, r = tr["entry_px"], tr["stop_px"], tr["r_value"]
    pnl_r = (cur - entry) / r if r > 0 else 0.0
    hold_days = (time.time() - tr["opened_at"]) / 86400

    # 最高浮盈（用于判断回撤）
    peak_r = max(tr.get("peak_r", pnl_r), pnl_r)
    drawdown_r = peak_r - pnl_r

    ctx = {
        "symbol": inst,
        "entry_px": entry,
        "cur_px": cur,
        "stop_px": stop,
        "pnl_r": round(pnl_r, 2),
        "peak_r": round(peak_r, 2),
        "drawdown_from_peak_r": round(drawdown_r, 2),
        "hold_days": round(hold_days, 1),
        "s_pct": round(tr.get("s_pct", 0), 2),
        "risk_usdt": round(tr.get("risk_usdt", 0), 3),
        "trimmed": bool(tr.get("trimmed")),
        "stop_source": tr.get("stop_source", ""),
    }

    # 日线状态
    try:
        st = st_daily or strategy.build_daily_state(client, inst, strategy.Config(cfg))
        if st:
            ctx.update({
                "ema9_daily": round(st.ema9, 8),
                "ema21_daily": round(st.ema21, 8),
                "price_vs_ema9_pct": round((cur - st.ema9) / st.ema9 * 100, 2),
                "ema21_dev_pct": round(st.ema21_dev_pct, 2),
                "weekly_streak": st.weekly_streak,
                "atr_daily_pct": round(st.atr_daily_pct, 2),
                "adr_pct": round(st.adr_pct, 2),
                "trend_ok": st.trend_ok,
            })
    except Exception:
        pass

    # 1h 延伸度
    try:
        ext, dev_atr = strategy.is_extended(client, inst, cfg)
        ctx["h1_extended"] = ext
        ctx["h1_dev_atr"] = round(dev_atr, 2)
    except Exception:
        ctx["h1_extended"] = None
        ctx["h1_dev_atr"] = None

    # 大盘（用 BTC 代理）
    try:
        btc = client.get_candles("BTC-USDT-SWAP", "1D", 30)
        bb = strategy.to_bars(btc)
        if len(bb) >= 22:
            closes = [b[3] for b in bb]
            e21 = strategy.ema_series(closes, 21)
            ctx["btc_24h_pct"] = round((closes[-1] / closes[-2] - 1) * 100, 2)
            ctx["btc_vs_ema21_pct"] = round((closes[-1] - e21[-1]) / e21[-1] * 100, 2)
    except Exception:
        pass

    # 资金费率
    try:
        fr = client.get_funding_rate(inst, 3)
        if fr:
            ctx["funding_rate_8h_pct"] = round(float(fr[0]["fundingRate"]) * 100, 4)
    except Exception:
        pass

    return ctx


# ============ 提示词 ============
SYSTEM_PROMPT = """你是量化止盈判断器。根据持仓与盘面，决定是否减仓。

判断要点（按优先级）：
1. 日线趋势结构还在（价格在 EMA9 上方、均线多头排列）→ 默认 hold，让利润奔跑。
2. 只有出现以下情况才减仓：日线趋势结构破坏、从峰值浮盈回撤超过 1/3、大盘系统性转弱。
3. 不要因为「已经赚了很多 R」就减仓，要因为「趋势变了」才减。

输出的 pct 只能从这三个值里选一个：15、30、50（代表减掉当前仓位的百分比，越小越保守）。

只输出一个 JSON 对象，不要解释文字、不要 markdown：
{"action":"hold|trim","pct":0|15|30|50,"confidence":0到1,"reason":"20字内中文"}

- 拿不准、或只是轻微警讯 → hold（宁可不动作，也不乱减）
- 有明确警讯但趋势未彻底破坏 → trim 15 或 30
- 趋势明确破坏 → trim 50

思考控制在 3 步内，立即给结论。
"""


def build_user_prompt(ctx, cfg):
    l1 = cfg["exit"]["first_trim"]
    max_pct = cfg.get("advisor", {}).get("max_trim_pct_per_call", 30)
    lines = [
        "【持仓】",
        f"标的: {ctx['symbol']}",
        f"入场价: {ctx['entry_px']}  当前价: {ctx['cur_px']}  止损价: {ctx['stop_px']}",
        f"止损宽度: {ctx['s_pct']}%   1R 风险额: {ctx['risk_usdt']} USDT",
        f"当前浮盈: {ctx['pnl_r']}R   持仓期间最高浮盈: {ctx['peak_r']}R   从高点回撤: {ctx['drawdown_from_peak_r']}R",
        f"已持有: {ctx['hold_days']} 天   是否已减过仓: {'是' if ctx['trimmed'] else '否'}",
        "",
        "【技术面】",
        f"日线 EMA9: {ctx.get('ema9_daily','?')}   价格相对 EMA9: {ctx.get('price_vs_ema9_pct','?')}%",
        f"日线 EMA21 偏离: {ctx.get('ema21_dev_pct','?')}%   连涨周数: {ctx.get('weekly_streak','?')}",
        f"日线趋势结构是否完好(EMA多头排列): {ctx.get('trend_ok','?')}",
        f"1h 图是否延伸过度: {ctx.get('h1_extended','?')}   偏离 EMA21 约 {ctx.get('h1_dev_atr','?')} 倍 ATR",
        f"日线 ATR: {ctx.get('atr_daily_pct','?')}%   ADR: {ctx.get('adr_pct','?')}%",
        "",
        "【大盘与成本】",
        f"BTC 24h 涨跌: {ctx.get('btc_24h_pct','?')}%   BTC 相对 EMA21: {ctx.get('btc_vs_ema21_pct','?')}%",
        f"当前资金费率(8h): {ctx.get('funding_rate_8h_pct','?')}%",
        "",
        "【系统参考规则（仅作参考，你可以推翻）】",
        f"机械规则是：浮盈 >= {l1.get('r_threshold',5)}R 且 1h 延伸过度 -> 减仓 {l1.get('trim_pct',15)}%",
        f"单次减仓上限: {max_pct}%",
        "",
        "请基于以上信息判断。注意：如果趋势结构完好且没有衰竭迹象，hold 是完全合理的答案。",
    ]
    return "\n".join(lines)


# ============ LLM 调用 ============
def ask_llm(user_prompt, cfg, api_key, base_url, model):
    acfg = cfg.get("advisor", {})
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": acfg.get("temperature", 0),
        # 推理模型会把 token 花在 reasoning 上，必须给足空间
        "max_tokens": acfg.get("max_tokens", 4000),
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=acfg.get("timeout_seconds", 120)) as r:
        d = json.loads(r.read().decode())
    return d["choices"][0]["message"]["content"]


def parse_decision(text, max_pct, min_confidence=0.6):
    """
    解析 LLM 输出并收敛到固定档位。任何异常都回落 hold。

    收敛原因：实测推理模型即使 temperature=0 也会在 trim30% 与 close 之间摇摆，
    对实盘是不可接受的。因此把输出空间压成 hold / trim{15,30,50} 三档，
    并要求 confidence 达标，避免低置信度的激进动作。
    """
    ALLOWED = [15.0, 30.0, 50.0]

    def _hold(reason):
        return {"action": "hold", "pct": 0, "reason": reason, "ok": False}

    if not text:
        return _hold("空响应")
    t = text.strip()
    if "```" in t:
        parts = t.split("```")
        t = parts[1] if len(parts) > 1 else t
        t = t.replace("json", "", 1).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return _hold("输出非 JSON")
    try:
        d = json.loads(t[i:j + 1])
    except Exception:
        return _hold("JSON 解析失败")

    action = str(d.get("action", "hold")).lower().strip()
    try:
        conf = float(d.get("confidence", 0) or 0)
    except Exception:
        conf = 0.0
    reason = str(d.get("reason", ""))[:40]

    # close 不再接受：只允许 hold / trim（平仓交给跟踪止损与时间止损）
    if action == "close":
        action = "trim"
        d["pct"] = 50

    if action != "trim":
        return {"action": "hold", "pct": 0, "reason": reason or "模型选择持有",
                "confidence": conf, "ok": True}

    # 置信度不够 -> 不动
    if conf < min_confidence:
        return {"action": "hold", "pct": 0,
                "reason": f"置信度 {conf:.2f} < {min_confidence}，不动作",
                "confidence": conf, "ok": True}

    try:
        raw_pct = float(d.get("pct", 0) or 0)
    except Exception:
        raw_pct = 0.0
    if raw_pct <= 0:
        return {"action": "hold", "pct": 0, "reason": reason or "pct 无效",
                "confidence": conf, "ok": True}

    # 吸附到最近的允许档位，再受 config 上限约束
    pct = min(ALLOWED, key=lambda x: abs(x - raw_pct))
    pct = min(pct, float(max_pct))
    return {"action": "trim", "pct": pct, "reason": reason,
            "confidence": conf, "ok": True, "raw": text[:300]}


# ============ 对外入口 ============
def decide(client, tr, cfg, api_key, base_url, model, st_daily=None,
           log=print):
    """
    完整决策流程。任何时候出错都返回 hold，不抛异常。
    返回 dict: {action, pct, reason, ok, context}
    """
    acfg = cfg.get("advisor", {})
    if not acfg.get("enabled", True):
        return {"action": "hold", "pct": 0, "reason": "advisor 未启用", "ok": False}
    if not api_key:
        return {"action": "hold", "pct": 0, "reason": "缺 ARK_API_KEY", "ok": False}

    try:
        ctx = collect_context(client, tr, cfg, st_daily)
    except Exception as e:
        log(f"    [advisor] 盘面收集失败: {str(e)[:80]}")
        return {"action": "hold", "pct": 0, "reason": "盘面收集失败", "ok": False}

    prompt = build_user_prompt(ctx, cfg)
    try:
        text = ask_llm(prompt, cfg, api_key, base_url, model)
    except Exception as e:
        log(f"    [advisor] LLM 调用失败，本轮不动作: {type(e).__name__} {str(e)[:80]}")
        return {"action": "hold", "pct": 0, "reason": "LLM 调用失败", "ok": False,
                "context": ctx}

    d = parse_decision(text, acfg.get("max_trim_pct_per_call", 50),
                       acfg.get("min_confidence", 0.6))
    d["context"] = ctx
    if not d["ok"]:
        log(f"    [advisor] 解析异常（{d['reason']}），本轮不动作")
    return d
