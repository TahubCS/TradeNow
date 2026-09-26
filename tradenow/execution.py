"""In-memory order manager and broker for offline simulations only."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ApprovedOrder:
    order_id: str
    date: str
    side: str
    opening_price: Decimal


class SimulatedBroker:
    def __init__(self, starting_cash: Decimal, multiplier: Decimal,
                 slippage_per_side: Decimal, commission_per_side: Decimal):
        self.cash = starting_cash
        self.multiplier = multiplier
        self.slippage_per_side = slippage_per_side
        self.commission_per_side = commission_per_side
        self.entry_price: Decimal | None = None
        self.entry_date: str | None = None
        self.fills: list[dict] = []
        self.closed_trades: list[dict] = []
        self._orders: dict[str, tuple[ApprovedOrder, dict]] = {}

    def submit(self, order: ApprovedOrder) -> dict:
        previous = self._orders.get(order.order_id)
        if previous is not None:
            if previous[0] != order:
                raise ValueError(f"Order ID reused for different order: {order.order_id}")
            return previous[1]
        if not order.order_id or order.side not in ("BUY", "SELL"):
            raise ValueError("Invalid simulated order")
        if not order.opening_price.is_finite() or order.opening_price <= 0:
            raise ValueError("Invalid opening price")
        if order.side == "BUY" and self.entry_price is not None:
            raise ValueError("Cannot buy while already holding a contract")
        if order.side == "SELL" and self.entry_price is None:
            raise ValueError("Cannot sell without an open contract")

        fill_price = (order.opening_price + self.slippage_per_side if order.side == "BUY"
                      else order.opening_price - self.slippage_per_side)
        if fill_price <= 0:
            raise ValueError("Simulated fill price must be positive")
        fill = {"order_id": order.order_id, "date": order.date,
                "side": order.side, "price": str(fill_price), "contracts": 1}
        if order.side == "BUY":
            self.cash -= self.commission_per_side
            self.entry_price = fill_price
            self.entry_date = order.date
        else:
            gross_pnl = (fill_price - self.entry_price) * self.multiplier
            self.cash += gross_pnl - self.commission_per_side
            self.closed_trades.append({"entry_date": self.entry_date,
                                       "exit_date": order.date,
                                       "gross_pnl": str(gross_pnl),
                                       "net_pnl": str(gross_pnl - 2 * self.commission_per_side)})
            self.entry_price = None
            self.entry_date = None
        self.fills.append(fill)
        self._orders[order.order_id] = (order, fill)
        return fill

    def equity(self, close: Decimal) -> Decimal:
        return (self.cash if self.entry_price is None else
                self.cash + (close - self.entry_price) * self.multiplier)

    def reconcile(self) -> None:
        net_fills = sum(1 if fill["side"] == "BUY" else -1 for fill in self.fills)
        if net_fills != int(self.entry_price is not None):
            raise RuntimeError("Simulated broker position does not match fill ledger")
