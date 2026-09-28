"""How a fixed mix behaved historically (ADR-015, information only).

The mix runs through the portfolio simulator with fixed-mix mode's rules:
checks at the first bar and each quarter's last close, a 5-point band per
asset, $0.01 slippage, whole shares, and no drawdown halt. SPY alone is
shown beside it for context. This is not a test and cannot pass anything;
choosing a mix by its best past return is hindsight.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

from .mix_config import Mix
from .mix_rules import BAND
from .portfolio import PortfolioConfig, benchmark_spy, signal_days, simulate_portfolio
from .universe import Universe


PREVIEW_CONFIG = PortfolioConfig(rebalance_band=BAND, drawdown_halt=False)


def quarterly_schedule(dates: list[date]) -> list[int]:
    """The first bar and the last trading day of March, June, September, December."""
    return [index for index in signal_days(dates)
            if index == 0 or dates[index].month in (3, 6, 9, 12)]


def _years(curve: list[dict], start: Decimal) -> dict[str, str]:
    """Calendar-year returns from year-end equity (the first year is partial)."""
    ends: dict[str, Decimal] = {}
    for day in curve:
        ends[day["date"][:4]] = Decimal(day["equity"])
    years, previous = {}, start
    for year, equity in ends.items():
        years[year] = str(((equity / previous - 1) * 100).quantize(Decimal("0.01")))
        previous = equity
    return years


def _worst_drawdown(curve: list[dict], start: Decimal) -> dict:
    peak, peak_date = start, curve[0]["date"]
    worst = {"pct": "0.000", "peak_date": None, "trough_date": None, "recovered_date": None}
    worst_value, worst_peak = Decimal(0), start
    for day in curve:
        equity = Decimal(day["equity"])
        if equity >= peak:
            if worst["trough_date"] and worst["recovered_date"] is None and equity >= worst_peak:
                worst["recovered_date"] = day["date"]
            peak, peak_date = equity, day["date"]
        drawdown = (peak - equity) / peak
        if drawdown > worst_value:
            worst_value, worst_peak = drawdown, peak
            worst = {"pct": str((drawdown * 100).quantize(Decimal("0.001"))),
                     "peak_date": peak_date, "trough_date": day["date"], "recovered_date": None}
    return worst


def _summary(result: dict, start: Decimal) -> dict:
    metrics = result["metrics"]
    years = _years(result["equity_curve"], start)
    full = dict(list(years.items())[1:]) or years  # skip the partial first year
    worst = min(full, key=lambda year: Decimal(full[year]))
    best = max(full, key=lambda year: Decimal(full[year]))
    return {"total_return_pct": result["total_return_pct"],
            "annualized_return_pct": metrics["annualized_return_pct"],
            "annualized_volatility_pct": metrics["annualized_volatility_pct"],
            "sharpe": metrics["sharpe"], "max_drawdown_pct": result["max_drawdown_pct"],
            "worst_drawdown": _worst_drawdown(result["equity_curve"], start),
            "worst_year": [worst, full[worst]], "best_year": [best, full[best]],
            "fills": len(result["fills"]), "turnover_pct": result["turnover_pct"],
            "calendar_year_returns_pct": years}


def preview(universe: Universe, mix: Mix,
            config: PortfolioConfig = PREVIEW_CONFIG) -> dict:
    missing = [symbol for symbol in mix.targets if symbol not in universe.symbols]
    if missing:
        raise ValueError(f"No history loaded for {', '.join(missing)}")
    config = replace(config, drawdown_halt=False)
    weights = dict(mix.targets)
    result = simulate_portfolio(universe, lambda _views: weights, config,
                                schedule=quarterly_schedule(universe.dates))
    report = {"mode": "mix_preview", "adr": "ADR-015", "information_only": True,
              "mix": {symbol: str(weight) for symbol, weight in mix.targets.items()},
              "cash_weight": str(mix.cash_weight), "mix_sha256": mix.sha256,
              "first_date": universe.dates[0].isoformat(),
              "last_date": universe.dates[-1].isoformat(), "days": len(universe.dates),
              "data_sha256": universe.sha256,
              "rules": {"rebalance": "first bar and each quarter's last close",
                        "band_pct_of_equity": str(BAND * 100),
                        "slippage_per_share": str(config.slippage_per_share),
                        "drawdown_halt": False},
              "mix_result": _summary(result, config.starting_cash)}
    if "SPY" in universe.symbols:
        report["spy_alone"] = _summary(benchmark_spy(universe, config), config.starting_cash)
    report["notes"] = [
        "Information only: past behaviour, not a forecast or a test.",
        "Choose a mix by the drops you could live through, not by the best past return.",
        "Starts from the first day every symbol has history; later-starting ETFs "
        "shorten the period."]
    return report


def render_preview(report: dict) -> str:
    rows = ["# Mix preview (ADR-015, information only)", "",
            "Mix: " + ", ".join(f"{symbol} {weight}" for symbol, weight in report["mix"].items())
            + f"; cash {report['cash_weight']}", "",
            f"{report['days']} trading days, {report['first_date']} to {report['last_date']}.", "",
            "| | Total | Annualized | Volatility | Sharpe | Worst drop | Worst year | Best year |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for label, key in (("This mix", "mix_result"), ("SPY alone", "spy_alone")):
        if key not in report:
            continue
        item = report[key]
        rows.append(f"| {label} | {item['total_return_pct']}% | {item['annualized_return_pct']}% | "
                    f"{item['annualized_volatility_pct']}% | {item['sharpe'] or 'n/a'} | "
                    f"{item['max_drawdown_pct']}% | {item['worst_year'][0]} "
                    f"{item['worst_year'][1]}% | {item['best_year'][0]} {item['best_year'][1]}% |")
    worst = report["mix_result"]["worst_drawdown"]
    rows += ["", f"Worst drop for this mix: {worst['pct']}% from {worst['peak_date']} to "
             f"{worst['trough_date']}; " + (f"back to its peak by {worst['recovered_date']}."
                                            if worst["recovered_date"] else
                                            "not yet back to that peak."), "",
             *(f"- {note}" for note in report["notes"]), ""]
    return "\n".join(rows)
