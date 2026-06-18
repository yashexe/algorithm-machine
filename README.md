# Algorithm Machine

A personal algorithmic trading engine built for swing trading and daily rebalancing on free market data. The system is designed around a single core invariant: **the risk engine has absolute, unbreachable authority over order approval**. No trade reaches the execution layer without passing through the risk gatekeeper.

---

## Documentation Tree

| Document | Purpose |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System philosophy, component map, data-flow diagram, inter-component contracts |
| [docs/DATA_PIPELINE.md](docs/DATA_PIPELINE.md) | Data sources, bar event schema, normalization, caching, universe management |
| [docs/EVENT_SYSTEM.md](docs/EVENT_SYSTEM.md) | Event taxonomy, event bus design, ordering guarantees, look-ahead bias prevention |
| [docs/STRATEGY_INTERFACE.md](docs/STRATEGY_INTERFACE.md) | Strategy protocol, Order Intent schema, strategy isolation rules |
| [docs/RISK_ENGINE.md](docs/RISK_ENGINE.md) | The gatekeeper contract, built-in rules, approval/resize/reject decision model |
| [docs/PORTFOLIO_STATE.md](docs/PORTFOLIO_STATE.md) | Portfolio and position data models, MTM valuation, drawdown tracking, state transitions |
| [docs/EXECUTION_ENGINE.md](docs/EXECUTION_ENGINE.md) | Paper broker, order lifecycle, fill simulation (slippage + commissions) |
| [docs/BACKTEST_HARNESS.md](docs/BACKTEST_HARNESS.md) | Historical replay loop, temporal consistency, performance metrics, reporting |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Full configuration schema, all tunable parameters, environment variables |

---

## Repository Layout

```
algorithm-machine/
├── README.md
├── docs/                        # Specification tree (read before touching code)
│   ├── ARCHITECTURE.md
│   ├── DATA_PIPELINE.md
│   ├── EVENT_SYSTEM.md
│   ├── STRATEGY_INTERFACE.md
│   ├── RISK_ENGINE.md
│   ├── PORTFOLIO_STATE.md
│   ├── EXECUTION_ENGINE.md
│   ├── BACKTEST_HARNESS.md
│   └── CONFIGURATION.md
│
├── engine/                      # Core runtime (framework code — do not edit per-strategy)
│   ├── data/
│   │   ├── fetchers/            # Source adapters (yfinance, etc.)
│   │   ├── normalizer.py        # OHLCV → BarEvent
│   │   └── cache.py             # On-disk bar cache
│   ├── events/
│   │   ├── bus.py               # EventBus (publish / subscribe)
│   │   └── types.py             # All event dataclasses
│   ├── strategy/
│   │   └── base.py              # AbstractStrategy protocol
│   ├── risk/
│   │   ├── gatekeeper.py        # RiskGatekeeper orchestrator
│   │   └── rules/               # Individual rule implementations
│   ├── portfolio/
│   │   ├── state.py             # PortfolioState aggregate
│   │   └── position.py          # Position model
│   ├── execution/
│   │   ├── paper_broker.py      # PaperBroker (fill simulation)
│   │   └── order.py             # Order + OrderStatus models
│   └── backtest/
│       ├── runner.py            # BacktestRunner (historical replay)
│       └── metrics.py           # Performance analytics
│
├── strategies/                  # User-authored strategies live here
│   └── sma_crossover.py         # Reference implementation
│
├── config/
│   └── default.yaml             # Default configuration (see docs/CONFIGURATION.md)
│
├── tests/
│   ├── unit/
│   └── integration/
│
├── notebooks/                   # Exploratory analysis only — never imported by engine
│
├── pyproject.toml
└── .env.example
```

---

## Quickstart

```bash
# 1. Install dependencies
pip install -e ".[dev]"

# 2. Run the reference backtest (SMA crossover on SPY, 2020–2024)
python -m engine.backtest.runner --config config/default.yaml --strategy sma_crossover

# 3. Run live paper trading (simulated, no real orders)
python -m engine.run --config config/default.yaml --strategy sma_crossover --mode paper
```

---

## The One Rule You Must Not Break

> The strategy layer **must never** directly modify portfolio state, call the broker, or bypass the risk engine. An `OrderIntent` is a *proposal*. Only `RiskGatekeeper.evaluate()` produces an `ApprovedOrder`. Only `PaperBroker.submit()` accepts an `ApprovedOrder`.

This boundary is enforced architecturally — the strategy receives no reference to the portfolio, broker, or risk engine. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
