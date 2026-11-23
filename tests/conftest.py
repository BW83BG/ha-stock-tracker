"""Shared fixtures."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.stock_tracker.api import (
    StockQuote,
    SymbolNotFoundError,
    YahooSearchResult,
)
from custom_components.stock_tracker.calc import TradingPeriods
from custom_components.stock_tracker.onvista import OnvistaInstrument, OnvistaVenue

TODAY = date(2026, 11, 23)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom_components in every test."""
    return


def make_quote(symbol: str, price: float, prev: float, currency: str, name: str | None) -> StockQuote:
    return StockQuote(
        symbol=symbol,
        name=name,
        currency=currency,
        exchange="TEST",
        price=price,
        previous_close=prev,
        day_high=price,
        day_low=prev,
        volume=1000,
        fifty_two_week_high=price * 1.2,
        fifty_two_week_low=prev * 0.8,
        market_time=datetime(2026, 11, 23, 12, tzinfo=UTC),
        periods=TradingPeriods(),
    )


def make_history(start, end, price: float):
    """Weekday closes with a constant price."""
    day, out = start, []
    while day <= end:
        if day.weekday() < 5:
            out.append((day, price))
        day += timedelta(days=1)
    return out


QUOTES = {
    "SAP.DE": make_quote("SAP.DE", 150.0, 148.0, "EUR", "SAP SE"),
    "AAPL": make_quote("AAPL", 250.0, 245.0, "USD", "Apple Inc."),
    "VOD.L": make_quote("VOD.L", 100.0, 99.0, "GBp", "Vodafone"),
    "VUSA.DE": make_quote("VUSA.DE", 100.0, 100.0, "EUR", "Vanguard S&P 500"),
    "USDEUR=X": make_quote("USDEUR=X", 0.9, 0.9, "EUR", None),
    "GBPEUR=X": make_quote("GBPEUR=X", 1.2, 1.2, "EUR", None),
}
HISTORY_PRICES = {"SAP.DE": 100.0, "AAPL": 200.0, "USDEUR=X": 0.9, "VUSA.DE": 100.0}
# VUSA.DE: quarterly distributions of 0.25 EUR per share
DIVIDENDS = {
    "VUSA.DE": [
        (date(2025, 12, 18), 0.25),
        (date(2026, 3, 19), 0.25),
        (date(2026, 6, 18), 0.25),
        (date(2026, 9, 17), 0.25),
    ],
}
SEARCH = {
    "IE00B4L5Y983": [YahooSearchResult("IWDA.L", "iShares Core MSCI World", "LSE", "ETF")],
}

OV_ISIN = "IE0006WW1TQ4"
OV_INSTRUMENT = OnvistaInstrument("FUND", "268395859", "Xtrackers MSCI World ex USA", OV_ISIN, "DBX0VH")
OV_VENUES = [
    OnvistaVenue(454412888, "Xetra", "GER", "EUR", 39.49),
    OnvistaVenue(455467839, "LS Exchange", "LSX", "EUR", 39.425),
]


@pytest.fixture
def mock_api():
    """Patch Yahoo and onvista with static data."""

    async def _quote(self, symbol):
        if symbol not in QUOTES:
            raise SymbolNotFoundError(symbol)
        return QUOTES[symbol]

    async def _history(self, symbol, start):
        return make_history(start, TODAY, HISTORY_PRICES[symbol])

    async def _dividends(self, symbol, start):
        return [(d, a) for d, a in DIVIDENDS.get(symbol, []) if d >= start]

    async def _search(self, query):
        return SEARCH.get(query, [])

    async def _ov_search(self, query):
        return [OV_INSTRUMENT] if query in (OV_ISIN, "DBX0VH") else []

    async def _ov_venues(self, entity_type, entity_value):
        return OV_VENUES

    async def _ov_quote(self, entity_type, entity_value, notation, symbol):
        return make_quote(symbol, 40.0, 39.0, "EUR", OV_INSTRUMENT.name)

    async def _ov_history(self, entity_type, entity_value, notation, start):
        return make_history(start, TODAY, 38.0)

    base = "custom_components.stock_tracker"
    with (
        patch(f"{base}.api.YahooClient.async_get_quote", autospec=True, side_effect=_quote) as mock,
        patch(f"{base}.api.YahooClient.async_get_history", autospec=True, side_effect=_history),
        patch(f"{base}.api.YahooClient.async_get_dividends", autospec=True, side_effect=_dividends),
        patch(f"{base}.api.YahooClient.async_search", autospec=True, side_effect=_search),
        patch(f"{base}.onvista.OnvistaClient.async_search", autospec=True, side_effect=_ov_search),
        patch(f"{base}.onvista.OnvistaClient.async_get_venues", autospec=True, side_effect=_ov_venues),
        patch(f"{base}.onvista.OnvistaClient.async_get_quote", autospec=True, side_effect=_ov_quote),
        patch(f"{base}.onvista.OnvistaClient.async_get_history", autospec=True, side_effect=_ov_history),
    ):
        yield mock


@pytest.fixture
def mock_history(mock_api):
    """Kept for readability: history is part of mock_api."""
    return mock_api
