import json
import os
import sys
import tempfile
import unittest

from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch


# ============================================================
# IMPORT PROJECT MODULE
# ============================================================

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import mcd_monitor as monitor  # noqa: E402


# ============================================================
# UNIT TESTS
# ============================================================

class MCDContractTests(unittest.TestCase):

    def candles(self, count=100):
        """
        Generate synthetic M5 OHLC candles.
        Used only for unit testing.
        """
        now_epoch = int(datetime.now(timezone.utc).timestamp())

        rows = []
        base = 2300.0

        for i in range(count):
            close = base + i * 0.08
            open_price = close - 0.04

            rows.append({
                "t": now_epoch - (count - i) * 300,
                "o": open_price,
                "h": close + 0.08,
                "l": open_price - 0.08,
                "c": close,
                "v": 0.0,
                "is_open": False,
            })

        return rows

    # ========================================================
    # TEST 1: SIGNAL ENGINE
    # ========================================================

    def test_evaluate_uses_one_sl_one_tp_and_four_techniques(self):
        trends = {
            "d1": "BULLISH",
            "h4": "BULLISH",
            "h1": "BULLISH",
            "m30": "BULLISH",
            "m15": "BULLISH",
        }

        result = monitor.evaluate(
            self.candles(),
            trends
        )

        self.assertIn(
            result["signal"],
            {"BUY", "SELL", "WAIT"}
        )

        self.assertIn("sl", result)
        self.assertIn("tp1", result)

        # Only one SL and one TP are allowed.
        self.assertNotIn("tp2", result)
        self.assertNotIn("tp3", result)

        # Four required techniques.
        self.assertEqual(
            set(result["techniques"]),
            {"snr", "smc", "pa", "breakout"}
        )

        if result["signal"] in {"BUY", "SELL"}:
            self.assertIsNotNone(result["entry"])
            self.assertIsNotNone(result["sl"])
            self.assertIsNotNone(result["tp1"])

            if result["signal"] == "BUY":
                self.assertLess(result["sl"], result["entry"])
                self.assertGreater(result["tp1"], result["entry"])

            elif result["signal"] == "SELL":
                self.assertGreater(result["sl"], result["entry"])
                self.assertLess(result["tp1"], result["entry"])

    # ========================================================
    # TEST 2: ECONOMIC CALENDAR
    # ========================================================

    def test_finance_calendar_maps_usd_events_without_api_key(self):
        payload = {
            "events": [
                {
                    "title": "US CPI",
                    "impact": "high",
                    "release_at_utc": "2026-10-22T12:30:00+00:00",
                    "consensus": "3.1%",
                    "prior": "3.0%",
                    "actual": "",
                    "country": "US",
                    "url": "https://example.com/cpi",
                },
                {
                    "title": "US Consumer Confidence",
                    "impact": "medium",
                    "date": "2026-10-23",
                    "country": "US",
                },
            ]
        }

        with patch.object(
            monitor,
            "get_json",
            return_value=payload
        ) as mock_get:
            result = monitor.fetch_economic_calendar()

        self.assertEqual(result["status"], "FRESH")

        self.assertEqual(
            result["source"],
            "Finance Calendar API"
        )

        # Both events match the current keyword and impact filters.
        self.assertEqual(
            len(result["events"]),
            2
        )

        self.assertEqual(
            result["events"][0]["event"],
            "US CPI"
        )

        self.assertEqual(
            result["events"][0]["importance"],
            3
        )

        self.assertEqual(
            result["events"][1]["event"],
            "US Consumer Confidence"
        )

        self.assertEqual(
            result["events"][1]["importance"],
            2
        )

        self.assertEqual(
            result["events"][0]["currency"],
            "USD"
        )

        self.assertIn(
            "country=US",
            mock_get.call_args.args[0]
        )

        self.assertIn(
            "Asia%2FKuala_Lumpur",
            mock_get.call_args.args[0]
        )

        self.assertEqual(
            result["attribution_url"],
            "https://www.financecalendar.com/"
        )

    # ========================================================
    # TEST 3: CALENDAR FAILURE MUST NOT BLOCK SIGNAL
    # ========================================================

    def test_news_lock_calendar_error_does_not_block_signal(self):
        calendar_error = {
            "status": "ERROR",
            "events": [],
            "message": "Calendar unavailable",
        }

        result = monitor.news_lock(calendar_error)

        self.assertEqual(
            result["status"],
            "CALENDAR_ERROR"
        )

        self.assertFalse(
            result["blocks_signal"]
        )

        self.assertIsNone(
            result["event"]
        )

        self.assertIn(
            "signal continues",
            result["message"].lower()
        )

    def test_news_lock_invalid_calendar_does_not_block_signal(self):
        result = monitor.news_lock(None)

        self.assertEqual(
            result["status"],
            "CALENDAR_ERROR"
        )

        self.assertFalse(
            result["blocks_signal"]
        )

    # ========================================================
    # TEST 4: VALID NEWS EVENT STILL BLOCKS NEW ENTRY
    # ========================================================

    def test_news_lock_blocks_within_thirty_minute_window(self):
        fixed_now = datetime(
            2026, 10, 29, 12, 0, 0,
            tzinfo=timezone.utc
        )

        event_time = fixed_now + timedelta(minutes=15)

        calendar = {
            "status": "FRESH",
            "events": [
                {
                    "date": event_time.isoformat(),
                    "event": "US Core PCE",
                    "currency": "USD",
                    "importance": 3,
                }
            ],
        }

        with patch.object(
            monitor,
            "now",
            return_value=fixed_now
        ):
            result = monitor.news_lock(calendar)

        self.assertEqual(
            result["status"],
            "NEWS_LOCK"
        )

        self.assertTrue(
            result["blocks_signal"]
        )

        self.assertEqual(
            result["event"],
            "US Core PCE"
        )

        self.assertEqual(
            result["minutes_to_event"],
            15
        )

    def test_news_lock_outside_window_does_not_block(self):
        fixed_now = datetime(
            2026, 10, 29, 12, 0, 0,
            tzinfo=timezone.utc
        )

        event_time = fixed_now + timedelta(minutes=45)

        calendar = {
            "status": "FRESH",
            "events": [
                {
                    "date": event_time.isoformat(),
                    "event": "US CPI",
                    "currency": "USD",
                    "importance": 3,
                }
            ],
        }

        with patch.object(
            monitor,
            "now",
            return_value=fixed_now
        ):
            result = monitor.news_lock(calendar)

        self.assertEqual(
            result["status"],
            "NORMAL"
        )

        self.assertFalse(
            result["blocks_signal"]
        )

    # ========================================================
    # TEST 5: ACTIVE SETUP RECOVERY
    # ========================================================

    def test_ensure_state_recovers_active_setups_from_history(self):
        active_buy = {
            "id": "MCD-BUY-10001",
            "side": "BUY",
            "status": "ACTIVE",
            "entry": 2300.0,
            "sl": 2295.0,
            "tp1": 2307.5,
            "created_candle_t": 10001,
        }

        completed_sell = {
            "id": "MCD-SELL-10002",
            "side": "SELL",
            "status": "TP_HIT",
            "entry": 2310.0,
            "sl": 2315.0,
            "tp1": 2302.5,
            "created_candle_t": 10002,
        }

        state = {
            "active_setups": [],
            "signal_history": [
                dict(active_buy),
                dict(completed_sell),
            ],
        }

        result = monitor.ensure_state(state)

        self.assertEqual(
            len(result["active_setups"]),
            1
        )

        self.assertEqual(
            result["active_setups"][0]["id"],
            "MCD-BUY-10001"
        )

        self.assertEqual(
            result["active_setups"][0]["status"],
            "ACTIVE"
        )

        # Completed setup must not be restored as active.
        active_ids = {
            item["id"] for item in result["active_setups"]
        }

        self.assertNotIn(
            "MCD-SELL-10002",
            active_ids
        )

        # Both records must remain in history.
        history_ids = {
            item["id"] for item in result["signal_history"]
        }

        self.assertIn(
            "MCD-BUY-10001",
            history_ids
        )

        self.assertIn(
            "MCD-SELL-10002",
            history_ids
        )

        self.assertNotIn(
            "active_setup",
            result
        )

    def test_ensure_state_migrates_legacy_active_setup(self):
        legacy_setup = {
            "id": "MCD-LEGACY-10003",
            "side": "SELL",
            "status": "ACTIVE",
            "entry": 2300.0,
            "sl": 2305.0,
            "tp1": 2292.5,
        }

        state = {
            "active_setup": legacy_setup,
            "signal_history": [],
        }

        result = monitor.ensure_state(state)

        self.assertEqual(
            len(result["active_setups"]),
            1
        )

        self.assertEqual(
            result["active_setups"][0]["id"],
            "MCD-LEGACY-10003"
        )

        self.assertNotIn(
            "active_setup",
            result
        )

    def test_ensure_state_rejects_invalid_active_setup(self):
        invalid_setup = {
            "id": "MCD-INVALID-10004",
            "side": "BUY",
            "status": "ACTIVE",
            "entry": 2300.0,
            "sl": 2305.0,
            "tp1": 2295.0,
        }

        state = {
            "active_setups": [invalid_setup],
            "signal_history": [],
        }

        result = monitor.ensure_state(state)

        self.assertEqual(
            result["active_setups"],
            []
        )

    # ========================================================
    # TEST 6: PERFORMANCE CONTRACT
    # ========================================================

    def test_performance_counts_only_terminal_outcomes(self):
        history = [
            {"id": "1", "status": "ACTIVE"},
            {"id": "2", "status": "TP_HIT"},
            {"id": "3", "status": "SL_HIT"},
            {"id": "4", "status": "TP_HIT"},
        ]

        result = monitor.calculate_performance(history)

        self.assertEqual(
            result["total_entries"],
            4
        )

        self.assertEqual(
            result["active_entries"],
            1
        )

        self.assertEqual(
            result["tp_hits"],
            2
        )

        self.assertEqual(
            result["sl_hits"],
            1
        )

        self.assertEqual(
            result["decided_entries"],
            3
        )

        self.assertAlmostEqual(
            result["win_rate_percent"],
            66.67,
            places=2
        )

    # ========================================================
    # TEST 7: DASHBOARD JSON CONTRACT
    # ========================================================

    def test_main_writes_dashboard_json_contract_without_external_network(self):
        candles = self.candles()

        quote = {
            "bid": candles[-1]["c"] - 0.1,
            "ask": candles[-1]["c"] + 0.1,
            "mid": candles[-1]["c"],
            "spread": 0.2,
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
            "age": 0,
        }

        calendar = {
            "status": "FRESH",
            "source": "Finance Calendar API",
            "updated_utc": datetime.now(
                timezone.utc
            ).isoformat(),
            "events": [],
            "message": "Calendar test fixture",
            "url": (
                "https://www.financecalendar.com/"
                "api/economic-calendar/"
            ),
            "attribution_url": (
                "https://www.financecalendar.com/"
            ),
            "timezone_note": (
                "MYT timezone test fixture."
            ),
        }

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()

            try:
                os.chdir(tmp)

                with (
                    patch.object(
                        monitor,
                        "get_quote",
                        return_value=quote
                    ),

                    patch.object(
                        monitor,
                        "get_bars",
                        return_value=candles
                    ),

                    patch.object(
                        monitor,
                        "get_trend",
                        return_value="BULLISH"
                    ),

                    patch.object(
                        monitor,
                        "fetch_economic_calendar",
                        return_value=calendar
                    ),

                    patch.object(
                        monitor,
                        "notify",
                        return_value=True
                    )
                ):
                    monitor.main()

                # Verify dashboard JSON.
                data = json.loads(
                    Path("signal_status.json").read_text(
                        encoding="utf-8"
                    )
                )

                self.assertEqual(
                    data["symbol"],
                    "XAUUSD"
                )

                self.assertEqual(
                    data["timeframe"],
                    "M5"
                )

                self.assertIn(
                    data["signal"],
                    {"BUY", "SELL", "WAIT"}
                )

                self.assertIn("sl", data)
                self.assertIn("tp1", data)

                # Only one TP.
                self.assertNotIn("tp2", data)
                self.assertNotIn("tp3", data)

                self.assertIn(
                    "economic_calendar",
                    data
                )

                self.assertIsInstance(
                    data["economic_calendar"]["events"],
                    list
                )

                self.assertEqual(
                    set(data["techniques"]),
                    {"snr", "smc", "pa", "breakout"}
                )

                self.assertIn(
                    "performance",
                    data
                )

                self.assertIn(
                    "active_setups",
                    data
                )

                self.assertIsInstance(
                    data["active_setups"],
                    list
                )

                self.assertIn(
                    "signal_history",
                    data
                )

                self.assertIsInstance(
                    data["signal_history"],
                    list
                )

                # Verify persistent state file.
                self.assertTrue(
                    Path(".mcd_signal_state.json").exists()
                )

            finally:
                os.chdir(old_cwd)

    # ========================================================
    # TEST 8: DASHBOARD CONTINUES WHEN CALENDAR FAILS
    # ========================================================

    def test_main_continues_when_calendar_fails(self):
        candles = self.candles()

        quote = {
            "bid": candles[-1]["c"] - 0.1,
            "ask": candles[-1]["c"] + 0.1,
            "mid": candles[-1]["c"],
            "spread": 0.2,
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
            "age": 0,
        }

        calendar_error = {
            "status": "ERROR",
            "source": "Finance Calendar API",
            "updated_utc": datetime.now(
                timezone.utc
            ).isoformat(),
            "events": [],
            "message": "Calendar unavailable",
        }

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()

            try:
                os.chdir(tmp)

                with (
                    patch.object(
                        monitor,
                        "get_quote",
                        return_value=quote
                    ),

                    patch.object(
                        monitor,
                        "get_bars",
                        return_value=candles
                    ),

                    patch.object(
                        monitor,
                        "get_trend",
                        return_value="BULLISH"
                    ),

                    patch.object(
                        monitor,
                        "fetch_economic_calendar",
                        return_value=calendar_error
                    ),

                    patch.object(
                        monitor,
                        "notify",
                        return_value=True
                    )
                ):
                    monitor.main()

                data = json.loads(
                    Path("signal_status.json").read_text(
                        encoding="utf-8"
                    )
                )

                # Calendar error must not become a data error.
                self.assertEqual(
                    data["data_status"],
                    "FRESH"
                )

                self.assertEqual(
                    data["news_impact"]["status"],
                    "CALENDAR_ERROR"
                )

                self.assertFalse(
                    data["news_impact"]["blocks_signal"]
                )

                self.assertIn(
                    "calendar_notice",
                    data
                )

                # The signal engine must still return a normal signal state.
                self.assertIn(
                    data["signal"],
                    {"BUY", "SELL", "WAIT"}
                )

            finally:
                os.chdir(old_cwd)


# ============================================================
# TEST RUNNER
# ============================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
