import math
import unittest
from datetime import date, timedelta
from decimal import Decimal

from tradenow.equity import EquityBar
from tradenow.features import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    compute_features,
    feature_code_sha256,
    snapshot,
)


def bars(count: int = 400) -> list[EquityBar]:
    result, day, index = [], date(2020, 1, 1), 0
    while index < count:
        if day.weekday() < 5:
            close = Decimal(str(round(150 + index * 0.05 + 6 * math.sin(index / 9), 2)))
            result.append(EquityBar(day, "GLD", close - Decimal("0.3"), close + 1,
                                    close - 1, close, 1_000_000 + (index * 7919) % 50_000))
            index += 1
        day += timedelta(days=1)
    return result


SERIES = bars()
ROWS = compute_features(SERIES)


class FeatureTests(unittest.TestCase):
    def test_no_row_uses_future_bars(self):
        # The row for day t must equal what was computable with history ending at t.
        for t in (0, 13, 14, 19, 20, 54, 55, 125, 199, 251, 252, 300, len(SERIES) - 1):
            with self.subTest(t=t):
                self.assertEqual(compute_features(SERIES[:t + 1])[-1], ROWS[t])

    def test_values_appear_only_after_their_warmup(self):
        first = {name: next(t for t, row in enumerate(ROWS) if row[name] is not None)
                 for name in FEATURE_NAMES}
        self.assertEqual(first["ret_1"], 1)
        self.assertEqual(first["ret_252"], 252)
        self.assertEqual(first["mom_12_1"], 252)
        self.assertEqual(first["sma_200"], 199)
        self.assertEqual(first["rsi_14"], 14)
        self.assertEqual(first["atr_14"], 14)
        self.assertEqual(first["vol_20"], 20)
        self.assertEqual(first["vol_20_median_252"], 20 + 251)
        self.assertEqual(first["donchian_high_55"], 55)
        self.assertEqual(first["drawdown_252"], 251)

    def test_definitions_by_hand(self):
        t = 300
        row, closes = ROWS[t], [bar.close for bar in SERIES]
        self.assertEqual(row["ret_20"], closes[t] / closes[t - 20] - 1)
        self.assertEqual(row["mom_12_1"], closes[t - 21] / closes[t - 252] - 1)
        self.assertEqual(row["sma_10"], sum(closes[t - 9:t + 1], Decimal(0)) / 10)
        self.assertEqual(row["donchian_high_55"], max(bar.high for bar in SERIES[t - 55:t]))
        self.assertEqual(row["donchian_low_20"], min(bar.low for bar in SERIES[t - 20:t]))
        self.assertEqual(row["drawdown_252"], closes[t] / max(closes[t - 251:t + 1]) - 1)
        self.assertTrue(0 <= row["rsi_14"] <= 100)
        self.assertGreater(row["vol_20"], 0)

    def test_rsi_is_100_when_price_only_rises(self):
        rising = [EquityBar(bar.date, "GLD", Decimal(100 + i), Decimal(101 + i),
                            Decimal(99 + i), Decimal(100 + i), 1000)
                  for i, bar in enumerate(SERIES[:30])]
        self.assertEqual(compute_features(rising)[-1]["rsi_14"], Decimal(100))

    def test_snapshot_for_any_date_is_versioned(self):
        day = SERIES[260].date
        result = snapshot(SERIES, day)
        self.assertEqual(result["feature_version"], FEATURE_VERSION)
        self.assertEqual(result["feature_code_sha256"], feature_code_sha256())
        self.assertEqual(result["features"]["ret_252"], str(ROWS[260]["ret_252"]))
        self.assertEqual(snapshot(SERIES[:261], day), result)
        with self.assertRaisesRegex(ValueError, "No GLD bar"):
            snapshot(SERIES, date(2019, 1, 1))


if __name__ == "__main__":
    unittest.main()
