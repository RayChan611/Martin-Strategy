"""
飞书通知 · 交易事件推送

设计：
1. 用飞书自定义机器人 webhook，发 interactive card（比纯文本好看）
2. 颜色约定按中文习惯：开仓=蓝，盈利=红，亏损=绿，风控/异常=橙
   （注意：颜色只是辅助，文案里会明确写「盈利/亏损」，避免误读）
3. 发送失败只记日志，不阻断交易主流程 —— 通知不该影响下单
4. webhook 从环境变量读，不写进代码或 config

用法：
    n = FeishuNotifier.from_env()
    n.notify_open(inst, px, stop, lots, notional, risk, s_pct, reasons)
    n.notify_close(inst, px_in, px_out, pnl_usdt, pnl_r, reason)
"""

import json
import os
import time
import urllib.error
import urllib.request


class FeishuNotifier:
    def __init__(self, webhook: str = "", enabled: bool = True, timeout: int = 8):
        self.webhook = (webhook or "").strip()
        self.enabled = enabled and bool(self.webhook)
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def from_env(cls, enabled=True):
        return cls(os.getenv("FEISHU_WEBHOOK", ""), enabled=enabled)

    # ---------- 底层 ----------
    def _post(self, payload) -> bool:
        if not self.enabled:
            return False
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.webhook, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0 (compatible; martin-live/1.0)"},
        )
        for attempt in range(2):
            try:
                with self._opener.open(req, timeout=self.timeout) as r:
                    resp = json.loads(r.read().decode() or "{}")
                # 飞书返回 {code:0, msg:"success"} 或 {StatusCode:0}
                code = resp.get("code", resp.get("StatusCode", -1))
                if code == 0:
                    return True
                print(f"    [feishu] 返回异常 code={code} msg={resp.get('msg')}")
                return False
            except Exception as e:
                if attempt == 0:
                    time.sleep(1.2)
                    continue
                print(f"    [feishu] 发送失败（不阻断交易）: {type(e).__name__}: {str(e)[:80]}")
                return False
        return False

    def send_card(self, title, lines, template="blue", footer=None):
        """lines: 字符串列表，支持 lark_md 语法（**粗体**、`代码`）"""
        elements = []
        for i, ln in enumerate(lines):
            if ln == "---":
                elements.append({"tag": "hr"})
                continue
            elements.append({"tag": "div", "text": {"content": ln, "tag": "lark_md"}})
        elements.append({
            "tag": "note",
            "elements": [{"tag": "lark_md",
                          "content": footer or time.strftime("%Y-%m-%d %H:%M:%S")}],
        })
        return self._post({
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"content": title, "tag": "plain_text"},
                           "template": template},
                "elements": elements,
            },
        })

    # ---------- 业务事件 ----------
    def notify_open(self, inst, entry_px, stop_px, notional, risk_usdt, s_pct,
                    risk_pct, stop_source, equity, leverage, dry=False):
        """开仓卡片：不含张数，含账户余额与杠杆"""
        prefix = "【DRY】" if dry else ""
        r_val = entry_px - stop_px
        lines = [
            f"**标的**　`{inst}`",
            "**方向**　做多（市价）",
            "---",
            f"**入场价**　{entry_px:.6g}",
            f"**止损价**　{stop_px:.6g}　（{stop_source}）",
            f"**止损宽度**　{s_pct:.2f}%",
            f"**1R 幅度**　{r_val:.6g}",
            "---",
            f"**名义金额**　{notional:.2f} USDT",
            f"**风险额**　{risk_usdt:.3f} USDT　（占比 {risk_pct:.2f}%）",
            "---",
            f"**账户余额**　{equity:.2f} USDT",
            f"**杠杆**　{leverage}x",
        ]
        return self.send_card(f"{prefix}🔵 开仓 · {inst.split('-')[0]}",
                              lines, template="blue")

    def notify_close(self, inst, entry_px, exit_px, pnl_usdt, pnl_r, reason, dry=False):
        prefix = "【DRY】" if dry else ""
        win = pnl_usdt >= 0
        # 中文习惯：盈利红、亏损绿
        tpl = "red" if win else "green"
        icon = "🔴" if win else "🟢"
        lines = [
            f"**标的**　`{inst}`",
            f"**出场原因**　{reason}",
            "---",
            f"**入场**　{entry_px:.6g}",
            f"**出场**　{exit_px:.6g}",
            f"**盈亏**　{'+' if win else ''}{pnl_usdt:.4f} USDT　（{pnl_r:+.2f}R）",
        ]
        return self.send_card(f"{prefix}{icon} 平仓 · {inst.split('-')[0]}",
                              lines, template=tpl)

    def notify_risk(self, title, detail_lines, level="warn"):
        tpl = {"warn": "orange", "stop": "red", "info": "grey"}.get(level, "orange")
        icon = {"warn": "⚠️", "stop": "⛔", "info": "ℹ️"}.get(level, "⚠️")
        return self.send_card(f"{icon} {title}", detail_lines, template=tpl)

    def notify_plain(self, text, template="grey"):
        return self.send_card("Martin Live", [text], template=template)


if __name__ == "__main__":
    n = FeishuNotifier.from_env()
    print("webhook 已配置:", bool(n.webhook), "| enabled:", n.enabled)
    ok1 = n.notify_open(
        "BTC-USDT-SWAP", 78000, 76830, 1000.0, 15.0, 1.5, 0.50, "day_low",
        equity=2999.58, leverage=3)
    print("开仓卡片:", "✅" if ok1 else "❌")
    ok2 = n.notify_close(
        "BTC-USDT-SWAP", 78000, 79200, 0.61, 1.54, "9EMA 跟踪")
    print("平仓卡片:", "✅" if ok2 else "❌")
    ok3 = n.notify_risk("测试风控告警", ["这是连通性测试", "**无需处理**"], level="warn")
    print("风控卡片:", "✅" if ok3 else "❌")
