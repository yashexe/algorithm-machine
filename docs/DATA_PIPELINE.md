# Data Pipeline Specification

## 1. Responsibilities

The data pipeline has exactly three jobs:

1. **Fetch** raw OHLCV data from an external source for a configured universe of symbols.
2. **Normalize** that data into the system's canonical `BarEvent` schema.
3. **Cache** bars on disk so repeated backtests do not re-hit the network.

The pipeline publishes `BarEvent` objects onto the event bus. Nothing downstream cares how the data was fetched or from where.

---

## 2. Data Sources

### 2.1 Primary Source: yfinance

`yfinance` is the default and only source in the MVP. It provides free EOD (end-of-day) OHLCV data adjusted for splits and dividends.

**Capabilities used:**
- `yf.download(tickers, start, end, interval="1d", auto_adjust=True)`
- Multi-ticker batch download in a single call
- Adjusted close (handles corporate actions transparently)

**Known limitations:**
- Survivorship bias: delisted symbols return no data.
- Data quality: occasional gaps, stale volume on thinly traded names.
- Rate limiting: no published rate limit, but bulk downloads for >50 symbols should be batched.

### 2.2 Fetcher Interface

All data sources implement `AbstractFetcher`:

```
AbstractFetcher
  .fetch(symbols: list[str], start: date, end: date) -> RawDataFrame
```

`RawDataFrame` is a `pd.DataFrame` with a `DatetimeIndex` and columns `[open, high, low, close, volume]` per symbol. The fetcher is responsible for returning adjusted prices. It is not responsible for validation or normalization — those are the normalizer's jobs.

### 2.3 Adding a New Source

Create a new module under `engine/data/fetchers/`, implement `AbstractFetcher`, and register the class name in `config.data.source`. The rest of the pipeline is unaffected.

---

## 3. Bar Event Schema

The `BarEvent` is the atomic unit of market data in the system. Every downstream component — strategies, portfolio state, broker — operates on `BarEvent` objects.

```
BarEvent
  timestamp   : datetime   # UTC bar close time (daily: YYYY-MM-DD 21:00:00 UTC)
  symbol      : str         # Ticker (uppercase, e.g. "AAPL")
  open        : Decimal     # Adjusted open price
  high        : Decimal     # Adjusted high price
  low         : Decimal     # Adjusted low price
  close       : Decimal     # Adjusted close price
  volume      : int         # Share volume
  bar_type    : BarType     # Enum: DAILY | WEEKLY | MONTHLY
  source      : str         # Fetcher identifier (e.g. "yfinance")
  is_complete : bool        # False if bar is in-progress (always True for EOD)
```

**Why `Decimal` for prices?** Floating-point arithmetic accumulates errors in P&L calculations. All price values are stored and computed as `decimal.Decimal` with a precision of 6 significant figures (configurable). Conversion from float happens once, at the normalizer boundary, never inside the engine.

---

## 4. Normalization

The `Normalizer` is a stateless transformer. It receives a `RawDataFrame` and produces a list of `BarEvent` objects.

### 4.1 Normalization Steps

1. **Timestamp standardization** — Convert index to UTC `datetime`. Daily bars are assigned a timestamp of `YYYY-MM-DD 21:00:00 UTC` (market close approximation). This value is configurable.

2. **Column aliasing** — Map source-specific column names to canonical lowercase names (`Open` → `open`, etc.).

3. **Float → Decimal conversion** — All price columns are converted via `Decimal(str(value))` to avoid float representation errors.

4. **Volume coercion** — Cast volume to `int`. Handle NaN volume as `0`.

5. **Symbol injection** — Attach the `symbol` field from context (the fetcher knows which ticker each column belongs to).

6. **Completeness flag** — Set `is_complete = True` for all historical bars. Live-mode intraday bars would set this `False` until bar close.

### 4.2 Validation Rules

The normalizer validates each bar and drops (with a warning log) any bar that fails:

| Rule | Condition |
|---|---|
| No null prices | `open`, `high`, `low`, `close` must all be non-null |
| Price positivity | All price fields > 0 |
| OHLC coherence | `low ≤ open ≤ high` and `low ≤ close ≤ high` |
| Non-zero volume | `volume ≥ 0` (zero is a warning, not a drop) |
| No future timestamps | `bar.timestamp ≤ now()` in live mode |
| No duplicate bars | (symbol, timestamp) must be unique in the output set |

Validation failures are logged at `WARNING` level with the raw value included. They are never silently swallowed.

---

## 5. Universe Management

The **universe** is the set of symbols the engine monitors. It is declared in configuration and does not change at runtime (the MVP does not support dynamic universe expansion).

```yaml
universe:
  symbols: ["SPY", "QQQ", "AAPL", "MSFT", "GOOGL"]
  benchmark: "SPY"
```

The `DataFetcher` fetches all universe symbols in a single batch request. If a symbol returns no data for a given date range, a warning is logged and that symbol is excluded from that bar cycle. The engine continues for the remaining symbols.

---

## 6. Bar Cache

Network calls are expensive and yfinance has soft rate limits. The cache stores bars locally so that re-running a backtest over the same date range is instant.

### 6.1 Cache Format

- Storage: Parquet files, one file per (symbol, bar_type) tuple.
- Path: `{config.cache_dir}/{symbol}/{bar_type}.parquet`
- Schema: matches `BarEvent` fields as columns, `timestamp` as the index.

### 6.2 Cache Invalidation Policy

The cache uses a **staleness window** strategy:

- A cache file is **valid** if its most recent bar's timestamp is within `config.data.cache_staleness_hours` of `now()`.
- A cache file is **stale** if the most recent bar is older than the staleness window.
- A cache miss or stale file triggers a full re-fetch for the requested date range.
- Cache writes are atomic: write to a temp file, then `os.rename()` to the target path.

### 6.3 Backtest vs. Live Mode

| Mode | Cache behavior |
|---|---|
| Backtest | Cache hit: read from disk, skip network. Cache miss: fetch and write. |
| Paper live | Always fetch the latest bar; write to cache after fetch. |

---

## 7. Data Flow Diagram

```
                     ┌──────────────┐
                     │  config.yaml │
                     │  universe    │
                     └──────┬───────┘
                            │
                     ┌──────▼───────┐         ┌───────────┐
                     │ DataFetcher  │◄────────►│ Bar Cache │
                     │ (yfinance)   │         │ (Parquet) │
                     └──────┬───────┘         └───────────┘
                            │ RawDataFrame
                     ┌──────▼───────┐
                     │  Normalizer  │
                     │  validate +  │
                     │  schema map  │
                     └──────┬───────┘
                            │ List[BarEvent]
                     ┌──────▼───────┐
                     │  Event Bus   │
                     │ publish each │
                     │  BarEvent    │
                     └──────────────┘
```

---

## 8. Configuration Reference

See [CONFIGURATION.md](CONFIGURATION.md) for the full schema. Data-pipeline-specific keys:

```yaml
data:
  source: "yfinance"               # Fetcher class to use
  cache_dir: ".cache/bars"         # Relative to project root
  cache_staleness_hours: 4         # Hours before a cache file is considered stale
  batch_size: 50                   # Max symbols per yfinance download call
  adjusted: true                   # Use split/dividend-adjusted prices
  bar_type: "daily"                # daily | weekly | monthly
  bar_close_utc_hour: 21           # UTC hour assigned as daily bar timestamp
```
