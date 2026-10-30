import json,urllib.request,statistics,datetime,time
from collections import defaultdict
def get(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req,timeout=25))
def kl(sym,iv,lim,start=None):
    u=f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval={iv}&limit={lim}"
    if start: u+=f"&startTime={start}"
    return get(u)
def fetch5(sym,days=30):
    now=int(time.time()*1000); cur=now-days*24*3600*1000
    rows={}
    for _ in range(12):
        d=kl(sym,"5m",1500,cur)
        if not d: break
        for r in d: rows[int(r[0])]=r
        last=int(d[-1][0])
        if last>=now: break
        cur=last+300000; time.sleep(0.3)
    return [rows[k] for k in sorted(rows)]

syms=["BTCUSDT","ETHUSDT","SOLUSDT","DOGEUSDT","LINKUSDT"]
HOLD=24

print("="*100)
print("实测 4（修正）· 紧止损在加密：真实被扫率 + 扫掉后走势")
print("="*100)
print("分母 = 全部样本点；『回归』=扫后4h收盘回到入场价；『恶化』=扫后跌到 -2s")
print()
print("%-10s%-7s%10s%14s%13s%12s"%("symbol","s","被扫率","扫后回归","扫后恶化","其余(横)"))
for s in syms:
    try:
        rows=fetch5(s,30)
        if len(rows)<3000: print(f"  {s} 不足 {len(rows)}"); continue
        C=[float(x[4]) for x in rows]; L=[float(x[3]) for x in rows]
        n=len(C)
        for sp in [1.0,1.5,2.0,3.0]:
            total=0; stops=0; recov=0; worse=0
            for i in range(0,n-HOLD-48,12):
                total+=1
                e=C[i]; sl=e*(1-sp/100); hit=None
                for j in range(i+1,i+HOLD+1):
                    if L[j]<=sl: hit=j; break
                if hit is None: continue
                stops+=1
                back=worsef=False
                for j in range(hit+1,min(hit+49,n)):
                    if C[j]>=e: back=True; break
                    if C[j]<=e*(1-2*sp/100): worsef=True; break
                if back: recov+=1
                elif worsef: worse+=1
            if total>50:
                print("%-10s%-7s%9.1f%%%13.1f%%%12.1f%%%11.1f%%"%(s,f"{sp}%",
                    stops/total*100, recov/total*100, worse/total*100, (stops-recov-worse)/total*100))
        print(); time.sleep(0.4)
    except Exception as e:
        print(f"  {s} err {repr(e)[:60]}")

print("="*100)
print("实测 5（修正）· 全天各 UTC 小时的 V 型反转质量")
print("="*100)
seg=defaultdict(list)
for s in syms:
    try:
        rows=fetch5(s,30)
        if len(rows)<3000: continue
        g=defaultdict(list)
        for idx,r in enumerate(rows):
            dt=datetime.datetime.fromtimestamp(int(r[0])//1000,datetime.UTC)
            g[dt.strftime("%Y-%m-%d")].append((idx,r))
        for dk,items in sorted(g.items()):
            if len(items)!=288: continue
            idxs=[x[0] for x in items]; rr=[x[1] for x in items]
            for k in range(12,276):
                low_prev=min(float(x[3]) for x in rr[k-12:k])
                if float(rr[k][3])<low_prev and float(rr[k+1][2])>float(rr[k][2]):
                    e=float(rr[k+1][2]); ex=idxs[k+1]+HOLD
                    if ex<len(rows):
                        hour=int(rr[k][0])//3600000%24
                        seg[hour].append((float(rows[ex][4])-e)/e*100)
        time.sleep(0.35)
    except Exception as e:
        print(f"  {s} err {repr(e)[:50]}")
print("%-8s%9s%14s%12s"%("UTC","样本","后续2h中位","上涨占比"))
rank=[]
for h in sorted(seg):
    v=seg[h]
    if len(v)>=25:
        m=statistics.median(v); up=sum(1 for x in v if x>0)/len(v)*100
        rank.append((m,h,len(v),up))
        print("%-8s%9d%13.3f%%%11.0f%%"%(f"{h:02d}:00",len(v),m,up))
print()
if rank:
    top=sorted(rank,reverse=True)[:4]; bot=sorted(rank)[:4]
    print("最优时段：", " ".join(f"{h:02d}:00({m:+.2f}%,{u:.0f}%)" for m,h,n,u in top))
    print("最差时段：", " ".join(f"{h:02d}:00({m:+.2f}%,{u:.0f}%)" for m,h,n,u in bot))
