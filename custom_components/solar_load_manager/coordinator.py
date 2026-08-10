"""Coordinator: reads inputs, runs the allocator, commands devices."""
from __future__ import annotations

import logging
from dataclasses import replace
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
    CONF_EXPORT_MARGIN,
    CONF_HOURLY_BALANCE_SENSOR,
    CONF_IMPORT_TOLERANCE,
    CONF_NAME,
    CONF_OVERRIDE_MINUTES,
    CONF_SELL_PRICE_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_SMOOTHING_SECONDS,
    CONF_TREND_FACTOR,
    DEFAULT_CHEAP_PRICE,
    DEFAULT_EXCLUSIVE,
    DEFAULT_EXPORT_MARGIN,
    DEFAULT_IMPORT_TOLERANCE,
    DEFAULT_OVERRIDE_MINUTES,
    DEFAULT_SMOOTHING_SECONDS,
    DEFAULT_TREND_FACTOR,
    STARTUP_SETTLE_SECONDS,
    TREND_SLOW_MULTIPLIER,
    DEVICE_TYPE_CLIMATE,
    DEVICE_TYPE_SETPOINT,
    DEVICE_TYPE_TESLA,
    DOMAIN,
    UPDATE_INTERVAL_SECONDS,
)
from .models import (
    Decision,
    DeviceConfig,
    DeviceInput,
    allocate,
    marginal_price,
    power_to_watts,
)

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
        # slower average of the same signal; fast - slow is the trend
        self._ema_slow: float | None = None
        self._last_balance: float | None = None
        self._last_balance_ts: datetime | None = None
        self._started = dt_util.now()
        # the trend is only meaningful once the slow average has seen a full
        # window of samples; until then it stays at zero
        self._trend_ready_at: datetime | None = None
        # runtime state, keyed by device name
        self.enabled: dict[str, bool] = {d.name: False for d in self.devices}
        # runtime override of the configured solar_only flag (switch entity)
        self.solar_only: dict[str, bool] = {d.name: d.solar_only for d in self.devices}
        self._last_command: dict[str, tuple[bool, datetime]] = {}
        self._override_until: dict[str, datetime] = {}
        self._boost_until: dict[str, datetime] = {}
        # setpoint devices: the setpoint seen right before boosting, so the
        # schedule's value can be restored when the boost ends
        self._restore_temp: dict[str, float] = {}

    # -- helpers -----------------------------------------------------------

    def _conf(self, key: str, default):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def option(self, key: str, default):
        """Current value of a hub-level option."""
        return self._conf(key, default)

    def device_config(self, name: str) -> DeviceConfig | None:
        """Current config of a managed device, by name."""
        for cfg in self.devices:
            if cfg.name == name:
                return cfg
        return None

    # -- options -----------------------------------------------------------

    def try_apply_options(self) -> bool:
        """Adopt changed options in place, without reloading the entry.

        Returns False when the change adds, removes or retypes a device — the
        entity set then has to be rebuilt, which only a reload can do. Applying
        in place keeps the smoothing/trend averages and the anti-cycling
        timers alive, so tweaking a setting from the dashboard does not reset
        the decision loop.
        """
        new_devices = device_configs_from_entry(self.entry)
        signature = [(d.name, d.device_type) for d in new_devices]
        if signature != [(d.name, d.device_type) for d in self.devices]:
            return False
        self.devices = new_devices
        for cfg in new_devices:
            # Runtime overrides win over the configured defaults; only devices
            # seen for the first time take their value from the config.
            self.enabled.setdefault(cfg.name, False)
            self.solar_only.setdefault(cfg.name, cfg.solar_only)
        return True

    def async_set_hub_option(self, key: str, value) -> None:
        """Persist a hub-level option."""
        options = dict(self.entry.options)
        options[key] = value
        self.hass.config_entries.async_update_entry(self.entry, options=options)

    def async_set_device_option(self, name: str, key: str, value) -> None:
        """Persist one option of one managed device."""
        options = dict(self.entry.options)
        devices = [dict(d) for d in options.get(CONF_DEVICES, [])]
        for device in devices:
            if device.get(CONF_NAME) == name:
                device[key] = value
                break
        else:
            raise ValueError(f"Unknown device: {name}")
        options[CONF_DEVICES] = devices
        self.hass.config_entries.async_update_entry(self.entry, options=options)

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

    def _power_w(self, entity_id: str | None) -> float | None:
        """Read a power sensor in watts, honouring its unit_of_measurement."""
        value = self._float_state(entity_id)
        if value is None:
            return None
        state = self.hass.states.get(entity_id)
        unit = state.attributes.get("unit_of_measurement") if state else None
        return power_to_watts(value, unit)

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
        if cfg.device_type == DEVICE_TYPE_SETPOINT:
            # "on" means the boost setpoint is currently applied.
            try:
                setpoint = float(state.attributes.get("temperature"))
            except (TypeError, ValueError):
                return None
            return setpoint >= cfg.boost_temp - 0.1
        return state.state not in ("off",)

    def set_enabled(self, name: str, value: bool) -> None:
        self.enabled[name] = value

    def set_solar_only(self, name: str, value: bool) -> None:
        self.solar_only[name] = value

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
        minutes_to_hour_end = 60 - now_local.minute - now_local.second / 60
        remaining_h = max(0.1, (60 - now_local.minute) / 60)
        bank_w = (balance_kwh or 0.0) * 1000 / remaining_h
        budget_w = (net_w if net_w is not None else 0.0) + bank_w
        trend_w = self._trend_w(now_local)

        sell_price = self._float_state(
            self._conf(CONF_SELL_PRICE_SENSOR, self._conf(CONF_PRICE_SENSOR, None))
        )
        buy_price = self._buy_price()
        price, price_source = marginal_price(
            balance_kwh,
            sell_price,
            buy_price,
            float(self._conf(CONF_EXPORT_MARGIN, DEFAULT_EXPORT_MARGIN)),
        )

        override_minutes = float(self._conf(CONF_OVERRIDE_MINUTES, DEFAULT_OVERRIDE_MINUTES))
        pairs: list[tuple[DeviceConfig, DeviceInput]] = []
        for cfg in self.devices:
            solar_only = self.solar_only.get(cfg.name, cfg.solar_only)
            if solar_only != cfg.solar_only:
                cfg = replace(cfg, solar_only=solar_only)
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
                inp.own_power_w = self._power_w(cfg.charger_power_sensor) or 0.0
                if cfg.battery_level_sensor and cfg.charge_limit_entity:
                    level = self._float_state(cfg.battery_level_sensor)
                    limit = self._float_state(cfg.charge_limit_entity)
                    if level is not None and limit is not None:
                        inp.battery_full = level >= limit
            if cfg.target_temp_off or cfg.hysteresis > 0:
                inp.current_temp = self._current_temp(cfg)
                inp.effective_target = self._effective_target(cfg)
            if cfg.target_temp_off:
                inp.temp_reached = (
                    inp.current_temp is not None
                    and inp.effective_target is not None
                    and inp.current_temp >= inp.effective_target
                )
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
            bank_w=bank_w,
            minutes_to_hour_end=minutes_to_hour_end,
            trend_w=trend_w,
            trend_factor=float(self._conf(CONF_TREND_FACTOR, DEFAULT_TREND_FACTOR)),
        )

        if balance_kwh is not None:
            for cfg, inp in pairs:
                await self._apply(cfg, inp, decisions[cfg.name], now_utc)

        return {
            "surplus": net_w,
            "balance_kwh": balance_kwh,
            "bank_w": round(bank_w) if balance_kwh is not None else None,
            "budget_w": round(budget_w) if balance_kwh is not None else None,
            "trend_w": round(trend_w) if balance_kwh is not None else None,
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
        # Right after startup the balance sensor may still be restoring a stale
        # value; the jump to its real value would read as a huge burst of power
        # and seed both averages with it.
        settling = (now - self._started).total_seconds() < STARTUP_SETTLE_SECONDS
        if not settling and self._last_balance is not None and self._last_balance_ts is not None:
            dt = (now - self._last_balance_ts).total_seconds()
            # Skip the sample when the hour rolled over (sensor resets) or
            # time didn't advance.
            if 0 < dt < 1800 and now.hour == self._last_balance_ts.hour:
                raw_w = (balance_kwh - self._last_balance) * 3_600_000 / dt
                window = float(self._conf(CONF_SMOOTHING_SECONDS, DEFAULT_SMOOTHING_SECONDS))
                if self._ema is None or window <= 0:
                    self._ema = raw_w
                    self._ema_slow = raw_w
                    self._trend_ready_at = now + timedelta(
                        seconds=window * TREND_SLOW_MULTIPLIER
                    )
                else:
                    alpha = dt / (window + dt)
                    self._ema += alpha * (raw_w - self._ema)
                    slow_window = window * TREND_SLOW_MULTIPLIER
                    alpha_slow = dt / (slow_window + dt)
                    if self._ema_slow is None:
                        self._ema_slow = raw_w
                    else:
                        self._ema_slow += alpha_slow * (raw_w - self._ema_slow)
        self._last_balance = balance_kwh
        self._last_balance_ts = now
        return self._ema

    def _trend_w(self, now: datetime) -> float:
        """Where the surplus is heading [W]: fast average minus slow average.

        Positive while the surplus is rising, negative while it collapses.
        Zero until the slow average has run for a full window — before that
        the difference reflects how the averages were seeded, not the PV
        curve, and would bias every start decision.
        """
        if self._ema is None or self._ema_slow is None:
            return 0.0
        if self._trend_ready_at is None or now < self._trend_ready_at:
            return 0.0
        return self._ema - self._ema_slow

    def _current_temp(self, cfg: DeviceConfig) -> float | None:
        """Temperature the device regulates, from its temp source or itself."""
        source = cfg.temp_entity or cfg.entity
        state = self.hass.states.get(source)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        current = state.attributes.get("current_temperature")
        if current is None:
            current = state.state
        try:
            return float(current)
        except (TypeError, ValueError):
            return None

    def _effective_target(self, cfg: DeviceConfig) -> float | None:
        """Setpoint the device heats to once the manager turns it on."""
        target = cfg.target_temp
        if not target and cfg.device_type == DEVICE_TYPE_SETPOINT:
            # For setpoint devices the manager commands the boost setpoint,
            # so that - not the schedule's current value - is the target.
            target = cfg.boost_temp
        if not target:
            climate = self.hass.states.get(cfg.entity)
            if climate is not None:
                target = climate.attributes.get("temperature")
        try:
            return float(target) if target is not None else None
        except (TypeError, ValueError):
            return None

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
        if cfg.device_type == DEVICE_TYPE_SETPOINT:
            # Snapshot the schedule's current setpoint so it can be restored
            # when the boost ends; the user's time-based automations remain
            # the source of truth for the normal temperature.
            state = self.hass.states.get(cfg.entity)
            if state is not None:
                try:
                    current = float(state.attributes.get("temperature"))
                except (TypeError, ValueError):
                    current = None
                if current is not None and current < cfg.boost_temp - 0.1:
                    self._restore_temp[cfg.name] = current
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": cfg.entity, "temperature": cfg.boost_temp},
                blocking=True,
            )
        elif cfg.device_type == DEVICE_TYPE_CLIMATE:
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
        if cfg.device_type == DEVICE_TYPE_SETPOINT:
            # Restore the pre-boost setpoint; fall back to the configured
            # normal temperature when it is unknown (e.g. after a restart).
            restore = self._restore_temp.pop(cfg.name, None)
            if restore is None:
                restore = cfg.restore_temp
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": cfg.entity, "temperature": restore},
                blocking=True,
            )
        elif cfg.device_type == DEVICE_TYPE_CLIMATE:
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
