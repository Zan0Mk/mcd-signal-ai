#!/usr/bin/env python3
"""MCD Signal Monitor - XAUUSD M5, notifications only; no trades executed."""
import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://biquote.io/api"
TWELVE_DATA_BASE = "https://api.twelvedata.com"
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
TWELVE_DATA_SYMBOL = os.getenv("TWELVE_DATA_SYMBOL", "XAU/USD").strip()
SYMBOL = "XAUUSD"
STATE = ".mcd_signal_state.json"
STATUS = "signal_status.json"
TOPIC = os.getenv("NTFY_TOPIC", "").strip()
SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

MIN_BARS = 60
MAX_CANDLE_AGE = 900
MAX_QUOTE_AGE = 300
MAX_ENTRY_DRIFT_ATR = 1.2
TP1_R = 1.5
TP2_R = 3.0
MIN_SCORE = 5


def now():
    return datetime.now(timezone.utc)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def fmt(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return f"{value:.2f}"
    return "N/A"


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "MCD-SIGNAL/1.5"})
    with urllib.request.urlopen(req, timeout=20) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def get_quote():
    data = get_json(f"{BASE}/{SYMBOL}?allowStale=false")
    try:
        bid, ask, mid = float(data["bid"]), float(data["ask"]), float(data["mid"])
        stamp = datetime.fromisoformat(str(data["timestamp"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Quote tidak lengkap: {exc}") from exc

    if not all(math.isfinite(x) and x > 0 for x in (bid, ask, mid)):
        raise RuntimeError("Harga quote tidak sah")
    if ask < bid:
        raise RuntimeError("Ask lebih rendah daripada Bid")
    age = int(now().timestamp() - stamp.timestamp())
    if age < -60 or age > MAX_QUOTE_AGE:
        raise RuntimeError(f"Quote lapuk ({age}s)")
    if str(data.get("marketState", "")).lower() != "open":
        raise RuntimeError(f"Pasaran bukan OPEN: {data.get('marketState', 'UNKNOWN')}")
    spread = float(data.get("spread", ask - bid))
    if not math.isfinite(spread) or spread < 0:
        spread = ask - bid
    return {
        "bid": bid, "ask": ask, "mid": mid, "spread": spread,
        "timestamp": stamp.astimezone(timezone.utc).isoformat(), "age": max(0, age),
    }


def get_bars(interval="5m", limit=1000):
    """Fetch OHLC from Twelve Data; requires the TWELVE_DATA_API_KEY secret."""
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY belum ditetapkan dalam GitHub Secrets/environment")

    interval_map = {
        "5m": ("5min", 300),
        "15m": ("15min", 900),
        "30m": ("30min", 1800),
        "1h": ("1h", 3600),
        "4h": ("4h", 14400),
        "1d": ("1day", 86400),
    }
    if interval not in interval_map:
        raise RuntimeError(f"Interval Twelve Data tidak disokong: {interval}")
    td_interval, duration = interval_map[interval]
    params = urllib.parse.urlencode({
        "symbol": TWELVE_DATA_SYMBOL,
        "interval": td_interval,
        "outputsize": min(max(int(limit), 1), 5000),
        "timezone": "UTC",
        "apikey": TWELVE_DATA_API_KEY,
        "format": "JSON",
    })
    payload = get_json(f"{TWELVE_DATA_BASE}/time_series?{params}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Twelve Data respons tidak sah: {interval}")
    if payload.get("status") == "error" or ("code" in payload and "values" not in payload):
        raise RuntimeError(f"Twelve Data {interval}: {payload.get('message', 'API error')}")
    values = payload.get("values")
    if not isinstance(values, list) or not values:
        raise RuntimeError(f"Twelve Data tiada data OHLC: {interval}")

    unique = {}
    current_epoch = int(now().timestamp())
    for item in values:
        try:
            stamp = datetime.fromisoformat(str(item["datetime"]).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            candle_time = int(stamp.timestamp())
            candle = {
                "t": candle_time, "o": float(item["open"]), "h": float(item["high"]),
                "l": float(item["low"]), "c": float(item["close"]),
                "is_open": candle_time + duration > current_epoch,
            }
            prices = [candle[k] for k in ("o", "h", "l", "c")]
            if not all(math.isfinite(v) and v > 0 for v in prices):
                continue
            if candle["h"] < max(candle["o"], candle["c"], candle["l"]):
                continue
            if candle["l"] > min(candle["o"], candle["c"], candle["h"]):
                continue
            unique[candle_time] = candle
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return [unique[t] for t in sorted(unique)]


def fetch_candles(interval_biquote, interval_twelve, limit):
    """Use Twelve Data OHLC; Biquote is only used for the live quote."""
    mapping = {
        "5min": "5m", "15min": "15m", "30min": "30m",
        "1h": "1h", "4h": "4h", "1day": "1d",
    }
    interval = mapping.get(interval_twelve)
    if interval is None:
        raise RuntimeError(f"Interval Twelve Data tidak disokong: {interval_twelve}")
    return get_bars(interval, limit), "Twelve Data"


def ema(values, period):
    if len(values) < period:
        raise RuntimeError(f"Candle tidak cukup untuk EMA{period}")
    alpha = 2 / (period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1 - alpha) * result[-1])
    return result


def atr(candles, period=14):
    tr = []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]["c"]
        tr.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev), abs(cur["l"] - prev)))
    if len(tr) < period:
        raise RuntimeError("Candle tidak cukup untuk ATR")
    value = sum(tr[:period]) / period
    for x in tr[period:]:
        value = (value * (period - 1) + x) / period
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError("ATR tidak sah")
    return value


def get_trend(interval):
    candles = get_bars(interval, 120)
    candles = [c for c in candles if not c["is_open"]]
    if len(candles) < 60:
        return "UNKNOWN"
    closes = [c["c"] for c in candles]
    fast, slow = ema(closes, 20)[-1], ema(closes, 50)[-1]
    return "BULLISH" if fast > slow else "BEARISH" if fast < slow else "FLAT"


def evaluate(candles, trends):
    closes = [c["c"] for c in candles]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    atr14 = atr(candles)
    cur, prev = candles[-1], candles[-2]
    high20 = max(c["h"] for c in candles[-21:-1])
    low20 = min(c["l"] for c in candles[-21:-1])
    low10 = min(c["l"] for c in candles[-11:-1])
    high10 = max(c["h"] for c in candles[-11:-1])
    support30 = min(c["l"] for c in candles[-31:-1])
    resistance30 = max(c["h"] for c in candles[-31:-1])
    bull, bear = cur["c"] > cur["o"], cur["c"] < cur["o"]

    buy_setups = {
        "SnR Rejection": cur["l"] <= support30 + atr14 * .25 and cur["c"] > support30 and bull,
        "Liquidity Sweep": cur["l"] < low10 and cur["c"] > low10 and bull,
        "Engulfing": bull and prev["c"] < prev["o"] and cur["o"] <= prev["c"] and cur["c"] >= prev["o"],
        "Breakout": cur["c"] > high20 and prev["c"] <= high20,
    }
    sell_setups = {
        "SnR Rejection": cur["h"] >= resistance30 - atr14 * .25 and cur["c"] < resistance30 and bear,
        "Liquidity Sweep": cur["h"] > high10 and cur["c"] < high10 and bear,
        "Engulfing": bear and prev["c"] > prev["o"] and cur["o"] >= prev["c"] and cur["c"] <= prev["o"],
        "Breakout": cur["c"] < low20 and prev["c"] >= low20,
    }
    body_ok = abs(cur["c"] - cur["o"]) >= atr14 * .1
    extension_ok = abs(cur["c"] - e20[-1]) <= atr14 * 2
    buy_momentum, sell_momentum = e20[-1] > e50[-1] and bull, e20[-1] < e50[-1] and bear
    bull_htf = sum(v == "BULLISH" for v in trends.values())
    bear_htf = sum(v == "BEARISH" for v in trends.values())

    buy_score = max(0, min(8, int(cur["c"] > e50[-1]) + int(buy_momentum)
        + int(abs(cur["c"] - support30) <= atr14)
        + int(any(buy_setups[k] for k in ("SnR Rejection", "Liquidity Sweep", "Engulfing")))
        + int(buy_setups["Breakout"]) + int(body_ok) + int(extension_ok)
        + int(bull_htf > 0) - int(bull_htf == 0)))
    sell_score = max(0, min(8, int(cur["c"] < e50[-1]) + int(sell_momentum)
        + int(abs(cur["c"] - resistance30) <= atr14)
        + int(any(sell_setups[k] for k in ("SnR Rejection", "Liquidity Sweep", "Engulfing")))
        + int(sell_setups["Breakout"]) + int(body_ok) + int(extension_ok)
        + int(bear_htf > 0) - int(bear_htf == 0)))

    side, score, setup = "WAIT", max(buy_score, sell_score), "None"
    if any(buy_setups.values()) and buy_score >= MIN_SCORE and body_ok and extension_ok and not any(sell_setups.values()):
        side, score = "BUY", buy_score
        setup = next(k for k, v in buy_setups.items() if v)
    elif any(sell_setups.values()) and sell_score >= MIN_SCORE and body_ok and extension_ok and not any(buy_setups.values()):
        side, score = "SELL", sell_score
        setup = next(k for k, v in sell_setups.items() if v)

    quality = "WAIT" if score <= 3 else "RISIKO" if score == 4 else "RENDAH (C)" if score <= 6 else "SEDERHANA (B)" if score == 7 else "KUAT (A)"
    support_pivots, resistance_pivots = [], []
    look = candles[-100:]
    for i in range(3, len(look) - 3):
        window = look[i-3:i+4]
        if look[i]["l"] == min(c["l"] for c in window) and look[i]["l"] < cur["c"] - atr14 * .25:
            support_pivots.append(look[i]["l"])
        if look[i]["h"] == max(c["h"] for c in window) and look[i]["h"] > cur["c"] + atr14 * .25:
            resistance_pivots.append(look[i]["h"])
    support = max(support_pivots) if support_pivots else None
    resistance = min(resistance_pivots) if resistance_pivots else None

    result = {
        "signal": side, "score": score, "quality": quality, "strategy": setup,
        "buy_score": buy_score, "sell_score": sell_score, "atr14": atr14,
        "ema20": e20[-1], "ema50": e50[-1], "support": support, "resistance": resistance,
        "trend": trends,
        "techniques": {
            "snr": bool(buy_setups["SnR Rejection"] or sell_setups["SnR Rejection"]),
            "smc": bool(buy_setups["Liquidity Sweep"] or sell_setups["Liquidity Sweep"]),
            "pa": bool(buy_setups["Engulfing"] or sell_setups["Engulfing"]),
            "breakout": bool(buy_setups["Breakout"] or sell_setups["Breakout"]),
        },
    }
    if side in ("BUY", "SELL"):
        entry = cur["c"]
        distance = max(atr14 * 1.5, (cur["h"] - cur["l"]) * 1.2)
        if side == "BUY":
            sl = min(cur["l"] - atr14 * .15, entry - distance)
            risk = entry - sl
            tp1, tp2 = entry + risk * TP1_R, entry + risk * TP2_R
        else:
            sl = max(cur["h"] + atr14 * .15, entry + distance)
            risk = sl - entry
            tp1, tp2 = entry - risk * TP1_R, entry - risk * TP2_R
        result.update({"entry": entry, "zone_low": entry-atr14*.15, "zone_high": entry+atr14*.15,
                       "sl": sl, "tp1": tp1, "tp2": tp2, "risk_distance": risk})
    else:
        result.update({"entry": None, "zone_low": None, "zone_high": None,
                       "sl": None, "tp1": None, "tp2": None, "risk_distance": None})
    return result


def notify(title, message):
    if not TOPIC:
        print("NTFY_SKIPPED: NTFY_TOPIC tiada")
        return False
    req = urllib.request.Request(
        f"{SERVER}/{TOPIC}", data=message.encode("utf-8"), method="POST",
        headers={"Title": title.encode("ascii", "replace").decode("ascii"),
                 "Priority": "high", "Tags": "chart"})
    with urllib.request.urlopen(req, timeout=20) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"ntfy HTTP {response.status}")
    return True


def clear_signal(public):
    for key in ("entry", "zone_low", "zone_high", "sl", "tp1", "tp2", "risk_distance"):
        public[key] = None
    public.update({"signal": "WAIT", "score": 0, "quality": "WAIT", "strategy": "None"})


def monitor_active(state, public, quote_data):
    """Monitor every persisted setup; quote polling can miss intrainterval touches."""
    setups = state.get("active_setups")
    if not isinstance(setups, list):
        legacy = state.get("active_setup")
        setups = [legacy] if isinstance(legacy, dict) else []

    remaining = []
    events = []
    stamp = quote_data["timestamp"]

    for active in setups:
        if not isinstance(active, dict):
            continue
        status = active.get("status")
        side = active.get("side")
        if status not in ("ACTIVE", "TP1_HIT") or side not in ("BUY", "SELL"):
            # Keep closed setups for dashboard/history, but don't monitor them.
            remaining.append(active)
            continue
        if stamp == active.get("last_checked_quote_timestamp"):
            remaining.append(active)
            continue

        active["last_checked_quote_timestamp"] = stamp
        price = quote_data["bid"] if side == "BUY" else quote_data["ask"]
        try:
            sl, tp1, tp2 = (float(active[k]) for k in ("sl", "tp1", "tp2"))
        except (KeyError, TypeError, ValueError):
            active["monitor_error"] = "SL/TP missing or invalid"
            remaining.append(active)
            continue

        sl_hit = price <= sl if side == "BUY" else price >= sl
        tp1_hit = price >= tp1 if side == "BUY" else price <= tp1
        tp2_hit = price >= tp2 if side == "BUY" else price <= tp2

        # Conservative precedence: if SL and TP are both beyond a sampled quote,
        # report SL first because the polling data cannot establish intrabar order.
        event = None
        if sl_hit:
            event = "SL_HIT"
        elif status == "ACTIVE" and tp1_hit:
            event = "TP1_HIT"
        elif status == "TP1_HIT" and tp2_hit:
            event = "TP2_HIT"

        if event and active.get("last_event_notified") != event:
            message = (
                f"MCD UPDATE - {event}\n"
                f"Symbol: XAUUSD | M5\nSide: {side}\n"
                f"Observed price: {fmt(price)} ({'BID' if side == 'BUY' else 'ASK'})\n"
                f"Entry: {fmt(active.get('entry'))}\n"
                f"SL: {fmt(sl)}\nTP1: {fmt(tp1)}\nTP2: {fmt(tp2)}\n"
                f"Quote UTC: {stamp}\n"
                "Nota: pemeriksaan berkala; bukan pengesahan tick/eksekusi HFM MT5."
            )
            try:
                if notify(f"MCD UPDATE - {event}", message):
                    active["status"] = event
                    active["last_event_notified"] = event
                    active["last_event_utc"] = now().isoformat()
                    events.append({"setup_id": active.get("id"), "event": event, "price": price, "utc": stamp})
                    if event in ("SL_HIT", "TP2_HIT"):
                        active["closed_utc"] = now().isoformat()
                else:
                    active["notification_error"] = "NTFY_TOPIC tiada; update tidak dihantar."
            except Exception as exc:
                active["notification_error"] = str(exc)
        remaining.append(active)

    state["active_setups"] = remaining
    # Legacy compatibility: expose latest setup for dashboards that still use this field.
    state["active_setup"] = remaining[-1] if remaining else None
    public["active_setups"] = remaining
    public["active_setup"] = state["active_setup"]
    public["sl_tp_events"] = events
    public["sampled_exit_price"] = quote_data["bid"]
    public["sampled_exit_price_type"] = "BID"


def main():
    run_time = now()
    state = read_json(STATE)
    public = {
        "version": "V1.5.1", "symbol": SYMBOL, "timeframe": "M5",
        "data_source": "Twelve Data OHLC + Biquote quote; bukan feed rasmi HFM XAUUSDc",
        "updated_utc": run_time.isoformat(), "data_status": "DATA_ERROR",
        "signal": "WAIT", "score": 0, "quality": "WAIT", "reason": "Menunggu data sah.",
        "trend": {"d1": "UNKNOWN", "h4": "UNKNOWN", "h1": "UNKNOWN", "m30": "UNKNOWN", "m15": "UNKNOWN"},
        "support": None, "resistance": None, "techniques": {"snr": False, "smc": False, "pa": False, "breakout": False},
        "price": None, "quote_bid": None, "quote_ask": None, "quote_mid": None,
        "quote_spread": None, "quote_timestamp_utc": None, "quote_age_seconds": None,
        "chart_candles": [], "entry": None, "zone_low": None, "zone_high": None,
        "sl": None, "tp1": None, "tp2": None, "active_setup": state.get("active_setup"), "active_setups": state.get("active_setups", []),
    }
    try:
        q = get_quote()
        epoch = int(run_time.timestamp())
        raw, candle_source = fetch_candles("5m", "5min", 1000)
        candles = [c for c in raw if not c["is_open"] and c["t"] + 300 <= epoch]
        if len(candles) < MIN_BARS:
            raise RuntimeError(f"Candle M5 tidak cukup: {len(candles)}")
        last = candles[-1]
        candle_age = epoch - (last["t"] + 300)
        if candle_age < 0 or candle_age > MAX_CANDLE_AGE:
            raise RuntimeError(f"Candle M5 lapuk ({candle_age}s)")
        if len(candles) > 1 and last["t"] - candles[-2]["t"] > 600:
            raise RuntimeError("Jurang candle M5 terlalu besar")

        trends = {}
        for key, interval in (("d1", "1d"), ("h4", "4h"), ("h1", "1h"), ("m30", "30m"), ("m15", "15m")):
            try:
                trends[key] = get_trend(interval)
            except Exception as exc:
                trends[key] = "UNKNOWN"
                print(f"{key} trend unavailable: {exc}")

        result = evaluate(candles[-1000:], trends)
        if result["signal"] in ("BUY", "SELL"):
            live_price = q["ask"] if result["signal"] == "BUY" else q["bid"]
            drift = abs(live_price - result["entry"])
            if drift > result["atr14"] * MAX_ENTRY_DRIFT_ATR:
                rejected = result["signal"]
                result.update({"signal": "WAIT", "strategy": "NO CHASE", "entry": None,
                               "zone_low": None, "zone_high": None, "sl": None,
                               "tp1": None, "tp2": None, "risk_distance": None,
                               "no_chase_rejected": rejected, "no_chase_drift": drift})
        public.update(result)
        public.update({
            "data_status": "FRESH", "updated_utc": run_time.isoformat(),
            "last_candle_utc": iso(last["t"] + 300), "candle_age_seconds": candle_age,
            "price": q["mid"], "quote_mid": q["mid"], "quote_bid": q["bid"],
            "quote_ask": q["ask"], "quote_spread": q["spread"],
            "quote_timestamp_utc": q["timestamp"], "quote_age_seconds": q["age"],
            "quote_source": "Biquote API", "ohlc_source": candle_source, "market_state": "open", "candle_close": last["c"],
            "chart_candles": [{k: c[k] for k in ("t", "o", "h", "l", "c")} for c in candles[-40:]],
            "reason": "Candle Twelve Data dan quote Biquote lulus semakan. Bandingkan harga dengan broker.",
        })
        monitor_active(state, public, q)

        if result["signal"] in ("BUY", "SELL"):
            key = f'{result["signal"]}-{last["t"]}'
            if state.get("last_signal_key") != key:
                grade = result["quality"].split("(")[-1].rstrip(")") if "(" in result["quality"] else result["quality"]
                if result["signal"] == "BUY":
                    emoji = "🟢"
                    arrow = "▲"
                else:
                    emoji = "🔴"
                    arrow = "▼"
                msg = (
                    f'{emoji} MCD SIGNAL >> {result["signal"]} {arrow}\\n'
                    f'Zone: {fmt(result["zone_low"])} – {fmt(result["zone_high"])}\\n'
                    f'SL: {fmt(result["sl"])}\\n'
                    f'TP1: {fmt(result["tp1"])}\\n'
                    f'TP2: {fmt(result["tp2"])}\\n'
                    f'Setup: {result["strategy"]}\\n'
                    f'Skor: {result["score"]}/8 - {grade}'
                )
                try:
                    sent = notify(f'MCD SIGNAL - {result["signal"]}', msg)
                    if sent:
                        state["last_signal_key"] = key
                        new_setup = {
                            "id": key, "side": result["signal"], "status": "ACTIVE",
                            "entry": result["entry"], "sl": result["sl"], "tp1": result["tp1"],
                            "tp2": result["tp2"], "risk_distance": result["risk_distance"],
                            "strategy": result["strategy"], "score": result["score"],
                            "created_candle_t": last["t"], "created_utc": iso(last["t"] + 300),
                        }
                        setups = state.get("active_setups")
                        if not isinstance(setups, list):
                            legacy = state.get("active_setup")
                            setups = [legacy] if isinstance(legacy, dict) else []
                        setups.append(new_setup)
                        state["active_setups"] = setups
                        state["active_setup"] = new_setup
                        public["active_setups"] = setups
                        public["active_setup"] = new_setup
                    else:
                        public["notification_error"] = "NTFY_TOPIC tiada; signal tidak dihantar."
                except Exception as exc:
                    public["notification_error"] = str(exc)
        else:
            public["reason"] = (
                "WAIT - harga sudah bergerak terlalu jauh daripada entry; jangan kejar harga."
                if result.get("strategy") == "NO CHASE"
                else f'WAIT - BUY {result["buy_score"]}/8, SELL {result["sell_score"]}/8; tiada setup sah.'
            )
        state["last_status"], state["last_error"], state["last_candle_t"] = "DATA_VALID", None, last["t"]
    except Exception as exc:
        clear_signal(public)
        public.update({
            "data_status": "DATA_ERROR", "reason": f"DATA_ERROR - {exc}. Signal disekat.",
            "price": None, "quote_bid": None, "quote_ask": None, "quote_mid": None,
            "quote_spread": None, "quote_timestamp_utc": None, "quote_age_seconds": None,
            "quote_source": None, "ohlc_source": None, "market_state": None, "candle_close": None, "chart_candles": [],
        })
        state["last_status"], state["last_error"] = "DATA_ERROR", str(exc)
        print("WAIT / DATA_ERROR:", exc)

    state["last_run_utc"] = run_time.isoformat()
    public["updated_utc"] = run_time.isoformat()
    public["active_setup"] = state.get("active_setup")
    public["active_setups"] = state.get("active_setups", [])
    write_json(STATUS, public)
    write_json(STATE, state)


if __name__ == "__main__":
    main()
