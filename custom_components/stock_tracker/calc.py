"""Pure portfolio / fee / tax calculations (no Home Assistant imports).

Tax model: German capital gains tax (Abgeltungsteuer) for private investors.

* Abgeltungsteuer 25 %, Solidaritätszuschlag 5.5 % of it, optional church tax
  (8 % / 9 % of it). With church tax the base rate is reduced:
  Abgeltungsteuer = taxable / (4 + k)  ->  total rate = (1 + 0.055 + k) / (4 + k)
* Fees: buy fees raise the cost basis, sell fees reduce the proceeds.
* Partial exemption (Teilfreistellung) for funds reduces gains and losses.
* Losses from stocks may only be offset against gains from stocks; other
  losses offset everything. Existing loss pots and the remaining saver's
  allowance (Sparerpauschbetrag) are applied afterwards.

Simplifications: no foreign withholding tax credit, no Vorabpauschale,
no FIFO lot tracking (irrelevant when the whole position is sold).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class AssetType(StrEnum):
    """Asset classes with different tax treatment."""

    STOCK = "stock"  # Aktie: separate loss pot
    EQUITY_FUND = "equity_fund"  # Aktienfonds/-ETF: 30 % Teilfreistellung
    MIXED_FUND = "mixed_fund"  # Mischfonds: 15 % Teilfreistellung
    REAL_ESTATE_FUND = "real_estate_fund"  # Immobilienfonds: 60 %
    OTHER = "other"  # Anleihen, ETCs, Derivate, ...: no exemption
    TAX_FREE = "tax_free"  # e.g. crypto/gold held > 1 year


PARTIAL_EXEMPTION: dict[AssetType, float] = {
    AssetType.STOCK: 0.0,
    AssetType.EQUITY_FUND: 0.30,
    AssetType.MIXED_FUND: 0.15,
    AssetType.REAL_ESTATE_FUND: 0.60,
    AssetType.OTHER: 0.0,
    AssetType.TAX_FREE: 1.0,
}

SOLI_RATE = 0.055

# Currencies quoted in minor units by Yahoo -> (major currency, divisor)
MINOR_CURRENCIES: dict[str, tuple[str, int]] = {
    "GBp": ("GBP", 100),
    "GBX": ("GBP", 100),
    "ZAc": ("ZAR", 100),
    "ILA": ("ILS", 100),
}


def normalize_currency(currency: str | None) -> tuple[str | None, int]:
    """Map minor currency units (GBp) to the major currency and a divisor."""
    if currency is None:
        return None, 1
    return MINOR_CURRENCIES.get(currency, (currency, 1))


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FeeSettings:
    """Broker sell fee: fixed + percentage, at least `minimum`."""

    fixed: float = 0.0
    percent: float = 0.0
    minimum: float = 0.0

    def sell_fee(self, proceeds: float) -> float:
        if proceeds <= 0:
            return 0.0
        return round(max(self.minimum, self.fixed + proceeds * self.percent / 100), 2)


@dataclass(frozen=True, slots=True)
class TaxSettings:
    """Personal tax situation for the current year."""

    church_tax_rate: float = 0.0  # 0.0, 0.08 or 0.09
    allowance_remaining: float = 0.0  # remaining Sparerpauschbetrag
    stock_loss_pot: float = 0.0  # Verlusttopf Aktien (positive number)
    general_loss_pot: float = 0.0  # Allgemeiner Verlusttopf (positive number)

    @property
    def total_rate(self) -> float:
        k = self.church_tax_rate
        return (1 + SOLI_RATE + k) / (4 + k)


@dataclass(frozen=True, slots=True)
class TaxResult:
    """Tax due for a hypothetical sale."""

    taxable: float  # after netting, loss pots and allowance
    capital_gains_tax: float  # Abgeltungsteuer
    solidarity: float  # Solidaritätszuschlag
    church: float  # Kirchensteuer
    allowance_used: float

    @property
    def total(self) -> float:
        return round(self.capital_gains_tax + self.solidarity + self.church, 2)


def calculate_tax(
    stock_result: float, other_result: float, settings: TaxSettings
) -> TaxResult:
    """Tax for a sale with the given taxable results (after Teilfreistellung).

    stock_result: net result of all sold stocks (Aktien)
    other_result: net result of everything else (funds, bonds, ...)
    """
    stock = stock_result - settings.stock_loss_pot
    stock = max(stock, 0.0)  # remaining stock losses cannot offset other gains
    total = max(stock + other_result - settings.general_loss_pot, 0.0)

    allowance_used = min(total, max(settings.allowance_remaining, 0.0))
    taxable = round(total - allowance_used, 2)

    k = settings.church_tax_rate
    base = taxable / (4 + k)
    return TaxResult(
        taxable=taxable,
        capital_gains_tax=round(base, 2),
        solidarity=round(base * SOLI_RATE, 2),
        church=round(base * k, 2),
        allowance_used=round(allowance_used, 2),
    )


# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Position:
    """A holding as configured by the user (amounts in portfolio currency)."""

    symbol: str
    quantity: float
    purchase_price: float  # per share, portfolio currency
    purchase_fees: float  # total buy fees, portfolio currency
    asset_type: AssetType = AssetType.STOCK


@dataclass(frozen=True, slots=True)
class PositionResult:
    """Valuation of one position if it were sold now."""

    symbol: str
    asset_type: AssetType
    quantity: float
    price: float  # per share, portfolio currency
    fx_rate: float
    market_value: float
    cost_basis: float  # quantity * purchase_price + purchase_fees
    sell_fee: float
    gain: float  # before tax, after all fees
    taxable_gain: float  # gain after Teilfreistellung (before netting)
    tax: TaxResult  # if only this position were sold
    day_change: float

    @property
    def gain_percent(self) -> float | None:
        return round(self.gain / self.cost_basis * 100, 2) if self.cost_basis else None

    @property
    def gain_after_tax(self) -> float:
        return round(self.gain - self.tax.total, 2)

    @property
    def net_proceeds(self) -> float:
        return round(self.market_value - self.sell_fee - self.tax.total, 2)


def _split(asset_type: AssetType, taxable: float) -> tuple[float, float]:
    if asset_type is AssetType.STOCK:
        return taxable, 0.0
    return 0.0, taxable


def evaluate_position(
    position: Position,
    price: float,
    change: float | None,
    fx_rate: float,
    fees: FeeSettings,
    tax: TaxSettings,
) -> PositionResult:
    """Value one position. `price`/`change` in quote currency (major unit)."""
    price_pf = price * fx_rate
    market_value = round(position.quantity * price_pf, 2)
    cost_basis = round(
        position.quantity * position.purchase_price + position.purchase_fees, 2
    )
    sell_fee = fees.sell_fee(market_value)
    gain = round(market_value - sell_fee - cost_basis, 2)
    taxable = round(gain * (1 - PARTIAL_EXEMPTION[position.asset_type]), 2)

    return PositionResult(
        symbol=position.symbol,
        asset_type=position.asset_type,
        quantity=position.quantity,
        price=round(price_pf, 4),
        fx_rate=fx_rate,
        market_value=market_value,
        cost_basis=cost_basis,
        sell_fee=sell_fee,
        gain=gain,
        taxable_gain=taxable,
        tax=calculate_tax(*_split(position.asset_type, taxable), tax),
        day_change=round(position.quantity * (change or 0.0) * fx_rate, 2),
    )


@dataclass(frozen=True, slots=True)
class PortfolioResult:
    """Totals if the whole portfolio were sold now."""

    positions: list[PositionResult] = field(default_factory=list)
    tax: TaxResult = field(
        default_factory=lambda: calculate_tax(0.0, 0.0, TaxSettings())
    )

    def _sum(self, attr: str) -> float:
        return round(sum(getattr(p, attr) for p in self.positions), 2)

    @property
    def market_value(self) -> float:
        return self._sum("market_value")

    @property
    def cost_basis(self) -> float:
        return self._sum("cost_basis")

    @property
    def sell_fees(self) -> float:
        return self._sum("sell_fee")

    @property
    def gain(self) -> float:
        return self._sum("gain")

    @property
    def gain_percent(self) -> float | None:
        cost = self.cost_basis
        return round(self.gain / cost * 100, 2) if cost else None

    @property
    def gain_after_tax(self) -> float:
        return round(self.gain - self.tax.total, 2)

    @property
    def net_proceeds(self) -> float:
        return round(self.market_value - self.sell_fees - self.tax.total, 2)

    @property
    def day_change(self) -> float:
        return self._sum("day_change")


def evaluate_portfolio(
    results: list[PositionResult], tax: TaxSettings
) -> PortfolioResult:
    """Combine positions, netting gains and losses for the tax calculation."""
    stock = sum(r.taxable_gain for r in results if r.asset_type is AssetType.STOCK)
    other = sum(
        r.taxable_gain for r in results if r.asset_type is not AssetType.STOCK
    )
    return PortfolioResult(positions=results, tax=calculate_tax(stock, other, tax))


# --------------------------------------------------------------------------- #
# Market state
# --------------------------------------------------------------------------- #


class MarketState(StrEnum):
    PRE = "pre"
    OPEN = "open"
    POST = "post"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class TradingPeriods:
    """Unix timestamps of today's trading sessions."""

    pre_start: int | None = None
    regular_start: int | None = None
    regular_end: int | None = None
    post_end: int | None = None

    def state(self, now: datetime) -> MarketState | None:
        if self.regular_start is None or self.regular_end is None:
            return None
        ts = now.timestamp()
        if self.regular_start <= ts < self.regular_end:
            return MarketState.OPEN
        if self.pre_start is not None and self.pre_start <= ts < self.regular_start:
            return MarketState.PRE
        if self.post_end is not None and self.regular_end <= ts < self.post_end:
            return MarketState.POST
        return MarketState.CLOSED
