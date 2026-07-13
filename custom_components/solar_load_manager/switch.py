"""Enable switches for managed devices."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SlmCoordinator
from .models import DeviceConfig


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SlmCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = []
    for cfg in coordinator.devices:
        entities.append(SlmEnableSwitch(coordinator, entry, cfg))
        entities.append(SlmSolarOnlySwitch(coordinator, entry, cfg))
    async_add_entities(entities)


class SlmEnableSwitch(CoordinatorEntity[SlmCoordinator], SwitchEntity, RestoreEntity):
    """Arms/disarms automation for one managed device."""

    _attr_icon = "mdi:robot"

    def __init__(
        self, coordinator: SlmCoordinator, entry: ConfigEntry, cfg: DeviceConfig
    ) -> None:
        super().__init__(coordinator)
        self._cfg = cfg
        self._attr_unique_id = f"{entry.entry_id}_{cfg.slug}_enable"
        self._attr_name = f"{cfg.name} automation"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{cfg.slug}")},
            name=f"SLM {cfg.name}",
            manufacturer="Solar Load Manager",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self.coordinator.set_enabled(self._cfg.name, last.state == "on")

    @property
    def is_on(self) -> bool:
        return self.coordinator.enabled.get(self._cfg.name, False)

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.set_enabled(self._cfg.name, True)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.set_enabled(self._cfg.name, False)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class SlmSolarOnlySwitch(CoordinatorEntity[SlmCoordinator], SwitchEntity, RestoreEntity):
    """Runtime toggle for the device's solar_only mode.

    Overrides the configured value; the configured value is the default
    until the switch is first toggled (then the last state is restored).
    """

    _attr_icon = "mdi:weather-sunny"

    def __init__(
        self, coordinator: SlmCoordinator, entry: ConfigEntry, cfg: DeviceConfig
    ) -> None:
        super().__init__(coordinator)
        self._cfg = cfg
        self._attr_unique_id = f"{entry.entry_id}_{cfg.slug}_solar_only"
        self._attr_name = f"{cfg.name} solar only"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{cfg.slug}")},
            name=f"SLM {cfg.name}",
            manufacturer="Solar Load Manager",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self.coordinator.set_solar_only(self._cfg.name, last.state == "on")

    @property
    def is_on(self) -> bool:
        return self.coordinator.solar_only.get(self._cfg.name, self._cfg.solar_only)

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.set_solar_only(self._cfg.name, True)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.set_solar_only(self._cfg.name, False)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
