#!/usr/bin/env python3
"""MCD SIGNAL V1.3 - Read-only XAUUSD M5 monitor.
Source: Biquote API, NOT official HFM XAUUSDc feed.
Notifications only; no trades are executed.
"""

import json
import math
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone

BASE = "https://biquote.io/api"
SYMBOL = "XAUUSD"
STATE = ".mcd_signal_state.json"
STATUS = "signal_status.json"

TOPIC = os.getenv("NTFY_TOPIC", "").strip()
SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

MIN_BARS = 60
MAX_CANDLE_AGE = 900
MAX_QUOTE_AGE = 300
TP1_R = 2.0
TP2_R = 3.0
MIN_SCORE = 5

# S/R pivot configuration
PIVOT_LEFT = 3
PIVOT_RIGHT = 3
SR_MIN_ATR = 0.25
SR_LOOKBACK = 100


def now():
    return datetime.now(timezone.utc)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def fmt(x):
    if isinstance(x, (int, float)) and math.isfinite(x):
        return f"{x:.2f}"
    return "N/A"


def get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "MCD-SIGNAL/1.3"}
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"API HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write(path, obj):
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(temp, path)


def quote():
    data = get_json(f"{BASE}/{SYMBOL}?allowStale=false")

    try:
        bid = float(data["bid"])
        ask = float(data["ask"])
        mid = float(data["mid"])
        timestamp = datetime.fromisoformat(
            str(data["timestamp"]).replace("Z", "+00:00")
        )

        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        age = int(now().timestamp() - timestamp.timestamp())

    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Quote tidak lengkap: {exc}") from exc

    if not all(
        math.isfinite(v) and v > 0 for v in (bid, ask, mid)
    ):
        raise RuntimeError("Harga bid/ask/mid tidak sah")

    if ask < bid:
        raise RuntimeError("Ask lebih rendah daripada bid")

    if age < -60 or age > MAX_QUOTE_AGE:
        raise RuntimeError(f"Quote lapuk ({age}s)")

    market_state = str(data.get("marketState", "")).lower()

    if market_state != "open":
        raise RuntimeError(
            f"Pasaran bukan OPEN: {data.get('marketState', 'UNKNOWN')}"
        )

    spread = float(data.get("spread", ask - bid))

    if not math.isfinite(spread) or spread < 0:
        spread = ask - bid

    return {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread": spread,
        "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
        "age": max(0, age),
    }


def bars(interval="5m", limit=1000):
    url = (
        f"{BASE}/{SYMBOL}/ohlc?"
        + urllib.parse.urlencode(
            {"interval": interval, "limit": limit}
        )
    )

    data = get_json(url)

    if not isinstance(data, dict) or not isinstance(data.get("bars"), list):
        raise RuntimeError(f"OHLC tiada: {interval}")

    unique = {}

    for item in data["bars"]:
        try:
            timestamp = datetime.fromisoformat(
                str(item["openTime"]).replace("Z", "+00:00")
            )

            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)

            candle = {
                "t": int(timestamp.timestamp()),
                "o": float(item["open"]),
                "h": float(item["high"]),
                "l": float(item["low"]),
                "c": float(item["close"]),
                "is_open": bool(item.get("isOpen", False)),
            }

            values = [candle[k] for k in ("o", "h", "l", "c")]

            if not all(math.isfinite(v) for v in values):
                continue

            if candle["h"] < max(
                candle["o"], candle["c"], candle["l"]
            ):
                continue

            if candle["l"] > min(
                candle["o"], candle["c"], candle["h"]
            ):
                continue

            unique[candle["t"]] = candle

        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    return [unique[t] for t in sorted(unique)]


def ema(values, period):
    if len(values) < period:
        raise RuntimeError(f"Candle tidak cukup EMA{period}")

    alpha = 2.0 / (period + 1)
    output = [float(values[0])]

    for value in values[1:]:
        output.append(
            alpha * float(value) + (1 - alpha) * output[-1]
        )

    return output


def atr(candles, period=14):
    true_ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["c"]

        true_ranges.append(
            max(
                current["h"] - current["l"],
                abs(current["h"] - previous_close),
                abs(current["l"] - previous_close),
            )
        )

    if len(true_ranges) < period:
        raise RuntimeError("Candle tidak cukup ATR")

    value = sum(true_ranges[:period]) / period

    for item in true_ranges[period:]:
        value = (value * (period - 1) + item) / period

    if not math.isfinite(value) or value <= 0:
        raise RuntimeError("ATR tidak sah")

    return value


def trend(timeframe, limit=120):
    candles = [
        c for c in bars(timeframe, limit) if not c["is_open"]
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


def quality(score):
    if score <= 3:
        return "WAIT"

    if score == 4:
        return "RISIKO"

    if score <= 6:
        return "RENDAH (C)"

    if score == 7:
        return "SEDERHANA (B)"

    return "KUAT (A)"


def find_support_resistance(candles, current_price, atr_value):
    """Find confirmed local pivots; return nearest valid S/R.

    A pivot is only accepted after candles to its right have closed.
    Nearby levels within SR_MIN_ATR * ATR are ignored.
    """
    recent = candles[-SR_LOOKBACK:]
    left = PIVOT_LEFT
    right = PIVOT_RIGHT
    min_distance = atr_value * SR_MIN_ATR

    pivot_lows = []
    pivot_highs = []

    if len(recent) < left + right + 1:
        return None, None

    for i in range(left, len(recent) - right):
        candle = recent[i]
        window = recent[i - left:i + right + 1]

        lows = [item["l"] for item in window]
        highs = [item["h"] for item in window]

        if (
            candle["l"] == min(lows)
            and lows.count(candle["l"]) == 1
            and candle["l"] < current_price - min_distance
        ):
            pivot_lows.append(candle["l"])

        if (
            candle["h"] == max(highs)
            and highs.count(candle["h"]) == 1
            and candle["h"] > current_price + min_distance
        ):
            pivot_highs.append(candle["h"])

    support = max(pivot_lows) if pivot_lows else None
    resistance = min(pivot_highs) if pivot_highs else None

    return support, resistance


def evaluate(candles, htf):
    closes = [c["c"] for c in candles]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    atr14 = atr(candles)

    current = candles[-1]
    previous = candles[-2]

    r20 = candles[-21:-1]
    r10 = candles[-11:-1]
    r30 = candles[-31:-1]

    high20 = max(c["h"] for c in r20)
    low20 = min(c["l"] for c in r20)
    low10 = min(c["l"] for c in r10)
    high10 = max(c["h"] for c in r10)

    strategy_support = min(c["l"] for c in r30)
    strategy_resistance = max(c["h"] for c in r30)

    bullish = current["c"] > current["o"]
    bearish = current["c"] < current["o"]

    # Strategy 1: Support and Resistance rejection
    snr_buy = (
        current["l"] <= strategy_support + atr14 * 0.25
        and current["c"] > strategy_support
        and bullish
    )

    snr_sell = (
        current["h"] >= strategy_resistance - atr14 * 0.25
        and current["c"] < strategy_resistance
        and bearish
    )

    # Strategy 2: Liquidity sweep
    sweep_buy = (
        current["l"] < low10
        and current["c"] > low10
        and bullish
    )

    sweep_sell = (
        current["h"] > high10
        and current["c"] < high10
        and bearish
    )

    # Strategy 3: Engulfing
    engulf_buy = (
        bullish
        and previous["c"] < previous["o"]
        and current["o"] <= previous["c"]
        and current["c"] >= previous["o"]
        and current["c"] >= strategy_support
    )

    engulf_sell = (
        bearish
        and previous["c"] > previous["o"]
        and current["o"] >= previous["c"]
        and current["c"] <= previous["o"]
        and current["c"] <= strategy_resistance
    )

    # Strategy 4: Breakout confirmed by candle close
    breakout_buy = (
        current["c"] > high20 and previous["c"] <= high20
    )

    breakout_sell = (
        current["c"] < low20 and previous["c"] >= low20
    )

    buy_setups = {
        "SnR Rejection": snr_buy,
        "Liquidity Sweep": sweep_buy,
        "Engulfing": engulf_buy,
        "Breakout": breakout_buy,
    }

    sell_setups = {
        "SnR Rejection": snr_sell,
        "Liquidity Sweep": sweep_sell,
        "Engulfing": engulf_sell,
        "Breakout": breakout_sell,
    }

    body_ok = abs(current["c"] - current["o"]) >= atr14 * 0.1
    extension_ok = abs(current["c"] - ema20[-1]) <= atr14 * 2

    buy_momentum = ema20[-1] > ema50[-1] and bullish
    sell_momentum = ema20[-1] < ema50[-1] and bearish

    bullish_htf = sum(v == "BULLISH" for v in htf.values())
    bearish_htf = sum(v == "BEARISH" for v in htf.values())

    buy_score = max(
        0,
        min(
            8,
            int(current["c"] > ema50[-1])
            + int(buy_momentum)
            + int(abs(current["c"] - strategy_support) <= atr14)
            + int(sweep_buy or engulf_buy or snr_buy)
            + int(breakout_buy)
            + int(body_ok)
            + int(extension_ok)
            + int(bullish_htf > 0)
            - int(bullish_htf == 0),
        ),
    )

    sell_score = max(
        0,
        min(
            8,
            int(current["c"] < ema50[-1])
            + int(sell_momentum)
            + int(abs(current["c"] - strategy_resistance) <= atr14)
            + int(sweep_sell or engulf_sell or snr_sell)
            + int(breakout_sell)
            + int(body_ok)
            + int(extension_ok)
            + int(bearish_htf > 0)
            - int(bearish_htf == 0),
        ),
    )

    side = "WAIT"
    score = max(buy_score, sell_score)
    setup = "None"

    if (
        any(buy_setups.values())
        and buy_score >= MIN_SCORE
        and body_ok
        and extension_ok
        and not any(sell_setups.values())
    ):
        side = "BUY"
        score = buy_score
        setup = next(k for k, v in buy_setups.items() if v)

    elif (
        any(sell_setups.values())
        and sell_score >= MIN_SCORE
        and body_ok
        and extension_ok
        and not any(buy_setups.values())
    ):
        side = "SELL"
        score = sell_score
        setup = next(k for k, v in sell_setups.items() if v)

    support, resistance = find_support_resistance(
        candles, current["c"], atr14
    )

    result = {
        "signal": side,
        "score": score,
        "quality": quality(score),
        "strategy": setup,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "atr14": atr14,
        "ema20": ema20[-1],
        "ema50": ema50[-1],
        "support": support,
        "resistance": resistance,
        "trend": htf,
        "trend_45m": (
            "UPTREND"
            if current["c"] > candles[-9]["o"]
            else "DOWNTREND"
            if current["c"] < candles[-9]["o"]
            else "FLAT"
        ),
        "techniques": {
            "snr": bool(snr_buy or snr_sell),
            "smc": bool(sweep_buy or sweep_sell),
            "pa": bool(engulf_buy or engulf_sell),
            "breakout": bool(breakout_buy or breakout_sell),
        },
    }

    if side in ("BUY", "SELL"):
        entry = current["c"]
        distance = max(
            atr14 * 1.5,
            (current["h"] - current["l"]) * 1.2,
        )

        if side == "BUY":
            stop = min(
                current["l"] - atr14 * 0.15,
                entry - distance,
            )
            risk = entry - stop
            tp1 = entry + risk * TP1_R
            tp2 = entry + risk * TP2_R

        else:
            stop = max(
                current["h"] + atr14 * 0.15,
                entry + distance,
            )
            risk = stop - entry
            tp1 = entry - risk * TP1_R
            tp2 = entry - risk * TP2_R

        result.update({
            "entry": entry,
            "zone_low": entry - atr14 * 0.15,
            "zone_high": entry + atr14 * 0.15,
            "sl": stop,
            "tp1": tp1,
            "tp2": tp2,
            "risk_distance": risk,
        })

    else:
        result.update({
            "entry": None,
            "zone_low": None,
            "zone_high": None,
            "sl": None,
            "tp1": None,
            "tp2": None,
            "risk_distance": None,
        })

    return result


def notify(title, message):
    if not TOPIC:
        print("NTFY_SKIPPED: NTFY_TOPIC tiada")
        return False

    safe_title = title.encode("ascii", "replace").decode("ascii")

    request = urllib.request.Request(
        f"{SERVER}/{TOPIC}",
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": safe_title,
            "Priority": "high",
            "Tags": "chart",
        },
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"ntfy HTTP {response.status}")

    return True


def clear_signal(public):
    public.update({
        "signal": "WAIT",
        "score": 0,
        "quality": "WAIT",
        "strategy": "None",
        "entry": None,
        "zone_low": None,
        "zone_high": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
        "risk_distance": None,
    })


def track_setup(state, candle, public, quote_data):
    """Monitor an active setup using the latest valid Biquote price.

    BUY exits use Bid; SELL exits use Ask.
    Failed ntfy events are retained and retried on the next run.
    This is sampled-price monitoring, not a broker execution feed.
    """
    active = state.get("active_setup")

    if not isinstance(active, dict):
        public["active_setup"] = active
        return

    if active.get("status") not in ("ACTIVE", "TP1_HIT"):
        public["active_setup"] = active
        return

    side = active.get("side")

    if side not in ("BUY", "SELL"):
        public["active_setup"] = active
        return

    # Retry any event that was detected but not successfully notified.
    pending = active.get("pending_event")

    if isinstance(pending, dict):
        event = pending.get("event")
        target_status = pending.get("target_status")
        message = pending.get("message", "")

        try:
            if notify(f"MCD UPDATE - {event}", message):
                active["status"] = target_status
                active["last_event_notified"] = event
                active["pending_event"] = None
                active.pop("notification_error", None)

                if target_status in ("SL_HIT", "TP2_HIT"):
                    active["closed_utc"] = now().isoformat()

                elif target_status == "TP1_HIT":
                    active["tp1_hit_utc"] = now().isoformat()

        except Exception as exc:
            active["notification_error"] = str(exc)
            print("NTFY retry failed:", exc)

        state["active_setup"] = active
        public["active_setup"] = active
        public["setup_event"] = event
        return

    quote_timestamp = quote_data.get("timestamp")

    if not quote_timestamp:
        public["active_setup"] = active
        return

    # Avoid processing the same provider quote repeatedly.
    if quote_timestamp == active.get("last_checked_quote_timestamp"):
        public["active_setup"] = active
        return

    active["last_checked_quote_timestamp"] = quote_timestamp

    # Closing-side price: BUY exits at Bid; SELL exits at Ask.
    sampled_price = (
        quote_data["bid"] if side == "BUY"
        else quote_data["ask"]
    )

    try:
        sl = float(active["sl"])
        tp1 = float(active["tp1"])
        tp2 = float(active["tp2"])
        entry = float(active["entry"])

    except (KeyError, TypeError, ValueError) as exc:
        active["monitoring_error"] = f"Level setup tidak sah: {exc}"
        state["active_setup"] = active
        public["active_setup"] = active
        return

    if not all(
        math.isfinite(v) for v in (sampled_price, sl, tp1, tp2, entry)
    ):
        active["monitoring_error"] = "Harga atau level setup tidak sah"
        state["active_setup"] = active
        public["active_setup"] = active
        return

    if side == "BUY":
        sl_hit = sampled_price <= sl
        tp1_hit = sampled_price >= tp1
        tp2_hit = sampled_price >= tp2

    else:
        sl_hit = sampled_price >= sl
        tp1_hit = sampled_price <= tp1
        tp2_hit = sampled_price <= tp2

    event = None
    target_status = None

    # SL has priority if the sampled quote is already at/past SL.
    if sl_hit:
        event, target_status = "SL_HIT", "SL_HIT"

    # TP1 is notified first. TP2 is monitored on subsequent runs.
    elif active["status"] == "ACTIVE" and tp1_hit:
        event, target_status = "TP1_HIT", "TP1_HIT"

    elif active["status"] == "TP1_HIT" and tp2_hit:
        event, target_status = "TP2_HIT", "TP2_HIT"

    if event:
        message = (
            f"MCD SETUP UPDATE - {event}\n"
            f"Symbol: XAUUSD | M5\n"
            f"Side: {side}\n"
            f"Sampled price: {fmt(sampled_price)}\n"
            f"Price type: {'BID' if side == 'BUY' else 'ASK'}\n"
            f"Entry: {fmt(entry)}\n"
            f"SL: {fmt(sl)}\n"
            f"TP1: {fmt(tp1)}\n"
            f"TP2: {fmt(tp2)}\n"
            f"Quote timestamp: {quote_timestamp}\n"
            f"Detected UTC: {now().isoformat()}\n"
            "Source: Biquote, bukan feed rasmi HFM XAUUSDc.\n"
            "Notification only; no trades are executed."
        )

        # Save event first so it can be retried if ntfy fails.
        active["pending_event"] = {
            "event": event,
            "target_status": target_status,
            "message": message,
        }

        try:
            if notify(f"MCD UPDATE - {event}", message):
                active["status"] = target_status
                active["last_event_notified"] = event
                active["pending_event"] = None
                active.pop("notification_error", None)

                if target_status in ("SL_HIT", "TP2_HIT"):
                    active["closed_utc"] = now().isoformat()

                elif target_status == "TP1_HIT":
                    active["tp1_hit_utc"] = now().isoformat()

        except Exception as exc:
            active["notification_error"] = str(exc)
            print("NTFY update failed; event will retry:", exc)

    state["active_setup"] = active
    public["active_setup"] = active
    public["setup_event"] = event
    public["sampled_exit_price"] = sampled_price
    public["sampled_exit_price_type"] = (
        "BID" if side == "BUY" else "ASK"
    )


def main():
    current_time = now()
    state = read(STATE)

    public = {
        "version": "V1.3",
        "symbol": SYMBOL,
        "timeframe": "M5",
        "data_source": "Biquote API - bukan feed HFM XAUUSDc",
        "updated_utc": current_time.isoformat(),
        "data_status": "DATA_ERROR",
        "signal": "WAIT",
        "score": 0,
        "quality": "WAIT",
        "reason": "Menunggu data sah.",
        "trend": {
            "d1": "UNKNOWN",
            "h4": "UNKNOWN",
            "h1": "UNKNOWN",
            "m30": "UNKNOWN",
            "m15": "UNKNOWN",
        },
        "support": None,
        "resistance": None,
        "trend_45m": "UNKNOWN",
        "techniques": {
            "snr": False,
            "smc": False,
            "pa": False,
            "breakout": False,
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
        "active_setup": state.get("active_setup"),
        "setup_event": None,
    }

    try:
        quote_data = quote()
        raw_candles = bars("5m", 1000)
        epoch = int(current_time.timestamp())

        candles = [
            c for c in raw_candles
            if not c["is_open"] and c["t"] + 300 <= epoch
        ]

        if len(candles) < MIN_BARS:
            raise RuntimeError(
                f"Candle M5 tidak cukup: {len(candles)}"
            )

        last = candles[-1]
        candle_age = epoch - (last["t"] + 300)

        if candle_age < 0 or candle_age > MAX_CANDLE_AGE:
            raise RuntimeError(f"Candle M5 lapuk ({candle_age}s)")

        if (
            len(candles) > 1
            and last["t"] - candles[-2]["t"] > 600
        ):
            raise RuntimeError("Jurang candle M5 terlalu besar")

        higher_timeframes = {}

        for key, timeframe, limit in [
            ("d1", "1d", 90),
            ("h4", "4h", 120),
            ("h1", "1h", 120),
            ("m30", "30m", 120),
            ("m15", "15m", 120),
        ]:
            try:
                higher_timeframes[key] = trend(timeframe, limit)

            except Exception as exc:
                higher_timeframes[key] = "UNKNOWN"
                print(key, "trend unavailable:", exc)

        result = evaluate(candles[-1000:], higher_timeframes)
        public.update(result)

        public.update({
            "data_status": "FRESH",
            "updated_utc": current_time.isoformat(),
            "last_candle_utc": iso(last["t"] + 300),
            "candle_age_seconds": candle_age,
            "price": quote_data["mid"],
            "quote_mid": quote_data["mid"],
            "quote_bid": quote_data["bid"],
            "quote_ask": quote_data["ask"],
            "quote_spread": quote_data["spread"],
            "quote_timestamp_utc": quote_data["timestamp"],
            "quote_age_seconds": quote_data["age"],
            "quote_source": "Biquote API",
            "market_state": "open",
            "candle_close": last["c"],
            "chart_candles": [
                {k: c[k] for k in ("t", "o", "h", "l", "c")}
                for c in candles[-9:]
            ],
            "reason": (
                "Data Biquote lulus semakan. "
                "Bandingkan harga dengan broker sebelum digunakan."
            ),
        })

        # Updated: pass the current quote to the setup monitor.
        track_setup(state, last, public, quote_data)

        if result["signal"] in ("BUY", "SELL"):
            key = f'{result["signal"]}-{last["t"]}'

            if state.get("last_signal_key") != key:
                message = (
                    f'MCD SIGNAL - {result["signal"]}\n'
                    "Symbol: XAUUSD | M5\n"
                    f'Zone: {fmt(result["zone_low"])} - '
                    f'{fmt(result["zone_high"])}\n'
                    f'Entry: {fmt(result["entry"])}\n'
                    f'SL: {fmt(result["sl"])}\n'
                    f'TP1: {fmt(result["tp1"])} (2R)\n'
                    f'TP2: {fmt(result["tp2"])} (3R)\n'
                    f'Quality: {result["score"]}/8 - '
                    f'{result["quality"]}\n'
                    f'Setup: {result["strategy"]}\n'
                    f'Bid/Ask: {fmt(quote_data["bid"])}/'
                    f'{fmt(quote_data["ask"])} | '
                    f'Spread: {fmt(quote_data["spread"])}\n'
                    "Biquote, bukan feed rasmi HFM XAUUSDc."
                )

                try:
                    sent = notify(
                        f'MCD SIGNAL - {result["signal"]}',
                        message,
                    )

                    # Track the setup only after successful notification.
                    if sent:
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
                            "last_checked_candle_t": last["t"],
                            "created_utc": iso(last["t"] + 300),
                        }

                        public["active_setup"] = state["active_setup"]

                    else:
                        public["notification_error"] = (
                            "NTFY_TOPIC tiada; signal tidak dihantar."
                        )

                except Exception as exc:
                    public["notification_error"] = str(exc)
                    print("NTFY signal failed:", exc)

        else:
            public["reason"] = (
                f'WAIT - BUY {result["buy_score"]}/8, '
                f'SELL {result["sell_score"]}/8; '
                "tiada setup sah."
            )

        state["last_status"] = "DATA_VALID"
        state["last_error"] = None
        state["last_candle_t"] = last["t"]

    except Exception as exc:
        clear_signal(public)

        public.update({
            "data_status": "DATA_ERROR",
            "reason": f"DATA_ERROR - {exc}. Signal disekat.",
            "price": None,
            "quote_bid": None,
            "quote_ask": None,
            "quote_mid": None,
            "quote_spread": None,
            "quote_timestamp_utc": None,
            "quote_age_seconds": None,
            "quote_source": None,
            "market_state": None,
            "candle_close": None,
            "chart_candles": [],
        })

        state["last_status"] = "DATA_ERROR"
        state["last_error"] = str(exc)
        print("WAIT / DATA_ERROR:", exc)

    state["last_run_utc"] = current_time.isoformat()
    public["updated_utc"] = current_time.isoformat()
    public["active_setup"] = state.get("active_setup")

    write(STATUS, public)
    write(STATE, state)


if __name__ == "__main__":
    main()
