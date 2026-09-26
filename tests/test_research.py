import unittest
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tradenow.market_data import load_bars
from tradenow.simulation import Config, simulate


SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "mgc_synthetic.csv"


class ResearchTests(unittest.TestCase):
    def test_sample_is_deterministic_and_fills_after_signal(self):
        bars = load_bars(SAMPLE)
        first = simulate(bars)
        self.assertEqual(first, simulate(bars))
        self.assertEqual(first["mode"], "offline_simulation")
        self.assertEqual(first["fills"][0]["date"], "2026-09-10")
        self.assertEqual(first["fills"][0]["side"], "BUY")
        self.assertEqual(first["open_contracts"], 0)
        self.assertEqual(first["ending_equity"], "99895.00")

    def test_risk_rejects_excess_notional(self):
        result = simulate(load_bars(SAMPLE), Config(max_notional_fraction=Decimal("0.01")))
        self.assertEqual(result["fills"], [])
        self.assertTrue(any(not decision["approved"] for decision in result["risk_decisions"]))

    def test_invalid_bars_fail_before_simulation(self):
        text = SAMPLE.read_text(encoding="utf-8")
        rows = [row for row in text.splitlines() if row]
        duplicate = "\n".join(rows + [rows[-1]]) + "\n"
        with patch.object(Path, "open", return_value=StringIO(duplicate)):
            with self.assertRaisesRegex(ValueError, "unique and increasing"):
                load_bars(Path("bad.csv"))

    def test_same_day_close_cannot_change_that_days_fill(self):
        bars = load_bars(SAMPLE)
        original = simulate(bars)
        changed = bars.copy()
        changed[6] = replace(changed[6], close=Decimal("2011"))
        other = simulate(changed)
        self.assertEqual(original["fills"][0], other["fills"][0])


if __name__ == "__main__":
    unittest.main()
