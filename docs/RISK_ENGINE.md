# Risk Engine Specification

## 1. The Gatekeeper Contract

The risk engine is the single point of authority over whether a proposed trade becomes a real order. It sits between the strategy layer and the execution layer, and its verdict is final.

**Formal contract:**
```
RiskGatekeeper.evaluate(intent: OrderIntentEvent, portfolio: PortfolioSnapshot)
  → ApprovedOrder | RejectedOrder
```

This function is the only path by which an `OrderIntentEvent` can become an `ApprovedOrderEvent`. There is no bypass, no override flag, and no emergency path. An `ApprovedOrder` cannot be constructed outside of `gatekeeper.py`.

**The three possible outcomes:**

| Outcome | Meaning |
|---|---|
| **Approve** | Intent passes all rules as-is. `approved_qty == intent.quantity`. |
| **Resize + Approve** | Intent passes after quantity reduction. `approved_qty < intent.quantity`. At least one rule reduced the size. |
| **Reject** | Intent fails a hard rule. No order is created. |

---

## 2. PortfolioSnapshot (Risk Engine's View)

The risk engine never holds a live reference to `PortfolioState`. Instead, it receives an **immutable snapshot** taken at the start of each evaluation. This prevents mid-evaluation state mutation and makes the evaluation a pure function.

```
PortfolioSnapshot
  timestamp       : datetime
  equity          : Decimal          # Total portfolio value
  cash            : Decimal
  peak_equity     : Decimal          # All-time high equity (for drawdown calc)
  positions       : dict[str, PositionSnapshot]
  open_orders     : list[PendingOrder]   # Orders approved but not yet filled
  drawdown_pct    : Decimal          # (peak - current) / peak
```

`PortfolioState.snapshot()` produces this object. It is a deep copy — the risk engine's rules cannot mutate portfolio state.

---

## 3. Rule Chain Architecture

The risk engine runs a **sequential rule chain**. Rules are evaluated in a defined order. Each rule can:

- **Pass** — no objection, evaluation continues to the next rule.
- **Resize** — reduce the quantity and pass. Subsequent rules see the reduced quantity.
- **Reject** — terminate the chain immediately. No further rules are evaluated.

```
RuleChain
  rules: list[AbstractRule]      # Ordered list of rule instances

  .evaluate(intent, snapshot) -> (verdict: Verdict, trace: list[RuleTrace])
```

### 3.1 RuleTrace

Every rule evaluation produces a `RuleTrace` entry, whether or not it triggered:

```
RuleTrace
  rule_name     : str
  verdict       : Verdict          # PASS | RESIZE | REJECT
  original_qty  : Decimal
  output_qty    : Decimal          # Same as original_qty if PASS or REJECT
  reason        : str              # Human-readable explanation
```

The full trace is attached to every `ApprovedOrderEvent` and `RejectedOrderEvent`. This is the primary debugging tool for understanding why a trade was (or wasn't) made.

---

## 4. Built-In Rules

### 4.1 MaxDrawdownRule

**Purpose:** Halt all new BUY orders when portfolio drawdown exceeds a threshold.

**Parameters:**
```yaml
max_drawdown_pct: 0.15   # 15% drawdown from peak equity halts new buys
```

**Logic:**
- Compute `drawdown = (peak_equity - current_equity) / peak_equity`
- If `drawdown > max_drawdown_pct` and `intent.side == BUY`: **REJECT**
- SELL orders (closing positions) are always allowed through this rule.

**Rationale:** A 15% drawdown is a strong signal that current market conditions are hostile. New long exposure should not be added until recovery. Sells are exempted because forcing position closure in a drawdown can lock in losses; the rule's purpose is to prevent *adding* risk, not to force liquidation.

---

### 4.2 PositionSizeRule

**Purpose:** Cap the notional value of any single position as a percentage of portfolio equity.

**Parameters:**
```yaml
max_position_pct: 0.20   # No single position may exceed 20% of equity
```

**Logic:**
1. Compute `current_notional = snapshot.positions[symbol].market_value` (0 if no position).
2. Compute `intent_notional = intent.quantity × current_bar_close_price`.
3. Compute `proposed_notional = current_notional + intent_notional` (for BUY) or `current_notional - intent_notional` (for SELL).
4. Compute `max_allowed_notional = snapshot.equity × max_position_pct`.
5. If `proposed_notional > max_allowed_notional`:
   - Compute `allowed_additional = max_allowed_notional - current_notional`.
   - If `allowed_additional ≤ 0`: **REJECT** (position is already at or over cap).
   - Else: **RESIZE** quantity to `floor(allowed_additional / current_bar_close_price)`.
6. If resized quantity is 0 or negative: **REJECT**.

---

### 4.3 CashSolvencyRule

**Purpose:** Prevent orders that would require more cash than available.

**Parameters:** None (uses current cash balance).

**Logic:**
1. Compute `required_cash = intent.quantity × current_price × (1 + commission_rate)`.
2. If `intent.side == BUY` and `required_cash > snapshot.cash`:
   - Compute affordable quantity: `affordable_qty = floor(snapshot.cash / (current_price × (1 + commission_rate)))`.
   - If `affordable_qty ≤ 0`: **REJECT**.
   - Else: **RESIZE** to `affordable_qty`.
3. SELL orders skip this rule (selling generates cash, not consumes it).

---

### 4.4 ConcentrationRule

**Purpose:** Limit the total number of open positions to avoid over-diversification of attention and under-diversification of capital.

**Parameters:**
```yaml
max_open_positions: 10
```

**Logic:**
- If `intent.side == BUY` and `len(snapshot.positions) >= max_open_positions` and `symbol not in snapshot.positions`:
  - **REJECT** (opening a new position would exceed the limit).
- Sells and adds to existing positions are not affected.

---

### 4.5 DailyOrderLimitRule

**Purpose:** Rate-limit the number of order intents processed per bar cycle to prevent strategy bugs from flooding the system.

**Parameters:**
```yaml
max_orders_per_day: 10
```

**Logic:**
- Tracks count of `ApprovedOrderEvent` objects in the current bar cycle.
- If `today_approved_count >= max_orders_per_day`: **REJECT** all further intents for this bar cycle.
- Reset counter at the start of each new bar cycle.

---

## 5. Rule Configuration and Ordering

Rules are configured as an ordered list in `config.yaml`. The order determines evaluation sequence.

```yaml
risk:
  rules:
    - rule: "MaxDrawdownRule"
      max_drawdown_pct: 0.15

    - rule: "DailyOrderLimitRule"
      max_orders_per_day: 10

    - rule: "PositionSizeRule"
      max_position_pct: 0.20

    - rule: "CashSolvencyRule"

    - rule: "ConcentrationRule"
      max_open_positions: 10
```

**Recommended ordering rationale:**
1. `MaxDrawdownRule` first — hard safety brake, cheapest to evaluate, should short-circuit immediately in drawdown.
2. `DailyOrderLimitRule` second — catches strategy bugs before expensive sizing math.
3. `PositionSizeRule` — computes target size.
4. `CashSolvencyRule` — further constrains by available cash.
5. `ConcentrationRule` last — universe-level check, least likely to trigger.

---

## 6. Adding a Custom Rule

Implement `AbstractRule`:

```
AbstractRule
  .name: str                   # Unique rule identifier
  .evaluate(
      intent: OrderIntentEvent,
      snapshot: PortfolioSnapshot,
      current_price: Decimal   # Bar close price for the intent's symbol
  ) -> RuleResult

RuleResult
  verdict      : Verdict         # PASS | RESIZE | REJECT
  output_qty   : Decimal
  reason       : str
```

Register the rule class in `engine/risk/rules/__init__.py` and add it to the config.

---

## 7. Risk Engine Audit Log

In addition to the event bus trace, the risk engine maintains its own structured log:

```
{config.log_dir}/risk/{run_id}_risk.ndjson
```

Each line contains:
- `intent_id` — UUID of the `OrderIntentEvent`
- `strategy_id`
- `symbol`, `side`, `intent_qty`
- `verdict` — final outcome
- `approved_qty` (if approved/resized)
- `full_rule_trace` — array of `RuleTrace` objects
- `snapshot_equity`, `snapshot_cash`, `snapshot_drawdown_pct`
- `timestamp`

This log is separate from the event log to allow independent analysis of risk decisions without parsing the full event stream.

---

## 8. Testing the Risk Engine

Risk rules must be tested with property-based tests (using `hypothesis`) because their correctness conditions hold across a continuous domain of inputs, not just spot values.

**Key properties to verify for each rule:**

- **PositionSizeRule:** `∀ approved_qty, price: approved_qty × price ≤ equity × max_pct`
- **CashSolvencyRule:** `∀ approved_qty, price: approved_qty × price × (1 + fee) ≤ cash`
- **MaxDrawdownRule:** `drawdown > threshold ∧ side == BUY ⟹ verdict == REJECT`
- **Resize idempotence:** Running the rule chain a second time on an already-resized intent produces the same output.
- **No negative quantities:** No rule ever produces `output_qty < 0`.
