import sys, yaml, time, statistics
from datetime import datetime, timezone
sys.path.insert(0, ".")
from okx_client import OKXClient
import strategy
cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
c = OKXClient(); ccfg = strategy.Config(cfg)

def fetch5(inst, days=30):
    """从最新往前翻页，取近 days 天的 5m。OKX: after=该时间戳之前(更旧)"""
    allb={}
    cur=None
    floor=int(time.time()*1000)-days*86400*1000
    for _ in range(32):
        d = c.get_candles(inst,"5m",300,after=cur) if cur else c.get_candles(inst,"5m",300)
        if not d: break
        for k in d: allb[int(k[0])]=k
        earliest=min(int(k[0]) for k in d)
        if earliest<=floor: break
        cur=earliest
        time.sleep(0.13)
    return [allb[k] for k in sorted(allb) if k>=floor]

print("="*88)
print("真实触发点验证：历史上「回调到 EMA9 + 3根内收复」时，止损候选能否落进区间")
print("="*88)
print("数据：过去 30 天 5m ｜ 日线 EMA9 按 UTC 日期对齐 ｜ 只看触发时刻的真实宽度")
print()

for inst in ["BTC-USDT-SWAP","ETH-USDT-SWAP","SOL-USDT-SWAP","DOGE-USDT-SWAP","SUI-USDT-SWAP"]:
    # 日线：日期 -> EMA9 / ATR% / ADR%
    d1 = c.get_candles(inst,"1D",300); b1 = strategy.to_bars(d1)
    # 日线接口返回倒序，to_bars 已转正序；日期另取
    d1raw = list(reversed(d1))
    closes=[float(k[4]) for k in d1raw]
    e9=strategy.ema_series(closes,9)
    atr_d=strategy.atr_series(b1,14)
    ema_map={}
    for i,k in enumerate(d1raw):
        day=datetime.fromtimestamp(int(k[0])/1000,timezone.utc).strftime("%Y-%m-%d")
        ema_map[day]=(e9[i], atr_d[i]/closes[i]*100 if atr_d and closes[i] else 0)

    raw5=fetch5(inst,30)
    if len(raw5)<800:
        print(f"{inst} 5m 仅 {len(raw5)} 根({len(raw5)/288:.1f}天)，样本偏少"); 
    b5=strategy.to_bars(raw5); ts5=[int(k[0]) for k in raw5]
    a5=strategy.atr_series(b5,14)

    # 按 UTC 日分组（用于当日最低）
    from collections import defaultdict
    day_idx=defaultdict(list)
    for i,t in enumerate(ts5):
        day=datetime.fromtimestamp(t/1000,timezone.utc).strftime("%Y-%m-%d")
        day_idx[day].append(i)

    hits=[]; passes=0; total=0
    for day,idxs in day_idx.items():
        if day not in ema_map: continue
        ema9,atrd = ema_map[day]
        if atrd<=0: continue
        for pos in range(12, len(idxs)-3):
            i=idxs[pos]
            # 回调：当前 5m 最低 <= EMA9
            if b5[i][2] > ema9: continue
            # V2：之后 3 根内收盘 >= EMA9
            rec=None
            for j in range(i+1, min(i+4, idxs[-1]+1)):
                if b5[j][3] >= ema9: rec=j; break
            if rec is None: continue
            # 触发：rec 那根之后，下一根高点被突破（用 rec+1 的收盘>rec 的高点近似）
            if rec+1 >= len(b5): continue
            cur=b5[rec+1][3]
            if cur <= b5[rec][1]: continue
            total+=1
            day_low=min(b5[k][2] for k in idxs[:pos+1])
            swing_low=min(b5[k][2] for k in idxs[max(0,pos-11):pos+1])
            atr5p=a5[rec]/cur*100 if a5 else 0
            s_max=min(atrd*ccfg.s_max_atr_mult, 4.5*ccfg.s_max_adr_ratio*2, ccfg.s_max_cap)
            s_min=max(ccfg.s_min_floor, atr5p*ccfg.s_min_atr5m_mult)
            for nm,p in [("day_low",day_low),("swing_low",swing_low),("trigger_bar_low",b5[rec][2])]:
                s=(cur-p)/cur*100 if p<cur else 999
                if s_min<=s<=s_max:
                    passes+=1; hits.append((nm,s)); break
    if total:
        print(f"{inst:<18} 触发时刻 {total:>4} 个 ｜ 止损候选可用 {passes:>4} 个 = {passes/total*100:>5.1f}%")
        if hits:
            from collections import Counter
            cnt=Counter(n for n,_ in hits)
            print(f"                   命中来源: {dict(cnt)}  中位宽度 {statistics.median([s for _,s in hits]):.2f}%")
    else:
        print(f"{inst:<18} 30 天内无触发")
    time.sleep(0.3)
