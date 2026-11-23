"""Config flow for the Aktienkurse integration.

* The main config entry holds global settings (interval, currency, sell fees,
  tax situation). They are stored as options and editable via "Configure".
* Every tracked stock is a config *subentry* ("Aktie hinzufügen") holding the
  instrument (Yahoo ticker or onvista venue, found by ticker/ISIN/WKN/name),
  optionally the position (quantity, purchase price, fees), a savings plan
  with its own fees and the income settings (distributing/accumulating).
* When a stock is reconfigured, savings plan executions calculated so far are
  folded into the manual position and the plan start moves forward; likewise
  calculated distributions are added to the manual total. This keeps the
  history stable when the configuration changes.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.util import dt as dt_util
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .calc import AssetType
from .coordinator import stock_config_from_data
from .const import (
    ACCUMULATING,
    CHURCH_TAX_OPTIONS,
    CONF_ADVANCE_TAXED,
    CONF_CANDIDATE,
    CONF_DISTRIBUTION,
    CONF_DIVIDENDS_RECEIVED,
    CONF_DIVIDENDS_SINCE,
    CONF_QUERY,
    DISTRIBUTING,
    INCOME_KEYS,
    SECTION_INCOME,
    SOURCE_YAHOO,
    CONF_ALLOWANCE,
    CONF_ASSET_TYPE,
    CONF_CHURCH_TAX,
    CONF_CURRENCY,
    CONF_GENERAL_LOSS_POT,
    CONF_PLAN_ACTIVE,
    CONF_PLAN_AMOUNT,
    CONF_PLAN_DAY,
    CONF_PLAN_FEE_FIXED,
    CONF_PLAN_FEE_INCLUDED,
    CONF_PLAN_FEE_PERCENT,
    CONF_PLAN_INTERVAL,
    CONF_PLAN_START,
    CONF_PURCHASE_FEES,
    CONF_PURCHASE_PRICE,
    CONF_QUANTITY,
    CONF_SCAN_INTERVAL,
    CONF_SELL_FEE_FIXED,
    CONF_SELL_FEE_MIN,
    CONF_SELL_FEE_PERCENT,
    CONF_STOCK_LOSS_POT,
    CONF_SYMBOL,
    DEFAULT_NAME,
    DEFAULT_OPTIONS,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    PLAN_KEYS,
    SECTION_PLAN,
    SUBENTRY_TYPE_STOCK,
)
from .plan import PlanInterval, combine, next_start, plan_from_data
from .sources import Candidate, MarketData, instrument_data

SECTION_GENERAL = "general"
SECTION_FEES = "fees"
SECTION_TAX = "tax"

CURRENCIES = ["EUR", "USD", "CHF", "GBP", "JPY", "CAD", "AUD", "SEK", "NOK", "DKK"]


def _number(unit: str | None = None, step: float | str = 0.01, max_: float = 1e9):
    config = NumberSelectorConfig(
        min=0, max=max_, step=step, mode=NumberSelectorMode.BOX
    )
    if unit:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    """Global settings grouped into sections."""
    o = {**DEFAULT_OPTIONS, **current}
    cur = o[CONF_CURRENCY]
    return vol.Schema(
        {
            vol.Required(SECTION_GENERAL): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_SCAN_INTERVAL, default=o[CONF_SCAN_INTERVAL]
                        ): NumberSelector(
                            NumberSelectorConfig(
                                min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL,
                                step=1, unit_of_measurement="s",
                                mode=NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Required(CONF_CURRENCY, default=cur): SelectSelector(
                            SelectSelectorConfig(
                                options=CURRENCIES,
                                custom_value=True,
                                mode=SelectSelectorMode.DROPDOWN,
                            )
                        ),
                    }
                ),
                {"collapsed": False},
            ),
            vol.Required(SECTION_FEES): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_SELL_FEE_FIXED, default=o[CONF_SELL_FEE_FIXED]
                        ): _number(cur),
                        vol.Required(
                            CONF_SELL_FEE_PERCENT, default=o[CONF_SELL_FEE_PERCENT]
                        ): _number("%", step=0.001, max_=100),
                        vol.Required(
                            CONF_SELL_FEE_MIN, default=o[CONF_SELL_FEE_MIN]
                        ): _number(cur),
                    }
                ),
                {"collapsed": False},
            ),
            vol.Required(SECTION_TAX): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_CHURCH_TAX, default=o[CONF_CHURCH_TAX]
                        ): SelectSelector(
                            SelectSelectorConfig(
                                options=list(CHURCH_TAX_OPTIONS),
                                translation_key=CONF_CHURCH_TAX,
                                mode=SelectSelectorMode.DROPDOWN,
                            )
                        ),
                        vol.Required(
                            CONF_ALLOWANCE, default=o[CONF_ALLOWANCE]
                        ): _number(cur),
                        vol.Required(
                            CONF_STOCK_LOSS_POT, default=o[CONF_STOCK_LOSS_POT]
                        ): _number(cur),
                        vol.Required(
                            CONF_GENERAL_LOSS_POT, default=o[CONF_GENERAL_LOSS_POT]
                        ): _number(cur),
                    }
                ),
                {"collapsed": False},
            ),
        }
    )


def _flatten(user_input: dict[str, Any]) -> dict[str, Any]:
    """Turn sectioned form data into flat, typed options."""
    flat: dict[str, Any] = {}
    for value in user_input.values():
        flat.update(value)
    flat[CONF_SCAN_INTERVAL] = int(flat[CONF_SCAN_INTERVAL])
    flat[CONF_CURRENCY] = str(flat[CONF_CURRENCY]).strip().upper()
    for key in (
        CONF_SELL_FEE_FIXED, CONF_SELL_FEE_PERCENT, CONF_SELL_FEE_MIN,
        CONF_ALLOWANCE, CONF_STOCK_LOSS_POT, CONF_GENERAL_LOSS_POT,
    ):
        flat[key] = float(flat[key])
    return flat


class StockTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the (single) main entry."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(
                title=DEFAULT_NAME, data={}, options=_flatten(user_input)
            )

        return self.async_show_form(step_id="user", data_schema=_options_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return StockTrackerOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_STOCK: StockSubentryFlow}


class StockTrackerOptionsFlow(OptionsFlow):
    """Edit global settings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=_flatten(user_input))

        return self.async_show_form(
            step_id="init", data_schema=_options_schema(dict(self.config_entry.options))
        )


# --------------------------------------------------------------------------- #
# Subentry: one stock / position
# --------------------------------------------------------------------------- #


def _position_fields(currency: str, plan_collapsed: bool, income_collapsed: bool) -> dict:
    return {
        vol.Required(CONF_ASSET_TYPE, default=AssetType.STOCK.value): SelectSelector(
            SelectSelectorConfig(
                options=[t.value for t in AssetType],
                translation_key=CONF_ASSET_TYPE,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(CONF_QUANTITY, default=0): _number(step="any"),
        vol.Optional(CONF_PURCHASE_PRICE, default=0): _number(currency, step="any"),
        vol.Optional(CONF_PURCHASE_FEES, default=0): _number(currency),
        vol.Required(SECTION_INCOME): section(
            vol.Schema(
                {
                    vol.Optional(CONF_DISTRIBUTION, default=ACCUMULATING): SelectSelector(
                        SelectSelectorConfig(
                            options=[ACCUMULATING, DISTRIBUTING],
                            translation_key=CONF_DISTRIBUTION,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Optional(CONF_DIVIDENDS_RECEIVED, default=0): _number(currency),
                    vol.Optional(CONF_ADVANCE_TAXED, default=0): _number(currency),
                }
            ),
            {"collapsed": income_collapsed},
        ),
        vol.Required(SECTION_PLAN): section(
            vol.Schema(
                {
                    vol.Optional(CONF_PLAN_ACTIVE, default=True): BooleanSelector(),
                    vol.Optional(CONF_PLAN_AMOUNT, default=0): _number(currency),
                    vol.Optional(
                        CONF_PLAN_INTERVAL, default=PlanInterval.MONTHLY.value
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[i.value for i in PlanInterval],
                            translation_key=CONF_PLAN_INTERVAL,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(CONF_PLAN_DAY, default=1): NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=31, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Optional(CONF_PLAN_START): DateSelector(),
                    vol.Optional(CONF_PLAN_FEE_FIXED, default=0): _number(currency),
                    vol.Optional(CONF_PLAN_FEE_PERCENT, default=0): _number(
                        "%", step=0.001, max_=100
                    ),
                    vol.Optional(CONF_PLAN_FEE_INCLUDED, default=True): BooleanSelector(),
                }
            ),
            {"collapsed": plan_collapsed},
        ),
    }


def _position_data(user_input: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Flat subentry data from the form, plus an error key if invalid."""
    plan = user_input.get(SECTION_PLAN) or {}
    income = user_input.get(SECTION_INCOME) or {}
    data: dict[str, Any] = {
        CONF_ASSET_TYPE: user_input.get(CONF_ASSET_TYPE, AssetType.STOCK.value),
        CONF_QUANTITY: float(user_input.get(CONF_QUANTITY) or 0),
        CONF_PURCHASE_PRICE: float(user_input.get(CONF_PURCHASE_PRICE) or 0),
        CONF_PURCHASE_FEES: float(user_input.get(CONF_PURCHASE_FEES) or 0),
        CONF_DISTRIBUTION: income.get(CONF_DISTRIBUTION, ACCUMULATING),
        CONF_DIVIDENDS_RECEIVED: float(income.get(CONF_DIVIDENDS_RECEIVED) or 0),
        CONF_ADVANCE_TAXED: float(income.get(CONF_ADVANCE_TAXED) or 0),
        # automatic distribution calculation starts now
        CONF_DIVIDENDS_SINCE: dt_util.now().date().isoformat(),
        CONF_PLAN_ACTIVE: bool(plan.get(CONF_PLAN_ACTIVE, True)),
        CONF_PLAN_AMOUNT: float(plan.get(CONF_PLAN_AMOUNT) or 0),
        CONF_PLAN_INTERVAL: plan.get(CONF_PLAN_INTERVAL, PlanInterval.MONTHLY.value),
        CONF_PLAN_DAY: int(plan.get(CONF_PLAN_DAY) or 1),
        CONF_PLAN_START: plan.get(CONF_PLAN_START),
        CONF_PLAN_FEE_FIXED: float(plan.get(CONF_PLAN_FEE_FIXED) or 0),
        CONF_PLAN_FEE_PERCENT: float(plan.get(CONF_PLAN_FEE_PERCENT) or 0),
        CONF_PLAN_FEE_INCLUDED: bool(plan.get(CONF_PLAN_FEE_INCLUDED, True)),
    }
    if data[CONF_PLAN_AMOUNT] > 0:
        if not data[CONF_PLAN_START]:
            return data, "plan_start_required"
        fee = data[CONF_PLAN_FEE_FIXED] + data[CONF_PLAN_AMOUNT] * data[CONF_PLAN_FEE_PERCENT] / 100
        if data[CONF_PLAN_FEE_INCLUDED] and fee >= data[CONF_PLAN_AMOUNT]:
            return data, "plan_fee_too_high"
    return data, None


def _form_values(data: dict[str, Any]) -> dict[str, Any]:
    """Flat subentry data -> form values (plan/income fields in their sections)."""
    nested = set(PLAN_KEYS) | set(INCOME_KEYS)
    values = {k: v for k, v in data.items() if k not in nested and v is not None}
    values[SECTION_PLAN] = {k: data[k] for k in PLAN_KEYS if data.get(k) is not None}
    values[SECTION_INCOME] = {k: data[k] for k in INCOME_KEYS if data.get(k) is not None}
    return values


class StockSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure a tracked stock."""

    def __init__(self) -> None:
        self._candidates: list[Candidate] = []
        self._selected: Candidate | None = None

    @property
    def _currency(self) -> str:
        return self._get_entry().options.get(
            CONF_CURRENCY, DEFAULT_OPTIONS[CONF_CURRENCY]
        )

    # -------------------------------------------------------------- add: search

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Step 1: search by ticker, ISIN, WKN or name."""
        errors: dict[str, str] = {}

        if user_input is not None:
            query = user_input[CONF_QUERY].strip()
            result = await MarketData(async_get_clientsession(self.hass)).search(query)
            self._candidates = result.candidates
            if not self._candidates:
                errors["base"] = "cannot_connect" if result.errors else "symbol_not_found"
            else:
                first = self._candidates[0]
                # exact Yahoo ticker (e.g. "SAP.DE"): no need to choose
                if first.instrument.symbol == query.upper() and first.instrument.source == SOURCE_YAHOO:
                    return await self._async_select(first)
                return await self.async_step_select()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_QUERY): TextSelector()}), user_input
            ),
            errors=errors,
        )

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Step 2: pick a listing / trading venue."""
        if user_input is not None:
            return await self._async_select(self._candidates[int(user_input[CONF_CANDIDATE])])

        options = [
            SelectOptionDict(value=str(i), label=c.label)
            for i, c in enumerate(self._candidates)
        ]
        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CANDIDATE, default="0"): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
        )

    async def _async_select(self, candidate: Candidate) -> SubentryFlowResult:
        symbol = candidate.instrument.symbol
        if any(sub.unique_id == symbol for sub in self._get_entry().subentries.values()):
            return self.async_abort(reason="already_configured")
        self._selected = candidate
        return await self.async_step_details()

    async def async_step_details(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Step 3: name, position, income and savings plan."""
        assert self._selected is not None
        candidate = self._selected
        symbol = candidate.instrument.symbol
        errors: dict[str, str] = {}

        if user_input is not None:
            position, error = _position_data(user_input)
            if error:
                errors["base"] = error
            else:
                name = (user_input.get(CONF_NAME) or "").strip() or candidate.name or symbol
                return self.async_create_entry(
                    title=f"{name} ({symbol})",
                    data={
                        **instrument_data(candidate.instrument),
                        CONF_NAME: name,
                        **position,
                    },
                    unique_id=symbol,
                )

        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME): TextSelector(),
                **_position_fields(
                    self._currency, plan_collapsed=not errors, income_collapsed=True
                ),
            }
        )
        return self.async_show_form(
            step_id="details",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or {CONF_NAME: candidate.name}
            ),
            errors=errors,
            description_placeholders={
                "currency": self._currency,
                "instrument": candidate.label,
            },
        )

    # -------------------------------------------------------------- reconfigure

    def _folded_data(self, data: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
        """Fold calculated plan executions and distributions into the base data."""
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED or not entry.runtime_data.data:
            return data, 0, 0
        current = entry.runtime_data.data
        stock = stock_config_from_data(data)
        symbol = stock.symbol
        executions = payments = 0

        result = current.plans.get(symbol)
        if stock.plan and stock.base and result and result.executions:
            folded = combine(stock.base, result)
            data = {
                **data,
                CONF_QUANTITY: folded.quantity,
                CONF_PURCHASE_PRICE: round(folded.purchase_price, 4),
                CONF_PURCHASE_FEES: folded.purchase_fees,
                CONF_PLAN_START: next_start(stock.plan, result).isoformat(),
            }
            executions = len(result.executions)

        dividends = current.dividends.get(symbol)
        if dividends and dividends.payments:
            data = {**data, CONF_DIVIDENDS_RECEIVED: dividends.total_net}
            payments = len(dividends.payments)
        return data, executions, payments

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit name, position, income and savings plan (instrument stays fixed)."""
        subentry = self._get_reconfigure_subentry()
        symbol = subentry.data[CONF_SYMBOL]
        errors: dict[str, str] = {}

        if user_input is not None:
            name = (user_input.get(CONF_NAME) or "").strip() or symbol
            position, error = _position_data(user_input)
            if error:
                errors["base"] = error
            else:
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    title=f"{name} ({symbol})",
                    data={**subentry.data, CONF_NAME: name, **position},
                )
            suggested, executions, payments = user_input, 0, 0
        else:
            folded, executions, payments = self._folded_data(dict(subentry.data))
            suggested = _form_values(folded)

        stock = stock_config_from_data(dict(subentry.data))
        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME): TextSelector(),
                **_position_fields(
                    self._currency,
                    plan_collapsed=not (stock.plan or errors),
                    income_collapsed=not (stock.distributing or errors),
                ),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={
                "symbol": symbol,
                "currency": self._currency,
                "executions": str(executions),
                "payments": str(payments),
            },
        )
