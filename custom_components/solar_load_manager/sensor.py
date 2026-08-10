"""Sensors: hub metrics and per-device status."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import UnitOfPower
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CHEAP_PRICE,
    CONF_EXCLUSIVE,
    CONF_EXPORT_MARGIN,
    CONF_IMPORT_TOLERANCE,
    CONF_OVERRIDE_MINUTES,
    CONF_SMOOTHING_SECONDS,
    CONF_TREND_FACTOR,
    DEFAULT_CHEAP_PRICE,
    DEFAULT_EXCLUSIVE,
    DEFAULT_EXPORT_MARGIN,
    DEFAULT_IMPORT_TOLERANCE,
    DEFAULT_OVERRIDE_MINUTES,
    DEFAULT_SMOOTHING_SECONDS,
    DEFAULT_TREND_FACTOR,
    DOMAIN,
)
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
            "trend_w": data.get("trend_w"),
            # hub settings, so the dashboard can show them without the options flow
            "smoothing_seconds": self.coordinator.option(
                CONF_SMOOTHING_SECONDS, DEFAULT_SMOOTHING_SECONDS
            ),
            "import_tolerance": self.coordinator.option(
                CONF_IMPORT_TOLERANCE, DEFAULT_IMPORT_TOLERANCE
            ),
            "cheap_price": self.coordinator.option(CONF_CHEAP_PRICE, DEFAULT_CHEAP_PRICE),
            "export_margin_kwh": self.coordinator.option(
                CONF_EXPORT_MARGIN, DEFAULT_EXPORT_MARGIN
            ),
            "exclusive_mode": self.coordinator.option(CONF_EXCLUSIVE, DEFAULT_EXCLUSIVE),
            "override_minutes": self.coordinator.option(
                CONF_OVERRIDE_MINUTES, DEFAULT_OVERRIDE_MINUTES
            ),
            "trend_factor": self.coordinator.option(CONF_TREND_FACTOR, DEFAULT_TREND_FACTOR),
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
        self._name = cfg.name
        self._fallback_cfg = cfg
        self._attr_unique_id = f"{entry.entry_id}_{cfg.slug}_status"
        self._attr_name = f"{cfg.name} status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{cfg.slug}")},
            name=f"SLM {cfg.name}",
            manufacturer="Solar Load Manager",
        )

    @property
    def _cfg(self) -> DeviceConfig:
        """Current config, which options edits replace in place."""
        return self.coordinator.device_config(self._name) or self._fallback_cfg

    @property
    def native_value(self) -> str | None:
        decision = ((self.coordinator.data or {}).get("decisions") or {}).get(self._name)
        if decision is None:
            return None
        if not self.coordinator.enabled.get(self._name, False):
            return "disabled"
        return decision.reason

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        cfg = self._cfg
        decision = (data.get("decisions") or {}).get(self._name)
        inp = (data.get("inputs") or {}).get(self._name)
        attrs = {
            "priority": cfg.priority,
            "device_type": cfg.device_type,
            "rated_power": cfg.rated_power,
            "solar_only": self.coordinator.solar_only.get(self._name, cfg.solar_only),
            # settings, so the dashboard can show them without the options flow
            "max_price": cfg.max_price,
            "on_factor": cfg.on_factor,
            "min_on_minutes": cfg.min_on_minutes,
            "min_off_minutes": cfg.min_off_minutes,
            "hysteresis": cfg.hysteresis,
            "target_temp_off": cfg.target_temp_off,
            "configured_target_temp": cfg.target_temp,
            "must_run": (
                f"{cfg.must_run_start}-{cfg.must_run_end}"
                if cfg.must_run_enabled and cfg.must_run_start and cfg.must_run_end
                else "off"
            ),
            "controlled_entity": cfg.charge_switch if cfg.device_type == "tesla" else cfg.entity,
        }
        if cfg.device_type == "setpoint":
            attrs["boost_temp"] = cfg.boost_temp
            attrs["restore_temp"] = cfg.restore_temp
        if cfg.device_type == "tesla":
            attrs["min_amps"] = cfg.min_amps
            attrs["max_amps"] = cfg.max_amps
            attrs["phases"] = cfg.phases
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
            if cfg.hysteresis > 0:
                attrs["current_temp"] = inp.current_temp
                attrs["target_temp"] = inp.effective_target
                attrs["start_below_temp"] = (
                    round(inp.effective_target - cfg.hysteresis, 1)
                    if inp.effective_target is not None
                    else None
                )
        return attrs
