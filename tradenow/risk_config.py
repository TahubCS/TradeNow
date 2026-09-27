"""User risk settings for paper trading, bounded by ceilings fixed in code.

The file is risk.toml in the private data directory. It can make trading
stricter than the defaults, or looser up to each ceiling, but never beyond it.
A missing file means the defaults, with automatic submission off. Anything
unexpected (an unknown key, a wrong type, a value outside its range, a file
that does not parse) stops the command instead of guessing.
"""

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, fields, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .equity import EquityConfig
from .settings import SETTINGS


RISK_FILE = SETTINGS.data_dir / "risk.toml"
MAX_RISK_FILE_BYTES = 10_000


@dataclass(frozen=True)
class RiskConfig:
    max_position_fraction: Decimal = Decimal("0.50")
    daily_loss_limit_fraction: Decimal = Decimal("0.02")
    max_drawdown_fraction: Decimal = Decimal("0.10")
    buy_limit_buffer: Decimal = Decimal("0.01")
    max_volume_fraction: Decimal = Decimal("0.01")
    auto_submit: bool = False

    def equity_config(self, base: EquityConfig = EquityConfig()) -> EquityConfig:
        """The simulator settings used for strategy selection match the paper limits."""
        return replace(base, max_position_fraction=self.max_position_fraction,
                       max_drawdown_fraction=self.max_drawdown_fraction)


# (lowest allowed, hard ceiling). No setting can be raised past its ceiling.
LIMITS: dict[str, tuple[Decimal, Decimal]] = {
    "max_position_fraction": (Decimal("0.01"), Decimal("1.00")),  # cash only, never margin
    "daily_loss_limit_fraction": (Decimal("0.001"), Decimal("0.05")),
    "max_drawdown_fraction": (Decimal("0.01"), Decimal("0.25")),
    "buy_limit_buffer": (Decimal("0"), Decimal("0.03")),
    "max_volume_fraction": (Decimal("0.0001"), Decimal("0.05")),
}


@dataclass(frozen=True)
class LoadedRisk:
    config: RiskConfig
    sha256: str
    source: str


def _decimal(name: str, value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"risk.toml: {name} must be a number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"risk.toml: {name} must be a number") from None
    low, high = LIMITS[name]
    if not number.is_finite() or not low <= number <= high:
        raise ValueError(f"risk.toml: {name} must be between {low} and {high}; got {number}")
    return number


def parse_risk(data: dict) -> RiskConfig:
    known = {item.name for item in fields(RiskConfig)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"risk.toml: unknown setting(s) {', '.join(unknown)}")
    values: dict[str, Any] = {}
    for name, value in data.items():
        if name == "auto_submit":
            if not isinstance(value, bool):
                raise ValueError("risk.toml: auto_submit must be true or false")
            values[name] = value
        else:
            values[name] = _decimal(name, value)
    return replace(RiskConfig(), **values)


def risk_sha256(config: RiskConfig) -> str:
    """Hash of the effective values, so comments or formatting never change it."""
    body = json.dumps({key: str(value) for key, value in asdict(config).items()},
                      sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def default_risk() -> LoadedRisk:
    config = RiskConfig()
    return LoadedRisk(config, risk_sha256(config), "defaults (no risk.toml)")


def load_risk(path: Path = RISK_FILE) -> LoadedRisk:
    if not path.exists():
        return default_risk()
    if path.stat().st_size > MAX_RISK_FILE_BYTES:
        raise ValueError("risk.toml is unexpectedly large")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"risk.toml does not parse: {error}") from None
    config = parse_risk(data)
    return LoadedRisk(config, risk_sha256(config), str(path))
