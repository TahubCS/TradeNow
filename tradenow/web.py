"""Read-only ASGI dashboard for local synthetic simulations."""

import asyncio
import csv
import json
from io import StringIO
from pathlib import Path
from urllib.parse import parse_qs

from .offline import run_offline
from .stress import run_stress


HTML_PATH = Path(__file__).with_name("dashboard.html")


def _number(params: dict[str, list[str]], name: str, default: int,
            minimum: int, maximum: int) -> int:
    values = params.get(name, [str(default)])
    if len(values) != 1:
        raise ValueError(f"{name} must be supplied once")
    try:
        value = int(values[0])
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def simulation_view(seed: int, days: int) -> dict:
    report, csv_text = run_offline(seed, days)
    research = report["research"]
    first_holdout = report["data"]["periods"]["holdout"]["first_date"]
    prices = [{"date": row["date"], "close": float(row["close"])}
              for row in csv.DictReader(StringIO(csv_text)) if row["date"] >= first_holdout]
    candidates = [{"name": item["name"],
                   "development_pnl": item["development"]["total_pnl"],
                   "validation_pnl": item["validation"]["total_pnl"],
                   "validation_score": item["validation_score"]}
                  for item in research["hypotheses"]]
    holdout = research["holdout_result"]
    return {"mode": report["mode"], "run_id": report["run_id"],
            "seed": seed, "days": days, "periods": report["data"]["periods"],
            "selected_hypothesis": research["selected_hypothesis"],
            "candidates": candidates, "holdout_summary": research["holdout_summary"],
            "prices": prices, "equity_curve": holdout["equity_curve"],
            "fills": holdout["fills"], "risk_decisions": holdout["risk_decisions"],
            "proposals": holdout["proposals"]}


async def _respond(send, status: int, body: bytes, content_type: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", content_type),
                            (b"cache-control", b"no-store"),
                            (b"x-content-type-options", b"nosniff")]})
    await send({"type": "http.response.body", "body": body})


async def app(scope, receive, send) -> None:
    if scope["type"] != "http":
        return
    if scope["method"] != "GET":
        await _respond(send, 405, b'{"error":"Read-only service"}', b"application/json")
        return

    path = scope["path"]
    if path == "/":
        await _respond(send, 200, HTML_PATH.read_bytes(), b"text/html; charset=utf-8")
        return
    if path == "/favicon.ico":
        await _respond(send, 204, b"", b"image/x-icon")
        return
    if path == "/api/health":
        await _respond(send, 200, b'{"status":"ok","mode":"offline_simulation"}',
                       b"application/json")
        return

    try:
        params = parse_qs(scope.get("query_string", b"").decode("ascii"), keep_blank_values=True)
        if path == "/api/simulation":
            seed = _number(params, "seed", 3, 0, 1_000_000)
            days = _number(params, "days", 360, 180, 1000)
            payload = await asyncio.to_thread(simulation_view, seed, days)
        elif path == "/api/stress":
            seeds = _number(params, "seeds", 12, 1, 32)
            days = _number(params, "days", 360, 180, 1000)
            payload = await asyncio.to_thread(run_stress, seeds, days)
        else:
            await _respond(send, 404, b'{"error":"Not found"}', b"application/json")
            return
    except (UnicodeDecodeError, ValueError) as error:
        await _respond(send, 400, json.dumps({"error": str(error)}).encode(),
                       b"application/json")
        return

    await _respond(send, 200, json.dumps(payload).encode(), b"application/json")
