# Configuration Specification

## 1. Overview

All runtime behavior is controlled by a single YAML configuration file, validated at startup by a `pydantic` schema. If the config is invalid, the engine refuses to start and prints the validation errors. There is no partial initialization.

**Default config location:** `config/default.yaml`  
**Override at runtime:** `--config path/to/config.yaml`  
**Environment variable overrides:** Any key can be overridden via environment variable using the pattern `ATM_<SECTION>__<KEY>` (double underscore for nesting). Example: `ATM_RISK__MAX_DRAWDOWN_PCT=0.10`.

---

## 2. Full Configuration Schema

```yaml
# ─────────────────────────────────────────────────────────
#  algorithm-machine | default.yaml
#  All values shown are defaults unless marked REQUIRED.
# ─────────────────────────────────────────────────────────

# ── Engine meta ───────────────────────────────────────────
engine:
  mode: "backtest"              # backtest | paper
  run_id: null                  # Auto-generated UUID if null
  log_level: "INFO"             # DEBUG | INFO | WARNING | ERROR
  log_dir: "logs"
  output_dir: "results"
  seed: 42                      # RNG seed for reproducible slippage simulation

# ── Universe ──────────────────────────────────────────────
universe:
  symbols:                      # REQUIRED — at least one symbol
    - "SPY"
  benchmark: "SPY"              # Symbol used for relative performance metrics; null to disable

# ── Data pipeline ─────────────────────────────────────────
data:
  source: "yfinance"            # Registered fetcher class alias
  adjusted: true                # Use split/dividend-adjusted prices
  bar_type: "daily"             # daily | weekly | monthly
  bar_close_utc_hour: 21        # UTC hour assigned as daily bar close timestamp
  cache_dir: ".cache/bars"
  cache_staleness_hours: 4      # Hours before cache file is considered stale
  batch_size: 50                # Max symbols per yfinance batch request

# ── Backtest ──────────────────────────────────────────────
# Only used when engine.mode = "backtest"
backtest:
  start_date: "2020-01-01"      # REQUIRED for backtest mode (ISO-8601)
  end_date: "2024-12-31"        # REQUIRED for backtest mode (ISO-8601)
  initial_cash: 100000.00       # Starting cash in USD
  risk_free_rate: 0.05          # Annual risk-free rate for Sharpe calculation
  audit_lookahead: false        # Enable look-ahead bias detection (slow — use in CI only)

# ── Risk engine ───────────────────────────────────────────
risk:
  # Rules are evaluated in the order listed.
  # Each rule entry must have a "rule" key matching a registered rule class alias.
  rules:
    - rule: "MaxDrawdownRule"
      max_drawdown_pct: 0.15    # Halt new buys when drawdown exceeds 15%

    - rule: "DailyOrderLimitRule"
      max_orders_per_day: 10    # Safety valve against strategy bugs

    - rule: "ShortSellingRule"  # Rejects SELL intents that exceed current position quantity

    - rule: "PositionSizeRule"
      max_position_pct: 0.20    # No single position > 20% of equity

    - rule: "CashSolvencyRule"  # Resize BUY orders to fit available cash

    - rule: "ConcentrationRule"
      max_open_positions: 10    # Maximum number of concurrent open positions

# ── Execution engine ──────────────────────────────────────
execution:
  broker: "PaperBroker"         # Registered broker class alias

  fill_at: "next_open"          # next_open | prev_close (prev_close is optimistic — avoid)

  slippage_model: "fixed_pct"   # fixed_pct | zero | volume_based (volume_based not in MVP)
  slippage_pct: 0.0005          # 0.05% per side (used by fixed_pct model)

  commission_model: "per_share" # per_share | flat | zero
  commission_per_share: 0.005   # $0.005 per share (used by per_share model)
  min_commission: 1.00          # Minimum commission per order in USD

# ── Strategies ────────────────────────────────────────────
# List of strategy instances to run. Multiple strategies may run concurrently.
strategies:
  - id: "sma_crossover_v1"                        # REQUIRED — unique within this config
    class: "strategies.sma_crossover.SmaCrossoverStrategy"  # REQUIRED — importable dotted path
    symbols: ["SPY"]                              # Symbols this strategy instance trades
    params:
      fast_period: 10                             # Fast EMA lookback (bars)
      slow_period: 30                             # Slow SMA lookback (bars)
      quantity: 100                               # Fixed share quantity per signal
```

---

## 3. Field Reference

### 3.1 `engine`

| Key | Type | Default | Description |
|---|---|---|---|
| `mode` | `str` | `"backtest"` | Operating mode. `"paper"` runs continuously against live data. |
| `run_id` | `str \| null` | `null` | Unique identifier for this run. Auto-generated UUID if null. Used for log file naming. |
| `log_level` | `str` | `"INFO"` | Python logging level. Set to `"DEBUG"` for verbose event tracing. |
| `log_dir` | `str` | `"logs"` | Directory for log files, relative to project root. |
| `output_dir` | `str` | `"results"` | Directory for backtest result files. |
| `seed` | `int` | `42` | Random seed for slippage model. Ensures reproducible results across runs. |

### 3.2 `universe`

| Key | Type | Required | Description |
|---|---|---|---|
| `symbols` | `list[str]` | Yes | Ticker symbols to monitor. Must be valid yfinance identifiers (uppercase). |
| `benchmark` | `str \| null` | No | Benchmark symbol for relative performance metrics. Must be in `symbols` or separately fetchable. |

### 3.3 `data`

| Key | Type | Default | Description |
|---|---|---|---|
| `source` | `str` | `"yfinance"` | Data fetcher alias. Must be registered in `engine/data/fetchers/`. |
| `adjusted` | `bool` | `true` | Use split- and dividend-adjusted prices. Strongly recommended. |
| `bar_type` | `str` | `"daily"` | Bar frequency. Only `"daily"` is supported in the MVP. |
| `bar_close_utc_hour` | `int` | `21` | UTC hour assigned to daily bar timestamps. 21:00 UTC ≈ 4:00 PM New York close. |
| `cache_dir` | `str` | `".cache/bars"` | On-disk bar cache directory. |
| `cache_staleness_hours` | `int` | `4` | Cache TTL in hours. Cache files older than this are re-fetched. |
| `batch_size` | `int` | `50` | Max symbols per yfinance request. Reduce if hitting rate limits. |

### 3.4 `backtest`

| Key | Type | Required | Description |
|---|---|---|---|
| `start_date` | `str (ISO-8601)` | In backtest mode | Inclusive start date for historical replay. |
| `end_date` | `str (ISO-8601)` | In backtest mode | Inclusive end date for historical replay. |
| `initial_cash` | `float` | No (default `100000`) | Starting cash in USD. |
| `risk_free_rate` | `float` | No (default `0.05`) | Annual risk-free rate used for Sharpe and Sortino calculation. |
| `audit_lookahead` | `bool` | No (default `false`) | Enable look-ahead bias detection. Slows runs by ~2–3×. Use in CI. |

### 3.5 `risk.rules`

Each entry in the list must have a `rule` key that matches a registered rule class alias. Additional keys are rule-specific parameters.

| Rule alias | Parameters | Description |
|---|---|---|
| `MaxDrawdownRule` | `max_drawdown_pct: float` | Rejects BUY intents when portfolio drawdown exceeds threshold. |
| `DailyOrderLimitRule` | `max_orders_per_day: int` | Rejects intents once daily approval count is reached. |
| `ShortSellingRule` | _(none)_ | Rejects SELL intents where quantity > current position size. |
| `PositionSizeRule` | `max_position_pct: float` | Resizes or rejects intents that would exceed per-symbol position cap. |
| `CashSolvencyRule` | _(none)_ | Resizes BUY intents to fit available cash. |
| `ConcentrationRule` | `max_open_positions: int` | Rejects new BUY intents when open position count is at maximum. |

### 3.6 `execution`

| Key | Type | Default | Description |
|---|---|---|---|
| `broker` | `str` | `"PaperBroker"` | Broker class alias. Must implement `AbstractBroker`. |
| `fill_at` | `str` | `"next_open"` | Fill timing. `"next_open"` is realistic; `"prev_close"` is optimistic and should only be used for comparison. |
| `slippage_model` | `str` | `"fixed_pct"` | Slippage model alias. |
| `slippage_pct` | `float` | `0.0005` | Slippage fraction per side (used by `fixed_pct` model). |
| `commission_model` | `str` | `"per_share"` | Commission model alias. |
| `commission_per_share` | `float` | `0.005` | Per-share commission in USD. |
| `min_commission` | `float` | `1.00` | Minimum commission per order in USD. |

### 3.7 `strategies`

| Key | Type | Required | Description |
|---|---|---|---|
| `id` | `str` | Yes | Unique strategy identifier for this run. Used in logs and event traces. |
| `class` | `str` | Yes | Importable Python class path (`module.submodule.ClassName`). |
| `symbols` | `list[str]` | Yes | Subset of `universe.symbols` this strategy operates on. |
| `params` | `dict` | No | Strategy-specific parameters. Passed as `StrategyConfig.params`. |

---

## 4. Validation Rules (Pydantic)

At startup, the config is validated and the engine exits with a clear error if any of the following fail:

1. `universe.symbols` is non-empty.
2. `backtest.start_date < backtest.end_date` (backtest mode only).
3. `backtest.initial_cash > 0`.
4. All `strategies[*].symbols` are a subset of `universe.symbols`.
5. All `strategies[*].id` values are unique.
6. All `strategies[*].class` values are importable (checked at startup, not schema validation time).
7. `risk.rules` is non-empty.
8. All rule `rule:` keys resolve to a registered `AbstractRule` subclass.
9. `execution.slippage_pct >= 0` and `execution.commission_per_share >= 0`.
10. `risk.rules[PositionSizeRule].max_position_pct` in `(0.0, 1.0]`.

---

## 5. Environment Variable Overrides

Any config value can be overridden via environment variable. Nesting uses double underscores. Examples:

```bash
# Override initial cash
export ATM_BACKTEST__INITIAL_CASH=500000

# Override max drawdown rule parameter
export ATM_RISK__RULES__0__MAX_DRAWDOWN_PCT=0.10

# Switch to paper mode
export ATM_ENGINE__MODE=paper

# Set log level to DEBUG
export ATM_ENGINE__LOG_LEVEL=DEBUG
```

Array index overrides (`RULES__0__`) are supported for list-type config sections.

---

## 6. `.env.example`

```dotenv
# Copy to .env and populate. Never commit .env to version control.

# Optional: override config file path
# ATM_CONFIG_PATH=config/custom.yaml

# Optional: override specific values
# ATM_ENGINE__LOG_LEVEL=DEBUG
# ATM_BACKTEST__INITIAL_CASH=50000
```
