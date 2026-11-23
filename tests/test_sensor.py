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
