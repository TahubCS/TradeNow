import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from test_multi import synthetic_universe

from tradenow.__main__ import main
from tradenow.equity_types import FeatureRow
from tradenow.experiments import read_experiments
from tradenow.features import FEATURE_NAMES, compute_features
from tradenow.logs import close_logging
from tradenow.ml_dataset import FEATURES, Sample, month_end_positions, training_samples
from tradenow.ml_models import fit_predict
from tradenow.ml_research import ML_STUDY
from tradenow.ml_strategies import ML_CANDIDATES, MlCandidate, Predictor
from tradenow.multi_evaluation import universe_rows
from tradenow.multi_research import run_study
from tradenow.strategies import History
from tradenow.universe import UNIVERSE


D = Decimal
DAYS = [date(2026, 1, 29), date(2026, 1, 30), date(2026, 2, 2), date(2026, 2, 27),
        date(2026, 3, 2), date(2026, 3, 31), date(2026, 4, 1)]


def row(day: date, close: int | str, **overrides: str | None) -> FeatureRow:
    values: dict[str, Decimal | None] = dict.fromkeys(FEATURE_NAMES)
    values.update(dict.fromkeys(FEATURES, D("0.1")))
    values["vol_60"] = D("0.2")
    values.update({name: None if value is None else D(value)
                   for name, value in overrides.items()})
    return FeatureRow(day, D(close), values)


def sample(x: float, label: float) -> Sample:
    return Sample("GLD", date(2020, 1, 31), date(2020, 2, 28),
                  (D(str(x)),) + (D(0),) * (len(FEATURES) - 1), D(str(label)))


class CandidateListTests(unittest.TestCase):
    def test_registered_candidates_are_pinned(self):
        self.assertEqual([item.name for item in ML_CANDIDATES],
                         ["ridge_eq", "ridge_iv35", "knn_eq", "knn_iv35"])
        self.assertEqual(ML_CANDIDATES[2].parameters,
                         {"model": "KNeighborsRegressor(n_neighbors=50)", "sizing": "eq",
                          "in_when": "prediction > 0"})
        self.assertEqual(ML_STUDY.registered_trials_total, 22)
        with self.assertRaises(ValueError):
            MlCandidate("ridge", "eq", Predictor("knn"))


class DatasetTests(unittest.TestCase):
    ROWS = [row(DAYS[0], 100), row(DAYS[1], 100), row(DAYS[2], 105), row(DAYS[3], 110),
            row(DAYS[4], 108), row(DAYS[5], 99), row(DAYS[6], 99)]

    def test_month_ends_need_the_next_day(self):
        self.assertEqual(month_end_positions(self.ROWS), [1, 3, 5])
        self.assertEqual(month_end_positions(self.ROWS[:6]), [1, 3])

    def test_labels_are_volatility_scaled_next_month_returns(self):
        samples = training_samples({"GLD": History(self.ROWS, 6)})
        # (110 / 100 - 1) / 0.2 and (99 / 110 - 1) / 0.2.
        self.assertEqual([(s.start, s.end, s.label) for s in samples],
                         [(DAYS[1], DAYS[3], D("0.5")), (DAYS[3], DAYS[5], D("-0.5"))])
        self.assertEqual(samples[0].features, (D("0.1"),) * 11 + (D("0.2"), D("0.1")))

    def test_label_ending_on_the_signal_day_is_not_used(self):
        samples = training_samples({"GLD": History(self.ROWS, 5)})
        self.assertEqual([s.end for s in samples], [DAYS[3]])

    def test_incomplete_features_are_skipped(self):
        rows = list(self.ROWS)
        rows[3] = row(DAYS[3], 110, rsi_14=None)
        self.assertEqual([s.start for s in training_samples({"GLD": History(rows, 6)})],
                         [DAYS[1]])


class ModelTests(unittest.TestCase):
    SAMPLES = [sample(x / 50, x / 50) for x in range(-50, 51)]

    def test_models_learn_a_simple_relation(self):
        current = {"UP": (D("0.9"),) + (D(0),) * 12, "DOWN": (D("-0.9"),) + (D(0),) * 12}
        for model in ("ridge", "knn"):
            with self.subTest(model=model):
                predicted = fit_predict(model, self.SAMPLES, current)
                self.assertGreater(predicted["UP"], 0)
                self.assertLess(predicted["DOWN"], 0)
                self.assertTrue(all(value == value.quantize(D("1e-10"))
                                    for value in predicted.values()))
        # The 50 nearest labels to 0.9 are 0.02 to 1.0: their mean is 0.51.
        self.assertEqual(fit_predict("knn", self.SAMPLES, current)["UP"], D("0.51"))

    def test_nearest_neighbours_needs_fifty_samples(self):
        with self.assertRaises(ValueError):
            fit_predict("knn", self.SAMPLES[:49], {"A": (D(0),) * 13})


class FixedPredictor(Predictor):
    def __init__(self, predictions: dict[str, Decimal]):
        super().__init__("ridge")
        self.predictions = predictions

    def __call__(self, views):
        return self.predictions


class AllocatorTests(unittest.TestCase):
    def test_positive_predictions_are_in_and_sized(self):
        rows = {symbol: [row(DAYS[0], 100, vol_60=vol)]
                for symbol, vol in (("A", "0.1"), ("B", "0.2"), ("C", "0.4"), ("D", "0.4"))}
        views = {symbol: History(items, 0) for symbol, items in rows.items()}
        predictor = FixedPredictor({"A": D("0.3"), "B": D(0), "C": D("0.01")})
        self.assertEqual(MlCandidate("ridge", "eq", predictor)(views),
                         {"A": D("0.25"), "C": D("0.25")})
        # iv35 over all four with a volatility; B (zero) and D (no prediction) stay cash.
        self.assertEqual(MlCandidate("ridge", "iv35", predictor)(views),
                         {"A": D("0.35"), "C": D("0.1625")})

    def test_no_predictions_while_warming_up(self):
        universe = synthetic_universe(400)
        rows = universe_rows(universe)
        views = {s: History(rows[s], 399) for s in UNIVERSE}
        self.assertEqual(Predictor("ridge")(views), {})  # under 24 label months
        self.assertEqual(ML_CANDIDATES[0](views), {})


class NoLookAheadTests(unittest.TestCase):
    def test_predictions_match_history_cut_at_the_signal_day(self):
        universe = synthetic_universe(860, noise=20)
        full = universe_rows(universe)
        for day in (800, 830, 859):
            cut = {s: compute_features(universe.bars(s)[:day + 1]) for s in UNIVERSE}
            for predictor in (ML_CANDIDATES[0].predictor, ML_CANDIDATES[2].predictor):
                with self.subTest(day=day, model=predictor.model):
                    # Alternating full and cut views also proves the cache resets.
                    on_full = predictor({s: History(full[s], day) for s in UNIVERSE})
                    on_cut = predictor({s: History(cut[s], day) for s in UNIVERSE})
                    self.assertEqual(set(on_full), set(UNIVERSE))
                    self.assertEqual(on_full, on_cut)
                    self.assertEqual(on_full, Predictor(predictor.model)(
                        {s: History(cut[s], day) for s in UNIVERSE}))


class StudyTests(unittest.TestCase):
    def test_identical_inputs_give_byte_identical_reports(self):
        first = run_study(synthetic_universe(1000, noise=20), ML_STUDY)
        again = run_study(synthetic_universe(1000, noise=20), ML_STUDY)
        self.assertEqual(json.dumps(first, indent=2), json.dumps(again, indent=2))
        self.assertEqual(first["candidates"], [item.name for item in ML_CANDIDATES])
        self.assertEqual((first["mode"], first["adr"]), ("ml_research", "ADR-012"))

    def test_ml_command_writes_reports_and_its_own_experiment_family(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(close_logging)
        root = Path(directory.name)
        with patch.dict("os.environ", {"TRADENOW_DATA_DIR": str(root)}), \
                patch("tradenow.__main__.load_universe",
                      return_value=synthetic_universe(1000)), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["ml", "--output", str(root / "ml")]), 0)
        printed = json.loads(stdout.getvalue())
        self.assertEqual((printed["adr"], printed["registered_trials_total"]), ("ADR-012", 22))
        self.assertEqual(len(list((root / "ml").glob("report-*.md"))), 1)
        record = read_experiments(root / "experiments.jsonl")[0]
        self.assertEqual(record["strategy_version"], "ml_adr012_candidates_v1")
        self.assertEqual(record["candidates_evaluated"], 4)

    def test_missing_model_libraries_fail_with_instructions(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(close_logging)
        root = Path(directory.name)
        stderr = io.StringIO()
        # A None entry makes the import fail, as if the ml extra were not installed.
        with patch.dict("os.environ", {"TRADENOW_DATA_DIR": str(root)}), \
                patch.dict(sys.modules, {"tradenow.ml_research": None}), \
                patch("tradenow.__main__.load_universe",
                      return_value=synthetic_universe(1000)), \
                redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            self.assertEqual(main(["ml"]), 1)
        self.assertIn('pip install -e ".[ml]"', stderr.getvalue())
        self.assertFalse((root / "experiments.jsonl").exists())

    def test_other_commands_do_not_load_scikit_learn(self):
        loaded = subprocess.run(
            [sys.executable, "-c",
             "import sys, tradenow.__main__; print('sklearn' in sys.modules)"],
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(loaded, "False")


if __name__ == "__main__":
    unittest.main()
