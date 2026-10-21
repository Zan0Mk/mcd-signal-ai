import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import mcd_monitor as monitor  # noqa: E402


class MCDContractTests(unittest.TestCase):
    def candles(self, count=100):
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        rows = []
        base = 2300.0
        # Stable synthetic M5 OHLC series, used only for code-contract tests.
        for i in range(count):
            close = base + i * 0.08
            open_ = close - 0.04
            rows.append({
                "t": now_epoch - (count - i) * 300,
                "o": open_, "h": close + 0.08, "l": open_ - 0.08,
                "c": close, "is_open": False,
            })
        return rows

    def test_evaluate_uses_one_sl_one_tp_and_four_techniques(self):
        trends = {"d1": "BULLISH", "h4": "BULLISH", "h1": "BULLISH", "m30": "BULLISH", "m15": "BULLISH"}
        result = monitor.evaluate(self.candles(), trends)
        self.assertIn(result["signal"], {"BUY", "SELL", "WAIT"})
        self.assertIn("sl", result)
        self.assertIn("tp1", result)
        self.assertNotIn("tp2", result)
        self.assertNotIn("tp3", result)
        self.assertEqual(set(result["techniques"]), {"snr", "smc", "pa", "breakout"})
        if result["signal"] in {"BUY", "SELL"}:
            self.assertIsNotNone(result["sl"])
            self.assertIsNotNone(result["tp1"])


    def test_finance_calendar_maps_usd_events_without_api_key(self):
        payload = {
            "events": [
                {"title": "US CPI", "impact": "high", "release_at_utc": "2026-10-22T12:30:00+00:00", "consensus": "3.1%", "prior": "3.0%", "actual": "", "country": "US", "url": "https://example.com/cpi"},
                {"title": "US Consumer Confidence", "impact": "medium", "date": "2026-10-23", "country": "US"},
            ]
        }
        with patch.object(monitor, "get_json", return_value=payload) as mock_get:
            result = monitor.fetch_economic_calendar()
        self.assertEqual(result["status"], "FRESH")
        self.assertEqual(result["source"], "Finance Calendar API")
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["event"], "US CPI")
        self.assertEqual(result["events"][0]["importance"], 3)
        self.assertIn("country=US", mock_get.call_args.args[0])
        self.assertIn("Asia%2FKuala_Lumpur", mock_get.call_args.args[0])
        self.assertEqual(result["attribution_url"], "https://www.financecalendar.com/")

    def test_main_writes_dashboard_json_contract_without_external_network(self):
        candles = self.candles()
        quote = {
            "bid": candles[-1]["c"] - 0.1,
            "ask": candles[-1]["c"] + 0.1,
            "mid": candles[-1]["c"],
            "spread": 0.2,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "age": 0,
        }
        calendar = {
            "status": "FRESH", "source": "Finance Calendar API",
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "events": [], "message": "Calendar test fixture", "url": "https://www.financecalendar.com/api/economic-calendar/",
            "attribution_url": "https://www.financecalendar.com/",
            "timezone_note": "MYT timezone test fixture.",
        }
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                with patch.object(monitor, "get_quote", return_value=quote), \
                     patch.object(monitor, "fetch_candles", return_value=(candles, "Synthetic test")), \
                     patch.object(monitor, "get_trend", return_value="BULLISH"), \
                     patch.object(monitor, "fetch_economic_calendar", return_value=calendar), \
                     patch.object(monitor, "notify", return_value=True):
                    monitor.main()
                data = json.loads(Path("signal_status.json").read_text(encoding="utf-8"))
                self.assertEqual(data["symbol"], "XAUUSD")
                self.assertEqual(data["timeframe"], "M5")
                self.assertIn(data["signal"], {"BUY", "SELL", "WAIT"})
                self.assertIn("sl", data)
                self.assertIn("tp1", data)
                self.assertNotIn("tp2", data)
                self.assertNotIn("tp3", data)
                self.assertIn("economic_calendar", data)
                self.assertIsInstance(data["economic_calendar"]["events"], list)
                self.assertEqual(set(data["techniques"]), {"snr", "smc", "pa", "breakout"})
                self.assertIn("performance", data)
                self.assertTrue(Path(".mcd_signal_state.json").exists())
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
