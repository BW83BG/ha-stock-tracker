"""Config flow for the Aktienkurse integration.

* The main config entry holds global settings (interval, currency, sell fees,
  tax situation). They are stored as options and editable via "Configure".
* Every tracked stock is a config *subentry* ("Aktie hinzufügen") holding the
  symbol and - optionally - the position (quantity, purchase price, fees).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .api import StockApiClient, StockApiError, StockQuote, SymbolNotFoundError
from .calc import AssetType
from .const import (
    CHURCH_TAX_OPTIONS,
    CONF_ALLOWANCE,
    CONF_ASSET_TYPE,
    CONF_CHURCH_TAX,
    CONF_CURRENCY,
    CONF_GENERAL_LOSS_POT,
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
    SUBENTRY_TYPE_STOCK,
)

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


def _position_fields(currency: str) -> dict:
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
    }


def _position_data(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_ASSET_TYPE: user_input.get(CONF_ASSET_TYPE, AssetType.STOCK.value),
        CONF_QUANTITY: float(user_input.get(CONF_QUANTITY) or 0),
        CONF_PURCHASE_PRICE: float(user_input.get(CONF_PURCHASE_PRICE) or 0),
        CONF_PURCHASE_FEES: float(user_input.get(CONF_PURCHASE_FEES) or 0),
    }


class StockSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure a tracked stock."""

    @property
    def _currency(self) -> str:
        return self._get_entry().options.get(
            CONF_CURRENCY, DEFAULT_OPTIONS[CONF_CURRENCY]
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a new stock."""
        errors: dict[str, str] = {}

        if user_input is not None:
            symbol = user_input[CONF_SYMBOL].strip().upper()
            entry = self._get_entry()

            if any(sub.unique_id == symbol for sub in entry.subentries.values()):
                return self.async_abort(reason="already_configured")

            quote, error = await self._async_validate(symbol)
            if error:
                errors["base"] = error
            else:
                assert quote is not None
                name = (user_input.get(CONF_NAME) or "").strip() or quote.name or symbol
                return self.async_create_entry(
                    title=f"{name} ({symbol})",
                    data={
                        CONF_SYMBOL: symbol,
                        CONF_NAME: name,
                        **_position_data(user_input),
                    },
                    unique_id=symbol,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_SYMBOL): TextSelector(),
                vol.Optional(CONF_NAME): TextSelector(),
                **_position_fields(self._currency),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders={"currency": self._currency},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit name and position (the symbol itself stays fixed)."""
        subentry = self._get_reconfigure_subentry()
        symbol = subentry.data[CONF_SYMBOL]

        if user_input is not None:
            name = (user_input.get(CONF_NAME) or "").strip() or symbol
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title=f"{name} ({symbol})",
                data={
                    **subentry.data,
                    CONF_NAME: name,
                    **_position_data(user_input),
                },
            )

        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME): TextSelector(),
                **_position_fields(self._currency),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                schema, dict(subentry.data)
            ),
            description_placeholders={"symbol": symbol, "currency": self._currency},
        )

    async def _async_validate(
        self, symbol: str
    ) -> tuple[StockQuote | None, str | None]:
        """Check that Yahoo knows the symbol."""
        client = StockApiClient(async_get_clientsession(self.hass))
        try:
            return await client.async_get_quote(symbol), None
        except SymbolNotFoundError:
            return None, "symbol_not_found"
        except StockApiError:
            return None, "cannot_connect"
