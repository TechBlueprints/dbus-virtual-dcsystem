#!/usr/bin/env python3
# Copyright 2025 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Virtual DC System service for Venus OS.

Publishes a com.victronenergy.dcsystem.virtual D-Bus service that reports
the DC system load (power consumed by non-battery DC loads).  This causes
dbus-systemcalc-py to set Dc/System/MeasurementType = 1, which activates
DVCC's native DC load compensation -- DVCC inflates the charge current
limit (CCL) by the DC load before distributing among chargers, ensuring
both solar chargers AND the Multi/Quattro get a proper share.

The service auto-discovers all DC sources and battery current measurements
on D-Bus with zero configuration:

  DC sources: solarcharger, vebus, multi, alternator, charger,
              fuelcell, dcsource, inverter
  Battery:    SmartShunts (preferred for sub-amp precision) or BMS

  dc_load = sum(sources) - sum(battery)

SmartShunts are preferred for battery current when the number of
SmartShunt-type battery services equals the number of BMS battery
services (i.e. one shunt per physical battery).  Otherwise BMS V*I
is used as a fallback.

Battery services whose name contains "aggregate" are excluded to
prevent double-counting from dbus-aggregate-batteries.
"""

import configparser
import logging
import os
import platform
import sys
import time

# Add ext folder to sys.path for velib_python
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))

import dbus  # noqa: E402
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import GLib  # noqa: E402
from dbusmonitor import DbusMonitor  # noqa: E402
from vedbus import VeDbusService  # noqa: E402

from gate import (
    DC_SYSTEM_THRESHOLDS,
    HEARTBEAT_INTERVAL_S,
    _is_substantial,
)

VERSION = "1.0.0"
SERVICE_NAME = "com.victronenergy.dcsystem.virtual"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config():
    """Load configuration from config.ini, falling back to config.default.ini."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config = configparser.ConfigParser(inline_comment_prefixes=(";",))
    config.read([
        os.path.join(script_dir, "config.default.ini"),
        os.path.join(script_dir, "config.ini"),
    ])
    return config["DEFAULT"]


cfg = load_config()
LOG_LEVEL = getattr(logging, cfg.get("LOGGING", "INFO").upper(), logging.INFO)
UPDATE_INTERVAL = int(cfg.get("UPDATE_INTERVAL", "1"))
LOG_INTERVAL = int(cfg.get("LOG_INTERVAL", "300"))
SMARTSHUNT_KEYWORD = cfg.get("SMARTSHUNT_KEYWORD", "SmartShunt")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dbus-virtual-dcsystem")

# ---------------------------------------------------------------------------
# D-Bus monitor list -- paths we need from each service type
# ---------------------------------------------------------------------------

_DUMMY = {"code": None, "whenToLog": "configChange", "accessLevel": None}


def _build_monitor_list():
    """Build the DbusMonitor service/path specification."""
    # Minimal paths needed for V*I calculation per source type
    vi_paths = {
        "/Dc/0/Voltage": _DUMMY,
        "/Dc/0/Current": _DUMMY,
        "/ProductName": _DUMMY,
    }

    return {
        # --- DC sources ---
        "com.victronenergy.solarcharger": dict(vi_paths),
        "com.victronenergy.vebus": dict(vi_paths),
        "com.victronenergy.multi": dict(vi_paths),
        "com.victronenergy.alternator": {
            **vi_paths,
            "/Dc/0/Power": _DUMMY,  # Orion XS provides direct power reading
        },
        "com.victronenergy.charger": dict(vi_paths),
        "com.victronenergy.fuelcell": dict(vi_paths),
        "com.victronenergy.dcsource": dict(vi_paths),
        "com.victronenergy.inverter": {
            **vi_paths,
            "/Ac/Out/L1/V": _DUMMY,  # fallback for DC-less inverters
            "/Ac/Out/L1/I": _DUMMY,
        },
        # --- Batteries (for battery current measurement) ---
        "com.victronenergy.battery": {
            "/Dc/0/Voltage": _DUMMY,
            "/Dc/0/Current": _DUMMY,
            "/Dc/0/Power": _DUMMY,
            "/ProductName": _DUMMY,
            "/DeviceInstance": _DUMMY,
        },
    }


# ---------------------------------------------------------------------------
# Main service class
# ---------------------------------------------------------------------------

class VirtualDcSystem:
    """Calculates and publishes DC system load to D-Bus."""

    def __init__(self):
        # Set up D-Bus monitor (ignore our own service to avoid feedback)
        monitor_list = _build_monitor_list()
        self._dbusmon = DbusMonitor(
            monitor_list,
            ignoreServices=[SERVICE_NAME],
        )

        # Create the VeDbusService on a separate bus connection to avoid
        # root object-path conflicts with DbusMonitor
        self._bus = dbus.SystemBus(private=True)
        self._service = VeDbusService(SERVICE_NAME, self._bus, register=False)

        # Identity paths
        self._service.add_path("/Mgmt/ProcessName", __file__)
        self._service.add_path("/Mgmt/ProcessVersion", VERSION)
        self._service.add_path("/Mgmt/Connection", "Virtual DC System Service")
        self._service.add_path("/DeviceInstance", 100)
        self._service.add_path("/ProductId", 0xBA45)
        self._service.add_path("/ProductName", "Virtual DC System")
        self._service.add_path("/Connected", 1)

        # Data paths
        self._service.add_path(
            "/Dc/0/Power", 0, writeable=True,
            gettextcallback=lambda a, x: "{:.0f}W".format(x),
        )
        self._service.add_path(
            "/Dc/0/Current", 0, writeable=True,
            gettextcallback=lambda a, x: "{:.1f}A".format(x),
        )
        self._service.add_path(
            "/Dc/0/Voltage", 0, writeable=True,
            gettextcallback=lambda a, x: "{:.2f}V".format(x),
        )
        self._service.add_path(
            "/History/EnergyIn", 0, writeable=True,
            gettextcallback=lambda a, x: "{:.3f}kWh".format(x),
        )
        self._service.add_path(
            "/History/EnergyOut", 0, writeable=True,
            gettextcallback=lambda a, x: "{:.3f}kWh".format(x),
        )

        # Register on D-Bus
        self._service.register()
        logger.info("Registered %s on D-Bus (DeviceInstance=100)", SERVICE_NAME)

        # State for energy integration
        self._energy_in = 0.0   # cumulative kWh consumed by DC loads
        self._last_time = time.time()
        self._last_log_time = 0.0

        # Emit-gating state (see gate.py).  ``_last_substantial`` caches
        # the precise value of each gated path at the moment we last
        # emitted; the next cycle's value is compared against it via
        # the per-path threshold in DC_SYSTEM_THRESHOLDS.  When nothing
        # crosses a threshold we skip the D-Bus write entirely — vedbus
        # emits no ItemsChanged for that cycle.
        # ``_last_emit_time`` (monotonic) drives the periodic heartbeat
        # that forces an emit at least every HEARTBEAT_INTERVAL_S even
        # during a long quiet period, so freshness watchers don't think
        # the service has died.
        self._last_substantial: dict = {}
        self._last_emit_time: float = 0.0

        # Start periodic update
        GLib.timeout_add_seconds(UPDATE_INTERVAL, self._update)
        logger.info(
            "Update loop started (interval=%ds, log_interval=%ds)",
            UPDATE_INTERVAL, LOG_INTERVAL,
        )

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    def _get_service_list(self, svc_type):
        """Return list of service names for a given com.victronenergy.* type."""
        return self._dbusmon.get_service_list(svc_type)

    def _get_value(self, service, path):
        """Read a D-Bus value, returning None on failure."""
        try:
            return self._dbusmon.get_value(service, path)
        except Exception:
            return None

    def _sum_vi(self, svc_type):
        """Sum V*I for all services of a given type."""
        total = 0.0
        for svc in self._get_service_list(svc_type):
            v = self._get_value(svc, "/Dc/0/Voltage") or 0
            i = self._get_value(svc, "/Dc/0/Current") or 0
            total += v * i
        return total

    @staticmethod
    def _is_aggregate(service_name):
        """Check if a battery service is an aggregate (should be excluded)."""
        return "aggregate" in service_name.lower()

    def _is_smartshunt(self, product_name):
        """Check if a battery is a SmartShunt by ProductName."""
        return product_name is not None and SMARTSHUNT_KEYWORD in product_name

    # -------------------------------------------------------------------
    # Main calculation
    # -------------------------------------------------------------------

    def _update(self):
        """Recalculate DC system load and publish to D-Bus."""
        try:
            self._do_update()
        except Exception:
            logger.exception("Error in DC system calculation")
        return True  # keep the GLib timer alive

    def _do_update(self):
        # ---- DC SOURCES (positive = providing power to the DC bus) ----
        # When a device is consuming (e.g. MultiPlus inverting), its
        # current is negative, so V*I naturally goes negative -- the
        # signed math handles both directions automatically.

        # 1. Solar MPPTs
        solar_power = self._sum_vi("com.victronenergy.solarcharger")

        # 2. VE.Bus (Multi/Quattro) -- positive when charging from AC,
        #    negative when inverting (consuming DC to produce AC)
        vebus_power = self._sum_vi("com.victronenergy.vebus")

        # 3. Multi RS and future inverter/chargers
        multi_power = self._sum_vi("com.victronenergy.multi")

        # 4. Alternator / Orion XS (prefer /Dc/0/Power if available)
        alternator_power = 0.0
        for svc in self._get_service_list("com.victronenergy.alternator"):
            p = self._get_value(svc, "/Dc/0/Power")
            if p is not None:
                alternator_power += p
            else:
                v = self._get_value(svc, "/Dc/0/Voltage") or 0
                i = self._get_value(svc, "/Dc/0/Current") or 0
                alternator_power += v * i

        # 5. AC Chargers (Phoenix Smart Charger, etc.)
        charger_power = self._sum_vi("com.victronenergy.charger")

        # 6. Fuel Cells
        fuelcell_power = self._sum_vi("com.victronenergy.fuelcell")

        # 7. DC Sources (SmartShunts configured as DC source meters)
        dcsource_power = self._sum_vi("com.victronenergy.dcsource")

        # 8. VE.Direct Inverters -- DC current is negative when inverting,
        #    so V*I gives negative power (consuming from DC bus).
        #    Fallback to -Vac*Iac if DC values unavailable.
        inverter_power = 0.0
        for svc in self._get_service_list("com.victronenergy.inverter"):
            inv_i = self._get_value(svc, "/Dc/0/Current")
            if inv_i is not None:
                inv_v = self._get_value(svc, "/Dc/0/Voltage") or 0
                inverter_power += inv_v * inv_i
            else:
                # Fallback: estimate from AC output (negative = consuming DC)
                ac_v = self._get_value(svc, "/Ac/Out/L1/V") or 0
                ac_i = self._get_value(svc, "/Ac/Out/L1/I") or 0
                inverter_power -= ac_v * ac_i

        # ---- BATTERY POWER ----
        # Auto-detect SmartShunts vs BMS.  Prefer SmartShunts for sub-amp
        # precision when shunt_count == bms_count (one shunt per battery).
        # Exclude any service name containing "aggregate" to prevent
        # double-counting from dbus-aggregate-batteries.
        shunt_power = 0.0
        shunt_count = 0
        bms_power = 0.0
        bms_count = 0

        for svc in self._get_service_list("com.victronenergy.battery"):
            if self._is_aggregate(svc):
                continue
            pn = self._get_value(svc, "/ProductName") or ""
            v = self._get_value(svc, "/Dc/0/Voltage") or 0
            i = self._get_value(svc, "/Dc/0/Current") or 0
            power = v * i
            if self._is_smartshunt(pn):
                shunt_power += power
                shunt_count += 1
            else:
                bms_power += power
                bms_count += 1

        # Decide which battery current to trust
        if shunt_count > 0 and shunt_count == bms_count:
            battery_power = shunt_power
            battery_source = "shunt"
        else:
            battery_power = bms_power
            battery_source = "bms"

        # ---- FINAL CALCULATION ----
        total_sources = (
            solar_power + vebus_power + multi_power
            + alternator_power + charger_power
            + fuelcell_power + dcsource_power
            + inverter_power
        )
        dc_system_power = total_sources - battery_power

        # Clamp to zero only to suppress measurement noise.  A truly
        # negative value would mean batteries are absorbing more than all
        # sources provide, which is a measurement artifact (timing skew
        # between D-Bus readings).  Real DC loads show as positive even
        # when the system is inverting because the signed math cancels out
        # the inverter consumption against battery discharge.
        if dc_system_power < 0:
            dc_system_power = 0.0

        # Use the highest voltage across SmartShunts and VE.Bus (MultiPlus)
        # as the DC bus reference.  The highest reading best represents the
        # actual bus voltage -- chargers push voltage slightly above battery
        # resting voltage, and SmartShunts measure at the battery terminals.
        voltage = 0.0
        for svc in self._get_service_list("com.victronenergy.battery"):
            if self._is_aggregate(svc):
                continue
            pn = self._get_value(svc, "/ProductName") or ""
            if self._is_smartshunt(pn):
                v = self._get_value(svc, "/Dc/0/Voltage")
                if v and v > voltage:
                    voltage = v
        for svc in self._get_service_list("com.victronenergy.vebus"):
            v = self._get_value(svc, "/Dc/0/Voltage")
            if v and v > voltage:
                voltage = v

        dc_load_current = dc_system_power / voltage if voltage > 0 else 0.0

        # ---- ENERGY TRACKING ----
        now = time.time()
        if dc_system_power > 0 and self._last_time is not None:
            dt_hours = (now - self._last_time) / 3600.0
            self._energy_in += dc_system_power * dt_hours / 1000.0  # W*h -> kWh
        self._last_time = now

        # ---- PUBLISH TO D-BUS (gated) ----
        #
        # Compare the new aggregates against the precise values we last
        # emitted.  If no path has moved by at least its threshold (see
        # DC_SYSTEM_THRESHOLDS in gate.py), we skip the write block
        # entirely and vedbus emits no signal for this cycle.  When we
        # DO emit, we publish the precise (unrounded) values so
        # downstream consumers keep full resolution.
        new_values = {
            "/Dc/0/Power":         dc_system_power,
            "/Dc/0/Current":       dc_load_current,
            "/Dc/0/Voltage":       voltage,
            "/History/EnergyIn":   self._energy_in,
        }
        substantial = _is_substantial(
            new_values, self._last_substantial, DC_SYSTEM_THRESHOLDS
        )
        heartbeat_due = (
            now - self._last_emit_time >= HEARTBEAT_INTERVAL_S
        )

        if substantial or heartbeat_due:
            with self._service as svc:
                svc["/Dc/0/Power"] = dc_system_power
                svc["/Dc/0/Current"] = dc_load_current
                svc["/Dc/0/Voltage"] = voltage
                svc["/History/EnergyIn"] = self._energy_in
                svc["/History/EnergyOut"] = 0
            # Snapshot what we just emitted so the next cycle compares
            # against THIS, not against the previous emit.
            for path, val in new_values.items():
                self._last_substantial[path] = val
            self._last_emit_time = now

        # ---- PERIODIC LOGGING ----
        if now - self._last_log_time >= LOG_INTERVAL:
            self._last_log_time = now
            parts = []
            if solar_power:
                parts.append("solar=%.0fW" % solar_power)
            if vebus_power:
                parts.append("vebus=%.0fW" % vebus_power)
            if multi_power:
                parts.append("multi=%.0fW" % multi_power)
            if alternator_power:
                parts.append("alt=%.0fW" % alternator_power)
            if charger_power:
                parts.append("charger=%.0fW" % charger_power)
            if fuelcell_power:
                parts.append("fuel=%.0fW" % fuelcell_power)
            if dcsource_power:
                parts.append("dcsrc=%.0fW" % dcsource_power)
            if inverter_power:
                parts.append("inv=%.0fW" % inverter_power)
            parts.append(
                "batt=%.0fW[%s:%d+%d]" % (
                    battery_power, battery_source, shunt_count, bms_count,
                )
            )
            parts.append("dc_load=%.0fW" % dc_system_power)
            parts.append("%.1fA" % dc_load_current)
            parts.append("energy=%.1fkWh" % self._energy_in)
            logger.info("DC system: %s", " | ".join(parts))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    DBusGMainLoop(set_as_default=True)

    logger.info(
        "dbus-virtual-dcsystem v%s starting (Python %s)",
        VERSION, platform.python_version(),
    )

    _service = VirtualDcSystem()

    logger.info("Entering GLib main loop")
    mainloop = GLib.MainLoop()
    mainloop.run()


if __name__ == "__main__":
    main()
