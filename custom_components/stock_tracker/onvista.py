"""Small async client for the (unofficial) onvista JSON API.

Used for instruments Yahoo does not know - typically European ETFs. Lookup by
ISIN, WKN or name; prices are per trading venue ("notation").
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import aiohttp

from .api import StockApiError, StockQuote, SymbolNotFoundError
from .calc import TradingPeriods
from .const import REQUEST_TIMEOUT

_BASE = "https://api.onvista.de/api/v1"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
_TYPES = {"STOCK", "FUND", "BOND", "INDEX", "CERTIFICATE", "PRECIOUS_METAL", "CURRENCY"}


@dataclass(frozen=True, slots=True)
class OnvistaInstrument:
    """An instrument found by the search."""

    entity_type: str
    entity_value: str
    name: str
    isin: str | None
    wkn: str | None


@dataclass(frozen=True, slots=True)
class OnvistaVenue:
    """A trading venue (notation) of an instrument with its last price."""

    notation: int
    market: str
    market_code: str
    currency: str | None
    last: float | None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class OnvistaClient:
    """Fetch instruments, quotes and history from onvista."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                resp = await self._session.get(
                    f"{_BASE}{path}", params=params, headers=_HEADERS
                )
                if resp.status == 404:
                    raise SymbolNotFoundError(path)
                if resp.status == 429:
                    raise StockApiError("Rate limit reached (HTTP 429)")
                resp.raise_for_status()
                data = await resp.json()
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise StockApiError(f"onvista error for {path}: {err}") from err
        if isinstance(data, dict) and data.get("errorCode"):
            raise SymbolNotFoundError(path)
        return data

    async def async_search(self, query: str) -> list[OnvistaInstrument]:
        """Search by ISIN, WKN or name."""
        data = await self._get("/instruments/search/facet", {"searchValue": query})
        found: list[OnvistaInstrument] = []
        for facet in data.get("facets") or []:
            for item in facet.get("results") or []:
                if item.get("entityType") not in _TYPES or not item.get("entityValue"):
                    continue
                found.append(
                    OnvistaInstrument(
                        entity_type=item["entityType"],
                        entity_value=str(item["entityValue"]),
                        name=item.get("name") or "",
                        isin=item.get("isin"),
                        wkn=item.get("wkn"),
                    )
                )
        return found

    async def _snapshot(self, entity_type: str, entity_value: str) -> dict:
        return await self._get(f"/instruments/{entity_type}/{entity_value}/snapshot")

    async def async_get_venues(
        self, entity_type: str, entity_value: str
    ) -> list[OnvistaVenue]:
        """All trading venues with their last price."""
        snap = await self._snapshot(entity_type, entity_value)
        venues: list[OnvistaVenue] = []
        for q in (snap.get("quoteList") or {}).get("list") or []:
            market = q.get("market") or {}
            if not market.get("idNotation"):
                continue
            venues.append(
                OnvistaVenue(
                    notation=int(market["idNotation"]),
                    market=market.get("name") or "",
                    market_code=market.get("codeExchange") or market.get("codeMarket") or "",
                    currency=q.get("isoCurrency"),
                    last=q.get("last"),
                )
            )
        return venues

    async def async_get_quote(
        self, entity_type: str, entity_value: str, notation: int, symbol: str
    ) -> StockQuote:
        """Current quote at the given venue."""
        snap = await self._snapshot(entity_type, entity_value)
        quotes = [snap.get("quote") or {}, *((snap.get("quoteList") or {}).get("list") or [])]
        quote = next(
            (q for q in quotes if (q.get("market") or {}).get("idNotation") == notation),
            None,
        )
        if quote is None or quote.get("last") is None:
            raise SymbolNotFoundError(symbol)
        instrument = snap.get("instrument") or {}
        market = quote.get("market") or {}
        return StockQuote(
            symbol=symbol,
            name=instrument.get("name"),
            currency=quote.get("isoCurrency"),
            exchange=market.get("name"),
            price=float(quote["last"]),
            previous_close=quote.get("previousLast"),
            day_high=quote.get("high"),
            day_low=quote.get("low"),
            volume=quote.get("totalVolume"),
            fifty_two_week_high=quote.get("highPrice1Year"),
            fifty_two_week_low=quote.get("lowPrice1Year"),
            market_time=_parse_dt(quote.get("datetimeLast")),
            periods=TradingPeriods(),
        )

    async def async_get_history(
        self, entity_type: str, entity_value: str, notation: int, start: date
    ) -> list[tuple[date, float]]:
        """Daily closes since `start` (timestamps are 12:00 UTC)."""
        years = (date.today() - start).days / 365
        span = "Y1" if years < 1 else "Y3" if years < 3 else "Y5" if years < 5 else "MAX"
        data = await self._get(
            f"/instruments/{entity_type}/{entity_value}/eod_history",
            {"idNotation": notation, "range": span, "startDate": start.isoformat()},
        )
        out: dict[date, float] = {}
        for ts, close in zip(
            data.get("datetimeLast") or [], data.get("last") or [], strict=False
        ):
            if close is None:
                continue
            day = datetime.fromtimestamp(ts, UTC).date()
            if day >= start:
                out[day] = float(close)
        return sorted(out.items())
