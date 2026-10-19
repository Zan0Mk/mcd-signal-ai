#!/usr/bin/env python3
"""MCD SIGNAL V1 — read-only monitor. No broker orders are sent.
Biquote XAUUSD is NOT guaranteed to match HFM XAUUSDc; compare prices first.
"""
import json, math, os, urllib.request, urllib.parse
from datetime import datetime, timezone

BASE = 'https://biquote.io/api'
SYMBOL = 'XAUUSD'
STATE_PATH = '.mcd_signal_state.json'
STATUS_PATH = 'signal_status.json'
NTFY_TOPIC = os.getenv('NTFY_TOPIC', '').strip()
NTFY_SERVER = os.getenv('NTFY_SERVER', 'https://ntfy.sh').rstrip('/')
MIN_BARS, MAX_CANDLE_AGE, MAX_QUOTE_AGE = 60, 900, 300
TP1_R, TP2_R, MIN_SCORE = 2.0, 3.0, 5


def now(): return datetime.now(timezone.utc)
def iso(ts): return datetime.fromtimestamp(ts, timezone.utc).isoformat()
def fmt(x): return f'{x:.2f}' if isinstance(x, (int, float)) and math.isfinite(x) else '—'


def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'MCD-SIGNAL/1.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        if not 200 <= r.status < 300: raise RuntimeError(f'API HTTP {r.status}')
        return json.loads(r.read().decode('utf-8'))


def read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            v = json.load(f)
            return v if isinstance(v, dict) else {}
    except (OSError, ValueError): return {}


def write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def quote():
    q = get_json(f'{BASE}/{SYMBOL}?allowStale=false')
    try:
        bid, ask, mid = float(q['bid']), float(q['ask']), float(q['mid'])
        dt = datetime.fromisoformat(str(q['timestamp']).replace('Z', '+00:00'))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        age = int(now().timestamp() - dt.timestamp())
    except (KeyError, TypeError, ValueError) as e: raise RuntimeError(f'Quote tidak lengkap: {e}')
    if not all(math.isfinite(x) and x > 0 for x in (bid, ask, mid)) or ask < bid: raise RuntimeError('Bid/Ask tidak sah')
    if age < -60 or age > MAX_QUOTE_AGE: raise RuntimeError(f'Quote lapuk ({age}s)')
    if str(q.get('marketState', '')).lower() != 'open': raise RuntimeError(f'Pasaran bukan OPEN: {q.get("marketState", "UNKNOWN")}')
    return {'bid': bid, 'ask': ask, 'mid': mid, 'spread': float(q.get('spread', ask-bid)),
            'timestamp': dt.astimezone(timezone.utc).isoformat(), 'age': max(0, age), 'source': q.get('source', 'Biquote')}


def bars(interval='5m', limit=1000):
    qs = urllib.parse.urlencode({'interval': interval, 'limit': limit})
    data = get_json(f'{BASE}/{SYMBOL}/ohlc?{qs}')
    if not isinstance(data, dict) or not isinstance(data.get('bars'), list): raise RuntimeError(f'OHLC bars tiada: {interval}')
    unique = {}
    for x in data['bars']:
        try:
            dt = datetime.fromisoformat(str(x['openTime']).replace('Z', '+00:00'))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            c = {'t': int(dt.timestamp()), 'o': float(x['open']), 'h': float(x['high']), 'l': float(x['low']), 'c': float(x['close']), 'is_open': bool(x.get('isOpen', False))}
            if not all(math.isfinite(c[k]) for k in ('o','h','l','c')): continue
            if c['h'] < max(c['o'],c['c'],c['l']) or c['l'] > min(c['o'],c['c'],c['h']): continue
            unique[c['t']] = c
        except (KeyError, TypeError, ValueError, OverflowError): continue
    return [unique[t] for t in sorted(unique)]


def ema(v, n):
    if len(v) < n: raise RuntimeError(f'Candle tidak cukup EMA{n}')
    k, out = 2/(n+1), [float(v[0])]
    for x in v[1:]: out.append(k*x+(1-k)*out[-1])
    return out


def atr(cs, n=14):
    tr = [max(cs[i]['h']-cs[i]['l'], abs(cs[i]['h']-cs[i-1]['c']), abs(cs[i]['l']-cs[i-1]['c'])) for i in range(1,len(cs))]
    if len(tr) < n: raise RuntimeError('Candle tidak cukup ATR')
    a = sum(tr[:n])/n
    for x in tr[n:]: a = (a*(n-1)+x)/n
    if not math.isfinite(a) or a <= 0: raise RuntimeError('ATR tidak sah')
    return a


def trend(interval, limit=120):
    cs = [c for c in bars(interval, limit) if not c['is_open']]
    if len(cs) < 60: return 'UNKNOWN'
    v = [c['c'] for c in cs]; f, s = ema(v,20)[-1], ema(v,50)[-1]
    return 'BULLISH' if f>s else 'BEARISH' if f<s else 'FLAT'


def label(n): return 'WAIT' if n<=3 else 'RISIKO' if n==4 else 'RENDAH (C)' if n<=6 else 'SEDERHANA (B)' if n==7 else 'KUAT (A)'


def evaluate(cs, htf):
    cl = [c['c'] for c in cs]; e20, e50, av = ema(cl,20), ema(cl,50), atr(cs)
    x, p = cs[-1], cs[-2]; r20,r10,r30 = cs[-21:-1],cs[-11:-1],cs[-31:-1]
    hi20,lo20 = max(c['h'] for c in r20),min(c['l'] for c in r20)
    lo10,hi10 = min(c['l'] for c in r10),max(c['h'] for c in r10)
    sup0,res0 = min(c['l'] for c in r30),max(c['h'] for c in r30)
    bull,bear = x['c']>x['o'],x['c']<x['o']
    # SnR, liquidity sweep, engulfing, breakout
    snb=x['l']<=sup0+av*.25 and x['c']>sup0 and bull; sns=x['h']>=res0-av*.25 and x['c']<res0 and bear
    swb=x['l']<lo10 and x['c']>lo10 and bull; sws=x['h']>hi10 and x['c']<hi10 and bear
    eb=bull and p['c']<p['o'] and x['o']<=p['c'] and x['c']>=p['o']; es=bear and p['c']>p['o'] and x['o']>=p['c'] and x['c']<=p['o']
    pab,pas=eb and x['c']>=sup0,es and x['c']<=res0
    brb=x['c']>hi20 and p['c']<=hi20; brs=x['c']<lo20 and p['c']>=lo20
    buy={'SnR Rejection':snb,'Liquidity Sweep':swb,'Engulfing':pab,'Breakout':brb}
    sell={'SnR Rejection':sns,'Liquidity Sweep':sws,'Engulfing':pas,'Breakout':brs}
    body=abs(x['c']-x['o'])>=av*.1; ext=abs(x['c']-e20[-1])<=av*2
    bm=e20[-1]>e50[-1] and bull; sm=e20[-1]<e50[-1] and bear
    bh=sum(v=='BULLISH' for v in htf.values()); sh=sum(v=='BEARISH' for v in htf.values())
    bs=max(0,min(8,int(x['c']>e50[-1])+int(bm)+int(abs(x['c']-sup0)<=av)+int(swb or eb or snb)+int(brb)+int(body)+int(ext)+int(bh>0)-int(bh==0)))
    ss=max(0,min(8,int(x['c']<e50[-1])+int(sm)+int(abs(x['c']-res0)<=av)+int(sws or es or sns)+int(brs)+int(body)+int(ext)+int(sh>0)-int(sh==0)))
    side,score,setup='WAIT',max(bs,ss),'None'
    if any(buy.values()) and bs>=MIN_SCORE and body and ext and not any(sell.values()): side,score,setup='BUY',bs,next(k for k,v in buy.items() if v)
    elif any(sell.values()) and ss>=MIN_SCORE and body and ext and not any(buy.values()): side,score,setup='SELL',ss,next(k for k,v in sell.items() if v)
    lows=[c['l'] for c in cs[-100:] if c['l']<x['c']]; highs=[c['h'] for c in cs[-100:] if c['h']>x['c']]
    o={'signal':side,'score':score,'quality':label(score),'strategy':setup,'buy_score':bs,'sell_score':ss,'atr14':av,'ema20':e20[-1],'ema50':e50[-1],
       'support':max(lows) if lows else None,'resistance':min(highs) if highs else None,'trend':htf,
       'trend_45m':'UPTREND' if cs[-1]['c']>cs[-9]['o'] else 'DOWNTREND' if cs[-1]['c']<cs[-9]['o'] else 'FLAT',
       'techniques':{'snr':bool(snb or sns),'smc':bool(swb or sws),'pa':bool(pab or pas),'breakout':bool(brb or brs)}}
    if side in ('BUY','SELL'):
        entry=x['c']; dist=max(av*1.5,(x['h']-x['l'])*1.2)
        sl=min(x['l']-av*.15,entry-dist) if side=='BUY' else max(x['h']+av*.15,entry+dist)
        risk=entry-sl if side=='BUY' else sl-entry
        o.update({'entry':entry,'zone_low':entry-av*.15,'zone_high':entry+av*.15,'sl':sl,
                  'tp1':entry+risk*TP1_R if side=='BUY' else entry-risk*TP1_R,
                  'tp2':entry+risk*TP2_R if side=='BUY' else entry-risk*TP2_R,'risk_distance':risk})
    else: o.update({'entry':None,'zone_low':None,'zone_high':None,'sl':None,'tp1':None,'tp2':None,'risk_distance':None})
    return o


def notify(title, msg):
    if not NTFY_TOPIC: print('NTFY_SKIPPED: secret NTFY_TOPIC tiada'); return
    req=urllib.request.Request(f'{NTFY_SERVER}/{NTFY_TOPIC}',data=msg.encode(),method='POST',headers={'Title':title,'Priority':'high','Tags':'chart'})
    with urllib.request.urlopen(req,timeout=20) as r:
        if not 200<=r.status<300: raise RuntimeError(f'ntfy HTTP {r.status}')


def main():
    t,state=now(),read_json(STATE_PATH)
    pub={'version':'V1','symbol':'XAUUSD','timeframe':'M5','data_source':'Biquote API — bukan feed HFM XAUUSDc','updated_utc':t.isoformat(),
         'data_status':'DATA_ERROR','signal':'WAIT','score':0,'quality':'WAIT','reason':'Menunggu data sah.',
         'trend':{'d1':'UNKNOWN','h4':'UNKNOWN','h1':'UNKNOWN','m30':'UNKNOWN','m15':'UNKNOWN'},'support':None,'resistance':None,
         'trend_45m':'UNKNOWN','trend_m5':'UNKNOWN','techniques':{'snr':False,'smc':False,'pa':False,'breakout':False},
         'price':None,'quote_bid':None,'quote_ask':None,'quote_mid':None,'quote_spread':None,'quote_timestamp_utc':None,'quote_age_seconds':None,
         'chart_candles':[],'entry':None,'zone_low':None,'zone_high':None,'sl':None,'tp1':None,'tp2':None}
    try:
        q=quote(); raw=bars('5m',1000); ts=int(t.timestamp())
        cs=[c for c in raw if not c['is_open'] and c['t']+300<=ts]
        if len(cs)<MIN_BARS: raise RuntimeError(f'Candle M5 tidak cukup: {len(cs)}')
        last=cs[-1]; age=ts-(last['t']+300)
        if age<0 or age>MAX_CANDLE_AGE: raise RuntimeError(f'Candle M5 lapuk ({age}s)')
        if len(cs)>1 and last['t']-cs[-2]['t']>600: raise RuntimeError('Jurang candle M5 terlalu besar')
        htf={}
        for key,tf,n in [('d1','1d',90),('h4','4h',120),('h1','1h',120),('m30','30m',120),('m15','15m',120)]:
            try: htf[key]=trend(tf,n)
            except Exception as e: htf[key]='UNKNOWN'; print(f'{key} trend unavailable: {e}')
        r=evaluate(cs[-1000:],htf); pub.update(r)
        pub.update({'data_status':'FRESH','updated_utc':t.isoformat(),'last_candle_utc':iso(last['t']+300),'candle_age_seconds':age,
            'price':q['mid'],'quote_mid':q['mid'],'quote_bid':q['bid'],'quote_ask':q['ask'],'quote_spread':q['spread'],
            'quote_timestamp_utc':q['timestamp'],'quote_age_seconds':q['age'],'quote_source':q['source'],'market_state':'open','candle_close':last['c'],
            'chart_candles':[{k:c[k] for k in ('t','o','h','l','c')} for c in cs[-9:]],
            'reason':'Biquote lulus semakan kesegaran. Bandingkan dengan HFM sebelum digunakan.'})
        if r['signal'] in ('BUY','SELL'):
            key=f'{r["signal"]}-{last["t"]}'
            if state.get('last_signal_key')!=key:
                msg=(f'MCD SIGNAL — {r["signal"]}\nSymbol: XAUUSD | M5\nZone: {fmt(r["zone_low"])} – {fmt(r["zone_high"])}\n'
                     f'Entry: {fmt(r["entry"])}\nSL: {fmt(r["sl"])}\nTP1: {fmt(r["tp1"])} (2R)\nTP2: {fmt(r["tp2"])} (3R)\n'
                     f'Quality: {r["score"]}/8 — {r["quality"]}\nSetup: {r["strategy"]}\nBid/Ask: {fmt(q["bid"])}/{fmt(q["ask"])} | Spread: {fmt(q["spread"])}\n'
                     'Sumber Biquote, bukan feed HFM XAUUSDc. Tiada auto-trading.')
                notify(f'MCD SIGNAL — {r["signal"]}',msg); state['last_signal_key']=key
        else: pub['reason']=f'WAIT — BUY {r["buy_score"]}/8, SELL {r["sell_score"]}/8; tiada setup sah.'
        state['last_status'],state['last_error']='DATA_VALID',None; state['last_candle_t']=last['t']
    except Exception as e:
        pub.update({'data_status':'DATA_ERROR','signal':'WAIT','score':0,'quality':'WAIT','reason':f'DATA_ERROR — {e}. Signal disekat.'})
        state['last_status'],state['last_error']='DATA_ERROR',str(e); print('WAIT / DATA_ERROR:',e)
    state['last_run_utc']=t.isoformat(); pub['updated_utc']=t.isoformat()
    write_json(STATUS_PATH,pub); write_json(STATE_PATH,state)

if __name__=='__main__': main()
