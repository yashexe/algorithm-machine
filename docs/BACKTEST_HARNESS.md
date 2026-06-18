# Backtest Harness Specification

## 1. Purpose

The backtest harness replays historical market data through the live engine pipeline in chronological order, producing a performance report. Because the engine is event-driven, the backtest runner's job is simple: feed `BarEvent` objects into the event bus in time order, then let the existing components (strategy, risk engine, broker, portfolio state) do their jobs.

There is **no "backtest mode"** in any component. The strategy doesn't know whether it's running against historical data or live data. Neither does the risk engine. This is the primary benefit of the event-driven architecture.

---

## 2. BacktestRunner

### 2.1 Inputs

```
BacktestRunner.run(
    config      : BacktestConfig,
    strategy    : AbstractStrategy,
    start_date  : date,
    end_date    : date,
    save_artifacts : bool = True,
)
```

```
BacktestConfig
  initial_cash   : Decimal
  universe       : list[str]
  data_config    : DataConfig
  risk_config    : RiskConfig
  execution_config: ExecutionConfig
```

### 2.2 Initialization Sequence

```
1. Instantiate EventBus
2. Instantiate PortfolioState(initial_cash=config.initial_cash)
3. Instantiate RiskGatekeeper(rules=config.risk_config.rules)
4. Instantiate PaperBroker(config.execution_config)
5. Instantiate strategy(strategy_id, strategy_config, bus)
6. Register event handlers in canonical order (see EVENT_SYSTEM.md §3.3)
7. Fetch all historical bars for universe × [start_date, end_date]
8. Sort bars: primary key = timestamp, secondary key = symbol (deterministic ordering)
9. Call strategy.on_start(universe)
10. Enter replay loop
11. Call strategy.on_stop()
12. Compute performance metrics from PortfolioSnapshotEvent history
13. Return BacktestResult
```

### 2.3 Replay Loop

```
for bar_date in trading_days(start_date, end_date):
    bars_for_date = [b for b in all_bars if b.timestamp.date() == bar_date]
    # Sort by symbol within the same date for deterministic ordering
    for bar in sorted(bars_for_date, key=lambda b: b.symbol):
        bus.publish(bar)
    # After all symbols for this date have been published,
    # flush pending broker fills for next-day open — handled automatically
    # because PaperBroker fills on next BarEvent, not end-of-day.
```

**Important:** All symbols for a given date are published before moving to the next date. This means a strategy that receives `AAPL`'s Monday bar will also see `MSFT`'s Monday bar before any Tuesday bars arrive. Within a date, symbol order is deterministic (alphabetical) to ensure reproducible runs.

---

## 3. Look-Ahead Bias Audit

The replay loop has a built-in look-ahead detection mode (enabled with `--audit-lookahead` flag):

- Every `BarEvent` is stamped with a `replay_sequence_number`.
- If any handler's execution reads a bar with a sequence number greater than the current bar's, the run aborts with an error.
- This is implemented via a `LookAheadDetector` subscriber that wraps strategy handlers in inspection proxies during audit runs.

Audit mode is slow (2–3× runtime due to tracing overhead) and not used in production runs.

---

## 4. Trading Day Calendar

The backtest uses a simplified U.S. equity trading calendar:

- Weekend days are excluded.
- Federal holidays (New Year's, MLK Day, Presidents' Day, Good Friday, Memorial Day, Independence Day, Labor Day, Thanksgiving, Christmas) are excluded.
- Half-days are not modeled (the full bar is used).

The calendar is implemented as a static list for the MVP. For production use, the `trading_calendars` library (based on `pandas_market_calendars`) would be a direct drop-in.

---

## 5. Performance Metrics

The `MetricsEngine` computes performance statistics from the series of `PortfolioSnapshotEvent` objects published during the run.

### 5.1 Core Metrics

| Metric | Description | Formula |
|---|---|---|
| **Total Return** | Net percentage gain/loss | `(final_equity - initial_cash) / initial_cash` |
| **Annualized Return** | Compound annual growth rate | `(1 + total_return)^(252/trading_days) - 1` |
| **Volatility** | Annualized std dev of daily returns | `std(daily_returns) × √252` |
| **Sharpe Ratio** | Risk-adjusted return | `(annualized_return - risk_free_rate) / volatility` |
| **Sortino Ratio** | Downside risk-adjusted return | `(annualized_return - risk_free_rate) / downside_std × √252` |
| **Max Drawdown** | Largest peak-to-trough equity drop | `max((peak - trough) / peak)` over all periods |
| **Max Drawdown Duration** | Longest time underwater (days) | Length of longest continuous drawdown period |
| **Calmar Ratio** | Return per unit of max drawdown | `annualized_return / max_drawdown` |
| **Win Rate** | Fraction of trades with positive P&L | `profitable_trades / total_closed_trades` |
| **Profit Factor** | Gross profit / gross loss | `sum(winning_trades) / abs(sum(losing_trades))` |
| **Average Trade P&L** | Mean realized P&L per trade | `total_realized_pnl / num_trades` |
| **Average Win / Average Loss** | Trade size asymmetry | Separate means for winning and losing trades |
| **Number of Trades** | Total completed round-trips | Count of SELL fills that closed a position |
| **Avg Days Held** | Average holding period | Mean days between BUY fill and SELL fill per position |
| **Exposure Pct** | Fraction of time in market | `days_with_open_position / total_trading_days` |

### 5.2 Benchmark Comparison

If a benchmark symbol is configured (default: `"SPY"`), the following relative metrics are also computed:

| Metric | Description |
|---|---|
| **Alpha** | Excess return vs. benchmark (CAPM alpha) |
| **Beta** | Sensitivity to benchmark moves |
| **Correlation** | Pearson correlation of daily returns with benchmark |
| **Information Ratio** | `(strategy_return - benchmark_return) / tracking_error` |

### 5.3 Daily Returns Computation

```
daily_equity_series = [snapshot.equity for snapshot in sorted(snapshots, key=lambda s: s.timestamp)]
daily_returns = [(equity[t] - equity[t-1]) / equity[t-1] for t in range(1, len(equity))]
```

### 5.4 Drawdown Series

The drawdown series is computed from the equity series:

```
running_peak = initial_cash
for each equity_value in daily_equity_series:
    running_peak = max(running_peak, equity_value)
    drawdown[t] = (running_peak - equity_value) / running_peak
max_drawdown = max(drawdown)
```

---

## 6. BacktestResult

The `BacktestResult` object returned by `BacktestRunner.run()` contains:

```
BacktestResult
  run_id          : str
  strategy_id     : str
  start_date      : date
  end_date        : date
  initial_cash    : Decimal

  # Summary metrics
  total_return_pct         : float
  annualized_return_pct    : float
  volatility_annualized    : float
  sharpe_ratio             : float
  sortino_ratio            : float
  calmar_ratio             : float
  max_drawdown_pct         : float
  max_drawdown_duration_days: int
  win_rate                 : float
  profit_factor            : float
  num_trades               : int
  avg_days_held            : float
  exposure_pct             : float

  # Benchmark relative (if configured)
  alpha         : float | None
  beta          : float | None
  correlation   : float | None

  # Time series (for charting)
  equity_curve     : pd.Series        # DatetimeIndex → equity value
  drawdown_curve   : pd.Series        # DatetimeIndex → drawdown pct
  daily_returns    : pd.Series        # DatetimeIndex → daily return

  # Trade log
  trade_log        : pd.DataFrame     # One row per completed round-trip
```

---

## 7. Output and Reporting

### 7.1 Console Output

At run completion, the backtest runner prints a formatted summary table:

```
=== Backtest Results: sma_crossover_v1 ===
Period:          2020-01-01 → 2024-12-31 (1258 trading days)
Initial Capital: $100,000.00
Final Equity:    $134,521.44

── Returns ──────────────────────────────
Total Return:        +34.52%
Annualized Return:   +6.08%
Benchmark (SPY):     +91.42% (+14.18% ann.)

── Risk ─────────────────────────────────
Volatility (ann.):   12.43%
Max Drawdown:        -18.76%  (recovered in 142 days)
Sharpe Ratio:        0.49
Sortino Ratio:       0.71

── Trades ───────────────────────────────
Total Trades:        47
Win Rate:            57.4%
Profit Factor:       1.31
Avg Holding Period:  21.3 days
Time in Market:      62.1%
```

### 7.2 File Output

```
{config.output_dir}/{run_id}/
  summary.json        # BacktestResult as JSON
  equity_curve.csv    # DatetimeIndex → equity
  drawdown_curve.csv  # DatetimeIndex → drawdown
  trade_log.csv       # Full trade log
  events.ndjson       # Full event log (symlink to log dir)
  risk_log.ndjson     # Risk decisions log
```

### 7.3 Plotting

A `plot_results(result: BacktestResult)` utility generates a 4-panel matplotlib figure:
1. Equity curve vs. benchmark (log scale)
2. Drawdown series
3. Monthly returns heatmap
4. Rolling Sharpe ratio (12-month window)

This requires `matplotlib` as an optional dependency.

---

## 8. Walk-Forward Validation

Walk-forward optimization is implemented in `engine/backtest/walk_forward.py` and invoked via `run_walk_forward.py`.

### 8.1 Purpose

A full-period backtest (e.g., 2015–2024) is vulnerable to curve-fitting: the
"best" parameters may have been discovered by evaluating them on the same data
used to select them. Walk-forward optimization addresses this by:

1. **Partitioning** the date range into rolling IS (in-sample) and OOS (out-of-sample) windows.
2. **Optimizing** strategy parameters on the IS window only (grid search over a configured parameter space).
3. **Locking** those parameters and running a completely blind OOS evaluation.
4. **Stitching** all OOS equity curves into a continuous, realistic performance record.

If the strategy beats the benchmark OOS across multiple independent folds, the
edge is statistically proven — not a hindsight artefact.

### 8.2 WalkForwardValidator

```
WalkForwardValidator(config: AppConfig)
  .run(
      param_grid      : dict[str, list[Any]],   # e.g. {"vol_target_pct": [0.001, 0.003], ...}
      is_years        : int = 3,                # in-sample window
      oos_years       : int = 1,                # out-of-sample window
      optimize_metric : str = "sharpe_ratio",   # BacktestResult attribute to maximize
      output_dir      : Path | None = None,
      silent_folds    : bool = True,
  ) → WalkForwardResult
```

**Fold generation (rolling windows):**

```
Full period: 2015-01-01 → 2024-12-31   IS=3yr  OOS=1yr

Fold 1: IS 2015–2017  OOS 2018
Fold 2: IS 2016–2018  OOS 2019
Fold 3: IS 2017–2019  OOS 2020
Fold 4: IS 2018–2020  OOS 2021
Fold 5: IS 2019–2021  OOS 2022
Fold 6: IS 2020–2022  OOS 2023
Fold 7: IS 2021–2023  OOS 2024
```

Each fold advances by `oos_years`. The total number of folds is
`(total_years - is_years) / oos_years`.

### 8.3 IS Optimization

For each IS window, all Cartesian product combinations of `param_grid` are
evaluated via `BacktestRunner`. The combination with the highest value of
`optimize_metric` (default: Sharpe ratio) is selected.

**Default parameter grid (36 combinations per fold):**

```python
param_grid = {
    "vol_target_pct": [0.001, 0.002, 0.003, 0.005],   # 4
    "top_n":          [10, 15, 20],                     # 3
    "ma_period":      [150, 200, 250],                  # 3
}
```

### 8.4 WalkForwardResult

```
WalkForwardResult
  run_id                  : str
  strategy_id             : str
  folds                   : list[WalkForwardFold]
  aggregate_oos_ann_return : float
  aggregate_oos_sharpe     : float
  aggregate_oos_max_dd     : float
  best_params_by_fold      : list[dict]     # tracks parameter stability
  oos_equity_curve         : pd.Series      # stitched continuous OOS curve
  benchmark_equity         : pd.Series | None

WalkForwardFold
  fold_index   : int
  is_start / is_end / oos_start / oos_end : date
  best_params  : dict[str, Any]
  best_is_sharpe : float
  oos_result   : BacktestResult            # full OOS result object
```

### 8.5 CLI Usage

```bash
# Default: 3-year IS, 1-year OOS, default 36-combo grid
.venv/bin/python run_walk_forward.py --config config/equity_momentum.yaml

# Custom windows
.venv/bin/python run_walk_forward.py --is-years 2 --oos-years 1

# Custom grid (quick smoke test — 2×2×2 = 8 combos per fold)
.venv/bin/python run_walk_forward.py \
    --vol-targets 0.002,0.003 \
    --top-ns 15,20 \
    --ma-periods 150,200

# With matplotlib chart
.venv/bin/python run_walk_forward.py --plot

# Increase warmup if strategy has a longer lookback (default 300 calendar days)
.venv/bin/python run_walk_forward.py --warmup-bars 400
```

> [!NOTE]
> `--warmup-bars` controls how far before each OOS start the data fetch begins,
> giving the strategy enough bar history to warm up. The burn-in period is trimmed
> from the equity curve before stitching, so it never inflates OOS metrics.
> Set it to at least `momentum_lookback + skip_recent_days + 60` (≥ 332 days for defaults).


### 8.6 Output Files

```
results/wfo_{run_id}/
  wfo_summary.json          # aggregate stats + per-fold metadata
  oos_equity_curve.csv      # stitched OOS equity series
  wfo_results.png           # 3-panel chart (if --plot used)
  fold_1/
    is/combo_0/             # IS grid search results per combination
    oos/                    # OOS BacktestResult files
  fold_2/ ...
```

Walk-forward internal runner calls disable root-level `BacktestResult` artifacts
for warmup-inclusive IS/OOS runs. The WFO directory stores the trimmed, scored
results that should be inspected.

### 8.7 Interpreting Results

| Signal | Interpretation |
|---|---|
| OOS Sharpe > 0 across all folds | Edge exists and is not fold-specific |
| IS Sharpe >> OOS Sharpe | Overfitting present — widen OOS window or reduce grid |
| Parameter stability across folds | Robust signal; unstable params suggest fragility |
| OOS equity curve beats SPY | Strategy adds alpha out-of-sample |

---

## 9. Configuration Reference

```yaml
backtest:
  start_date: "2020-01-01"
  end_date:   "2024-12-31"
  initial_cash: 100000.00
  risk_free_rate: 0.05         # Annual rate for Sharpe calculation
  benchmark: "SPY"             # null to disable benchmark comparison
  output_dir: "results"
  audit_lookahead: false        # Enable look-ahead detection (slow)
```
