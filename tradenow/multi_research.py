"""Multi-asset research runs: report, verdict, and files.

A Study names what is being tested: the ADR-011 rules or the ADR-012 models,
with the code that can change their result. run_study evaluates its
candidates on an aligned universe and returns a report whose run ID hashes
the data, code, candidates, and configuration. Identical inputs give
byte-identical reports: nothing here reads the clock. Loading data and
writing the experiment log happen in the command, not here.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .experiments import MULTI_STRATEGY_FAMILY
from .features import FEATURE_VERSION, feature_code_sha256
from .live_gate import multi_research_gate, verdict
from .multi_evaluation import PortfolioCandidate, evaluate_multi
from .multi_strategies import MULTI_CANDIDATES
from .portfolio import PortfolioConfig
from .universe import UNIVERSE, Universe


# Code shared by every study whose change can change a result.
CORE_CODE = ("equity_types.py", "features.py", "strategies.py", "metrics.py",
             "selection.py", "gld_evaluation.py", "universe.py", "portfolio.py",
             "multi_strategies.py", "multi_evaluation.py", "multi_research.py",
             "live_gate.py")


@dataclass(frozen=True)
class Study:
    mode: str
    title: str
    adr: str
    strategy_family: str  # the experiment log's strategy version
    candidates: tuple[PortfolioCandidate, ...]
    registered_trials_total: int  # every registered candidate so far, all ADRs
    code: tuple[str, ...] = CORE_CODE


MULTI_STUDY = Study("multi_asset_research", "Multi-asset research report", "ADR-011",
                    MULTI_STRATEGY_FAMILY, MULTI_CANDIDATES, 18)


def code_sha256(files: tuple[str, ...] = CORE_CODE) -> str:
    digest = hashlib.sha256()
    for name in files:
        digest.update(name.encode("utf-8"))
        digest.update((Path(__file__).parent / name).read_bytes())
    return digest.hexdigest()


def _meaning(passed: bool, adr: str) -> str:
    if passed:
        return (f"Research stage passed R1 to R5. Per {adr}, the next step is a new ADR for "
                "paper trading and a year of forward testing (F1 to F4). No real money; "
                "nothing is enabled automatically.")
    return ("Not proven to beat both an equal-weight mix of the six ETFs (B1) and SPY (B2). "
            "Do not trade real money; a low-cost index fund is expected to do at least as "
            f"well. Per {adr}, the candidates are not tuned and re-run.")


def run_study(universe: Universe, study: Study,
              config: PortfolioConfig = PortfolioConfig()) -> dict:
    if universe.symbols != UNIVERSE:
        raise ValueError(f"Multi-asset research needs exactly {', '.join(UNIVERSE)} in order")
    candidates = study.candidates
    evaluation = evaluate_multi(universe, config, candidates)
    config_record = {key: str(value) for key, value in vars(config).items()}
    code_hash = code_sha256(study.code)
    run_inputs = {"schema_version": 1, "mode": study.mode, "data_hash": universe.sha256,
                  "code_hash": code_hash,
                  "candidates": [[item.name, item.parameters] for item in candidates],
                  "config": config_record}
    run_id = hashlib.sha256(json.dumps(run_inputs, sort_keys=True).encode()).hexdigest()[:16]
    research = multi_research_gate(evaluation, len(candidates))
    gate = verdict(research, None)
    gate["meaning"] = _meaning(research["passed"], study.adr)
    return {
        "schema_version": 1, "mode": study.mode, "run_id": run_id,
        "title": study.title, "adr": study.adr, "strategy_family": study.strategy_family,
        "code_sha256": code_hash, "config": config_record,
        "candidates": [item.name for item in candidates],
        "candidate_parameters": {item.name: item.parameters for item in candidates},
        "registered_trials_total": study.registered_trials_total,
        "feature_version": FEATURE_VERSION, "feature_code_sha256": feature_code_sha256(),
        "data": {"symbols": list(universe.symbols), "bars": len(universe.dates),
                 "first_date": universe.dates[0].isoformat(),
                 "last_date": universe.dates[-1].isoformat(), "sha256": universe.sha256,
                 "price_basis": "dividend- and split-adjusted",
                 "assets": {symbol: {"source": asset.source_name, "sha256": asset.sha256,
                                     "dividends": asset.dividends, "splits": asset.splits}
                            for symbol, asset in universe.assets.items()}},
        "evaluation": evaluation,
        "live_gate": gate,
        "limits": ["provider prices are not independently verified",
                   "daily bars; simulated fills at the open with fixed per-share slippage",
                   "orders are sized at the fill-day open, which live orders cannot do",
                   "the drawdown halt checks closing equity and exits at the next open; "
                   "it does not cap losses",
                   "the holdout is information only; the gate uses the rolling windows",
                   f"{study.registered_trials_total} candidates have been registered across "
                   "all ADRs; the more are tried, the likelier one passes by luck"],
    }


def run_multi(universe: Universe, config: PortfolioConfig = PortfolioConfig()) -> dict:
    """The ADR-011 rule candidates."""
    return run_study(universe, MULTI_STUDY, config)


def _row(label: str, item: dict) -> str:
    return (f"| {label} | {item['total_return_pct']}% | {item['max_drawdown_pct']}% | "
            f"{item['metrics']['sharpe'] or 'n/a'} | {item['closed_trades']} | "
            f"{item['turnover_pct']}% |")


def render_multi_markdown(report: dict) -> str:
    data, evaluation, gate = report["data"], report["evaluation"], report["live_gate"]
    selection = evaluation["selection"]
    rows = [
        f"# {report['title']} ({report['adr']})", "",
        f"Run ID: `{report['run_id']}`  ",
        f"Symbols: {', '.join(data['symbols'])}  ",
        f"Bars: {data['bars']} ({data['first_date']} to {data['last_date']}), "
        f"{data['price_basis']} prices  ",
        f"Data SHA-256: `{data['sha256']}`  ",
        f"Code SHA-256: `{report['code_sha256']}`  ",
        f"Candidates: {len(report['candidates'])} ({', '.join(report['candidates'])}); "
        f"{report['registered_trials_total']} registered across all ADRs", "",
        f"## Gate (ADR-011 R1 to R5): **{gate['verdict']}**", "", gate["meaning"], "",
        "| Check | Result | Detail |", "| --- | --- | --- |",
    ]
    for item in gate["research"]["checks"]:
        rows.append(f"| {item['check']} | {'pass' if item['passed'] else 'FAIL'} | "
                    f"{item['detail']} |")
    rows += ["| F1-F4 forward paper stage | not evaluated here | needs MA6 |", ""]
    rolling = evaluation["rolling_pre_holdout"]
    summary = rolling["summary"]
    rows += [
        "## Rolling tests before the holdout (the evidence)", "",
        f"{rolling['development_bars']} development, {rolling['validation_bars']} validation, "
        f"and {rolling['test_bars']} test bars per window; test blocks do not overlap. "
        f"Slippage ${rolling['slippage_per_share']} per share.", "",
        "| | Strategy | B1 equal weight | B2 SPY |", "| --- | ---: | ---: | ---: |",
        f"| Compounded return | {summary['compounded_return_pct']}% | "
        f"{summary['compounded_b1_return_pct']}% | {summary['compounded_b2_return_pct']}% |",
        f"| Chained max drawdown | {summary['chained_max_drawdown_pct']}% | "
        f"{summary['chained_b1_max_drawdown_pct']}% | "
        f"{summary['chained_b2_max_drawdown_pct']}% |", "",
        f"Windows: {summary['windows']}; beat B1: {summary['beat_b1_windows']}; beat B2: "
        f"{summary['beat_b2_windows']}; beat both: {summary['beat_both_windows']}; positive: "
        f"{summary['positive_windows']}; selected a strategy: {summary['selected_windows']}; "
        f"closed round trips: {summary['closed_trades']}.", "",
        "| Test dates | Selected | Strategy | B1 | B2 |", "| --- | --- | ---: | ---: | ---: |",
    ]
    for window in rolling["windows"]:
        rows.append(f"| {window['test_start']} to {window['test_end']} | "
                    f"{window['selected_hypothesis'] or 'CASH'} | {window['test_return_pct']}% | "
                    f"{window['b1_return_pct']}% | {window['b2_return_pct']}% |")
    stressed = evaluation["rolling_pre_holdout_stressed"]["summary"]
    rows += [
        "", f"At ${evaluation['rolling_pre_holdout_stressed']['slippage_per_share']} per share: "
        f"strategy {stressed['compounded_return_pct']}%, B1 "
        f"{stressed['compounded_b1_return_pct']}%, B2 {stressed['compounded_b2_return_pct']}%.",
        "", "## Holdout (information only, not evidence)", "",
        f"Selected from the 60/20 periods: **{selection['selected_hypothesis'] or 'CASH'}** "
        f"(best {selection['best_candidate']}, validation score "
        f"{selection['best_validation_score']}).", "",
        "| Portfolio | Return | Max drawdown | Sharpe | Round trips | Turnover |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        _row("Selected strategy", evaluation["holdout"]["strategy"]),
        _row("B1 equal weight", evaluation["holdout"]["b1"]),
        _row("B2 SPY", evaluation["holdout"]["b2"]), "",
        "## Validation scores (60/20 selection)", "",
        "| Candidate | Validation return | Validation max drawdown | Score |",
        "| --- | ---: | ---: | ---: |",
    ]
    for item in selection["hypotheses"]:
        rows.append(f"| {item['name']} | {item['validation']['total_return_pct']}% | "
                    f"{item['validation']['max_drawdown_pct']}% | {item['validation_score']} |")
    rows += ["", "## Limits", "", *(f"- {item}" for item in report["limits"]), ""]
    return "\n".join(rows)


def save_multi_report(report: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"report-{report['run_id']}"
    json_path, md_path = output_dir / f"{stem}.json", output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_multi_markdown(report), encoding="utf-8")
    return json_path, md_path
