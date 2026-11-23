"""Small async client for the (unofficial) Yahoo Finance chart API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiohttp

from .calc import TradingPeriods
from .const import REQUEST_TIMEOUT

_BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# Yahoo rejects requests without a browser-like User-Agent.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class StockApiError(Exception):
    """Generic API error (network, rate limit, unexpected payload)."""


class SymbolNotFoundError(StockApiError):
    """The requested symbol does not exist."""


@dataclass(slots=True)
class StockQuote:
    """Normalized quote data for one symbol."""

    symbol: str
    name: str | None
    currency: str | None
    exchange: str | None
    price: float | None
    previous_close: float | None
    day_high: float | None
    day_low: float | None
    volume: int | None
    fifty_two_week_high: float | None
    fifty_two_week_low: float | None
    market_time: datetime | None
    periods: TradingPeriods = field(default_factory=TradingPeriods)

    @property
    def change(self) -> float | None:
        """Absolute change compared to the previous close."""
        if self.price is None or self.previous_close is None:
            return None
        return round(self.price - self.previous_close, 4)

    @property
    def change_percent(self) -> float | None:
        """Relative change in percent compared to the previous close."""
        if self.change is None or not self.previous_close:
            return None
        return round(self.change / self.previous_close * 100, 2)


class StockApiClient:
    """Fetch quotes from Yahoo Finance."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def async_get_quote(self, symbol: str) -> StockQuote:
        """Fetch the current quote for a single symbol."""
        url = _BASE_URL.format(symbol=symbol)
        params = {"interval": "1d", "range": "1d"}
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                resp = await self._session.get(url, params=params, headers=_HEADERS)
                if resp.status == 404:
                    raise SymbolNotFoundError(symbol)
                if resp.status == 429:
                    raise StockApiError("Rate limit reached (HTTP 429)")
                resp.raise_for_status()
                payload: dict[str, Any] = await resp.json()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise StockApiError(f"Error fetching {symbol}: {err}") from err

        chart = payload.get("chart") or {}
        if chart.get("error"):
            code = (chart["error"] or {}).get("code", "")
            if code == "Not Found":
                raise SymbolNotFoundError(symbol)
            raise StockApiError(str(chart["error"]))

        results = chart.get("result") or []
        if not results:
            raise SymbolNotFoundError(symbol)

        meta: dict[str, Any] = results[0].get("meta") or {}
        if meta.get("regularMarketPrice") is None:
            raise SymbolNotFoundError(symbol)

        market_ts = meta.get("regularMarketTime")
        period = meta.get("currentTradingPeriod") or {}
        periods = TradingPeriods(
            pre_start=(period.get("pre") or {}).get("start"),
            regular_start=(period.get("regular") or {}).get("start"),
            regular_end=(period.get("regular") or {}).get("end"),
            post_end=(period.get("post") or {}).get("end"),
        )
        return StockQuote(
            symbol=meta.get("symbol", symbol),
            name=meta.get("longName") or meta.get("shortName"),
            currency=meta.get("currency"),
            exchange=meta.get("fullExchangeName") or meta.get("exchangeName"),
            price=meta.get("regularMarketPrice"),
            previous_close=meta.get("chartPreviousClose")
            or meta.get("previousClose"),
            day_high=meta.get("regularMarketDayHigh"),
            day_low=meta.get("regularMarketDayLow"),
            volume=meta.get("regularMarketVolume"),
            fifty_two_week_high=meta.get("fiftyTwoWeekHigh"),
            fifty_two_week_low=meta.get("fiftyTwoWeekLow"),
            market_time=datetime.fromtimestamp(market_ts, tz=UTC)
            if market_ts
            else None,
            periods=periods,
        )
