"""Tests for the pure fee / tax / portfolio calculations."""

from datetime import UTC, datetime

import pytest

from custom_components.stock_tracker.calc import (
    AssetType,
    FeeSettings,
    MarketState,
    Position,
    TaxSettings,
    TradingPeriods,
    calculate_tax,
    evaluate_portfolio,
    evaluate_position,
    normalize_currency,
)

FEES = FeeSettings(fixed=4.90, percent=0.25, minimum=9.90)
NO_ALLOWANCE = TaxSettings()


def test_tax_rates():
    assert TaxSettings().total_rate == pytest.approx(0.26375)
    assert TaxSettings(church_tax_rate=0.09).total_rate == pytest.approx(0.279951, abs=1e-6)
    assert TaxSettings(church_tax_rate=0.08).total_rate == pytest.approx(0.278186, abs=1e-6)


def test_tax_breakdown_with_church_tax():
    tax = calculate_tax(1000, 0, TaxSettings(church_tax_rate=0.09))
    assert tax.capital_gains_tax == pytest.approx(244.50)
    assert tax.solidarity == pytest.approx(13.45)
    assert tax.church == pytest.approx(22.00)
    assert tax.total == pytest.approx(279.95)


def test_sell_fee():
    assert FEES.sell_fee(1000) == pytest.approx(9.90)  # minimum applies
    assert FEES.sell_fee(10000) == pytest.approx(29.90)  # 4.90 + 25.00
    assert FEES.sell_fee(0) == 0


def test_position_gain_and_tax():
    pos = Position("SAP.DE", quantity=10, purchase_price=100, purchase_fees=5)
    r = evaluate_position(pos, price=150, change=2, fx_rate=1.0, fees=FEES, tax=NO_ALLOWANCE)
    assert r.market_value == 1500
    assert r.cost_basis == 1005
    assert r.sell_fee == pytest.approx(9.90)
    assert r.gain == pytest.approx(485.10)
    assert r.gain_percent == pytest.approx(48.27)
    assert r.tax.total == pytest.approx(485.10 * 0.26375, abs=0.02)
    assert r.gain_after_tax == pytest.approx(r.gain - r.tax.total)
    assert r.net_proceeds == pytest.approx(1500 - 9.90 - r.tax.total)
    assert r.day_change == 20


def test_allowance_covers_gain():
    pos = Position("SAP.DE", 10, 100, 5)
    r = evaluate_position(pos, 150, 0, 1.0, FEES, TaxSettings(allowance_remaining=1000))
    assert r.tax.total == 0
    assert r.tax.allowance_used == pytest.approx(485.10)
    assert r.gain_after_tax == r.gain


def test_partial_exemption_equity_fund():
    pos = Position("EUNL.DE", 10, 100, 5, AssetType.EQUITY_FUND)
    r = evaluate_position(pos, 150, 0, 1.0, FEES, NO_ALLOWANCE)
    assert r.taxable_gain == pytest.approx(485.10 * 0.7, abs=0.01)


def test_loss_is_not_taxed():
    pos = Position("SAP.DE", 10, 200, 5)
    r = evaluate_position(pos, 150, 0, 1.0, FEES, NO_ALLOWANCE)
    assert r.gain < 0
    assert r.tax.total == 0
    assert r.gain_after_tax == r.gain


def test_fx_conversion():
    pos = Position("AAPL", 10, 200, 0)
    r = evaluate_position(pos, 250, 5, 0.9, FeeSettings(), NO_ALLOWANCE)
    assert r.price == pytest.approx(225)
    assert r.market_value == pytest.approx(2250)
    assert r.day_change == pytest.approx(45)


def test_netting_stock_loss_does_not_offset_fund_gain():
    assert calculate_tax(-200, 300, NO_ALLOWANCE).taxable == 300


def test_netting_fund_loss_offsets_stock_gain():
    assert calculate_tax(300, -200, NO_ALLOWANCE).taxable == 100


def test_loss_pots():
    s = TaxSettings(stock_loss_pot=100, general_loss_pot=50)
    assert calculate_tax(300, 0, s).taxable == 150
    # stock loss pot cannot be used against fund gains
    assert calculate_tax(0, 300, s).taxable == 250


def test_portfolio_nets_positions():
    fees = FeeSettings()
    winner = evaluate_position(Position("A", 10, 100, 0), 150, 0, 1.0, fees, NO_ALLOWANCE)
    loser = evaluate_position(Position("B", 10, 100, 0), 80, 0, 1.0, fees, NO_ALLOWANCE)
    pf = evaluate_portfolio([winner, loser], TaxSettings(allowance_remaining=100))
    assert pf.gain == 300  # 500 - 200
    assert pf.tax.taxable == 200  # 300 - 100 allowance
    assert pf.tax.total == pytest.approx(52.75)
    assert pf.market_value == 2300
    assert pf.net_proceeds == pytest.approx(2300 - 52.75)


def test_normalize_currency():
    assert normalize_currency("GBp") == ("GBP", 100)
    assert normalize_currency("EUR") == ("EUR", 1)
    assert normalize_currency(None) == (None, 1)


@pytest.mark.parametrize(
    ("hour", "state"),
    [(8, MarketState.PRE), (10, MarketState.OPEN), (18, MarketState.POST), (22, MarketState.CLOSED)],
)
def test_market_state(hour, state):
    day = datetime(2026, 11, 23, tzinfo=UTC).timestamp()
    periods = TradingPeriods(
        pre_start=int(day + 7 * 3600),
        regular_start=int(day + 9 * 3600),
        regular_end=int(day + 17 * 3600),
        post_end=int(day + 20 * 3600),
    )
    assert periods.state(datetime(2026, 11, 23, hour, tzinfo=UTC)) is state
    assert TradingPeriods().state(datetime.now(UTC)) is None


def test_advance_lump_sum_reduces_taxable_gain():
    pos = Position("EUNL.DE", 10, 100, 0, AssetType.EQUITY_FUND, advance_taxed=100)
    r = evaluate_position(pos, 150, 0, 1.0, FeeSettings(), NO_ALLOWANCE)
    assert r.gain == 500
    assert r.taxable_gain == pytest.approx((500 - 100) * 0.7)


def test_total_return_includes_dividends():
    pos = Position("X", 10, 100, 0)
    r = evaluate_position(pos, 110, 0, 1.0, FeeSettings(), TaxSettings(allowance_remaining=1000), dividends=25)
    assert r.gain_after_tax == 100
    assert r.total_return == 125
    pf = evaluate_portfolio([r], TaxSettings(allowance_remaining=1000))
    assert pf.dividends == 25
    assert pf.total_return == 125
