#!/usr/bin/env python3
"""MCD Signal Monitor - XAUUSD M5, notifications only."""

import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================================================
# CONFIGURATION
# ============================================================
TWELVE_DATA_BASE = "https://api.twelvedata.com"
TWELVE_DATA_API_KEY = (
    os.getenv("TWELVE_DATA_API_KEY")
    or os.getenv("TWELVE_API_KEY")
    or ""
).strip()
TWELVE_DATA_SYMBOL = os.getenv("TWELVE_DATA_SYMBOL", "XAU/USD").strip()

BIQUOTE_BASE = "https://biquote.io/api"
SYMBOL = "XAUUSD"
TIMEFRAME = "M5"

STATE_FILE = ".mcd_signal_state.json"
STATUS_FILE = "signal_status.json"

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

MIN_BARS = 60
MAX_CANDLE_AGE = 900
MAX_QUOTE_AGE = 300
MAX_ENTRY_DRIFT_ATR = 1.2
MIN_SCORE = 5

TP1_R = 1.5
TP2_R = 3.0


# ============================================================
# UTILITIES
# ============================================================
def utc_now():
    return datetime.now(timezone.utc)


def iso_utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def fmt(value):
    try:
        value = float(value)
        return f"{value:.2f}" if math.isfinite(value) else "N/A"
    except (TypeError, ValueError):
        return "N/A"


def get_json(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "MCD-SIGNAL/2.1"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
    os.replace(temporary, path)


# ============================================================
# TWELVE DATA OHLC
# ============================================================
def get_candles(interval="5min", limit=300):
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError(
            "TWELVE_DATA_API_KEY tiada. Semak GitHub Actions Secrets."
        )

    parameters = urllib.parse.urlencode({
        "symbol": TWELVE_DATA_SYMBOL,
        "interval": interval,
        "outputsize": min(max(int(limit), 1), 5000),
        "timezone": "UTC",
        "apikey": TWELVE_DATA_API_KEY,
    })

    payload = get_json(
        f"{TWELVE_DATA_BASE}/time_series?{parameters}"
    )

    if not isinstance(payload, dict):
        raise RuntimeError("Respons Twelve Data tidak sah.")

    if payload.get("status") == "error" or (
        "code" in payload and "values" not in payload
    ):
        raise RuntimeError(
            "Twelve Data: " + str(payload.get("message", "API error"))
        )

    values = payload.get("values")
    if not isinstance(values, list) or not values:
        raise RuntimeError("Twelve Data tidak memulangkan candle.")

    duration_map = {
        "5min": 300,
        "15min": 900,
        "30min": 1800,
        "1h": 3600,
        "4h": 14400,
        "1day": 86400,
    }
    duration = duration_map.get(interval)
    if duration is None:
        raise RuntimeError(f"Interval tidak disokong: {interval}")

    current_epoch = int(utc_now().timestamp())
    candles_by_time = {}

    for item in values:
        try:
            stamp = datetime.fromisoformat(
                str(item["datetime"]).replace("Z", "+00:00")
            )
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)

            candle_time = int(stamp.timestamp())
            candle = {
                "t": candle_time,
                "o": float(item["open"]),
                "h": float(item["high"]),
                "l": float(item["low"]),
                "c": float(item["close"]),
                "is_open": candle_time + duration > current_epoch,
            }

            prices = [candle[k] for k in ("o", "h", "l", "c")]
            if not all(math.isfinite(p) and p > 0 for p in prices):
                continue
            if candle["h"] < max(candle["o"], candle["c"], candle["l"]):
                continue
            if candle["l"] > min(candle["o"], candle["c"], candle["h"]):
                continue

            candles_by_time[candle_time] = candle

        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    return [candles_by_time[t] for t in sorted(candles_by_time)]


# ============================================================
# BIQUOTE LIVE QUOTE
# ============================================================
def get_quote():
    payload = get_json(f"{BIQUOTE_BASE}/{SYMBOL}?allowStale=false")

    try:
        bid = float(payload["bid"])
        ask = float(payload["ask"])
        mid = float(payload["mid"])
        stamp = datetime.fromisoformat(
            str(payload["timestamp"]).replace("Z", "+00:00")
        )
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Quote Biquote tidak lengkap: {exc}") from exc

    if not all(math.isfinite(x) and x > 0 for x in (bid, ask, mid)):
        raise RuntimeError("Harga quote tidak sah.")
    if ask < bid:
        raise RuntimeError("Ask lebih rendah daripada Bid.")

    age = int(utc_now().timestamp() - stamp.timestamp())
    if age < -60 or age > MAX_QUOTE_AGE:
        raise RuntimeError(f"Quote lapuk: {age} saat.")
    if str(payload.get("marketState", "")).lower() != "open":
        raise RuntimeError(
            f"Pasaran bukan OPEN: {payload.get('marketState', 'UNKNOWN')}"
        )

    try:
        spread = float(payload.get("spread", ask - bid))
        if not math.isfinite(spread) or spread < 0:
            spread = ask - bid
    except (TypeError, ValueError):
        spread = ask - bid

    return {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread": spread,
        "timestamp": stamp.astimezone(timezone.utc).isoformat(),
        "age": max(0, age),
    }


# ============================================================
# TECHNICAL INDICATORS
# ============================================================
def ema(values, period):
    if len(values) < period:
        raise RuntimeError(f"Candle tidak cukup untuk EMA{period}")

    alpha = 2.0 / (period + 1)
    result = [float(values[0])]

    for value in values[1:]:
        result.append(alpha * float(value) + (1 - alpha) * result[-1])

    return result


def atr(candles, period=14):
    true_ranges = []

    for index in range(1, len(candles)):
        current = candles[index]
        previous_close = candles[index - 1]["c"]
        true_ranges.append(max(
            current["h"] - current["l"],
            abs(current["h"] - previous_close),
            abs(current["l"] - previous_close),
        ))

    if len(true_ranges) < period:
        raise RuntimeError("Candle tidak cukup untuk ATR.")

    value = sum(true_ranges[:period]) / period
    for current_range in true_ranges[period:]:
        value = (value * (period - 1) + current_range) / period

    if not math.isfinite(value) or value <= 0:
        raise RuntimeError("ATR tidak sah.")
    return value


def get_trend(interval):
    candles = [
        c for c in get_candles(interval, 120)
        if not c["is_open"]
    ]

    if len(candles) < 60:
        return "UNKNOWN"

    closes = [c["c"] for c in candles]
    fast = ema(closes, 20)[-1]
    slow = ema(closes, 50)[-1]

    if fast > slow:
        return "BULLISH"
    if fast < slow:
        return "BEARISH"
    return "FLAT"


# ============================================================
# SIGNAL ENGINE
# ============================================================
def evaluate(candles, trends):
    if len(candles) < MIN_BARS:
        raise RuntimeError("Candle M5 tidak mencukupi.")

    closes = [c["c"] for c in candles]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    atr14 = atr(candles)

    current = candles[-1]
    previous = candles[-2]

    high20 = max(c["h"] for c in candles[-21:-1])
    low20 = min(c["l"] for c in candles[-21:-1])
    low10 = min(c["l"] for c in candles[-11:-1])
    high10 = max(c["h"] for c in candles[-11:-1])
    support = min(c["l"] for c in candles[-31:-1])
    resistance = max(c["h"] for c in candles[-31:-1])

    bullish = current["c"] > current["o"]
    bearish = current["c"] < current["o"]

    buy_setups = {
        "SnR Rejection": (
            current["l"] <= support + atr14 * 0.25
            and current["c"] > support and bullish
        ),
        "Liquidity Sweep": (
            current["l"] < low10 and current["c"] > low10 and bullish
        ),
        "Engulfing": (
            bullish and previous["c"] < previous["o"]
            and current["o"] <= previous["c"]
            and current["c"] >= previous["o"]
        ),
        "Breakout": (
            current["c"] > high20 and previous["c"] <= high20
        ),
    }

    sell_setups = {
        "SnR Rejection": (
            current["h"] >= resistance - atr14 * 0.25
            and current["c"] < resistance and bearish
        ),
        "Liquidity Sweep": (
            current["h"] > high10 and current["c"] < high10 and bearish
        ),
        "Engulfing": (
            bearish and previous["c"] > previous["o"]
            and current["o"] >= previous["c"]
            and current["c"] <= previous["o"]
        ),
        "Breakout": (
            current["c"] < low20 and previous["c"] >= low20
        ),
    }

    body_ok = abs(current["c"] - current["o"]) >= atr14 * 0.1
    extension_ok = abs(current["c"] - ema20[-1]) <= atr14 * 2
    buy_momentum = ema20[-1] > ema50[-1] and bullish
    sell_momentum = ema20[-1] < ema50[-1] and bearish

    bullish_htf = sum(v == "BULLISH" for v in trends.values())
    bearish_htf = sum(v == "BEARISH" for v in trends.values())

    buy_score = max(0, min(8, sum([
        int(current["c"] > ema50[-1]),
        int(buy_momentum),
        int(abs(current["c"] - support) <= atr14),
        int(any(buy_setups[k] for k in (
            "SnR Rejection", "Liquidity Sweep", "Engulfing"
        ))),
        int(buy_setups["Breakout"]),
        int(body_ok),
        int(extension_ok),
        int(bullish_htf > 0) - int(bullish_htf == 0),
    ])))

    sell_score = max(0, min(8, sum([
        int(current["c"] < ema50[-1]),
        int(sell_momentum),
        int(abs(current["c"] - resistance) <= atr14),
        int(any(sell_setups[k] for k in (
            "SnR Rejection", "Liquidity Sweep", "Engulfing"
        ))),
        int(sell_setups["Breakout"]),
        int(body_ok),
        int(extension_ok),
        int(bearish_htf > 0) - int(bearish_htf == 0),
    ])))

    side = "WAIT"
    score = max(buy_score, sell_score)
    strategy = "None"

    if (
        any(buy_setups.values()) and buy_score >= MIN_SCORE
        and body_ok and extension_ok and not any(sell_setups.values())
    ):
        side, score = "BUY", buy_score
        strategy = next(k for k, v in buy_setups.items() if v)

    elif (
        any(sell_setups.values()) and sell_score >= MIN_SCORE
        and body_ok and extension_ok and not any(buy_setups.values())
    ):
        side, score = "SELL", sell_score
        strategy = next(k for k, v in sell_setups.items() if v)

    quality = (
        "WAIT" if score <= 3 else
        "RISIKO" if score == 4 else
        "RENDAH (C)" if score <= 6 else
        "SEDERHANA (B)" if score == 7 else
        "KUAT (A)"
    )

    result = {
        "signal": side,
        "score": score,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "quality": quality,
        "strategy": strategy,
        "atr14": atr14,
        "ema20": ema20[-1],
        "ema50": ema50[-1],
        "support": support,
        "resistance": resistance,
        "trend": trends,
        "techniques": {
            "snr": bool(
                buy_setups["SnR Rejection"] or sell_setups["SnR Rejection"]
            ),
            "smc": bool(
                buy_setups["Liquidity Sweep"] or sell_setups["Liquidity Sweep"]
            ),
            "pa": bool(buy_setups["Engulfing"] or sell_setups["Engulfing"]),
            "breakout": bool(
                buy_setups["Breakout"] or sell_setups["Breakout"]
            ),
        },
    }

    if side in ("BUY", "SELL"):
        entry = current["c"]
        distance = max(
            atr14 * 1.5,
            (current["h"] - current["l"]) * 1.2,
        )

        if side == "BUY":
            sl = min(current["l"] - atr14 * 0.15, entry - distance)
            risk = entry - sl
            tp1 = entry + risk * TP1_R
            tp2 = entry + risk * TP2_R
        else:
            sl = max(current["h"] + atr14 * 0.15, entry + distance)
            risk = sl - entry
            tp1 = entry - risk * TP1_R
            tp2 = entry - risk * TP2_R

        result.update({
            "entry": entry,
            "zone_low": entry - atr14 * 0.15,
            "zone_high": entry + atr14 * 0.15,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "risk_distance": risk,
        })
    else:
        result.update({
            "entry": None, "zone_low": None, "zone_high": None,
            "sl": None, "tp1": None, "tp2": None,
            "risk_distance": None,
        })

    return result


# ============================================================
# NTFY
# ============================================================
def notify(title, message):
    if not NTFY_TOPIC:
        print("NTFY_SKIPPED: NTFY_TOPIC tidak ditetapkan.")
        return False

    request = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "chart",
        },
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"ntfy HTTP {response.status}")
    return True


# ============================================================
# ACTIVE SETUP MONITOR
# ============================================================
def monitor_active_setup(state, public, quote):
    """
    Semak setup yang sudah dihantar kepada ntfy.
    BUY exit menggunakan Bid; SELL exit menggunakan Ask.
    Setiap peristiwa hanya dihantar sekali.
    """
    active = state.get("active_setup")
    if not isinstance(active, dict):
        public["active_setup"] = None
        return

    if active.get("status") not in ("ACTIVE", "TP1_HIT"):
        public["active_setup"] = active
        return

    side = active.get("side")
    if side not in ("BUY", "SELL"):
        public["active_setup"] = active
        return

    quote_timestamp = quote["timestamp"]
    if quote_timestamp == active.get("last_checked_quote_timestamp"):
        public["active_setup"] = active
        return

    active["last_checked_quote_timestamp"] = quote_timestamp
    exit_price = quote["bid"] if side == "BUY" else quote["ask"]

    try:
        sl = float(active["sl"])
        tp1 = float(active["tp1"])
        tp2 = float(active["tp2"])
    except (KeyError, TypeError, ValueError):
        active["monitor_error"] = "Nilai SL/TP setup aktif tidak sah."
        public["active_setup"] = active
        return

    if side == "BUY":
        sl_hit = exit_price <= sl
        tp1_hit = exit_price >= tp1
        tp2_hit = exit_price >= tp2
    else:
        sl_hit = exit_price >= sl
        tp1_hit = exit_price <= tp1
        tp2_hit = exit_price <= tp2

    event = None
    if sl_hit:
        event = "SL_HIT"
    elif active["status"] == "ACTIVE" and tp1_hit:
        event = "TP1_HIT"
    elif active["status"] == "TP1_HIT" and tp2_hit:
        event = "TP2_HIT"

    if not event or active.get("last_event_notified") == event:
        public["active_setup"] = active
        public["sampled_exit_price"] = exit_price
        public["sampled_exit_price_type"] = "BID" if side == "BUY" else "ASK"
        return

    message = (
        f"MCD UPDATE - {event}\n"
        f"Symbol: XAUUSD | M5\n"
        f"Side: {side}\n"
        f"Price: {fmt(exit_price)} ({'BID' if side == 'BUY' else 'ASK'})\n"
        f"Entry: {fmt(active.get('entry'))}\n"
        f"SL: {fmt(sl)}\n"
        f"TP1: {fmt(tp1)} ({TP1_R}R)\n"
        f"TP2: {fmt(tp2)} ({TP2_R}R)\n"
        f"Quote UTC: {quote_timestamp}\n"
        f"Source: Biquote; bukan feed rasmi HFM XAUUSDc."
    )

    try:
        if notify(f"MCD UPDATE - {event}", message):
            active["status"] = event
            active["last_event_notified"] = event
            active["last_event_utc"] = utc_now().isoformat()
            active["last_event_price"] = exit_price
            if event in ("SL_HIT", "TP2_HIT"):
                active["closed_utc"] = utc_now().isoformat()
        else:
            active["notification_error"] = "NTFY_TOPIC tiada."
    except Exception as exc:
        active["notification_error"] = str(exc)

    public["active_setup"] = active
    public["sampled_exit_price"] = exit_price
    public["sampled_exit_price_type"] = "BID" if side == "BUY" else "ASK"


# ============================================================
# MAIN MONITOR
# ============================================================
def main():
    run_time = utc_now()
    epoch = int(run_time.timestamp())
    state = read_json(STATE_FILE)

    public = {
        "version": "V2.1",
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "updated_utc": run_time.isoformat(),
        "data_status": "DATA_ERROR",
        "data_source": "Twelve Data OHLC + Biquote quote",
        "ohlc_source": "Twelve Data",
        "quote_source": "Biquote API",
        "data_note": "Biquote bukan feed rasmi HFM XAUUSDc.",
        "signal": "WAIT",
        "score": 0,
        "quality": "WAIT",
        "reason": "Menunggu data sah.",
        "trend": {
            "d1": "UNKNOWN", "h4": "UNKNOWN", "h1": "UNKNOWN",
            "m30": "UNKNOWN", "m15": "UNKNOWN",
        },
        "techniques": {
            "snr": False, "smc": False, "pa": False, "breakout": False,
        },
        "price": None,
        "quote_bid": None,
        "quote_ask": None,
        "quote_mid": None,
        "quote_spread": None,
        "quote_timestamp_utc": None,
        "quote_age_seconds": None,
        "chart_candles": [],
        "entry": None,
        "zone_low": None,
        "zone_high": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
        "risk_distance": None,
        "active_setup": state.get("active_setup"),
    }

    try:
        if not TWELVE_DATA_API_KEY:
            raise RuntimeError(
                "API key kosong. Semak secret TWELVE_DATA_API_KEY."
            )

        quote = get_quote()
        raw = get_candles("5min", 500)
        candles = [
            c for c in raw
            if not c["is_open"] and c["t"] + 300 <= epoch
        ]

        if len(candles) < MIN_BARS:
            raise RuntimeError(f"Candle M5 tidak cukup: {len(candles)}")

        last = candles[-1]
        candle_age = epoch - (last["t"] + 300)

        if candle_age < 0 or candle_age > MAX_CANDLE_AGE:
            raise RuntimeError(f"Candle M5 lapuk: {candle_age}s")
        if len(candles) > 1 and last["t"] - candles[-2]["t"] > 600:
            raise RuntimeError("Jurang candle M5 terlalu besar.")

        trends = {}
        for name, interval in (
            ("d1", "1day"), ("h4", "4h"), ("h1", "1h"),
            ("m30", "30min"), ("m15", "15min"),
        ):
            try:
                trends[name] = get_trend(interval)
            except Exception as exc:
                trends[name] = "UNKNOWN"
                print(f"Trend {name} tidak tersedia: {exc}")

        result = evaluate(candles[-300:], trends)

        # Anti-chasing: BUY guna Ask, SELL guna Bid.
        if result["signal"] in ("BUY", "SELL"):
            live_price = (
                quote["ask"] if result["signal"] == "BUY"
                else quote["bid"]
            )
            drift = abs(live_price - result["entry"])

            if drift > result["atr14"] * MAX_ENTRY_DRIFT_ATR:
                rejected = result["signal"]
                result.update({
                    "signal": "WAIT",
                    "strategy": "NO CHASE",
                    "entry": None,
                    "zone_low": None,
                    "zone_high": None,
                    "sl": None,
                    "tp1": None,
                    "tp2": None,
                    "risk_distance": None,
                    "no_chase_rejected": rejected,
                    "no_chase_drift": drift,
                })

        public.update(result)
        public.update({
            "data_status": "FRESH",
            "updated_utc": run_time.isoformat(),
            "last_candle_utc": iso_utc(last["t"] + 300),
            "candle_age_seconds": candle_age,
            "price": quote["mid"],
            "quote_mid": quote["mid"],
            "quote_bid": quote["bid"],
            "quote_ask": quote["ask"],
            "quote_spread": quote["spread"],
            "quote_timestamp_utc": quote["timestamp"],
            "quote_age_seconds": quote["age"],
            "market_state": "open",
            "candle_close": last["c"],
            "chart_candles": [
                {k: c[k] for k in ("t", "o", "h", "l", "c")}
                for c in candles[-40:]
            ],
        })

        # Pantau setup terdahulu menggunakan quote semasa.
        monitor_active_setup(state, public, quote)

        if result["signal"] in ("BUY", "SELL"):
            key = f'{result["signal"]}-{last["t"]}'

            if state.get("last_signal_key") != key:
                message = (
                    f'MCD SIGNAL - {result["signal"]}\n'
                    f'Symbol: XAUUSD | M5\n'
                    f'Entry: {fmt(result["entry"])}\n'
                    f'SL: {fmt(result["sl"])}\n'
                    f'TP1: {fmt(result["tp1"])} ({TP1_R}R)\n'
                    f'TP2: {fmt(result["tp2"])} ({TP2_R}R)\n'
                    f'Score: {result["score"]}/8 - {result["quality"]}\n'
                    f'Setup: {result["strategy"]}\n'
                    f'Price: {fmt(quote["mid"])}\n'
                    f'Bid/Ask: {fmt(quote["bid"])}/{fmt(quote["ask"])}\n'
                    f'OHLC: Twelve Data; quote: Biquote.\n'
                    f'Bukan feed rasmi HFM XAUUSDc. Notification only.'
                )

                if notify(f'MCD SIGNAL - {result["signal"]}', message):
                    state["last_signal_key"] = key
                    state["active_setup"] = {
                        "id": key,
                        "side": result["signal"],
                        "status": "ACTIVE",
                        "entry": result["entry"],
                        "sl": result["sl"],
                        "tp1": result["tp1"],
                        "tp2": result["tp2"],
                        "risk_distance": result["risk_distance"],
                        "strategy": result["strategy"],
                        "score": result["score"],
                        "created_candle_t": last["t"],
                        "created_utc": iso_utc(last["t"] + 300),
                        "last_checked_quote_timestamp": quote["timestamp"],
                        "last_event_notified": None,
                    }
                    public["active_setup"] = state["active_setup"]
                else:
                    public["notification_error"] = (
                        "NTFY_TOPIC tiada; signal tidak dihantar."
                    )

            public["reason"] = (
                "Setup sah; semak spread dan harga broker sebelum tindakan."
            )
        elif result.get("strategy") == "NO CHASE":
            public["reason"] = (
                "WAIT - harga bergerak lebih daripada 1.2 ATR. Jangan kejar."
            )
        else:
            public["reason"] = (
                f'WAIT - BUY {result["buy_score"]}/8, '
                f'SELL {result["sell_score"]}/8; tiada setup sah.'
            )

        state["last_status"] = "DATA_VALID"
        state["last_error"] = None
        state["last_candle_t"] = last["t"]

    except Exception as exc:
        for key in (
            "entry", "zone_low", "zone_high", "sl", "tp1", "tp2",
            "risk_distance",
        ):
            public[key] = None

        public.update({
            "data_status": "DATA_ERROR",
            "signal": "WAIT",
            "score": 0,
            "quality": "WAIT",
            "strategy": "None",
            "reason": f"DATA_ERROR - {exc}. Signal disekat.",
            "price": None,
            "quote_bid": None,
            "quote_ask": None,
            "quote_mid": None,
            "quote_spread": None,
            "quote_timestamp_utc": None,
            "quote_age_seconds": None,
            "chart_candles": [],
        })

        state["last_status"] = "DATA_ERROR"
        state["last_error"] = str(exc)
        print("WAIT / DATA_ERROR:", exc)

    state["last_run_utc"] = run_time.isoformat()
    public["updated_utc"] = run_time.isoformat()
    public["active_setup"] = state.get("active_setup")

    write_json(STATUS_FILE, public)
    write_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
