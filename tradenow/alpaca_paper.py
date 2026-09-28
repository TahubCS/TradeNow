"""Small, paper-only Alpaca client. No live trading endpoint is accepted.

Every response is validated into a typed record here, so the rest of the
system never handles raw Alpaca payloads.
"""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .logs import register_secret
from .settings import PROJECT_ROOT


PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
DATA_ENDPOINT = "https://data.alpaca.markets"
# Alpaca's dashboard shows the paper URL with "/v2"; both spellings mean the same host.
ACCEPTED_ENDPOINTS = {PAPER_ENDPOINT, PAPER_ENDPOINT + "/", PAPER_ENDPOINT + "/v2",
                      PAPER_ENDPOINT + "/v2/"}
ENV_NAMES = ("ALPACA_PAPER_ENDPOINT", "ALPACA_PAPER_KEY_ID",
             "ALPACA_PAPER_SECRET_KEY")
MAX_RESPONSE_BYTES = 1_000_000
# About 250 KB of JSON per page, well inside the response limit.
HISTORY_PAGE_BARS = 2000
MAX_HISTORY_PAGES = 20
MAX_ORDER_SHARES = 10_000
FRACTION_BEYOND_MICROS = re.compile(r"(\.\d{6})\d+")
CLIENT_ORDER_ID = re.compile(r"tn-gld-[a-z0-9-]{1,48}")
MIX_CLIENT_ORDER_ID = re.compile(r"tn-mix-[a-z0-9-]{1,48}")
SYMBOL = re.compile(r"[A-Z]{1,5}")
# Statuses after which Alpaca will not fill an order further.
FINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})


class AlpacaError(ValueError):
    """A sanitized Alpaca failure; never contains credentials."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class PaperCredentials:
    endpoint: str
    key_id: str = field(repr=False)
    secret_key: str = field(repr=False)


@dataclass(frozen=True)
class Account:
    status: str
    currency: str
    cash: Decimal
    equity: Decimal
    is_paper_account: bool
    trading_blocked: bool
    # Equity at the previous trading day's close; the daily loss limit's baseline.
    last_equity: Decimal | None = None


@dataclass(frozen=True)
class Clock:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True)
class Position:
    symbol: str
    shares: int


@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: int
    filled_qty: int
    filled_avg_price: Decimal | None
    status: str
    submitted_at: datetime | None = None
    filled_at: datetime | None = None

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_ORDER_STATUSES


@dataclass(frozen=True)
class DailyBar:
    date: date
    open: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class PaperOrder:
    """The only order shapes this system may send: whole-share GLD, day only.

    Buys are limit orders so an upward overnight gap cannot fill at any price.
    Sells are market orders so a risk exit is not blocked by a limit.
    """
    client_order_id: str
    side: str
    qty: int
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not CLIENT_ORDER_ID.fullmatch(self.client_order_id):
            raise ValueError("Invalid GLD paper client order ID")
        if self.side not in ("buy", "sell"):
            raise ValueError("GLD paper order side must be buy or sell")
        if (isinstance(self.qty, bool) or not isinstance(self.qty, int)
                or not 0 < self.qty <= MAX_ORDER_SHARES):
            raise ValueError(f"GLD paper order must be 1 to {MAX_ORDER_SHARES} whole shares")
        if self.side == "sell" and self.limit_price is not None:
            raise ValueError("GLD paper sells must be market orders")
        if self.side == "buy":
            price = self.limit_price
            if (price is None or not price.is_finite() or price <= 0
                    or price != price.quantize(Decimal("0.01"))):
                raise ValueError("GLD paper buys need a positive whole-cent limit price")

    def payload(self) -> dict:
        body = {"symbol": "GLD", "qty": str(self.qty), "side": self.side,
                "time_in_force": "day", "client_order_id": self.client_order_id,
                "type": "market" if self.limit_price is None else "limit"}
        if self.limit_price is not None:
            body["limit_price"] = str(self.limit_price)
        return body


@dataclass(frozen=True)
class MixOrder:
    """Fixed-mix orders (ADR-015): one ETF, whole shares, day only. Buys are
    limit orders so a gap cannot fill at any price and the total stays within
    cash; sells are market orders so a rebalance or exit is never blocked."""
    client_order_id: str
    symbol: str
    side: str
    qty: int
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not MIX_CLIENT_ORDER_ID.fullmatch(self.client_order_id):
            raise ValueError("Invalid mix client order ID")
        if not SYMBOL.fullmatch(self.symbol):
            raise ValueError("Invalid mix order symbol")
        if self.side not in ("buy", "sell"):
            raise ValueError("Mix order side must be buy or sell")
        if (isinstance(self.qty, bool) or not isinstance(self.qty, int)
                or not 0 < self.qty <= MAX_ORDER_SHARES):
            raise ValueError(f"Mix order must be 1 to {MAX_ORDER_SHARES} whole shares")
        if self.side == "sell" and self.limit_price is not None:
            raise ValueError("Mix sells must be market orders")
        if self.side == "buy":
            price = self.limit_price
            if (price is None or not price.is_finite() or price <= 0
                    or price != price.quantize(Decimal("0.01"))):
                raise ValueError("Mix buys need a positive whole-cent limit price")

    def payload(self) -> dict:
        body = {"symbol": self.symbol, "qty": str(self.qty), "side": self.side,
                "time_in_force": "day", "client_order_id": self.client_order_id,
                "type": "market" if self.limit_price is None else "limit"}
        if self.limit_price is not None:
            body["limit_price"] = str(self.limit_price)
        return body


def load_paper_credentials(env_file: Path | None = None) -> PaperCredentials:
    """Read only the three paper variables; never expose their values in errors."""
    values = {name: os.environ.get(name, "").strip() for name in ENV_NAMES}
    from_file: set[str] = set()
    path = env_file or PROJECT_ROOT / ".env.local"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, value = stripped.split("=", 1)
            name = name.strip()
            if name not in values:
                continue
            if name in from_file:
                raise ValueError(f"Duplicate {name} in .env.local")
            from_file.add(name)
            if not values[name]:
                values[name] = value.strip().strip('"\'')
    if values["ALPACA_PAPER_ENDPOINT"] not in ACCEPTED_ENDPOINTS:
        raise ValueError("ALPACA_PAPER_ENDPOINT must be the Alpaca paper URL "
                         f"({PAPER_ENDPOINT})")
    for name in ENV_NAMES[1:]:
        if not values[name] or any(char.isspace() for char in values[name]):
            raise ValueError(f"{name} is missing or malformed")
        register_secret(values[name])
    return PaperCredentials(PAPER_ENDPOINT, values[ENV_NAMES[1]], values[ENV_NAMES[2]])


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise AlpacaError("Alpaca API redirected; request stopped")


def _alpaca_message(error: HTTPError) -> str:
    """Keep Alpaca's short explanation (never credentials) for diagnosis."""
    try:
        body = json.loads(error.read(4_000).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return ""
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, str):
        return ""
    return ": " + "".join(char for char in message if char.isprintable())[:200]


def _text(item: dict, name: str) -> str:
    value = item.get(name)
    if not isinstance(value, str) or not value:
        raise AlpacaError(f"Alpaca returned an invalid {name}")
    return value


def _flag(item: dict, name: str) -> bool:
    value = item.get(name)
    if not isinstance(value, bool):
        raise AlpacaError(f"Alpaca returned an invalid {name}")
    return value


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise AlpacaError(f"Alpaca returned an invalid {name}")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise AlpacaError(f"Alpaca returned an invalid {name}") from None
    if not number.is_finite():
        raise AlpacaError(f"Alpaca returned an invalid {name}")
    return number


def _whole_shares(value: object, name: str) -> int:
    number = _decimal(value, name)
    if number != number.to_integral_value():
        raise AlpacaError(f"Alpaca returned fractional GLD shares in {name}")
    return int(number)


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise AlpacaError(f"Alpaca returned an invalid {name}")
    # Alpaca may send nanoseconds; Python keeps at most microseconds.
    trimmed = FRACTION_BEYOND_MICROS.sub(r"\1", value)
    try:
        parsed = datetime.fromisoformat(trimmed)
    except ValueError:
        raise AlpacaError(f"Alpaca returned an invalid {name}") from None
    if parsed.tzinfo is None:
        raise AlpacaError(f"Alpaca returned a timestamp without an offset in {name}")
    return parsed


def _optional_timestamp(value: object, name: str) -> datetime | None:
    return None if value is None else _timestamp(value, name)


def _object(value: object, what: str) -> dict:
    if not isinstance(value, dict):
        raise AlpacaError(f"Alpaca returned a malformed {what}")
    return value


def parse_account(raw: object) -> Account:
    item = _object(raw, "account")
    blocked = (_flag(item, "trading_blocked") or _flag(item, "account_blocked")
               or _flag(item, "trade_suspended_by_user"))
    return Account(status=_text(item, "status"), currency=_text(item, "currency"),
                   cash=_decimal(item.get("cash"), "cash"),
                   equity=_decimal(item.get("equity"), "equity"),
                   is_paper_account=_text(item, "account_number").startswith("PA"),
                   trading_blocked=blocked,
                   last_equity=_decimal(item.get("last_equity"), "last_equity"))


def parse_clock(raw: object) -> Clock:
    item = _object(raw, "clock")
    return Clock(timestamp=_timestamp(item.get("timestamp"), "timestamp"),
                 is_open=_flag(item, "is_open"),
                 next_open=_timestamp(item.get("next_open"), "next_open"),
                 next_close=_timestamp(item.get("next_close"), "next_close"))


def parse_position(raw: object) -> Position:
    item = _object(raw, "position")
    shares = _whole_shares(item.get("qty"), "position qty")
    if _text(item, "side") != "long" or shares <= 0:
        raise AlpacaError("Alpaca returned a non-long position")
    return Position(symbol=_text(item, "symbol"), shares=shares)


def parse_order(raw: object) -> BrokerOrder:
    item = _object(raw, "order")
    price = item.get("filled_avg_price")
    order = BrokerOrder(broker_order_id=_text(item, "id"),
                        client_order_id=_text(item, "client_order_id"),
                        symbol=_text(item, "symbol"), side=_text(item, "side"),
                        qty=_whole_shares(item.get("qty"), "order qty"),
                        filled_qty=_whole_shares(item.get("filled_qty"), "filled_qty"),
                        filled_avg_price=(None if price is None
                                          else _decimal(price, "filled_avg_price")),
                        status=_text(item, "status"),
                        submitted_at=_optional_timestamp(item.get("submitted_at"),
                                                         "submitted_at"),
                        filled_at=_optional_timestamp(item.get("filled_at"), "filled_at"))
    if not 0 <= order.filled_qty <= order.qty:
        raise AlpacaError("Alpaca returned an impossible filled quantity")
    return order


def parse_daily_bars(raw: object) -> list[DailyBar]:
    bars, token = parse_bar_page(raw)
    if token:
        raise AlpacaError("Alpaca bars response was unexpectedly paginated")
    return bars


def parse_bar_page(raw: object,
                   after: DailyBar | None = None) -> tuple[list[DailyBar], str | None]:
    """One page of daily bars and the token for the next page, if any. `after`
    is the previous page's last bar; this page must continue after it."""
    item = _object(raw, "bars response")
    token = item.get("next_page_token")
    if token is not None and (not isinstance(token, str) or not token):
        raise AlpacaError("Alpaca returned an invalid page token")
    rows = item.get("bars")
    if rows is None:
        return [], token
    if not isinstance(rows, list):
        raise AlpacaError("Alpaca returned malformed bars")
    bars: list[DailyBar] = []
    for row in rows:
        bar = _object(row, "bar")
        # Daily bars are stamped at midnight New York time (04:00 or 05:00 UTC).
        stamp = _timestamp(bar.get("t"), "bar timestamp")
        if stamp.utcoffset() or stamp.hour not in (4, 5) or stamp.minute or stamp.second:
            raise AlpacaError("Alpaca returned an unexpected daily bar timestamp")
        volume = bar.get("v")
        if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
            raise AlpacaError("Alpaca returned an invalid bar volume")
        open_price = _decimal(bar.get("o"), "bar open")
        close = _decimal(bar.get("c"), "bar close")
        previous = bars[-1] if bars else after
        if open_price <= 0 or close <= 0 or (previous and stamp.date() <= previous.date):
            raise AlpacaError("Alpaca returned invalid or unordered daily bars")
        bars.append(DailyBar(stamp.date(), open_price, close, volume))
    return bars, token


class PaperClient:
    def __init__(self, credentials: PaperCredentials):
        if credentials.endpoint != PAPER_ENDPOINT:
            raise ValueError("Only the Alpaca paper endpoint is allowed")
        self._credentials = credentials
        self._opener = build_opener(_NoRedirect())

    def _request(self, method: str, base: str, path: str, payload: dict | None = None,
                 missing_ok: bool = False) -> object:
        if (base not in (PAPER_ENDPOINT, DATA_ENDPOINT) or not path.startswith("/v2/")
                or "://" in path or (base == DATA_ENDPOINT and method != "GET")):
            raise ValueError("Invalid Alpaca paper API request")
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(base + path, data=body, method=method,
                          headers={"APCA-API-KEY-ID": self._credentials.key_id,
                                   "APCA-API-SECRET-KEY": self._credentials.secret_key,
                                   "Accept": "application/json",
                                   "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=10) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise AlpacaError(f"Alpaca API returned HTTP {error.code}"
                              f"{_alpaca_message(error)}", error.code) from None
        except (URLError, TimeoutError, OSError):
            raise AlpacaError("Alpaca API connection failed") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AlpacaError("Alpaca API response is too large")
        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AlpacaError("Alpaca API returned invalid JSON") from None

    def _list(self, path: str) -> list:
        items = self._request("GET", PAPER_ENDPOINT, path)
        if not isinstance(items, list):
            raise AlpacaError("Alpaca returned a malformed list")
        return items

    def account(self) -> Account:
        return parse_account(self._request("GET", PAPER_ENDPOINT, "/v2/account"))

    def clock(self) -> Clock:
        return parse_clock(self._request("GET", PAPER_ENDPOINT, "/v2/clock"))

    def positions(self) -> list[Position]:
        return [parse_position(item) for item in self._list("/v2/positions")]

    def open_orders(self) -> list[BrokerOrder]:
        return [parse_order(item) for item in
                self._list("/v2/orders?status=open&limit=500")]

    def gld_tradable(self) -> bool:
        asset = _object(self._request("GET", PAPER_ENDPOINT, "/v2/assets/GLD"), "asset")
        return (asset.get("symbol") == "GLD" and asset.get("status") == "active"
                and asset.get("tradable") is True)

    def order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        path = "/v2/orders:by_client_order_id?" + urlencode(
            {"client_order_id": client_order_id})
        raw = self._request("GET", PAPER_ENDPOINT, path, missing_ok=True)
        return None if raw is None else parse_order(raw)

    def submit_gld(self, order: PaperOrder) -> BrokerOrder:
        submitted = parse_order(self._request("POST", PAPER_ENDPOINT, "/v2/orders",
                                              order.payload()))
        if (submitted.client_order_id != order.client_order_id
                or submitted.symbol != "GLD" or submitted.side != order.side
                or submitted.qty != order.qty):
            raise AlpacaError("Alpaca acknowledged a different order than was sent")
        return submitted

    def submit_mix(self, order: MixOrder) -> BrokerOrder:
        submitted = parse_order(self._request("POST", PAPER_ENDPOINT, "/v2/orders",
                                              order.payload()))
        if (submitted.client_order_id != order.client_order_id
                or submitted.symbol != order.symbol or submitted.side != order.side
                or submitted.qty != order.qty):
            raise AlpacaError("Alpaca acknowledged a different order than was sent")
        return submitted

    def asset_tradable(self, symbol: str) -> bool:
        if not SYMBOL.fullmatch(symbol):
            raise ValueError("Invalid symbol")
        asset = _object(self._request("GET", PAPER_ENDPOINT, f"/v2/assets/{symbol}"), "asset")
        return (asset.get("symbol") == symbol and asset.get("status") == "active"
                and asset.get("tradable") is True)

    def cancel_all_orders(self) -> int:
        """Kill-switch path: ask Alpaca to cancel every open order in the paper account."""
        result = self._request("DELETE", PAPER_ENDPOINT, "/v2/orders")
        if result is None:
            return 0
        if not isinstance(result, list):
            raise AlpacaError("Alpaca returned a malformed cancel response")
        return len(result)

    def daily_history(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        """Consolidated (SIP) raw daily bars from start through end, page by
        page, used only to cross-check Tiingo imports (ADR-013)."""
        if not symbol.isascii() or not symbol.isalpha() or not symbol.isupper():
            raise ValueError("Invalid symbol")
        bars: list[DailyBar] = []
        token: str | None = None
        for _ in range(MAX_HISTORY_PAGES):
            # Daily bars are stamped 04:00 or 05:00 UTC, so the next midnight
            # includes the end day.
            query = {"timeframe": "1Day", "start": start.isoformat(),
                     "end": (end + timedelta(days=1)).isoformat(), "adjustment": "raw",
                     "feed": "sip",
                     "limit": HISTORY_PAGE_BARS}
            if token:
                query["page_token"] = token
            page, token = parse_bar_page(
                self._request("GET", DATA_ENDPOINT,
                              f"/v2/stocks/{symbol}/bars?{urlencode(query)}"),
                bars[-1] if bars else None)
            bars.extend(page)
            if token is None:
                return bars
        raise AlpacaError(f"Alpaca returned more than {MAX_HISTORY_PAGES} pages of bars")

    def gld_daily_bars(self, start: date, end: datetime) -> list[DailyBar]:
        """Consolidated (SIP) raw daily bars, used only to cross-check Tiingo."""
        query = urlencode({"timeframe": "1Day", "start": start.isoformat(),
                           "end": end.isoformat(timespec="seconds"),
                           "adjustment": "raw", "feed": "sip", "limit": 1000})
        return parse_daily_bars(self._request("GET", DATA_ENDPOINT,
                                              f"/v2/stocks/GLD/bars?{query}"))
