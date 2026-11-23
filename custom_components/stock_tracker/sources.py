"""Unified access to the price sources (Yahoo Finance, onvista)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
import re
from typing import Any
from urllib.parse import quote as urlquote

import aiohttp

from .api import StockApiError, StockQuote, SymbolNotFoundError, YahooClient
from .const import (
    CONF_ISIN,
    CONF_OV_ID,
    CONF_OV_NOTATION,
    CONF_OV_TYPE,
    CONF_SOURCE,
    CONF_SYMBOL,
    SOURCE_ONVISTA,
    SOURCE_YAHOO,
)
from .onvista import OnvistaClient

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
WKN_RE = re.compile(r"^[A-Z0-9]{6}$")
MAX_ONVISTA_INSTRUMENTS = 3


@dataclass(frozen=True, slots=True)
class Instrument:
    """Where to get prices for one configured stock."""

    symbol: str  # identity (Yahoo ticker or ISIN@venue for onvista)
    source: str = SOURCE_YAHOO
    ov_type: str | None = None
    ov_id: str | None = None
    ov_notation: int | None = None
    isin: str | None = None

    @property
    def supports_dividends(self) -> bool:
        return self.source == SOURCE_YAHOO

    @property
    def url(self) -> str:
        if self.source == SOURCE_ONVISTA:
            return f"https://www.onvista.de/suche/?searchValue={urlquote(self.isin or self.symbol)}"
        return f"https://finance.yahoo.com/quote/{urlquote(self.symbol)}"

    @property
    def provider(self) -> str:
        return "onvista" if self.source == SOURCE_ONVISTA else "Yahoo Finance"


def instrument_from_data(data: Mapping[str, Any]) -> Instrument:
    """Instrument from subentry data (entries without source are Yahoo)."""
    return Instrument(
        symbol=data[CONF_SYMBOL],
        source=data.get(CONF_SOURCE, SOURCE_YAHOO),
        ov_type=data.get(CONF_OV_TYPE),
        ov_id=data.get(CONF_OV_ID),
        ov_notation=int(data[CONF_OV_NOTATION]) if data.get(CONF_OV_NOTATION) else None,
        isin=data.get(CONF_ISIN),
    )


def instrument_data(inst: Instrument) -> dict[str, Any]:
    """Subentry data describing the instrument."""
    data: dict[str, Any] = {CONF_SYMBOL: inst.symbol, CONF_SOURCE: inst.source}
    if inst.source == SOURCE_ONVISTA:
        data |= {
            CONF_OV_TYPE: inst.ov_type,
            CONF_OV_ID: inst.ov_id,
            CONF_OV_NOTATION: inst.ov_notation,
            CONF_ISIN: inst.isin,
        }
    return data


@dataclass(frozen=True, slots=True)
class Candidate:
    """A search hit the user can pick."""

    instrument: Instrument
    label: str
    name: str | None = None


@dataclass(slots=True)
class SearchResult:
    candidates: list[Candidate] = field(default_factory=list)
    errors: int = 0  # sources that failed


def _fmt_price(price: float | None, currency: str | None) -> str:
    if price is None:
        return ""
    return f" · {price:,.2f} {currency or ''}".rstrip()


class MarketData:
    """Facade over all price sources."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.yahoo = YahooClient(session)
        self.onvista = OnvistaClient(session)

    async def quote(self, inst: Instrument) -> StockQuote:
        if inst.source == SOURCE_ONVISTA:
            assert inst.ov_type and inst.ov_id and inst.ov_notation
            return await self.onvista.async_get_quote(
                inst.ov_type, inst.ov_id, inst.ov_notation, inst.symbol
            )
        return await self.yahoo.async_get_quote(inst.symbol)

    async def history(self, inst: Instrument, start: date) -> list[tuple[date, float]]:
        if inst.source == SOURCE_ONVISTA:
            assert inst.ov_type and inst.ov_id and inst.ov_notation
            return await self.onvista.async_get_history(
                inst.ov_type, inst.ov_id, inst.ov_notation, start
            )
        return await self.yahoo.async_get_history(inst.symbol, start)

    async def dividends(self, inst: Instrument, start: date) -> list[tuple[date, float]]:
        if not inst.supports_dividends:
            return []
        return await self.yahoo.async_get_dividends(inst.symbol, start)

    async def search(self, query: str) -> SearchResult:
        """Search Yahoo (ticker, search) and onvista (ISIN/WKN/name)."""
        q = query.strip()
        upper = q.upper()
        result = SearchResult()
        seen: set[str] = set()

        def add(c: Candidate) -> None:
            if c.instrument.symbol not in seen:
                seen.add(c.instrument.symbol)
                result.candidates.append(c)

        # 1) exact Yahoo ticker
        try:
            quote = await self.yahoo.async_get_quote(upper)
            add(
                Candidate(
                    Instrument(symbol=upper),
                    f"{upper} · {quote.name or ''} · {quote.exchange or ''}"
                    f"{_fmt_price(quote.price, quote.currency)} (Yahoo)",
                    quote.name,
                )
            )
        except SymbolNotFoundError:
            pass
        except StockApiError:
            result.errors += 1

        # 2) Yahoo search (name / ISIN)
        try:
            for hit in await self.yahoo.async_search(q):
                add(
                    Candidate(
                        Instrument(symbol=hit.symbol),
                        f"{hit.symbol} · {hit.name or ''} · {hit.exchange or ''}"
                        f"{' · ' + hit.quote_type if hit.quote_type else ''} (Yahoo)",
                        hit.name,
                    )
                )
        except StockApiError:
            result.errors += 1

        # 3) onvista: one entry per trading venue
        try:
            instruments = await self.onvista.async_search(q)
            if ISIN_RE.match(upper):
                instruments = [i for i in instruments if i.isin == upper] or instruments
            for ins in instruments[:MAX_ONVISTA_INSTRUMENTS]:
                for venue in await self.onvista.async_get_venues(
                    ins.entity_type, ins.entity_value
                ):
                    ident = ins.isin or ins.wkn or ins.entity_value
                    add(
                        Candidate(
                            Instrument(
                                symbol=f"{ident}@{venue.market_code}",
                                source=SOURCE_ONVISTA,
                                ov_type=ins.entity_type,
                                ov_id=ins.entity_value,
                                ov_notation=venue.notation,
                                isin=ins.isin,
                            ),
                            f"{venue.market} · {ins.name}"
                            f"{_fmt_price(venue.last, venue.currency)} (onvista)",
                            ins.name,
                        )
                    )
        except SymbolNotFoundError:
            pass
        except StockApiError:
            result.errors += 1

        return result
