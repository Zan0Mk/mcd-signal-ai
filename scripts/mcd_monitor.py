#!/usr/bin/env python3
"""MCD SIGNAL read-only monitor. Source is Biquote, not HFM XAUUSDc."""
import json, math, os, urllib.request, urllib.parse
from datetime import datetime, timezone
BASE='https://biquote.io/api'; SYMBOL='XAUUSD'; STATE='.mcd_signal_state.json'; STATUS='signal_status.json'
TOPIC=os.getenv('NTFY_TOPIC','').strip(); SERVER=os.getenv('NTFY_SERVER','https://ntfy.sh').rstrip('/')
MIN_BARS=60; MAX_CANDLE_AGE=900; MAX_QUOTE_AGE=300; TP1_R=2.0; TP2_R=3.0; MIN_SCORE=5

def now(): return datetime.now(timezone.utc)
def iso(ts): return datetime.fromtimestamp(ts,timezone.utc).isoformat()
def fmt(x): return f'{x:.2f}' if isinstance(x,(int,float)) and math.isfinite(x) else '—'
def get_json(url):
    req=urllib.request.Request(url,headers={'User-Agent':'MCD-SIGNAL/1.1'})
    with urllib.request.urlopen(req,timeout=20) as r:
        if not 200<=r.status<300: raise RuntimeError(f'API HTTP {r.status}')
        return json.loads(r.read().decode('utf-8'))
def read(path):
    try:
        with open(path,encoding='utf-8') as f: x=json.load(f)
        return x if isinstance(x,dict) else {}
    except (OSError,ValueError): return {}
def write(path,obj):
    tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f: json.dump(obj,f,indent=2,ensure_ascii=False)
    os.replace(tmp,path)
def quote():
    q=get_json(f'{BASE}/{SYMBOL}?allowStale=false')
    try:
        bid,ask,mid=float(q['bid']),float(q['ask']),float(q['mid']); dt=datetime.fromisoformat(str(q['timestamp']).replace('Z','+00:00'))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        age=int(now().timestamp()-dt.timestamp())
    except (KeyError,TypeError,ValueError) as e: raise RuntimeError(f'Quote tidak lengkap: {e}')
    if not all(math.isfinite(x) and x>0 for x in (bid,ask,mid)) or ask<bid: raise RuntimeError('Bid/Ask tidak sah')
    if age < -60 or age>MAX_QUOTE_AGE: raise RuntimeError(f'Quote lapuk ({age}s)')
    if str(q.get('marketState','')).lower()!='open': raise RuntimeError(f'Pasaran bukan OPEN: {q.get("marketState","UNKNOWN")}')
    return {'bid':bid,'ask':ask,'mid':mid,'spread':float(q.get('spread',ask-bid)),'timestamp':dt.astimezone(timezone.utc).isoformat(),'age':max(0,age)}
def bars(interval='5m',limit=1000):
    data=get_json(f'{BASE}/{SYMBOL}/ohlc?'+urllib.parse.urlencode({'interval':interval,'limit':limit}))
    if not isinstance(data,dict) or not isinstance(data.get('bars'),list): raise RuntimeError(f'OHLC tiada: {interval}')
    out={}
    for x in data['bars']:
        try:
            dt=datetime.fromisoformat(str(x['openTime']).replace('Z','+00:00'))
            if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
            c={'t':int(dt.timestamp()),'o':float(x['open']),'h':float(x['high']),'l':float(x['low']),'c':float(x['close']),'is_open':bool(x.get('isOpen',False))}
            if not all(math.isfinite(c[k]) for k in ('o','h','l','c')) or c['h']<max(c['o'],c['c'],c['l']) or c['l']>min(c['o'],c['c'],c['h']): continue
            out[c['t']]=c
        except (KeyError,TypeError,ValueError,OverflowError): continue
    return [out[t] for t in sorted(out)]
def ema(v,n):
    if len(v)<n: raise RuntimeError(f'Candle tidak cukup EMA{n}')
    k=2/(n+1); out=[float(v[0])]
    for x in v[1:]: out.append(k*x+(1-k)*out[-1])
    return out
def atr(cs,n=14):
    tr=[max(cs[i]['h']-cs[i]['l'],abs(cs[i]['h']-cs[i-1]['c']),abs(cs[i]['l']-cs[i-1]['c'])) for i in range(1,len(cs))]
    if len(tr)<n: raise RuntimeError('Candle tidak cukup ATR')
    a=sum(tr[:n])/n
    for x in tr[n:]: a=(a*(n-1)+x)/n
    if not math.isfinite(a) or a<=0: raise RuntimeError('ATR tidak sah')
    return a
def trend(tf,limit=120):
    cs=[c for c in bars(tf,limit) if not c['is_open']]
    if len(cs)<60:return 'UNKNOWN'
    v=[c['c'] for c in cs]; f,s=ema(v,20)[-1],ema(v,50)[-1]
    return 'BULLISH' if f>s else 'BEARISH' if f<s else 'FLAT'
def quality(n): return 'WAIT' if n<=3 else 'RISIKO' if n==4 else 'RENDAH (C)' if n<=6 else 'SEDERHANA (B)' if n==7 else 'KUAT (A)'
def evaluate(cs,htf):
    cl=[c['c'] for c in cs]; e20,e50,av=ema(cl,20),ema(cl,50),atr(cs); x,p=cs[-1],cs[-2]
    r20,r10,r30=cs[-21:-1],cs[-11:-1],cs[-31:-1]; hi20,lo20=max(c['h'] for c in r20),min(c['l'] for c in r20); lo10,hi10=min(c['l'] for c in r10),max(c['h'] for c in r10); sup0,res0=min(c['l'] for c in r30),max(c['h'] for c in r30); bull,bear=x['c']>x['o'],x['c']<x['o']
    sb=x['l']<=sup0+av*.25 and x['c']>sup0 and bull; ss=x['h']>=res0-av*.25 and x['c']<res0 and bear; swb=x['l']<lo10 and x['c']>lo10 and bull; sws=x['h']>hi10 and x['c']<hi10 and bear
    eb=bull and p['c']<p['o'] and x['o']<=p['c'] and x['c']>=p['o'] and x['c']>=sup0; es=bear and p['c']>p['o'] and x['o']>=p['c'] and x['c']<=p['o'] and x['c']<=res0; brb=x['c']>hi20 and p['c']<=hi20; brs=x['c']<lo20 and p['c']>=lo20
    buy={'SnR Rejection':sb,'Liquidity Sweep':swb,'Engulfing':eb,'Breakout':brb}; sell={'SnR Rejection':ss,'Liquidity Sweep':sws,'Engulfing':es,'Breakout':brs}; body=abs(x['c']-x['o'])>=av*.1; ext=abs(x['c']-e20[-1])<=av*2; bm=e20[-1]>e50[-1] and bull; sm=e20[-1]<e50[-1] and bear; bh=sum(v=='BULLISH' for v in htf.values()); sh=sum(v=='BEARISH' for v in htf.values())
    bs=max(0,min(8,int(x['c']>e50[-1])+int(bm)+int(abs(x['c']-sup0)<=av)+int(swb or eb or sb)+int(brb)+int(body)+int(ext)+int(bh>0)-int(bh==0))); sc=max(0,min(8,int(x['c']<e50[-1])+int(sm)+int(abs(x['c']-res0)<=av)+int(sws or es or ss)+int(brs)+int(body)+int(ext)+int(sh>0)-int(sh==0)))
    side,score,setup='WAIT',max(bs,sc),'None'
    if any(buy.values()) and bs>=MIN_SCORE and body and ext and not any(sell.values()): side,score,setup='BUY',bs,next(k for k,v in buy.items() if v)
    elif any(sell.values()) and sc>=MIN_SCORE and body and ext and not any(buy.values()): side,score,setup='SELL',sc,next(k for k,v in sell.items() if v)
    lows=[c['l'] for c in cs[-100:] if c['l']<x['c']]; highs=[c['h'] for c in cs[-100:] if c['h']>x['c']]
    o={'signal':side,'score':score,'quality':quality(score),'strategy':setup,'buy_score':bs,'sell_score':sc,'atr14':av,'ema20':e20[-1],'ema50':e50[-1],'support':max(lows) if lows else None,'resistance':min(highs) if highs else None,'trend':htf,'trend_45m':'UPTREND' if x['c']>cs[-9]['o'] else 'DOWNTREND' if x['c']<cs[-9]['o'] else 'FLAT','techniques':{'snr':bool(sb or ss),'smc':bool(swb or sws),'pa':bool(eb or es),'breakout':bool(brb or brs)}}
    if side in ('BUY','SELL'):
        entry=x['c']; dist=max(av*1.5,(x['h']-x['l'])*1.2); stop=min(x['l']-av*.15,entry-dist) if side=='BUY' else max(x['h']+av*.15,entry+dist); risk=entry-stop if side=='BUY' else stop-entry
        o.update({'entry':entry,'zone_low':entry-av*.15,'zone_high':entry+av*.15,'sl':stop,'tp1':entry+risk*TP1_R if side=='BUY' else entry-risk*TP1_R,'tp2':entry+risk*TP2_R if side=='BUY' else entry-risk*TP2_R,'risk_distance':risk})
    else:o.update({'entry':None,'zone_low':None,'zone_high':None,'sl':None,'tp1':None,'tp2':None,'risk_distance':None})
    return o
def notify(title,msg):
    if not TOPIC: print('NTFY_SKIPPED: NTFY_TOPIC tiada'); return False
    # HTTP headers are ASCII to avoid the observed latin-1 encoding error.
    title=title.encode('ascii','replace').decode('ascii'); req=urllib.request.Request(f'{SERVER}/{TOPIC}',data=msg.encode('utf-8'),method='POST',headers={'Title':title,'Priority':'high','Tags':'chart'})
    with urllib.request.urlopen(req,timeout=20) as r:
        if not 200<=r.status<300: raise RuntimeError(f'ntfy HTTP {r.status}')
    return True
def clear_signal(pub):
    pub.update({'signal':'WAIT','score':0,'quality':'WAIT','strategy':'None','entry':None,'zone_low':None,'zone_high':None,'sl':None,'tp1':None,'tp2':None,'risk_distance':None})
def track_setup(state,c,pub):
    a=state.get('active_setup')
    if not isinstance(a,dict) or a.get('status') not in ('ACTIVE','TP1_HIT'): return
    if c['t']<=int(a.get('last_checked_candle_t',0)): pub['active_setup']=a; return
    side=a['side']; slhit=c['l']<=a['sl'] if side=='BUY' else c['h']>=a['sl']; t1hit=c['h']>=a['tp1'] if side=='BUY' else c['l']<=a['tp1']; t2hit=c['h']>=a['tp2'] if side=='BUY' else c['l']<=a['tp2']; a['last_checked_candle_t']=c['t']; event=None
    if slhit and (t1hit or t2hit): event='AMBIGUOUS_SL_TP'; a['status']='AMBIGUOUS'; a['closed_utc']=iso(c['t']+300)
    elif slhit: event='SL_HIT'; a['status']='SL_HIT'; a['closed_utc']=iso(c['t']+300)
    elif a['status']=='ACTIVE' and t1hit: event='TP1_HIT'; a['status']='TP1_HIT'; a['tp1_hit_utc']=iso(c['t']+300)
    elif a['status'] in ('ACTIVE','TP1_HIT') and t2hit: event='TP2_HIT'; a['status']='TP2_HIT'; a['closed_utc']=iso(c['t']+300)
    if event:
        msg=f'MCD SETUP UPDATE - {event}\nSymbol: XAUUSD | M5\nSide: {side}\nEntry: {fmt(a["entry"])}\nSL: {fmt(a["sl"])}\nTP1: {fmt(a["tp1"])}\nTP2: {fmt(a["tp2"])}\nCandle: {iso(c["t"]+300)}\nBiquote, bukan feed rasmi HFM XAUUSDc.'
        try: notify(f'MCD UPDATE - {event}',msg); a['last_event_notified']=event
        except Exception as e: a['notification_error']=str(e); print('NTFY update failed:',e)
    state['active_setup']=a; pub['active_setup']=a; pub['setup_event']=event
def main():
    t=now(); state=read(STATE); pub={'version':'V1.1','symbol':SYMBOL,'timeframe':'M5','data_source':'Biquote API — bukan feed HFM XAUUSDc','updated_utc':t.isoformat(),'data_status':'DATA_ERROR','signal':'WAIT','score':0,'quality':'WAIT','reason':'Menunggu data sah.','trend':{'d1':'UNKNOWN','h4':'UNKNOWN','h1':'UNKNOWN','m30':'UNKNOWN','m15':'UNKNOWN'},'support':None,'resistance':None,'trend_45m':'UNKNOWN','techniques':{'snr':False,'smc':False,'pa':False,'breakout':False},'price':None,'quote_bid':None,'quote_ask':None,'quote_mid':None,'quote_spread':None,'quote_timestamp_utc':None,'quote_age_seconds':None,'chart_candles':[],'entry':None,'zone_low':None,'zone_high':None,'sl':None,'tp1':None,'tp2':None,'active_setup':state.get('active_setup'),'setup_event':None}
    try:
        q=quote(); raw=bars('5m',1000); epoch=int(t.timestamp()); cs=[c for c in raw if not c['is_open'] and c['t']+300<=epoch]
        if len(cs)<MIN_BARS: raise RuntimeError(f'Candle M5 tidak cukup: {len(cs)}')
        last=cs[-1]; age=epoch-(last['t']+300)
        if age<0 or age>MAX_CANDLE_AGE: raise RuntimeError(f'Candle M5 lapuk ({age}s)')
        if len(cs)>1 and last['t']-cs[-2]['t']>600: raise RuntimeError('Jurang candle M5 terlalu besar')
        htf={}
        for key,tf,n in [('d1','1d',90),('h4','4h',120),('h1','1h',120),('m30','30m',120),('m15','15m',120)]:
            try: htf[key]=trend(tf,n)
            except Exception as e: htf[key]='UNKNOWN'; print(key,'trend unavailable:',e)
        r=evaluate(cs[-1000:],htf); pub.update(r); pub.update({'data_status':'FRESH','updated_utc':t.isoformat(),'last_candle_utc':iso(last['t']+300),'candle_age_seconds':age,'price':q['mid'],'quote_mid':q['mid'],'quote_bid':q['bid'],'quote_ask':q['ask'],'quote_spread':q['spread'],'quote_timestamp_utc':q['timestamp'],'quote_age_seconds':q['age'],'quote_source':'Biquote API','market_state':'open','candle_close':last['c'],'chart_candles':[{k:c[k] for k in ('t','o','h','l','c')} for c in cs[-9:]],'reason':'Biquote lulus semakan kesegaran. Bandingkan dengan HFM sebelum digunakan.'})
        track_setup(state,last,pub)
        if r['signal'] in ('BUY','SELL'):
            key=f'{r["signal"]}-{last["t"]}'
            if state.get('last_signal_key')!=key:
                msg=f'MCD SIGNAL - {r["signal"]}\nSymbol: XAUUSD | M5\nZone: {fmt(r["zone_low"])} - {fmt(r["zone_high"])}\nEntry: {fmt(r["entry"])}\nSL: {fmt(r["sl"])}\nTP1: {fmt(r["tp1"])} (2R)\nTP2: {fmt(r["tp2"])} (3R)\nQuality: {r["score"]}/8 - {r["quality"]}\nSetup: {r["strategy"]}\nBid/Ask: {fmt(q["bid"])}/{fmt(q["ask"])} | Spread: {fmt(q["spread"])}\nBiquote, bukan feed rasmi HFM XAUUSDc.'
                try:
                    notify(f'MCD SIGNAL - {r["signal"]}',msg); state['last_signal_key']=key; state['active_setup']={'id':key,'side':r['signal'],'status':'ACTIVE','entry':r['entry'],'sl':r['sl'],'tp1':r['tp1'],'tp2':r['tp2'],'risk_distance':r['risk_distance'],'strategy':r['strategy'],'score':r['score'],'created_candle_t':last['t'],'last_checked_candle_t':last['t'],'created_utc':iso(last['t']+300)}
                except Exception as e: pub['notification_error']=str(e); print('NTFY signal failed:',e)
        else: pub['reason']=f'WAIT - BUY {r["buy_score"]}/8, SELL {r["sell_score"]}/8; tiada setup sah.'
        state['last_status'],state['last_error']='DATA_VALID',None; state['last_candle_t']=last['t']
    except Exception as e:
        clear_signal(pub); pub.update({'data_status':'DATA_ERROR','reason':f'DATA_ERROR - {e}. Signal disekat.','price':None,'quote_bid':None,'quote_ask':None,'quote_mid':None,'quote_spread':None,'quote_timestamp_utc':None,'quote_age_seconds':None,'quote_source':None,'market_state':None,'candle_close':None,'chart_candles':[]}); state['last_status'],state['last_error']='DATA_ERROR',str(e); print('WAIT / DATA_ERROR:',e)
    state['last_run_utc']=t.isoformat(); pub['updated_utc']=t.isoformat(); pub['active_setup']=state.get('active_setup'); write(STATUS,pub); write(STATE,state)
if __name__=='__main__': main()
