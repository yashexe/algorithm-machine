# Algorithmic Trading Engine: System Architecture Spec

## 1. Project Overview

Design and implement a personal algorithmic trading MVP in Python. The system must operate on free internet data sources and focus on medium-to-long-term timeframes (swing trading/daily rebalancing). Execution speed (HFT) is not a priority.

## 2. Core Directives

* **Creative Freedom:** You are the Principal Architect. Choose the best design patterns, asynchronous data handling methods, and libraries. Optimize for code cleanliness, production-grade error handling, and extensibility.
* **The Invariant:** There must be a strict, unbreachable boundary between signal generation (the strategy) and order approval (the risk engine).

## 3. System Components

The architecture should loosely reflect these domains, though you have the freedom to structure the exact interfaces:

* **Data Ingestion:** Fetches and standardizes market data from free sources (e.g., yfinance) into a unified event state.
* **Strategy (Signal Generator):** Evaluates market events and proposes trades (Order Intents).
* **Risk Gatekeeper:** An independent module that evaluates Order Intents against account safety rules (max drawdown, position sizing). It has absolute authority to approve, resize, or reject trades.
* **Execution / Simulation:** Routes approved trades to a mock paper-trading engine to track portfolio state.

## 4. Required Deliverables

1. **Directory Structure:** A clean repository layout.
2. **Core Implementation:** The Python MVP, including the event loop, data flow, and risk gating mechanism.
3. **Sample Strategy:** A basic implementation (e.g., a simple moving average crossover) to prove the pipeline works end-to-end.
4. **Documentation:** A brief `README.md` explaining how to execute a backtest or simulation with this codebase.
