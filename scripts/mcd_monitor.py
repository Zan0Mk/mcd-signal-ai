#!/usr/bin/env python3
import json, os, urllib.request, urllib.error
from datetime import datetime, timezone

API = "https://xaus.com/api/v1/chart?symbol=xau&range=1d&interval=5m"
STATE_PATH = ".mcd_signal_state.json"
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

def now_utc():
    return datetime.now(timezone.utc)

def notify(title, message, priority="default", tags="chart_with_upwards_trend"):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC secret not set; notification skipped.")
        return
    # Topic is treated as a secret. ntfy.sh topics are not private by access control;
    # use a long, unguessable topic name and never publish it.
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    req = urllib.request.Request(
        url, data=message.encode("utf-8"), method="POST",
        headers={"Title": title, "Priority": priority, "Tags": tags,
                 "Content-Type": "text/plain; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print("ntfy response:", resp.status)

def fetch_points():
    req = urllib.request.Request(API, headers={"User-Agent": "MCD-Signal-AI/1.4"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        obj = json.loads(resp.read().decode("utf-8"))
    pts = obj.get("points")
    if not isinstance(pts, list):
        raise RuntimeError("API format invalid: points missing")
    out = []
    for p in pts:
        try:
            c = {"t": int(p["t"]), "o": float(p["o"]), "h": float(p["h"]),
                 "l": float(p["l"]), "c": float(p["c"])}
            out.append(c)
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["t"])
    return out

def ema(values, period):
    k = 2 / (period + 1)
    result = [values[0]]
    for x in values[1:]:
        result.append(x * k + result[-1] * (1-k))
    return result

def rsi(values, period=14):
    if len(values) <= period: return float("nan")
    gains, losses = 0.0, 0.0
    for i in range(1, period+1):
        d = values[i] - values[i-1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains/period, losses/period
    for i in range(period+1, len(values)):
        d = values[i] - values[i-1]
        ag = (ag*(period-1)+max(d,0))/period
        al = (al*(period-1)+max(-d,0))/period
    if al == 0: return 100.0
    return 100 - 100/(1+ag/al)

def atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i-1]
        trs.append(max(cur["h"]-cur["l"], abs(cur["h"]-prev["c"]), abs(cur["l"]-prev["c"])))
    if len(trs) < period: return float("nan")
    a = sum(trs[:period])/period
    for tr in trs[period:]:
        a = (a*(period-1)+tr)/period
    return a

def read_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def write_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def main():
    now = now_utc()
    # Approximate XAUUSD retail market window in UTC. Broker holidays and DST may differ.
    # Sunday before 22:00 UTC and Friday from 21:00 UTC are treated as closed.
    wd, mins = now.weekday(), now.hour*60+now.minute
    closed = (wd == 5) or (wd == 6 and mins < 22*60) or (wd == 4 and mins >= 21*60)
    state = read_state()
    if closed:
        print("MARKET_CLOSED (approx UTC schedule); no BUY/SELL evaluation.")
        state["last_status"] = "MARKET_CLOSED"
        write_state(state)
        return

    try:
        points = fetch_points()
        now_ts = int(now.timestamp())
        closed_candles = [p for p in points if p["t"] + 300 <= now_ts]
        if len(closed_candles) < 40:
            raise RuntimeError(f"Not enough closed M5 candles ({len(closed_candles)})")
        last = closed_candles[-1]
        age = now_ts - (last["t"] + 300)
        if age < 0 or age > 900:
            raise RuntimeError(f"Stale M5 candle ({max(0, age)//60} minutes old)")
        candles = closed_candles[-150:]
        closes = [p["c"] for p in candles]
        e9, e21 = ema(closes, 9), ema(closes, 21)
        rv, av = rsi(closes, 14), atr(candles, 14)
        if not (av > 0 and rv == rv):
            raise RuntimeError("Indicators invalid")
        n = len(closes)
        prev_diff, diff = e9[n-2]-e21[n-2], e9[n-1]-e21[n-1]
        side = None
        if prev_diff <= 0 and diff > 0 and 50 <= rv <= 68:
            side = "BUY"
        elif prev_diff >= 0 and diff < 0 and 32 <= rv <= 50:
            side = "SELL"

        state["last_status"] = "DATA_VALID"
        if side:
            candle_id = str(last["t"])
            key = f"{side}-{candle_id}"
            if state.get("last_signal_key") != key:
                entry = last["c"]
                dist = max(av*1.5, (last["h"]-last["l"])*1.2)
                sl = entry-dist if side == "BUY" else entry+dist
                tp = entry+dist if side == "BUY" else entry-dist
                msg = (f"XAUUSD M5 | {side}\n"
                       f"Entry: {entry:.2f}\nSL: {sl:.2f}\nTP1: {tp:.2f}\n"
                       f"RSI: {rv:.1f} | EMA9: {e9[-1]:.2f} | EMA21: {e21[-1]:.2f}\n"
                       f"Candle UTC: {datetime.fromtimestamp(last['t']+300, timezone.utc).strftime('%Y-%m-%d %H:%M')}\n"
                       "Eksperimen sahaja; semak harga/spread HFM. Tiada auto-trading.")
                notify(f"MCD SIGNAL AI — {side}", msg, "high" if side in ("BUY","SELL") else "default",
                       "chart_with_upwards_trend" if side == "BUY" else "chart_with_downwards_trend")
                state["last_signal_key"] = key
                print("Signal sent:", key)
            else:
                print("Same signal candle already notified.")
        else:
            print("DATA_VALID; no new EMA/RSI crossover signal.")
        state["last_candle_t"] = last["t"]
        write_state(state)
    except Exception as e:
        # Fail closed: do not send BUY/SELL if feed is stale, closed, or malformed.
        print("WAIT / DATA_ERROR:", str(e))
        state["last_status"] = "DATA_ERROR"
        state["last_error"] = str(e)
        write_state(state)

if __name__ == "__main__":
    main()
