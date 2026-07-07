"""Sensors: hub metrics and per-device status."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import UnitOfPower
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SlmCoordinator
from .models import DeviceConfig


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SlmCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        SlmSurplusSensor(coordinator, entry),
        SlmMarginalPriceSensor(coordinator, entry),
    ]
    entities.extend(
        SlmDeviceStatusSensor(coordinator, entry, cfg) for cfg in coordinator.devices
    )
    async_add_entities(entities)


class SlmHubSensor(CoordinatorEntity[SlmCoordinator], SensorEntity):
    """Base for hub-level sensors."""

    def __init__(self, coordinator: SlmCoordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Solar Load Manager",
            manufacturer="Solar Load Manager",
        )


class SlmSurplusSensor(SlmHubSensor):
    _attr_name = "Smoothed surplus"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator: SlmCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "smoothed_surplus")

    @property
    def native_value(self) -> float | None:
        value = (self.coordinator.data or {}).get("surplus")
        return round(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "hourly_balance_kwh": data.get("balance_kwh"),
            "bank_w": data.get("bank_w"),
            "budget_w": data.get("budget_w"),
        }


class SlmMarginalPriceSensor(SlmHubSensor):
    """Cost of one extra kWh right now under hourly net-billing."""

    _attr_name = "Marginal price"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash"

    def __init__(self, coordinator: SlmCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "price_score")

    @property
    def native_value(self) -> float | None:
        value = (self.coordinator.data or {}).get("price")
        return round(value, 4) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "source": data.get("price_source"),
            "sell_price": data.get("sell_price"),
            "buy_price": data.get("buy_price"),
        }


class SlmDeviceStatusSensor(CoordinatorEntity[SlmCoordinator], SensorEntity):
    """Decision status for one managed device."""

    _attr_icon = "mdi:lightning-bolt-circle"

    def __init__(
        self, coordinator: SlmCoordinator, entry: ConfigEntry, cfg: DeviceConfig
    ) -> None:
        super().__init__(coordinator)
        self._cfg = cfg
        self._attr_unique_id = f"{entry.entry_id}_{cfg.slug}_status"
        self._attr_name = f"{cfg.name} status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{cfg.slug}")},
            name=f"SLM {cfg.name}",
            manufacturer="Solar Load Manager",
        )

    @property
    def native_value(self) -> str | None:
        decision = ((self.coordinator.data or {}).get("decisions") or {}).get(self._cfg.name)
        if decision is None:
            return None
        if not self.coordinator.enabled.get(self._cfg.name, False):
            return "disabled"
        return decision.reason

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        decision = (data.get("decisions") or {}).get(self._cfg.name)
        inp = (data.get("inputs") or {}).get(self._cfg.name)
        attrs = {
            "priority": self._cfg.priority,
            "device_type": self._cfg.device_type,
            "rated_power": self._cfg.rated_power,
            "solar_only": self._cfg.solar_only,
        }
        if decision is not None:
            attrs.update(
                {
                    "should_be_on": decision.should_be_on,
                    "allocated_w": round(decision.allocated_w),
                    "target_amps": decision.target_amps,
                }
            )
            if decision.required_w is not None:
                attrs["required_w"] = round(decision.required_w)
                attrs["missing_w"] = round(decision.missing_w or 0)
        if inp is not None:
            attrs.update(
                {
                    "device_is_on": inp.is_on,
                    "manual_override": inp.override_active,
                    "boost": inp.boost_active,
                    "battery_full": inp.battery_full,
                }
            )
        return attrs
