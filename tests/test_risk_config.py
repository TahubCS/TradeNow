import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tradenow.risk_config import LIMITS, RiskConfig, default_risk, load_risk, risk_sha256


class RiskConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "risk.toml"

    def load(self, text: str):
        self.path.write_text(text, encoding="utf-8")
        return load_risk(self.path)

    def test_missing_file_means_defaults_with_auto_submit_off(self):
        loaded = load_risk(self.path)
        self.assertEqual(loaded.config, RiskConfig())
        self.assertFalse(loaded.config.auto_submit)
        self.assertEqual(loaded.sha256, default_risk().sha256)

    def test_values_within_ceilings_are_accepted(self):
        loaded = self.load("max_position_fraction = 0.25\n"
                           "daily_loss_limit_fraction = '0.015'\nauto_submit = true\n")
        self.assertEqual(loaded.config.max_position_fraction, Decimal("0.25"))
        self.assertEqual(loaded.config.daily_loss_limit_fraction, Decimal("0.015"))
        self.assertTrue(loaded.config.auto_submit)
        self.assertNotEqual(loaded.sha256, default_risk().sha256)

    def test_every_ceiling_is_enforced(self):
        for name, (low, high) in LIMITS.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, name):
                    self.load(f"{name} = {high + Decimal('0.0001')}\n")
                with self.assertRaisesRegex(ValueError, name):
                    self.load(f"{name} = {low - Decimal('0.0001')}\n")

    def test_margin_is_impossible(self):
        with self.assertRaisesRegex(ValueError, "max_position_fraction"):
            self.load("max_position_fraction = 1.5\n")

    def test_typos_wrong_types_and_bad_syntax_stop_the_run(self):
        with self.assertRaisesRegex(ValueError, "unknown setting"):
            self.load("max_postion_fraction = 0.5\n")
        with self.assertRaisesRegex(ValueError, "true or false"):
            self.load('auto_submit = "yes"\n')
        with self.assertRaisesRegex(ValueError, "must be a number"):
            self.load("buy_limit_buffer = true\n")
        with self.assertRaisesRegex(ValueError, "does not parse"):
            self.load("max_position_fraction = \n")

    def test_hash_ignores_comments_and_formatting(self):
        plain = self.load("max_position_fraction = 0.3\n").sha256
        commented = self.load("# smaller position\nmax_position_fraction   =   0.30\n").sha256
        self.assertEqual(plain, commented)
        self.assertEqual(plain, risk_sha256(RiskConfig(max_position_fraction=Decimal("0.3"))))

    def test_example_file_matches_the_defaults(self):
        example = Path(__file__).resolve().parent.parent / "risk.example.toml"
        self.assertEqual(load_risk(example).config, RiskConfig())


if __name__ == "__main__":
    unittest.main()
