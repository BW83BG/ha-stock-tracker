"""Tests for savings plan calculations."""

from datetime import date
from itertools import islice

import pytest

from custom_components.stock_tracker.calc import Position
from custom_components.stock_tracker.plan import (
    PlanInterval,
    SavingsPlan,
    combine,
    evaluate_plan,
    next_start,
    plan_from_data,
)

from .conftest import make_history


def _plan(**kw) -> SavingsPlan:
    args = dict(amount=100, interval=PlanInterval.MONTHLY, day=1, start=date(2026, 8, 1))
    args.update(kw)
    return SavingsPlan(**args)


def test_schedule_monthly_clamps_month_end():
    plan = _plan(day=31, start=date(2026, 1, 15))
    assert list(islice(plan.schedule(), 3)) == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31),
    ]


def test_schedule_quarterly_starts_next_month():
    plan = _plan(interval=PlanInterval.QUARTERLY, day=1, start=date(2026, 1, 20))
    assert list(islice(plan.schedule(), 3)) == [
        date(2026, 2, 1), date(2026, 5, 1), date(2026, 8, 1),
    ]


def test_schedule_weekly():
    plan = _plan(interval=PlanInterval.BIWEEKLY, start=date(2026, 11, 2))
    assert list(islice(plan.schedule(), 3)) == [
        date(2026, 11, 2), date(2026, 11, 16), date(2026, 11, 30),
    ]


def test_weekend_execution_moves_to_next_trading_day():
    # 2026-08-01 is a Saturday
    history = make_history(date(2026, 8, 1), date(2026, 8, 20), 50.0)
    result = evaluate_plan(_plan(), history, None, today=date(2026, 8, 20))
    assert len(result.executions) == 1
    assert result.executions[0].traded == date(2026, 8, 3)
    assert result.next_date == date(2026, 9, 1)


def test_fee_included_vs_on_top():
    history = make_history(date(2026, 8, 1), date(2026, 8, 20), 50.0)
    today = date(2026, 8, 20)
    incl = evaluate_plan(_plan(fee_percent=1.5), history, None, today)
    assert incl.executions[0].fee == 1.5
    assert incl.quantity == pytest.approx(98.5 / 50)
    assert incl.invested == pytest.approx(100)

    on_top = evaluate_plan(_plan(fee_fixed=1.0, fee_included=False), history, None, today)
    assert on_top.quantity == pytest.approx(2.0)
    assert on_top.invested == pytest.approx(101)


def test_currency_conversion_and_minor_units():
    history = make_history(date(2026, 8, 1), date(2026, 8, 20), 5000.0)  # pence
    fx = make_history(date(2026, 8, 1), date(2026, 8, 20), 1.2)
    result = evaluate_plan(_plan(), history, fx, date(2026, 8, 20), divisor=100)
    assert result.executions[0].price == pytest.approx(60.0)  # 50 GBP * 1.2


def test_pending_execution_today_without_price():
    history = make_history(date(2026, 8, 1), date(2026, 8, 31), 50.0)
    # 2026-09-01 is scheduled; no bar for today yet
    result = evaluate_plan(_plan(), history, None, today=date(2026, 9, 1))
    assert len(result.executions) == 1
    assert result.next_date == date(2026, 9, 1)


def test_future_start_and_inactive():
    assert evaluate_plan(_plan(start=date(2027, 1, 1)), [], None, date(2026, 11, 23)).next_date == date(2027, 1, 1)
    assert evaluate_plan(_plan(active=False), [], None, date(2026, 11, 23)).executions == []


def test_combine_and_next_start():
    history = make_history(date(2026, 8, 1), date(2026, 10, 20), 50.0)
    plan = _plan()
    result = evaluate_plan(plan, history, None, date(2026, 10, 20))
    assert len(result.executions) == 3  # Aug, Sep, Oct

    base = Position("X", quantity=10, purchase_price=40, purchase_fees=5)
    total = combine(base, result)
    assert total.quantity == pytest.approx(16)
    assert total.purchase_price == pytest.approx((400 + 300) / 16)
    assert total.purchase_fees == 5
    assert next_start(plan, result) == date(2026, 11, 1)


def test_plan_from_data():
    assert plan_from_data({"plan_amount": 0}) is None
    assert plan_from_data({"plan_amount": 50}) is None  # no start
    plan = plan_from_data({"plan_amount": 50, "plan_start": "2026-01-01", "plan_interval": "quarterly"})
    assert plan.interval is PlanInterval.QUARTERLY
    assert plan.fee_included is True
