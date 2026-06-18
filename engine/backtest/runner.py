"""
BacktestRunner — wires all engine components and replays historical bars.

Handler registration order (canonical, from EVENT_SYSTEM.md §3.3):
  1. PortfolioState.on_bar     — MTM update before strategies evaluate
  2. strategy.on_bar           — signal generation
  3. RiskGatekeeper.on_order_intent
  4. PaperBroker.on_approved_order
  5. PortfolioState.on_fill
  6. EventAuditLogger.on_any   — registered last so it always fires
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd

from engine.backtest.calendar import trading_days
from engine.backtest.metrics import BacktestResult, MetricsEngine
from engine.config.schema import AppConfig, StrategyConfig
from engine.data import DataPipeline
from engine.events.bus import EventBus
from engine.events.logger import EventAuditLogger
from engine.events.types import (
    ApprovedOrderEvent,
    BarEvent,
    BaseEvent,
    FillEvent,
    OrderIntentEvent,
    PortfolioSnapshotEvent,
)
from engine.execution.paper_broker import PaperBroker
from engine.portfolio.state import PortfolioState
from engine.risk.gatekeeper import RiskGatekeeper
from engine.risk.rules import build_rule_chain
from engine.strategy.base import AbstractStrategy

logger = logging.getLogger(__name__)


class BacktestRunner:
    """
    Builds all engine components from an AppConfig and runs the replay loop.

    Usage::

        config = load_config("config/default.yaml")
        result = BacktestRunner(config).run()
        result.print_summary()
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def run(self, *, save_artifacts: bool = True, write_log_files: bool = True) -> BacktestResult:
        cfg = self._config
        run_id: str = cfg.engine.run_id or __import__("uuid").uuid4().hex

        _configure_logging(cfg.engine.log_level, cfg.engine.log_dir, run_id, write_log_files)
        logger.info("Starting backtest run %s", run_id)

        # -- Portfolio state --
        portfolio = PortfolioState(
            initial_cash=cfg.backtest.initial_cash,
            run_id=run_id,
        )

        # -- Event bus --
        bus = EventBus()

        # -- Risk engine --
        rule_entries = [{"rule": r.rule, **r.params} for r in cfg.risk.rules]
        rule_chain = build_rule_chain(rule_entries)
        gatekeeper = RiskGatekeeper(
            bus=bus,
            snapshot_provider=portfolio.snapshot,
            rule_chain=rule_chain,
            risk_log_path=(
                Path(cfg.engine.log_dir) / "risk" / f"{run_id}.ndjson"
                if write_log_files else None
            ),
        )

        # -- Paper broker --
        exec_cfg = cfg.execution
        broker = PaperBroker(
            bus=bus,
            portfolio=portfolio,
            universe=cfg.universe.symbols,
            slippage_model=exec_cfg.slippage_model,
            slippage_pct=exec_cfg.slippage_pct,
            commission_model=exec_cfg.commission_model,
            commission_per_share=exec_cfg.commission_per_share,
            min_commission=exec_cfg.min_commission,
            fill_at=exec_cfg.fill_at,
            max_participation_pct=exec_cfg.max_participation_pct,
        )

        # -- Strategies --
        strategies = [
            _instantiate_strategy(s_cfg, bus)
            for s_cfg in cfg.strategies
        ]

        # -- Register handlers in canonical order --
        # 1. Broker fills pending orders at today's open before portfolio MTM at today's close.
        #    This ensures sells queued yesterday fill at today's open price, preventing the
        #    portfolio from MTM'ing a position at a higher close that was never actually held.
        bus.subscribe(BarEvent, broker.on_bar)
        # 2. Portfolio MTM at today's close — runs after fills so peak_equity reflects
        #    settled positions, not positions that were exited at today's open.
        bus.subscribe(BarEvent, portfolio.on_bar)
        # 3. Strategies generate signals against up-to-date portfolio state
        for strategy in strategies:
            bus.subscribe(BarEvent, strategy.on_bar)
        # 3b. Strategies observe end-of-day snapshot for circuit breakers
        for strategy in strategies:
            bus.subscribe(PortfolioSnapshotEvent, strategy.on_snapshot)
        # 4-6. Risk → broker queuing → fill accounting
        # Warmup gate: intents whose signal bar pre-dates scored_start are discarded so
        # warmup bars accumulate price history without opening real positions.
        scored_start: date | None = cfg.backtest.scored_start

        def _gated_intent(event: OrderIntentEvent) -> None:
            if scored_start is not None and event.signal_bar.timestamp.date() < scored_start:
                return
            gatekeeper.on_order_intent(event)

        bus.subscribe(OrderIntentEvent, _gated_intent)
        bus.subscribe(ApprovedOrderEvent, broker.on_approved_order)
        bus.subscribe(FillEvent, portfolio.on_fill)
        # 6. Strategy fill reconciliation — fires after portfolio.on_fill so cash/position
        #    state is already settled when the strategy updates its internal holdings.
        for strategy in strategies:
            bus.subscribe(FillEvent, strategy.on_fill)
        if write_log_files:
            audit_logger = EventAuditLogger.for_run(cfg.engine.log_dir, run_id)
            bus.subscribe(BaseEvent, audit_logger.on_any)

        # -- Fetch bars --
        data_cfg = cfg.data
        pipeline = DataPipeline.build(
            cache_dir=data_cfg.cache_dir,
            batch_size=data_cfg.batch_size,
            staleness_hours=data_cfg.cache_staleness_hours,
            bar_close_utc_hour=data_cfg.bar_close_utc_hour,
            adjusted=data_cfg.adjusted,
        )

        start: date = cfg.backtest.start_date
        end: date = cfg.backtest.end_date
        universe = cfg.universe.symbols
        benchmark_symbol = cfg.universe.benchmark

        symbols_to_fetch = list(universe)
        if benchmark_symbol and benchmark_symbol not in symbols_to_fetch:
            symbols_to_fetch.append(benchmark_symbol)

        logger.info("Fetching bars for %d symbols %s → %s", len(symbols_to_fetch), start, end)
        all_bars = pipeline.get_bars(symbols_to_fetch, start, end)

        universe_set = set(universe)
        universe_bars = [b for b in all_bars if b.symbol in universe_set]
        benchmark_returns: pd.Series | None = None
        if benchmark_symbol:
            bench_bars = [b for b in all_bars if b.symbol == benchmark_symbol]
            benchmark_returns = _benchmark_returns(bench_bars)

        logger.info("Loaded %d universe bars", len(universe_bars))

        # -- Build per-date lookup --
        bars_by_date: dict[date, list[BarEvent]] = {}
        for bar in universe_bars:
            bars_by_date.setdefault(bar.timestamp.date(), []).append(bar)

        # -- Start strategies --
        for strategy in strategies:
            strategy.on_start(universe)

        # -- Replay loop --
        days = trading_days(start, end)
        logger.info("Replaying %d trading days", len(days))

        bar_dates: list[date] = []  # dates where at least one bar was published
        for day in days:
            day_bars = sorted(bars_by_date.get(day, []), key=lambda b: b.symbol)
            for bar in day_bars:
                bus.publish(bar)
            if day_bars:
                portfolio.publish_snapshot(bus)
                bar_dates.append(day)

        # -- Stop strategies --
        for strategy in strategies:
            strategy.on_stop()

        # -- Compute metrics --
        snapshots: list[PortfolioSnapshotEvent] = bus.get_history(PortfolioSnapshotEvent)
        fills: list[FillEvent] = bus.get_history(FillEvent)
        strategy_id = strategies[0].strategy_id if strategies else "unknown"

        logger.info(
            "Run complete — %d snapshots, %d fills", len(snapshots), len(fills)
        )

        result = MetricsEngine.compute(
            snapshots=snapshots,
            fills=fills,
            initial_cash=cfg.backtest.initial_cash,
            risk_free_rate=float(cfg.backtest.risk_free_rate),
            trading_day_count=len(days),
            run_id=run_id,
            strategy_id=strategy_id,
            start_date=start,
            end_date=end,
            benchmark_returns=benchmark_returns,
            bar_dates=bar_dates,
        )

        # -- Save output --
        if save_artifacts:
            output_dir = Path(cfg.engine.output_dir) / run_id
            result.save(output_dir)
            logger.info("Results saved to %s", output_dir)

        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _instantiate_strategy(
    strategy_cfg: StrategyConfig,
    bus: EventBus,
) -> AbstractStrategy:
    from engine.config.schema import resolve_dotted_path
    cls = resolve_dotted_path(strategy_cfg.class_path)
    if not issubclass(cls, AbstractStrategy):
        raise TypeError(
            f"{strategy_cfg.class_path!r} must be an AbstractStrategy subclass"
        )
    return cls(strategy_id=strategy_cfg.id, config=strategy_cfg, bus=bus)


def _benchmark_returns(bars: list[BarEvent]) -> pd.Series:
    if not bars:
        return pd.Series(dtype=float)
    sorted_bars = sorted(bars, key=lambda b: b.timestamp)
    dates = pd.to_datetime([b.timestamp.date() for b in sorted_bars])
    closes = [float(b.close) for b in sorted_bars]
    closes_series = pd.Series(closes, index=dates)
    return closes_series.pct_change().dropna()


def _configure_logging(level: str, log_dir: str, run_id: str, write_log_file: bool = True) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if write_log_file:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(Path(log_dir) / f"{run_id}.log", encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        handlers=handlers,
    )
