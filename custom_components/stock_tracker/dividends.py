"""Distribution (dividend) calculations - pure, no Home Assistant imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .calc import PARTIAL_EXEMPTION, AssetType, TaxSettings
from .plan import Execution, History, _first_on_or_after


@dataclass(frozen=True, slots=True)
class DividendPayment:
    """One distribution received (amounts in portfolio currency)."""

    ex_date: date
    per_share: float
    shares: float
    gross: float
    tax: float

    @property
    def net(self) -> float:
        return round(self.gross - self.tax, 2)


@dataclass(frozen=True, slots=True)
class DividendResult:
    """Distributions since the last reconciliation plus the manual total."""

    payments: list[DividendPayment] = field(default_factory=list)
    manual_net: float = 0.0  # entered by the user / folded earlier

    @property
    def auto_gross(self) -> float:
        return round(sum(p.gross for p in self.payments), 2)

    @property
    def auto_net(self) -> float:
        return round(sum(p.net for p in self.payments), 2)

    @property
    def total_net(self) -> float:
        return round(self.manual_net + self.auto_net, 2)


def evaluate_dividends(
    events: History,
    since: date,
    today: date,
    base_quantity: float,
    executions: list[Execution],
    fx_rates: History | None,
    divisor: int,
    asset_type: AssetType,
    tax: TaxSettings,
    manual_net: float = 0.0,
) -> DividendResult:
    """Distributions with ex-date in [since, today].

    Shares entitled on an ex-date: the base quantity (held since `since`) plus
    plan executions traded *before* the ex-date. Tax is estimated without the
    saver's allowance (Teilfreistellung applies to funds).
    """
    taxable_share = 1 - PARTIAL_EXEMPTION[asset_type]
    payments: list[DividendPayment] = []
    for ex_date, amount in events:
        if ex_date < since or ex_date > today:
            continue
        shares = base_quantity + sum(e.quantity for e in executions if e.traded < ex_date)
        if shares <= 0:
            continue
        rate = 1.0
        if fx_rates is not None:
            bar = _first_on_or_after(fx_rates, ex_date) or (fx_rates[-1] if fx_rates else None)
            if bar is None:
                continue
            rate = bar[1]
        per_share = amount / divisor * rate
        gross = round(shares * per_share, 2)
        payments.append(
            DividendPayment(
                ex_date=ex_date,
                per_share=round(per_share, 6),
                shares=round(shares, 6),
                gross=gross,
                tax=round(gross * taxable_share * tax.total_rate, 2),
            )
        )
    return DividendResult(payments=payments, manual_net=manual_net)


def trailing_yield(events: History, price: float | None, today: date) -> float | None:
    """Distributions of the last 12 months relative to the price (same unit)."""
    if not price:
        return None
    cutoff = today - timedelta(days=365)
    total = sum(a for d, a in events if cutoff < d <= today)
    return round(total / price * 100, 2)
