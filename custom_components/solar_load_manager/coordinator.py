"""Coordinator: reads inputs, runs the allocator, commands devices."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CHEAP_SCORE,
    CONF_DEVICES,
    CONF_IMPORT_TOLERANCE,
    CONF_OVERRIDE_MINUTES,
    CONF_PRICE_MAX_SENSOR,
    CONF_PRICE_MIN_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_SMOOTHING_SECONDS,
    CONF_SURPLUS_SENSOR,
    DEFAULT_CHEAP_SCORE,
    DEFAULT_IMPORT_TOLERANCE,
    DEFAULT_OVERRIDE_MINUTES,
    DEFAULT_SMOOTHING_SECONDS,
    DEVICE_TYPE_CLIMATE,
    DEVICE_TYPE_TESLA,
    DOMAIN,
    UPDATE_INTERVAL_SECONDS,
)
from .models import Decision, DeviceConfig, DeviceInput, allocate, price_score

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
        self._ema: float | None = None
        self._ema_ts: datetime | None = None
        # runtime state, keyed by device name
        self.enabled: dict[str, bool] = {d.name: False for d in self.devices}
        self._last_command: dict[str, tuple[bool, datetime]] = {}
        self._override_until: dict[str, datetime] = {}
        self._boost_until: dict[str, datetime] = {}

    # -- helpers -----------------------------------------------------------

    def _conf(self, key: str, default):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def _float_state(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

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

        raw_surplus = self._float_state(self._conf(CONF_SURPLUS_SENSOR, None))
        surplus = self._update_ema(raw_surplus, now_utc)

        score = price_score(
            self._float_state(self._conf(CONF_PRICE_SENSOR, None)),
            self._float_state(self._conf(CONF_PRICE_MIN_SENSOR, None)),
            self._float_state(self._conf(CONF_PRICE_MAX_SENSOR, None)),
        )

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
            pairs.append((cfg, inp))

        decisions = allocate(
            pairs,
            surplus if surplus is not None else 0.0,
            score,
            float(self._conf(CONF_CHEAP_SCORE, DEFAULT_CHEAP_SCORE)),
            float(self._conf(CONF_IMPORT_TOLERANCE, DEFAULT_IMPORT_TOLERANCE)),
            now_local,
        )

        if surplus is not None:
            for cfg, inp in pairs:
                await self._apply(cfg, inp, decisions[cfg.name], now_utc)

        return {
            "surplus": surplus,
            "raw_surplus": raw_surplus,
            "price_score": score,
            "decisions": decisions,
            "inputs": {cfg.name: inp for cfg, inp in pairs},
        }

    def _update_ema(self, raw: float | None, now: datetime) -> float | None:
        if raw is None:
            return self._ema
        window = float(self._conf(CONF_SMOOTHING_SECONDS, DEFAULT_SMOOTHING_SECONDS))
        if self._ema is None or self._ema_ts is None or window <= 0:
            self._ema = raw
        else:
            dt = (now - self._ema_ts).total_seconds()
            alpha = dt / (window + dt)
            self._ema += alpha * (raw - self._ema)
        self._ema_ts = now
        return self._ema

    # -- actuation ---------------------------------------------------------

    async def _apply(
        self, cfg: DeviceConfig, inp: DeviceInput, decision: Decision, now: datetime
    ) -> None:
        if not inp.enabled or not inp.available or inp.override_active:
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
        elif not decision.should_be_on and inp.is_on:
            await self.hass.services.async_call(
                "switch", "turn_off", {"entity_id": cfg.charge_switch}, blocking=True
            )
            self._last_command[cfg.name] = (False, now)

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
