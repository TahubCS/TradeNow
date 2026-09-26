import json
import unittest

from tradenow.web import app


async def request(path: str, query: str = "", method: str = "GET") -> tuple[int, bytes]:
    messages = []

    async def send(message):
        messages.append(message)

    await app({"type": "http", "method": method, "path": path,
               "query_string": query.encode("ascii")}, None, send)
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
