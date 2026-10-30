#!/usr/bin/env python3
"""
MCD SIGNAL V3.1 - XAUUSD M5
Manual execution only. No broker orders are sent.

Data:
- OHLC: Twelve Data
- Live sampled quote: Biquote
- Economic calendar: Finance Calendar API

Important:
- For XAU/USD, Twelve Data may not provide real traded volume.
  When volume is unavailable, VWAP is explicitly marked as a fallback.
- TP/SL outcomes are based on sampled Biquote prices, not broker fills.
"""

import json
import math
import os
import statistics
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta

BASE = "https://biquote.io/api"
TD_BASE = "https://api.twelvedata.com"
CALENDAR_URL = "https://www.financecalendar.com/wp-json/fc/v1/calendar"
CALENDAR_PAGE = "https://www.financecalendar.com/api/economic-calendar/"

SYMBOL = "XAUUSD"
TD_SYMBOL = os.getenv("TWELVE_DATA_SYMBOL", "XAU/USD").strip()
TD_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

STATE_FILE = ".mcd_signal_state.json"
STATUS_FILE = "signal_status.json"

MIN_BARS = 100
MAX_CANDLE_AGE = 900
MAX_QUOTE_AGE = 300
MAX_ENTRY_DRIFT_ATR = 1.2
MIN_SCORE = 6
MIN_VOTES = 3
TP_R = 1.5
MAX_HISTORY = 1000
NEWS_LOCK_MINUTES = 30
SETUP_ID_PREFIX = "MCD"
MYT = timezone(timedelta(hours=8))


# ============================================================
# GENERAL HELPERS
# ============================================================

def now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def fmt(value):
    return f"{value:.2f}" if finite(value) else "N/A"


def get_json(url, timeout=20):
    request = urllib.request.Request(
        url, headers={"User-Agent": "MCD-SIGNAL/3.1"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def write_json(path, data):
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, allow_nan=False)
    os.replace(temp_path, path)


def read_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            return {}
        return value
    except (OSError, ValueError):
        return {}


def parse_datetime(value, naive_tz=timezone.utc):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_tz)
    return parsed.astimezone(timezone.utc)


# ============================================================
# QUOTE AND OHLC DATA
# ============================================================

def get_quote():
    payload = get_json(f"{BASE}/{SYMBOL}?allowStale=false")
    try:
        bid = float(payload["bid"])
        ask = float(payload["ask"])
        mid = float(payload["mid"])
        stamp = parse_datetime(payload["timestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Quote tidak lengkap: {exc}") from exc

    if not all(finite(x) and x > 0 for x in (bid, ask, mid)):
        raise RuntimeError("Quote tidak sah")
    if ask < bid:
        raise RuntimeError("Ask lebih rendah daripada Bid")

    age = int((now() - stamp).total_seconds())
    if age < -60 or age > MAX_QUOTE_AGE:
        raise RuntimeError(f"Quote lapuk: {age}s")
    if str(payload.get("marketState", "")).lower() != "open":
        raise RuntimeError("Pasaran bukan OPEN")

    spread = float(payload.get("spread", ask - bid))
    if not finite(spread) or spread < 0:
        spread = ask - bid

    return {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread": spread,
        "timestamp": iso(stamp),
        "age": max(0, age),
    }


def get_bars(interval="5m", limit=1000):
    if not TD_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY belum ditetapkan")

    interval_map = {
        "5m": ("5min", 300),
        "15m": ("15min", 900),
        "30m": ("30min", 1800),
        "1h": ("1h", 3600),
        "4h": ("4h", 14400),
        "1d": ("1day", 86400),
    }
    if interval not in interval_map:
        raise RuntimeError(f"Interval tidak disokong: {interval}")

    td_interval, seconds = interval_map[interval]
    query = urllib.parse.urlencode({
        "symbol": TD_SYMBOL,
        "interval": td_interval,
        "outputsize": min(max(int(limit), 1), 5000),
        "timezone": "UTC",
        "apikey": TD_KEY,
    })
    payload = get_json(f"{TD_BASE}/time_series?{query}")
    if not isinstance(payload, dict) or payload.get("status") == "error":
        raise RuntimeError(
            f"Twelve Data: {payload.get('message', 'respons tidak sah')}"
        )

    values = payload.get("values")
    if not isinstance(values, list):
        raise RuntimeError("Data OHLC tidak tersedia")

    epoch_now = int(now().timestamp())
    unique = {}

    for item in values:
        try:
            stamp = parse_datetime(item["datetime"])
            epoch = int(stamp.timestamp())
            o = float(item["open"])
            h = float(item["high"])
            low = float(item["low"])
            close = float(item["close"])
            raw_volume = item.get("volume")
            volume = float(raw_volume) if raw_volume not in (None, "") else 0.0

            if not all(finite(x) and x > 0 for x in (o, h, low, close)):
                continue
            if h < max(o, low, close) or low > min(o, h, close):
                continue
            if volume < 0 or not finite(volume):
                volume = 0.0

            unique[epoch] = {
                "t": epoch,
                "o": o,
                "h": h,
                "l": low,
                "c": close,
                "v": volume,
                "is_open": epoch + seconds > epoch_now,
            }
        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    return [unique[key] for key in sorted(unique)]


def get_trend(interval):
    candles = [x for x in get_bars(interval, 150) if not x["is_open"]]
    if len(candles) < 60:
        return "UNKNOWN"
    closes = [x["c"] for x in candles]
    fast = ema(closes, 20)[-1]
    slow = ema(closes, 50)[-1]
    if fast > slow:
        return "BULLISH"
    if fast < slow:
        return "BEARISH"
    return "FLAT"


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        raise RuntimeError(f"Data tidak cukup untuk EMA{period}")
    alpha = 2.0 / (period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1 - alpha) * result[-1])
    return result


def atr_series(candles, period=14):
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
        raise RuntimeError("Data tidak cukup untuk ATR")
    result = [None] * period
    value = sum(true_ranges[:period]) / period
    result.append(value)
    for item in true_ranges[period:]:
        value = (value * (period - 1) + item) / period
        result.append(value)
    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        raise RuntimeError("Data tidak cukup untuk RSI")
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for index in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
    if avg_loss == 0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return 100 - (100 / (1 + relative_strength))


def macd(values):
    fast = ema(values, 12)
    slow = ema(values, 26)
    line = [fast[i] - slow[i] for i in range(len(values))]
    signal = ema(line, 9)
    histogram = [line[i] - signal[i] for i in range(len(line))]
    return line, signal, histogram


def session_vwap(candles):
    session_date = datetime.fromtimestamp(
        candles[-1]["t"], timezone.utc
    ).astimezone(MYT).date()

    session = [
        candle for candle in candles
        if datetime.fromtimestamp(
            candle["t"], timezone.utc
        ).astimezone(MYT).date() == session_date
    ]
    if not session:
        raise RuntimeError("Session VWAP tidak tersedia")

    total_volume = sum(c.get("v", 0.0) for c in session)
    typical = [
        (c["h"] + c["l"] + c["c"]) / 3 for c in session
    ]

    if total_volume > 0:
        value = sum(
            typical[i] * session[i].get("v", 0.0)
            for i in range(len(session))
        ) / total_volume
        return value, "VOLUME_WEIGHTED"

    # Explicit fallback: not a true volume-weighted VWAP.
    return sum(typical) / len(typical), "EQUAL_WEIGHT_FALLBACK"


# ============================================================
# SIGNAL ENGINE - 10 POINTS
# ============================================================

def evaluate(candles, trends):
    if len(candles) < MIN_BARS:
        raise RuntimeError(f"Candle M5 tidak cukup: {len(candles)}")

    closes = [x["c"] for x in candles]
    current = candles[-1]
    previous = candles[-2]
    price = current["c"]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    atrs = atr_series(candles, 14)
    atr14 = atrs[-1]
    rsi14 = rsi(closes, 14)
    macd_line, macd_signal, macd_hist = macd(closes)
    vwap, vwap_method = session_vwap(candles)

    ma_bull = ema9[-1] > ema21[-1]
    ma_bear = ema9[-1] < ema21[-1]
    prev_bull = ema9[-2] > ema21[-2]
    prev_bear = ema9[-2] < ema21[-2]

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

    vwap_bull = price > vwap
    vwap_bear = price < vwap
    vwap_bias = "BULLISH" if vwap_bull else "BEARISH" if vwap_bear else "NEUTRAL"

    rsi_bull = rsi14 >= 52
    rsi_bear = rsi14 <= 48
    rsi_bias = "BULLISH" if rsi_bull else "BEARISH" if rsi_bear else "NEUTRAL"

    macd_bull = macd_line[-1] > macd_signal[-1] and macd_hist[-1] > 0
    macd_bear = macd_line[-1] < macd_signal[-1] and macd_hist[-1] < 0
    macd_bias = "BULLISH" if macd_bull else "BEARISH" if macd_bear else "NEUTRAL"

    recent_atrs = [x for x in atrs[-51:] if finite(x) and x > 0]
    median_atr = statistics.median(recent_atrs) if recent_atrs else atr14
    atr_ratio = atr14 / median_atr if median_atr else 1.0
    atr_pass = 0.55 <= atr_ratio <= 2.50

    high20 = max(x["h"] for x in candles[-21:-1])
    low20 = min(x["l"] for x in candles[-21:-1])
    high10 = max(x["h"] for x in candles[-11:-1])
    low10 = min(x["l"] for x in candles[-11:-1])
    high30 = max(x["h"] for x in candles[-31:-1])
    low30 = min(x["l"] for x in candles[-31:-1])

    bullish = price > current["o"]
    bearish = price < current["o"]
    body_ok = abs(price - current["o"]) >= atr14 * 0.10
    extension_ok = abs(price - ema21[-1]) <= atr14 * 2.0

    buy_breakout = price > high20 and previous["c"] <= high20
    sell_breakout = price < low20 and previous["c"] >= low20
    buy_sweep = current["l"] < low10 and price > low10 and bullish
    sell_sweep = current["h"] > high10 and price < high10 and bearish
    buy_rejection = current["l"] <= low30 + atr14 * 0.25 and price > low30 and bullish
    sell_rejection = current["h"] >= high30 - atr14 * 0.25 and price < high30 and bearish
    buy_engulf = (
        bullish and previous["c"] < previous["o"]
        and current["o"] <= previous["c"] and price >= previous["o"]
    )
    sell_engulf = (
        bearish and previous["c"] > previous["o"]
        and current["o"] >= previous["c"] and price <= previous["o"]
    )

    buy_structure = any((buy_breakout, buy_sweep, buy_rejection, buy_engulf))
    sell_structure = any((sell_breakout, sell_sweep, sell_rejection, sell_engulf))

    buy_votes = sum((ma_bull, vwap_bull, rsi_bull, macd_bull))
    sell_votes = sum((ma_bear, vwap_bear, rsi_bear, macd_bear))
    htf_bull = sum(value == "BULLISH" for value in trends.values())
    htf_bear = sum(value == "BEARISH" for value in trends.values())

    buy_score = sum((
        ma_bull, vwap_bull, rsi_bull, macd_bull,
        htf_bull >= 1, buy_structure, buy_breakout,
        bullish, body_ok, extension_ok,
    ))
    sell_score = sum((
        ma_bear, vwap_bear, rsi_bear, macd_bear,
        htf_bear >= 1, sell_structure, sell_breakout,
        bearish, body_ok, extension_ok,
    ))

    buy_valid = (
        buy_score >= MIN_SCORE and buy_votes >= MIN_VOTES
        and buy_structure and bullish and body_ok and extension_ok
        and atr_pass and not sell_structure
    )
    sell_valid = (
        sell_score >= MIN_SCORE and sell_votes >= MIN_VOTES
        and sell_structure and bearish and body_ok and extension_ok
        and atr_pass and not buy_structure
    )

    side = "WAIT"
    setup_type = "NONE"
    score = max(buy_score, sell_score)

    if buy_valid and not sell_valid:
        side, score = "BUY", buy_score
        setup_type = (
            "BREAKOUT" if buy_breakout else
            "LIQUIDITY SWEEP" if buy_sweep else
            "BULLISH ENGULFING" if buy_engulf else
            "SUPPORT REJECTION"
        )
    elif sell_valid and not buy_valid:
        side, score = "SELL", sell_score
        setup_type = (
            "BREAKOUT" if sell_breakout else
            "LIQUIDITY SWEEP" if sell_sweep else
            "BEARISH ENGULFING" if sell_engulf else
            "RESISTANCE REJECTION"
        )

    quality = (
        "STRONG (A)" if side != "WAIT" and score >= 8 else
        "MODERATE (B)" if side != "WAIT" and score >= 6 else
        "WAIT"
    )

    support = low30
    resistance = high30
    indicators = {
        "ma_cross": ma_cross,
        "ma_bias": "BULLISH" if ma_bull else "BEARISH" if ma_bear else "NEUTRAL",
        "ema9": ema9[-1],
        "ema21": ema21[-1],
        "vwap": vwap,
        "vwap_bias": vwap_bias,
        "vwap_method": vwap_method,
        "rsi14": rsi14,
        "rsi_bias": rsi_bias,
        "macd": macd_line[-1],
        "macd_signal": macd_signal[-1],
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
        "strategy": setup_type,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "atr14": atr14,
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
        distance = max(atr14 * 1.5, (current["h"] - current["l"]) * 1.2)
        if side == "BUY":
            sl = min(current["l"] - atr14 * 0.15, entry - distance)
            tp = entry + (entry - sl) * TP_R
        else:
            sl = max(current["h"] + atr14 * 0.15, entry + distance)
            tp = entry - (sl - entry) * TP_R

        result.update({
            "entry": entry,
            "zone_low": entry - atr14 * 0.15,
            "zone_high": entry + atr14 * 0.15,
            "sl": sl,
            "tp1": tp,
            "risk_distance": abs(entry - sl),
        })
    else:
        result.update({
            "entry": None, "zone_low": None, "zone_high": None,
            "sl": None, "tp1": None, "risk_distance": None,
        })

    return result


# ============================================================
# ECONOMIC CALENDAR AND NEWS LOCK
# ============================================================

def fetch_economic_calendar():
    query = urllib.parse.urlencode({
        "country": "US",
        "impact": "high,medium",
        "tz": "Asia/Kuala_Lumpur",
        "limit": "500",
    })
    try:
        payload = get_json(f"{CALENDAR_URL}?{query}")
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise RuntimeError("Format kalendar tidak sah")

        keywords = (
            "fomc", "federal reserve", "fed ", "interest rate",
            "rate decision", "cpi", "pce", "inflation", "nonfarm",
            "payroll", "unemployment", "jobless claims", "gdp",
            "ism", "retail sales", "producer price", "ppi",
            "powell", "minutes", "jolts", "adp employment",
            "personal income", "personal spending", "employment cost",
            "consumer confidence", "durable goods",
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
            if not any(word in name.lower() for word in keywords):
                continue

            date_value = (
                item.get("release_at_utc") or item.get("time_utc")
                or item.get("time_local") or item.get("date")
            )
            if not date_value:
                continue

            events.append({
                "date": str(date_value),
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
            "events": events[:100],
            "message": "Kalendar berjaya dimuatkan.",
            "url": CALENDAR_PAGE,
            "attribution_url": "https://www.financecalendar.com/",
            "timezone_note": "Waktu paparan Asia/Kuala_Lumpur.",
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
            "timezone_note": "Waktu acara tidak dapat disahkan.",
        }


def news_lock(calendar):
    if not isinstance(calendar, dict) or calendar.get("status") != "FRESH":
        return {
            "status": "CALENDAR_ERROR",
            "blocks_signal": True,
            "event": None,
            "minutes_to_event": None,
            "message": "NEWS FILTER: kalendar gagal dimuatkan; signal baharu disekat.",
        }

    current = now()
    active_locks = []

    for event in calendar.get("events", []):
        try:
            event_time = parse_datetime(event["date"], MYT)
        except (KeyError, TypeError, ValueError):
            continue

        delta_minutes = (event_time - current).total_seconds() / 60
        if -NEWS_LOCK_MINUTES <= delta_minutes <= NEWS_LOCK_MINUTES:
            active_locks.append((abs(delta_minutes), delta_minutes, event))

    if active_locks:
        _, delta, event = min(active_locks, key=lambda x: x[0])
        return {
            "status": "NEWS_LOCK",
            "blocks_signal": True,
            "event": event.get("event"),
            "minutes_to_event": round(delta),
            "message": (
                f"NEWS LOCK: {event.get('event')} dalam tetingkap "
                f"{NEWS_LOCK_MINUTES} minit sebelum/selepas berita."
            ),
        }

    return {
        "status": "NORMAL",
        "blocks_signal": False,
        "event": None,
        "minutes_to_event": None,
        "message": "Tiada berita terpilih dalam tetingkap sekatan ±30 minit.",
    }


# ============================================================
# NOTIFICATIONS
# ============================================================

def notify(title, message):
    if not NTFY_TOPIC:
        print("NTFY skipped: NTFY_TOPIC belum ditetapkan.")
        return False

    request = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title.encode("ascii", "replace").decode("ascii"),
            "Priority": "high",
            "Tags": "chart",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"ntfy HTTP {response.status}")
    return True


# ============================================================
# MULTIPLE ACTIVE SETUPS AND HISTORY
# ============================================================

def ensure_state(state):
    if not isinstance(state.get("active_setups"), list):
        legacy = state.get("active_setup")
        state["active_setups"] = (
            [legacy] if isinstance(legacy, dict)
            and legacy.get("status") == "ACTIVE" else []
        )
    if not isinstance(state.get("signal_history"), list):
        state["signal_history"] = []
    return state


def create_setup(result, candle, quote):
    side = result["signal"]
    key = f"{side}-{candle['t']}"
    setup_id = f"{SETUP_ID_PREFIX}-{key}"

    return {
        "id": setup_id,
        "key": key,
        "side": side,
        "status": "ACTIVE",
        "entry": result["entry"],
        "zone_low": result["zone_low"],
        "zone_high": result["zone_high"],
        "sl": result["sl"],
        "tp1": result["tp1"],
        "risk_distance": result["risk_distance"],
        "strategy": result["strategy"],
        "score": result["score"],
        "quality": result["quality"],
        "created_candle_t": candle["t"],
        "created_utc": iso(datetime.fromtimestamp(
            candle["t"] + 300, timezone.utc
        )),
        "created_quote_utc": quote["timestamp"],
        "last_checked_quote_timestamp": None,
        "closed_utc": None,
        "exit_price": None,
        "exit_reason": None,
    }


def monitor_active_setups(state, quote):
    active_setups = state["active_setups"]
    still_active = []
    changed = []

    for setup in active_setups:
        if not isinstance(setup, dict) or setup.get("status") != "ACTIVE":
            continue

        side = setup.get("side")
        if side not in ("BUY", "SELL"):
            continue

        quote_stamp = quote["timestamp"]
        if setup.get("last_checked_quote_timestamp") == quote_stamp:
            still_active.append(setup)
            continue

        setup["last_checked_quote_timestamp"] = quote_stamp
        sampled_price = quote["bid"] if side == "BUY" else quote["ask"]

        try:
            sl = float(setup["sl"])
            tp = float(setup["tp1"])
        except (KeyError, TypeError, ValueError):
            still_active.append(setup)
            continue

        sl_hit = sampled_price <= sl if side == "BUY" else sampled_price >= sl
        tp_hit = sampled_price >= tp if side == "BUY" else sampled_price <= tp

        # Conservative handling if both levels are crossed between samples:
        # record SL first because intrabar order is unknown.
        outcome = "SL_HIT" if sl_hit else "TP_HIT" if tp_hit else None

        if outcome:
            setup["status"] = outcome
            setup["exit_reason"] = outcome
            setup["exit_price"] = sampled_price
            setup["exit_price_type"] = "BID" if side == "BUY" else "ASK"
            setup["closed_utc"] = now().isoformat()
            setup["exit_quote_utc"] = quote_stamp
            changed.append(setup)
        else:
            still_active.append(setup)

    state["active_setups"] = still_active
    return changed


def update_history(state, changed_setups):
    history = state["signal_history"]
    by_id = {
        item.get("id"): item for item in history
        if isinstance(item, dict) and item.get("id")
    }

    for setup in changed_setups:
        setup_id = setup["id"]
        if setup_id in by_id:
            by_id[setup_id].update(setup)
        else:
            history.append(dict(setup))

    # Keep active and terminal records in the same history ledger.
    known = {x.get("id") for x in history if isinstance(x, dict)}
    for setup in state["active_setups"]:
        if setup.get("id") not in known:
            history.append(dict(setup))

    # Update any existing active history row from the current active state.
    by_id = {
        item.get("id"): item for item in history
        if isinstance(item, dict) and item.get("id")
    }
    for setup in state["active_setups"]:
        if setup.get("id") in by_id:
            by_id[setup["id"]].update(setup)

    state["signal_history"] = history[-MAX_HISTORY:]


def calculate_performance(history):
    terminal = [
        item for item in history
        if item.get("status") in ("TP_HIT", "SL_HIT")
    ]
    tp = sum(item.get("status") == "TP_HIT" for item in terminal)
    sl = sum(item.get("status") == "SL_HIT" for item in terminal)
    decided = len(terminal)

    return {
        "total_entries": len(history),
        "active_entries": sum(x.get("status") == "ACTIVE" for x in history),
        "tp_hits": tp,
        "sl_hits": sl,
        "decided_entries": decided,
        "win_rate_percent": round(tp / decided * 100, 2) if decided else None,
        "note": (
            "Keputusan berdasarkan quote Biquote yang disampel, "
            "bukan pengesahan fill broker."
        ),
    }


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    run_time = now()
    state = ensure_state(read_state())

    calendar = fetch_economic_calendar()
    news = news_lock(calendar)

    public = {
        "version": "V3.1-MULTI-SETUP",
        "symbol": SYMBOL,
        "timeframe": "M5",
        "data_source": "Twelve Data OHLC + Biquote sampled quote",
        "data_note": (
            "Bukan feed rasmi broker HFM XAUUSDc. Bandingkan harga "
            "dengan MT5 sebelum membuat keputusan."
        ),
        "updated_utc": run_time.isoformat(),
        "data_status": "DATA_ERROR",
        "signal": "WAIT",
        "score": 0,
        "quality": "WAIT",
        "reason": "Menunggu data.",
        "entry": None,
        "zone_low": None,
        "zone_high": None,
        "sl": None,
        "tp1": None,
        "risk_distance": None,
        "price": None,
        "quote_bid": None,
        "quote_ask": None,
        "quote_mid": None,
        "quote_spread": None,
        "quote_timestamp_utc": None,
        "quote_age_seconds": None,
        "trend": {
            "d1": "UNKNOWN", "h4": "UNKNOWN", "h1": "UNKNOWN",
            "m30": "UNKNOWN", "m15": "UNKNOWN",
        },
        "trend_45m": "UNKNOWN",
        "indicators": {},
        "techniques": {
            "snr": False, "smc": False, "pa": False, "breakout": False,
        },
        "economic_calendar": calendar,
        "news_impact": news,
        "active_setups": state["active_setups"],
        "active_setup": state["active_setups"][-1] if state["active_setups"] else None,
        "signal_history": state["signal_history"],
        "performance": calculate_performance(state["signal_history"]),
        "chart_candles": [],
    }

    try:
        quote = get_quote()
        candles = get_bars("5m", 1000)
        candles = [x for x in candles if not x["is_open"]]

        if len(candles) < MIN_BARS:
            raise RuntimeError(f"Candle M5 tidak mencukupi: {len(candles)}")

        last = candles[-1]
        epoch = int(run_time.timestamp())
        candle_age = epoch - (last["t"] + 300)

        if candle_age < 0 or candle_age > MAX_CANDLE_AGE:
            raise RuntimeError(f"Candle M5 lapuk: {candle_age}s")
        if len(candles) > 1 and last["t"] - candles[-2]["t"] > 600:
            raise RuntimeError("Jurang candle M5 terlalu besar")

        trends = {}
        for key, interval in (
            ("d1", "1d"), ("h4", "4h"), ("h1", "1h"),
            ("m30", "30m"), ("m15", "15m"),
        ):
            try:
                trends[key] = get_trend(interval)
            except Exception as exc:
                trends[key] = "UNKNOWN"
                print(f"Trend {key} unavailable: {exc}")

        result = evaluate(candles, trends)

        # Monitor every existing setup on every successful quote cycle.
        changed = monitor_active_setups(state, quote)
        update_history(state, changed)

        # News lock blocks only new entries; active setups continue to be monitored.
        candidate = result["signal"]
        if news["blocks_signal"] and candidate in ("BUY", "SELL"):
            result["signal"] = "WAIT"
            result["quality"] = "WAIT"
            result["strategy"] = "NEWS FILTER"
            result["entry"] = None
            result["zone_low"] = None
            result["zone_high"] = None
            result["sl"] = None
            result["tp1"] = None
            result["risk_distance"] = None
            result["reason"] = news["message"]

        # Do not chase entries that have already moved too far.
        if result["signal"] in ("BUY", "SELL"):
            executable_price = quote["ask"] if result["signal"] == "BUY" else quote["bid"]
            drift = abs(executable_price - result["entry"])
            if drift > result["atr14"] * MAX_ENTRY_DRIFT_ATR:
                result["signal"] = "WAIT"
                result["quality"] = "WAIT"
                result["strategy"] = "NO CHASE"
                result["entry"] = None
                result["zone_low"] = None
                result["zone_high"] = None
                result["sl"] = None
                result["tp1"] = None
                result["risk_distance"] = None
                result["reason"] = "WAIT — harga sudah bergerak terlalu jauh."

        # Record each new setup independently. Existing active setups are retained.
        if result["signal"] in ("BUY", "SELL"):
            setup = create_setup(result, last, quote)
            duplicate = any(
                item.get("id") == setup["id"]
                for item in state["active_setups"]
            )
            already_recorded = any(
                item.get("id") == setup["id"]
                for item in state["signal_history"]
            )

            if not duplicate and not already_recorded:
                state["active_setups"].append(setup)
                state["signal_history"].append(dict(setup))
                try:
                    notify(
                        f'MCD SIGNAL - {result["signal"]}',
                        (
                            f'MCD SIGNAL {result["signal"]}\n'
                            f'Symbol: {SYMBOL} | M5\n'
                            f'Entry: {fmt(result["entry"])}\n'
                            f'SL: {fmt(result["sl"])}\n'
                            f'TP: {fmt(result["tp1"])} ({TP_R:g}R)\n'
                            f'Score: {result["score"]}/10\n'
                            f'Setup: {result["strategy"]}\n'
                            f'Quote: {fmt(quote["mid"])}\n'
                            'Manual execution only; no order was sent.'
                        ),
                    )
                except Exception as exc:
                    public["notification_error"] = str(exc)

        update_history(state, [])
        active_setups = state["active_setups"]
        history = state["signal_history"]

        public.update(result)
        public.update({
            "data_status": "FRESH",
            "updated_utc": run_time.isoformat(),
            "last_candle_utc": datetime.fromtimestamp(
                last["t"] + 300, timezone.utc
            ).isoformat(),
            "candle_age_seconds": candle_age,
            "price": quote["mid"],
            "quote_bid": quote["bid"],
            "quote_ask": quote["ask"],
            "quote_mid": quote["mid"],
            "quote_spread": quote["spread"],
            "quote_timestamp_utc": quote["timestamp"],
            "quote_age_seconds": quote["age"],
            "quote_source": "Biquote API",
            "ohlc_source": "Twelve Data",
            "market_state": "open",
            "trend": trends,
            "trend_45m": (
                "BULLISH" if candles[-1]["c"] > candles[-10]["c"]
                else "BEARISH" if candles[-1]["c"] < candles[-10]["c"]
                else "FLAT"
            ),
            "active_setups": active_setups,
            "active_setup": active_setups[-1] if active_setups else None,
            "signal_history": history,
            "performance": calculate_performance(history),
            "chart_candles": [
                {key: candle[key] for key in ("t", "o", "h", "l", "c")}
                for candle in candles[-60:]
            ],
        })

        if result["signal"] == "WAIT" and not public.get("reason"):
            public["reason"] = (
                f'WAIT — BUY {result["buy_score"]}/10; '
                f'SELL {result["sell_score"]}/10.'
            )
        elif result["signal"] in ("BUY", "SELL"):
            public["reason"] = (
                "Setup baharu direkodkan. Sahkan harga, spread dan "
                "risiko pada platform broker sebelum entry manual."
            )

        state["last_status"] = "DATA_VALID"
        state["last_error"] = None
        state["last_run_utc"] = run_time.isoformat()
        state["last_candle_t"] = last["t"]

    except Exception as exc:
        public.update({
            "data_status": "DATA_ERROR",
            "signal": "WAIT",
            "score": 0,
            "quality": "WAIT",
            "reason": f"DATA_ERROR — {exc}. Signal baharu disekat.",
            "entry": None,
            "zone_low": None,
            "zone_high": None,
            "sl": None,
            "tp1": None,
            "risk_distance": None,
            "indicators": {},
            "chart_candles": [],
            "active_setups": state["active_setups"],
            "active_setup": state["active_setups"][-1] if state["active_setups"] else None,
            "signal_history": state["signal_history"],
            "performance": calculate_performance(state["signal_history"]),
        })
        state["last_status"] = "DATA_ERROR"
        state["last_error"] = str(exc)
        state["last_run_utc"] = run_time.isoformat()
        print("MCD DATA_ERROR:", exc)

    public["active_setups"] = state["active_setups"]
    public["active_setup"] = (
        state["active_setups"][-1] if state["active_setups"] else None
    )
    public["signal_history"] = state["signal_history"]
    public["performance"] = calculate_performance(state["signal_history"])

    write_json(STATUS_FILE, public)
    write_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
