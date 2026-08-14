# Copyright 2025 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""
Pure-Python helpers for resolving the declared measurement graph.

Battery services may publish /Measurement/* declarations (see
mr-manuel/venus-os_dbus-serialbattery#496):

  Kind            "direct" (own sensors) or "derived" (aggregator)
  PhysicalDevice  ID of the physical pack the service observes
  PeerServices    other direct observers of the same pack (e.g. a
                  SmartShunt wired in series, which cannot declare
                  itself)
  LineAuthority   the peer best suited for line V/I sums

Design principle: **topology is not a per-cycle fact**.  The graph
describes which physical pack each service observes — that only
changes when hardware or drivers change, i.e. when battery services
appear on or leave the bus.  A transient ``None`` from the dbus value
cache (a flaky ``/Measurement/PhysicalDevice`` read) must never
collapse a group and silently downgrade the battery term to a worse
source: one flaky read = one double-counted battery term = one wrong
DC-load emit that DVCC corrects against.

Accordingly the service derives a fresh graph every cycle (the reads
are served from DbusMonitor's in-memory cache, so this is cheap) but
merges it into the cached topology with ``merge_topology``: a cached
group is only dropped when its declarer actually leaves the bus, never
because its declarations read as ``None`` this cycle.

Only representative *selection* within a group is a per-cycle fact —
whichever member currently publishes V+I gets to speak for the pack.

Pure functions — no I/O, no clocks, no D-Bus — so they're directly
testable without standing up the full service.
"""

from __future__ import annotations


def derive_groups(services, read):
    """One derivation pass over the battery *services*.

    ``read(service, path)`` returns the current value or ``None``.

    Returns ``(groups, claimed)``:

      groups   {physical_device_id: {"declarer": svc,
                                     "authority": svc-or-None,
                                     "members": set-of-svc}}
      claimed  set of ALL services referenced by any declaration
               (group members, peers, and "derived" aggregators), so
               the caller can exclude them from legacy summation.

    Both are empty when no service declares anything.
    """
    groups = {}
    claimed = set()
    for svc in services:
        kind = read(svc, "/Measurement/Kind")
        if kind == "derived":
            claimed.add(svc)
            continue
        if kind != "direct":
            continue
        phys = read(svc, "/Measurement/PhysicalDevice")
        if not phys:
            continue
        group = groups.setdefault(
            phys, {"declarer": svc, "authority": None, "members": set()}
        )
        group["members"].add(svc)
        claimed.add(svc)
        peers = read(svc, "/Measurement/PeerServices") or ""
        for peer in str(peers).split(","):
            peer = peer.strip()
            if peer:
                group["members"].add(peer)
                claimed.add(peer)
        authority = read(svc, "/Measurement/LineAuthority")
        if authority:
            group["authority"] = str(authority).strip()
    return groups, claimed


def _copy_group(group):
    return {
        "declarer": group["declarer"],
        "authority": group["authority"],
        "members": set(group["members"]),
    }


def merge_topology(cached_groups, cached_claimed, fresh_groups, fresh_claimed,
                   present):
    """Merge a fresh derivation into the cached topology.

    *present* is the set of battery services currently on the bus.

    Rules:
      * A group present in the fresh derivation is adopted as-is (a
        live declaration is authoritative for its own pack).
      * A cached group missing from the fresh derivation is KEPT if
        its declarer is still on the bus — the declarations read as
        ``None`` this cycle, which is a flaky-cache artifact, not a
        topology change.
      * A cached group whose declarer left the bus is dropped.
      * ``claimed`` stays sticky: everything claimed by the merged
        groups, plus fresh claims, plus prior claims for services
        still on the bus (so a "derived" aggregator stays excluded
        from legacy summation even on a cycle where its Kind reads
        as ``None``).

    Returns ``(groups, claimed, changes, retained)`` where *changes*
    is a list of human-readable topology-change descriptions (log at
    INFO — these are edge-triggered) and *retained* lists groups kept
    despite a flaky read (log at DEBUG — may recur every cycle while
    the read stays flaky).
    """
    changes = []
    retained = []

    merged = {phys: _copy_group(g) for phys, g in fresh_groups.items()}
    for phys, group in cached_groups.items():
        if phys in merged:
            if (merged[phys]["declarer"] != group["declarer"]
                    or merged[phys]["authority"] != group["authority"]
                    or merged[phys]["members"] != group["members"]):
                changes.append(
                    "group %s updated (declarer=%s authority=%s members=%s)"
                    % (phys, merged[phys]["declarer"],
                       merged[phys]["authority"],
                       ",".join(sorted(merged[phys]["members"])))
                )
            continue
        if group["declarer"] in present:
            merged[phys] = _copy_group(group)
            retained.append(
                "kept cached group %s (flaky /Measurement read on %s)"
                % (phys, group["declarer"])
            )
        else:
            changes.append(
                "dropped group %s (declarer %s left the bus)"
                % (phys, group["declarer"])
            )
    for phys in merged:
        if phys not in cached_groups:
            changes.append(
                "new group %s (declarer %s, members %s)"
                % (phys, merged[phys]["declarer"],
                   ",".join(sorted(merged[phys]["members"])))
            )

    claimed = set(fresh_claimed)
    for group in merged.values():
        claimed |= group["members"]
    claimed |= {svc for svc in cached_claimed if svc in present}

    return merged, claimed, changes, retained


def select_representative(group, has_vi):
    """Pick the service that speaks for a group's pack this cycle.

    Preference order: LineAuthority, then the declarer, then remaining
    members in sorted order — first one for which ``has_vi(svc)`` is
    True (i.e. it currently publishes both /Dc/0/Voltage and
    /Dc/0/Current).  Returns ``None`` if nothing in the group
    currently publishes V+I.
    """
    candidates = []
    if group["authority"]:
        candidates.append(group["authority"])
    candidates.append(group["declarer"])
    candidates.extend(sorted(group["members"]))
    for cand in candidates:
        if has_vi(cand):
            return cand
    return None
