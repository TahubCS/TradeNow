import unittest
import hashlib
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tradenow.execution import ApprovedOrder, SimulatedBroker
from tradenow.market_data import load_bars, parse_bars
from tradenow.offline import run_local_csv, run_offline, save_offline
from tradenow.simulation import Config, simulate
from tradenow.stress import audit_simulation, run_stress
from tradenow.synthetic import bars_to_csv, generate_bars


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
        self.assertEqual(first["total_pnl"], "-105.00")
        self.assertEqual(first["closed_trades"][0]["net_pnl"], "-105.00")
        self.assertEqual(first["equity_curve"][-1]["equity"], first["ending_equity"])
        self.assertFalse(first["halted"])

    def test_risk_rejects_excess_notional(self):
        result = simulate(load_bars(SAMPLE), Config(max_notional_fraction=Decimal("0.01")))
        self.assertEqual(result["fills"], [])
        self.assertTrue(any(not decision["approved"] for decision in result["risk_decisions"]))

    def test_drawdown_halts_entries_and_closes_at_next_open(self):
        result = simulate(load_bars(SAMPLE),
                          Config(max_drawdown_fraction=Decimal("0.0009")))
        self.assertTrue(result["halted"])
        self.assertEqual(result["fills"][-1]["date"], "2026-09-15")
        self.assertEqual(result["fills"][-1]["side"], "SELL")
        self.assertEqual(result["open_contracts"], 0)
        self.assertTrue(any(decision["reason"] == "DRAWDOWN_LIMIT"
                            for decision in result["risk_decisions"]))
        self.assertTrue(all(signal["target_contracts"] == 0
                            for signal in result["signals"] if signal["date"] >= "2026-09-14"))

    def test_final_bar_halt_reports_unclosed_position(self):
        bars = load_bars(SAMPLE)[:8]
        bars[-1] = replace(bars[-1], low=Decimal("1980"), close=Decimal("1980"))
        result = simulate(bars, Config(max_drawdown_fraction=Decimal("0.001")))
        self.assertTrue(result["halted"])
        self.assertEqual(result["open_contracts"], 1)
        self.assertEqual([fill["side"] for fill in result["fills"]], ["BUY"])

    def test_invalid_bars_fail_before_simulation(self):
        text = SAMPLE.read_text(encoding="utf-8")
        rows = [row for row in text.splitlines() if row]
        duplicate = "\n".join(rows + [rows[-1]]) + "\n"
        with patch.object(Path, "open", return_value=StringIO(duplicate)):
            with self.assertRaisesRegex(ValueError, "unique and increasing"):
                load_bars(Path("bad.csv"))

    def test_nonfinite_price_is_rejected(self):
        csv_text = "date,contract,open,high,low,close,volume\n2026-09-01,MGC_SIM,NaN,2010,1990,2000,100\n"
        with self.assertRaisesRegex(ValueError, "finite and positive"):
            parse_bars(StringIO(csv_text))

    def test_same_day_close_cannot_change_that_days_fill(self):
        bars = load_bars(SAMPLE)
        original = simulate(bars)
        changed = bars.copy()
        changed[6] = replace(changed[6], close=Decimal("2011"))
        other = simulate(changed)
        self.assertEqual(original["fills"][0], other["fills"][0])

    def test_simulated_order_id_is_idempotent(self):
        broker = SimulatedBroker(Decimal("100000"), Decimal("10"),
                                 Decimal("0.10"), Decimal("1.50"))
        order = ApprovedOrder("order-1", "2026-09-10", "BUY", Decimal("2000"))
        first = broker.submit(order)
        self.assertEqual(first, broker.submit(order))
        self.assertEqual(len(broker.fills), 1)
        self.assertEqual(broker.cash, Decimal("99998.50"))
        with self.assertRaisesRegex(ValueError, "reused"):
            broker.submit(replace(order, opening_price=Decimal("2001")))
        broker.reconcile()
        broker.fills.clear()
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            broker.reconcile()

    def test_offline_research_is_replayable_and_keeps_holdout_separate(self):
        report, csv_text = run_offline()
        self.assertEqual((report, csv_text), run_offline())
        self.assertEqual(report["mode"], "offline_simulation")
        self.assertEqual(report["data"]["bars"], 360)
        self.assertEqual(len(report["research"]["hypotheses"]), 3)
        self.assertEqual(report["research"]["selected_hypothesis"], "sma_3_10")
        self.assertGreater(report["research"]["holdout_summary"]["fills"], 0)
        periods = report["data"]["periods"]
        self.assertLess(periods["development"]["last_date"], periods["validation"]["first_date"])
        self.assertLess(periods["validation"]["last_date"], periods["holdout"]["first_date"])

    def test_offline_selection_can_choose_no_trade(self):
        report, _ = run_offline(seed=7)
        self.assertIsNone(report["research"]["selected_hypothesis"])
        self.assertEqual(report["research"]["holdout_summary"]["fills"], 0)

    def test_offline_rejects_too_short_dataset(self):
        with self.assertRaisesRegex(ValueError, "at least 180"):
            run_offline(days=40)

    def test_local_csv_replays_and_preserves_source_bytes(self):
        source = b"\xef\xbb\xbf" + bars_to_csv(generate_bars(3, 180)).replace("\n", "\r\n").encode()
        report, original = run_local_csv(source, r"C:\data\mgc-history.csv")
        self.assertEqual(original, source)
        self.assertEqual(report["data"]["source"], "local_csv")
        self.assertEqual(report["data"]["source_name"], "mgc-history.csv")
        self.assertEqual(report["data"]["sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual(report["data"]["bars"], 180)
        self.assertEqual(report["data"]["contract"], "MGC_SIM")
        self.assertEqual(report, run_local_csv(source, "mgc-history.csv")[0])
        with (patch.object(Path, "mkdir"), patch.object(Path, "write_bytes") as write_bytes,
              patch.object(Path, "write_text")):
            save_offline(report, original, Path("artifacts/test-output"))
        write_bytes.assert_called_once_with(source)

    def test_local_csv_rejects_invalid_inputs(self):
        with self.assertRaisesRegex(ValueError, "at least 180"):
            run_local_csv(SAMPLE.read_bytes())
        valid = bars_to_csv(generate_bars(3, 180)).encode()
        rows = valid.splitlines()
        rows[90] = rows[90].replace(b"MGC_SIM", b"MGCZ26")
        with self.assertRaisesRegex(ValueError, "returns after a roll"):
            run_local_csv(b"\n".join(rows) + b"\n")
        with self.assertRaisesRegex(ValueError, "MGC contracts only"):
            run_local_csv(valid.replace(b"MGC_SIM", b"GC_SIM"))
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            run_local_csv(b"\xff")

    def test_stress_suite_covers_trade_no_trade_and_faults(self):
        report = run_stress()
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["seed_runs"]), 12)
        self.assertEqual(len(report["fault_cases"]), 7)
        self.assertGreater(report["coverage"]["strategies_selected"], 0)
        self.assertGreater(report["coverage"]["no_trade_selections"], 0)
        self.assertGreater(report["coverage"]["negative_holdouts"], 0)

    def test_stress_audit_rejects_fill_without_risk_approval(self):
        result = simulate(load_bars(SAMPLE))
        result["risk_decisions"] = []
        with self.assertRaisesRegex(AssertionError, "lacks risk approval"):
            audit_simulation(result)


if __name__ == "__main__":
    unittest.main()
