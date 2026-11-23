"""Small async client for the (unofficial) Yahoo Finance chart API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiohttp

from homeassistant.util import dt as dt_util

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

    async def _async_chart(self, symbol: str, params: dict[str, Any]) -> dict[str, Any]:
        """Call the chart endpoint and return the first result."""
        url = _BASE_URL.format(symbol=symbol)
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

        return results[0]

    async def async_get_history(self, symbol: str, start: date) -> list[tuple[date, float]]:
        """Daily closes from `start` until today, keyed by exchange-local date."""
        begin = datetime.combine(start - timedelta(days=1), datetime.min.time(), UTC)
        result = await self._async_chart(
            symbol,
            {
                "interval": "1d",
                "period1": int(begin.timestamp()),
                "period2": int(datetime.now(UTC).timestamp()),
            },
        )
        meta = result.get("meta") or {}
        tz = await dt_util.async_get_time_zone(
            meta.get("exchangeTimezoneName") or "UTC"
        ) or UTC
        timestamps = result.get("timestamp") or []
        quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quotes.get("close") or []
        history: dict[date, float] = {}
        for ts, close in zip(timestamps, closes, strict=False):
            if close is None:
                continue
            day = datetime.fromtimestamp(ts, tz).date()
            if day >= start:
                history[day] = float(close)
        return sorted(history.items())

    async def async_get_quote(self, symbol: str) -> StockQuote:
        """Fetch the current quote for a single symbol."""
        result = await self._async_chart(symbol, {"interval": "1d", "range": "1d"})
        meta: dict[str, Any] = result.get("meta") or {}
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
