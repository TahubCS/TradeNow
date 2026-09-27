"""Manual, GLD-only import from Tiingo's free end-of-day endpoints."""

import csv
import hashlib
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PRIVATE_DIR = Path(__file__).resolve().parent.parent / "data" / "private" / "tiingo"
MAX_RESPONSE_BYTES = 10_000_000
HEADER = ("date", "symbol", "open", "high", "low", "close", "volume",
          "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume",
          "div_cash", "split_factor")


def load_api_key(env_file: Path | None = None) -> str:
    """Use a process variable or the ignored project-root .env.local file."""
    key = os.environ.get("TIINGO_API_KEY", "").strip()
    if not key:
        path = env_file or Path(__file__).resolve().parent.parent / ".env.local"
        if not path.exists():
            raise ValueError("Set TIINGO_API_KEY or create a project-root .env.local")
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip().startswith("TIINGO_API_KEY="):
                if key:
                    raise ValueError("Duplicate TIINGO_API_KEY in .env.local")
                key = line.split("=", 1)[1].strip().strip('"\'')
    if not key or any(char.isspace() for char in key):
        raise ValueError("TIINGO_API_KEY is missing or malformed")
    return key


def _get_json(url: str, key: str) -> tuple[object, bytes]:
    request = Request(url, headers={"Authorization": f"Token {key}",
                                    "Accept": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        if error.code in (401, 403):
            raise ValueError("Tiingo rejected the API key or account access") from None
        if error.code == 429:
            raise ValueError("Tiingo rate limit reached; import stopped without retry") from None
        raise ValueError(f"Tiingo request failed with HTTP {error.code}") from None
    except (URLError, TimeoutError):
        raise ValueError("Tiingo request failed; check the network connection") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Tiingo response exceeded the import size limit")
    try:
        return json.loads(raw.decode("utf-8"), parse_float=Decimal), raw
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Tiingo returned invalid JSON") from None


def _day(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("Tiingo returned an invalid date")
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise ValueError("Tiingo returned an invalid date") from None


def _decimal(value: object, field: str, positive: bool = True) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"Tiingo returned an invalid {field}") from None
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise ValueError(f"Tiingo returned an invalid {field}")
    return number


def _volume(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Tiingo returned an invalid {field}")
    return value


def _normalize_prices(payload: object, start: date, end: date) -> tuple[bytes, int, date, date]:
    if not isinstance(payload, list) or not payload or len(payload) > 10_000:
        raise ValueError("Tiingo returned no usable GLD daily bars")
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(HEADER)
    previous: date | None = None
    first: date | None = None
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Tiingo returned a malformed GLD bar")
        bar_date = _day(item.get("date"))
        if (bar_date < start or bar_date > end or bar_date.weekday() >= 5
                or previous is not None and bar_date <= previous):
            raise ValueError("Tiingo returned an out-of-range or unordered GLD date")
        raw = [_decimal(item.get(field), field) for field in
               ("open", "high", "low", "close")]
        adjusted = [_decimal(item.get(field), field) for field in
                    ("adjOpen", "adjHigh", "adjLow", "adjClose")]
        for prices in (raw, adjusted):
            if prices[2] > min(prices[0], prices[3]) or prices[1] < max(prices[0], prices[3]):
                raise ValueError("Tiingo returned an invalid GLD OHLC range")
        volume = _volume(item.get("volume"), "volume")
        adj_volume = _volume(item.get("adjVolume"), "adjVolume")
        dividend = _decimal(item.get("divCash"), "divCash", positive=False)
        split = _decimal(item.get("splitFactor"), "splitFactor")
        writer.writerow((bar_date.isoformat(), "GLD", *(str(price) for price in raw),
                         volume, *(str(price) for price in adjusted), adj_volume,
                         str(dividend), str(split)))
        first = first or bar_date
        previous = bar_date
    return output.getvalue().encode("utf-8"), len(payload), first, previous


def import_gld(start: date, end: date, key: str, output_dir: Path = PRIVATE_DIR) -> dict:
    """Fetch one symbol with two requests, validate, then save private source files."""
    if start > end or end > date.today():
        raise ValueError("Expected start <= end <= today")
    stem = f"GLD-{start:%Y%m%d}-{end:%Y%m%d}"
    bars_path = output_dir / f"{stem}.csv"
    raw_path = output_dir / f"{stem}.raw.json"
    manifest_path = output_dir / f"{stem}.manifest.json"
    if any(path.exists() for path in (bars_path, raw_path, manifest_path)):
        raise ValueError("This GLD date range is already imported; no API request was made")

    base_url = "https://api.tiingo.com/tiingo/daily/GLD"
    metadata, _ = _get_json(base_url, key)
    if not isinstance(metadata, dict) or metadata.get("ticker") != "GLD":
        raise ValueError("Tiingo did not confirm GLD EOD coverage")
    available_start = _day(metadata.get("startDate"))
    available_end = _day(metadata.get("endDate"))
    if end < available_start or start > available_end:
        raise ValueError("Requested range is outside Tiingo's GLD coverage")

    query = urlencode({"startDate": start.isoformat(), "endDate": end.isoformat()})
    prices, raw = _get_json(f"{base_url}/prices?{query}", key)
    csv_bytes, count, first, last = _normalize_prices(prices, start, end)
    manifest = {"provider": "Tiingo", "product": "EOD", "symbol": "GLD",
                "requested_start": start.isoformat(), "requested_end": end.isoformat(),
                "provider_start": available_start.isoformat(),
                "provider_end": available_end.isoformat(),
                "first_bar": first.isoformat(), "last_bar": last.isoformat(),
                "bars": count, "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "bars_sha256": hashlib.sha256(csv_bytes).hexdigest(),
                "price_policy": "Raw OHLCV for execution; adjusted fields retained separately"}
    output_dir.mkdir(parents=True, exist_ok=True)
    bars_path.write_bytes(csv_bytes)
    raw_path.write_bytes(raw)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"symbol": "GLD", "bars": count, "first_bar": manifest["first_bar"],
            "last_bar": manifest["last_bar"], "bars_sha256": manifest["bars_sha256"],
            "bars_file": str(bars_path), "manifest_file": str(manifest_path)}
