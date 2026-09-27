import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from tradenow.alpaca_paper import (
    PAPER_ENDPOINT,
    AlpacaError,
    PaperClient,
    PaperCredentials,
    PaperOrder,
    load_paper_credentials,
    parse_account,
    parse_daily_bars,
    parse_order,
)


ACCOUNT = {"account_number": "PA3EXAMPLE", "status": "ACTIVE", "currency": "USD",
           "cash": "100000", "equity": "100000", "trading_blocked": False,
           "account_blocked": False, "trade_suspended_by_user": False}
ORDER = {"id": "b1", "client_order_id": "tn-gld-20260928-buy-abc", "symbol": "GLD",
         "side": "buy", "qty": "10", "filled_qty": "0", "filled_avg_price": None,
         "status": "accepted"}


def env_file(directory: str, text: str) -> Path:
    path = Path(directory) / ".env.local"
    path.write_text(text, encoding="utf-8")
    return path


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class RecordingOpener:
    def __init__(self, responses: list):
        self.responses = responses
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeResponse(json.dumps(response).encode())


def client_with(responses: list) -> tuple[PaperClient, RecordingOpener]:
    client = PaperClient(PaperCredentials(PAPER_ENDPOINT, "key-id", "secret-key"))
    opener = RecordingOpener(responses)
    client._opener = opener
    return client, opener


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {"ALPACA_PAPER_ENDPOINT": "",
                                             "ALPACA_PAPER_KEY_ID": "",
                                             "ALPACA_PAPER_SECRET_KEY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_accepts_dashboard_url_with_v2_and_hides_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = env_file(directory, "ALPACA_PAPER_ENDPOINT=https://paper-api.alpaca.markets/v2\n"
                                       "ALPACA_PAPER_KEY_ID=PKEXAMPLE\n"
                                       "ALPACA_PAPER_SECRET_KEY='s3cret'\n")
            credentials = load_paper_credentials(path)
        self.assertEqual(credentials.endpoint, PAPER_ENDPOINT)
        self.assertEqual(credentials.secret_key, "s3cret")
        self.assertNotIn("s3cret", repr(credentials))
        self.assertNotIn("PKEXAMPLE", repr(credentials))

    def test_rejects_live_endpoint_and_duplicate_names(self):
        with tempfile.TemporaryDirectory() as directory:
            live = env_file(directory, "ALPACA_PAPER_ENDPOINT=https://api.alpaca.markets\n"
                                       "ALPACA_PAPER_KEY_ID=a\nALPACA_PAPER_SECRET_KEY=b\n")
            with self.assertRaisesRegex(ValueError, "paper URL"):
                load_paper_credentials(live)
            duplicate = env_file(directory, f"ALPACA_PAPER_ENDPOINT={PAPER_ENDPOINT}\n"
                                            "ALPACA_PAPER_KEY_ID=a\nALPACA_PAPER_KEY_ID=c\n"
                                            "ALPACA_PAPER_SECRET_KEY=b\n")
            with self.assertRaisesRegex(ValueError, "Duplicate ALPACA_PAPER_KEY_ID"):
                load_paper_credentials(duplicate)
            blank = env_file(directory, f"ALPACA_PAPER_ENDPOINT={PAPER_ENDPOINT}\n"
                                        "ALPACA_PAPER_KEY_ID=a\n")
            with self.assertRaisesRegex(ValueError, "SECRET_KEY is missing"):
                load_paper_credentials(blank)


class OrderShapeTests(unittest.TestCase):
    def test_payload_is_whole_share_gld_day_order_only(self):
        buy = PaperOrder("tn-gld-20260928-buy-abc", "buy", 12, Decimal("401.25"))
        self.assertEqual(buy.payload(), {"symbol": "GLD", "qty": "12", "side": "buy",
                                         "time_in_force": "day", "type": "limit",
                                         "client_order_id": "tn-gld-20260928-buy-abc",
                                         "limit_price": "401.25"})
        sell = PaperOrder("tn-gld-20260928-sell-abc", "sell", 12)
        self.assertEqual(sell.payload()["type"], "market")
        self.assertNotIn("extended_hours", sell.payload())

    def test_rejects_unsafe_order_shapes(self):
        cases = [("other-id", "buy", 1, Decimal("1.00")),
                 ("tn-gld-x", "buy", 0, Decimal("1.00")),
                 ("tn-gld-x", "buy", True, Decimal("1.00")),
                 ("tn-gld-x", "buy", 10_001, Decimal("1.00")),
                 ("tn-gld-x", "buy", 1, None),
                 ("tn-gld-x", "buy", 1, Decimal("1.001")),
                 ("tn-gld-x", "sell", 1, Decimal("1.00")),
                 ("tn-gld-x", "short", 1, None)]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                PaperOrder(*args)


class ParserTests(unittest.TestCase):
    def test_account_detects_paper_and_blocks(self):
        account = parse_account(ACCOUNT)
        self.assertTrue(account.is_paper_account)
        self.assertFalse(account.trading_blocked)
        self.assertFalse(parse_account({**ACCOUNT, "account_number": "3EXAMPLE"}).is_paper_account)
        self.assertTrue(parse_account({**ACCOUNT, "account_blocked": True}).trading_blocked)
        with self.assertRaises(AlpacaError):
            parse_account({**ACCOUNT, "cash": "NaN"})

    def test_order_rejects_fractional_or_impossible_fills(self):
        self.assertEqual(parse_order(ORDER).qty, 10)
        with self.assertRaisesRegex(AlpacaError, "fractional"):
            parse_order({**ORDER, "qty": "1.5"})
        with self.assertRaisesRegex(AlpacaError, "impossible"):
            parse_order({**ORDER, "filled_qty": "11"})

    def test_order_keeps_submission_and_fill_times(self):
        self.assertIsNone(parse_order(ORDER).filled_at)
        order = parse_order({**ORDER, "submitted_at": "2025-12-09T01:00:00.123456Z",
                             "filled_at": "2025-12-09T14:30:01.5Z"})
        self.assertEqual(order.submitted_at.tzinfo.utcoffset(None), timedelta(0))
        self.assertEqual((order.filled_at - order.submitted_at).total_seconds(), 48601.376544)
        nanos = parse_order({**ORDER, "filled_at": "2025-12-09T14:30:01.123456789Z"})
        self.assertEqual(nanos.filled_at.microsecond, 123456)
        with self.assertRaisesRegex(AlpacaError, "without an offset"):
            parse_order({**ORDER, "filled_at": "2025-12-09T14:30:01"})

    def test_daily_bars_use_new_york_session_dates(self):
        bars = parse_daily_bars({"bars": [
            {"t": "2026-01-05T05:00:00Z", "o": Decimal("400.1"), "c": Decimal("401.2"), "v": 5},
            {"t": "2026-09-14T04:00:00Z", "o": Decimal("391.89"), "c": Decimal("392.84"), "v": 5}]})
        self.assertEqual([bar.date for bar in bars], [date(2026, 1, 5), date(2026, 9, 14)])
        self.assertEqual(bars[1].close, Decimal("392.84"))
        with self.assertRaisesRegex(AlpacaError, "timestamp"):
            parse_daily_bars({"bars": [{"t": "2026-09-14T13:30:00Z", "o": 1, "c": 1, "v": 1}]})
        with self.assertRaisesRegex(AlpacaError, "paginated"):
            parse_daily_bars({"bars": [], "next_page_token": "x"})


class ClientTests(unittest.TestCase):
    def test_sends_credentials_only_as_headers_to_paper_host(self):
        client, opener = client_with([ACCOUNT])
        client.account()
        request = opener.requests[0]
        self.assertEqual(request.full_url, "https://paper-api.alpaca.markets/v2/account")
        self.assertEqual(request.get_header("Apca-api-key-id"), "key-id")
        self.assertNotIn("secret-key", request.full_url)

    def test_http_error_keeps_alpaca_message_but_never_secrets(self):
        body = io.BytesIO(json.dumps({"code": 40310000,
                                      "message": "insufficient buying power"}).encode())
        error = HTTPError(PAPER_ENDPOINT, 403, "Forbidden", {}, body)
        client, _ = client_with([error])
        with self.assertRaises(AlpacaError) as raised:
            client.account()
        self.assertEqual(raised.exception.status, 403)
        self.assertIn("insufficient buying power", str(raised.exception))
        self.assertNotIn("secret-key", str(raised.exception))

    def test_missing_client_order_returns_none(self):
        client, opener = client_with([HTTPError(PAPER_ENDPOINT, 404, "Not Found", {}, None)])
        self.assertIsNone(client.order_by_client_id("tn-gld-x"))
        self.assertIn("client_order_id=tn-gld-x", opener.requests[0].full_url)

    def test_submit_verifies_acknowledgement_matches(self):
        client, opener = client_with([{**ORDER, "qty": "9"}])
        order = PaperOrder("tn-gld-20260928-buy-abc", "buy", 10, Decimal("400.00"))
        with self.assertRaisesRegex(AlpacaError, "different order"):
            client.submit_gld(order)
        self.assertEqual(json.loads(opener.requests[0].data)["qty"], "10")

    def test_market_data_is_get_only_on_data_host(self):
        client, opener = client_with([{"bars": []}])
        end = datetime(2026, 9, 27, 0, 26, tzinfo=timezone.utc)
        self.assertEqual(client.gld_daily_bars(date(2026, 9, 4), end), [])
        url = opener.requests[0].full_url
        self.assertTrue(url.startswith("https://data.alpaca.markets/v2/stocks/GLD/bars?"))
        self.assertIn("feed=sip", url)
        with self.assertRaises(ValueError):
            client._request("POST", "https://data.alpaca.markets", "/v2/orders", {})


if __name__ == "__main__":
    unittest.main()
