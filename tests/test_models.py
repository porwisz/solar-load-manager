"""Tests for the pure allocator logic."""
from datetime import datetime, time
import pathlib
import sys

sys.path.insert(
    0,
    str(pathlib.Path(__file__).parent.parent / "custom_components" / "solar_load_manager"),
)

from models import (  # noqa: E402
    DeviceConfig,
    DeviceInput,
    allocate,
    in_window,
    marginal_price,
)

NOW = datetime(2026, 7, 2, 12, 0, 0)


def dev(name, priority, power=1000, **kw):
    return DeviceConfig(name=name, priority=priority, device_type="switch",
                        entity=f"switch.{name}", rated_power=power, **kw)


def inp(**kw):
    defaults = dict(enabled=True, available=True, is_on=False, minutes_since_command=1e9)
    defaults.update(kw)
    return DeviceInput(**defaults)


def run(pairs, surplus, price=0.5, source="sell", cheap=0.15, tolerance=300, exclusive=False):
    return allocate(pairs, surplus, price, source, cheap, tolerance, NOW, exclusive=exclusive)


# --- marginal price --------------------------------------------------------

def test_marginal_price_exporting_uses_sell():
    assert marginal_price(0.5, 0.10, 0.62) == (0.10, "sell")


def test_marginal_price_importing_uses_buy():
    assert marginal_price(-0.3, 0.10, 0.62) == (0.62, "buy")
    assert marginal_price(0.0, 0.10, 0.62) == (0.62, "buy")


def test_marginal_price_missing_inputs():
    assert marginal_price(None, 0.10, 0.62) == (0.62, "buy")
    assert marginal_price(0.5, None, 0.62) == (0.62, "buy")
    assert marginal_price(-1.0, 0.10, None) == (None, "unknown")


# --- windows ---------------------------------------------------------------

def test_in_window_normal_and_midnight():
    assert in_window(NOW, time(11), time(13))
    assert not in_window(NOW, time(13), time(14))
    assert in_window(NOW, time(22), time(13))  # crosses midnight
    assert not in_window(NOW, time(22), time(6))
    assert not in_window(NOW, None, time(6))


# --- allocation ------------------------------------------------------------

def test_priority_ladder():
    d1, d2 = dev("cwu", 1, 1500), dev("ac", 2, 1000)
    decisions = run([(d1, inp()), (d2, inp())], surplus=1800, tolerance=0)
    assert decisions["cwu"].should_be_on          # 1800 >= 1650
    assert not decisions["ac"].should_be_on       # only 300 left
    assert decisions["ac"].reason == "insufficient_surplus"


def test_both_fit():
    d1, d2 = dev("cwu", 1, 1500), dev("ac", 2, 1000)
    decisions = run([(d1, inp()), (d2, inp())], surplus=3000, tolerance=0)
    assert decisions["cwu"].should_be_on and decisions["ac"].should_be_on


def test_running_device_power_returns_to_budget():
    # Device on: surplus (which already subtracts its draw) + rated must keep it on
    d1 = dev("cwu", 1, 1500)
    decisions = run([(d1, inp(is_on=True))], surplus=-100, tolerance=300)
    assert decisions["cwu"].should_be_on  # budget = -100+300+1500 = 1700 >= 1500


def test_shed_on_import():
    d1 = dev("cwu", 1, 1500)
    decisions = run([(d1, inp(is_on=True))], surplus=-400, tolerance=300)
    assert not decisions["cwu"].should_be_on
    assert decisions["cwu"].reason == "insufficient_surplus"


def test_cheap_price_forces_on_while_exporting():
    d1 = dev("cwu", 1, 1500)
    decisions = run([(d1, inp())], surplus=0, price=0.05, source="sell")
    assert decisions["cwu"].should_be_on
    assert decisions["cwu"].reason == "running_cheap"


def test_cheap_never_forces_grid_consumption():
    # Low tariff price while importing must NOT force devices on
    d1 = dev("cwu", 1, 1500)
    decisions = run([(d1, inp())], surplus=-3000, price=0.05, source="buy")
    assert not decisions["cwu"].should_be_on
    assert decisions["cwu"].reason == "insufficient_surplus"


def test_negative_price_forces_on_even_from_grid():
    d1 = dev("cwu", 1, 1500)
    decisions = run([(d1, inp())], surplus=-3000, price=-0.02, source="buy")
    assert decisions["cwu"].should_be_on
    assert decisions["cwu"].reason == "running_cheap"


def test_max_price_blocks_cheap_and_surplus():
    d1 = dev("cwu", 1, 1500, max_price=0.6)
    decisions = run([(d1, inp())], surplus=5000, price=0.9)
    assert not decisions["cwu"].should_be_on
    assert decisions["cwu"].reason == "price_blocked"


def test_anti_cycle_on_and_off():
    d1 = dev("cwu", 1, 1500, min_off_minutes=10, min_on_minutes=15)
    # turned off 3 min ago -> may not restart
    decisions = run([(d1, inp(is_on=False, minutes_since_command=3))], surplus=5000)
    assert not decisions["cwu"].should_be_on
    assert decisions["cwu"].reason == "anti_cycle_wait"
    # turned on 3 min ago -> may not stop
    decisions = run([(d1, inp(is_on=True, minutes_since_command=3))], surplus=-5000)
    assert decisions["cwu"].should_be_on
    assert decisions["cwu"].reason == "anti_cycle_hold"


def test_must_run_window():
    d1 = dev("cwu", 1, 1500, must_run_enabled=True,
             must_run_start=time(11), must_run_end=time(13))
    decisions = run([(d1, inp())], surplus=-5000, price=0.9)
    assert decisions["cwu"].should_be_on
    assert decisions["cwu"].reason == "must_run"


def test_manual_override_freezes():
    d1 = dev("cwu", 1, 1500)
    decisions = run([(d1, inp(is_on=True, override_active=True))], surplus=-5000)
    assert decisions["cwu"].reason == "manual_override"
    assert decisions["cwu"].should_be_on  # keeps current state


def test_disabled_device_claims_nothing():
    d1, d2 = dev("cwu", 1, 1500), dev("ac", 2, 1000)
    decisions = run([(d1, inp(enabled=False)), (d2, inp())], surplus=1200, tolerance=0)
    assert decisions["cwu"].reason == "disabled"
    assert decisions["ac"].should_be_on  # cwu doesn't consume budget


def tesla(priority=4, **kw):
    return DeviceConfig(name="tesla", priority=priority, device_type="tesla",
                        charge_switch="switch.charge", current_number="number.amps",
                        cable_sensor="binary_sensor.cable", phases=3, voltage=230,
                        min_amps=5, max_amps=16, **kw)


def test_tesla_amps_follow_surplus():
    t = tesla()
    decisions = run([(t, inp())], surplus=4140, tolerance=0)  # 4140/690 = 6 A
    d = decisions["tesla"]
    assert d.should_be_on and d.target_amps == 6


def test_tesla_own_power_included():
    t = tesla()
    # charging at 11 kW, surplus 0 -> budget 11040 -> 16 A
    decisions = run([(t, inp(is_on=True, own_power_w=11040))], surplus=0, tolerance=0)
    assert decisions["tesla"].target_amps == 16


def test_tesla_stops_below_min_amps():
    t = tesla()
    decisions = run(
        [(t, inp(is_on=True, own_power_w=3450, minutes_since_command=60))],
        surplus=-2760, tolerance=0,
    )  # budget = 690 -> 1 A < 5 A
    assert not decisions["tesla"].should_be_on


def test_tesla_cable_disconnected():
    t = tesla()
    decisions = run([(t, inp(cable_connected=False))], surplus=10000)
    assert not decisions["tesla"].should_be_on
    assert decisions["tesla"].reason == "cable_disconnected"


def test_tesla_cheap_full_power():
    t = tesla()
    decisions = run([(t, inp())], surplus=0, price=0.0)
    assert decisions["tesla"].should_be_on
    assert decisions["tesla"].target_amps == 16


def test_higher_priority_preempts_tesla():
    d1, t = dev("cwu", 1, 1500), tesla()
    # Tesla charging at 2070 W (3 A), cwu off; surplus 0
    decisions = run(
        [(d1, inp()), (t, inp(is_on=True, own_power_w=2070, minutes_since_command=60))],
        surplus=0, tolerance=0,
    )
    # budget = 2070; cwu needs 1650 -> on; tesla left 570 -> 0 A -> off
    assert decisions["cwu"].should_be_on
    assert not decisions["tesla"].should_be_on


def test_exclusive_only_highest_priority_runs():
    d1, d2 = dev("cwu", 1, 1500), dev("ac", 2, 1000)
    decisions = run([(d1, inp()), (d2, inp())], surplus=10000, tolerance=0, exclusive=True)
    assert decisions["cwu"].should_be_on
    assert not decisions["ac"].should_be_on
    assert decisions["ac"].reason == "waiting_for_priority"


def test_exclusive_lower_priority_runs_when_higher_ineligible():
    d1 = dev("cwu", 1, 1500, max_price=0.1)  # blocked by price
    d2 = dev("ac", 2, 1000)
    decisions = run([(d1, inp()), (d2, inp())], surplus=10000, price=0.5, tolerance=0, exclusive=True)
    assert not decisions["cwu"].should_be_on
    assert decisions["ac"].should_be_on


def test_exclusive_boost_overrides_slot():
    d1, d2 = dev("cwu", 1, 1500), dev("ac", 2, 1000)
    decisions = run([(d1, inp()), (d2, inp(boost_active=True))], surplus=10000, tolerance=0, exclusive=True)
    assert decisions["cwu"].should_be_on
    assert decisions["ac"].should_be_on  # boost bypasses exclusivity
    assert decisions["ac"].reason == "boost"


def test_target_reached_forces_off_despite_min_on_and_override():
    d1 = dev("spa", 1, 2000, target_temp_off=True, min_on_minutes=30)
    decisions = run(
        [(d1, inp(is_on=True, minutes_since_command=2, temp_reached=True))],
        surplus=10000,
    )
    assert not decisions["spa"].should_be_on
    assert decisions["spa"].reason == "target_reached"


def test_target_reached_blocks_turn_on_even_when_cheap():
    d1 = dev("spa", 1, 2000, target_temp_off=True)
    decisions = run([(d1, inp(temp_reached=True))], surplus=10000, price=0.01, source="sell")
    assert not decisions["spa"].should_be_on
    assert decisions["spa"].reason == "target_reached"
