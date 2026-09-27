import json
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from uuid import uuid4

from tradenow.tiingo import PRIVATE_DIR, _get_json, import_gld, load_api_key


START = date(2026, 9, 24)
END = date(2026, 9, 25)
META = {"ticker": "GLD", "startDate": "2004-11-18", "endDate": "2026-09-25"}


@contextmanager
def private_test_dir():
    directory = PRIVATE_DIR / f"test-{uuid4().hex}"
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()


def price(day: str, close: float = 201.0) -> dict:
    return {"date": f"{day}T00:00:00.000Z", "open": 200.0, "high": 202.0,
            "low": 199.0, "close": close, "volume": 1000,
            "adjOpen": 200.0, "adjHigh": 202.0, "adjLow": 199.0,
            "adjClose": close, "adjVolume": 1000, "divCash": 0.0,
            "splitFactor": 1.0}


class TiingoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        PRIVATE_DIR.mkdir(parents=True, exist_ok=True)

    def test_reads_ignored_env_file_without_printing_key(self):
        with private_test_dir() as directory:
            path = directory / ".env.local"
            path.write_text("# local secret\nTIINGO_API_KEY=sample-secret\n", encoding="utf-8")
            with patch.dict("os.environ", {"TIINGO_API_KEY": ""}):
                self.assertEqual(load_api_key(path), "sample-secret")
            with patch.dict("os.environ", {"TIINGO_API_KEY": "from-process"}):
                self.assertEqual(load_api_key(path), "from-process")

    def test_import_saves_raw_and_adjusted_bars_with_provenance(self):
        rows = [price("2026-09-24"), price("2026-09-25", 201.5)]
        raw = json.dumps(rows).encode()
        with private_test_dir() as directory:
            with patch("tradenow.tiingo._get_json",
                       side_effect=[(META, b"{}"), (rows, raw)]) as get:
                result = import_gld(START, END, "sample-secret", directory)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(get.call_args_list[0].args[0],
                             "https://api.tiingo.com/tiingo/daily/GLD")
            self.assertNotIn("sample-secret", get.call_args_list[1].args[0])
            self.assertEqual(result["bars"], 2)
            bars = Path(result["bars_file"]).read_text(encoding="utf-8")
            self.assertIn("adj_open,adj_high,adj_low,adj_close", bars)
            self.assertIn("2026-09-25,GLD,200.0,202.0,199.0,201.5", bars)
            manifest = json.loads(Path(result["manifest_file"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["provider"], "Tiingo")
            self.assertNotIn("sample-secret", json.dumps(manifest))
            with patch("tradenow.tiingo._get_json") as get_again:
                with self.assertRaisesRegex(ValueError, "already imported"):
                    import_gld(START, END, "sample-secret", directory)
                get_again.assert_not_called()

    def test_rejects_bad_bars_without_saving(self):
        with private_test_dir() as directory:
            bad_rows = [price("2026-09-24"), price("2026-09-24")]
            with patch("tradenow.tiingo._get_json",
                       side_effect=[(META, b"{}"), (bad_rows, b"[]")]):
                with self.assertRaisesRegex(ValueError, "unordered"):
                    import_gld(START, END, "sample-secret", directory)
            self.assertEqual(list(directory.iterdir()), [])

    def test_rate_limit_stops_without_retry_or_key_in_url(self):
        def reject(request, timeout):
            self.assertEqual(request.full_url, "https://api.tiingo.com/tiingo/daily/GLD")
            self.assertEqual(request.get_header("Authorization"), "Token sample-secret")
            raise HTTPError(request.full_url, 429, "rate limit", {}, None)

        with patch("tradenow.tiingo.urlopen", side_effect=reject) as open_url:
            with self.assertRaisesRegex(ValueError, "rate limit"):
                _get_json("https://api.tiingo.com/tiingo/daily/GLD", "sample-secret")
            self.assertEqual(open_url.call_count, 1)


if __name__ == "__main__":
    unittest.main()
