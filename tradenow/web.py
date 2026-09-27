"""Local ASGI dashboard for offline simulations, in-memory CSV analysis, and a
read-only view of the Alpaca paper account."""

import asyncio
import csv
import json
import re
from io import StringIO
from pathlib import Path
from urllib.parse import parse_qs

from .gld_research import MAX_GLD_CSV_BYTES, latest_imported_gld, run_gld_csv
from .alpaca_paper import PaperClient, load_paper_credentials
from .offline import run_local_csv, run_offline
from .paper_trading import PaperStore, paper_status
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
    return _report_view(report, csv_text)


def local_simulation_view(source_bytes: bytes, filename: str) -> dict:
    if source_bytes.removeprefix(b"\xef\xbb\xbf").startswith(b"date,symbol,"):
        return gld_simulation_view(source_bytes, filename)
    report, _ = run_local_csv(source_bytes, filename)
    return _report_view(report, source_bytes.decode("utf-8-sig"))


def gld_simulation_view(source_bytes: bytes, filename: str,
                        source: str = "local_gld_csv") -> dict:
    report = run_gld_csv(source_bytes, filename, source=source)
    research = report["research"]
    first_holdout = report["data"]["periods"]["holdout"]["first_date"]
    prices = [{"date": row["date"], "close": float(row["close"]),
               "contract": "GLD"}
              for row in csv.DictReader(StringIO(source_bytes.decode("utf-8-sig")))
              if row["date"] >= first_holdout]
    candidates = [{"name": item["name"],
                   "development_pnl": item["development"]["total_pnl"],
                   "validation_pnl": item["validation"]["total_pnl"],
                   "validation_score": item["validation_score"]}
                  for item in research["hypotheses"]]
    holdout = research["holdout_result"]
    return {"mode": report["mode"], "run_id": report["run_id"],
            "config": report["config"], "data": report["data"],
            "evaluation": report["evaluation"],
            "days": report["data"]["bars"], "periods": report["data"]["periods"],
            "selected_hypothesis": research["selected_hypothesis"],
            "candidates": candidates, "holdout_summary": research["holdout_summary"],
            "prices": prices, "equity_curve": holdout["equity_curve"],
            "fills": holdout["fills"], "risk_decisions": holdout["risk_decisions"],
            "proposals": holdout["proposals"],
            "unfilled_orders": holdout["unfilled_orders"], "rolls": [],
            "open_contract": None}


def _report_view(report: dict, csv_text: str) -> dict:
    research = report["research"]
    first_holdout = report["data"]["periods"]["holdout"]["first_date"]
    prices = [{"date": row["date"], "close": float(row["close"]),
               "contract": row["contract"]}
              for row in csv.DictReader(StringIO(csv_text)) if row["date"] >= first_holdout]
    candidates = [{"name": item["name"],
                   "development_pnl": item["development"]["total_pnl"],
                   "validation_pnl": item["validation"]["total_pnl"],
                   "validation_score": item["validation_score"]}
                  for item in research["hypotheses"]]
    holdout = research["holdout_result"]
    return {"mode": report["mode"], "run_id": report["run_id"],
            "config": report["config"],
            "data": {key: report["data"][key] for key in
                     ("source", "source_name", "contract", "contracts", "seed", "bars",
                      "sha256", "last_trade_dates_provided", "open_times_provided")},
            "days": report["data"]["bars"], "periods": report["data"]["periods"],
            "selected_hypothesis": research["selected_hypothesis"],
            "candidates": candidates, "holdout_summary": research["holdout_summary"],
            "prices": prices, "equity_curve": holdout["equity_curve"],
            "fills": holdout["fills"], "risk_decisions": holdout["risk_decisions"],
            "proposals": holdout["proposals"],
            "unfilled_orders": holdout["unfilled_orders"], "rolls": holdout["rolls"],
            "open_contract": holdout["open_contract"]}


def paper_view(store: PaperStore | None = None, client=None) -> dict:
    """Read-only paper snapshot; never plans, submits, or changes the kill switch."""
    store = store or PaperStore()
    client = client or PaperClient(load_paper_credentials())
    return {"status": paper_status(client, store), "latest_plan": store.latest_plan()}


# Only loopback names are accepted, which blocks DNS-rebinding requests.
LOCAL_HOST = re.compile(r"(?:127\.0\.0\.1|localhost)(?::\d{1,5})?")


def _is_local(scope) -> bool:
    host = dict(scope.get("headers", [])).get(b"host", b"").decode("ascii", errors="ignore")
    return LOCAL_HOST.fullmatch(host) is not None


async def _read_csv(receive) -> bytes:
    chunks = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            raise ValueError("Upload interrupted")
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_GLD_CSV_BYTES:
            raise ValueError(f"Local CSV exceeds {MAX_GLD_CSV_BYTES} bytes")
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


async def _respond(send, status: int, body: bytes, content_type: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", content_type),
                            (b"cache-control", b"no-store"),
                            (b"x-content-type-options", b"nosniff")]})
    await send({"type": "http.response.body", "body": body})


async def app(scope, receive, send) -> None:
    if scope["type"] != "http":
        return
    path = scope["path"]
    if path == "/api/simulation/local" and scope["method"] == "POST":
        headers = dict(scope.get("headers", []))
        host = headers.get(b"host", b"").decode("ascii", errors="ignore")
        origin = headers.get(b"origin", b"").decode("ascii", errors="ignore")
        content_type = headers.get(b"content-type", b"").decode("ascii", errors="ignore")
        if (not _is_local(scope) or origin != f"http://{host}"
                or content_type != "text/csv"):
            await _respond(send, 403, b'{"error":"Local CSV requests must come from this dashboard"}',
                           b"application/json")
            return
        try:
            params = parse_qs(scope.get("query_string", b"").decode("ascii"))
            filenames = params.get("filename", [])
            if len(filenames) != 1:
                raise ValueError("filename must be supplied once")
            source_bytes = await _read_csv(receive)
            payload = await asyncio.to_thread(local_simulation_view, source_bytes, filenames[0])
        except (UnicodeDecodeError, ValueError) as error:
            await _respond(send, 400, json.dumps({"error": str(error)}).encode(),
                           b"application/json")
            return
        await _respond(send, 200, json.dumps(payload).encode(), b"application/json")
        return
    if scope["method"] != "GET":
        await _respond(send, 405, b'{"error":"Method not allowed"}', b"application/json")
        return

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
        elif path == "/api/simulation/gld":
            if not _is_local(scope):
                await _respond(send, 403, b'{"error":"GLD data is local only"}',
                               b"application/json")
                return
            source_bytes, filename = await asyncio.to_thread(latest_imported_gld)
            payload = await asyncio.to_thread(gld_simulation_view, source_bytes, filename,
                                              "tiingo_eod_import")
        elif path == "/api/paper/status":
            if not _is_local(scope):
                await _respond(send, 403, b'{"error":"Paper account data is local only"}',
                               b"application/json")
                return
            payload = await asyncio.to_thread(paper_view)
        elif path == "/api/stress":
            seeds = _number(params, "seeds", 12, 1, 32)
            days = _number(params, "days", 360, 180, 1000)
            payload = await asyncio.to_thread(run_stress, seeds, days)
        else:
            await _respond(send, 404, b'{"error":"Not found"}', b"application/json")
            return
    except (OSError, UnicodeDecodeError, ValueError) as error:
        await _respond(send, 400, json.dumps({"error": str(error)}).encode(),
                       b"application/json")
        return

    await _respond(send, 200, json.dumps(payload).encode(), b"application/json")
