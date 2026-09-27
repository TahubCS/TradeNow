import csv
import hashlib
import json
import unittest
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from tests.test_tiingo import private_test_dir
from tradenow.gld_research import latest_imported_gld, run_gld_csv
from tradenow.tiingo import HEADER


def sample_gld_csv(count: int = 180) -> bytes:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(HEADER)
    day = date(2025, 1, 1)
    for index in range(count):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        price = Decimal("100") + Decimal(index) / 10
        raw = (str(price), str(price + 1), str(price - 1), str(price))
        writer.writerow((day.isoformat(), "GLD", *raw, 1000, *raw, 1000, "0", "1"))
        day += timedelta(days=1)
    return output.getvalue().encode("utf-8")


class GldResearchTests(unittest.TestCase):
    def test_research_selects_on_validation_then_replays_holdout(self):
        source = sample_gld_csv()
        report = run_gld_csv(source, "fixture.csv")
        self.assertEqual(report, run_gld_csv(source, "fixture.csv"))
        self.assertEqual(report["mode"], "offline_gld_simulation")
        self.assertEqual(report["data"]["bars"], 180)
        self.assertEqual(len(report["research"]["hypotheses"]), 3)
        periods = report["data"]["periods"]
        self.assertLess(periods["development"]["last_date"],
                        periods["validation"]["first_date"])
        self.assertLess(periods["validation"]["last_date"],
                        periods["holdout"]["first_date"])
        holdout = report["research"]["holdout_result"]
        self.assertGreater(len(holdout["fills"]), 0)
        self.assertTrue(all(Decimal(day["cash"]) >= 0 for day in holdout["equity_curve"]))
        self.assertTrue(all(any(decision["date"] == fill["date"] and
                                decision["action"] == fill["side"] and
                                decision["approved"]
                                for decision in holdout["risk_decisions"])
                            for fill in holdout["fills"]))

    def test_rejects_corporate_actions_and_duplicate_dates(self):
        source = sample_gld_csv()
        with self.assertRaisesRegex(ValueError, "corporate actions"):
            run_gld_csv(source.replace(b",0,1\n", b",1,1\n", 1))
        rows = source.splitlines()
        rows[2] = rows[1]
        with self.assertRaisesRegex(ValueError, "increasing weekdays"):
            run_gld_csv(b"\n".join(rows) + b"\n")

    def test_rejects_short_file_and_wrong_instrument(self):
        with self.assertRaisesRegex(ValueError, "at least 180"):
            run_gld_csv(sample_gld_csv(179))
        with self.assertRaisesRegex(ValueError, "GLD only"):
            run_gld_csv(sample_gld_csv().replace(b",GLD,", b",MGC,", 1))

    def test_private_import_must_match_manifest_hash(self):
        source = sample_gld_csv()
        with private_test_dir() as directory:
            stem = "GLD-20250101-20251231"
            (directory / f"{stem}.csv").write_bytes(source)
            manifest = {"provider": "Tiingo", "symbol": "GLD",
                        "bars_sha256": hashlib.sha256(source).hexdigest()}
            (directory / f"{stem}.manifest.json").write_text(json.dumps(manifest))
            self.assertEqual(latest_imported_gld(directory), (source, f"{stem}.csv"))
            (directory / f"{stem}.csv").write_bytes(source + b"\n")
            with self.assertRaisesRegex(ValueError, "does not match"):
                latest_imported_gld(directory)


if __name__ == "__main__":
    unittest.main()
