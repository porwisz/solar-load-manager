"""Editable settings exposed as number entities.

Only the settings that get tuned day to day live here; the rest stay in the
options flow. Writing one updates the config entry and is adopted in place,
so a slider does not reload the integration (see
`SlmCoordinator.try_apply_options`).
"""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_BOOST_TEMP,
    CONF_MAX_PRICE,
    CONF_MIN_OFF,
    CONF_MIN_ON,
    CONF_RESTORE_TEMP,
    CONF_TREND_FACTOR,
    DEFAULT_TREND_FACTOR,
    DEVICE_TYPE_SETPOINT,
    DOMAIN,
)
from .coordinator import SlmCoordinator
from .models import DeviceConfig


@dataclass(frozen=True)
class NumberSpec:
    """One editable setting.

    `key` is both the option key and the matching DeviceConfig attribute.
    """

    key: str
    name: str
    icon: str
    min_value: float
    max_value: float
    step: float
    unit: str | None = None
    device_types: tuple[str, ...] | None = None  # None = every device type


DEVICE_NUMBERS: tuple[NumberSpec, ...] = (
    NumberSpec(CONF_MAX_PRICE, "Max price", "mdi:cash-lock", 0, 5, 0.01, "PLN/kWh"),
    NumberSpec(CONF_MIN_ON, "Min on time", "mdi:timer-play", 0, 120, 1, UnitOfTime.MINUTES),
    NumberSpec(CONF_MIN_OFF, "Min off time", "mdi:timer-pause", 0, 120, 1, UnitOfTime.MINUTES),
    NumberSpec(
        CONF_BOOST_TEMP, "Boost temperature", "mdi:thermometer-chevron-up",
        30, 75, 0.5, UnitOfTemperature.CELSIUS, (DEVICE_TYPE_SETPOINT,),
    ),
    NumberSpec(
        CONF_RESTORE_TEMP, "Restore temperature", "mdi:thermometer-chevron-down",
        20, 70, 0.5, UnitOfTemperature.CELSIUS, (DEVICE_TYPE_SETPOINT,),
    ),
)

HUB_NUMBERS: tuple[NumberSpec, ...] = (
    NumberSpec(CONF_TREND_FACTOR, "Trend weight", "mdi:chart-line-variant", 0, 5, 0.1),
)

HUB_DEFAULTS = {CONF_TREND_FACTOR: DEFAULT_TREND_FACTOR}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SlmCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = [
        SlmHubNumber(coordinator, entry, spec) for spec in HUB_NUMBERS
    ]
    for cfg in coordinator.devices:
        for spec in DEVICE_NUMBERS:
            if spec.device_types is None or cfg.device_type in spec.device_types:
                entities.append(SlmDeviceNumber(coordinator, entry, cfg, spec))
    async_add_entities(entities)


class SlmNumber(CoordinatorEntity[SlmCoordinator], NumberEntity):
    """Base for settings backed by the config entry."""

    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SlmCoordinator, spec: NumberSpec) -> None:
        super().__init__(coordinator)
        self._spec = spec
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_native_min_value = spec.min_value
        self._attr_native_max_value = spec.max_value
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = spec.unit


class SlmHubNumber(SlmNumber):
    """A hub-level setting."""

    def __init__(
        self, coordinator: SlmCoordinator, entry: ConfigEntry, spec: NumberSpec
    ) -> None:
        super().__init__(coordinator, spec)
        self._attr_unique_id = f"{entry.entry_id}_{spec.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Solar Load Manager",
            manufacturer="Solar Load Manager",
        )

    @property
    def native_value(self) -> float:
        return float(self.coordinator.option(self._spec.key, HUB_DEFAULTS[self._spec.key]))

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.async_set_hub_option(self._spec.key, value)


class SlmDeviceNumber(SlmNumber):
    """A per-device setting."""

    def __init__(
        self,
        coordinator: SlmCoordinator,
        entry: ConfigEntry,
        cfg: DeviceConfig,
        spec: NumberSpec,
    ) -> None:
        super().__init__(coordinator, spec)
        self._device = cfg.name
        self._attr_unique_id = f"{entry.entry_id}_{cfg.slug}_{spec.key}"
        self._attr_name = f"{cfg.name} {spec.name.lower()}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{cfg.slug}")},
            name=f"SLM {cfg.name}",
            manufacturer="Solar Load Manager",
        )

    @property
    def native_value(self) -> float | None:
        cfg = self.coordinator.device_config(self._device)
        return None if cfg is None else float(getattr(cfg, self._spec.key))

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.async_set_device_option(self._device, self._spec.key, value)
