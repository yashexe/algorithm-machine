from __future__ import annotations

import importlib
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engine.execution.base import AbstractBroker
from engine.risk.rules import RULE_REGISTRY, create_rule


class EngineConfig(BaseModel):
    mode: Literal["backtest", "paper"] = "backtest"
    run_id: str | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_dir: str = "logs"
    output_dir: str = "results"
    seed: int = 42

    @model_validator(mode="after")
    def ensure_run_id(self) -> "EngineConfig":
        if self.run_id is None:
            self.run_id = uuid4().hex
        return self


class UniverseConfig(BaseModel):
    symbols: list[str]
    benchmark: str | None = "SPY"

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, symbols: list[str]) -> list[str]:
        normalized = [symbol.upper().strip() for symbol in symbols if symbol.strip()]
        if not normalized:
            raise ValueError("universe.symbols must contain at least one symbol")
        if len(normalized) != len(set(normalized)):
            raise ValueError("universe.symbols must not contain duplicates")
        return normalized

    @field_validator("benchmark")
    @classmethod
    def normalize_benchmark(cls, benchmark: str | None) -> str | None:
        return benchmark.upper().strip() if benchmark else None


class DataConfig(BaseModel):
    source: Literal["yfinance"] = "yfinance"
    adjusted: bool = True
    bar_type: Literal["daily"] = "daily"  # weekly/monthly not yet supported end-to-end
    bar_close_utc_hour: int = Field(default=21, ge=0, le=23)
    cache_dir: str = ".cache/bars"
    cache_staleness_hours: int = Field(default=4, ge=0)
    batch_size: int = Field(default=50, ge=1)


class BacktestConfig(BaseModel):
    start_date: date
    end_date: date
    initial_cash: Decimal = Field(default=Decimal("100000.00"), gt=0)
    risk_free_rate: Decimal = Field(default=Decimal("0.05"), ge=0)
    audit_lookahead: bool = False
    holdout_start: date | None = None
    scored_start: date | None = None  # bars before this date are warmup-only (no fills)

    @model_validator(mode="after")
    def validate_date_range(self) -> "BacktestConfig":
        if self.start_date >= self.end_date:
            raise ValueError("backtest.start_date must be before backtest.end_date")
        if self.holdout_start is not None:
            if self.holdout_start <= self.start_date:
                raise ValueError("backtest.holdout_start must be after backtest.start_date")
            if self.holdout_start > self.end_date:
                raise ValueError("backtest.holdout_start must be ≤ backtest.end_date")
        if self.scored_start is not None:
            if self.scored_start <= self.start_date:
                raise ValueError("backtest.scored_start must be after backtest.start_date")
            if self.scored_start > self.end_date:
                raise ValueError("backtest.scored_start must be ≤ backtest.end_date")
        return self


class RiskRuleConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    rule: str

    @property
    def params(self) -> dict[str, Any]:
        data = self.model_dump()
        data.pop("rule", None)
        return data


class RiskConfig(BaseModel):
    rules: list[RiskRuleConfig]

    @field_validator("rules")
    @classmethod
    def validate_rules(cls, rules: list[RiskRuleConfig]) -> list[RiskRuleConfig]:
        if not rules:
            raise ValueError("risk.rules must contain at least one rule")
        for rule_config in rules:
            if rule_config.rule not in RULE_REGISTRY:
                known = ", ".join(sorted(RULE_REGISTRY))
                raise ValueError(f"unknown risk rule {rule_config.rule!r}; known rules: {known}")
            create_rule(rule_config.rule, **rule_config.params)
        return rules


class ExecutionConfig(BaseModel):
    broker: str = "PaperBroker"
    fill_at: Literal["next_open", "prev_close"] = "next_open"
    slippage_model: Literal["fixed_pct", "zero"] = "fixed_pct"
    slippage_pct: Decimal = Field(default=Decimal("0.0005"), ge=0)
    commission_model: Literal["per_share", "flat", "zero"] = "per_share"
    commission_per_share: Decimal = Field(default=Decimal("0.005"), ge=0)
    min_commission: Decimal = Field(default=Decimal("1.00"), ge=0)
    max_participation_pct: Decimal = Field(default=Decimal("0.025"), ge=0)


class StrategyConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    class_path: str = Field(alias="class")
    symbols: list[str]
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "class_path")
    @classmethod
    def require_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must be non-empty")
        return value

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, symbols: list[str]) -> list[str]:
        normalized = [symbol.upper().strip() for symbol in symbols if symbol.strip()]
        if not normalized:
            raise ValueError("strategy symbols must contain at least one symbol")
        if len(normalized) != len(set(normalized)):
            raise ValueError("strategy symbols must not contain duplicates")
        return normalized


class AppConfig(BaseModel):
    engine: EngineConfig = Field(default_factory=EngineConfig)
    universe: UniverseConfig
    data: DataConfig = Field(default_factory=DataConfig)
    backtest: BacktestConfig
    risk: RiskConfig
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    strategies: list[StrategyConfig]

    @model_validator(mode="after")
    def validate_cross_references(self) -> "AppConfig":
        universe = set(self.universe.symbols)
        strategy_ids = [strategy.id for strategy in self.strategies]
        if len(strategy_ids) != len(set(strategy_ids)):
            raise ValueError("strategies[*].id values must be unique")

        for strategy in self.strategies:
            unknown = sorted(set(strategy.symbols) - universe)
            if unknown:
                raise ValueError(
                    f"strategy {strategy.id!r} references symbols outside universe: {unknown}"
                )
        return self

    def validate_runtime_references(self) -> None:
        for strategy in self.strategies:
            resolve_dotted_path(strategy.class_path)

        broker_cls = resolve_broker_class(self.execution.broker)
        if not issubclass(broker_cls, AbstractBroker):
            raise TypeError(f"{broker_cls.__qualname__} must inherit from AbstractBroker")


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path or os.environ.get("ATM_CONFIG_PATH", "config/default.yaml"))
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    _apply_env_overrides(raw)
    config = AppConfig.model_validate(raw)
    config.validate_runtime_references()
    return config


def resolve_broker_class(name_or_path: str) -> type[AbstractBroker]:
    aliases = {
        "PaperBroker": "engine.execution.paper_broker.PaperBroker",
    }
    resolved = aliases.get(name_or_path, name_or_path)
    broker_cls = resolve_dotted_path(resolved)
    if not isinstance(broker_cls, type):
        raise TypeError(f"{resolved!r} did not resolve to a class")
    if not issubclass(broker_cls, AbstractBroker):
        raise TypeError(f"{resolved!r} is not an AbstractBroker subclass")
    return broker_cls


def resolve_dotted_path(path: str) -> type[Any]:
    module_name, _, attr_name = path.rpartition(".")
    if not module_name or not attr_name:
        raise ImportError(f"{path!r} is not a dotted import path")
    module = importlib.import_module(module_name)
    value = getattr(module, attr_name)
    if not isinstance(value, type):
        raise TypeError(f"{path!r} resolved to {type(value).__name__}, not a class")
    return value


def _apply_env_overrides(raw: dict[str, Any]) -> None:
    for env_key, env_value in os.environ.items():
        if not env_key.startswith("ATM_") or env_key == "ATM_CONFIG_PATH":
            continue
        path = env_key.removeprefix("ATM_").lower().split("__")
        _set_nested(raw, path, _parse_env_value(env_value))


def _set_nested(target: dict[str, Any] | list[Any], path: list[str], value: Any) -> None:
    current: dict[str, Any] | list[Any] = target
    for index, part in enumerate(path):
        is_last = index == len(path) - 1
        if isinstance(current, list):
            list_index = int(part)
            if is_last:
                current[list_index] = value
                return
            current = current[list_index]
            continue

        if is_last:
            current[part] = value
            return

        next_part = path[index + 1]
        if part not in current:
            current[part] = [] if next_part.isdigit() else {}
        current = current[part]


def _parse_env_value(value: str) -> Any:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"none", "null"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return Decimal(value)
    except Exception:
        return value
