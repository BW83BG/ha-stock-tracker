"""Shared fixtures."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from custom_components.stock_tracker.api import StockQuote, SymbolNotFoundError
from custom_components.stock_tracker.calc import TradingPeriods


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom_components in every test."""
    return


def make_quote(symbol: str, price: float, prev: float, currency: str, name: str) -> StockQuote:
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


QUOTES = {
    "SAP.DE": make_quote("SAP.DE", 150.0, 148.0, "EUR", "SAP SE"),
    "AAPL": make_quote("AAPL", 250.0, 245.0, "USD", "Apple Inc."),
    "VOD.L": make_quote("VOD.L", 100.0, 99.0, "GBp", "Vodafone"),
    "USDEUR=X": make_quote("USDEUR=X", 0.9, 0.9, "EUR", None),
    "GBPEUR=X": make_quote("GBPEUR=X", 1.2, 1.2, "EUR", None),
}


@pytest.fixture
def mock_api():
    """Patch the Yahoo client with static quotes."""

    async def _quote(self, symbol):
        if symbol not in QUOTES:
            raise SymbolNotFoundError(symbol)
        return QUOTES[symbol]

    with patch(
        "custom_components.stock_tracker.api.StockApiClient.async_get_quote",
        autospec=True,
        side_effect=_quote,
    ) as mock:
        yield mock
