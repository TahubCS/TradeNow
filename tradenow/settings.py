"""Paths and trading mode, resolved in one place from the environment.

TRADENOW_DATA_DIR moves all private state (Tiingo imports, the paper ledger,
plans, and logs) together; a relative value is resolved against the project
root, never the current directory. TRADENOW_MODE may only be "paper": there is
no live mode, and any other value stops the program at import time.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODES = ("paper",)


@dataclass(frozen=True)
class Settings:
    mode: str
    data_dir: Path

    @property
    def tiingo_dir(self) -> Path:
        return self.data_dir / "tiingo"

    @property
    def alpaca_dir(self) -> Path:
        return self.data_dir / "alpaca"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


def load_settings(environ: Mapping[str, str] = os.environ) -> Settings:
    mode = environ.get("TRADENOW_MODE", "paper").strip().lower()
    if mode not in MODES:
        raise ValueError(f"TRADENOW_MODE must be one of {', '.join(MODES)}; got {mode!r}")
    configured = environ.get("TRADENOW_DATA_DIR", "").strip()
    data_dir = Path(configured) if configured else Path("data") / "private"
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    return Settings(mode, data_dir)


SETTINGS = load_settings()
