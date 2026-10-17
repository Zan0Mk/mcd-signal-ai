
#!/usr/bin/env python3
import json
import os
import urllib.request
from datetime import datetime, timezone

API = "https://xaus.com/api/v1/chart?symbol=xau&range=5d&interval=5m"
STATE_PATH = ".mcd_signal_state.json"
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

MAX_AGE_SECONDS = 900
MIN_CANDLES = 60
EMA_FAST = 20
EMA_SLOW = 50
ATR_LEN = 14
MIN_SCORE = 4
RR_TP1 = 1.0
RR_TP2 = 2.0


def now_ts():
    return int(datetime.now(timezone.utc).timestamp())


def notify(title, message, priority="default", tags="chart"):
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC secret belum ditetapkan")

    req = urllib.request.Request(
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
    with urllib.request.urlopen(req, timeout=20) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"ntfy gagal: HTTP {response.status}")
        print("NTFY_SENT:", response.status)


def fetch_candles():
    req = urllib.request.Request(
        API,
        headers={"User-Agent": "MCD-Signal-AI/2.0"},
    )
    with urllib.request.urlopen(req, timeout=25) as response:
        obj = json.loads(response.read().decode("utf-8"))

    points = obj.get("points")
    if not isinstance(points, list):
        raise RuntimeError("API tidak menyediakan points")

    candles = []
    for p in points:
        try:
            c = {
                "t": int(p["t"]),
                "o": float(p["o"]),
                "h": float(p["h"]),
                "l": float(p["l"]),
                "c": float(p["c"]),
            }
            if c["h"] >= max(c["o"], c["c"], c["l"]) and \
               c["l"] <= min(c["o"], c["c"], c["h"]):
                candles.append(c)
        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["t"])

    # Buang timestamp pendua.
    unique = {}
    for c in candles:
        unique[c["t"]] = c

    return list(unique.values())


def ema(values, period):
    if len(values) < period:
        raise RuntimeError(f"Candle tidak cukup untuk EMA{period}")

    alpha = 2.0 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def atr(candles, period=14):
    tr = []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        tr.append(max(
            cur["h"] - cur["l"],
            abs(cur["h"] - prev["c"]),
            abs(cur["l"] - prev["c"]),
        ))

    if len(tr) < period:
        raise RuntimeError("Candle tidak cukup untuk ATR")

    value = sum(tr[:period]) / period
    for x in tr[period:]:
        value = (value * (period - 1) + x) / period
    return value


def aggregate(candles, minutes):
    """Agregat candle M5 menjadi M15, M30 atau H1 mengikut UTC."""
    bucket_seconds = minutes * 60
    groups = {}

    for c in candles:
        bucket = (c["t"] // bucket_seconds) * bucket_seconds
        groups.setdefault(bucket, []).append(c)

    result = []
    for t in sorted(groups):
        group = sorted(groups[t], key=lambda x: x["t"])

        # Gunakan candle HTF yang lengkap sahaja.
        if group[0]["t"] != t:
            continue
        if group[-1]["t"] + 300 < t + bucket_seconds:
            continue

        result.append({
            "t": t,
            "o": group[0]["o"],
            "h": max(x["h"] for x in group),
            "l": min(x["l"] for x in group),
            "c": group[-1]["c"],
        })

    return result


def trend(candles):
    closes = [c["c"] for c in candles]
    if len(closes) < 60:
        return "UNKNOWN"

    fast = ema(closes, EMA_FAST)[-1]
    slow = ema(closes, EMA_SLOW)[-1]

    if fast > slow:
        return "UP"
    if fast < slow:
        return "DOWN"
    return "FLAT"


def read_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def market_closed(now):
    # Anggaran waktu pasaran emas OTC dalam UTC.
    # Jadual broker, cuti dan DST boleh berbeza.
    weekday = now.weekday()
    minutes = now.hour * 60 + now.minute

    return (
        weekday == 5
        or (weekday == 6 and minutes < 22 * 60)
        or (weekday == 4 and minutes >= 21 * 60)
    )


def evaluate(candles):
    m5 = candles
    closes = [c["c"] for c in m5]
    last = m5[-1]
    prev = m5[-2]

    a = atr(m5, ATR_LEN)
    ef = ema(closes, EMA_FAST)
    es = ema(closes, EMA_SLOW)

    if a <= 0:
        raise RuntimeError("ATR tidak sah")

    # Trend timeframe lebih tinggi.
    h1 = trend(aggregate(m5, 60))
    m30 = trend(aggregate(m5, 30))
    m15 = trend(aggregate(m5, 15))

    # Rujukan struktur menggunakan candle terdahulu.
    lookback = m5[-21:-1]
    range_high = max(c["h"] for c in lookback)
    range_low = min(c["l"] for c in lookback)

    prior = m5[-11:-1]
    prev_low = min(c["l"] for c in prior)
    prev_high = max(c["h"] for c in prior)

    # Sokongan/rintangan ringkas daripada swing terkini.
    support = min(c["l"] for c in m5[-30:-1])
    resistance = max(c["h"] for c in m5[-30:-1])

    bull = last["c"] > last["o"]
    bear = last["c"] < last["o"]

    # Strategi 1: SnR rejection.
    snr_buy = (
        last["l"] <= support + a * 0.25
        and last["c"] > support and bull
    )
    snr_sell = (
        last["h"] >= resistance - a * 0.25
        and last["c"] < resistance and bear
    )

    # Strategi 2: liquidity sweep.
    smc_buy = last["l"] < prev_low and last["c"] > prev_low and bull
    smc_sell = last["h"] > prev_high and last["c"] < prev_high and bear

    # Strategi 3: bullish/bearish engulfing.
    bull_engulf = (
        bull and prev["c"] < prev["o"]
        and last["o"] <= prev["c"]
        and last["c"] >= prev["o"]
    )
    bear_engulf = (
        bear and prev["c"] > prev["o"]
        and last["o"] >= prev["c"]
        and last["c"] <= prev["o"]
    )

    pa_buy = bull_engulf and last["c"] >= support
    pa_sell = bear_engulf and last["c"] <= resistance

    # Strategi 4: breakout.
    bo_buy = last["c"] > range_high and prev["c"] <= range_high
    bo_sell = last["c"] < range_low and prev["c"] >= range_low

    buy_setup = snr_buy or smc_buy or pa_buy or bo_buy
    sell_setup = snr_sell or smc_sell or pa_sell or bo_sell

    # Skor maksimum 8, berdasarkan struktur dan konfluens.
    body_ok = abs(last["c"] - last["o"]) >= a * 0.10
    extension_ok = abs(last["c"] - ef[-1]) <= a * 2.0
    momentum_buy = ef[-1] > es[-1] and bull
    momentum_sell = ef[-1] < es[-1] and bear

    htf_score_buy = sum(x == "UP" for x in (h1, m30, m15))
    htf_score_sell = sum(x == "DOWN" for x in (h1, m30, m15))

    buy_score = (
        int(last["c"] > es[-1])
        + int(momentum_buy)
        + int(abs(last["c"] - support) <= a)
        + int(bull_engulf or smc_buy or snr_buy)
        + int(last["c"] > range_high)
        + int(body_ok)
        + int(extension_ok)
        + int(htf_score_buy > 0)
    )

    sell_score = (
        int(last["c"] < es[-1])
        + int(momentum_sell)
        + int(abs(last["c"] - resistance) <= a)
        + int(bear_engulf or smc_sell or snr_sell)
        + int(last["c"] < range_low)
        + int(body_ok)
        + int(extension_ok)
        + int(htf_score_sell > 0)
    )

    # Sejajar dengan tetapan Balanced dan benarkan counter-trend
    # dengan penalti satu mata.
    buy_score -= int(htf_score_buy == 0)
    sell_score -= int(htf_score_sell == 0)

    side = None
    score = 0
    strategy = "None"

    if (
        buy_setup and buy_score >= MIN_SCORE
        and body_ok and extension_ok and not sell_setup
    ):
        side, score = "BUY", buy_score
        strategy = (
            "SnR" if snr_buy else
            "Liquidity Sweep" if smc_buy else
            "Price Action" if pa_buy else "Breakout"
        )
    elif (
        sell_setup and sell_score >= MIN_SCORE
        and body_ok and extension_ok and not buy_setup
    ):
        side, score = "SELL", sell_score
        strategy = (
            "SnR" if snr_sell else
            "Liquidity Sweep" if smc_sell else
            "Price Action" if pa_sell else "Breakout"
        )

    # SL mengambil kira ATR dan julat candle signal.
    if side == "BUY":
        entry = last["c"]
        distance = max(a * 1.5, (last["h"] - last["l"]) * 1.2)
        sl = min(last["l"] - a * 0.15, entry - distance)
        risk = entry - sl
        tp1 = entry + risk * RR_TP1
        tp2 = entry + risk * RR_TP2
    elif side == "SELL":
        entry = last["c"]
        distance = max(a * 1.5, (last["h"] - last["l"]) * 1.2)
        sl = max(last["h"] + a * 0.15, entry + distance)
        risk = sl - entry
        tp1 = entry - risk * RR_TP1
        tp2 = entry - risk * RR_TP2
    else:
        entry = sl = tp1 = tp2 = None

    return {
        "side": side,
        "score": score,
        "strategy": strategy,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "atr": a,
        "h1": h1,
        "m30": m30,
        "m15": m15,
        "candle": last,
    }


def main():
    now = datetime.now(timezone.utc)
    state = read_state()

    if market_closed(now):
        print("MARKET_CLOSED; no signal evaluation.")
        state["last_status"] = "MARKET_CLOSED"
        write_state(state)
        return

    try:
        candles = fetch_candles()
        current_ts = int(now.timestamp())

        closed = [
            c for c in candles
            if c["t"] + 300 <= current_ts
        ]

        if len(closed) < MIN_CANDLES:
            raise RuntimeError(
                f"Candle tidak cukup: {len(closed)}"
            )

        last = closed[-1]
        age = current_ts - (last["t"] + 300)

        if age < 0 or age > MAX_AGE_SECONDS:
            raise RuntimeError(
                f"Candle lapuk: {max(0, age) // 60} minit"
            )

        # Pastikan tiada jurang candle terkini yang besar.
        if len(closed) >= 2 and last["t"] - closed[-2]["t"] > 600:
            raise RuntimeError("Jurang candle M5 terlalu besar")

        result = evaluate(closed[-1000:])
        state["last_status"] = "DATA_VALID"
        state["last_error"] = None

        side = result["side"]
        candle_id = str(last["t"])

        if side:
            key = f"{side}-{candle_id}"

            if state.get("last_signal_key") != key:
                fmt = lambda x: f"{x:.2f}"
                msg = (
                    f"XAUUSD M5 | {side}\n"
                    f"Strategi: {result['strategy']}\n"
                    f"Skor: {result['score']}/8\n"
                    f"Entry: {fmt(result['entry'])}\n"
                    f"SL: {fmt(result['sl'])}\n"
                    f"TP1: {fmt(result['tp1'])}\n"
                    f"TP2: {fmt(result['tp2'])}\n"
                    f"Trend H1/M30/M15: "
                    f"{result['h1']}/{result['m30']}/{result['m15']}\n"
                    f"ATR14: {fmt(result['atr'])}\n"
                    f"Candle UTC: "
                    f"{datetime.fromtimestamp(last['t'] + 300, timezone.utc):%Y-%m-%d %H:%M}\n"
                    "Eksperimen; semak harga dan spread HFM. "
                    "Tiada auto-trading."
                )

                notify(
                    f"MCD SIGNAL — {side}",
                    msg,
                    "high",
                    "chart_with_upwards_trend"
                    if side == "BUY"
                    else "chart_with_downwards_trend",
                )

                state["last_signal_key"] = key
                print("SIGNAL_SENT:", key)
            else:
                print("DUPLICATE_SIGNAL; notification skipped.")
        else:
            print(
                "DATA_VALID; WAIT. "
                f"Trend={result['h1']}/{result['m30']}/{result['m15']}"
            )

        state["last_candle_t"] = last["t"]
        write_state(state)

    except Exception as exc:
        print("WAIT / DATA_ERROR:", str(exc))
        state["last_status"] = "DATA_ERROR"
        state["last_error"] = str(exc)
        write_state(state)


if __name__ == "__main__":
    main()
