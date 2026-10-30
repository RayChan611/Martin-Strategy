#!/usr/bin/env python3
"""
系统体检 · 一次看清所有关键状态

只读，不产生任何交易动作。用法：
    cd ~/martin-live && set -a && . ./.env && set +a && python3 tools/healthcheck.py
"""

import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from okx_client import OKXClient, OKXError
import strategy
import advisor


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


def line(k, v, flag=""):
    print(f"  {k:<16} {v} {flag}")


def main():
    print("=" * 74)
    print(f"Martin Live 体检  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 74)

    # ---------- 1. 服务 ----------
    print("\n[1] 服务进程")
    active = sh("systemctl is-active martin-live")
    line("systemd", active, "✅" if active == "active" else "❌ 未运行")
    since = sh("systemctl show martin-live -p ActiveEnterTimestamp --value")
    nrest = sh("systemctl show martin-live -p NRestarts --value")
    line("启动于", since or "?")
    line("重启次数", nrest or "0", "⚠️ 有异常重启" if nrest and nrest != "0" else "")
    pid = sh("systemctl show martin-live -p MainPID --value")
    mem = sh(f"ps -o rss= -p {pid} 2>/dev/null")
    if mem:
        line("内存", f"{int(mem)/1024:.1f} MB")
    line("进程存活", "是" if pid and pid != "0" else "否", "✅" if pid and pid != "0" else "❌")

    # ---------- 2. 配置 ----------
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    print("\n[2] 运行配置")
    dry = cfg["runtime"]["dry_run"]
    demo = cfg["account"]["demo"]
    line("交易模式", f"{'dry_run（不下单）' if dry else '真实下单'} / {'模拟盘' if demo else '⚠️ 实盘真钱'}",
         "✅" if demo or not dry else "⚠️ 真钱")
    line("单笔风险", f"{cfg['position']['risk_per_trade_pct']}%")
    line("最大持仓", f"{cfg['position']['max_positions']} 笔")
    line("杠杆", f"{cfg['position']['leverage_set']}x")
    line("标的数", f"{len(cfg['universe']['symbols'])} 个")
    acfg = cfg.get("advisor", {})
    line("LLM 判断", "启用" if acfg.get("enabled") else "未启用",
         "" if acfg.get("enabled") else "")

    # ---------- 3. 账户 ----------
    print("\n[3] OKX 账户")
    c = OKXClient.from_env()
    try:
        eq = c.get_usdt_equity()
        line("USDT 权益", f"{eq:.4f}", "✅" if eq > 0 else "❌")
        poss = [p for p in c.get_positions() if float(p.get("pos") or 0) != 0]
        line("持仓", f"{len(poss)} 笔")
        for p in poss:
            line(f"  {p['instId']}", f"pos={p['pos']} 均价={p.get('avgPx')} 浮盈={p.get('upl')}")
        algos = c.get_pending_algos("conditional")
        line("条件止损单", f"{len(algos)} 笔",
             "✅ 与持仓匹配" if len(algos) >= len(poss) else ("⚠️ 少于持仓，有裸仓风险" if poss else ""))
        # 关键：持仓数应等于止损单数
        if poss and len(algos) < len(poss):
            print("     ❌❌ 存在未挂止损的持仓，需立即处理")
    except OKXError as e:
        line("OKX 接口", f"❌ {e.code} {str(e.msg)[:60]}")

    # ---------- 4. 通知与 LLM ----------
    print("\n[4] 外部依赖")
    line("飞书 webhook", "已配置" if os.getenv("FEISHU_WEBHOOK") else "未配置",
         "✅" if os.getenv("FEISHU_WEBHOOK") else "❌")
    key = os.getenv("ARK_API_KEY", "")
    line("Ark Key", "已配置" if key else "未配置", "✅" if key else "❌")
    line("Ark 模型", os.getenv("ARK_MODEL", "?"))

    # ---------- 5. 主循环活性 ----------
    print("\n[5] 主循环活性")
    logf = cfg["runtime"]["log_file"]
    try:
        with open(logf, encoding="utf-8") as f:
            lines = f.readlines()
        total = len(lines)
        errs = [l for l in lines if "❌" in l]
        warnings = [l for l in lines if "⚠️" in l]
        scans = [l for l in lines if "扫描信号" in l]
        line("日志总行", str(total))
        line("扫描轮次", str(len(scans)))
        line("错误行", str(len(errs)), "⚠️" if errs else "✅")
        line("警告行", str(len(warnings)))
        # 最后一次活动时间
        last_ts = None
        for l in reversed(lines[-200:]):
            m = re.match(r"\[(\d\d-\d\d \d\d:\d\d:\d\d)\]", l)
            if m:
                last_ts = m.group(1)
                break
        if last_ts:
            line("最后活动", last_ts)
        if errs:
            print("     最近 3 条错误：")
            for l in errs[-3:]:
                print(f"       {l.strip()[:100]}")
    except FileNotFoundError:
        line("日志文件", "不存在", "❌")

    # ---------- 6. 当前市况 ----------
    print("\n[6] 当前市况（为什么没交易）")
    syms = cfg["universe"]["symbols"]
    ccfg = strategy.Config(cfg)
    for inst in syms:
        try:
            st = strategy.build_daily_state(c, inst, ccfg)
            if not st:
                line(inst, "数据不足")
                continue
            mark = "✅ 通过" if st.passed else "✗ 未过"
            line(inst, f"收盘 {st.close:>10.6g}  EMA9 {st.ema9:>10.6g}  "
                       f"偏离 {(st.close-st.ema9)/st.ema9*100:>+6.2f}%  {mark}")
        except Exception as e:
            line(inst, f"异常 {str(e)[:40]}")

    print("\n" + "=" * 74)
    print("体检完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
