# Event System Specification

## 1. Purpose

The event bus is the central nervous system of the engine. It decouples producers from consumers: a component publishes an event without knowing who (if anyone) is listening, and a subscriber handles events without knowing who produced them.

This design has two critical consequences:
- **Backtesting is trivially correct.** The backtest runner replays historical events through the same bus and the same handlers. No component needs a "backtest mode" switch.
- **The audit trail is complete.** Every event published to the bus is persisted, giving a full causal record of every decision the engine made.

---

## 2. Event Taxonomy

All events are immutable `dataclass(frozen=True)` instances. Once created, an event's fields cannot be modified.

### 2.1 Event Hierarchy

```
BaseEvent
  timestamp : datetime        # UTC time the event was created/occurred
  event_id  : UUID            # Globally unique identifier

  ├── MarketEvent
  │     └── BarEvent           # One OHLCV bar for one symbol
  │
  ├── SignalEvent
  │     └── OrderIntentEvent   # A strategy's trade proposal
  │
  ├── RiskEvent
  │     ├── ApprovedOrderEvent # Risk engine approved (possibly resized) the intent
  │     └── RejectedOrderEvent # Risk engine rejected the intent
  │
  ├── ExecutionEvent
  │     ├── OrderSubmittedEvent # Broker received the order
  │     └── FillEvent           # Order was filled (partial or complete)
  │
  └── PortfolioEvent
        └── PortfolioSnapshotEvent # Point-in-time portfolio state
```

### 2.2 BarEvent

Defined in full in [DATA_PIPELINE.md](DATA_PIPELINE.md). Summary:

```
BarEvent(BaseEvent)
  symbol      : str
  open        : Decimal
  high        : Decimal
  low         : Decimal
  close       : Decimal
  volume      : int
  bar_type    : BarType
  source      : str
  is_complete : bool
```

### 2.3 OrderIntentEvent

A strategy's proposal to trade. It is an expression of *desire*, not authority. It carries no guarantee of execution.

```
OrderIntentEvent(BaseEvent)
  strategy_id  : str          # Identifier of the emitting strategy
  symbol       : str
  side         : Side         # Enum: BUY | SELL
  intent_type  : IntentType   # Enum: MARKET | LIMIT
  quantity     : Decimal      # Proposed share quantity (may be resized by risk engine)
  limit_price  : Decimal | None  # Only for LIMIT intents
  signal_bar   : BarEvent     # The bar that triggered this intent (for audit)
  notes        : str          # Human-readable reason for the signal
```

### 2.4 ApprovedOrderEvent

Produced exclusively by `RiskGatekeeper`. Carries a final, binding order specification.

```
ApprovedOrderEvent(BaseEvent)
  origin_intent : OrderIntentEvent  # The intent that was approved
  approved_qty  : Decimal           # May differ from intent quantity (resizing)
  approved_side : Side
  approved_type : IntentType
  rule_trace    : list[RuleTrace]   # Which rules ran and their verdicts
  notes         : str               # Risk engine notes (e.g., "resized: position cap")
```

`ApprovedOrderEvent` is constructed only by `RiskGatekeeper._build_approval()`. No other code path produces this type.

### 2.5 RejectedOrderEvent

```
RejectedOrderEvent(BaseEvent)
  origin_intent : OrderIntentEvent
  rejection_reason : str            # Human-readable reason
  rule_trace    : list[RuleTrace]   # Which rule caused rejection
```

### 2.6 FillEvent

```
FillEvent(BaseEvent)
  order_id    : UUID
  symbol      : str
  side        : Side
  filled_qty  : Decimal
  fill_price  : Decimal           # Includes slippage model
  commission  : Decimal
  fill_bar    : BarEvent          # The bar at which the fill was simulated
```

### 2.7 PortfolioSnapshotEvent

```
PortfolioSnapshotEvent(BaseEvent)
  equity          : Decimal       # Total portfolio value
  cash            : Decimal
  positions       : list[PositionSnapshot]
  realized_pnl    : Decimal
  unrealized_pnl  : Decimal
  drawdown_pct    : Decimal       # Current drawdown from peak equity
  peak_equity     : Decimal
```

---

## 3. Event Bus Design

### 3.1 Architecture

The `EventBus` is a synchronous, in-process publish/subscribe system. It is a single shared instance within one engine run.

**Key design decisions:**
- **Synchronous dispatch.** When a producer calls `bus.publish(event)`, all registered handlers for that event type are called in registration order before `publish()` returns. There is no queue, no threading, no async. This makes the call stack deterministic and trivially debuggable.
- **Typed subscriptions.** Handlers register interest in a specific event type (or a base class). The bus uses `isinstance` checks for routing. A handler registered for `BaseEvent` receives everything.
- **No cross-handler communication.** Handlers cannot see each other's results. The only channel between components is the bus itself.

### 3.2 Bus Interface

```
EventBus
  .subscribe(event_type: type[E], handler: Callable[[E], None]) -> None
  .publish(event: BaseEvent) -> None
  .get_history(event_type: type[E]) -> list[E]   # Used by backtester and tests
  .clear_history() -> None                        # Used between backtest runs
```

### 3.3 Handler Registration Order

The engine registers handlers in this fixed order at startup:

1. `PortfolioState.on_bar` (must update MTM before strategies evaluate)
2. `Strategy.on_bar` (for each configured strategy)
3. `RiskGatekeeper.on_order_intent`
4. `PaperBroker.on_approved_order`
5. `PortfolioState.on_fill`
6. `Logger.on_any` (registered for `BaseEvent`, runs last)

**This ordering is critical.** The portfolio's mark-to-market price must be updated before the risk engine evaluates a new intent against current equity. See section 5 for the timing guarantee.

### 3.4 Error Handling in Handlers

If a handler raises an exception:
- The exception is caught by the bus.
- The event and the handler identity are logged at `ERROR` level.
- The bus **continues dispatching to subsequent handlers** for that event.
- The original exception is re-raised after all handlers complete.

This ensures the audit logger always fires even if, for example, the broker handler fails.

---

## 4. Event Persistence (Audit Log)

Every event published to the bus is appended to an append-only event log.

**Format:** newline-delimited JSON (`.ndjson`), one event per line.  
**Location:** `{config.log_dir}/events/{run_id}.ndjson`  
**Schema:** each line is the JSON serialization of the event dataclass, with an additional `event_type` field containing the class name.

The event log serves as the ground truth for:
- Post-run analysis and debugging
- Replaying a run deterministically
- Compliance audit trail

---

## 5. Look-Ahead Bias Prevention

Look-ahead bias — using future data to make a past decision — is the most common source of unrealistically good backtest results. The event system prevents it through three mechanisms:

### 5.1 Timestamp Ordering Guarantee

The backtest runner guarantees that events are published in strictly ascending timestamp order. No event with timestamp `T+1` is published before all events with timestamp `T` have been fully dispatched and handled.

### 5.2 Fill Timing Rule

When a strategy's `on_bar` handler fires for bar at time `T` and emits an `OrderIntentEvent`, the resulting fill (if approved) is simulated at the **open of bar T+1**, not at the close of bar T.

**Mechanism:** `PaperBroker` stores approved orders in a pending queue when it receives `ApprovedOrderEvent`. It processes the queue when it receives the *next* `BarEvent` for that symbol, using that bar's `open` price as the fill price (plus slippage).

```
Bar[T] close → Strategy signal → OrderIntent → RiskGate → ApprovedOrder
                                                                   ↓ queued
Bar[T+1] open → PaperBroker processes queue → Fill at open[T+1]
```

### 5.3 Strategy Data Isolation

Strategies receive `BarEvent` objects one at a time. They build their own internal state (e.g., a rolling price series). The engine does not pre-load a full historical DataFrame and pass it to the strategy — the strategy only ever "knows" bars up to and including the current simulation time.

---

## 6. Event Versioning

As the system evolves, event schemas may change. Each event includes a `schema_version: int` field (default `1`). The event log deserializer handles version upgrades via a registry of migration functions. This allows old event logs to be replayed against newer code.

---

## 7. Testing the Event System

The `EventBus` is designed to be fully testable without mocking:

- In tests, create a fresh `EventBus` instance.
- Register test handlers (or assert-handlers) inline.
- Call `bus.publish(event)`.
- Use `bus.get_history(EventType)` to assert what was published.

No patching, no mocking of the bus itself. The synchronous design means there is no async test complexity.
