"""Coordinator: reads inputs, runs the allocator, commands devices."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BUY_PRICE_ATTRIBUTE,
    CONF_BUY_PRICE_SENSOR,
    CONF_CHEAP_PRICE,
    CONF_DEVICES,
    CONF_EXCLUSIVE,
    CONF_HOURLY_BALANCE_SENSOR,
    CONF_IMPORT_TOLERANCE,
    CONF_OVERRIDE_MINUTES,
    CONF_SELL_PRICE_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_SMOOTHING_SECONDS,
    DEFAULT_CHEAP_PRICE,
    DEFAULT_EXCLUSIVE,
    DEFAULT_IMPORT_TOLERANCE,
    DEFAULT_OVERRIDE_MINUTES,
    DEFAULT_SMOOTHING_SECONDS,
    DEVICE_TYPE_CLIMATE,
    DEVICE_TYPE_TESLA,
    DOMAIN,
    UPDATE_INTERVAL_SECONDS,
)
from .models import Decision, DeviceConfig, DeviceInput, allocate, marginal_price

_LOGGER = logging.getLogger(__name__)


def device_configs_from_entry(entry: ConfigEntry) -> list[DeviceConfig]:
    """Build DeviceConfig list from the entry options."""
    from .config_flow import device_from_dict  # local import avoids cycle

    return [device_from_dict(d) for d in entry.options.get(CONF_DEVICES, [])]


class SlmCoordinator(DataUpdateCoordinator[dict]):
    """Single decision loop for all managed devices."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.entry = entry
        self.devices = device_configs_from_entry(entry)
        # net power derived from the hourly balance sensor
        self._ema: float | None = None
        self._last_balance: float | None = None
        self._last_balance_ts: datetime | None = None
        # runtime state, keyed by device name
        self.enabled: dict[str, bool] = {d.name: False for d in self.devices}
        self._last_command: dict[str, tuple[bool, datetime]] = {}
        self._override_until: dict[str, datetime] = {}
        self._boost_until: dict[str, datetime] = {}

    # -- helpers -----------------------------------------------------------

    def _conf(self, key: str, default):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def _float_state(self, entity_id: str | None, attribute: str | None = None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        value = state.attributes.get(attribute) if attribute else state.state
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _buy_price(self) -> float | None:
        """Tariff price: numeric state, or the configured attribute (e.g. 'price')."""
        entity_id = self._conf(CONF_BUY_PRICE_SENSOR, None)
        direct = self._float_state(entity_id)
        if direct is not None:
            return direct
        attribute = self._conf(CONF_BUY_PRICE_ATTRIBUTE, "price")
        return self._float_state(entity_id, attribute)

    def _device_is_on(self, cfg: DeviceConfig) -> bool | None:
        entity = cfg.charge_switch if cfg.device_type == DEVICE_TYPE_TESLA else cfg.entity
        state = self.hass.states.get(entity)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        return state.state not in ("off",)

    def set_enabled(self, name: str, value: bool) -> None:
        self.enabled[name] = value

    def start_boost(self, name: str, minutes: float) -> None:
        self._boost_until[name] = dt_util.utcnow() + timedelta(minutes=minutes)

    # -- decision loop -----------------------------------------------------

    async def _async_update_data(self) -> dict:
        now_utc = dt_util.utcnow()
        now_local = dt_util.now()

        balance_kwh = self._float_state(self._conf(CONF_HOURLY_BALANCE_SENSOR, None))
        net_w = self._update_net_power(balance_kwh, now_local)

        # Banked hourly balance, spread over the rest of the hour: under
        # hourly net-billing, surplus accumulated earlier this hour can be
        # consumed until the hour ends without paying the tariff.
        remaining_h = max(0.1, (60 - now_local.minute) / 60)
        bank_w = (balance_kwh or 0.0) * 1000 / remaining_h
        budget_w = (net_w if net_w is not None else 0.0) + bank_w

        sell_price = self._float_state(
            self._conf(CONF_SELL_PRICE_SENSOR, self._conf(CONF_PRICE_SENSOR, None))
        )
        buy_price = self._buy_price()
        price, price_source = marginal_price(balance_kwh, sell_price, buy_price)

        override_minutes = float(self._conf(CONF_OVERRIDE_MINUTES, DEFAULT_OVERRIDE_MINUTES))
        pairs: list[tuple[DeviceConfig, DeviceInput]] = []
        for cfg in self.devices:
            is_on = self._device_is_on(cfg)
            inp = DeviceInput(
                enabled=self.enabled.get(cfg.name, False),
                available=is_on is not None,
                is_on=bool(is_on),
            )
            last = self._last_command.get(cfg.name)
            if last is None:
                # No in-memory command record (fresh start or options reload):
                # fall back to the entity's own last state change so minimum
                # on/off times survive reloads and restarts.
                entity = cfg.charge_switch if cfg.device_type == DEVICE_TYPE_TESLA else cfg.entity
                state = self.hass.states.get(entity)
                if state is not None:
                    inp.minutes_since_command = (
                        now_utc - state.last_changed
                    ).total_seconds() / 60
            if last is not None:
                commanded_on, when = last
                inp.minutes_since_command = (now_utc - when).total_seconds() / 60
                # External change detection: state no longer matches what we
                # commanded, and enough time passed for our command to settle.
                if (
                    is_on is not None
                    and is_on != commanded_on
                    and inp.minutes_since_command > 2
                    and cfg.name not in self._override_until
                ):
                    self._override_until[cfg.name] = now_utc + timedelta(minutes=override_minutes)
            until = self._override_until.get(cfg.name)
            if until is not None:
                if now_utc >= until:
                    self._override_until.pop(cfg.name, None)
                    self._last_command.pop(cfg.name, None)
                    inp.minutes_since_command = 1e9
                else:
                    inp.override_active = True
            boost = self._boost_until.get(cfg.name)
            if boost is not None:
                if now_utc >= boost:
                    self._boost_until.pop(cfg.name, None)
                else:
                    inp.boost_active = True
            if cfg.device_type == DEVICE_TYPE_TESLA:
                cable = self.hass.states.get(cfg.cable_sensor)
                inp.cable_connected = cable is not None and cable.state == "on"
                inp.own_power_w = (self._float_state(cfg.charger_power_sensor) or 0.0) * 1000
                if cfg.battery_level_sensor and cfg.charge_limit_entity:
                    level = self._float_state(cfg.battery_level_sensor)
                    limit = self._float_state(cfg.charge_limit_entity)
                    if level is not None and limit is not None:
                        inp.battery_full = level >= limit
            if cfg.target_temp_off:
                inp.temp_reached = self._temp_reached(cfg)
            pairs.append((cfg, inp))

        decisions = allocate(
            pairs,
            budget_w,
            price,
            price_source,
            float(self._conf(CONF_CHEAP_PRICE, DEFAULT_CHEAP_PRICE)),
            float(self._conf(CONF_IMPORT_TOLERANCE, DEFAULT_IMPORT_TOLERANCE)),
            now_local,
            exclusive=bool(self._conf(CONF_EXCLUSIVE, DEFAULT_EXCLUSIVE)),
        )

        if balance_kwh is not None:
            for cfg, inp in pairs:
                await self._apply(cfg, inp, decisions[cfg.name], now_utc)

        return {
            "surplus": net_w,
            "balance_kwh": balance_kwh,
            "bank_w": round(bank_w) if balance_kwh is not None else None,
            "budget_w": round(budget_w) if balance_kwh is not None else None,
            "price": price,
            "price_source": price_source,
            "sell_price": sell_price,
            "buy_price": buy_price,
            "decisions": decisions,
            "inputs": {cfg.name: inp for cfg, inp in pairs},
        }

    def _update_net_power(self, balance_kwh: float | None, now: datetime) -> float | None:
        """Derive smoothed net power [W] from the hourly balance sensor."""
        if balance_kwh is None:
            return self._ema
        if self._last_balance is not None and self._last_balance_ts is not None:
            dt = (now - self._last_balance_ts).total_seconds()
            # Skip the sample when the hour rolled over (sensor resets) or
            # time didn't advance.
            if 0 < dt < 1800 and now.hour == self._last_balance_ts.hour:
                raw_w = (balance_kwh - self._last_balance) * 3_600_000 / dt
                window = float(self._conf(CONF_SMOOTHING_SECONDS, DEFAULT_SMOOTHING_SECONDS))
                if self._ema is None or window <= 0:
                    self._ema = raw_w
                else:
                    alpha = dt / (window + dt)
                    self._ema += alpha * (raw_w - self._ema)
        self._last_balance = balance_kwh
        self._last_balance_ts = now
        return self._ema

    def _temp_reached(self, cfg: DeviceConfig) -> bool:
        """True when the device's current temperature is at/above its target."""
        source = cfg.temp_entity or cfg.entity
        state = self.hass.states.get(source)
        if state is None or state.state in ("unknown", "unavailable"):
            return False
        current = state.attributes.get("current_temperature")
        if current is None:
            try:
                current = float(state.state)
            except (TypeError, ValueError):
                return False
        target = cfg.target_temp
        if not target:
            climate = self.hass.states.get(cfg.entity)
            if climate is not None:
                target = climate.attributes.get("temperature")
        if target is None:
            return False
        try:
            return float(current) >= float(target)
        except (TypeError, ValueError):
            return False

    # -- actuation ---------------------------------------------------------

    async def _apply(
        self, cfg: DeviceConfig, inp: DeviceInput, decision: Decision, now: datetime
    ) -> None:
        if not inp.enabled or not inp.available:
            return
        if inp.override_active and decision.reason != "target_reached":
            return
        if decision.reason in ("cable_disconnected",):
            return

        try:
            if cfg.device_type == DEVICE_TYPE_TESLA:
                await self._apply_tesla(cfg, inp, decision, now)
            elif decision.should_be_on and not inp.is_on:
                await self._turn_on(cfg)
                self._last_command[cfg.name] = (True, now)
            elif not decision.should_be_on and inp.is_on:
                await self._turn_off(cfg)
                self._last_command[cfg.name] = (False, now)
        except Exception:  # noqa: BLE001 - keep the loop alive for other devices
            _LOGGER.exception("Failed to control %s", cfg.name)

    async def _apply_tesla(
        self, cfg: DeviceConfig, inp: DeviceInput, decision: Decision, now: datetime
    ) -> None:
        if decision.should_be_on and decision.target_amps is not None:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": cfg.current_number, "value": decision.target_amps},
                blocking=True,
            )
            if not inp.is_on:
                await self.hass.services.async_call(
                    "switch", "turn_on", {"entity_id": cfg.charge_switch}, blocking=True
                )
                self._last_command[cfg.name] = (True, now)
                await self._press_refresh(cfg)
        elif not decision.should_be_on and inp.is_on:
            await self.hass.services.async_call(
                "switch", "turn_off", {"entity_id": cfg.charge_switch}, blocking=True
            )
            self._last_command[cfg.name] = (False, now)
            await self._press_refresh(cfg)

    async def _press_refresh(self, cfg: DeviceConfig) -> None:
        """Force a data refresh so sensors reflect the new charging state."""
        if not cfg.refresh_button:
            return
        await self.hass.services.async_call(
            "button", "press", {"entity_id": cfg.refresh_button}, blocking=False
        )

    async def _turn_on(self, cfg: DeviceConfig) -> None:
        if cfg.device_type == DEVICE_TYPE_CLIMATE:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": cfg.entity, "hvac_mode": cfg.hvac_mode},
                blocking=True,
            )
        else:
            await self.hass.services.async_call(
                "homeassistant", "turn_on", {"entity_id": cfg.entity}, blocking=True
            )

    async def _turn_off(self, cfg: DeviceConfig) -> None:
        if cfg.device_type == DEVICE_TYPE_CLIMATE:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": cfg.entity, "hvac_mode": "off"},
                blocking=True,
            )
        else:
            await self.hass.services.async_call(
                "homeassistant", "turn_off", {"entity_id": cfg.entity}, blocking=True
            )
