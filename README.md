# dbus-virtual-dcsystem

> **Experimental software.** This project is provided as-is under the
> [Apache License 2.0](LICENSE). The author makes no warranties and accepts
> no liability for any use of this software. You are solely responsible for
> evaluating whether it is suitable for your system. See the
> [Limitations](#limitations) section below.

A standalone Venus OS service that publishes a virtual DC system monitor
(`com.victronenergy.dcsystem.virtual`) on D-Bus, enabling DVCC's native DC
load compensation without requiring a dedicated physical DC meter.

## The Problem

Victron's DVCC (Distributed Voltage and Current Control) distributes a
battery's requested charge current limit (CCL) among all connected chargers --
solar MPPTs, the MultiPlus/Quattro, alternator chargers, and so on. However,
DVCC only accounts for DC loads (lights, refrigerators, pumps, inverters, and
other consumers wired directly to the DC bus) when it detects a **measured** DC
system power source on D-Bus (`Dc/System/MeasurementType = 1`).

Without that signal, DVCC uses a calculated estimate and its DC load
compensation stays disabled. The result: DC loads silently eat into the charge
budget. If your batteries request 10 A of charge current but your DC loads
consume 8 A, chargers are told to produce only 10 A total -- leaving just 2 A
actually reaching the batteries. Charging slows down, the system may never
reach absorption voltage, and the MultiPlus can appear stuck in "Bulk" even
though the batteries are nearly full.

The typical Victron solution is to install a SmartShunt configured as a "DC
System" meter. But many installations already have SmartShunts monitoring
individual batteries, and buying an additional shunt solely to unlock a
software feature is unnecessary when all the data already exists on D-Bus.

## What This Service Does

**dbus-virtual-dcsystem** synthesises a `com.victronenergy.dcsystem` D-Bus
service from the power readings that are already available on your system. By
publishing a measured DC system value, it activates DVCC's native
compensation: DVCC inflates the CCL by the DC load *before* distributing
among chargers, so both solar chargers and the Multi/Quattro receive a proper
share and the batteries actually get what they asked for.

The service requires zero configuration. It auto-discovers every DC source and
battery on D-Bus and recalculates once per second:

```
dc_load = sum(all DC sources) − sum(battery power)
```

### DC Sources (auto-discovered)

| Service type | Examples |
|---|---|
| `com.victronenergy.solarcharger` | Victron MPPT solar chargers |
| `com.victronenergy.vebus` | MultiPlus, Quattro (positive when charging from AC, negative when inverting) |
| `com.victronenergy.multi` | Multi RS |
| `com.victronenergy.alternator` | Orion XS, alternator chargers |
| `com.victronenergy.charger` | Phoenix Smart Charger, other AC chargers |
| `com.victronenergy.fuelcell` | Fuel cells |
| `com.victronenergy.dcsource` | SmartShunts configured as DC source meters |
| `com.victronenergy.inverter` | VE.Direct inverters |

### Battery Power

The service sums **V × I** across every `com.victronenergy.battery.*`
service that publishes both `/Dc/0/Voltage` and `/Dc/0/Current`.  This
catches the common monitor types — SmartShunts, BMV-712/700,
Pylontech/BYD/Discover/etc. BMSes, and community drivers like
`dbus-serialbattery` — without trying to guess the device type from
its `ProductName` string.

A battery service that publishes voltage but no current (e.g. a
SeeLevel BTP3 channel configured as a battery voltage gauge) is
excluded from the sum.  Such a service can't contribute a meaningful
V × I term, and silently treating its current as zero would pull the
battery_power total toward zero and break DC-Load math everywhere
downstream.

Aggregator services (name contains "aggregate") are also excluded so
that `dbus-aggregate-batteries` and its constituent shunts don't
get counted twice.

**Overlap caveat**: if you have a SmartShunt AND a separate BMS both
reporting the SAME battery bank as distinct services, both will be
counted and you'll see roughly double the real battery power.  In
that topology the right solution is to put `dbus-aggregate-batteries`
in front of them so the overlap is resolved upstream, or use
`/Settings/SystemSetup/BatteryService` to mark one as the primary
battery service.

Battery services whose D-Bus name contains `aggregate` are automatically
excluded to prevent double-counting from services like
[dbus-aggregate-batteries](https://github.com/TechBlueprints/dbus-aggregate-batteries).

### DC Bus Voltage

The published voltage is the **maximum** reading across all battery
monitors that publish current (see above) plus all VE.Bus devices.
The highest reading best represents the actual DC bus voltage,
especially when chargers are actively pushing voltage above battery
resting levels.  Voltage-only services (like a SeeLevel battery
channel) are excluded from this reference selection too, since they
sometimes report a slightly different rail voltage that's less
authoritative than the main-bus reading.

## Use Cases

- **Multi-battery systems with DC loads**: You have two or more batteries
  managed by `dbus-aggregate-batteries`, a MultiPlus, solar MPPTs, and DC
  loads. Without this service, the DC loads reduce effective charge current.
- **Replacing a dedicated DC meter**: You already have SmartShunts on each
  battery and don't want to buy another just to enable DVCC compensation.
- **Mobile / RV / marine installations**: DC loads like refrigerators, water
  pumps, and lighting are always present. This service ensures they don't
  silently prevent batteries from reaching full charge.
- **Debugging charge distribution**: The periodic log output shows a
  per-source power breakdown, making it easy to see where power is going.

## Limitations

This service has important limitations you should understand before deploying:

1. **Experimental**: This software has been tested on a single Victron Cerbo
   GX installation. It has not been tested across a wide variety of hardware
   configurations, firmware versions, or load profiles. Use at your own risk.

2. **Measurement timing skew**: The DC load is computed from D-Bus values that
   are read sequentially across multiple services. These values are not
   perfectly synchronised -- each device updates at its own rate. During rapid
   load transients, the calculated DC load may briefly be inaccurate. The
   service clamps negative values to zero to avoid publishing nonsensical
   readings, but this means transient over-estimation is possible.

3. **Cumulative rounding errors**: Battery power computed from V\*I inherits
   the precision limits of each device. BMS units that round current to whole
   amps can introduce meaningful error at low charge rates.

4. **No physical measurement**: This is a *virtual* service. It is only as
   accurate as the D-Bus readings it aggregates. A dedicated physical DC meter
   (e.g. a SmartShunt configured as "DC System") will always be more accurate.

5. **Single-system scope**: The service sees only the D-Bus of the device it
   runs on. It does not aggregate across multiple Venus OS installations or
   GX devices.

6. **Energy tracking is session-based**: The `/History/EnergyIn` counter
   resets to zero each time the service restarts. It is not persisted to disk.

7. **Venus OS firmware updates**: Victron firmware updates may overwrite or
   reset service symlinks under `/service/`. You may need to re-run
   `enable.sh` after a firmware update. The service files under `/data/apps/`
   survive firmware updates.

8. **Interaction with other services**: If you have `dbus-aggregate-batteries`
   with its own `DC_LOAD_COMPENSATION` feature enabled, disable that feature
   (`DC_LOAD_COMPENSATION = False` in its `config.ini`) to avoid conflicts.
   Only one service should publish `com.victronenergy.dcsystem.virtual`.

## Installation

### One-line install

```bash
curl -fsSL https://raw.githubusercontent.com/TechBlueprints/dbus-virtual-dcsystem/main/install.sh | bash
```

### Manual install

```bash
cd /data/apps
git clone https://github.com/TechBlueprints/dbus-virtual-dcsystem.git
cd dbus-virtual-dcsystem
bash enable.sh
svc -u /service/dbus-virtual-dcsystem
```

## Configuration

Configuration is optional. The service works with zero configuration by
auto-discovering all devices on D-Bus.

To override defaults, copy `config.default.ini` to `config.ini` and edit:

```ini
[DEFAULT]
; Logging level: ERROR, WARNING, INFO, DEBUG
LOGGING = INFO

; How often to recalculate DC system power (seconds)
UPDATE_INTERVAL = 1

; How often to emit a summary log line (seconds)
LOG_INTERVAL = 300
```

> The previous `SMARTSHUNT_KEYWORD` setting was removed.  Battery
> services are now categorised by whether they publish
> `/Dc/0/Current`, not by their `ProductName` string — see the
> "Battery Power" section above.

## Service Management

```bash
# Start
svc -u /service/dbus-virtual-dcsystem

# Stop
svc -d /service/dbus-virtual-dcsystem

# Restart
svc -t /service/dbus-virtual-dcsystem

# Status
svstat /service/dbus-virtual-dcsystem

# View logs (tai64nlocal converts daemontools timestamps to human-readable)
tail -n 50 /var/log/dbus-virtual-dcsystem/current | tai64nlocal
```

## Uninstall

```bash
bash /data/apps/dbus-virtual-dcsystem/disable.sh
rm -rf /data/apps/dbus-virtual-dcsystem
```

## D-Bus Paths Published

| Path | Type | Description |
|---|---|---|
| `/Dc/0/Power` | W | DC system load (positive = consuming) |
| `/Dc/0/Current` | A | DC system load current |
| `/Dc/0/Voltage` | V | DC bus voltage (max across SmartShunts and VE.Bus) |
| `/History/EnergyIn` | kWh | Cumulative energy consumed by DC loads (resets on restart) |
| `/History/EnergyOut` | kWh | Always 0 |
| `/ProductName` | string | "Virtual DC System" |
| `/DeviceInstance` | int | 100 |
| `/Connected` | int | 1 |

## Third-Party Software

This project includes [velib_python](https://github.com/victronenergy/velib_python)
by Victron Energy BV, located in `ext/velib_python/`. It is licensed under the
MIT License:

> Copyright (c) 2014 Victron Energy BV
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in
> all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.

The full MIT license text is available at [`ext/velib_python/LICENSE`](ext/velib_python/LICENSE).

## License

Apache License 2.0 -- see [LICENSE](LICENSE).

This software is provided "as is", without warranty of any kind, express or
implied. The author is not responsible for any damage, data loss, or other
issues arising from the use of this software. See the full license text for
details.
