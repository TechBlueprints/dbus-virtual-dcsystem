# Copyright 2025 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""
Pure-Python helpers for the virtual DC system service's emit gating.

The service runs an aggregation pass once per ``UPDATE_INTERVAL`` (1 s
by default), computing DC-bus power / current / voltage / energy.  In
a typical setup the precise reading flickers every cycle (solar MPPT
± a few watts, battery current ± a fraction of an amp) even when the
real underlying load is steady — and at 1 Hz this generates one
``ItemsChanged`` signal per second from this service, which every
downstream listener on the bus (gui-v2, vrmlogger, dbus-systemcalc,
mqtt-rpc, ...) has to process.

This module decides when those flickers are worth signalling: a write
to D-Bus only happens when at least one gated path's *rounded* value
has moved by at least its threshold compared to the value we last
emitted.  The published values themselves are still precise — the
rounding is only used for the comparison, not the write.

If no path crosses its threshold the whole ``with self._service`` block
is skipped and vedbus emits no signal at all for that cycle.  A
periodic heartbeat forces an emit at least every
``HEARTBEAT_INTERVAL_S`` regardless, so freshness watchers can tell
the service is alive.
"""

from __future__ import annotations


# Per-path "substantial change" thresholds.  Coarser = quieter bus,
# less responsive.  Values tuned for a 12-48 V DC system where sensor
# noise is sub-watt / sub-amp on the inputs (compounded across many
# sources in the aggregator) but real load events easily exceed.
DC_SYSTEM_THRESHOLDS = {
    "/Dc/0/Power":         25,     # W   — small loads still register at 25 W
    "/Dc/0/Current":       2.0,    # A   — fridge inrush still trips
    "/Dc/0/Voltage":       0.2,    # V   — above bus-voltage noise
    "/History/EnergyIn":   0.01,   # kWh — monotonic counter, ~6 min at 100 W
    # /History/EnergyOut is always 0 in this service, no threshold needed
}

# Force a full write at least this often even when nothing crosses a
# threshold, so freshness watchers can tell the service is alive.
HEARTBEAT_INTERVAL_S = 900


def _is_substantial(new_values: dict, last_values: dict, thresholds: dict) -> bool:
    """Return True iff at least one path in *new_values* has moved
    by at least its threshold (or has no prior value).

    Used inside ``_do_update()`` to decide whether to write to D-Bus
    at all.  Returning False means we skip the whole ``with
    self._service`` block, and vedbus emits no ``ItemsChanged``
    signal for this cycle.

    Semantics:
      * ``None`` values in ``new_values`` are skipped (no data yet).
      * Paths without an entry in ``thresholds`` are skipped (caller
        chose not to gate on them — e.g. EnergyOut which is always 0).
      * ``last_values.get(path) is None`` means "first sample" —
        always substantial.
      * The comparison is ``>=``, so a value sitting exactly on the
        threshold boundary triggers an emit.

    Pure function — no I/O, no clocks, no D-Bus — so it's directly
    testable without standing up the full service.
    """
    for path, val in new_values.items():
        if val is None:
            continue
        threshold = thresholds.get(path)
        if threshold is None:
            continue
        last = last_values.get(path)
        if last is None or abs(val - last) >= threshold:
            return True
    return False
