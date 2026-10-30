import json,urllib.request,statistics,datetime,time
def get(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req,timeout=25))
def kl(sym,iv,lim):
    return get(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval={iv}&limit={lim}")

print("="*104)
print("实测 1 · 加密 24 小时的活跃度分布：通用规格里的「开盘后第一个小时」在加密对应什么时段？")
print("="*104)
print("口径：近 200 根 1h K 线，按 UTC 小时统计「平均成交额」与「平均振幅 (H-L)/C」")
print()
syms=["BTCUSDT","ETHUSDT","SOLUSDT"]
agg={}
for s in syms:
    try:
        d=kl(s,"1h",200)
        for r in d:
            h=datetime.datetime.fromtimestamp(int(r[0])//1000,datetime.UTC).hour
            qv=float(r[7]); amp=(float(r[2])-float(r[3]))/float(r[4])*100
            agg.setdefault(h,{"qv":[],"amp":[]})
            agg[h]["qv"].append(qv); agg[h]["amp"].append(amp)
        time.sleep(0.3)
    except Exception as e:
        print(f"  {s} err {repr(e)[:50]}")
print("%-6s%16s%12s%10s"%("UTC","中位成交额","中位振幅%","相对活跃度"))
base=statistics.median([statistics.median(v["qv"]) for v in agg.values()])
rank=[]
for h in sorted(agg):
    q=statistics.median(agg[h]["qv"]); a=statistics.median(agg[h]["amp"])
    rel=q/base
    rank.append((rel,h,q,a))
    bar="#"*max(1,int(rel*20))
    print("%-6s%16s%11.2f%%%10.2fx  %s"%(f"{h:02d}:00",f"{q:,.0f}",a,rel,bar))
print()
print("活跃度 TOP5 时段：", " ".join(f"{h:02d}:00({r:.2f}x)" for r,h,q,a in sorted(rank,reverse=True)[:5]))
print("最冷时段：", " ".join(f"{h:02d}:00({r:.2f}x)" for r,h,q,a in sorted(rank)[:3]))

print()
print("="*104)
print("实测 2 · 资金费率：加密独有的持仓成本，通用规格完全没算这一项")
print("="*104)
for s in ["BTCUSDT","ETHUSDT","SOLUSDT"]:
    try:
        fr=get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={s}&limit=1000")
        rates=[float(x["fundingRate"]) for x in fr]
        n=len(rates)
        avg=sum(rates)/n
        pos=sum(1 for x in rates if x>0)/n
        days=n/3
        ann=avg*3*365*100
        print(f"  {s:<10} {n} 期(约{days:.0f}天)  均值 {avg*100:+.4f}%/8h  年化 {ann:+.1f}%  正值占比 {pos:.0%}")
        print(f"     持仓 7 天(21期)多单应付费率 = {avg*21*100:+.3f}%   持仓 30 天 = {avg*90*100:+.3f}%")
        time.sleep(0.3)
    except Exception as e:
        print(f"  {s} err {repr(e)[:60]}")

print()
print("="*104)
print("实测 3 · 插针风险：紧止损在加密会不会被影线扫掉？")
print("="*104)
print("口径：近 1000 根 5m K 线，统计下影线占比 (min(O,C) − L) / C")
print("%-10s%12s%12s%12s%12s"%("symbol","下影中位%","下影P90%","下影P99%","下影>0.5%占比"))
for s in ["BTCUSDT","ETHUSDT","SOLUSDT","DOGEUSDT","LINKUSDT"]:
    try:
        d=kl(s,"5m",1000)
        wicks=[]
        for r in d:
            o,c,l=float(r[1]),float(r[4]),float(r[3])
            w=(min(o,c)-l)/c*100
            if w>=0: wicks.append(w)
        wicks.sort()
        n=len(wicks)
        over=sum(1 for x in wicks if x>0.5)/n*100
        print("%-10s%11.3f%%%11.3f%%%11.3f%%%11.1f%%"%(s,wicks[n//2],wicks[int(n*0.9)],wicks[int(n*0.99)],over))
        time.sleep(0.3)
    except Exception as e:
        print(f"  {s} err {repr(e)[:50]}")
