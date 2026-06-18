# Execution Engine Specification

## 1. Role

The execution engine is the layer that turns an approved order into a simulated fill. In the MVP, this is exclusively the `PaperBroker` — a paper trading simulator that models realistic fill prices including slippage and commissions.

The `PaperBroker` receives only `ApprovedOrderEvent` objects. It will not accept anything else. This structural constraint enforces the invariant: no fill happens without prior risk engine approval.

---

## 2. AbstractBroker Interface

The paper broker implements `AbstractBroker`, allowing a live broker adapter to be swapped in without changing any upstream code.

```
AbstractBroker
  .submit(order: ApprovedOrder) -> None
      Accept an approved order for execution. Implementations may queue it
      for next-bar fill (paper) or route it to a real exchange (live).

  .on_bar(event: BarEvent) -> None
      Provide the broker with the latest bar data. The paper broker uses this
      to simulate fills for pending orders (next-bar-open fill discipline).

  .cancel(order_id: UUID) -> None
      Cancel a pending order. In paper mode, removes from the pending queue.
```

---

## 3. PaperBroker

### 3.1 Core Design

The paper broker operates on a **pending orders queue**. When it receives an `ApprovedOrderEvent`, it does not fill immediately. Instead it enqueues the order and waits for the next `BarEvent` for that symbol. The fill is simulated at the next bar's open price, plus slippage.

Because this is a cash account, a queued BUY order must also remain affordable at the actual next-open fill price. If an overnight gap, slippage, or commission would push cash below zero, the broker cancels that pending order before publishing a fill. This is a last-resort cash guard, not a partial fill model.

This design enforces the look-ahead bias prevention rule described in [EVENT_SYSTEM.md](EVENT_SYSTEM.md): a signal generated on bar T's close cannot be filled at bar T's close. It is filled at bar T+1's open.

### 3.2 Order Lifecycle

```
ApprovedOrderEvent received
  → PaperBroker._pending[symbol].append(PendingOrder)
  → EventBus.publish(OrderSubmittedEvent)

Next BarEvent[symbol] received
  → for each PendingOrder in _pending[symbol]:
      fill_price = simulate_fill_price(bar.open, order.side, slippage_model)
      commission = simulate_commission(order.approved_qty, fill_price)
      if BUY and total_cost > available_cash:
          mark order CANCELLED
          release reservation
          remove from _pending[symbol]
          continue
      fill = FillEvent(...)
      EventBus.publish(FillEvent)
      _pending[symbol].remove(order)
```

### 3.3 Order States

```
Enum OrderStatus:
  PENDING     # Enqueued, awaiting next bar
  FILLED      # Fill event published
  CANCELLED   # Cancelled before fill, including unaffordable next-open BUY
  EXPIRED     # Limit order expired (not applicable in MVP market orders)
```

`PendingOrder` holds:
```
PendingOrder
  order_id      : UUID
  origin_event  : ApprovedOrderEvent
  symbol        : str
  side          : Side
  order_type    : IntentType        # MARKET in MVP
  approved_qty  : Decimal
  limit_price   : Decimal | None
  submitted_at  : datetime
  status        : OrderStatus
```

---

## 4. Fill Simulation Model

### 4.1 Market Orders (MVP)

All executable orders in the MVP are simulated as market orders filled at the next bar's open. The fill model applies a slippage adjustment to the open price. BUY orders that are no longer affordable at the actual next-open fill price are cancelled before fill instead of creating negative cash.

**Fill price formula:**

```
BUY  fill_price = bar.open × (1 + slippage_pct)
SELL fill_price = bar.open × (1 - slippage_pct)
```

Slippage is modeled as a fixed percentage (adverse price impact: buys fill slightly above open, sells slightly below). Default: `0.05%` per side.

**Rationale for open-price fill:** Daily-bar swing trading orders are typically submitted as market-on-open (MOO) orders. Using the bar open is the most realistic simple approximation for this trading style. Using the prior day's close would be optimistic (gaps can and do occur).

### 4.2 Slippage Model

The MVP uses a simple **fixed-percentage slippage model**:

```yaml
execution:
  slippage_model: "fixed_pct"
  slippage_pct: 0.0005           # 0.05% per trade per side
```

**Future extension:** A volume-based slippage model (e.g., Almgren-Chriss) can be plugged in by implementing `AbstractSlippageModel`:

```
AbstractSlippageModel
  .compute(bar: BarEvent, side: Side, quantity: Decimal) -> Decimal
      Returns the fill price (including slippage) for a given order.
```

The fixed-pct model is used for daily swing trading where order sizes are small relative to ADV. If the engine is ever extended to handle larger positions, the volume-based model should be activated.

### 4.3 Commission Model

```yaml
execution:
  commission_model: "per_share"
  commission_per_share: 0.005    # $0.005 per share, consistent with IBKR tiered pricing
  min_commission: 1.00           # Minimum $1.00 per order
```

**Commission formula:**
```
commission = max(min_commission, approved_qty × commission_per_share)
```

Commission is deducted from cash in the portfolio on every fill, regardless of side. The `FillEvent` carries the commission amount as a separate field so P&L attribution is clean.

**Alternative model — zero commission:**
```yaml
execution:
  commission_model: "zero"
```

Useful for initial strategy development to isolate signal quality from friction costs.

---

## 5. Fill Event

The `FillEvent` is the authoritative record of an executed trade. Its fields are described in [EVENT_SYSTEM.md](EVENT_SYSTEM.md). Key points:

- `fill_price` includes slippage — it is the realistic execution price, not the raw bar open.
- `commission` is the total commission for this fill (not per-share).
- `fill_bar` is the `BarEvent` that triggered the fill — the bar whose open was used as the base price.
- Partial fills are not modeled in the MVP. A market order either fills in full or is cancelled before fill by the cash-account guard.

---

## 6. Order Validation

Before enqueuing, `PaperBroker` performs a final sanity check on the `ApprovedOrderEvent`:

1. `approved_qty > 0` — reject orders with zero or negative quantity.
2. `symbol in current_universe` — reject orders for unknown symbols.
3. No duplicate `order_id` in the pending queue.
4. BUY reservation cost fits available cash using the signal bar close, slippage, and commission.

These checks are last-resort guards. They should never trigger if the risk engine is functioning correctly. Failures here are logged at `ERROR` level and the order is dropped (not submitted).

The broker repeats the BUY cash check at fill time using the actual next-open fill price. If this check fails, the submitted order is marked `CANCELLED`, its reservation is released, and no `FillEvent` is published.

---

## 7. Multiple Pending Orders for the Same Symbol

If two strategies both emit approved BUY orders for the same symbol within the same bar cycle, both will be queued. Any order that remains affordable at the next bar's open will fill at that open price. `PortfolioState` will process fills sequentially, with later fills correctly computing the new weighted average cost against the updated position.

The risk engine's `open_orders` accounting (see [PORTFOLIO_STATE.md](PORTFOLIO_STATE.md)) prevents double-committing cash: when the first order is approved and registered as pending, the cash reservation reduces the available cash that the second order's evaluation sees.

---

## 8. Live Broker Extension Point

To replace `PaperBroker` with a live broker:

1. Implement `AbstractBroker` with real order routing (e.g., IBKR TWS API, Alpaca API).
2. The live broker's `on_bar` method becomes a no-op (fills arrive asynchronously via broker callbacks).
3. Broker callbacks must publish `FillEvent` objects to the same event bus.
4. Register the live broker class in config:

```yaml
execution:
  broker: "engine.execution.ibkr_broker.IBKRBroker"
```

The risk engine, portfolio state, and strategies require zero changes.

---

## 9. Configuration Reference

```yaml
execution:
  broker: "engine.execution.paper_broker.PaperBroker"

  slippage_model: "fixed_pct"
  slippage_pct: 0.0005

  commission_model: "per_share"
  commission_per_share: 0.005
  min_commission: 1.00

  fill_at: "next_open"       # next_open | prev_close (prev_close is optimistic, for comparison only)
```
