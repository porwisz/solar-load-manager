"""Pure decision logic for Solar Load Manager (no Home Assistant imports)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time


@dataclass
class DeviceConfig:
    """Static configuration of a managed device."""

    name: str
    priority: int
    device_type: str  # switch | climate | tesla
    entity: str = ""
    rated_power: float = 1500.0
    on_factor: float = 1.1
    min_on_minutes: float = 15.0
    min_off_minutes: float = 10.0
    max_price: float = 999.0  # do not start above this marginal price [PLN/kWh]
    solar_only: bool = False  # run only on solar surplus; price never starts it
    hvac_mode: str = "heat"
    target_temp_off: bool = False  # safeguard: force off once target temp reached
    temp_entity: str = ""
    target_temp: float | None = None
    must_run_enabled: bool = False
    must_run_start: time | None = None
    must_run_end: time | None = None
    # tesla
    charge_switch: str = ""
    current_number: str = ""
    cable_sensor: str = ""
    charger_power_sensor: str = ""
    battery_level_sensor: str = ""
    charge_limit_entity: str = ""
    refresh_button: str = ""  # pressed after commands to force fresh data
    phases: int = 3
    voltage: float = 230.0
    min_amps: int = 5
    max_amps: int = 16

    @property
    def slug(self) -> str:
        return self.name.lower().replace(" ", "_")

    @property
    def watts_per_amp(self) -> float:
        return self.voltage * self.phases

    @property
    def min_power(self) -> float:
        """Minimum power the device needs to run."""
        if self.device_type == "tesla":
            return self.min_amps * self.watts_per_amp
        return self.rated_power


@dataclass
class DeviceInput:
    """Runtime state of a device at decision time."""

    enabled: bool = True
    available: bool = True
    is_on: bool = False
    minutes_since_command: float = 1e9
    override_active: bool = False
    boost_active: bool = False
    cable_connected: bool = True  # tesla only
    own_power_w: float = 0.0  # tesla: current charging power
    temp_reached: bool = False  # safeguard: target temperature reached
    battery_full: bool = False  # tesla: battery level at/above charge limit


@dataclass
class Decision:
    """Allocator output for one device."""

    should_be_on: bool
    allocated_w: float
    target_amps: int | None
    reason: str
    required_w: float | None = None  # budget needed for the device to (keep) running
    missing_w: float | None = None  # how much more surplus that requires right now


def marginal_price(
    hourly_balance_kwh: float | None,
    sell_price: float | None,
    buy_price: float | None,
) -> tuple[float | None, str]:
    """Cost of one extra kWh under hourly net-billing.

    While the hour's balance is positive the house is a net exporter, so
    extra consumption only forgoes the sell (RCE) price; once net-importing,
    it costs the tariff (buy) price. Returns (price, source).
    """
    if hourly_balance_kwh is not None and hourly_balance_kwh > 0:
        if sell_price is not None:
            return sell_price, "sell"
    if buy_price is not None:
        return buy_price, "buy"
    return None, "unknown"


def in_window(now: datetime, start: time | None, end: time | None) -> bool:
    """True when now's local time falls in [start, end); handles midnight crossing."""
    if start is None or end is None:
        return False
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def allocate(
    devices: list[tuple[DeviceConfig, DeviceInput]],
    surplus_w: float,
    price: float | None,
    price_source: str,
    cheap_price: float,
    import_tolerance: float,
    now: datetime,
    exclusive: bool = False,
) -> dict[str, Decision]:
    """Decide on/off and power allocation for every device.

    Budget model: the measured surplus already includes consumption of
    devices that are currently running, so the distributable budget is
    surplus + power of managed devices that are on. Devices are served
    in priority order (1 = first); shedding falls out naturally because
    a low-priority device stops fitting the budget before a high-priority
    one does.
    """
    ordered = sorted(devices, key=lambda pair: pair[0].priority)
    effective_price = 999.0 if price is None else price
    # "Cheap" may only force devices on when consuming beats selling:
    # while net-exporting with a worthless sell price, or when the price
    # is negative. It must never force consumption from the grid at tariff.
    cheap = (price_source == "sell" and effective_price <= cheap_price) or (
        effective_price <= 0
    )

    budget = surplus_w + import_tolerance
    for cfg, inp in ordered:
        if inp.is_on:
            budget += inp.own_power_w if cfg.device_type == "tesla" else cfg.rated_power

    decisions: dict[str, Decision] = {}
    slot_taken = False  # exclusive mode: only one device may run at a time
    for cfg, inp in ordered:
        forced_reason = None
        if inp.boost_active:
            forced_reason = "boost"
        elif cfg.must_run_enabled and in_window(now, cfg.must_run_start, cfg.must_run_end):
            forced_reason = "must_run"
        elif cheap and effective_price <= cfg.max_price and not cfg.solar_only:
            forced_reason = "running_cheap"

        if not inp.enabled or not inp.available:
            decisions[cfg.name] = Decision(inp.is_on, 0.0, None, "disabled")
            continue
        if inp.temp_reached:
            # Safeguard: target temperature reached - force off, bypassing
            # anti-cycling, boost, must-run and manual override.
            decisions[cfg.name] = Decision(False, 0.0, None, "target_reached")
            continue
        if cfg.device_type == "tesla" and not inp.cable_connected:
            decisions[cfg.name] = Decision(False, 0.0, None, "cable_disconnected")
            continue
        if cfg.device_type == "tesla" and inp.battery_full:
            # Safeguard: battery already at the charge limit - charging is
            # pointless, force off like target_reached.
            decisions[cfg.name] = Decision(False, 0.0, None, "battery_full")
            continue
        if inp.override_active:
            decisions[cfg.name] = Decision(inp.is_on, 0.0, None, "manual_override")
            continue

        if forced_reason:
            amps = cfg.max_amps if cfg.device_type == "tesla" else None
            claim = cfg.rated_power if cfg.device_type != "tesla" else cfg.max_amps * cfg.watts_per_amp
            budget -= claim
            decisions[cfg.name] = Decision(True, claim, amps, forced_reason)
            slot_taken = True
            continue

        if not cfg.solar_only and effective_price > cfg.max_price:
            decisions[cfg.name] = _guarded_off(cfg, inp, "price_blocked")
            budget -= decisions[cfg.name].allocated_w
            slot_taken = slot_taken or decisions[cfg.name].should_be_on
            continue

        if exclusive and slot_taken:
            decisions[cfg.name] = _guarded_off(cfg, inp, "waiting_for_priority")
            budget -= decisions[cfg.name].allocated_w
            continue

        if cfg.device_type == "tesla":
            amps = int(budget // cfg.watts_per_amp)
            amps = min(amps, cfg.max_amps)
            threshold = cfg.min_amps * cfg.watts_per_amp
            if amps >= cfg.min_amps:
                claim = amps * cfg.watts_per_amp
                decision = _guarded_on(cfg, inp, claim, amps, "running_surplus")
            else:
                decision = _guarded_off(cfg, inp, "insufficient_surplus")
        else:
            threshold = cfg.rated_power * (cfg.on_factor if not inp.is_on else 1.0)
            if budget >= threshold:
                decision = _guarded_on(cfg, inp, cfg.rated_power, None, "running_surplus")
            else:
                decision = _guarded_off(cfg, inp, "insufficient_surplus")
        decision.required_w = threshold
        decision.missing_w = max(0.0, threshold - budget)

        budget -= decision.allocated_w
        decisions[cfg.name] = decision
        slot_taken = slot_taken or decision.should_be_on

    return decisions


def _guarded_on(
    cfg: DeviceConfig, inp: DeviceInput, claim: float, amps: int | None, reason: str
) -> Decision:
    """Turn on, unless the device turned off too recently (anti-cycling)."""
    if not inp.is_on and inp.minutes_since_command < cfg.min_off_minutes:
        return Decision(False, 0.0, None, "anti_cycle_wait")
    return Decision(True, claim, amps, reason)


def _guarded_off(cfg: DeviceConfig, inp: DeviceInput, reason: str) -> Decision:
    """Turn off, unless the device turned on too recently (anti-cycling)."""
    if inp.is_on and inp.minutes_since_command < cfg.min_on_minutes:
        claim = inp.own_power_w if cfg.device_type == "tesla" else cfg.rated_power
        return Decision(True, claim, None, "anti_cycle_hold")
    return Decision(False, 0.0, None, reason)
