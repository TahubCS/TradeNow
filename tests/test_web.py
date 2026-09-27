import json
import unittest

from tradenow.web import app
from tradenow.synthetic import bars_to_csv, generate_bars


async def request(path: str, query: str = "", method: str = "GET", body: bytes = b"",
                  headers: list[tuple[bytes, bytes]] | None = None) -> tuple[int, bytes]:
    messages = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    await app({"type": "http", "method": method, "path": path,
               "query_string": query.encode("ascii"), "headers": headers or []}, receive, send)
    start, body = messages
    return start["status"], body["body"]


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_and_health_load(self):
        status, body = await request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"OFFLINE ONLY", body)
        self.assertIn(b"priceChart", body)
        status, body = await request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["mode"], "offline_simulation")

    async def test_simulation_api_returns_chart_and_decisions(self):
        status, body = await request("/api/simulation", "seed=3&days=180")
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["mode"], "offline_simulation")
        self.assertEqual(len(result["prices"]), 36)
        self.assertEqual(len(result["equity_curve"]), 36)
        self.assertEqual(len(result["candidates"]), 3)
        self.assertGreater(len(result["proposals"]), 0)

    async def test_stress_api_and_read_only_validation(self):
        status, body = await request("/api/stress", "seeds=1&days=180")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["passed"])
        status, body = await request("/api/simulation", "days=20")
        self.assertEqual(status, 400)
        self.assertIn("days must be between", json.loads(body)["error"])
        status, _ = await request("/api/simulation", method="POST")
        self.assertEqual(status, 405)

    async def test_local_csv_upload_and_rejection_paths(self):
        source = bars_to_csv(generate_bars(3, 180)).encode()
        headers = [(b"host", b"127.0.0.1:8000"),
                   (b"origin", b"http://127.0.0.1:8000"),
                   (b"content-type", b"text/csv")]
        status, body = await request("/api/simulation/local", "filename=mgc.csv",
                                     "POST", source, headers)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["data"]["source"], "local_csv")
        self.assertEqual(payload["data"]["bars"], 180)
        self.assertEqual(len(payload["prices"]), 36)
        status, _ = await request("/api/simulation/local", "filename=mgc.csv",
                                  "POST", source, headers[:1] +
                                  [(b"origin", b"http://example.com"), headers[2]])
        self.assertEqual(status, 403)
        status, body = await request("/api/simulation/local", "filename=mgc.csv",
                                     "POST", source[:100], headers)
        self.assertEqual(status, 400)
        self.assertIn("Invalid value", json.loads(body)["error"])
        status, body = await request("/api/simulation/local", "filename=mgc.csv",
                                     "POST", b"x" * 1_000_001, headers)
        self.assertEqual(status, 400)
        self.assertIn("exceeds", json.loads(body)["error"])
