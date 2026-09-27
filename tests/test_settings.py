import unittest
from pathlib import Path

from tradenow.settings import PROJECT_ROOT, load_settings


class SettingsTests(unittest.TestCase):
    def test_defaults_to_paper_mode_and_project_private_directory(self):
        settings = load_settings({})
        self.assertEqual(settings.mode, "paper")
        self.assertEqual(settings.data_dir, PROJECT_ROOT / "data" / "private")
        self.assertEqual(settings.tiingo_dir, settings.data_dir / "tiingo")
        self.assertEqual(settings.alpaca_dir, settings.data_dir / "alpaca")
        self.assertEqual(settings.log_dir, settings.data_dir / "logs")

    def test_relative_data_directory_resolves_against_project_root(self):
        self.assertEqual(load_settings({"TRADENOW_DATA_DIR": "state"}).data_dir,
                         PROJECT_ROOT / "state")
        absolute = Path(PROJECT_ROOT.anchor) / "elsewhere"
        self.assertEqual(load_settings({"TRADENOW_DATA_DIR": str(absolute)}).data_dir, absolute)

    def test_live_mode_is_refused(self):
        self.assertEqual(load_settings({"TRADENOW_MODE": " Paper "}).mode, "paper")
        with self.assertRaisesRegex(ValueError, "TRADENOW_MODE"):
            load_settings({"TRADENOW_MODE": "live"})


if __name__ == "__main__":
    unittest.main()
