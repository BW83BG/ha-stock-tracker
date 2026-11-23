"""Constants for the Aktienkurse (stock_tracker) integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "stock_tracker"

SUBENTRY_TYPE_STOCK: Final = "stock"

# Subentry (per stock)
CONF_SYMBOL: Final = "symbol"
CONF_ASSET_TYPE: Final = "asset_type"
CONF_QUANTITY: Final = "quantity"
CONF_PURCHASE_PRICE: Final = "purchase_price"  # per share, portfolio currency
CONF_PURCHASE_FEES: Final = "purchase_fees"  # total, portfolio currency

# Main entry options (global)
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_CURRENCY: Final = "currency"
CONF_SELL_FEE_FIXED: Final = "sell_fee_fixed"
CONF_SELL_FEE_PERCENT: Final = "sell_fee_percent"
CONF_SELL_FEE_MIN: Final = "sell_fee_min"
CONF_CHURCH_TAX: Final = "church_tax"
CONF_ALLOWANCE: Final = "allowance_remaining"
CONF_STOCK_LOSS_POT: Final = "stock_loss_pot"
CONF_GENERAL_LOSS_POT: Final = "general_loss_pot"

CHURCH_TAX_OPTIONS: Final = {"none": 0.0, "8": 0.08, "9": 0.09}

DEFAULT_NAME: Final = "Aktienkurse"
DEFAULT_CURRENCY: Final = "EUR"
DEFAULT_SCAN_INTERVAL: Final = 300  # seconds
DEFAULT_ALLOWANCE: Final = 1000.0
MIN_SCAN_INTERVAL: Final = 60
MAX_SCAN_INTERVAL: Final = 86400

DEFAULT_OPTIONS: Final = {
    CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    CONF_CURRENCY: DEFAULT_CURRENCY,
    CONF_SELL_FEE_FIXED: 0.0,
    CONF_SELL_FEE_PERCENT: 0.0,
    CONF_SELL_FEE_MIN: 0.0,
    CONF_CHURCH_TAX: "none",
    CONF_ALLOWANCE: DEFAULT_ALLOWANCE,
    CONF_STOCK_LOSS_POT: 0.0,
    CONF_GENERAL_LOSS_POT: 0.0,
}

REQUEST_TIMEOUT: Final = 15

ATTRIBUTION: Final = "Daten von Yahoo Finance"


def scan_interval(seconds: int) -> timedelta:
    """Return the scan interval as timedelta."""
    return timedelta(seconds=seconds)
