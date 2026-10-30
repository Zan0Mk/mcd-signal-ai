
#!/usr/bin/env python3
"""MCD SIGNAL V3 - XAUUSD M5 | Manual execution | One SL + One TP."""

import json
import math
import os
import statistics
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = "https://biquote.io/api"
TD_BASE = "https://api.twelvedata.com"
TD_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
TD_SYMBOL = os.getenv("TWELVE_DATA_SYMBOL", "XAU/USD").strip()

SYMBOL = "XAUUSD"
STATE = ".mcd_signal_state.json"
STATUS = "signal_status.json"
TOPIC = os.getenv("NTFY_TOPIC", "").strip()
SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

CALENDAR_URL = "https://www.financecalendar.com/wp-json/fc/v1/calendar"
CALENDAR_PAGE = "https://www.financecalendar.com/api/economic-calendar/"

MIN_BARS = 100
MAX_CANDLE_AGE = 900
MAX_QUOTE_AGE = 300
MAX_ENTRY_DRIFT_ATR = 1.2
MAX_HISTORY = 500

TP_R = 1.5
MIN_SCORE = 6
MIN_VOTES = 3

MYT = timezone(timedelta(hours=8))


# ------------------------------------------------------------
# UTILITIES
# ------------------------------------------------------------

def now():
    return datetime.now(timezone.utc)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def fmt(value):
    return f"{value:.2f}" if finite(value) else "N/A"


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "MCD-SIGNAL/3.0"}
    )
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
        json.dump(data, f, indent=2, ensure_ascii=False, allow_nan=False)
    os.replace(tmp, path)


# ------------------------------------------------------------
# QUOTE - BIQUOTE
# ------------------------------------------------------------

def get_quote():
    data = get_json(f"{BASE}/{SYMBOL}?allowStale=false")

    try:
        bid = float(data["bid"])
        ask = float(data["ask"])
        mid = float(data["mid"])
        stamp = datetime.fromisoformat(
            str(data["timestamp"]).replace("Z", "+00:00")
        )
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Quote tidak lengkap: {exc}") from exc

    if not all(finite(x) and x > 0 for x in (bid, ask, mid)):
        raise RuntimeError("Harga quote tidak sah")
    if ask < bid:
        raise RuntimeError("Ask lebih rendah daripada Bid")

    age = int(now().timestamp() - stamp.timestamp())
    if age < -60 or age > MAX_QUOTE_AGE:
        raise RuntimeError(f"Quote lapuk ({age}s)")

    if str(data.get("marketState", "")).lower() != "open":
        raise RuntimeError("Pasaran bukan OPEN")

    spread = float(data.get("spread", ask - bid))
    if not finite(spread) or spread < 0:
        spread = ask - bid

    return {
        "bid": bid, "ask": ask, "mid": mid,
        "spread": spread,
        "timestamp": stamp.astimezone(timezone.utc).isoformat(),
        "age": max(0, age),
    }


# ------------------------------------------------------------
# TWELVE DATA OHLC
# ------------------------------------------------------------

def get_bars(interval="5m", limit=1000):
    if not TD_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY belum ditetapkan")

    intervals = {
        "5m": ("5min", 300),
        "15m": ("15min", 900),
        "30m": ("30min", 1800),
        "1h": ("1h", 3600),
        "4h": ("4h", 14400),
        "1d": ("1day", 86400),
    }
    if interval not in intervals:
        raise RuntimeError(f"Interval tidak disokong: {interval}")

    td_interval, duration = intervals[interval]
    params = urllib.parse.urlencode({
        "symbol": TD_SYMBOL,
        "interval": td_interval,
        "outputsize": min(max(int(limit), 1), 5000),
        "timezone": "UTC",
        "apikey": TD_KEY,
    })

    payload = get_json(f"{TD_BASE}/time_series?{params}")
    if not isinstance(payload, dict) or payload.get("status") == "error":
        raise RuntimeError(
            f"Twelve Data: {payload.get('message', 'respons tidak sah')}"
        )

    values = payload.get("values")
    if not isinstance(values, list):
        raise RuntimeError("Twelve Data tiada data OHLC")

    unique = {}
    epoch = int(now().timestamp())

    for item in values:
        try:
            stamp = datetime.fromisoformat(
                str(item["datetime"]).replace("Z", "+00:00")
            )
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)

            t = int(stamp.timestamp())
            candle = {
                "t": t,
                "o": float(item["open"]),
                "h": float(item["high"]),
                "l": float(item["low"]),
                "c": float(item["close"]),
                "v": max(0.0, float(item.get("volume") or 0)),
                "is_open": t + duration > epoch,
            }

            prices = [candle[k] for k in ("o", "h", "l", "c")]
            if not all(finite(x) and x > 0 for x in prices):
                continue
            if candle["h"] < max(prices) or candle["l"] > min(prices):
                continue

            unique[t] = candle
        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    return [unique[t] for t in sorted(unique)]


def fetch_candles(interval, limit):
    return get_bars(interval, limit), "Twelve Data"


# ------------------------------------------------------------
# INDICATORS
# ------------------------------------------------------------

def ema(values, period):
    if len(values) < period:
        raise RuntimeError(f"Data tidak cukup untuk EMA{period}")
    alpha = 2.0 / (period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1 - alpha) * result[-1])
    return result


def atr_series(candles, period=14):
    tr = []
    for i in range(1, len(candles)):
        c = candles[i]
        pc = candles[i - 1]["c"]
        tr.append(max(
            c["h"] - c["l"],
            abs(c["h"] - pc),
            abs(c["l"] - pc)
        ))

    if len(tr) < period:
        raise RuntimeError("Data tidak cukup untuk ATR")

    result = [None] * period
    value = sum(tr[:period]) / period
    result.append(value)

    for x in tr[period:]:
        value = (value * (period - 1) + x) / period
        result.append(value)

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        raise RuntimeError("Data tidak cukup untuk RSI")

    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def macd(values):
    fast = ema(values, 12)
    slow = ema(values, 26)
    line = [fast[i] - slow[i] for i in range(len(values))]
    signal = ema(line, 9)
    hist = [line[i] - signal[i] for i in range(len(line))]
    return line, signal, hist


def session_vwap(candles):
    session_date = datetime.fromtimestamp(
        candles[-1]["t"], timezone.utc
    ).astimezone(MYT).date()

    session = [
        c for c in candles
        if datetime.fromtimestamp(
            c["t"], timezone.utc
        ).astimezone(MYT).date() == session_date
    ]

    if not session:
        raise RuntimeError("Session VWAP tidak tersedia")

    volume = sum(c.get("v", 0) for c in session)

    if volume > 0:
        value = sum(
            ((c["h"] + c["l"] + c["c"]) / 3) * c["v"]
            for c in session
        ) / volume
        return value, "VOLUME_WEIGHTED"

    # Fallback is explicitly not represented as true VWAP.
    value = sum(
        (c["h"] + c["l"] + c["c"]) / 3 for c in session
    ) / len(session)

    return value, "EQUAL_WEIGHT_FALLBACK"


# ------------------------------------------------------------
# HIGHER TIMEFRAME TREND
# ------------------------------------------------------------

def get_trend(interval):
    candles, _ = fetch_candles(interval, 120)
    candles = [c for c in candles if not c["is_open"]]

    if len(candles) < 60:
        return "UNKNOWN"

    closes = [c["c"] for c in candles]
    fast = ema(closes, 20)[-1]
    slow = ema(closes, 50)[-1]

    return (
        "BULLISH" if fast > slow
        else "BEARISH" if fast < slow
        else "FLAT"
    )


# ------------------------------------------------------------
# SIGNAL ENGINE
# ------------------------------------------------------------

def evaluate(candles, trends):
    if len(candles) < MIN_BARS:
        raise RuntimeError("Candle M5 tidak mencukupi")

    closes = [c["c"] for c in candles]
    cur, prev = candles[-1], candles[-2]
    price = cur["c"]

    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    atrs = atr_series(candles, 14)
    atr14 = atrs[-1]
    rsi14 = rsi(closes, 14)
    macd_line, macd_sig, macd_hist = macd(closes)
    vwap, vwap_method = session_vwap(candles)

    # MA cross and direction
    ma_bull = e9[-1] > e21[-1]
    ma_bear = e9[-1] < e21[-1]
    prev_bull = e9[-2] > e21[-2]
    prev_bear = e9[-2] < e21[-2]

    if ma_bull and not prev_bull:
        ma_cross = "BULLISH CROSS"
    elif ma_bear and not prev_bear:
        ma_cross = "BEARISH CROSS"
    elif ma_bull:
        ma_cross = "BULLISH"
    elif ma_bear:
        ma_cross = "BEARISH"
    else:
        ma_cross = "NEUTRAL"

    # VWAP
    vwap_bull = price > vwap
    vwap_bear = price < vwap
    vwap_bias = "BULLISH" if vwap_bull else "BEARISH" if vwap_bear else "NEUTRAL"

    # RSI
    rsi_bull = rsi14 >= 52
    rsi_bear = rsi14 <= 48
    rsi_bias = "BULLISH" if rsi_bull else "BEARISH" if rsi_bear else "NEUTRAL"

    # MACD
    macd_bull = macd_line[-1] > macd_sig[-1] and macd_hist[-1] > 0
    macd_bear = macd_line[-1] < macd_sig[-1] and macd_hist[-1] < 0
    macd_bias = "BULLISH" if macd_bull else "BEARISH" if macd_bear else "NEUTRAL"

    # ATR filter against median of recent ATR values
    recent_atrs = [x for x in atrs[-51:] if finite(x) and x > 0]
    median_atr = statistics.median(recent_atrs) if recent_atrs else atr14
    atr_ratio = atr14 / median_atr if median_atr else 1.0
    atr_pass = 0.55 <= atr_ratio <= 2.50

    # Price action / structure
    high20 = max(c["h"] for c in candles[-21:-1])
    low20 = min(c["l"] for c in candles[-21:-1])
    high10 = max(c["h"] for c in candles[-11:-1])
    low10 = min(c["l"] for c in candles[-11:-1])
    support30 = min(c["l"] for c in candles[-31:-1])
    resistance30 = max(c["h"] for c in candles[-31:-1])

    bull = price > cur["o"]
    bear = price < cur["o"]
    body_ok = abs(price - cur["o"]) >= atr14 * 0.10
    extension_ok = abs(price - e21[-1]) <= atr14 * 2.0

    buy_breakout = price > high20 and prev["c"] <= high20
    sell_breakout = price < low20 and prev["c"] >= low20

    buy_sweep = cur["l"] < low10 and price > low10 and bull
    sell_sweep = cur["h"] > high10 and price < high10 and bear

    buy_rejection = (
        cur["l"] <= support30 + atr14 * 0.25
        and price > support30 and bull
    )
    sell_rejection = (
        cur["h"] >= resistance30 - atr14 * 0.25
        and price < resistance30 and bear
    )

    buy_engulf = (
        bull and prev["c"] < prev["o"]
        and cur["o"] <= prev["c"] and price >= prev["o"]
    )
    sell_engulf = (
        bear and prev["c"] > prev["o"]
        and cur["o"] >= prev["c"] and price <= prev["o"]
    )

    buy_structure = any([
        buy_breakout, buy_sweep, buy_rejection, buy_engulf
    ])
    sell_structure = any([
        sell_breakout, sell_sweep, sell_rejection, sell_engulf
    ])

    bull_htf = sum(v == "BULLISH" for v in trends.values())
    bear_htf = sum(v == "BEARISH" for v in trends.values())

    buy_votes = sum([ma_bull, vwap_bull, rsi_bull, macd_bull])
    sell_votes = sum([ma_bear, vwap_bear, rsi_bear, macd_bear])

    # Ten-point directional score.
    buy_score = sum([
        ma_bull, vwap_bull, rsi_bull, macd_bull,
        bull_htf >= 1, buy_structure, buy_breakout,
        bull, body_ok, extension_ok
    ])
    sell_score = sum([
        ma_bear, vwap_bear, rsi_bear, macd_bear,
        bear_htf >= 1, sell_structure, sell_breakout,
        bear, body_ok, extension_ok
    ])

    side, score, setup = "WAIT", max(buy_score, sell_score), "NONE"

    buy_valid = (
        buy_score >= MIN_SCORE and buy_votes >= MIN_VOTES
        and buy_structure and bull and body_ok and extension_ok
        and atr_pass and not sell_structure
    )
    sell_valid = (
        sell_score >= MIN_SCORE and sell_votes >= MIN_VOTES
        and sell_structure and bear and body_ok and extension_ok
        and atr_pass and not buy_structure
    )

    if buy_valid and not sell_valid:
        side, score = "BUY", buy_score
        setup = (
            "BREAKOUT" if buy_breakout else
            "LIQUIDITY SWEEP" if buy_sweep else
            "BULLISH ENGULFING" if buy_engulf else
            "SUPPORT REJECTION"
        )
    elif sell_valid and not buy_valid:
        side, score = "SELL", sell_score
        setup = (
            "BREAKOUT" if sell_breakout else
            "LIQUIDITY SWEEP" if sell_sweep else
            "BEARISH ENGULFING" if sell_engulf else
            "RESISTANCE REJECTION"
        )

    quality = (
        "STRONG (A)" if score >= 8 else
        "MODERATE (B)" if score >= 6 else
        "LOW (C)" if score >= 5 else "WAIT"
    )

    # Confirmed swing levels from historical candles.
    support_pivots, resistance_pivots = [], []
    look = candles[-100:]

    for i in range(3, len(look) - 3):
        window = look[i - 3:i + 4]
        if (
            look[i]["l"] == min(x["l"] for x in window)
            and look[i]["l"] < price - atr14 * 0.25
        ):
            support_pivots.append(look[i]["l"])
        if (
            look[i]["h"] == max(x["h"] for x in window)
            and look[i]["h"] > price + atr14 * 0.25
        ):
            resistance_pivots.append(look[i]["h"])

    support = max(support_pivots) if support_pivots else None
    resistance = min(resistance_pivots) if resistance_pivots else None

    indicators = {
        "ma_cross": ma_cross,
        "ma_bias": "BULLISH" if ma_bull else "BEARISH" if ma_bear else "NEUTRAL",
        "ema9": e9[-1],
        "ema21": e21[-1],
        "vwap": vwap,
        "vwap_bias": vwap_bias,
        "vwap_method": vwap_method,
        "rsi14": rsi14,
        "rsi_bias": rsi_bias,
        "macd": macd_line[-1],
        "macd_signal": macd_sig[-1],
        "macd_histogram": macd_hist[-1],
        "macd_bias": macd_bias,
        "atr14": atr14,
        "atr_ratio": atr_ratio,
        "atr_filter": "PASS" if atr_pass else "BLOCK",
        "buy_votes": buy_votes,
        "sell_votes": sell_votes,
    }

    result = {
        "signal": side,
        "score": score,
        "quality": quality,
        "strategy": setup,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "atr14": atr14,
        "ema20": ema(closes, 20)[-1],
        "ema50": ema(closes, 50)[-1],
        "support": support,
        "resistance": resistance,
        "trend": trends,
        "indicators": indicators,
        "techniques": {
            "snr": bool(buy_rejection or sell_rejection),
            "smc": bool(buy_sweep or sell_sweep),
            "pa": bool(buy_engulf or sell_engulf),
            "breakout": bool(buy_breakout or sell_breakout),
        },
    }

    if side in ("BUY", "SELL"):
        entry = price
        distance = max(atr14 * 1.5, (cur["h"] - cur["l"]) * 1.2)

        if side == "BUY":
            sl = min(cur["l"] - atr14 * 0.15, entry - distance)
            risk = entry - sl
            tp = entry + risk * TP_R
        else:
            sl = max(cur["h"] + atr14 * 0.15, entry + distance)
            risk = sl - entry
            tp = entry - risk * TP_R

        result.update({
            "entry": entry,
            "zone_low": entry - atr14 * 0.15,
            "zone_high": entry + atr14 * 0.15,
            "sl": sl,
            "tp1": tp,
            "risk_distance": risk,
        })
    else:
        result.update({
            "entry": None, "zone_low": None, "zone_high": None,
            "sl": None, "tp1": None, "risk_distance": None,
        })

    return result


# ------------------------------------------------------------
# ECONOMIC CALENDAR
# ------------------------------------------------------------

def fetch_economic_calendar():
    params = urllib.parse.urlencode({
        "country": "US",
        "impact": "high,medium",
        "tz": "Asia/Kuala_Lumpur",
        "limit": "500",
    })

    try:
        payload = get_json(f"{CALENDAR_URL}?{params}")
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise RuntimeError("Format kalendar tidak sah")

        terms = (
            "fomc", "federal reserve", "fed ", "interest rate",
            "rate decision", "cpi", "pce", "inflation", "nonfarm",
            "payroll", "unemployment", "jobless claims", "gdp",
            "ism", "retail sales", "producer price", "ppi",
            "powell", "minutes", "jolts", "adp employment",
            "personal income", "personal spending", "employment cost",
            "consumer confidence", "durable goods", "treasury",
        )

        events = []
        for item in payload["events"]:
            if not isinstance(item, dict):
                continue

            impact = str(item.get("impact") or "").lower()
            importance = {"high": 3, "medium": 2, "low": 1}.get(impact, 0)

            name = str(
                item.get("title") or item.get("name")
                or item.get("series_name") or ""
            ).strip()

            if importance < 2 or not name:
                continue
            if not any(term in name.lower() for term in terms):
                continue

            date = str(
                item.get("release_at_utc") or item.get("time_utc")
                or item.get("time_local") or item.get("date") or ""
            ).strip()

            if not date:
                continue

            events.append({
                "date": date,
                "country": item.get("country", "US"),
                "event": name,
                "importance": importance,
                "actual": item.get("actual") or "",
                "forecast": item.get("consensus") or item.get("forecast") or "",
                "previous": item.get("prior") or item.get("previous") or "",
                "currency": "USD",
                "source_url": item.get("url") or CALENDAR_PAGE,
            })

        return {
            "status": "FRESH",
            "source": "Finance Calendar API",
            "updated_utc": now().isoformat(),
            "events": events[:80],
            "message": "Kalendar ekonomi berjaya dikemas kini.",
            "url": CALENDAR_PAGE,
            "attribution_url": "https://www.financecalendar.com/",
            "timezone_note": "Masa kalendar diminta dalam Asia/Kuala_Lumpur.",
        }

    except Exception as exc:
        return {
            "status": "ERROR",
            "source": "Finance Calendar API",
            "updated_utc": now().isoformat(),
            "events": [],
            "message": f"Kalendar tidak tersedia: {exc}",
            "url": CALENDAR_PAGE,
            "attribution_url": "https://www.financecalendar.com/",
            "timezone_note": "Masa acara tidak dapat disahkan.",
        }


def event_timestamp(event):
    try:
        stamp = datetime.fromisoformat(
            str(event["date"]).replace("Z", "+00:00")
        )
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=MYT)
        return stamp.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None


def analyze_news_impact(calendar, candles, technical_signal, atr_value):
    # Fail closed if calendar is unavailable: do not assume there is no news.
    if not isinstance(calendar, dict) or calendar.get("status") != "FRESH":
        return {
            "status": "CALENDAR_ERROR",
            "bias": "WAIT",
            "blocks_signal": True,
            "event": None,
            "message": "NEWS FILTER — kalendar gagal dimuatkan; signal baharu disekat.",
            "source": "Finance Calendar API",
        }

    current = now()
    high_events = []

    for event in calendar.get("events", []):
        if int(event.get("importance", 0) or 0) < 3:
            continue
        stamp = event_timestamp(event)
        if stamp:
            high_events.append((stamp, event))

    upcoming = [
        (stamp, event) for stamp, event in high_events
        if timedelta(minutes=-15) <= stamp - current <= timedelta(minutes=30)
    ]

    if upcoming:
        stamp, event = min(
            upcoming,
            key=lambda x: abs((x[0] - current).total_seconds())
        )
        mins = round((stamp - current).total_seconds() / 60)
        return {
            "status": "NEWS_LOCK",
            "bias": "WAIT",
            "blocks_signal": True,
            "event": event.get("event"),
            "minutes_to_event": mins,
            "message": f"NEWS LOCK — {event.get('event')} dalam/selepas {abs(mins)} minit.",
            "source": "Finance Calendar API",
        }

    recent = [
        (stamp, event) for stamp, event in high_events
        if timedelta(minutes=15) < current - stamp <= timedelta(minutes=60)
    ]

    if recent:
        stamp, event = max(recent, key=lambda x: x[0])

        # Use only bars opened after the event time.
        post_event = [
            c for c in candles
            if datetime.fromtimestamp(c["t"], timezone.utc) >= stamp
        ]

        if len(post_event) < 3:
            bias = "WAIT"
        else:
            move = post_event[-1]["c"] - post_event[0]["o"]
            candle = post_event[-1]
            threshold = float(atr_value) * 0.25

            if move >= threshold and candle["c"] > candle["o"]:
                bias = "BUY BIAS"
            elif move <= -threshold and candle["c"] < candle["o"]:
                bias = "SELL BIAS"
            else:
                bias = "WAIT"

        blocks = (
            bias == "WAIT"
            or (
                technical_signal in ("BUY", "SELL")
                and technical_signal != bias.replace(" BIAS", "")
            )
        )

        return {
            "status": "NEWS_WAIT" if blocks else bias,
            "bias": bias,
            "blocks_signal": blocks,
            "event": event.get("event"),
            "minutes_since_event": int((current - stamp).total_seconds() / 60),
            "message": (
                f"NEWS WAIT — reaksi selepas {event.get('event')} belum mengesahkan arah."
                if blocks else
                f"{bias} selepas {event.get('event')}; reaksi candle M5 sehaluan."
            ),
            "source": "Finance Calendar API + candle selepas acara",
        }

    return {
        "status": "NORMAL",
        "bias": "NEUTRAL",
        "blocks_signal": False,
        "event": None,
        "message": "Tiada berita USD berimpak tinggi dalam tetingkap sekatan.",
        "source": "Finance Calendar API",
    }


# ------------------------------------------------------------
# NOTIFICATIONS / ACTIVE SETUP
# ------------------------------------------------------------

def notify(title, message):
    if not TOPIC:
        print("NTFY_SKIPPED: NTFY_TOPIC tiada")
        return False

    req = urllib.request.Request(
        f"{SERVER}/{TOPIC}",
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title.encode("ascii", "replace").decode("ascii"),
            "Priority": "high",
            "Tags": "chart",
        },
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"ntfy HTTP {response.status}")
    return True


def clear_signal(public):
    for key in (
        "entry", "zone_low", "zone_high",
        "sl", "tp1", "risk_distance"
    ):
        public[key] = None

    public.update({
        "signal": "WAIT",
        "score": 0,
        "quality": "WAIT",
        "strategy": "NONE",
    })


def monitor_active(state, public, quote):
    active = state.get("active_setup")

    if not isinstance(active, dict) or active.get("status") != "ACTIVE":
        public["active_setup"] = active
        return

    side = active.get("side")
    if side not in ("BUY", "SELL"):
        public["active_setup"] = active
        return

    stamp = quote["timestamp"]
    if stamp == active.get("last_checked_quote_timestamp"):
        public["active_setup"] = active
        return

    active["last_checked_quote_timestamp"] = stamp
    price = quote["bid"] if side == "BUY" else quote["ask"]

    try:
        sl = float(active["sl"])
        tp = float(active["tp1"])
    except (KeyError, TypeError, ValueError):
        public["active_setup"] = active
        return

    sl_hit = price <= sl if side == "BUY" else price >= sl
    tp_hit = price >= tp if side == "BUY" else price <= tp

    event = "SL_HIT" if sl_hit else "TP_HIT" if tp_hit else None

    if event:
        active["status"] = event
        active["last_event_utc"] = now().isoformat()
        active["last_event_price"] = price
        active["closed_utc"] = now().isoformat()

        if active.get("last_event_notified") != event:
            msg = (
                f"MCD UPDATE - {event}\n"
                f"Symbol: XAUUSD | M5\nSide: {side}\n"
                f"Sampled price: {fmt(price)}\n"
                f"Entry: {fmt(active.get('entry'))}\n"
                f"SL: {fmt(sl)}\nTP: {fmt(tp)}\n"
                f"Quote UTC: {stamp}\n"
                "Biquote sampled quote; bukan pengesahan fill broker."
            )
            try:
                if notify(f"MCD UPDATE - {event}", msg):
                    active["last_event_notified"] = event
            except Exception as exc:
                active["notification_error"] = str(exc)

    public["active_setup"] = active
    public["sampled_exit_price"] = price
    public["sampled_exit_price_type"] = "BID" if side == "BUY" else "ASK"


def update_history_and_stats(state, public):
    history = state.get("signal_history", [])
    if not isinstance(history, list):
        history = []

    active = state.get("active_setup")

    if isinstance(active, dict) and active.get("id"):
        row = next(
            (x for x in history if isinstance(x, dict) and x.get("id") == active["id"]),
            None
        )

        if row is None:
            history.append({
                "id": active["id"],
                "side": active.get("side"),
                "status": active.get("status", "ACTIVE"),
                "entry": active.get("entry"),
                "sl": active.get("sl"),
                "tp1": active.get("tp1"),
                "created_utc": active.get("created_utc"),
                "strategy": active.get("strategy"),
                "score": active.get("score"),
            })
        else:
            row["status"] = active.get("status", row.get("status"))
            row["closed_utc"] = active.get("closed_utc")

    history = history[-MAX_HISTORY:]
    state["signal_history"] = history

    terminal = [
        x for x in history
        if x.get("status") in ("TP_HIT", "SL_HIT", "EXPIRED", "BREAK_EVEN")
    ]

    tp = sum(x.get("status") == "TP_HIT" for x in terminal)
    sl = sum(x.get("status") == "SL_HIT" for x in terminal)

    public["signal_history"] = history
    public["performance"] = {
        "total_signals": len(history),
        "tp_hits": tp,
        "sl_hits": sl,
        "break_even": sum(x.get("status") == "BREAK_EVEN" for x in terminal),
        "expired": sum(x.get("status") == "EXPIRED" for x in terminal),
        "decided_signals": len(terminal),
        "win_rate_percent": round(tp / len(terminal) * 100, 2) if terminal else None,
        "note": (
            "TP_HIT/SL_HIT berdasarkan quote Biquote yang disampel; "
            "bukan pengesahan fill broker."
        ),
    }


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():
    run_time = now()
    state = read_json(STATE)

    public = {
        "version": "V3.0-M5-INDICATORS-SINGLE-TP",
        "symbol": SYMBOL,
        "timeframe": "M5",
        "data_source": "Twelve Data OHLC + Biquote quote",
        "data_note": (
            "Bukan feed rasmi HFM XAUUSDc. Bandingkan harga dengan broker. "
            "Signal dinilai daripada candle M5 yang telah tutup."
        ),
        "refresh_model": "GitHub Actions setiap 5 minit.",
        "updated_utc": run_time.isoformat(),
        "data_status": "DATA_ERROR",
        "signal": "WAIT",
        "score": 0,
        "quality": "WAIT",
        "reason": "Menunggu data sah.",
        "trend_45m": "UNKNOWN",
        "trend": {
            "d1": "UNKNOWN", "h4": "UNKNOWN", "h1": "UNKNOWN",
            "m30": "UNKNOWN", "m15": "UNKNOWN",
        },
        "indicators": {},
        "techniques": {
            "snr": False, "smc": False,
            "pa": False, "breakout": False,
        },
        "economic_calendar": {},
        "news_impact": {
            "status": "WAIT", "bias": "WAIT",
            "blocks_signal": True, "event": None,
            "message": "News belum dinilai.",
            "source": "Finance Calendar API",
        },
        "support": None,
        "resistance": None,
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
        "active_setup": state.get("active_setup"),
    }

    public["economic_calendar"] = fetch_economic_calendar()

    try:
        quote = get_quote()
        epoch = int(run_time.timestamp())

        raw, candle_source = fetch_candles("5m", 1000)
        candles = [
            c for c in raw
            if not c["is_open"] and c["t"] + 300 <= epoch
        ]

        if len(candles) < MIN_BARS:
            raise RuntimeError(f"Candle M5 tidak cukup: {len(candles)}")

        last = candles[-1]
        candle_age = epoch - (last["t"] + 300)

        if candle_age < 0 or candle_age > MAX_CANDLE_AGE:
            raise RuntimeError(f"Candle M5 lapuk ({candle_age}s)")

        if len(candles) > 1 and last["t"] - candles[-2]["t"] > 600:
            raise RuntimeError("Jurang candle M5 terlalu besar")

        trends = {}
        for key, interval in (
            ("d1", "1d"), ("h4", "4h"), ("h1", "1h"),
            ("m30", "30m"), ("m15", "15m")
        ):
            try:
                trends[key] = get_trend(interval)
            except Exception as exc:
                trends[key] = "UNKNOWN"
                print(f"{key} trend unavailable: {exc}")

        result = evaluate(candles[-1000:], trends)

        news = analyze_news_impact(
            public["economic_calendar"],
            candles,
            result["signal"],
            result["atr14"],
        )
        public["news_impact"] = news

        if news.get("blocks_signal") and result["signal"] in ("BUY", "SELL"):
            rejected = result["signal"]
            result.update({
                "signal": "WAIT",
                "strategy": "NEWS FILTER",
                "entry": None, "zone_low": None, "zone_high": None,
                "sl": None, "tp1": None, "risk_distance": None,
                "news_filter_rejected": rejected,
            })

        # Anti-chasing uses executable side of quote.
        if result["signal"] in ("BUY", "SELL"):
            live_price = quote["ask"] if result["signal"] == "BUY" else quote["bid"]
            drift = abs(live_price - result["entry"])

            if drift > result["atr14"] * MAX_ENTRY_DRIFT_ATR:
                rejected = result["signal"]
                result.update({
                    "signal": "WAIT",
                    "strategy": "NO CHASE",
                    "entry": None, "zone_low": None, "zone_high": None,
                    "sl": None, "tp1": None, "risk_distance": None,
                    "no_chase_rejected": rejected,
                    "no_chase_drift": drift,
                })

        public.update(result)
        public.update({
            "data_status": "FRESH",
            "updated_utc": run_time.isoformat(),
            "last_candle_utc": iso(last["t"] + 300),
            "candle_age_seconds": candle_age,
            "trend_45m": (
                "BULLISH" if candles[-1]["c"] > candles[-10]["c"]
                else "BEARISH" if candles[-1]["c"] < candles[-10]["c"]
                else "FLAT"
            ),
            "price": quote["mid"],
            "quote_mid": quote["mid"],
            "quote_bid": quote["bid"],
            "quote_ask": quote["ask"],
            "quote_spread": quote["spread"],
            "quote_timestamp_utc": quote["timestamp"],
            "quote_age_seconds": quote["age"],
            "quote_source": "Biquote API",
            "ohlc_source": candle_source,
            "market_state": "open",
            "candle_close": last["c"],
            "quote_live_endpoint": f"{BASE}/{SYMBOL}?allowStale=false",
            "chart_candles": [
                {k: c[k] for k in ("t", "o", "h", "l", "c")}
                for c in candles[-40:]
            ],
            "reason": (
                f"{candle_source} OHLC dan Biquote quote lulus semakan. "
                "Bandingkan harga dengan broker."
            ),
        })

        monitor_active(state, public, quote)

        if result["signal"] in ("BUY", "SELL"):
            key = f'{result["signal"]}-{last["t"]}'

            # Do not overwrite an existing active setup.
            active = state.get("active_setup")
            if not isinstance(active, dict) or active.get("status") != "ACTIVE":
                if state.get("last_setup_key") != key:
                    state["active_setup"] = {
                        "id": key,
                        "side": result["signal"],
                        "status": "ACTIVE",
                        "entry": result["entry"],
                        "sl": result["sl"],
                        "tp1": result["tp1"],
                        "risk_distance": result["risk_distance"],
                        "strategy": result["strategy"],
                        "score": result["score"],
                        "created_candle_t": last["t"],
                        "created_utc": iso(last["t"] + 300),
                    }
                    state["last_setup_key"] = key

            public["active_setup"] = state.get("active_setup")

            if state.get("last_signal_key") != key:
                message = (
                    f'MCD SIGNAL - {result["signal"]}\n'
                    "Symbol: XAUUSD | M5\n"
                    f'Entry: {fmt(result["entry"])}\n'
                    f'SL: {fmt(result["sl"])}\n'
                    f'TP: {fmt(result["tp1"])} ({TP_R:g}R)\n'
                    f'Quality: {result["score"]}/10 - {result["quality"]}\n'
                    f'Setup: {result["strategy"]}\n'
                    f'Current: {fmt(quote["mid"])}\n'
                    f'Bid/Ask: {fmt(quote["bid"])} / {fmt(quote["ask"])}\n'
                    f'Spread: {fmt(quote["spread"])}\n'
                    f'MA: {result["indicators"]["ma_cross"]}\n'
                    f'VWAP: {result["indicators"]["vwap_bias"]}\n'
                    f'RSI: {fmt(result["indicators"]["rsi14"])}\n'
                    f'MACD: {result["indicators"]["macd_bias"]}\n'
                    f'ATR filter: {result["indicators"]["atr_filter"]}\n'
                    f'Quote UTC: {quote["timestamp"]}\n'
                    "Notification only; tiada order dihantar."
                )

                try:
                    if notify(f'MCD SIGNAL - {result["signal"]}', message):
                        state["last_signal_key"] = key
                    else:
                        public["notification_error"] = "NTFY_TOPIC tiada."
                except Exception as exc:
                    public["notification_error"] = str(exc)

        else:
            if result.get("strategy") == "NEWS FILTER":
                public["reason"] = news.get("message", "News filter menyekat signal.")
            elif result.get("strategy") == "NO CHASE":
                public["reason"] = (
                    "WAIT — harga sudah bergerak terlalu jauh daripada entry."
                )
            else:
                public["reason"] = (
                    f'WAIT — BUY {result["buy_score"]}/10, '
                    f'SELL {result["sell_score"]}/10; tiada setup sah.'
                )

        state["last_status"] = "DATA_VALID"
        state["last_error"] = None
        state["last_candle_t"] = last["t"]

    except Exception as exc:
        clear_signal(public)
        public.update({
            "data_status": "DATA_ERROR",
            "reason": f"DATA_ERROR — {exc}. Signal disekat.",
            "price": None,
            "quote_bid": None,
            "quote_ask": None,
            "quote_mid": None,
            "quote_spread": None,
            "quote_timestamp_utc": None,
            "quote_age_seconds": None,
            "quote_source": None,
            "ohlc_source": None,
            "market_state": None,
            "candle_close": None,
            "chart_candles": [],
        })
        state["last_status"] = "DATA_ERROR"
        state["last_error"] = str(exc)
        print("WAIT / DATA_ERROR:", exc)

    state["last_run_utc"] = run_time.isoformat()
    public["updated_utc"] = run_time.isoformat()
    public["active_setup"] = state.get("active_setup")

    update_history_and_stats(state, public)
    write_json(STATUS, public)
    write_json(STATE, state)


if __name__ == "__main__":
    main()
