# Strategy Interface Specification

## 1. Role of the Strategy Layer

A strategy is a pure signal generator. It observes market data and, when its logic is triggered, proposes a trade by emitting an `OrderIntentEvent`. It has no authority to execute anything.

**What a strategy CAN do:**
- Maintain internal state (rolling indicators, position flags, etc.)
- Read `BarEvent` data passed to its handlers
- Publish `OrderIntentEvent` objects to the event bus

**What a strategy CANNOT do:**
- Access `PortfolioState` directly
- Access `RiskGatekeeper` or call any approval method
- Call `PaperBroker` or any execution interface
- Subscribe to any event type other than `BarEvent` (and optionally `PortfolioSnapshotEvent` in read-only mode for informational purposes)
- Modify any shared mutable state

This isolation is enforced at construction time: the engine's `StrategyRunner` injects only an `EventBus` handle and a `StrategyConfig` object when instantiating each strategy. No other dependency is provided.

---

## 2. AbstractStrategy Protocol

```
AbstractStrategy
  strategy_id    : str                  # Unique name, set in config (e.g., "sma_crossover_v1")
  config         : StrategyConfig       # Strategy-specific config section
  _bus           : EventBus             # Injected at construction, private

  # Lifecycle hooks
  .on_start(universe: list[str]) -> None
      Called once before the first bar. Use for one-time initialization
      (e.g., pre-allocate indicator buffers, log startup info).

  .on_bar(event: BarEvent) -> None
      Called for every BarEvent on the bus. This is where signal logic lives.
      Subclasses must implement this method.

  .on_stop() -> None
      Called once after the last bar. Use for cleanup or final logging.

  # Intent emission (protected, called from within on_bar)
  ._emit_intent(
      symbol    : str,
      side      : Side,
      intent_type : IntentType,
      quantity  : Decimal,
      limit_price : Decimal | None = None,
      notes     : str = ""
  ) -> None
      Constructs an OrderIntentEvent and publishes it to the bus.
      Strategies MUST use this method to propose trades — they must NOT
      construct OrderIntentEvent directly to prevent bypassing validation.
```

---

## 3. Order Intent Schema

An `OrderIntentEvent` is a formal proposal. Its fields are described in detail in [EVENT_SYSTEM.md](EVENT_SYSTEM.md). Key design points:

### 3.1 Quantity is a Proposal, Not a Guarantee

The `quantity` field in an intent represents the strategy's desired exposure. The risk engine may reduce this quantity (resize) or reject the intent entirely. The strategy should not assume its stated quantity will be filled.

### 3.2 Quantity Must Be Positive

Strategies always express quantity as a positive integer (shares). Direction is conveyed by `side: Side.BUY` or `side: Side.SELL`. The risk engine and broker both enforce that `quantity > 0`.

### 3.3 Sizing Responsibility

Strategies may optionally compute their own desired position size. However, since strategies have no direct access to portfolio state, they must express sizing as either:

a) **Fixed quantity** — a hardcoded share count (simplest, for reference strategies)
b) **Notional target** — a target dollar value, declared via a `target_notional` field in the intent (the risk engine will convert this to shares using the current market price)
c) **Weight target** — a target portfolio weight (0.0–1.0), declared via `target_weight` (the risk engine converts to shares using current equity)

The MVP reference strategy uses fixed quantity. The risk engine will reduce it if it violates sizing rules.

---

## 4. Strategy State Management

### 4.1 Internal State

Each strategy instance maintains its own internal state. The canonical pattern is a rolling buffer per symbol, sized to the longest lookback window required:

```
_price_history: dict[str, deque[Decimal]]   # keyed by symbol
_in_position:   dict[str, bool]             # are we currently long/short this symbol?
```

### 4.2 No Cross-Strategy State Sharing

Strategies are isolated from each other. If two strategies trade the same symbol, the risk engine is responsible for managing combined exposure — the strategies do not coordinate.

### 4.3 Warm-Up Period

Many indicators require a minimum number of bars before they produce a valid signal (e.g., a 50-day SMA needs 50 bars of history). Strategies must track their own warm-up state and suppress signal emission until the warm-up period is satisfied.

The `AbstractStrategy` base class provides a helper:

```
._is_warmed_up(symbol: str, required_bars: int) -> bool
```

This returns `True` once the strategy has seen at least `required_bars` bars for the given symbol. Strategies must gate all signal logic behind this check.

---

## 5. Reference Implementation: SMA Crossover

The SMA crossover strategy is the canonical reference implementation. It proves the pipeline end-to-end and is used as the integration test strategy.

### 5.1 Logic

- Compute a fast EMA (configurable, default 10 days) and a slow SMA (configurable, default 30 days) on adjusted close.
- **BUY signal:** fast EMA crosses above slow SMA (golden cross), when not already in position.
- **SELL signal:** fast EMA crosses below slow SMA (death cross), when currently in position.
- Emits a single `OrderIntentEvent` per signal event. Does not emit on every bar.

### 5.2 Configuration

```yaml
strategies:
  - id: "sma_crossover_v1"
    class: "strategies.sma_crossover.SmaCrossoverStrategy"
    symbols: ["SPY", "QQQ"]
    params:
      fast_period: 10
      slow_period: 30
      quantity: 100          # Fixed share quantity per signal
```

### 5.3 Intent Emission Pattern

```
on_bar(event: BarEvent):
  # 1. Update internal price buffer for event.symbol
  # 2. Check warm-up (need slow_period bars minimum)
  # 3. Compute fast EMA and slow SMA
  # 4. Detect crossover
  # 5. If golden cross and not in_position[symbol]:
  #      _emit_intent(symbol, BUY, MARKET, quantity, notes="golden cross")
  #      _in_position[symbol] = True  (optimistic, before fill)
  # 6. If death cross and in_position[symbol]:
  #      _emit_intent(symbol, SELL, MARKET, quantity, notes="death cross")
  #      _in_position[symbol] = False
```

Note: setting `_in_position` optimistically (before the fill is confirmed) prevents double-signaling within the same bar cycle. The risk engine may still reject the intent, in which case the position flag is stale. A more robust implementation would update `_in_position` only after receiving a `FillEvent` — this is a known MVP simplification.

---

## 6. Multi-Strategy Execution

The engine supports multiple concurrent strategies. Each strategy runs its `on_bar` handler independently when a `BarEvent` is published. The order of strategy handler calls follows registration order (alphabetical by `strategy_id` by default, configurable).

**Conflict resolution is the risk engine's responsibility.** If two strategies both emit `BUY` intents for the same symbol in the same bar cycle, the risk engine evaluates each intent independently against the *same* portfolio snapshot. If the first is approved, it creates a position. The second intent will be evaluated against the (not-yet-updated) snapshot, since fills happen at the next bar's open — this is the correct behavior and prevents intra-bar double-counting.

---

## 7. Strategy Isolation Checklist

Before adding a new strategy, verify:

- [ ] The strategy class only inherits from `AbstractStrategy`.
- [ ] `__init__` accepts only `(strategy_id: str, config: StrategyConfig, bus: EventBus)`.
- [ ] No import of `PortfolioState`, `RiskGatekeeper`, `PaperBroker`, or `Order`.
- [ ] All trade proposals go through `._emit_intent()`, never by constructing `OrderIntentEvent` directly.
- [ ] The warm-up check is in place before any signal logic runs.
- [ ] Strategy is stateless across `on_stop()` / `on_start()` (i.e., state is re-initialized in `on_start()`).

---

## 8. Reference Implementation: ETF Mean Reversion

The ETF mean reversion strategy (`strategies/etf_mean_reversion.py`) is the
second canonical reference implementation. It proves the multi-strategy pipeline
end-to-end and demonstrates how an uncorrelated, absolute-return strategy can
run concurrently alongside the momentum strategy.

### 8.1 Logic

- Compute a rolling z-score of the log price ratio `log(price_a / price_b)` over a
  configurable lookback window (default 60 days).
- **BUY signal (leg A):** z-score < −`entry_z` — leg A (SPY) is unusually cheap relative to leg B (TLT).
- **BUY signal (leg B):** z-score > +`entry_z` — leg B (TLT) is unusually cheap relative to leg A (SPY).
- **SELL signal:** z-score reverts back within ±`exit_z` of zero — close the active leg position.

The strategy is long-only (enforced by `ShortSellingRule`). Each leg is expressed
as a BUY of the undervalued ETF; the trade is closed by selling that position
when the spread reverts.

### 8.2 Dual-Symbol Bar Accumulation

Unlike single-symbol strategies, the mean reversion strategy must wait for bars
from **both** symbols before evaluating signals. The `on_bar` handler accumulates
prices internally and only runs signal logic once both `symbol_a` and `symbol_b`
have arrived for the current bar date:

```
on_bar(event: BarEvent):
  # 1. Record bar for the arriving symbol (A or B)
  # 2. Check if both symbols have arrived for today's date
  # 3. If not, return — wait for the other symbol
  # 4. If both arrived, call _evaluate_signals()
```

This guarantees no look-ahead: both prices are from the same bar date.

### 8.3 Configuration

```yaml
strategies:
  - id: "etf_mean_reversion_v1"
    class: "strategies.etf_mean_reversion.EtfMeanReversionStrategy"
    symbols: ["SPY", "TLT"]
    params:
      symbol_a: "SPY"
      symbol_b: "TLT"
      lookback_days: 60       # rolling z-score window in trading days
      entry_z: 1.5            # enter at 1.5 standard deviations
      exit_z: 0.5             # exit when reversion reaches 0.5 sigma
      quantity_pct: 0.15      # target 15% of initial_cash per leg
      initial_cash: 100000    # must match backtest.initial_cash
      min_vol: 0.001          # skip if spread is too quiet (flat regime)
```

### 8.4 Multi-Strategy Conflict Resolution Example

When `equity_momentum_v1` and `etf_mean_reversion_v1` both want to BUY SPY:

1. `equity_momentum_v1` emits `OrderIntentEvent(symbol="SPY", side=BUY, qty=150)`.
2. `RiskGatekeeper` evaluates against the current `PortfolioSnapshot`.
   - `PositionSizeRule` caps to max 8% of equity → resizes to e.g. 120 shares.
   - `CashSolvencyRule` checks available cash → passes.
   - Emits `ApprovedOrderEvent`. SPY position is now pending.
3. `etf_mean_reversion_v1` emits `OrderIntentEvent(symbol="SPY", side=BUY, qty=80)`.
4. `RiskGatekeeper` evaluates against the **same** snapshot (fills are next-open).
   - `PositionSizeRule` sees the pending SPY order from step 2 in `open_orders`.
   - Proposed notional (existing pending + new) exceeds 8% → **REJECT**.
5. Both `RuleTrace` entries are logged to `risk/{run_id}.ndjson` with `strategy_id` tags.

Neither strategy coordinates with the other — the RiskGatekeeper enforces
capital discipline centrally, as per the invariant in `ARCHITECTURE.md §5`.

