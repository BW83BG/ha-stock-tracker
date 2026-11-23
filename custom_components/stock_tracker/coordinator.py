"""DataUpdateCoordinator for the Aktienkurse integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import StockApiClient, StockApiError, StockQuote
from .calc import (
    AssetType,
    FeeSettings,
    PortfolioResult,
    Position,
    PositionResult,
    TaxSettings,
    evaluate_portfolio,
    evaluate_position,
    normalize_currency,
)
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
    DEFAULT_OPTIONS,
    DOMAIN,
    SUBENTRY_TYPE_STOCK,
    scan_interval,
)

_LOGGER = logging.getLogger(__name__)

type StockConfigEntry = ConfigEntry[StockCoordinator]


@dataclass(slots=True)
class StockData:
    """Everything the sensors need."""

    quotes: dict[str, StockQuote] = field(default_factory=dict)
    fx_rates: dict[str, float] = field(default_factory=dict)  # currency -> rate
    positions: dict[str, PositionResult] = field(default_factory=dict)
    portfolio: PortfolioResult | None = None  # None if a position is incomplete
    missing: list[str] = field(default_factory=list)


def fx_symbol(currency: str, target: str) -> str:
    """Yahoo symbol for the exchange rate currency -> target."""
    return f"{currency}{target}=X"


class StockCoordinator(DataUpdateCoordinator[StockData]):
    """Fetch all configured symbols and value the portfolio."""

    config_entry: StockConfigEntry

    def __init__(self, hass: HomeAssistant, entry: StockConfigEntry) -> None:
        self.options: dict[str, Any] = {**DEFAULT_OPTIONS, **entry.options}
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=scan_interval(int(self.options[CONF_SCAN_INTERVAL])),
        )
        self.client = StockApiClient(async_get_clientsession(hass))
        self.currency: str = self.options[CONF_CURRENCY]
        self.fees = FeeSettings(
            fixed=float(self.options[CONF_SELL_FEE_FIXED]),
            percent=float(self.options[CONF_SELL_FEE_PERCENT]),
            minimum=float(self.options[CONF_SELL_FEE_MIN]),
        )
        self.tax = TaxSettings(
            church_tax_rate=CHURCH_TAX_OPTIONS.get(self.options[CONF_CHURCH_TAX], 0.0),
            allowance_remaining=float(self.options[CONF_ALLOWANCE]),
            stock_loss_pot=float(self.options[CONF_STOCK_LOSS_POT]),
            general_loss_pot=float(self.options[CONF_GENERAL_LOSS_POT]),
        )

    @property
    def symbols(self) -> list[str]:
        """All symbols configured as subentries."""
        return [
            sub.data[CONF_SYMBOL]
            for sub in self.config_entry.subentries.values()
            if sub.subentry_type == SUBENTRY_TYPE_STOCK
        ]

    @property
    def holdings(self) -> list[Position]:
        """Subentries with a quantity > 0."""
        result = []
        for sub in self.config_entry.subentries.values():
            if sub.subentry_type != SUBENTRY_TYPE_STOCK:
                continue
            quantity = float(sub.data.get(CONF_QUANTITY) or 0)
            if quantity <= 0:
                continue
            result.append(
                Position(
                    symbol=sub.data[CONF_SYMBOL],
                    quantity=quantity,
                    purchase_price=float(sub.data.get(CONF_PURCHASE_PRICE) or 0),
                    purchase_fees=float(sub.data.get(CONF_PURCHASE_FEES) or 0),
                    asset_type=AssetType(
                        sub.data.get(CONF_ASSET_TYPE, AssetType.STOCK)
                    ),
                )
            )
        return result

    async def _fetch(self, symbols: list[str], previous: dict[str, Any]) -> dict:
        """Fetch quotes; fall back to the last known value on errors."""
        results = await asyncio.gather(
            *(self.client.async_get_quote(s) for s in symbols),
            return_exceptions=True,
        )
        data: dict[str, StockQuote] = {}
        for symbol, result in zip(symbols, results, strict=True):
            if isinstance(result, StockQuote):
                data[symbol] = result
                continue
            if isinstance(result, StockApiError):
                _LOGGER.warning("Kurs für %s nicht abrufbar: %s", symbol, result)
            else:
                _LOGGER.error("Unerwarteter Fehler für %s", symbol, exc_info=result)
            if symbol in previous:
                data[symbol] = previous[symbol]
        return data

    async def _async_update_data(self) -> StockData:
        previous = self.data or StockData()
        symbols = self.symbols
        if not symbols:
            return StockData()

        quotes = await self._fetch(symbols, previous.quotes)
        if not quotes:
            raise UpdateFailed("Keine Kurse konnten geladen werden")

        # Exchange rates for every foreign currency in use
        currencies = {
            cur
            for q in quotes.values()
            if (cur := normalize_currency(q.currency)[0]) and cur != self.currency
        }
        fx_symbols = {fx_symbol(c, self.currency): c for c in currencies}
        prev_fx = {
            fx_symbol(c, self.currency): rate for c, rate in previous.fx_rates.items()
        }
        # previous rates are applied below via prev_fx
        fx_quotes = await self._fetch(list(fx_symbols), {})
        fx_rates: dict[str, float] = {self.currency: 1.0}
        for sym, cur in fx_symbols.items():
            if sym in fx_quotes and fx_quotes[sym].price:
                fx_rates[cur] = float(fx_quotes[sym].price)
            elif sym in prev_fx:
                fx_rates[cur] = prev_fx[sym]

        return self._evaluate(quotes, fx_rates)

    def _evaluate(
        self, quotes: dict[str, StockQuote], fx_rates: dict[str, float]
    ) -> StockData:
        positions: dict[str, PositionResult] = {}
        missing: list[str] = []
        holdings = self.holdings

        for holding in holdings:
            quote = quotes.get(holding.symbol)
            currency, divisor = normalize_currency(quote.currency if quote else None)
            rate = fx_rates.get(currency) if currency else None
            if quote is None or quote.price is None or rate is None:
                missing.append(holding.symbol)
                continue
            positions[holding.symbol] = evaluate_position(
                holding,
                price=quote.price / divisor,
                change=(quote.change / divisor) if quote.change is not None else None,
                fx_rate=rate,
                fees=self.fees,
                tax=self.tax,
            )

        portfolio = (
            evaluate_portfolio(list(positions.values()), self.tax)
            if holdings and not missing
            else None
        )
        return StockData(
            quotes=quotes,
            fx_rates=fx_rates,
            positions=positions,
            portfolio=portfolio,
            missing=missing,
        )
