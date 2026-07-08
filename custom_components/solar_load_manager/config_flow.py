"""Config flow for Solar Load Manager."""
from __future__ import annotations

from datetime import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BUY_PRICE_ATTRIBUTE,
    CONF_BUY_PRICE_SENSOR,
    CONF_CHEAP_PRICE,
    CONF_EXCLUSIVE,
    CONF_HOURLY_BALANCE_SENSOR,
    CONF_MAX_PRICE,
    CONF_SELL_PRICE_SENSOR,
    CONF_SOLAR_ONLY,
    CONF_BATTERY_LEVEL_SENSOR,
    CONF_CABLE_SENSOR,
    CONF_CHARGE_LIMIT_ENTITY,
    CONF_CHARGE_SWITCH,
    CONF_CHARGER_POWER_SENSOR,
    CONF_CURRENT_NUMBER,
    CONF_DEVICE_TYPE,
    CONF_DEVICES,
    CONF_ENTITY,
    CONF_HVAC_MODE,
    CONF_IMPORT_TOLERANCE,
    CONF_MAX_AMPS,
    CONF_MIN_AMPS,
    CONF_MIN_OFF,
    CONF_MIN_ON,
    CONF_MUST_RUN_ENABLED,
    CONF_MUST_RUN_END,
    CONF_MUST_RUN_START,
    CONF_NAME,
    CONF_ON_FACTOR,
    CONF_OVERRIDE_MINUTES,
    CONF_PHASES,
    CONF_PRIORITY,
    CONF_RATED_POWER,
    CONF_REFRESH_BUTTON,
    CONF_SMOOTHING_SECONDS,
    CONF_TARGET_TEMP,
    CONF_TARGET_TEMP_OFF,
    CONF_TEMP_ENTITY,
    CONF_VOLTAGE,
    DEFAULT_CHEAP_PRICE,
    DEFAULT_EXCLUSIVE,
    DEFAULT_MAX_PRICE,
    DEFAULT_IMPORT_TOLERANCE,
    DEFAULT_MAX_AMPS,
    DEFAULT_MIN_AMPS,
    DEFAULT_MIN_OFF,
    DEFAULT_MIN_ON,
    DEFAULT_ON_FACTOR,
    DEFAULT_OVERRIDE_MINUTES,
    DEFAULT_PHASES,
    DEFAULT_SMOOTHING_SECONDS,
    DEFAULT_VOLTAGE,
    DEVICE_TYPE_CLIMATE,
    DEVICE_TYPE_SWITCH,
    DEVICE_TYPE_TESLA,
    DEVICE_TYPES,
    DOMAIN,
)
from .models import DeviceConfig


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    parts = [int(p) for p in str(value).split(":")]
    while len(parts) < 3:
        parts.append(0)
    return time(parts[0], parts[1], parts[2])


def device_from_dict(data: dict[str, Any]) -> DeviceConfig:
    """Build a DeviceConfig from a stored options dict."""
    return DeviceConfig(
        name=data[CONF_NAME],
        priority=int(data.get(CONF_PRIORITY, 99)),
        device_type=data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_SWITCH),
        entity=data.get(CONF_ENTITY, ""),
        rated_power=float(data.get(CONF_RATED_POWER, 1500)),
        on_factor=float(data.get(CONF_ON_FACTOR, DEFAULT_ON_FACTOR)),
        min_on_minutes=float(data.get(CONF_MIN_ON, DEFAULT_MIN_ON)),
        min_off_minutes=float(data.get(CONF_MIN_OFF, DEFAULT_MIN_OFF)),
        max_price=float(data.get(CONF_MAX_PRICE, DEFAULT_MAX_PRICE)),
        solar_only=bool(data.get(CONF_SOLAR_ONLY, False)),
        hvac_mode=data.get(CONF_HVAC_MODE, "heat"),
        target_temp_off=bool(data.get(CONF_TARGET_TEMP_OFF, False)),
        temp_entity=data.get(CONF_TEMP_ENTITY, ""),
        target_temp=float(data[CONF_TARGET_TEMP]) if data.get(CONF_TARGET_TEMP) else None,
        must_run_enabled=bool(data.get(CONF_MUST_RUN_ENABLED, False)),
        must_run_start=_parse_time(data.get(CONF_MUST_RUN_START)),
        must_run_end=_parse_time(data.get(CONF_MUST_RUN_END)),
        charge_switch=data.get(CONF_CHARGE_SWITCH, ""),
        current_number=data.get(CONF_CURRENT_NUMBER, ""),
        cable_sensor=data.get(CONF_CABLE_SENSOR, ""),
        charger_power_sensor=data.get(CONF_CHARGER_POWER_SENSOR, ""),
        battery_level_sensor=data.get(CONF_BATTERY_LEVEL_SENSOR, ""),
        charge_limit_entity=data.get(CONF_CHARGE_LIMIT_ENTITY, ""),
        refresh_button=data.get(CONF_REFRESH_BUTTON, ""),
        phases=int(data.get(CONF_PHASES, DEFAULT_PHASES)),
        voltage=float(data.get(CONF_VOLTAGE, DEFAULT_VOLTAGE)),
        min_amps=int(data.get(CONF_MIN_AMPS, DEFAULT_MIN_AMPS)),
        max_amps=int(data.get(CONF_MAX_AMPS, DEFAULT_MAX_AMPS)),
    )


def _sensor_selector() -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


HUB_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOURLY_BALANCE_SENSOR): _sensor_selector(),
        vol.Required(CONF_SELL_PRICE_SENSOR): _sensor_selector(),
        vol.Required(CONF_BUY_PRICE_SENSOR): _sensor_selector(),
        vol.Optional(CONF_BUY_PRICE_ATTRIBUTE, default="price"): str,
        vol.Optional(CONF_SMOOTHING_SECONDS, default=DEFAULT_SMOOTHING_SECONDS): vol.Coerce(int),
        vol.Optional(CONF_IMPORT_TOLERANCE, default=DEFAULT_IMPORT_TOLERANCE): vol.Coerce(float),
        vol.Optional(CONF_CHEAP_PRICE, default=DEFAULT_CHEAP_PRICE): vol.Coerce(float),
        vol.Optional(CONF_EXCLUSIVE, default=DEFAULT_EXCLUSIVE): bool,
        vol.Optional(CONF_OVERRIDE_MINUTES, default=DEFAULT_OVERRIDE_MINUTES): vol.Coerce(float),
    }
)


class SlmConfigFlow(ConfigFlow, domain=DOMAIN):
    """Hub setup flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(
                title="Solar Load Manager",
                data=user_input,
                options={CONF_DEVICES: []},
            )
        return self.async_show_form(step_id="user", data_schema=HUB_SCHEMA)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SlmOptionsFlow()


class SlmOptionsFlow(OptionsFlow):
    """Options: hub settings, add/edit/remove devices."""

    def __init__(self) -> None:
        self._edit_name: str | None = None
        self._pending: dict[str, Any] = {}

    @property
    def _devices(self) -> list[dict[str, Any]]:
        return list(self.config_entry.options.get(CONF_DEVICES, []))

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options=["hub", "add_device", "edit_device", "remove_device"],
        )

    async def async_step_hub(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            options = dict(self.config_entry.options)
            options.update(user_input)
            return self.async_create_entry(title="", data=options)
        current = {**self.config_entry.data, **self.config_entry.options}
        schema = self.add_suggested_values_to_schema(HUB_SCHEMA, current)
        return self.async_show_form(step_id="hub", data_schema=schema)

    # -- add / edit ---------------------------------------------------------

    def _device_schema(self, existing: dict[str, Any] | None = None) -> vol.Schema:
        """Step 1: identity and settings common to every device type."""
        e = existing or {}
        return vol.Schema(
            {
                vol.Required(CONF_NAME, default=e.get(CONF_NAME, "")): str,
                vol.Required(CONF_PRIORITY, default=e.get(CONF_PRIORITY, 1)): vol.Coerce(int),
                vol.Required(
                    CONF_DEVICE_TYPE, default=e.get(CONF_DEVICE_TYPE, DEVICE_TYPE_SWITCH)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=DEVICE_TYPES)
                ),
                vol.Optional(CONF_MIN_ON, default=e.get(CONF_MIN_ON, DEFAULT_MIN_ON)): vol.Coerce(
                    float
                ),
                vol.Optional(
                    CONF_MIN_OFF, default=e.get(CONF_MIN_OFF, DEFAULT_MIN_OFF)
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_MAX_PRICE, default=e.get(CONF_MAX_PRICE, DEFAULT_MAX_PRICE)
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_SOLAR_ONLY, default=e.get(CONF_SOLAR_ONLY, False)
                ): bool,
                vol.Optional(
                    CONF_MUST_RUN_ENABLED, default=e.get(CONF_MUST_RUN_ENABLED, False)
                ): bool,
                vol.Optional(
                    CONF_MUST_RUN_START, default=e.get(CONF_MUST_RUN_START, "14:00:00")
                ): selector.TimeSelector(),
                vol.Optional(
                    CONF_MUST_RUN_END, default=e.get(CONF_MUST_RUN_END, "16:00:00")
                ): selector.TimeSelector(),
            }
        )

    def _onoff_schema(self, existing: dict[str, Any] | None = None) -> vol.Schema:
        """Step 2 for switch/climate devices: what to control and its power."""
        e = existing or {}
        entity_key = (
            vol.Required(CONF_ENTITY, default=e[CONF_ENTITY])
            if e.get(CONF_ENTITY)
            else vol.Required(CONF_ENTITY)
        )
        return vol.Schema(
            {
                entity_key: selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=["switch", "climate", "input_boolean"]
                    )
                ),
                vol.Required(
                    CONF_RATED_POWER, default=e.get(CONF_RATED_POWER, 1500)
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_ON_FACTOR, default=e.get(CONF_ON_FACTOR, DEFAULT_ON_FACTOR)
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_HVAC_MODE, default=e.get(CONF_HVAC_MODE, "heat")
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=["heat", "cool", "auto", "heat_cool"])
                ),
                vol.Optional(
                    CONF_TARGET_TEMP_OFF, default=e.get(CONF_TARGET_TEMP_OFF, False)
                ): bool,
                **(
                    {vol.Optional(CONF_TEMP_ENTITY, default=e[CONF_TEMP_ENTITY]): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["sensor", "climate"])
                    )}
                    if e.get(CONF_TEMP_ENTITY)
                    else {vol.Optional(CONF_TEMP_ENTITY): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["sensor", "climate"])
                    )}
                ),
                vol.Optional(
                    CONF_TARGET_TEMP, default=e.get(CONF_TARGET_TEMP, 0)
                ): vol.Coerce(float),
            }
        )

    def _tesla_schema(self, existing: dict[str, Any] | None = None) -> vol.Schema:
        e = existing or {}

        def req(key: str):
            return vol.Required(key, default=e[key]) if e.get(key) else vol.Required(key)

        return vol.Schema(
            {
                req(CONF_CHARGE_SWITCH): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
                req(CONF_CURRENT_NUMBER): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="number")
                ),
                req(CONF_CABLE_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="binary_sensor")
                ),
                req(CONF_CHARGER_POWER_SENSOR): _sensor_selector(),
                **(
                    {vol.Optional(CONF_BATTERY_LEVEL_SENSOR, default=e[CONF_BATTERY_LEVEL_SENSOR]): _sensor_selector()}
                    if e.get(CONF_BATTERY_LEVEL_SENSOR)
                    else {vol.Optional(CONF_BATTERY_LEVEL_SENSOR): _sensor_selector()}
                ),
                **(
                    {vol.Optional(CONF_REFRESH_BUTTON, default=e[CONF_REFRESH_BUTTON]): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="button")
                    )}
                    if e.get(CONF_REFRESH_BUTTON)
                    else {vol.Optional(CONF_REFRESH_BUTTON): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="button")
                    )}
                ),
                **(
                    {vol.Optional(CONF_CHARGE_LIMIT_ENTITY, default=e[CONF_CHARGE_LIMIT_ENTITY]): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["number", "sensor", "input_number"])
                    )}
                    if e.get(CONF_CHARGE_LIMIT_ENTITY)
                    else {vol.Optional(CONF_CHARGE_LIMIT_ENTITY): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["number", "sensor", "input_number"])
                    )}
                ),
                vol.Optional(
                    CONF_PHASES, default=str(e.get(CONF_PHASES, DEFAULT_PHASES))
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=["1", "3"])
                ),
                vol.Optional(
                    CONF_VOLTAGE, default=e.get(CONF_VOLTAGE, DEFAULT_VOLTAGE)
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_MIN_AMPS, default=e.get(CONF_MIN_AMPS, DEFAULT_MIN_AMPS)
                ): vol.Coerce(int),
                vol.Optional(
                    CONF_MAX_AMPS, default=e.get(CONF_MAX_AMPS, DEFAULT_MAX_AMPS)
                ): vol.Coerce(int),
            }
        )

    async def async_step_add_device(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            if any(d[CONF_NAME] == user_input[CONF_NAME] for d in self._devices):
                errors["base"] = "name_exists"
            else:
                self._pending = user_input
                if user_input[CONF_DEVICE_TYPE] == DEVICE_TYPE_TESLA:
                    return await self.async_step_tesla()
                return await self.async_step_onoff()
        return self.async_show_form(
            step_id="add_device", data_schema=self._device_schema(), errors=errors
        )

    async def async_step_edit_device(self, user_input: dict[str, Any] | None = None):
        devices = self._devices
        if not devices:
            return self.async_abort(reason="no_devices")
        if self._edit_name is None:
            if user_input is not None:
                self._edit_name = user_input[CONF_NAME]
                existing = next(d for d in devices if d[CONF_NAME] == self._edit_name)
                return self.async_show_form(
                    step_id="edit_device", data_schema=self._device_schema(existing)
                )
            return self.async_show_form(
                step_id="edit_device",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_NAME): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=[d[CONF_NAME] for d in devices]
                            )
                        )
                    }
                ),
            )
        assert user_input is not None
        user_input[CONF_NAME] = self._edit_name  # name is the key; keep it stable
        existing = next(d for d in devices if d[CONF_NAME] == self._edit_name)
        self._pending = {**existing, **user_input}
        if user_input[CONF_DEVICE_TYPE] == DEVICE_TYPE_TESLA:
            return await self.async_step_tesla()
        return await self.async_step_onoff()

    async def async_step_onoff(self, user_input: dict[str, Any] | None = None):
        """Second step for switch/climate devices."""
        if user_input is not None:
            device = {**self._pending, **user_input}
            return self._save_device(device, replace=self._edit_name)
        return self.async_show_form(
            step_id="onoff", data_schema=self._onoff_schema(self._pending)
        )

    async def async_step_tesla(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            device = {**self._pending, **user_input}
            return self._save_device(device, replace=self._edit_name)
        return self.async_show_form(
            step_id="tesla", data_schema=self._tesla_schema(self._pending)
        )

    def _save_device(self, device: dict[str, Any], replace: str | None = None):
        devices = [d for d in self._devices if d[CONF_NAME] != (replace or device[CONF_NAME])]
        devices.append(device)
        options = dict(self.config_entry.options)
        options[CONF_DEVICES] = devices
        return self.async_create_entry(title="", data=options)

    async def async_step_remove_device(self, user_input: dict[str, Any] | None = None):
        devices = self._devices
        if not devices:
            return self.async_abort(reason="no_devices")
        if user_input is not None:
            options = dict(self.config_entry.options)
            options[CONF_DEVICES] = [
                d for d in devices if d[CONF_NAME] != user_input[CONF_NAME]
            ]
            return self.async_create_entry(title="", data=options)
        return self.async_show_form(
            step_id="remove_device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=[d[CONF_NAME] for d in devices])
                    )
                }
            ),
        )
