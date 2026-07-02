"""Solar Load Manager integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import ATTR_DEVICE, ATTR_MINUTES, DOMAIN, SERVICE_BOOST
from .coordinator import SlmCoordinator

PLATFORMS = ["sensor", "switch"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

BOOST_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE): cv.string,
        vol.Optional(ATTR_MINUTES, default=60): vol.Coerce(float),
    }
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register services."""

    async def handle_boost(call: ServiceCall) -> None:
        name = call.data[ATTR_DEVICE]
        for coordinator in hass.data.get(DOMAIN, {}).values():
            if any(d.name == name for d in coordinator.devices):
                coordinator.start_boost(name, call.data[ATTR_MINUTES])
                await coordinator.async_request_refresh()
                return
        raise ServiceValidationError(f"Unknown device: {name}")

    hass.services.async_register(DOMAIN, SERVICE_BOOST, handle_boost, schema=BOOST_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the hub from a config entry."""
    coordinator = SlmCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
