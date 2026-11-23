"""Tests for the config flow, options flow and stock subentries."""

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.stock_tracker.const import DEFAULT_OPTIONS, DOMAIN

SECTIONED = {
    "general": {"scan_interval": 600, "currency": "EUR"},
    "fees": {"sell_fee_fixed": 4.9, "sell_fee_percent": 0.25, "sell_fee_min": 9.9},
    "tax": {
        "church_tax": "9",
        "allowance_remaining": 500,
        "stock_loss_pot": 0,
        "general_loss_pot": 0,
    },
}


async def test_user_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], SECTIONED)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["scan_interval"] == 600
    assert result["options"]["church_tax"] == "9"
    assert result["options"]["sell_fee_min"] == 9.9


async def test_single_instance(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant, mock_api) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=DEFAULT_OPTIONS)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], SECTIONED)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["allowance_remaining"] == 500


async def _search(hass, entry, query):
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "stock"), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"query": query}
    )


async def _add_stock(hass, entry, user_input):
    """Search for the symbol and submit the details form."""
    user_input = dict(user_input)
    result = await _search(hass, entry, user_input.pop("symbol"))
    if result["type"] is not FlowResultType.FORM or result["step_id"] != "details":
        return result
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"income": {}, "savings_plan": {}, **user_input}
    )


async def test_add_stock_subentry(hass: HomeAssistant, mock_api) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=DEFAULT_OPTIONS)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await _add_stock(
        hass,
        entry,
        {"symbol": " sap.de ", "asset_type": "stock", "quantity": 10,
         "purchase_price": 100, "purchase_fees": 5, "savings_plan": {}},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    sub = next(iter(entry.subentries.values()))
    assert sub.unique_id == "SAP.DE"
    assert sub.title == "SAP SE (SAP.DE)"
    assert sub.data["quantity"] == 10.0

    # entities were created after the automatic reload
    assert hass.states.async_entity_ids_count("sensor") > 0

    # duplicate
    result = await _add_stock(hass, entry, {"symbol": "SAP.DE", "savings_plan": {}})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_add_unknown_symbol(hass: HomeAssistant, mock_api) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=DEFAULT_OPTIONS)
    entry.add_to_hass(hass)

    result = await _search(hass, entry, "NOPE")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "symbol_not_found"}


async def test_reconfigure_stock(hass: HomeAssistant, mock_api) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options=DEFAULT_OPTIONS,
        subentries_data=[
            {"data": {"symbol": "SAP.DE", "name": "SAP"}, "subentry_type": "stock",
             "title": "SAP (SAP.DE)", "unique_id": "SAP.DE"}
        ],
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sub_id = next(iter(entry.subentries))

    result = await entry.start_subentry_reconfigure_flow(hass, sub_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "SAP Depot", "asset_type": "stock", "quantity": 3,
         "purchase_price": 120, "purchase_fees": 1, "savings_plan": {}, "income": {}},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    sub = entry.subentries[sub_id]
    assert sub.title == "SAP Depot (SAP.DE)"
    assert sub.data["quantity"] == 3.0
    assert sub.data["symbol"] == "SAP.DE"


async def test_add_stock_with_plan(hass: HomeAssistant, mock_api) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=DEFAULT_OPTIONS)
    entry.add_to_hass(hass)

    plan = {"plan_amount": 100, "plan_interval": "monthly", "plan_day": 1,
            "plan_fee_percent": 1.5, "plan_fee_included": True}
    # start date missing
    result = await _add_stock(hass, entry, {"symbol": "SAP.DE", "savings_plan": plan})
    assert result["errors"] == {"base": "plan_start_required"}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"income": {}, "savings_plan": {**plan, "plan_start": "2026-09-01"}},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = next(iter(entry.subentries.values())).data
    assert data["plan_amount"] == 100.0
    assert data["plan_start"] == "2026-09-01"
    assert data["plan_fee_included"] is True
    assert data["quantity"] == 0.0


async def test_search_isin_offers_yahoo_and_onvista(hass: HomeAssistant, mock_api) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=DEFAULT_OPTIONS)
    entry.add_to_hass(hass)

    result = await _search(hass, entry, "IE0006WW1TQ4")
    assert result["step_id"] == "select"
    options = result["data_schema"].schema["candidate"].config["options"]
    labels = [o["label"] for o in options]
    assert labels[0].startswith("Xetra · Xtrackers MSCI World ex USA · 39.49 EUR (onvista)")
    assert len(labels) == 2

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"candidate": "0"}
    )
    assert result["step_id"] == "details"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"quantity": 10, "purchase_price": 35, "income": {"distribution": "accumulating",
         "advance_lump_sum_taxed": 12.5}, "savings_plan": {}},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    sub = next(iter(entry.subentries.values()))
    assert sub.unique_id == "IE0006WW1TQ4@GER"
    assert sub.data["source"] == "onvista"
    assert sub.data["onvista_notation"] == 454412888
    assert sub.data["onvista_type"] == "FUND"
    assert sub.data["advance_lump_sum_taxed"] == 12.5
    assert sub.title == "Xtrackers MSCI World ex USA (IE0006WW1TQ4@GER)"


async def test_search_by_yahoo_isin(hass: HomeAssistant, mock_api) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options=DEFAULT_OPTIONS)
    entry.add_to_hass(hass)
    result = await _search(hass, entry, "IE00B4L5Y983")
    assert result["step_id"] == "select"
    labels = [o["label"] for o in result["data_schema"].schema["candidate"].config["options"]]
    assert labels == ["IWDA.L · iShares Core MSCI World · LSE · ETF (Yahoo)"]
