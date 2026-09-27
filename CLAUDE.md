# TradeNow: notes for Claude Code

A research and **paper-trading-only** system for gold and a small ETF universe.
It imports daily bars from Tiingo, tests rule-based strategies without
look-ahead, and trades an Alpaca **paper** account. There is no live mode.

The owner works on Windows (PowerShell) in
`C:\Users\muhammad tahub\Downloads\Projects\Tradenow`, runs the scheduled
`paper-auto` there, and prefers a plan in plain language before larger
changes. When asked for a plan only, do not implement.

## Rules that are never broken

1. **Paper only.** Never add a live endpoint or mode. `TRADENOW_MODE` accepts
   only `paper`, and the Alpaca client accepts only the paper URL.
2. **The live-trading gate (ADR-008, `docs/decisions.md`).** No real money
   unless a strategy beats plain buy-and-hold out of sample, after costs, over
   enough trades, historically **and** in a year of forward paper trading.
   Thresholds may be tightened, never loosened, without a new ADR written
   before the results it would affect.
3. **Register before testing (ADR-010, ADR-011).** Candidates, parameters,
   benchmarks, and pass criteria are fixed in an ADR before any result exists.
   Never tune or re-run registered candidates after seeing their results; a
   new idea needs a new ADR and counts as more trials.
4. **No look-ahead.** Strategies read history only through `History`
   (`tradenow/strategies.py`), which cannot index past today. Features are
   point-in-time and a test proves it.
5. **Secrets stay private.** Keys live in the ignored `.env.local`. They are
   never written to logs (`logs.register_secret` redacts them), plans, reports,
   commits, or notifications. `data/private/` and `artifacts/` are git-ignored.
6. **Be honest about odds.** The GLD strategies failed the gate. Do not present
   anything as likely to make money, and do not give financial advice.

## Commands

```powershell
python -m pip install -e ".[dev,web,ml]"   # ruff/mypy (dev) and scikit-learn (ml) are pinned
ruff check tradenow tests
mypy
python -m unittest discover -s tests -v    # CI runs these three on Ubuntu and Windows
```

Main CLI (`python -m tradenow <command>`):

- Data: `tiingo-import --start D --end D [--symbols A,B | --universe]` and
  `universe` (aligned six-ETF history).
- Research: `gld` (12 registered GLD candidates plus the gate verdict),
  `multi` (6 registered multi-asset candidates against B1 and B2, R1 to R5,
  reports in `artifacts/multi/`), `ml` (4 registered ADR-012 models, same
  gate, reports in `artifacts/ml/`), and `features --date D`.
- Paper trading: `paper-plan`, `paper-submit --approve ID`, `paper-status`,
  `paper-report`, `paper-halt`, `paper-resume --confirm`, and
  `paper-auto [--dry-run | --check]`.
- Other: `notify-test`, and `web` (the dashboard).
- `scripts/schedule-windows.ps1` registers the Windows scheduled tasks.

Every command appends one line to `data/private/logs/runs.jsonl`.

## Where things are

- `data/private/` (set with `TRADENOW_DATA_DIR`) holds `tiingo/` imports,
  `alpaca/` (ledger, plans, kill switch, equity history, `live_gate.json`),
  `logs/`, `experiments.jsonl`, and optional `risk.toml`. Copy
  `risk.example.toml` to create it. Without it the defaults apply: 50%
  position, 2% daily loss, 10% drawdown, `auto_submit = false`.
- Research core: `equity_types.py`, `features.py` (`gld_features_v2`),
  `strategies.py` (interface and `GLD_CANDIDATES`), `selection.py` (single
  selection rule, `RESEARCH_CONFIG` at the 100% cap), `equity.py`
  (single-asset simulator), `gld_research.py` and `gld_evaluation.py`,
  `metrics.py`, `live_gate.py`, and `experiments.py`.
- Multi-asset data: `tiingo.py` (`import_symbol`, `prune_imports`,
  `latest_import`) and `universe.py` (`UNIVERSE`, adjusted prices,
  `align`, `load_universe`).
- Multi-asset simulation: `portfolio.py` (`simulate_portfolio` with an
  allocator over `History` views, `signal_days`, benchmarks
  `benchmark_equal_weight` (B1) and `benchmark_spy` (B2)).
- Multi-asset research: `multi_strategies.py` (`MULTI_CANDIDATES`, rules,
  `capped_inverse_volatility`), `multi_evaluation.py` (selection, holdout,
  rolling and stressed checks, chained drawdowns), `multi_research.py`
  (`Study`, `run_study`, report and Markdown), `live_gate.multi_research_gate`
  (R1 to R5).
- Machine learning (ADR-012, needs the `ml` extra): `ml_dataset.py` (monthly
  samples from `History` views, volatility-scaled labels), `ml_models.py`
  (the only module using scikit-learn and floats), `ml_strategies.py`
  (`ML_CANDIDATES`, cached `Predictor`), `ml_research.py` (`ML_STUDY`). The
  CLI imports these only for `ml`, so other commands run without them.
- Paper trading: `alpaca_paper.py` (adapter), `paper_rules.py` (pure rules),
  `paper_trading.py` (orchestration and store), `paper_auto.py`,
  `risk_config.py`, `notify.py`, and `execution_quality.py`.
- The MGC futures files (`simulation.py`, `offline.py`, `stress.py`,
  `market_data.py`, `synthetic.py`) are the older synthetic workflow. Leave
  them alone unless asked.

## Conventions

- Money and prices are `Decimal`, never float. Pure rules are separate from
  I/O so they can be tested without the network.
- Every change comes with tests. `tests/test_strategies.py` holds golden
  SHA-256 hashes of pre-refactor outputs, so a failure there means a result
  changed. Investigate; never simply re-bless the hashes.
- Identical inputs must give byte-identical reports (tested).
- Keep ruff and mypy clean and CI green on both operating systems.
- Commits have short imperative subjects with a body explaining why. Solo
  project: commit directly to `main` and push; no branches or pull requests.

## Status (2026-09-27)

- Phases 0 to 5 are done. Phase 6 (unattended paper trading) is built and
  running as a dry run on the owner's PC. It passes after 20 unattended
  sessions with no reconciliation failures.
- **GLD research (ADR-010) failed the gate:** +15.6% compounded across 29
  rolling windows against +158.7% for holding GLD. That is final.
- **Multi-asset research (ADR-011) is registered:** six ETFs (GLD, SLV, SPY,
  EFA, IEF, DBC), dividend-adjusted prices from spring 2006, and 6 candidates
  (`mom`, `trend`, `both` × `eq`, `iv35`). Rebalancing is monthly. The gate
  is the strictest option: beat **both** an equal-weight buy-and-hold of the
  six **and** SPY on return, with no larger drawdown (R1 to R5, then F1 to F4).
- **Multi-asset research (ADR-011) failed the gate** (MA5, experiment
  `5fa2a52477b95cf4`): +11.0% compounded across 27 rolling windows against
  +144.9% for B1 and +435.2% for SPY; R1, R2 and R4 failed. It held cash in
  16 of 27 windows and trailed both benchmarks when invested. That is final:
  the six candidates and thresholds are not tuned and re-run.

- **Phase 7 machine learning (ADR-012) failed the gate** (experiment
  `50421538d55eb807`): +24.2% compounded across 27 rolling windows against
  +144.9% for B1 and +435.2% for SPY; R1, R2 and R4 failed. It held cash in
  16 windows, trailed both benchmarks when invested, and lost money in the
  final validation period, so the holdout held cash. That is final.
- **All 22 registered candidates (ADR-010, 011, 012) have failed.** The
  `ml` command needs the `ml` extra; run it with `.venv\Scripts\python.exe`
  (the system Python has no pip).

## Next: the owner decides

No strategy has earned paper trading, let alone live money. Any new idea
needs its own registration ADR before code, and counts as more trials
against the same history. Start nothing new without the owner's decision.
