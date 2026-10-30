#!/usr/bin/env python3
"""
下单链路验证（模拟盘专用）

依次验证：查权益 -> 拉精度 -> 市价开仓 -> 挂条件止损 -> 查持仓/挂单 -> 撤止损 -> 平仓 -> 确认干净

安全设计：
- 用 finally 确保无论哪一步失败，都会尝试撤单 + 平仓
- 仓位固定 31 USDT 量级（BTC 0.04 张），不按账户比例算，避免模拟盘 3000 U 开出大仓
- 每步都打印，失败即停
"""

import os
import sys
import time
import traceback

# 允许从 tools/ 目录运行：把上级目录加入 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from okx_client import OKXClient, OKXError, calc_lots, actual_notional, fmt_step

INST = "BTC-USDT-SWAP"
TARGET_NOTIONAL = 300.0   # 3000U 账户 x 0.5% 风险 / 1.5% 止损 ≈ 1000U，这里先用 300 小额验证


def step(n, msg):
    print(f"\n[{n}] {msg}")


def main():
    c = OKXClient.from_env()
    print("=" * 66)
    print(f"下单链路验证  |  模拟盘={c.demo}  |  标的={INST}")
    print("=" * 66)

    state = {"ord": None, "algo": None, "opened": False}

    try:
        # --- 1 权益 ---
        step(1, "查账户权益")
        eq = c.get_usdt_equity()
        print(f"    USDT 权益: {eq:.4f}")
        if eq <= 0:
            print("    ❌ 权益为 0，请确认是模拟盘且有资金")
            return 1

        # --- 2 精度 ---
        step(2, "拉合约精度")
        ins = c.get_instruments("SWAP", INST)[0]
        tick, lot, min_sz = ins["tickSz"], ins["lotSz"], ins["minSz"]
        ct_val = float(ins["ctVal"])
        px = float(c.get_ticker(INST)["last"])
        print(f"    现价 {px}  每张 {ct_val*px:.2f}U  tickSz={tick} lotSz={lot} minSz={min_sz}")
        lots = calc_lots(TARGET_NOTIONAL, px, ct_val, lot, min_sz)
        if lots <= 0:
            print("    ❌ 张数计算为 0")
            return 1
        notional = actual_notional(lots, px, ct_val)
        print(f"    目标 {TARGET_NOTIONAL}U -> {fmt_step(lots, lot)} 张 -> 实际 {notional:.2f}U")

        # --- 3 市价开仓 ---
        step(3, "市价买入")
        r = c.place_order(INST, "buy", fmt_step(lots, lot), td_mode="cross",
                          pos_side="long", ord_type="market")
        state["ord"] = r.get("ordId")
        state["opened"] = True
        print(f"    ordId={state['ord']}  sCode={r.get('sCode')}")

        fill_px = px
        for _ in range(12):
            time.sleep(0.6)
            o = c.get_order(INST, state["ord"])
            if o and o.get("state") == "filled":
                fill_px = float(o.get("avgPx") or px)
                break
        print(f"    成交均价 {fill_px}")

        # --- 4 挂条件止损 ---
        step(4, "挂条件止损单（-1.5%）")
        stop_px = float(fmt_step(fill_px * 0.985, tick))
        algo = c.place_stop_loss(INST, fmt_step(lots, lot), stop_px,
                                 td_mode="cross", pos_side="long", side="sell")
        state["algo"] = algo
        print(f"    algoId={algo}  触发价 {stop_px}")

        # --- 5 核对持仓与挂单 ---
        step(5, "核对持仓 + 挂单")
        poss = [p for p in c.get_positions(INST) if float(p.get("pos") or 0) != 0]
        for p in poss:
            print(f"    持仓: {p['instId']} pos={p['pos']} 均价={p.get('avgPx')} 未实现={p.get('upl')}")
        algos = c.get_pending_algos("conditional")
        mine = [a for a in algos if a.get("instId") == INST]
        print(f"    条件单 {len(mine)} 笔： " +
              ", ".join(f"{a.get('algoId')}(sl={a.get('slTriggerPx')})" for a in mine))

        if not poss:
            print("    ⚠️ 未查到持仓（可能模拟盘撮合延迟）")
        if not mine:
            print("    ❌ 未查到止损单！这就是最危险的情况（裸仓）")
            return 1

        print("\n    ✅ 五步全部通过：下单 + 挂止损 都在正常工作")

    except OKXError as e:
        print(f"\n❌ OKX 错误 code={e.code} msg={str(e.msg)[:200]}")
        if e.payload:
            print(f"   payload: {str(e.payload)[:300]}")
    except Exception as e:
        print(f"\n❌ 异常 {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        # --- 清理：撤止损 -> 平仓 ---
        print("\n[清理] 撤止损单 + 平仓")
        if state["algo"]:
            try:
                c.cancel_algo(INST, state["algo"])
                print("    已撤止损单")
            except Exception as e:
                print(f"    撤止损失败: {str(e)[:100]}")
        if state["opened"]:
            try:
                c.close_position(INST, pos_side="long", mgn_mode="cross")
                print("    已下平仓单")
            except Exception as e:
                print(f"    平仓失败: {str(e)[:100]}")
        time.sleep(2)
        try:
            left_p = [p for p in c.get_positions(INST) if float(p.get("pos") or 0) != 0]
            left_a = [a for a in c.get_pending_algos("conditional") if a.get("instId") == INST]
            print(f"    清理后：持仓 {len(left_p)} 笔，条件单 {len(left_a)} 笔")
            if left_p or left_a:
                print("    ⚠️ 仍有残留，请到 OKX 网页手动检查")
            else:
                print("    ✅ 账户干净")
        except Exception as e:
            print(f"    核对失败: {str(e)[:100]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
