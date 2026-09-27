"""Manual imports from Tiingo's free end-of-day endpoints, one symbol at a time.

Each import saves the raw response, a normalized CSV with raw and adjusted
prices, and a manifest with SHA-256 hashes. Refreshing a range is allowed only
while it is incomplete, and never when earlier raw bars were revised.
"""

import csv
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .logs import register_secret
from .settings import PROJECT_ROOT, SETTINGS


PRIVATE_DIR = SETTINGS.tiingo_dir
SYMBOL = re.compile(r"[A-Z]{1,5}")
# Completed imports kept per symbol; older ones are deleted after each import.
KEEP_IMPORTS = 3
logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 10_000_000
RAW_FIELDS = ("open", "high", "low", "close", "volume")
HEADER = ("date", "symbol", "open", "high", "low", "close", "volume",
          "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume",
          "div_cash", "split_factor")


def load_api_key(env_file: Path | None = None) -> str:
    """Use a process variable or the ignored project-root .env.local file."""
    key = os.environ.get("TIINGO_API_KEY", "").strip()
    if not key:
        path = env_file or PROJECT_ROOT / ".env.local"
        if not path.exists():
            raise ValueError("Set TIINGO_API_KEY or create a project-root .env.local")
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip().startswith("TIINGO_API_KEY="):
                if key:
                    raise ValueError("Duplicate TIINGO_API_KEY in .env.local")
                key = line.split("=", 1)[1].strip().strip('"\'')
    if not key or any(char.isspace() for char in key):
        raise ValueError("TIINGO_API_KEY is missing or malformed")
    register_secret(key)
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


def _check_symbol(symbol: str) -> str:
    if not SYMBOL.fullmatch(symbol):
        raise ValueError(f"Invalid ticker symbol {symbol!r}")
    return symbol


def _normalize_prices(payload: object, start: date, end: date,
                      symbol: str = "GLD") -> tuple[bytes, int, date, date]:
    if not isinstance(payload, list) or not payload or len(payload) > 10_000:
        raise ValueError(f"Tiingo returned no usable {symbol} daily bars")
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(HEADER)
    previous: date | None = None
    first: date | None = None
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError(f"Tiingo returned a malformed {symbol} bar")
        bar_date = _day(item.get("date"))
        if (bar_date < start or bar_date > end or bar_date.weekday() >= 5
                or previous is not None and bar_date <= previous):
            raise ValueError(f"Tiingo returned an out-of-range or unordered {symbol} date")
        raw = [_decimal(item.get(field), field) for field in
               ("open", "high", "low", "close")]
        adjusted = [_decimal(item.get(field), field) for field in
                    ("adjOpen", "adjHigh", "adjLow", "adjClose")]
        for prices in (raw, adjusted):
            if prices[2] > min(prices[0], prices[3]) or prices[1] < max(prices[0], prices[3]):
                raise ValueError(f"Tiingo returned an invalid {symbol} OHLC range")
        volume = _volume(item.get("volume"), "volume")
        adj_volume = _volume(item.get("adjVolume"), "adjVolume")
        dividend = _decimal(item.get("divCash"), "divCash", positive=False)
        split = _decimal(item.get("splitFactor"), "splitFactor")
        writer.writerow((bar_date.isoformat(), symbol, *(str(price) for price in raw),
                         volume, *(str(price) for price in adjusted), adj_volume,
                         str(dividend), str(split)))
        first = first or bar_date
        previous = bar_date
    assert first is not None and previous is not None
    return output.getvalue().encode("utf-8"), len(payload), first, previous


def _raw_rows(csv_bytes: bytes) -> dict[str, tuple[Decimal, ...]]:
    """Raw OHLCV by date: the fields the strategy and simulator trade on."""
    rows = csv.DictReader(StringIO(csv_bytes.decode("utf-8")))
    return {row["date"]: tuple(Decimal(row[field]) for field in RAW_FIELDS) for row in rows}


def _previous_import(paths: tuple[Path, Path, Path], end: date,
                     symbol: str = "GLD") -> tuple[dict, bytes] | None:
    """The earlier import of this exact range, if it may be refreshed.

    A complete import (its last bar is the requested end) is final and is
    refused before any API request. An incomplete one, such as an import run
    before Tiingo published that day's close, may be fetched again.
    """
    bars_path, raw_path, manifest_path = paths
    present = [path.exists() for path in paths]
    if not any(present):
        return None
    if not all(present):
        raise ValueError("An earlier import of this range is incomplete; review "
                         f"{bars_path.parent} before importing again")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    csv_bytes = bars_path.read_bytes()
    if (not isinstance(manifest, dict)
            or hashlib.sha256(csv_bytes).hexdigest() != manifest.get("bars_sha256")):
        raise ValueError(f"The earlier {symbol} import does not match its manifest; "
                         "no API request was made")
    if _day(manifest.get("last_bar")) >= end:
        raise ValueError(f"This {symbol} date range is already imported; "
                         "no API request was made")
    return manifest, csv_bytes


def _revised_dates(previous: bytes, current: bytes) -> list[str]:
    """Dates whose raw OHLCV changed or disappeared between two imports."""
    old, new = _raw_rows(previous), _raw_rows(current)
    return sorted(day for day, values in old.items() if new.get(day) != values)


def _replace_import(paths: tuple[Path, Path, Path], contents: tuple[bytes, bytes, bytes],
                    previous: dict | None) -> None:
    """Write the new files; an earlier import of the range moves to superseded/."""
    if previous is not None:
        stamp = re.sub(r"[^0-9]", "", str(previous.get("fetched_at_utc", "")))[:14] or "unknown"
        archive = paths[0].parent / "superseded"
        archive.mkdir(parents=True, exist_ok=True)
        for path in paths:
            os.replace(path, archive / f"{stamp}-{path.name}")
    for path, data in zip(paths, contents, strict=True):
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)


def _paths(start: date, end: date, output_dir: Path,
           symbol: str = "GLD") -> tuple[Path, Path, Path]:
    stem = f"{_check_symbol(symbol)}-{start:%Y%m%d}-{end:%Y%m%d}"
    return (output_dir / f"{stem}.csv", output_dir / f"{stem}.raw.json",
            output_dir / f"{stem}.manifest.json")


def import_complete(start: date, end: date, output_dir: Path = PRIVATE_DIR,
                    symbol: str = "GLD") -> bool:
    """True when this range was already imported through its end date (no request needed)."""
    manifest_path = _paths(start, end, output_dir, symbol)[2]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return _day(manifest.get("last_bar")) >= end
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError, ValueError):
        return False


def _manifests(symbol: str, directory: Path) -> list[tuple[str, str, Path]]:
    """(end, start, path) for each import of a symbol, newest range first."""
    pattern = re.compile(rf"{_check_symbol(symbol)}-(\d{{8}})-(\d{{8}})\.manifest\.json")
    found = []
    for path in directory.glob(f"{symbol}-*.manifest.json"):
        match = pattern.fullmatch(path.name)
        if match:
            found.append((match.group(2), match.group(1), path))
    return sorted(found, reverse=True)


def latest_import(symbol: str, directory: Path = PRIVATE_DIR,
                  max_bytes: int = 2_000_000) -> tuple[bytes, str]:
    """The newest import of a symbol, only if its CSV matches its manifest."""
    manifests = _manifests(symbol, directory)
    if not manifests:
        raise ValueError(f"No private {symbol} import found; run tiingo-import first")
    manifest_path = manifests[0][2]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("provider") != "Tiingo" or manifest.get("symbol") != symbol:
        raise ValueError(f"{symbol} import manifest is invalid")
    csv_path = manifest_path.with_name(manifest_path.name.replace(".manifest.json", ".csv"))
    if csv_path.stat().st_size > max_bytes:
        raise ValueError(f"Private {symbol} import exceeds size limit")
    source_bytes = csv_path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != manifest.get("bars_sha256"):
        raise ValueError(f"Private {symbol} CSV does not match its import manifest")
    return source_bytes, csv_path.name


def prune_imports(symbol: str, directory: Path = PRIVATE_DIR,
                  keep: int = KEEP_IMPORTS) -> list[str]:
    """Delete all but the newest `keep` imports of a symbol, and superseded copies
    beyond the newest `keep`. Only exact files of this symbol are ever touched."""
    if keep < 1:
        raise ValueError("At least one import must be kept")
    deleted: list[str] = []
    for _, _, manifest_path in _manifests(symbol, directory)[keep:]:
        stem = manifest_path.name[:-len(".manifest.json")]
        for suffix in (".csv", ".raw.json", ".manifest.json"):
            path = directory / f"{stem}{suffix}"
            if path.exists():
                path.unlink()
                deleted.append(path.name)
    archive = directory / "superseded"
    if archive.is_dir():
        pattern = re.compile(rf"(\d+|unknown)-{symbol}-\d{{8}}-\d{{8}}\.(csv|raw\.json|manifest\.json)")
        groups: dict[str, list[Path]] = {}
        for path in archive.iterdir():
            match = pattern.fullmatch(path.name)
            if match:
                groups.setdefault(path.name.split(".")[0], []).append(path)
        for name in sorted(groups, reverse=True)[keep:]:
            for path in groups[name]:
                path.unlink()
                deleted.append(f"superseded/{path.name}")
    if deleted:
        logger.info("pruned %d old %s import files", len(deleted), symbol)
    return deleted


def import_gld(start: date, end: date, key: str, output_dir: Path = PRIVATE_DIR) -> dict:
    return import_symbol("GLD", start, end, key, output_dir)


def import_symbol(symbol: str, start: date, end: date, key: str,
                  output_dir: Path = PRIVATE_DIR) -> dict:
    """Fetch one symbol with two requests, validate, then save private source files.

    Importing a range again is allowed only while the earlier import stops short
    of the requested end. The new data must contain a newer bar and leave every
    earlier raw bar unchanged; otherwise nothing is written.
    """
    _check_symbol(symbol)
    if start > end or end > date.today():
        raise ValueError("Expected start <= end <= today")
    paths = _paths(start, end, output_dir, symbol)
    bars_path, raw_path, manifest_path = paths
    previous = _previous_import(paths, end, symbol)

    base_url = f"https://api.tiingo.com/tiingo/daily/{symbol}"
    metadata, _ = _get_json(base_url, key)
    if not isinstance(metadata, dict) or str(metadata.get("ticker", "")).upper() != symbol:
        raise ValueError(f"Tiingo did not confirm {symbol} EOD coverage")
    available_start = _day(metadata.get("startDate"))
    available_end = _day(metadata.get("endDate"))
    if end < available_start or start > available_end:
        raise ValueError(f"Requested range is outside Tiingo's {symbol} coverage")

    query = urlencode({"startDate": start.isoformat(), "endDate": end.isoformat()})
    prices, raw = _get_json(f"{base_url}/prices?{query}", key)
    csv_bytes, count, first, last = _normalize_prices(prices, start, end, symbol)
    manifest = {"provider": "Tiingo", "product": "EOD", "symbol": symbol,
                "requested_start": start.isoformat(), "requested_end": end.isoformat(),
                "provider_start": available_start.isoformat(),
                "provider_end": available_end.isoformat(),
                "first_bar": first.isoformat(), "last_bar": last.isoformat(),
                "bars": count, "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "bars_sha256": hashlib.sha256(csv_bytes).hexdigest(),
                "price_policy": "Raw OHLCV for execution; adjusted fields retained separately"}
    result = "IMPORTED"
    if previous is not None:
        previous_manifest, previous_bytes = previous
        previous_last = _day(previous_manifest.get("last_bar"))
        if last <= previous_last:
            logger.info("Tiingo has no %s bar after %s yet; kept the earlier import",
                        symbol, previous_last)
            return {"symbol": symbol, "result": "UNCHANGED",
                    "bars": previous_manifest.get("bars"),
                    "last_bar": previous_last.isoformat(),
                    "previous_last_bar": previous_last.isoformat(),
                    "bars_file": str(bars_path),
                    "detail": f"Tiingo has no {symbol} bar after {previous_last} yet; "
                              "nothing was written. Try again later."}
        revised = _revised_dates(previous_bytes, csv_bytes)
        if revised:
            raise ValueError(f"Tiingo revised earlier {symbol} bars (" + ", ".join(revised[:5])
                             + "); the earlier import was kept. Review the change before "
                               "importing again.")
        manifest["replaces_fetched_at_utc"] = previous_manifest.get("fetched_at_utc")
        result = "REFRESHED"
    output_dir.mkdir(parents=True, exist_ok=True)
    _replace_import(paths, (csv_bytes, raw,
                            (json.dumps(manifest, indent=2) + "\n").encode("utf-8")),
                    None if previous is None else previous[0])
    logger.info("%s import %s through %s", symbol, result.lower(), last,
                extra={"fields": {"bars": count, "bars_sha256": manifest["bars_sha256"]}})
    pruned = prune_imports(symbol, output_dir)
    return {"symbol": symbol, "result": result, "bars": count,
            "previous_last_bar": None if previous is None else previous[0].get("last_bar"),
            "first_bar": manifest["first_bar"],
            "last_bar": manifest["last_bar"], "bars_sha256": manifest["bars_sha256"],
            "bars_file": str(bars_path), "manifest_file": str(manifest_path),
            "pruned_files": len(pruned)}
