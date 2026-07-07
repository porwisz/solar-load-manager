# Solar Load Manager

Home Assistant custom integration that turns household loads on and off to soak up
PV surplus, taking the hourly energy price into account. Built for RCE (PSE) dynamic
prices but works with any current/min/max price sensors.

## How it works

A single coordinator runs every 60 seconds:

1. Reads the PV surplus sensor and smooths it with an exponential moving average
   (default 5-minute window) so passing clouds don't flap devices.
2. Computes a **price score**: today's price position normalized to 0 (cheapest
   hour) … 1 (most expensive hour).
3. Allocates the power budget (`surplus + import tolerance + power of devices
   already running`) down the priority list. Each device runs when the budget
   covers its rated power (× turn-on factor for hysteresis), when the price is
   very cheap (score ≤ threshold), during its guaranteed-run window, or during
   a boost. Shedding happens in reverse priority order.
4. Commands the devices, honoring per-device minimum on/off times.

If you (or another automation) manually change a managed device, the manager
backs off for a configurable period (default 30 minutes).

### Device types

| Type | Control |
|------|---------|
| `switch` | `homeassistant.turn_on` / `turn_off` on any switch-like entity |
| `climate` | `climate.set_hvac_mode` (configured mode / `off`) |
| `tesla` | modulates charging amps to match the remaining surplus; stops below minimum amps; charges at max amps when the price is cheap |

### Entities

- Hub: `sensor.solar_load_manager_smoothed_surplus`, `sensor.solar_load_manager_price_score`
- Per device: an **enable switch** (arm/disarm, restored across restarts) and a
  **status sensor** (`running_surplus`, `running_cheap`, `must_run`, `boost`,
  `insufficient_surplus`, `anti_cycle_wait`, `manual_override`, …) with
  attributes: allocated watts, target amps, priority, the budget needed to run
  (`required_w`) and how much surplus is missing right now (`missing_w`).

### Services

- `solar_load_manager.boost` — force a device on for N minutes regardless of
  surplus and price.

## Installation

1. HACS → Integrations → three dots → *Custom repositories* →
   `https://github.com/porwisz/solar-load-manager` (type: Integration).
2. Install **Solar Load Manager**, restart Home Assistant.
3. Settings → Devices & Services → *Add Integration* → Solar Load Manager.
   Pick your surplus and price sensors.
4. Open the entry's **Configure** menu to add devices (name, priority, type,
   entity, rated power…). The Tesla type asks for its charging entities in a
   second step.
5. Flip each device's enable switch when you're ready to hand it over.

## Configuration reference

Hub: surplus sensor [W], current/min/max price sensors, smoothing window,
grid import tolerance [W], cheap-score threshold, manual-override backoff.

Device: priority (1 = served first), rated power [W], turn-on factor,
min on/off minutes, price block score, HVAC mode, guaranteed-run window.
Tesla adds: charge switch, current number, cable sensor, charger power
sensor, phases, voltage, min/max amps.
