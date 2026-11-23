"""DataUpdateCoordinator for the Aktienkurse integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

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
    HISTORY_MAX_AGE,
    SUBENTRY_TYPE_STOCK,
    scan_interval,
)
from .plan import History, PlanResult, SavingsPlan, combine, evaluate_plan, plan_from_data

_LOGGER = logging.getLogger(__name__)

type StockConfigEntry = ConfigEntry[StockCoordinator]


@dataclass(frozen=True, slots=True)
class Holding:
    """A configured position: manual base + optional savings plan."""

    base: Position
    plan: SavingsPlan | None


@dataclass(slots=True)
class StockData:
    """Everything the sensors need."""

    quotes: dict[str, StockQuote] = field(default_factory=dict)
    fx_rates: dict[str, float] = field(default_factory=dict)  # currency -> rate
    positions: dict[str, PositionResult] = field(default_factory=dict)
    plans: dict[str, PlanResult] = field(default_factory=dict)
    portfolio: PortfolioResult | None = None  # None if a position is incomplete
    missing: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _CachedHistory:
    start: date
    fetched: float
    data: History


def fx_symbol(currency: str, target: str) -> str:
    """Yahoo symbol for the exchange rate currency -> target."""
    return f"{currency}{target}=X"


def holding_from_data(data: dict[str, Any]) -> Holding | None:
    """Holding from subentry data; None for watch-only stocks."""
    plan = plan_from_data(data)
    quantity = float(data.get(CONF_QUANTITY) or 0)
    if quantity <= 0 and plan is None:
        return None
    return Holding(
        base=Position(
            symbol=data[CONF_SYMBOL],
            quantity=max(quantity, 0.0),
            purchase_price=float(data.get(CONF_PURCHASE_PRICE) or 0),
            purchase_fees=float(data.get(CONF_PURCHASE_FEES) or 0),
            asset_type=AssetType(data.get(CONF_ASSET_TYPE, AssetType.STOCK)),
        ),
        plan=plan,
    )


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
        self._history: dict[str, _CachedHistory] = {}

    def _stock_subentries(self):
        return [
            sub
            for sub in self.config_entry.subentries.values()
            if sub.subentry_type == SUBENTRY_TYPE_STOCK
        ]

    @property
    def symbols(self) -> list[str]:
        """All symbols configured as subentries."""
        return [sub.data[CONF_SYMBOL] for sub in self._stock_subentries()]

    @property
    def holdings(self) -> list[Holding]:
        """Stocks with a quantity > 0 or a savings plan."""
        return [
            h
            for sub in self._stock_subentries()
            if (h := holding_from_data(dict(sub.data))) is not None
        ]

    # ------------------------------------------------------------------ fetch

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

    async def _get_history(self, symbol: str, start: date) -> History | None:
        """Daily closes since `start`, cached for HISTORY_MAX_AGE seconds."""
        cached = self._history.get(symbol)
        if (
            cached
            and cached.start <= start
            and time.monotonic() - cached.fetched < HISTORY_MAX_AGE
        ):
            return cached.data
        try:
            data = await self.client.async_get_history(symbol, start)
        except StockApiError as err:
            _LOGGER.warning("Kursverlauf für %s nicht abrufbar: %s", symbol, err)
            return cached.data if cached and cached.start <= start else None
        self._history[symbol] = _CachedHistory(start, time.monotonic(), data)
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

        holdings = self.holdings
        plans = await self._evaluate_plans(holdings, quotes)
        return self._evaluate(holdings, quotes, fx_rates, plans)

    async def _evaluate_plans(
        self, holdings: list[Holding], quotes: dict[str, StockQuote]
    ) -> dict[str, PlanResult | None]:
        """Executions for every active plan (None if history is unavailable)."""
        today = dt_util.now().date()
        results: dict[str, PlanResult | None] = {}
        for holding in holdings:
            plan = holding.plan
            if plan is None or not plan.active:
                continue
            symbol = holding.base.symbol
            if plan.start > today:
                results[symbol] = evaluate_plan(plan, [], None, today)
                continue
            quote = quotes.get(symbol)
            currency, divisor = normalize_currency(quote.currency if quote else None)
            prices = await self._get_history(symbol, plan.start)
            fx: History | None = None
            if currency and currency != self.currency:
                fx = await self._get_history(fx_symbol(currency, self.currency), plan.start)
                if fx is None:
                    results[symbol] = None
                    continue
            if prices is None or currency is None:
                results[symbol] = None
                continue
            results[symbol] = evaluate_plan(plan, prices, fx, today, divisor)
        return results

    def _evaluate(
        self,
        holdings: list[Holding],
        quotes: dict[str, StockQuote],
        fx_rates: dict[str, float],
        plans: dict[str, PlanResult | None],
    ) -> StockData:
        positions: dict[str, PositionResult] = {}
        missing: list[str] = []

        for holding in holdings:
            symbol = holding.base.symbol
            quote = quotes.get(symbol)
            currency, divisor = normalize_currency(quote.currency if quote else None)
            rate = fx_rates.get(currency) if currency else None
            if symbol in plans and plans[symbol] is None:
                missing.append(symbol)
                continue
            if quote is None or quote.price is None or rate is None:
                missing.append(symbol)
                continue
            plan_result = plans.get(symbol)
            position = combine(holding.base, plan_result) if plan_result else holding.base
            if position.quantity <= 0:
                continue  # plan without executions yet
            positions[symbol] = evaluate_position(
                position,
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
            plans={s: p for s, p in plans.items() if p is not None},
            portfolio=portfolio,
            missing=missing,
        )
