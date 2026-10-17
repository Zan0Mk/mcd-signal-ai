#!/usr/bin/env python3
"""MCD SIGNAL V1 monitor.

Rule-based experimental XAUUSD M5 signal monitor. No broker orders are sent.
The feed is considered usable only when recent closed M5 candles pass checks.
"""
import json
import os
import urllib.request
from datetime import datetime, timezone

API = "https://xaus.com/api/v1/chart?symbol=xau&range=5d&interval=5m"
STATE_PATH = ".mcd_signal_state.json"
STATUS_PATH = "signal_status.json"
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

MAX_AGE_SECONDS = 900
MIN_M5_CANDLES = 60
EMA_FAST = 20
EMA_SLOW = 50
ATR_LEN = 14
MIN_SCORE = 5
RR_TP1 = 2.0
ZONE_HALF_WIDTH_ATR = 0.15


def utc_now():
    return datetime.now(timezone.utc)


def fmt(value):
    return f"{value:.2f}" if isinstance(value, (float, int)) else "—"


def notify(title, message, priority="high", tags="chart"):
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC secret belum ditetapkan")
    request = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": tags,
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"ntfy gagal: HTTP {response.status}")
        print("NTFY_SENT:", response.status)


def fetch_candles():
    request = urllib.request.Request(
        API, headers={"User-Agent": "MCD-SIGNAL-V1/1.0"}
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))

    points = payload.get("points")
    if not isinstance(points, list):
        raise RuntimeError("API tidak menyediakan points")

    unique = {}
    for point in points:
        try:
            candle = {
                "t": int(point["t"]),
                "o": float(point["o"]),
                "h": float(point["h"]),
                "l": float(point["l"]),
                "c": float(point["c"]),
            }
            if (
                candle["h"] >= max(candle["o"], candle["c"], candle["l"])
                and candle["l"] <= min(candle["o"], candle["c"], candle["h"])
            ):
                unique[candle["t"]] = candle
        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    return [unique[t] for t in sorted(unique)]


def fetch_interval_candles(interval, range_value):
    """Fetch higher-timeframe candles; UNKNOWN is used if feed rejects interval."""
    url = f"https://xaus.com/api/v1/chart?symbol=xau&range={range_value}&interval={interval}"
    request = urllib.request.Request(url, headers={"User-Agent": "MCD-SIGNAL-V1/1.0"})
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))
    points = payload.get("points")
    if not isinstance(points, list):
        raise RuntimeError(f"API tiada points untuk {interval}")
    unique = {}
    for point in points:
        try:
            c = {"t": int(point["t"]), "o": float(point["o"]), "h": float(point["h"]),
                 "l": float(point["l"]), "c": float(point["c"])}
            if c["h"] >= max(c["o"], c["c"], c["l"]) and c["l"] <= min(c["o"], c["c"], c["h"]):
                unique[c["t"]] = c
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return [unique[t] for t in sorted(unique)]

def trend_45m(candles):
    """Directional body of the last nine closed M5 candles (45 minutes)."""
    if len(candles) < 9:
        return "UNKNOWN"
    window = candles[-9:]
    start_open, end_close = window[0]["o"], window[-1]["c"]
    if end_close > start_open:
        return "UPTREND"
    if end_close < start_open:
        return "DOWNTREND"
    return "FLAT"

def ema(values, period):
    if len(values) < period:
        raise RuntimeError(f"Candle tidak cukup untuk EMA{period}")
    alpha = 2.0 / (period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def atr(candles, period=ATR_LEN):
    true_ranges = []
    for index in range(1, len(candles)):
        current, previous = candles[index], candles[index - 1]
        true_ranges.append(max(
            current["h"] - current["l"],
            abs(current["h"] - previous["c"]),
            abs(current["l"] - previous["c"]),
        ))
    if len(true_ranges) < period:
        raise RuntimeError("Candle tidak cukup untuk ATR")
    value = sum(true_ranges[:period]) / period
    for item in true_ranges[period:]:
        value = (value * (period - 1) + item) / period
    return value


def aggregate(candles, minutes):
    """Aggregate aligned, complete M5 candles into a higher timeframe."""
    seconds = minutes * 60
    groups = {}
    for candle in candles:
        bucket = (candle["t"] // seconds) * seconds
        groups.setdefault(bucket, []).append(candle)

    output = []
    for start in sorted(groups):
        group = sorted(groups[start], key=lambda item: item["t"])
        expected_count = minutes // 5
        expected_times = [start + 300 * i for i in range(expected_count)]
        times = [item["t"] for item in group]
        # Avoid HTF bars assembled from missing M5 candles.
        if times != expected_times:
            continue
        output.append({
            "t": start,
            "o": group[0]["o"],
            "h": max(item["h"] for item in group),
            "l": min(item["l"] for item in group),
            "c": group[-1]["c"],
        })
    return output


def trend(candles):
    if len(candles) < 60:
        return "UNKNOWN"
    closes = [item["c"] for item in candles]
    fast = ema(closes, EMA_FAST)[-1]
    slow = ema(closes, EMA_SLOW)[-1]
    if fast > slow:
        return "BULLISH"
    if fast < slow:
        return "BEARISH"
    return "FLAT"


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as file:
            value = json.load(file)
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(value, file, indent=2, ensure_ascii=False)
    os.replace(temporary, path)


def market_closed(now):
    # Approximate OTC gold schedule in UTC; broker holidays/DST may differ.
    weekday = now.weekday()
    minute_of_day = now.hour * 60 + now.minute
    return (
        weekday == 5
        or (weekday == 6 and minute_of_day < 22 * 60)
        or (weekday == 4 and minute_of_day >= 21 * 60)
    )


def quality_label(score):
    if score <= 3:
        return "WAIT"
    if score == 4:
        return "RISIKO"
    if score <= 6:
        return "RENDAH (C)"
    if score == 7:
        return "SEDERHANA (B)"
    return "KUAT (A)"


def evaluate(candles):
    if len(candles) < 60:
        raise RuntimeError("Candle M5 tidak mencukupi")
    closes = [item["c"] for item in candles]
    last, previous = candles[-1], candles[-2]
    atr_value = atr(candles)
    if not atr_value or atr_value <= 0:
        raise RuntimeError("ATR tidak sah")

    ema_fast = ema(closes, EMA_FAST)
    ema_slow = ema(closes, EMA_SLOW)
    higher = {
        "h1": trend(aggregate(candles, 60)),
        "m30": trend(aggregate(candles, 30)),
        "m15": trend(aggregate(candles, 15)),
        "h4": "UNKNOWN",
        "d1": "UNKNOWN",
    }
    # Separate interval requests provide enough history for D1/H4 EMA trend.
    try:
        higher["h4"] = trend(fetch_interval_candles("4h", "30d"))
    except Exception as exc:
        print("H4_TREND_UNAVAILABLE:", str(exc))
    try:
        higher["d1"] = trend(fetch_interval_candles("1d", "90d"))
    except Exception as exc:
        print("D1_TREND_UNAVAILABLE:", str(exc))

    recent_20 = candles[-21:-1]
    recent_10 = candles[-11:-1]
    recent_30 = candles[-31:-1]
    range_high = max(item["h"] for item in recent_20)
    range_low = min(item["l"] for item in recent_20)
    prior_low = min(item["l"] for item in recent_10)
    prior_high = max(item["h"] for item in recent_10)
    support = min(item["l"] for item in recent_30)
    resistance = max(item["h"] for item in recent_30)

    bullish = last["c"] > last["o"]
    bearish = last["c"] < last["o"]

    # Technique 1: SnR rejection. Require price to approach a prior swing.
    snr_buy = last["l"] <= support + atr_value * 0.25 and last["c"] > support and bullish
    snr_sell = last["h"] >= resistance - atr_value * 0.25 and last["c"] < resistance and bearish

    # Technique 2: liquidity sweep and close back inside prior range.
    sweep_buy = last["l"] < prior_low and last["c"] > prior_low and bullish
    sweep_sell = last["h"] > prior_high and last["c"] < prior_high and bearish

    # Technique 3: engulfing candle.
    bullish_engulf = (
        bullish and previous["c"] < previous["o"]
        and last["o"] <= previous["c"] and last["c"] >= previous["o"]
    )
    bearish_engulf = (
        bearish and previous["c"] > previous["o"]
        and last["o"] >= previous["c"] and last["c"] <= previous["o"]
    )
    pa_buy = bullish_engulf and last["c"] >= support
    pa_sell = bearish_engulf and last["c"] <= resistance

    # Technique 4: close-confirmed breakout of preceding 20 candles.
    breakout_buy = last["c"] > range_high and previous["c"] <= range_high
    breakout_sell = last["c"] < range_low and previous["c"] >= range_low

    buy_flags = {
        "SnR Rejection": snr_buy,
        "Liquidity Sweep": sweep_buy,
        "Engulfing": pa_buy,
        "Breakout": breakout_buy,
    }
    sell_flags = {
        "SnR Rejection": snr_sell,
        "Liquidity Sweep": sweep_sell,
        "Engulfing": pa_sell,
        "Breakout": breakout_sell,
    }
    buy_setup = any(buy_flags.values())
    sell_setup = any(sell_flags.values())

    body_ok = abs(last["c"] - last["o"]) >= atr_value * 0.10
    extension_ok = abs(last["c"] - ema_fast[-1]) <= atr_value * 2.0
    momentum_buy = ema_fast[-1] > ema_slow[-1] and bullish
    momentum_sell = ema_fast[-1] < ema_slow[-1] and bearish
    bullish_htf = sum(value == "BULLISH" for value in higher.values())
    bearish_htf = sum(value == "BEARISH" for value in higher.values())

    # Eight transparent points: price vs EMA50, EMA momentum, proximity to
    # swing, technique confirmation, breakout, candle body, extension and HTF.
    buy_score = (
        int(last["c"] > ema_slow[-1])
        + int(momentum_buy)
        + int(abs(last["c"] - support) <= atr_value)
        + int(sweep_buy or bullish_engulf or snr_buy)
        + int(breakout_buy)
        + int(body_ok)
        + int(extension_ok)
        + int(bullish_htf > 0)
        - int(bullish_htf == 0)
    )
    sell_score = (
        int(last["c"] < ema_slow[-1])
        + int(momentum_sell)
        + int(abs(last["c"] - resistance) <= atr_value)
        + int(sweep_sell or bearish_engulf or snr_sell)
        + int(breakout_sell)
        + int(body_ok)
        + int(extension_ok)
        + int(bearish_htf > 0)
        - int(bearish_htf == 0)
    )
    buy_score = max(0, min(8, buy_score))
    sell_score = max(0, min(8, sell_score))

    side, score, strategy = "WAIT", max(buy_score, sell_score), "None"
    if buy_setup and buy_score >= MIN_SCORE and body_ok and extension_ok and not sell_setup:
        side, score = "BUY", buy_score
        strategy = next(name for name, active in buy_flags.items() if active)
    elif sell_setup and sell_score >= MIN_SCORE and body_ok and extension_ok and not buy_setup:
        side, score = "SELL", sell_score
        strategy = next(name for name, active in sell_flags.items() if active)

    # Nearest observed support/resistance around current price from recent M5 candles.
    supports_below = [item["l"] for item in candles[-100:] if item["l"] < last["c"]]
    resistances_above = [item["h"] for item in candles[-100:] if item["h"] > last["c"]]
    nearest_support = max(supports_below) if supports_below else None
    nearest_resistance = min(resistances_above) if resistances_above else None

    result = {
        "side": side,
        "score": score,
        "support": nearest_support,
        "resistance": nearest_resistance,
        "quality": quality_label(score),
        "strategy": strategy,
        "atr": atr_value,
        "trend": higher,
        "trend_45m": trend_45m(candles),
        "candle": last,
        "techniques": {
            "snr": bool(snr_buy or snr_sell),
            "smc": bool(sweep_buy or sweep_sell),
            "pa": bool(pa_buy or pa_sell),
            "breakout": bool(breakout_buy or breakout_sell),
        },
        "buy_score": buy_score,
        "sell_score": sell_score,
    }

    if side in ("BUY", "SELL"):
        entry = last["c"]
        candle_range = last["h"] - last["l"]
        # SL is at least 1.5 ATR away; candle buffer may widen it.
        distance = max(atr_value * 1.5, candle_range * 1.2)
        if side == "BUY":
            sl = min(last["l"] - atr_value * 0.15, entry - distance)
            risk = entry - sl
            tp1 = entry + risk * RR_TP1
        else:
            sl = max(last["h"] + atr_value * 0.15, entry + distance)
            risk = sl - entry
            tp1 = entry - risk * RR_TP1
        half_zone = atr_value * ZONE_HALF_WIDTH_ATR
        result.update({
            "entry": entry,
            "zone_low": entry - half_zone,
            "zone_high": entry + half_zone,
            "sl": sl,
            "tp1": tp1,
            "risk_distance": risk,
        })
    else:
        result.update({
            "entry": None, "zone_low": None, "zone_high": None,
            "sl": None, "tp1": None, "risk_distance": None,
        })
    return result


def publish_status(state):
    """Write a Pages-readable JSON snapshot; no secrets are included."""
    write_json(STATUS_PATH, state)


def main():
    now = utc_now()
    state = read_json(STATE_PATH)
    public = {
        "version": "V1",
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "updated_utc": now.isoformat(),
        "data_status": "NOT FRESH",
        "signal": "WAIT",
        "score": 0,
        "quality": "WAIT",
        "reason": "Menunggu data sah.",
        "trend": {"d1": "UNKNOWN", "h4": "UNKNOWN", "h1": "UNKNOWN", "m30": "UNKNOWN", "m15": "UNKNOWN"},
        "support": None,
        "resistance": None,
        "trend_45m": "UNKNOWN",
        "trend_m5": "UNKNOWN",
        "techniques": {"snr": False, "smc": False, "pa": False, "breakout": False},
    }

    if market_closed(now):
        public["reason"] = "Pasaran dianggarkan tutup; jadual broker boleh berbeza."
        state["last_status"] = "MARKET_CLOSED"
        state["last_error"] = None
        state["last_run_utc"] = now.isoformat()
        publish_status(public)
        write_json(STATE_PATH, state)
        print("MARKET_CLOSED; no signal evaluation.")
        return

    try:
        candles = fetch_candles()
        current_ts = int(now.timestamp())
        closed = [item for item in candles if item["t"] + 300 <= current_ts]
        if len(closed) < MIN_M5_CANDLES:
            raise RuntimeError(f"Candle tidak cukup: {len(closed)}")

        last = closed[-1]
        age = current_ts - (last["t"] + 300)
        if age < 0 or age > MAX_AGE_SECONDS:
            raise RuntimeError(f"Candle lapuk: {max(0, age) // 60} minit")
        if len(closed) >= 2 and last["t"] - closed[-2]["t"] > 600:
            raise RuntimeError("Jurang candle M5 terlalu besar")

        result = evaluate(closed[-1000:])
        public.update({
            "data_status": "FRESH",
            "last_candle_utc": datetime.fromtimestamp(last["t"] + 300, timezone.utc).isoformat(),
            "candle_age_seconds": age,
            "price": last["c"],
            "ema20": ema([item["c"] for item in closed[-1000:]], EMA_FAST)[-1],
            "ema50": ema([item["c"] for item in closed[-1000:]], EMA_SLOW)[-1],
            "atr14": result["atr"],
            "support": result["support"],
            "resistance": result["resistance"],
            "signal": result["side"],
            "score": result["score"],
            "quality": result["quality"],
            "strategy": result["strategy"],
            "trend": result["trend"],
            "trend_45m": result["trend_45m"],
            "trend_m5": result["trend_45m"],
            "techniques": result["techniques"],
            "buy_score": result["buy_score"],
            "sell_score": result["sell_score"],
            "reason": "Data candle M5 lulus semakan kesegaran.",
        })
        if result["side"] in ("BUY", "SELL"):
            for key in ("entry", "zone_low", "zone_high", "sl", "tp1", "tp2", "risk_distance"):
                public[key] = result[key]

            key = f"{result['side']}-{last['t']}"
            if state.get("last_signal_key") != key:
                side = result["side"]
                trend = result["trend"]
                msg = (
                    f"MCD SIGNAL — {side}\n"
                    "Symbol: XAUUSD | Timeframe: M5\n"
                    "Status: ENTRY ZONE AKTIF\n"
                    f"Zone: {fmt(result['zone_low'])} – {fmt(result['zone_high'])}\n"
                    f"Entry rujukan: {fmt(result['entry'])}\n"
                    f"SL: {fmt(result['sl'])}\n"
                    f"TP: {fmt(result['tp1'])} (2R)\n"
                    f"Quality: {result['score']}/8 — {result['quality']}\n"
                    f"Trend D1/H4/H1/M30/M15: {trend['d1']}/{trend['h4']}/{trend['h1']}/{trend['m30']}/{trend['m15']}\n"
                    f"Setup: {result['strategy']}\n"
                    "Data: FRESH\n"
                    "Eksperimen sahaja; sahkan harga dan spread HFM. Tiada auto-trading."
                )
                notify(
                    f"MCD SIGNAL — {side}",
                    msg,
                    "high",
                    "chart_with_upwards_trend" if side == "BUY" else "chart_with_downwards_trend",
                )
                state["last_signal_key"] = key
                print("SIGNAL_SENT:", key)
            else:
                print("DUPLICATE_SIGNAL; notification skipped.")
        else:
            public["reason"] = (
                f"WAIT — tiada setup sah. BUY {result['buy_score']}/8, "
                f"SELL {result['sell_score']}/8."
            )
            print("DATA_VALID; WAIT:", public["reason"])

        state["last_status"] = "DATA_VALID"
        state["last_error"] = None
        state["last_candle_t"] = last["t"]
        state["last_run_utc"] = now.isoformat()
    except Exception as exc:
        public["data_status"] = "NOT FRESH"
        public["signal"] = "WAIT"
        public["reason"] = str(exc)
        state["last_status"] = "DATA_ERROR"
        state["last_error"] = str(exc)
        state["last_run_utc"] = now.isoformat()
        print("WAIT / DATA_ERROR:", str(exc))

    publish_status(public)
    write_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
