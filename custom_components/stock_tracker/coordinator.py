"""DataUpdateCoordinator for the Aktienkurse integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import StockApiError, StockQuote
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
    CONF_ADVANCE_TAXED,
    CONF_ALLOWANCE,
    CONF_ASSET_TYPE,
    CONF_CHURCH_TAX,
    CONF_CURRENCY,
    CONF_DISTRIBUTION,
    CONF_DIVIDENDS_RECEIVED,
    CONF_DIVIDENDS_SINCE,
    CONF_GENERAL_LOSS_POT,
    CONF_PURCHASE_FEES,
    CONF_PURCHASE_PRICE,
    CONF_QUANTITY,
    CONF_SCAN_INTERVAL,
    CONF_SELL_FEE_FIXED,
    CONF_SELL_FEE_MIN,
    CONF_SELL_FEE_PERCENT,
    CONF_STOCK_LOSS_POT,
    DEFAULT_OPTIONS,
    DISTRIBUTING,
    DIVIDEND_MAX_AGE,
    DOMAIN,
    HISTORY_MAX_AGE,
    SUBENTRY_TYPE_STOCK,
    scan_interval,
)
from .dividends import DividendResult, evaluate_dividends, trailing_yield
from .plan import History, PlanResult, SavingsPlan, combine, evaluate_plan, plan_from_data
from .sources import Instrument, MarketData, instrument_from_data

_LOGGER = logging.getLogger(__name__)

type StockConfigEntry = ConfigEntry[StockCoordinator]


@dataclass(frozen=True, slots=True)
class StockConfig:
    """Everything configured for one stock (subentry)."""

    instrument: Instrument
    base: Position | None  # None for watch-only
    plan: SavingsPlan | None
    distributing: bool
    dividends_since: date | None
    dividends_manual: float

    @property
    def symbol(self) -> str:
        return self.instrument.symbol

    @property
    def is_holding(self) -> bool:
        return self.base is not None


def stock_config_from_data(data: dict[str, Any]) -> StockConfig:
    """Parse subentry data."""
    plan = plan_from_data(data)
    quantity = float(data.get(CONF_QUANTITY) or 0)
    instrument = instrument_from_data(data)
    base = None
    if quantity > 0 or plan is not None:
        base = Position(
            symbol=instrument.symbol,
            quantity=max(quantity, 0.0),
            purchase_price=float(data.get(CONF_PURCHASE_PRICE) or 0),
            purchase_fees=float(data.get(CONF_PURCHASE_FEES) or 0),
            asset_type=AssetType(data.get(CONF_ASSET_TYPE, AssetType.STOCK)),
            advance_taxed=float(data.get(CONF_ADVANCE_TAXED) or 0),
        )
    since = data.get(CONF_DIVIDENDS_SINCE)
    return StockConfig(
        instrument=instrument,
        base=base,
        plan=plan,
        distributing=data.get(CONF_DISTRIBUTION) == DISTRIBUTING,
        dividends_since=date.fromisoformat(since) if since else None,
        dividends_manual=float(data.get(CONF_DIVIDENDS_RECEIVED) or 0),
    )


def is_holding(data: dict[str, Any]) -> bool:
    """True if the subentry describes a position (not watch-only)."""
    return stock_config_from_data(data).is_holding


@dataclass(slots=True)
class StockData:
    """Everything the sensors need."""

    quotes: dict[str, StockQuote] = field(default_factory=dict)
    fx_rates: dict[str, float] = field(default_factory=dict)  # currency -> rate
    positions: dict[str, PositionResult] = field(default_factory=dict)
    plans: dict[str, PlanResult] = field(default_factory=dict)
    dividends: dict[str, DividendResult] = field(default_factory=dict)
    yields: dict[str, float | None] = field(default_factory=dict)
    portfolio: PortfolioResult | None = None  # None if a position is incomplete
    missing: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Cached:
    start: date
    fetched: float
    data: History


def fx_symbol(currency: str, target: str) -> str:
    """Yahoo symbol for the exchange rate currency -> target."""
    return f"{currency}{target}=X"


class StockCoordinator(DataUpdateCoordinator[StockData]):
    """Fetch all configured instruments and value the portfolio."""

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
        self.market = MarketData(async_get_clientsession(hass))
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
        self._cache: dict[str, _Cached] = {}

    @property
    def stocks(self) -> list[StockConfig]:
        return [
            stock_config_from_data(dict(sub.data))
            for sub in self.config_entry.subentries.values()
            if sub.subentry_type == SUBENTRY_TYPE_STOCK
        ]

    # ------------------------------------------------------------------ fetch

    async def _fetch(
        self, instruments: list[Instrument], previous: dict[str, StockQuote]
    ) -> dict[str, StockQuote]:
        """Fetch quotes; fall back to the last known value on errors."""
        results = await asyncio.gather(
            *(self.market.quote(i) for i in instruments), return_exceptions=True
        )
        data: dict[str, StockQuote] = {}
        for inst, result in zip(instruments, results, strict=True):
            if isinstance(result, StockQuote):
                data[inst.symbol] = result
                continue
            if isinstance(result, StockApiError):
                _LOGGER.warning("Kurs für %s nicht abrufbar: %s", inst.symbol, result)
            else:
                _LOGGER.error("Unerwarteter Fehler für %s", inst.symbol, exc_info=result)
            if inst.symbol in previous:
                data[inst.symbol] = previous[inst.symbol]
        return data

    async def _cached(
        self, key: str, start: date, max_age: float, fetch
    ) -> History | None:
        """Cache series (history, dividends) per key."""
        cached = self._cache.get(key)
        if cached and cached.start <= start and time.monotonic() - cached.fetched < max_age:
            return cached.data
        try:
            data = await fetch()
        except StockApiError as err:
            _LOGGER.warning("Daten für %s nicht abrufbar: %s", key, err)
            return cached.data if cached and cached.start <= start else None
        self._cache[key] = _Cached(start, time.monotonic(), data)
        return data

    async def _history(self, inst: Instrument, start: date) -> History | None:
        return await self._cached(
            f"hist:{inst.symbol}", start, HISTORY_MAX_AGE,
            lambda: self.market.history(inst, start),
        )

    async def _dividend_events(self, inst: Instrument, start: date) -> History | None:
        return await self._cached(
            f"div:{inst.symbol}", start, DIVIDEND_MAX_AGE,
            lambda: self.market.dividends(inst, start),
        )

    async def _async_update_data(self) -> StockData:
        previous = self.data or StockData()
        stocks = self.stocks
        if not stocks:
            return StockData()

        quotes = await self._fetch([s.instrument for s in stocks], previous.quotes)
        if not quotes:
            raise UpdateFailed("Keine Kurse konnten geladen werden")

        currencies = {
            cur
            for q in quotes.values()
            if (cur := normalize_currency(q.currency)[0]) and cur != self.currency
        }
        fx_insts = {fx_symbol(c, self.currency): c for c in currencies}
        prev_fx = {fx_symbol(c, self.currency): r for c, r in previous.fx_rates.items()}
        fx_quotes = await self._fetch([Instrument(s) for s in fx_insts], {})
        fx_rates: dict[str, float] = {self.currency: 1.0}
        for sym, cur in fx_insts.items():
            if sym in fx_quotes and fx_quotes[sym].price:
                fx_rates[cur] = float(fx_quotes[sym].price)
            elif sym in prev_fx:
                fx_rates[cur] = prev_fx[sym]

        today = dt_util.now().date()
        data = StockData(quotes=quotes, fx_rates=fx_rates)
        for stock in stocks:
            await self._evaluate_stock(stock, data, today)

        holdings = [s for s in stocks if s.is_holding]
        data.portfolio = (
            evaluate_portfolio(list(data.positions.values()), self.tax)
            if holdings and not data.missing
            else None
        )
        return data

    async def _fx_history(self, currency: str | None, start: date) -> History | None | bool:
        """FX history to the portfolio currency; True if no conversion is needed."""
        if currency is None:
            return None
        if currency == self.currency:
            return True
        return await self._history(Instrument(fx_symbol(currency, self.currency)), start)

    async def _evaluate_stock(self, stock: StockConfig, data: StockData, today: date) -> None:
        symbol = stock.symbol
        quote = data.quotes.get(symbol)
        currency, divisor = normalize_currency(quote.currency if quote else None)

        # distribution yield (also for watch-only stocks)
        events: History | None = None
        if stock.distributing and stock.instrument.supports_dividends:
            since = stock.dividends_since or today
            events = await self._dividend_events(
                stock.instrument, min(since, today - timedelta(days=366))
            )
            data.yields[symbol] = (
                trailing_yield(events, quote.price, today) if events is not None and quote else None
            )

        if stock.base is None:
            return

        # savings plan
        plan_result: PlanResult | None = None
        if stock.plan is not None and stock.plan.active:
            if stock.plan.start > today:
                plan_result = evaluate_plan(stock.plan, [], None, today)
            else:
                prices = await self._history(stock.instrument, stock.plan.start)
                fx = await self._fx_history(currency, stock.plan.start)
                if prices is None or fx is None:
                    data.missing.append(symbol)
                    return
                plan_result = evaluate_plan(
                    stock.plan, prices, None if fx is True else fx, today, divisor
                )
            data.plans[symbol] = plan_result

        # distributions since the last reconciliation
        dividends_net = stock.dividends_manual
        if stock.distributing:
            result = DividendResult(manual_net=stock.dividends_manual)
            if events is not None and stock.dividends_since is not None:
                fx = await self._fx_history(currency, stock.dividends_since)
                if fx is not None:
                    result = evaluate_dividends(
                        events,
                        since=stock.dividends_since,
                        today=today,
                        base_quantity=stock.base.quantity,
                        executions=plan_result.executions if plan_result else [],
                        fx_rates=None if fx is True else fx,
                        divisor=divisor,
                        asset_type=stock.base.asset_type,
                        tax=self.tax,
                        manual_net=stock.dividends_manual,
                    )
            data.dividends[symbol] = result
            dividends_net = result.total_net

        rate = data.fx_rates.get(currency) if currency else None
        if quote is None or quote.price is None or rate is None:
            data.missing.append(symbol)
            return
        position = combine(stock.base, plan_result) if plan_result else stock.base
        if position.quantity <= 0:
            return  # plan without executions yet
        data.positions[symbol] = evaluate_position(
            position,
            price=quote.price / divisor,
            change=(quote.change / divisor) if quote.change is not None else None,
            fx_rate=rate,
            fees=self.fees,
            tax=self.tax,
            dividends=dividends_net,
        )
