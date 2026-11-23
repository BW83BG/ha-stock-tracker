"""Sensor platform for the Aktienkurse integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_NAME, PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import StockQuote
from .calc import MarketState, PortfolioResult, PositionResult, TaxResult
from .const import ATTRIBUTION, CONF_QUANTITY, CONF_SYMBOL, DOMAIN, SUBENTRY_TYPE_STOCK
from .coordinator import StockConfigEntry, StockCoordinator

# --------------------------------------------------------------------------- #
# Descriptions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class QuoteSensorDescription(SensorEntityDescription):
    """Sensor derived from the raw quote."""

    value_fn: Callable[[StockQuote], Any]
    use_quote_currency: bool = False


QUOTE_SENSORS: tuple[QuoteSensorDescription, ...] = (
    QuoteSensorDescription(
        key="price",
        translation_key="price",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        use_quote_currency=True,
        value_fn=lambda q: q.price,
    ),
    QuoteSensorDescription(
        key="change",
        translation_key="change",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        use_quote_currency=True,
        value_fn=lambda q: q.change,
    ),
    QuoteSensorDescription(
        key="change_percent",
        translation_key="change_percent",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=2,
        value_fn=lambda q: q.change_percent,
    ),
    QuoteSensorDescription(
        key="market_state",
        translation_key="market_state",
        device_class=SensorDeviceClass.ENUM,
        options=[s.value for s in MarketState],
        value_fn=lambda q: (
            s.value if (s := q.periods.state(dt_util.utcnow())) else None
        ),
    ),
)

type Valuation = PositionResult | PortfolioResult


@dataclass(frozen=True, kw_only=True)
class ValueSensorDescription(SensorEntityDescription):
    """Sensor derived from a position or the whole portfolio."""

    value_fn: Callable[[Valuation], float | None]
    is_money: bool = True


VALUE_SENSORS: tuple[ValueSensorDescription, ...] = (
    ValueSensorDescription(
        key="market_value",
        translation_key="market_value",
        value_fn=lambda v: v.market_value,
    ),
    ValueSensorDescription(
        key="cost_basis",
        translation_key="cost_basis",
        value_fn=lambda v: v.cost_basis,
    ),
    ValueSensorDescription(
        key="gain",
        translation_key="gain",
        value_fn=lambda v: v.gain,
    ),
    ValueSensorDescription(
        key="gain_percent",
        translation_key="gain_percent",
        native_unit_of_measurement=PERCENTAGE,
        is_money=False,
        value_fn=lambda v: v.gain_percent,
    ),
    ValueSensorDescription(
        key="tax",
        translation_key="tax",
        value_fn=lambda v: v.tax.total,
    ),
    ValueSensorDescription(
        key="gain_after_tax",
        translation_key="gain_after_tax",
        value_fn=lambda v: v.gain_after_tax,
    ),
    ValueSensorDescription(
        key="net_proceeds",
        translation_key="net_proceeds",
        value_fn=lambda v: v.net_proceeds,
    ),
    ValueSensorDescription(
        key="day_change_value",
        translation_key="day_change_value",
        value_fn=lambda v: v.day_change,
    ),
)


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StockConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensors for every stock subentry and the portfolio."""
    coordinator = entry.runtime_data
    has_holdings = False

    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_STOCK:
            continue
        entities: list[SensorEntity] = [
            QuoteSensor(coordinator, subentry, d) for d in QUOTE_SENSORS
        ]
        if float(subentry.data.get(CONF_QUANTITY) or 0) > 0:
            has_holdings = True
            entities += [PositionSensor(coordinator, subentry, d) for d in VALUE_SENSORS]
        async_add_entities(entities, config_subentry_id=subentry.subentry_id)

    if has_holdings:
        async_add_entities(
            [PortfolioSensor(coordinator, entry, d) for d in VALUE_SENSORS]
        )


def _stock_device(subentry: ConfigSubentry) -> DeviceInfo:
    symbol = subentry.data[CONF_SYMBOL]
    return DeviceInfo(
        identifiers={(DOMAIN, symbol)},
        name=subentry.data.get(CONF_NAME) or symbol,
        model=symbol,
        manufacturer="Yahoo Finance",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=f"https://finance.yahoo.com/quote/{symbol}",
    )


def _tax_attributes(tax: TaxResult) -> dict[str, Any]:
    return {
        "taxable_amount": tax.taxable,
        "capital_gains_tax": tax.capital_gains_tax,
        "solidarity_surcharge": tax.solidarity,
        "church_tax": tax.church,
        "allowance_used": tax.allowance_used,
    }


class _Base(CoordinatorEntity[StockCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION


# --------------------------------------------------------------------------- #
# Per-stock quote sensors
# --------------------------------------------------------------------------- #


class QuoteSensor(_Base):
    """Price, change and market state of one stock."""

    entity_description: QuoteSensorDescription

    def __init__(
        self,
        coordinator: StockCoordinator,
        subentry: ConfigSubentry,
        description: QuoteSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._symbol: str = subentry.data[CONF_SYMBOL]
        self._attr_unique_id = f"{self._symbol}_{description.key}"
        self._attr_device_info = _stock_device(subentry)

    @property
    def _quote(self) -> StockQuote | None:
        return self.coordinator.data.quotes.get(self._symbol) if self.coordinator.data else None

    @property
    def available(self) -> bool:
        return super().available and self._quote is not None

    @property
    def native_value(self) -> Any:
        quote = self._quote
        return self.entity_description.value_fn(quote) if quote else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.use_quote_currency:
            return self._quote.currency if self._quote else None
        return self.entity_description.native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "price" or (quote := self._quote) is None:
            return None
        return {
            "symbol": quote.symbol,
            "name": quote.name,
            "exchange": quote.exchange,
            "currency": quote.currency,
            "previous_close": quote.previous_close,
            "day_high": quote.day_high,
            "day_low": quote.day_low,
            "volume": quote.volume,
            "52_week_high": quote.fifty_two_week_high,
            "52_week_low": quote.fifty_two_week_low,
            "market_time": quote.market_time.isoformat() if quote.market_time else None,
        }


# --------------------------------------------------------------------------- #
# Value sensors (position / portfolio)
# --------------------------------------------------------------------------- #


class _ValueSensor(_Base):
    entity_description: ValueSensorDescription

    def __init__(
        self, coordinator: StockCoordinator, description: ValueSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 2
        if description.is_money:
            self._attr_native_unit_of_measurement = coordinator.currency

    @property
    def _valuation(self) -> Valuation | None:
        raise NotImplementedError

    @property
    def available(self) -> bool:
        return super().available and self._valuation is not None

    @property
    def native_value(self) -> float | None:
        v = self._valuation
        return self.entity_description.value_fn(v) if v else None


class PositionSensor(_ValueSensor):
    """Value / gain / tax of one holding if it were sold now."""

    def __init__(
        self,
        coordinator: StockCoordinator,
        subentry: ConfigSubentry,
        description: ValueSensorDescription,
    ) -> None:
        super().__init__(coordinator, description)
        self._symbol: str = subentry.data[CONF_SYMBOL]
        self._attr_unique_id = f"{self._symbol}_{description.key}"
        self._attr_device_info = _stock_device(subentry)

    @property
    def _valuation(self) -> PositionResult | None:
        data = self.coordinator.data
        return data.positions.get(self._symbol) if data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        p = self._valuation
        if p is None:
            return None
        key = self.entity_description.key
        if key == "market_value":
            return {
                "quantity": p.quantity,
                "price": p.price,
                "fx_rate": p.fx_rate,
                "sell_fee": p.sell_fee,
            }
        if key == "tax":
            return {
                **_tax_attributes(p.tax),
                "taxable_gain_before_allowance": p.taxable_gain,
                "scenario": "only_this_position",
            }
        return None


class PortfolioSensor(_ValueSensor):
    """Totals over all holdings if the whole portfolio were sold now."""

    def __init__(
        self,
        coordinator: StockCoordinator,
        entry: StockConfigEntry,
        description: ValueSensorDescription,
    ) -> None:
        super().__init__(coordinator, description)
        self._attr_unique_id = f"{entry.entry_id}_portfolio_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_portfolio")},
            name="Depot",
            manufacturer="Aktienkurse",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _valuation(self) -> PortfolioResult | None:
        return self.coordinator.data.portfolio if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        p = self._valuation
        if p is None:
            return None
        key = self.entity_description.key
        if key == "market_value":
            return {"positions": len(p.positions), "sell_fees": p.sell_fees}
        if key == "tax":
            return {**_tax_attributes(p.tax), "scenario": "whole_portfolio"}
        return None
