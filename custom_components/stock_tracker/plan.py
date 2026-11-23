"""Savings plan (Sparplan) calculations - pure, no Home Assistant imports.

A savings plan buys for a fixed amount at regular intervals. Executions since
`start` are derived from historical daily closes: an execution scheduled on a
non-trading day is filled on the next trading day.
"""

from __future__ import annotations

from bisect import bisect_left
from calendar import monthrange
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from .calc import Position
from .const import (
    CONF_PLAN_ACTIVE,
    CONF_PLAN_AMOUNT,
    CONF_PLAN_DAY,
    CONF_PLAN_FEE_FIXED,
    CONF_PLAN_FEE_INCLUDED,
    CONF_PLAN_FEE_PERCENT,
    CONF_PLAN_INTERVAL,
    CONF_PLAN_START,
)


class PlanInterval(StrEnum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    BIMONTHLY = "bimonthly"
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    ANNUAL = "annual"


_DAYS = {PlanInterval.WEEKLY: 7, PlanInterval.BIWEEKLY: 14}
_MONTHS = {
    PlanInterval.MONTHLY: 1,
    PlanInterval.BIMONTHLY: 2,
    PlanInterval.QUARTERLY: 3,
    PlanInterval.SEMIANNUAL: 6,
    PlanInterval.ANNUAL: 12,
}


@dataclass(frozen=True, slots=True)
class SavingsPlan:
    """Plan configuration (amounts in portfolio currency)."""

    amount: float
    interval: PlanInterval
    day: int  # day of month (1-31, clamped); ignored for weekly plans
    start: date  # first possible execution
    fee_fixed: float = 0.0
    fee_percent: float = 0.0
    fee_included: bool = True  # True: fee is deducted from the amount
    active: bool = True

    def fee(self) -> float:
        return round(self.fee_fixed + self.amount * self.fee_percent / 100, 2)

    def schedule(self) -> Iterator[date]:
        """Endless sequence of scheduled execution dates."""
        if self.interval in _DAYS:
            current = self.start
            while True:
                yield current
                current += timedelta(days=_DAYS[self.interval])

        step = _MONTHS[self.interval]
        year, month = self.start.year, self.start.month
        first = _clamped(year, month, self.day)
        if first < self.start:
            year, month = _add_months(year, month, 1)
        while True:
            yield _clamped(year, month, self.day)
            year, month = _add_months(year, month, step)


def _clamped(year: int, month: int, day: int) -> date:
    return date(year, month, min(max(day, 1), monthrange(year, month)[1]))


def _add_months(year: int, month: int, months: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + months
    return index // 12, index % 12 + 1


@dataclass(frozen=True, slots=True)
class Execution:
    """One (calculated) savings plan purchase."""

    scheduled: date
    traded: date
    price: float  # per share, portfolio currency
    quantity: float
    fee: float

    @property
    def cost(self) -> float:
        """Purchase value without fee."""
        return round(self.quantity * self.price, 2)


@dataclass(frozen=True, slots=True)
class PlanResult:
    """All executions since the plan start."""

    executions: list[Execution] = field(default_factory=list)
    next_date: date | None = None

    @property
    def quantity(self) -> float:
        return round(sum(e.quantity for e in self.executions), 6)

    @property
    def cost(self) -> float:
        return round(sum(e.cost for e in self.executions), 2)

    @property
    def fees(self) -> float:
        return round(sum(e.fee for e in self.executions), 2)

    @property
    def invested(self) -> float:
        """Total money spent (purchase value + fees)."""
        return round(self.cost + self.fees, 2)


type History = list[tuple[date, float]]  # sorted by date


def _first_on_or_after(history: History, day: date) -> tuple[date, float] | None:
    idx = bisect_left(history, (day, float("-inf")))
    return history[idx] if idx < len(history) else None


def evaluate_plan(
    plan: SavingsPlan,
    prices: History,
    fx_rates: History | None,
    today: date,
    divisor: int = 1,
) -> PlanResult:
    """Derive executions from historical closes.

    prices: daily closes in quote currency (minor unit, e.g. GBp)
    fx_rates: daily rates quote currency -> portfolio currency, None if equal
    """
    if not plan.active or plan.amount <= 0:
        return PlanResult()

    executions: list[Execution] = []
    for scheduled in plan.schedule():
        if scheduled > today:
            return PlanResult(executions, scheduled)
        bar = _first_on_or_after(prices, scheduled)
        if bar is None:  # not traded yet (weekend / before market open)
            return PlanResult(executions, max(scheduled, today))
        traded, close = bar
        rate = 1.0
        if fx_rates is not None:
            fx_bar = _first_on_or_after(fx_rates, traded) or (
                fx_rates[-1] if fx_rates else None
            )
            if fx_bar is None:
                return PlanResult(executions, max(scheduled, today))
            rate = fx_bar[1]
        price = close / divisor * rate
        if price <= 0:
            continue

        fee = plan.fee()
        invest = plan.amount - fee if plan.fee_included else plan.amount
        if invest <= 0:
            continue
        executions.append(
            Execution(
                scheduled=scheduled,
                traded=traded,
                price=round(price, 4),
                quantity=round(invest / price, 6),
                fee=fee,
            )
        )
    raise AssertionError("unreachable")  # schedule() is endless


def combine(base: Position, plan: PlanResult) -> Position:
    """Position including the savings plan executions."""
    if not plan.executions:
        return base
    quantity = round(base.quantity + plan.quantity, 6)
    cost = base.quantity * base.purchase_price + plan.cost
    return Position(
        symbol=base.symbol,
        quantity=quantity,
        purchase_price=round(cost / quantity, 6) if quantity else 0.0,
        purchase_fees=round(base.purchase_fees + plan.fees, 2),
        asset_type=base.asset_type,
        advance_taxed=base.advance_taxed,
    )


def next_start(plan: SavingsPlan, result: PlanResult) -> date:
    """Start date for a plan whose executions have been folded into the base."""
    if not result.executions:
        return plan.start
    last = result.executions[-1].scheduled
    return next(d for d in plan.schedule() if d > last)


def plan_from_data(data: Mapping[str, Any]) -> SavingsPlan | None:
    """Build a plan from subentry data; None if no plan is configured."""
    amount = float(data.get(CONF_PLAN_AMOUNT) or 0)
    start = data.get(CONF_PLAN_START)
    if amount <= 0 or not start:
        return None
    return SavingsPlan(
        amount=amount,
        interval=PlanInterval(data.get(CONF_PLAN_INTERVAL, PlanInterval.MONTHLY)),
        day=int(data.get(CONF_PLAN_DAY) or 1),
        start=date.fromisoformat(str(start)),
        fee_fixed=float(data.get(CONF_PLAN_FEE_FIXED) or 0),
        fee_percent=float(data.get(CONF_PLAN_FEE_PERCENT) or 0),
        fee_included=bool(data.get(CONF_PLAN_FEE_INCLUDED, True)),
        active=bool(data.get(CONF_PLAN_ACTIVE, True)),
    )
