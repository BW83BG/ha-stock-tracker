"""End-to-end sensor tests with mocked quotes."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.stock_tracker.const import DEFAULT_OPTIONS, DOMAIN

OPTIONS = {
    **DEFAULT_OPTIONS,
    "sell_fee_fixed": 4.9,
    "sell_fee_percent": 0.25,
    "sell_fee_min": 9.9,
    "allowance_remaining": 0.0,
}


def _sub(symbol, **position):
    return {
        "data": {"symbol": symbol, "name": symbol, **position},
        "subentry_type": "stock",
        "title": symbol,
        "unique_id": symbol,
    }


@pytest.fixture
async def setup(hass: HomeAssistant, mock_api):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options=OPTIONS,
        subentries_data=[
            _sub("SAP.DE", asset_type="stock", quantity=10, purchase_price=100, purchase_fees=5),
            _sub("AAPL", asset_type="stock", quantity=4, purchase_price=200, purchase_fees=0),
            _sub("VOD.L", asset_type="stock", quantity=0),  # watch only
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _state(hass, unique_id):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id, unique_id
    return hass.states.get(entity_id)


async def test_quote_sensors(hass: HomeAssistant, setup) -> None:
    price = _state(hass, "SAP.DE_price")
    assert float(price.state) == 150.0
    assert price.attributes["unit_of_measurement"] == "EUR"
    assert price.attributes["previous_close"] == 148.0
    assert float(_state(hass, "SAP.DE_change").state) == 2.0
    assert float(_state(hass, "SAP.DE_change_percent").state) == pytest.approx(1.35)
    assert _state(hass, "SAP.DE_market_state").state == "unknown"


async def test_watch_only_has_no_position_sensors(hass: HomeAssistant, setup) -> None:
    assert _state(hass, "VOD.L_price").attributes["unit_of_measurement"] == "GBp"
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "VOD.L_gain") is None


async def test_position_eur(hass: HomeAssistant, setup) -> None:
    assert float(_state(hass, "SAP.DE_market_value").state) == 1500
    assert float(_state(hass, "SAP.DE_cost_basis").state) == 1005
    assert float(_state(hass, "SAP.DE_gain").state) == pytest.approx(485.10)
    tax = _state(hass, "SAP.DE_tax")
    assert float(tax.state) == pytest.approx(127.95, abs=0.02)
    assert tax.attributes["scenario"] == "only_this_position"
    assert float(_state(hass, "SAP.DE_net_proceeds").state) == pytest.approx(
        1500 - 9.9 - float(tax.state), abs=0.01
    )


async def test_position_foreign_currency(hass: HomeAssistant, setup) -> None:
    value = _state(hass, "AAPL_market_value")
    assert float(value.state) == pytest.approx(900)  # 4 * 250 USD * 0.9
    assert value.attributes["unit_of_measurement"] == "EUR"
    assert value.attributes["fx_rate"] == 0.9
    # 900 - 9.90 fee - 800 cost
    assert float(_state(hass, "AAPL_gain").state) == pytest.approx(90.10)


async def test_portfolio(hass: HomeAssistant, setup) -> None:
    entry_id = setup.entry_id
    value = _state(hass, f"{entry_id}_portfolio_market_value")
    assert float(value.state) == pytest.approx(2400)
    assert value.attributes["positions"] == 2
    gain = float(_state(hass, f"{entry_id}_portfolio_gain").state)
    assert gain == pytest.approx(485.10 + 90.10)
    tax = float(_state(hass, f"{entry_id}_portfolio_tax").state)
    assert tax == pytest.approx(575.20 * 0.26375, abs=0.02)
    assert float(_state(hass, f"{entry_id}_portfolio_gain_after_tax").state) == pytest.approx(
        gain - tax, abs=0.01
    )


@pytest.fixture
async def setup_plan(hass: HomeAssistant, mock_api, mock_history, freezer):
    freezer.move_to("2026-11-23 12:00:00+01:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options=DEFAULT_OPTIONS,
        subentries_data=[
            _sub("SAP.DE", asset_type="stock", quantity=10, purchase_price=80, purchase_fees=5,
                 plan_amount=100, plan_interval="monthly", plan_day=1,
                 plan_start="2026-09-01", plan_fee_fixed=1.0, plan_fee_included=True),
            _sub("AAPL", quantity=0, plan_amount=90, plan_interval="monthly", plan_day=15,
                 plan_start="2026-10-01", plan_fee_included=False),
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_plan_sensors(hass: HomeAssistant, setup_plan) -> None:
    # SAP: Sep 1, Oct 1, Nov 2 (Nov 1 = Sunday) at 100 EUR, 99 EUR invested each
    assert float(_state(hass, "SAP.DE_plan_quantity").state) == pytest.approx(2.97)
    invested = _state(hass, "SAP.DE_plan_invested")
    assert float(invested.state) == pytest.approx(300)
    assert invested.attributes["executions"] == 3
    assert invested.attributes["last_executions"][-1]["date"] == "2026-11-02"
    assert _state(hass, "SAP.DE_plan_next_execution").state == "2026-12-01"

    assert float(_state(hass, "SAP.DE_quantity").state) == pytest.approx(12.97)
    # cost basis: 10*80 + 3*99 + fees (5 + 3*1)
    assert float(_state(hass, "SAP.DE_cost_basis").state) == pytest.approx(800 + 297 + 8)


async def test_plan_foreign_currency(hass: HomeAssistant, setup_plan) -> None:
    # AAPL: Oct 15, Nov 16 (Nov 15 = Sunday), 90 EUR / (200 USD * 0.9) = 0.5 shares each
    assert float(_state(hass, "AAPL_plan_quantity").state) == pytest.approx(1.0)
    assert float(_state(hass, "AAPL_quantity").state) == pytest.approx(1.0)
    assert float(_state(hass, "AAPL_cost_basis").state) == pytest.approx(180)


def _suggested(result) -> dict:
    """Pre-filled values of a form (sections as nested dicts)."""
    out = {}
    for key, val in result["data_schema"].schema.items():
        if hasattr(val, "schema"):
            out[str(key)] = {
                str(k): (k.description or {}).get("suggested_value") for k in val.schema.schema
            }
        else:
            out[str(key)] = (key.description or {}).get("suggested_value")
    return out


def _clean(values: dict) -> dict:
    """Drop empty values, keep sections."""
    return {
        k: _clean(v) if isinstance(v, dict) else v
        for k, v in values.items()
        if v is not None
    }


async def test_reconfigure_folds_plan(hass: HomeAssistant, setup_plan) -> None:
    entry = setup_plan
    sub_id = next(s.subentry_id for s in entry.subentries.values() if s.unique_id == "SAP.DE")
    result = await entry.start_subentry_reconfigure_flow(hass, sub_id)
    assert result["description_placeholders"]["executions"] == "3"

    # read the pre-filled (folded) values back from the form schema
    suggested = {}
    for key, val in result["data_schema"].schema.items():
        if hasattr(val, "schema"):
            suggested[str(key)] = {
                str(k): (k.description or {}).get("suggested_value") for k in val.schema.schema
            }
        else:
            suggested[str(key)] = (key.description or {}).get("suggested_value")
    assert suggested["quantity"] == pytest.approx(12.97)
    assert suggested["purchase_fees"] == pytest.approx(8)
    assert suggested["savings_plan"]["plan_start"] == "2026-12-01"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _clean(suggested)
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    data = entry.subentries[sub_id].data
    assert data["quantity"] == pytest.approx(12.97)
    assert data["plan_start"] == "2026-12-01"
    # after reload: no plan executions since Dec 1, total unchanged
    assert float(_state(hass, "SAP.DE_quantity").state) == pytest.approx(12.97)
    assert float(_state(hass, "SAP.DE_plan_quantity").state) == 0


@pytest.fixture
async def setup_income(hass: HomeAssistant, mock_api, freezer):
    freezer.move_to("2026-11-23 12:00:00+01:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options=DEFAULT_OPTIONS,  # allowance 1000 -> no tax on sale
        subentries_data=[
            # distributing ETF, held 100 shares since 2026-01-01, plus a plan
            _sub("VUSA.DE", asset_type="equity_fund", quantity=100, purchase_price=80,
                 purchase_fees=0, distribution="distributing",
                 dividends_since="2026-01-01", dividends_received=50.0,
                 plan_amount=1000, plan_interval="monthly", plan_day=1,
                 plan_start="2026-05-01"),
            # onvista ETF, accumulating
            {
                "data": {"symbol": "IE0006WW1TQ4@GER", "name": "World ex USA",
                         "source": "onvista", "onvista_type": "FUND",
                         "onvista_id": "268395859", "onvista_notation": 454412888,
                         "isin": "IE0006WW1TQ4", "quantity": 10, "purchase_price": 30,
                         "distribution": "accumulating", "asset_type": "equity_fund",
                         "plan_amount": 380, "plan_interval": "monthly", "plan_day": 15,
                         "plan_start": "2026-10-01"},
                "subentry_type": "stock",
                "title": "World ex USA",
                "unique_id": "IE0006WW1TQ4@GER",
            },
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_dividends_and_yield(hass: HomeAssistant, setup_income) -> None:
    # 12-month yield: 4 x 0.25 / 100 = 1 %
    assert float(_state(hass, "VUSA.DE_dividend_yield").state) == pytest.approx(1.0)

    # since 2026-01-01, plan buys 10 shares on the 1st from May:
    # Mar 19: 100 | Jun 18: 100 + May + Jun = 120 | Sep 17: 100 + May..Sep = 150
    div = _state(hass, "VUSA.DE_dividends")
    payments = div.attributes["last_payments"]
    assert [p["ex_date"] for p in payments] == ["2026-03-19", "2026-06-18", "2026-09-17"]
    assert [p["shares"] for p in payments] == [100, 120, 150]
    gross = 0.25 * (100 + 120 + 150)
    assert div.attributes["calculated_gross"] == pytest.approx(gross)
    # tax: 70 % taxable (equity fund) x 26.375 %
    net = gross - gross * 0.7 * 0.26375
    assert div.attributes["calculated_net"] == pytest.approx(net, abs=0.03)
    assert float(div.state) == pytest.approx(50 + net, abs=0.03)

    total = float(_state(hass, "VUSA.DE_total_return").state)
    after_tax = float(_state(hass, "VUSA.DE_gain_after_tax").state)
    assert total == pytest.approx(after_tax + float(div.state), abs=0.01)

    # portfolio includes income sensors
    entry_id = setup_income.entry_id
    assert float(_state(hass, f"{entry_id}_portfolio_dividends").state) == pytest.approx(
        float(div.state), abs=0.01
    )


async def test_onvista_instrument(hass: HomeAssistant, setup_income) -> None:
    price = _state(hass, "IE0006WW1TQ4@GER_price")
    assert float(price.state) == 40.0
    # plan: Oct 15, Nov 16 at 38 EUR -> 10 shares each
    assert float(_state(hass, "IE0006WW1TQ4@GER_plan_quantity").state) == pytest.approx(20)
    assert float(_state(hass, "IE0006WW1TQ4@GER_quantity").state) == pytest.approx(30)
    # accumulating / onvista: no income sensors
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, "IE0006WW1TQ4@GER_dividends") is None
    assert reg.async_get_entity_id("sensor", DOMAIN, "IE0006WW1TQ4@GER_dividend_yield") is None


async def test_reconfigure_folds_dividends(hass: HomeAssistant, setup_income) -> None:
    entry = setup_income
    sub_id = next(s.subentry_id for s in entry.subentries.values() if s.unique_id == "VUSA.DE")
    before = float(_state(hass, "VUSA.DE_dividends").state)

    result = await entry.start_subentry_reconfigure_flow(hass, sub_id)
    assert result["description_placeholders"]["payments"] == "3"
    suggested = _suggested(result)
    assert suggested["income"]["dividends_received"] == pytest.approx(before)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _clean(suggested)
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    data = entry.subentries[sub_id].data
    assert data["dividends_since"] == "2026-11-23"
    # after reload nothing is counted twice
    assert float(_state(hass, "VUSA.DE_dividends").state) == pytest.approx(before)
