import io
import json
import random
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tradenow.__main__ import main
from tradenow.equity_types import EquityBar, FeatureRow
from tradenow.experiments import MULTI_STRATEGY_FAMILY, read_experiments
from tradenow.features import FEATURE_NAMES, compute_features
from tradenow.live_gate import multi_research_gate
from tradenow.logs import close_logging
from tradenow.metrics import max_drawdown
from tradenow.multi_evaluation import (
    ROLL_SPAN,
    chain_curves,
    evaluate_multi,
    rolling_checks,
    run_selection,
    select_multi,
    universe_rows,
    window_starts,
)
from tradenow.multi_research import render_multi_markdown, run_multi
from tradenow.multi_strategies import (
    MULTI_CANDIDATES,
    MultiCandidate,
    capped_inverse_volatility,
    rule_holds,
)
from tradenow.portfolio import PortfolioConfig
from tradenow.strategies import History
from tradenow.universe import UNIVERSE, AssetHistory, Universe


D = Decimal


def synthetic_universe(count: int, seed: int = 1,
                       drifts: tuple[int, ...] = (3, -1, 2, 0, 1, -2),
                       noise: int = 15) -> Universe:
    """Six random-walk ETFs on one weekday calendar; drift and noise in 0.1% steps."""
    rng = random.Random(seed)
    days, day = [], date(2010, 1, 4)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    assets = {}
    for index, symbol in enumerate(UNIVERSE):
        price, bars = D(50 + 10 * index), []
        for day in days:
            open_ = price
            step = 1000 + rng.randint(-noise, noise) + drifts[index]
            price = max(D(5), (price * step / 1000).quantize(D("0.01")))
            bars.append(EquityBar(day, symbol, open_, max(open_, price), min(open_, price),
                                  price, 10000))
        assets[symbol] = AssetHistory(symbol, bars, {}, 0, 0, f"{symbol}.csv", symbol * 8)
    return Universe(UNIVERSE, days, assets, f"synthetic-{seed}-{count}")


def feature_row(**values: str | None) -> FeatureRow:
    row = dict.fromkeys(FEATURE_NAMES)
    row.update({name: None if value is None else D(value) for name, value in values.items()})
    return FeatureRow(date(2026, 1, 30), D(100), row)


def views(**rows: FeatureRow) -> dict[str, History]:
    return {symbol: History([row], 0) for symbol, row in rows.items()}


class CandidateTests(unittest.TestCase):
    def test_registered_candidates_are_pinned(self):
        self.assertEqual([item.name for item in MULTI_CANDIDATES],
                         ["mom_eq", "trend_eq", "both_eq", "mom_iv35", "trend_iv35",
                          "both_iv35"])
        with self.assertRaises(ValueError):
            MultiCandidate("mom", "iv50")

    def test_rules_treat_warmup_as_out(self):
        cases = {("0.1", "0.1"): (True, True, True), ("0.1", "-0.1"): (True, False, False),
                 ("0", "0.1"): (False, True, False), (None, "0.1"): (False, True, False),
                 ("0.1", None): (True, False, False)}
        for (momentum, trend), expected in cases.items():
            row = feature_row(mom_12_1=momentum, dist_sma_200=trend)
            with self.subTest(momentum=momentum, trend=trend):
                self.assertEqual(tuple(rule_holds(rule, row) for rule in ("mom", "trend", "both")),
                                 expected)

    def test_inverse_volatility_cap_shares_excess_proportionally(self):
        # Inverse 10, 5, 2.5, 2.5 -> 0.5, 0.25, 0.125, 0.125; A capped, 0.65 shared.
        self.assertEqual(capped_inverse_volatility(
            {"A": D("0.1"), "B": D("0.2"), "C": D("0.4"), "D": D("0.4")}),
            {"A": D("0.35"), "B": D("0.325"), "C": D("0.1625"), "D": D("0.1625")})

    def test_cap_repeats_until_none_exceeds_it(self):
        # After capping A, B's share rises to 0.448, so B is capped too.
        weights = capped_inverse_volatility({"A": D("0.1"), "B": D("0.15"), "C": D(1),
                                             "D": D(1), "E": D(1)})
        self.assertEqual(weights, {"A": D("0.35"), "B": D("0.35"), "C": D("0.1"),
                                   "D": D("0.1"), "E": D("0.1")})

    def test_warmup_and_too_few_assets(self):
        self.assertEqual(capped_inverse_volatility({"A": D("0.1"), "B": None, "C": D(0)}),
                         {"A": D("0.35")})  # the rest stays in cash
        thirds = capped_inverse_volatility({"A": D("0.2"), "B": D("0.2"), "C": D("0.2")})
        self.assertEqual(set(thirds.values()), {D("0.3333333333")})

    def test_out_assets_hold_cash_without_renormalizing(self):
        rows = {"A": feature_row(mom_12_1="0.1", dist_sma_200="0.1", vol_60="0.1"),
                "B": feature_row(mom_12_1="0.1", dist_sma_200="-0.1", vol_60="0.2"),
                "C": feature_row(mom_12_1="-0.1", dist_sma_200="0.1", vol_60="0.4"),
                "D": feature_row(mom_12_1="0.1", dist_sma_200="0.1", vol_60=None)}
        both_iv = MultiCandidate("both", "iv35")(views(**rows))
        self.assertEqual(both_iv, {"A": D("0.35")})  # B and C keep their share in cash
        # D has no volatility yet, so A, B, C share: B's 0.433 after A's cap is capped too.
        self.assertEqual(MultiCandidate("mom", "iv35")(views(**rows)),
                         {"A": D("0.35"), "B": D("0.35")})
        self.assertEqual(MultiCandidate("mom", "eq")(views(**rows)),
                         dict.fromkeys(("A", "B", "D"), D("0.25")))


class NoLookAheadTests(unittest.TestCase):
    def test_weights_from_full_history_match_truncated_history(self):
        universe = synthetic_universe(420)
        full = universe_rows(universe)
        for day in (300, 350, 419):
            truncated = {symbol: compute_features(universe.bars(symbol)[:day + 1])
                         for symbol in UNIVERSE}
            for candidate in MULTI_CANDIDATES:
                with self.subTest(day=day, candidate=candidate.name):
                    self.assertEqual(
                        candidate({s: History(full[s], day) for s in UNIVERSE}),
                        candidate({s: History(truncated[s], day) for s in UNIVERSE}))

    def test_changing_later_bars_leaves_earlier_windows_unchanged(self):
        universe = synthetic_universe(1100)
        cut = ROLL_SPAN + 126  # the second window's test block ends here
        changed_assets = {}
        for symbol, asset in universe.assets.items():
            bars = [bar if index < cut else replace(bar, open=bar.open * 2, close=bar.close * 3,
                                                    high=bar.high * 3)
                    for index, bar in enumerate(asset.bars)]
            changed_assets[symbol] = replace(asset, bars=bars)
        changed = replace(universe, assets=changed_assets)
        config = PortfolioConfig()
        original = rolling_checks(universe, universe_rows(universe), config, 1100)["windows"]
        altered = rolling_checks(changed, universe_rows(changed), config, 1100)["windows"]
        self.assertEqual(original[:2], altered[:2])
        self.assertNotEqual(original[2:], altered[2:])


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.universe = synthetic_universe(1200)
        cls.report = run_multi(cls.universe)

    def test_windows_are_disjoint_and_end_before_the_holdout(self):
        self.assertEqual(window_starts(1000), [0, 126])
        evaluation = self.report["evaluation"]
        windows = evaluation["rolling_pre_holdout"]["windows"]
        self.assertEqual(len(windows), 2)
        holdout_start = evaluation["periods"]["holdout"]["first_date"]
        dates = [day.isoformat() for day in self.universe.dates]
        for earlier, later in zip(windows, windows[1:], strict=False):
            self.assertEqual(dates.index(later["test_start"]),
                             dates.index(earlier["test_end"]) + 1)
        self.assertLess(windows[-1]["test_end"], holdout_start)
        self.assertEqual(evaluation["periods"]["holdout"]["bars"], 240)

    def test_stress_run_charges_everyone_ten_cents(self):
        evaluation = self.report["evaluation"]
        normal = evaluation["rolling_pre_holdout"]["summary"]
        stressed = evaluation["rolling_pre_holdout_stressed"]
        self.assertEqual(stressed["slippage_per_share"], "0.10")
        for key in ("compounded_b1_return_pct", "compounded_b2_return_pct"):
            self.assertLess(D(stressed["summary"][key]), D(normal[key]))

    def test_identical_inputs_give_byte_identical_reports(self):
        again = run_multi(synthetic_universe(1200))
        self.assertEqual(json.dumps(self.report, indent=2), json.dumps(again, indent=2))
        self.assertEqual(render_multi_markdown(self.report), render_multi_markdown(again))
        self.assertEqual(self.report["candidates"], [c.name for c in MULTI_CANDIDATES])
        self.assertEqual(self.report["live_gate"]["research"]["candidates_evaluated"], 6)

    def test_requires_the_registered_universe_and_enough_bars(self):
        with self.assertRaises(ValueError):
            run_multi(replace(self.universe, symbols=tuple(reversed(UNIVERSE))))
        with self.assertRaises(ValueError):
            evaluate_multi(synthetic_universe(900))


class SelectionTests(unittest.TestCase):
    def test_falling_markets_select_cash(self):
        universe = synthetic_universe(700, drifts=(-3,) * 6, noise=2)
        rows = universe_rows(universe)
        choice = select_multi(universe, rows, PortfolioConfig(), (0, 504), (504, 630))
        self.assertIsNone(choice.selected)
        self.assertEqual(choice.best.name, "mom_eq")  # ties go to the first registered
        result = run_selection(universe, rows, PortfolioConfig(), choice, 630, 700)
        self.assertEqual((result["fills"], result["total_return_pct"]), ([], "0.000"))


class ChainTests(unittest.TestCase):
    def test_daily_curves_chain_from_each_window_end(self):
        def run(*equity: int) -> dict:
            return {"starting_cash": "100",
                    "equity_curve": [{"equity": str(value)} for value in equity]}
        chained = chain_curves([run(110, 99), run(90, 120)])
        self.assertEqual(chained, [D("1.1"), D("0.99"), D("0.891"), D("1.188")])
        self.assertEqual(max_drawdown(D(1), chained), D("0.19"))


def gate_evaluation(**changes: object) -> dict:
    summary = {"windows": 10, "beat_both_windows": 6, "closed_trades": 30,
               "compounded_return_pct": "50", "compounded_b1_return_pct": "40",
               "compounded_b2_return_pct": "45", "chained_max_drawdown_pct": "10",
               "chained_b1_max_drawdown_pct": "10", "chained_b2_max_drawdown_pct": "20"}
    stressed = {"compounded_return_pct": "46", "compounded_b1_return_pct": "39",
                "compounded_b2_return_pct": "45"}
    for key, value in changes.items():
        if key.startswith("stressed_"):
            stressed[key.removeprefix("stressed_")] = str(value)
        else:
            summary[key] = value if isinstance(value, int) else str(value)
    return {"rolling_pre_holdout": {"summary": summary},
            "rolling_pre_holdout_stressed": {"slippage_per_share": "0.10",
                                             "summary": stressed}}


class GateTests(unittest.TestCase):
    def failing(self, **changes: object) -> list[str]:
        gate = multi_research_gate(gate_evaluation(**changes), 6)
        return [item["check"] for item in gate["checks"] if not item["passed"]]

    def test_edges_pass(self):
        self.assertEqual(self.failing(), [])
        self.assertTrue(multi_research_gate(gate_evaluation(), 6)["passed"])

    def test_each_check_fails_on_its_own(self):
        cases = {
            "R1_BEATS_BOTH_BENCHMARKS": {"compounded_return_pct": "45"},
            "R2_BEATS_CONSISTENTLY": {"beat_both_windows": 5},
            "R3_ENOUGH_TRADES": {"closed_trades": 29},
            "R4_SURVIVES_COSTS": {"stressed_compounded_return_pct": "44"},
            "R5_NO_DEEPER_DRAWDOWN": {"chained_max_drawdown_pct": "10.001"},
        }
        for check, changes in cases.items():
            with self.subTest(check=check):
                self.assertEqual(self.failing(**changes), [check])

    def test_beating_only_one_benchmark_fails(self):
        self.assertEqual(self.failing(compounded_b2_return_pct="60"),
                         ["R1_BEATS_BOTH_BENCHMARKS"])
        self.assertEqual(self.failing(chained_b2_max_drawdown_pct="9"),
                         ["R5_NO_DEEPER_DRAWDOWN"])

    def test_no_windows_fails(self):
        self.assertIn("R1_BEATS_BOTH_BENCHMARKS", self.failing(windows=0))


class CommandTests(unittest.TestCase):
    def test_multi_writes_reports_and_logs_one_experiment(self):
        universe = synthetic_universe(1000)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(close_logging)  # releases the log file before cleanup (Windows)
        root = Path(directory.name)
        output = root / "multi"
        with patch.dict("os.environ", {"TRADENOW_DATA_DIR": str(root)}), \
                patch("tradenow.__main__.load_universe", return_value=universe), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["multi", "--output", str(output)]), 0)
            self.assertEqual(main(["multi", "--output", str(output)]), 0)
        printed = json.loads(stdout.getvalue().split("\n}\n")[0] + "\n}")
        self.assertEqual(printed["candidates_evaluated"], 6)
        self.assertEqual(printed["live_gate"]["verdict"], "FAIL")
        self.assertEqual(len(list(output.glob("report-*.json"))), 1)
        self.assertEqual(len(list(output.glob("report-*.md"))), 1)
        records = read_experiments(root / "experiments.jsonl")
        self.assertEqual(len(records), 1)  # the identical re-run adds nothing
        self.assertEqual(records[0]["strategy_version"], MULTI_STRATEGY_FAMILY)
        self.assertEqual(records[0]["candidates_evaluated"], 6)
        runs = [json.loads(line) for line in
                (root / "logs" / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(run["command"], run["outcome"]) for run in runs],
                         [("multi", "OK"), ("multi", "OK")])


if __name__ == "__main__":
    unittest.main()
