"""The owner's fixed mix (ADR-015), read from data/private/mix.toml.

    [targets]
    SPY = 0.60
    AGG = 0.40

The system never suggests a mix; it only checks that the file is well formed.
An unknown key, a bad symbol, a weight out of range, more than four decimal
places, or weights adding up to more than 1 stops the command.
"""

import hashlib
import re
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .settings import SETTINGS


MIX_FILE = SETTINGS.data_dir / "mix.toml"
MAX_MIX_FILE_BYTES = 10_000
MAX_SYMBOLS = 12
WEIGHT_PLACES = Decimal("0.0001")
SYMBOL = re.compile(r"[A-Z]{1,5}")


@dataclass(frozen=True)
class Mix:
    targets: dict[str, Decimal]  # symbol -> weight, in the file's order
    sha256: str  # identifies this mix; a change triggers a rebalance

    @property
    def cash_weight(self) -> Decimal:
        return 1 - sum(self.targets.values(), Decimal(0))


def parse_mix(data: dict) -> dict[str, Decimal]:
    unknown = sorted(set(data) - {"targets"})
    if unknown:
        raise ValueError(f"mix.toml: unknown key(s) {', '.join(unknown)}")
    targets = data.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise ValueError("mix.toml: a [targets] table with at least one ETF is required")
    if len(targets) > MAX_SYMBOLS:
        raise ValueError(f"mix.toml: at most {MAX_SYMBOLS} ETFs")
    parsed: dict[str, Decimal] = {}
    for symbol, value in targets.items():
        if not SYMBOL.fullmatch(symbol):
            raise ValueError(f"mix.toml: {symbol!r} is not an ETF symbol (1 to 5 capitals)")
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"mix.toml: the weight of {symbol} must be a number")
        try:
            weight = Decimal(str(value))
        except InvalidOperation:
            raise ValueError(f"mix.toml: the weight of {symbol} is not a number") from None
        if not weight.is_finite() or not 0 < weight <= 1:
            raise ValueError(f"mix.toml: the weight of {symbol} must be above 0 and at most 1")
        if weight != weight.quantize(WEIGHT_PLACES):
            raise ValueError(f"mix.toml: the weight of {symbol} has more than 4 decimal places")
        parsed[symbol] = weight
    if sum(parsed.values(), Decimal(0)) > 1:
        raise ValueError("mix.toml: the weights add up to more than 1")
    return parsed


def load_mix(path: Path = MIX_FILE) -> Mix:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ValueError(f"No mix file at {path}; write your [targets] there first "
                         "(see docs/decisions.md, ADR-015)") from None
    if len(raw) > MAX_MIX_FILE_BYTES:
        raise ValueError("mix.toml is too large")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"mix.toml does not parse: {error}") from None
    targets = parse_mix(data)
    canonical = ";".join(f"{symbol}={weight}" for symbol, weight in targets.items())
    return Mix(targets, hashlib.sha256(canonical.encode()).hexdigest())
