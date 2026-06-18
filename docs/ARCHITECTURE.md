# System Architecture

## 1. Design Philosophy

This system is built around three principles:

1. **The Invariant** — Signal generation and order approval are separated by a hard architectural boundary. The strategy layer proposes; the risk engine disposes. This is not a soft convention — it is enforced by dependency injection: strategies receive no handle to the broker, portfolio, or risk engine.

2. **Event-Driven, Not Call-Driven** — Trading decisions and state-changing outcomes flow through a typed event bus. Narrow state-read and reservation APIs are allowed only where explicitly listed in the component contract. This decouples the pipeline stages, makes replay (backtesting) trivial, and produces a complete audit trail as a natural by-product.

3. **Simplicity Over Premature Abstraction** — This is a swing-trading engine operating on daily bars. Nanosecond latency is irrelevant. The architecture optimizes for legibility, testability, and the ability to add a new strategy or risk rule without touching unrelated code.

---

## 2. Component Map

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CLOCK / SCHEDULER                           │
│              (drives the event loop on a daily cadence)             │
└────────────────────────────┬────────────────────────────────────────┘
                             │  triggers
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         DATA PIPELINE                               │
│  DataFetcher → Normalizer → BarEvent                                │
│  (yfinance adapter, OHLCV normalization, on-disk bar cache)         │
└────────────────────────────┬────────────────────────────────────────┘
                             │  publishes BarEvent
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                           EVENT BUS                                 │
│  Typed pub/sub bus for decisions and state-changing outcomes.       │
└──┬───────────────────────────────────────────────────────────────┬──┘
   │ delivers BarEvent to subscribed strategies                    │
   ▼                                                               │
┌──────────────────────────────────────┐                           │
│         STRATEGY LAYER               │                           │
│  AbstractStrategy.on_bar(BarEvent)   │                           │
│  Reads: market data only             │                           │
│  Emits: OrderIntent (proposal only)  │                           │
│  Cannot access: portfolio, broker,   │                           │
│  risk engine, or other strategies    │                           │
└──────────────────────────────────────┘                           │
   │ publishes OrderIntentEvent                                    │
   ▼                                                               │
┌──────────────────────────────────────┐                           │
│        RISK GATEKEEPER               │ ◄─── PortfolioSnapshot     │
│  Receives: OrderIntent               │                           │
│  Reads: immutable snapshot only      │                           │
│  Evaluates: rule chain               │                           │
│  Emits: ApprovedOrder / RejectedOrder│                           │
│  Authority: absolute (can resize,    │                           │
│  reject, or pass through)            │                           │
└──────────────────────────────────────┘                           │
   │ publishes ApprovedOrderEvent                                  │
   ▼                                                               │
┌──────────────────────────────────────┐                           │
│        EXECUTION ENGINE              │                           │
│  PaperBroker.submit(ApprovedOrder)   │                           │
│  Simulates fill (slippage, fees)     │                           │
│  Publishes: OrderSubmitted / Fill    │                           │
└──────────────────────────────────────┘                           │
   │ publishes FillEvent when filled                              │
   ▼                                                               │
┌──────────────────────────────────────┐                           │
│        PORTFOLIO STATE               │ ──────────────────────────┘
│  Consumes: FillEvent, BarEvent        │
│  Maintains: positions, cash, equity   │
│  Publishes: PortfolioSnapshotEvent    │
└──────────────────────────────────────┘
```

---

## 3. Data Flow — One Bar Cycle

The following sequence describes a single daily bar cycle from data arrival to portfolio update.

```
1. Clock fires (daily at market close + buffer)
2. DataFetcher.fetch(universe, date) → raw OHLCV dict
3. Normalizer.normalize(raw) → List[BarEvent]
4. EventBus.publish(BarEvent) for each symbol
5. PortfolioState.on_bar(BarEvent) — update mark-to-market prices
6. Strategy.on_bar(BarEvent) — evaluate signals
   └── if signal triggered: EventBus.publish(OrderIntentEvent)
7. RiskGatekeeper.on_order_intent(OrderIntentEvent)
   ├── receives current PortfolioSnapshot
   ├── runs rule chain (drawdown, sizing, concentration)
   └── publishes ApprovedOrderEvent OR RejectedOrderEvent
8. PaperBroker.on_approved_order(ApprovedOrderEvent)
   ├── registers a pending cash reservation for BUY orders
   ├── simulates fill at next-bar open (no look-ahead)
   └── publishes FillEvent if filled
9. PortfolioState.on_fill(FillEvent) — update positions and cash
10. PortfolioState.publish_snapshot() → PortfolioSnapshotEvent
11. Logger/Metrics consume PortfolioSnapshotEvent
```

**Look-ahead discipline:** Strategies only see bars whose timestamp ≤ current simulation time. Fills are simulated at the *next bar's open*, not at the signal bar's close. See [EVENT_SYSTEM.md](EVENT_SYSTEM.md) for ordering guarantees.

---

## 4. Inter-Component Contracts

Each component has a single, narrow interface to the rest of the system. The table below is the authoritative contract. If a component needs something not listed here, the architecture must be extended — not worked around.

| Component | Inputs (events consumed) | Outputs (events published) | Direct dependencies |
|---|---|---|---|
| DataFetcher | Clock tick | `BarEvent` | External data source |
| Strategy | `BarEvent` | `OrderIntentEvent` | None |
| RiskGatekeeper | `OrderIntentEvent` | `ApprovedOrderEvent`, `RejectedOrderEvent` | `PortfolioSnapshot` provider |
| PaperBroker | `ApprovedOrderEvent`, `BarEvent` (for next-open fill) | `OrderSubmittedEvent`, `FillEvent` | `PortfolioState` pending-order reservation API |
| PortfolioState | `FillEvent`, `BarEvent` | `PortfolioSnapshotEvent` | None |

---

## 5. The Invariant — Formal Statement

```
∀ order o:
  o reaches PaperBroker.submit()
  ⟹ ∃ ApprovedOrder a such that
      a = RiskGatekeeper.evaluate(intent)
      ∧ a.origin_intent = o
      ∧ a.approved = True
```

In implementation terms: `PaperBroker.submit()` accepts only `ApprovedOrder` objects. `ApprovedOrder` is a final dataclass with no public constructor — it can only be instantiated by `RiskGatekeeper`. This is enforced by making the constructor private (`_approved_order_factory`) and importing it only within `gatekeeper.py`.

---

## 6. Technology Choices

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Ecosystem (pandas, yfinance), adequate for daily-bar latency |
| Event bus | In-process synchronous pub/sub (custom) | No broker overhead for a single-process engine; trivial to test |
| Data fetching | `yfinance` (primary) | Free, reliable for EOD bars, wide symbol coverage |
| Data model | `pandas.DataFrame` for series, `dataclasses` for events | DataFrames for indicator math; typed dataclasses for events crossing boundaries |
| Config | YAML + `pydantic` validation | Human-editable, schema-validated at startup |
| Async | None at MVP | Daily bars require no concurrency; add `asyncio` only if live tick data is added later |
| Testing | `pytest` + `hypothesis` for risk rules | Property-based testing is appropriate for rule-chain correctness |

---

## 7. Extension Points

The architecture is designed so that the following changes require **no modification to existing components**:

- **Add a new strategy** — subclass `AbstractStrategy`, place in `strategies/`, reference in config.
- **Add a new risk rule** — subclass `AbstractRule`, place in `engine/risk/rules/`, add to rule chain in config.
- **Add a new data source** — implement `AbstractFetcher`, register in `engine/data/fetchers/`.
- **Replace paper broker with live broker** — implement `AbstractBroker` and swap in config. The risk engine, portfolio state, and strategies are unaffected.

---

## 8. What This Architecture Deliberately Does Not Do

- **No HFT / tick data.** The event bus is synchronous and not optimized for sub-millisecond throughput.
- **No multi-process distribution.** All components run in a single Python process.
- **No live order routing.** The MVP only supports paper trading. Live broker integration is an extension point, not a current concern.
- **No ML model inference.** Strategies are rule-based. ML feature generation can be added as a pre-processing step within a strategy's `on_bar` handler.
