# Solar Load Manager — Algorithm Description

Current as of commit `f251dae` (v1.8.x). Describes exactly how the integration
decides which devices to run, in what order, and how devices are commanded.

The logic is split in two layers:

| Layer | File | Role |
|---|---|---|
| Coordinator | `custom_components/solar_load_manager/coordinator.py` | Reads Home Assistant state, assembles inputs, runs the allocator, commands devices |
| Pure model | `custom_components/solar_load_manager/models.py` | `allocate()` + `marginal_price()` — no HA imports, fully unit-testable |

---

## 1. The decision loop

`SlmCoordinator._async_update_data()` runs **every 60 seconds**
(`UPDATE_INTERVAL_SECONDS`). Each cycle:

1. Read the **hourly balance sensor** (kWh, resets each hour — the net-billing
   meter balance for the current hour).
2. Derive **smoothed net power** (`net_w`) from the balance delta (§2).
3. Compute the **bank power** and the total **budget** (§3).
4. Determine the **marginal price** of one extra kWh (§4).
5. Build a `DeviceInput` snapshot per device (§5).
6. Run `allocate()` — the pure allocator — to get a `Decision` per device (§6).
7. Apply the decisions to real devices, but **only if the balance sensor had a
   valid reading** this cycle (no actuation on blind data) (§7).

The loop can also be triggered immediately by: toggling an enable /
solar-only switch, or calling the `solar_load_manager.boost` service.

---

## 2. Net power estimation (EMA)

There is no direct grid-power sensor. Net power is derived from the hourly
balance sensor (`_update_net_power`):

```
raw_w = (balance_kwh - previous_balance_kwh) * 3_600_000 / dt_seconds
```

- Samples are **skipped** when the hour rolled over (sensor reset:
  `now.hour != last.hour`), or when `dt <= 0` or `dt >= 1800 s`.
- The raw value is smoothed with an **exponential moving average**:
  `alpha = dt / (smoothing_window + dt)`; default window
  `smoothing_seconds = 300`. Window ≤ 0 disables smoothing.
- If the sensor is unavailable this cycle, the last EMA value is reused.

Positive = exporting (surplus), negative = importing.

## 3. Budget model (hourly net-billing bank)

Under hourly net-billing, surplus accumulated earlier in the hour can be
consumed for free until the hour ends. The coordinator therefore spreads the
banked balance over the remainder of the hour:

```
remaining_h = max(0.1, (60 - minute) / 60)
bank_w      = balance_kwh * 1000 / remaining_h
budget_w    = net_w + bank_w
```

`budget_w` — not just instantaneous surplus — is what the allocator
distributes. A negative balance (already net-importing this hour) produces a
negative bank, correctly reducing the budget.

Inside `allocate()` the budget is further adjusted:

```
budget = budget_w + import_tolerance          # default 300 W slack
for each managed device currently ON:
    budget += its consumption                 # tesla: measured charger power
                                              # others: rated_power
```

Adding back the consumption of already-running managed devices makes the
budget the *distributable* total: the measured surplus already includes their
draw, so without this a running device would appear to consume its own
budget. Load shedding falls out naturally — as the budget shrinks, the
lowest-priority devices stop fitting first.

## 4. Marginal price (`marginal_price()`)

What does one extra kWh cost right now?

- If `balance_kwh > export_margin_kwh` (default **0.2 kWh**), the house is a
  net exporter this hour → extra consumption only *forgoes the sell price*
  (RCE). Source = `"sell"`.
- Otherwise it costs the **buy price** (tariff). Source = `"buy"`.
- If no usable price sensor → `(None, "unknown")`; the allocator then treats
  the price as 999 (blocks everything with a real `max_price`).

`export_margin_kwh` is hysteresis: balance readings arrive in chunks and
oscillate around zero, so the hour only counts as "exporting" once the
balance clearly clears the margin.

The buy price is read from the buy-price sensor's numeric state, falling back
to a configured attribute (default `price`).

**Cheap detection** (in `allocate()`):

```
cheap = (source == "sell" AND price <= cheap_price)   # default 0.15 PLN/kWh
        OR price <= 0
```

Cheap may only force devices on when consuming beats selling — while
net-exporting with a worthless sell price, or at a negative price. It never
forces grid import at normal tariff.

## 5. Per-device input assembly (coordinator)

For each configured device the coordinator builds a `DeviceInput`:

- **enabled** — the per-device "automation" switch (restored across restarts).
- **available / is_on** — from the device entity's state:
  - *switch/climate*: `is_on = state not in ("off",)`
  - *tesla*: state of the charge switch
  - *setpoint* (DHW boost): "on" means the boost setpoint is currently
    applied: `current setpoint >= boost_temp - 0.1`
- **minutes_since_command** — time since our last command. If there is no
  in-memory record (fresh start / options reload), it falls back to the
  entity's own `last_changed`, so min-on/min-off times survive restarts.
- **External-change (manual override) detection** — if the device state no
  longer matches what we commanded, and >2 min passed since our command (so
  it isn't our own command still settling), a **manual override** starts:
  the device is left alone for `override_minutes` (default **30 min**).
  When it expires, the command record is cleared and
  `minutes_since_command = ∞` (anti-cycling won't block the next action).
- **boost_active** — set by the `solar_load_manager.boost` service
  (default 60 min).
- **Tesla extras**: `cable_connected` (cable sensor == "on"),
  `own_power_w` (measured charger power, kW→W),
  `battery_full` (battery level ≥ charge limit, when both sensors set).
- **temp_reached** — when `target_temp_off` is set: current temperature
  (from `temp_entity`, or `current_temperature` attribute, or numeric state)
  ≥ target. Target resolution order: configured `target_temp` → for setpoint
  devices the `boost_temp` (the guard asks "is boosting still useful?") →
  the climate entity's own `temperature` attribute.
- **solar_only** — the configured flag, overridable at runtime by the
  per-device "solar only" switch (restored across restarts).

## 6. The allocator (`allocate()`)

Devices are processed **sorted by priority (1 = first served)**. A running
`budget` (see §3) is decremented as devices claim power. In **exclusive
mode** (default **on**), only one device may run at a time — the first device
that ends up ON takes the slot.

For each device, in this exact order:

### 6.1 Forced-on reasons (computed first, applied later)

Highest to lowest precedence:
1. `boost` — boost timer active.
2. `must_run` — `must_run_enabled` and local time inside the
   `[must_run_start, must_run_end)` window (midnight crossing handled).
3. `running_cheap` — `cheap` is true AND `price <= max_price` AND the device
   is **not** solar_only.

### 6.2 Hard gates (each short-circuits with its own status)

| Order | Condition | Decision |
|---|---|---|
| 1 | not enabled or entity unavailable | keep current state, `disabled` |
| 2 | `temp_reached` | **force OFF**, `target_reached` — bypasses anti-cycling, boost, must-run, and manual override |
| 3 | tesla and cable disconnected | OFF, `cable_disconnected` (no actuation) |
| 4 | tesla and battery at charge limit | force OFF, `battery_full` |
| 5 | manual override active | keep current state, `manual_override` (no actuation) |

### 6.3 Forced-on execution

If a forced reason survived the gates: the device turns ON unconditionally
(anti-cycling is *not* consulted), claims `rated_power` (tesla:
`max_amps × phases × voltage` at `max_amps`), decrements the budget, and
takes the exclusive slot.

### 6.4 Price ceiling

`max_price` is a hard ceiling for **every** device, including solar_only
ones: even free surplus is not diverted to a load when the forgone export
(or tariff) exceeds the limit. If `price > max_price` → guarded off,
`price_blocked`. (solar_only only opts out of the *cheap forcing* branch —
it never causes grid import, but still respects the ceiling.)

### 6.5 Exclusive slot

If exclusive mode and the slot is already taken → guarded off,
`waiting_for_priority`.

### 6.6 Surplus fit

- **Tesla** (modulating load):
  ```
  amps = min(floor(budget / (phases × voltage)), max_amps)
  ```
  If `amps >= min_amps` → guarded ON at `amps`, claiming
  `amps × watts_per_amp` (`running_surplus`); else → guarded off
  (`insufficient_surplus`). The charge current tracks the budget every cycle
  while running.
- **Switch / climate / setpoint** (fixed load):
  ```
  threshold = rated_power × on_factor   # when OFF (default factor 1.1 → 10% headroom)
  threshold = rated_power               # when already ON (hysteresis)
  ```
  `budget >= threshold` → guarded ON claiming `rated_power`; else guarded
  off.

Each surplus decision also records `required_w` (budget needed) and
`missing_w = max(0, threshold − budget)` for the status sensors.

### 6.7 Anti-cycling guards

- `_guarded_on`: if the device is OFF and turned off less than
  `min_off_minutes` ago (default 10) → stay OFF, `anti_cycle_wait`.
- `_guarded_off`: if the device is ON and turned on less than
  `min_on_minutes` ago (default 15) → stay ON, `anti_cycle_hold`.
  Tesla rides out the hold at **min_amps** to minimise grid import while the
  surplus is gone; fixed loads hold at `rated_power`.

Forced reasons (boost / must_run / running_cheap) and the safety cutoffs
(`target_reached`, `battery_full`) bypass anti-cycling.

## 7. Actuation (`_apply`)

Runs only when the balance sensor delivered a reading this cycle. Skipped
when: device disabled/unavailable, manual override active (except
`target_reached`, which always executes), or `cable_disconnected`.
Commands are recorded in `_last_command` with a timestamp (feeds
`minutes_since_command`). Exceptions are caught per device so one failure
doesn't stall the loop.

Per device type:

- **switch**: `homeassistant.turn_on` / `turn_off`.
- **climate**: ON → `climate.set_hvac_mode` to the configured `hvac_mode`
  (default `heat`); OFF → hvac mode `off`.
- **setpoint** (solar DHW boost): ON → snapshot the current setpoint (if
  below `boost_temp − 0.1`) into `_restore_temp`, then
  `climate.set_temperature` to `boost_temp` (default 55 °C). OFF → restore
  the snapshotted setpoint, falling back to configured `restore_temp`
  (default 45 °C) when unknown (e.g. after a restart). The user's
  schedule/automations remain the source of truth for the normal
  temperature.
- **tesla**: ON → `number.set_value` on the current entity to the target
  amps, then `switch.turn_on` on the charge switch if not already charging.
  OFF → `switch.turn_off`. After a switch change, the refresh button (if
  configured) is pressed to force fresh vehicle data. While ON, only the
  amps are re-adjusted each cycle (no redundant switch calls).

## 8. Exposed entities and services

- **Hub sensors**: *Smoothed surplus* (W; attributes: hourly balance, bank_w,
  budget_w) and *Marginal price* (attributes: source, sell/buy price).
- **Per device**: a *status* sensor (state = the decision reason, e.g.
  `running_surplus`, `anti_cycle_hold`, `price_blocked`), an *automation*
  enable switch, and a *solar only* switch (both restore their state).
- **Service** `solar_load_manager.boost` (`device`, `minutes` default 60):
  forces a device on regardless of surplus/price, subject only to the
  safety gates.

## 9. Key parameters (defaults)

| Parameter | Default | Meaning |
|---|---|---|
| `smoothing_seconds` | 300 | EMA window for net power |
| `import_tolerance` | 300 W | allowed grid import before shedding |
| `cheap_price` | 0.15 | sell price at/below which consuming beats exporting |
| `export_margin_kwh` | 0.2 | balance hysteresis before hour counts as exporting |
| `exclusive_mode` | true | only one managed device runs at a time |
| `override_minutes` | 30 | hands-off period after a detected manual change |
| `on_factor` | 1.1 | start headroom multiplier for fixed loads |
| `min_on_minutes` / `min_off_minutes` | 15 / 10 | anti-cycling times |
| `max_price` | 999 | per-device marginal-price ceiling |
| `boost_temp` / `restore_temp` | 55 / 45 °C | setpoint device temperatures |
| `min_amps` / `max_amps` / `phases` / `voltage` | 5 / 16 / 3 / 230 | tesla charging envelope |
| update interval | 60 s | decision loop period |

## 10. Decision states (status sensor values)

`running_surplus`, `running_cheap`, `must_run`, `boost`, `anti_cycle_hold`,
`anti_cycle_wait`, `insufficient_surplus`, `waiting_for_priority`,
`price_blocked`, `target_reached`, `battery_full`, `cable_disconnected`,
`manual_override`, `disabled`.
