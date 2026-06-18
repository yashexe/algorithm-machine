# Portfolio State Specification

## 1. Responsibilities

`PortfolioState` is the single source of truth for account state. It tracks:

- Current open positions (symbol, quantity, average cost, current market price)
- Cash balance
- Total portfolio equity (cash + sum of position market values)
- All-time peak equity (for drawdown computation)
- Realized and unrealized P&L

`PortfolioState` is a **consumer** of events. It does not generate signals or make decisions. It updates itself in response to `FillEvent` (position changes) and `BarEvent` (mark-to-market price updates).

---

## 2. Data Model

### 2.1 PortfolioState (Aggregate Root)

```
PortfolioState
  run_id          : str
  initial_cash    : Decimal
  cash            : Decimal            # Available cash
  positions       : dict[str, Position]  # Keyed by symbol
  peak_equity     : Decimal            # All-time high equity value
  realized_pnl    : Decimal            # Cumulative realized P&L
  _price_cache    : dict[str, Decimal] # Latest close price per symbol (from BarEvents)
  _fill_history   : list[FillEvent]    # Immutable record of all fills

  # Computed properties
  @property equity -> Decimal
      = cash + sum(pos.market_value for pos in positions.values())

  @property unrealized_pnl -> Decimal
      = sum(pos.unrealized_pnl for pos in positions.values())

  @property drawdown_pct -> Decimal
      = max(0, (peak_equity - equity) / peak_equity)  # 0 if at or above peak
```

### 2.2 Position

```
Position
  symbol        : str
  quantity      : Decimal         # Positive = long. MVP supports long-only.
  avg_cost      : Decimal         # Volume-weighted average cost basis
  last_price    : Decimal         # Most recent mark-to-market price

  @property market_value -> Decimal
      = quantity × last_price

  @property cost_basis -> Decimal
      = quantity × avg_cost

  @property unrealized_pnl -> Decimal
      = market_value - cost_basis

  @property unrealized_pnl_pct -> Decimal
      = unrealized_pnl / cost_basis
```

### 2.3 PositionSnapshot (Immutable, for Risk Engine)

```
PositionSnapshot
  symbol        : str
  quantity      : Decimal
  avg_cost      : Decimal
  last_price    : Decimal
  market_value  : Decimal
  unrealized_pnl: Decimal
```

`PositionSnapshot` is a frozen dataclass created by `Position.snapshot()`. The risk engine works only with `PositionSnapshot` objects, never with live `Position` instances.

---

## 3. State Transitions

### 3.1 On BarEvent

When a `BarEvent` arrives, `PortfolioState` updates mark-to-market prices:

```
on_bar(event: BarEvent):
  _price_cache[event.symbol] = event.close
  if event.symbol in positions:
    positions[event.symbol].last_price = event.close
  update_peak_equity_if_needed()
```

Peak equity is updated on every bar cycle after MTM prices are refreshed. This ensures drawdown is computed against the true equity peak, not a stale value.

### 3.2 On FillEvent

When a `FillEvent` arrives, the portfolio updates positions and cash:

**BUY fill:**
```
on_fill(fill: FillEvent) — BUY side:
  total_cost = fill.filled_qty × fill.fill_price + fill.commission
  if symbol in positions:
    # Add to existing position: recompute weighted average cost
    existing_cost_basis = positions[symbol].quantity × positions[symbol].avg_cost
    new_cost_basis = existing_cost_basis + (fill.filled_qty × fill.fill_price)
    new_quantity = positions[symbol].quantity + fill.filled_qty
    positions[symbol].avg_cost = new_cost_basis / new_quantity
    positions[symbol].quantity = new_quantity
  else:
    # Open new position
    positions[symbol] = Position(
      symbol=symbol,
      quantity=fill.filled_qty,
      avg_cost=fill.fill_price,
      last_price=fill.fill_price
    )
  cash -= total_cost
```

**SELL fill:**
```
on_fill(fill: FillEvent) — SELL side:
  assert symbol in positions
  assert positions[symbol].quantity >= fill.filled_qty

  proceeds = fill.filled_qty × fill.fill_price - fill.commission
  realized = fill.filled_qty × (fill.fill_price - positions[symbol].avg_cost)
  realized_pnl += realized

  positions[symbol].quantity -= fill.filled_qty
  if positions[symbol].quantity == 0:
    del positions[symbol]           # Position closed

  cash += proceeds
```

**Partial fills:** The MVP's paper broker always fills in full (no partial fills for market orders). If partial fills are added in future, the same logic applies with `fill.filled_qty < original_qty`.

### 3.3 Invariants

These invariants must hold after every state transition. They are asserted in debug mode:

1. `cash >= 0` — Negative cash is impossible; `CashSolvencyRule` prevents over-buying.
2. `position.quantity >= 0` for all positions — MVP is long-only.
3. `equity >= 0` — Cannot have negative total portfolio value in a cash account.
4. `peak_equity >= equity` — Peak equity is monotonically non-decreasing.
5. `sum(position.quantity) for each symbol` is consistent with fill history.

---

## 4. Drawdown Tracking

Drawdown is computed continuously from the peak equity watermark.

```
drawdown_pct = max(0, (peak_equity - current_equity) / peak_equity)
```

`peak_equity` is initialized to `initial_cash` and is updated whenever `current_equity > peak_equity`.

The drawdown percentage is the primary input for `MaxDrawdownRule` in the risk engine. It is also exported in every `PortfolioSnapshotEvent` for performance reporting.

**Maximum drawdown (for performance reporting):** Tracked separately in the backtest metrics module. The portfolio state only tracks current drawdown — historical max drawdown is computed post-run from the `PortfolioSnapshotEvent` series.

---

## 5. Snapshot API (for Risk Engine)

```
PortfolioState.snapshot() -> PortfolioSnapshot
```

Creates an immutable deep copy of the portfolio's current state. Called by `RiskGatekeeper` before evaluating each intent.

```
PortfolioSnapshot
  timestamp       : datetime
  equity          : Decimal
  cash            : Decimal
  peak_equity     : Decimal
  drawdown_pct    : Decimal
  positions       : dict[str, PositionSnapshot]
  open_orders     : list[PendingOrder]    # Approved but unfilled orders
```

`open_orders` allows the risk engine to account for capital already committed to pending orders when evaluating new intents. Without this, the engine could approve multiple buys in the same bar cycle whose combined cost exceeds available cash.

---

## 6. Open Order Accounting

When `PaperBroker` receives an `ApprovedOrderEvent`, it immediately notifies `PortfolioState`:

```
PortfolioState.register_pending_order(order: PendingOrder)
  → reserves required_cash from cash (soft reservation)
```

When the fill arrives:
```
PortfolioState.on_fill(fill: FillEvent)
  → removes the pending reservation
  → applies the fill via standard BUY/SELL logic
```

If a pending order expires without filling (not possible in the MVP paper broker, but needed for live broker integration), the reservation is released.

---

## 7. P&L Accounting

### 7.1 Realized P&L

Computed at fill time using FIFO cost basis (First-In, First-Out). In the MVP, since we track a single average cost per position (not a FIFO queue), realized P&L is computed using the average cost basis:

```
realized_pnl_per_fill = filled_qty × (fill_price - avg_cost)
```

This is an approximation of FIFO. Full FIFO accounting (required for tax reporting) is an extension point — the `Position` model would need a `cost_lots: deque[CostLot]` field.

### 7.2 Unrealized P&L

Updated on every `BarEvent` via the MTM price refresh:

```
unrealized_pnl = sum(pos.quantity × (pos.last_price - pos.avg_cost) for pos in positions)
```

### 7.3 Total Return

Computed as:
```
total_return_pct = (equity - initial_cash) / initial_cash
```

Available as a property on `PortfolioState` and included in `PortfolioSnapshotEvent`.

---

## 8. PortfolioSnapshotEvent

At the end of each bar cycle, after all fills are processed, `PortfolioState` publishes a `PortfolioSnapshotEvent` to the bus. This is the primary data source for:

- Performance metrics computation (backtest runner)
- Real-time monitoring (live mode logging)
- Risk engine's drawdown check (next bar cycle)

```
PortfolioSnapshotEvent
  timestamp       : datetime
  equity          : Decimal
  cash            : Decimal
  initial_cash    : Decimal
  total_return_pct: Decimal
  realized_pnl    : Decimal
  unrealized_pnl  : Decimal
  drawdown_pct    : Decimal
  peak_equity     : Decimal
  positions       : list[PositionSnapshot]
  num_positions   : int
```

---

## 9. Long-Only Constraint

The MVP is strictly long-only:

- Strategies may only emit `Side.BUY` and `Side.SELL` (close) intents.
- Selling more shares than currently held is a hard error. `CashSolvencyRule` and `PositionSizeRule` do not prevent this scenario for sells — a separate `ShortSellingRule` that rejects SELL intents where `quantity > positions[symbol].quantity` must be in the rule chain.
- Short selling and margin are not modeled.

This constraint is enforced at three layers: strategy documentation, risk rule, and portfolio state assertion.
