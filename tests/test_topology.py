"""Tests for the measurement-graph helpers in ``topology.py``.

The topology decides which battery services form the battery term in
``dc_load = sum(sources) - battery``.  Wrong semantics here means the
battery term flaps between correct and double-counted, feeding DVCC a
wrong DC load (observed live: /Dc/0/Power alternating 183 W / 280 W
because a transient None on /Measurement/PhysicalDevice collapsed the
graph for one cycle and the code fell through to summing both the BMS
pair and the shunt pair).
"""

from __future__ import annotations

import topology


# Mirrors the production topology that surfaced the bug: two BLE BMS
# services each declaring a paired Victron shunt (which cannot declare
# itself) as peer and line authority.
BLE_A = "com.victronenergy.battery.ble_5320b7d7f9e7"
BLE_B = "com.victronenergy.battery.ble_ab807254e0b4"
SHUNT_A = "com.victronenergy.battery.ttyS5"
SHUNT_B = "com.victronenergy.battery.ttyS6"
AGG = "com.victronenergy.battery.aggregate"

FULL_BUS = {
    BLE_A: {
        "/Measurement/Kind": "direct",
        "/Measurement/PhysicalDevice": "pack-a",
        "/Measurement/PeerServices": SHUNT_A,
        "/Measurement/LineAuthority": SHUNT_A,
    },
    BLE_B: {
        "/Measurement/Kind": "direct",
        "/Measurement/PhysicalDevice": "pack-b",
        "/Measurement/PeerServices": SHUNT_B,
        "/Measurement/LineAuthority": SHUNT_B,
    },
    SHUNT_A: {},  # Victron shunt: no /Measurement declarations
    SHUNT_B: {},
    AGG: {},      # aggregate (instance 99): no declarations either
}


def make_read(bus):
    return lambda svc, path: bus.get(svc, {}).get(path)


def derive(bus):
    return topology.derive_groups(list(bus), make_read(bus))


class TestDeriveGroups:
    def test_full_declarations(self):
        groups, claimed = derive(FULL_BUS)
        assert set(groups) == {"pack-a", "pack-b"}
        assert groups["pack-a"] == {
            "declarer": BLE_A,
            "authority": SHUNT_A,
            "members": {BLE_A, SHUNT_A},
        }
        assert claimed == {BLE_A, BLE_B, SHUNT_A, SHUNT_B}

    def test_no_declarations(self):
        bus = {SHUNT_A: {}, SHUNT_B: {}}
        groups, claimed = derive(bus)
        assert groups == {}
        assert claimed == set()

    def test_derived_service_is_claimed_but_not_grouped(self):
        bus = dict(FULL_BUS)
        bus[AGG] = {"/Measurement/Kind": "derived"}
        groups, claimed = derive(bus)
        assert set(groups) == {"pack-a", "pack-b"}
        assert AGG in claimed

    def test_direct_without_physical_device_is_skipped(self):
        # The flaky-read shape: Kind present, PhysicalDevice reads None.
        bus = {BLE_A: {"/Measurement/Kind": "direct"}}
        groups, claimed = derive(bus)
        assert groups == {}
        assert claimed == set()

    def test_two_declarers_same_pack_share_one_group(self):
        bus = {
            BLE_A: {
                "/Measurement/Kind": "direct",
                "/Measurement/PhysicalDevice": "pack-a",
            },
            SHUNT_A: {
                "/Measurement/Kind": "direct",
                "/Measurement/PhysicalDevice": "pack-a",
            },
        }
        groups, _ = derive(bus)
        assert set(groups) == {"pack-a"}
        assert groups["pack-a"]["members"] == {BLE_A, SHUNT_A}

    def test_multiple_peers_parsed_from_csv(self):
        bus = {
            BLE_A: {
                "/Measurement/Kind": "direct",
                "/Measurement/PhysicalDevice": "pack-a",
                "/Measurement/PeerServices": " %s , %s " % (SHUNT_A, SHUNT_B),
            },
        }
        groups, claimed = derive(bus)
        assert groups["pack-a"]["members"] == {BLE_A, SHUNT_A, SHUNT_B}
        assert claimed == {BLE_A, SHUNT_A, SHUNT_B}


class TestMergeTopology:
    """The stability core: a flaky read must never shrink the topology."""

    def setup_method(self):
        self.cached, self.cached_claimed = derive(FULL_BUS)

    def test_identical_fresh_derivation_is_a_noop(self):
        fresh, fresh_claimed = derive(FULL_BUS)
        merged, claimed, changes, retained = topology.merge_topology(
            self.cached, self.cached_claimed, fresh, fresh_claimed,
            set(FULL_BUS),
        )
        assert merged == self.cached
        assert claimed == self.cached_claimed
        assert changes == []
        assert retained == []

    def test_flaky_read_keeps_cached_group(self):
        # BLE_B's declarations all read None this cycle, but the
        # service is still on the bus -> its group must survive.
        bus = dict(FULL_BUS)
        bus[BLE_B] = {}
        fresh, fresh_claimed = derive(bus)
        assert set(fresh) == {"pack-a"}  # precondition: derivation degraded
        merged, claimed, changes, retained = topology.merge_topology(
            self.cached, self.cached_claimed, fresh, fresh_claimed, set(bus),
        )
        assert merged == self.cached
        assert claimed == self.cached_claimed
        assert changes == []
        assert len(retained) == 1
        assert BLE_B in retained[0]

    def test_all_reads_flaky_keeps_everything(self):
        bus = {svc: {} for svc in FULL_BUS}
        fresh, fresh_claimed = derive(bus)
        merged, claimed, changes, retained = topology.merge_topology(
            self.cached, self.cached_claimed, fresh, fresh_claimed, set(bus),
        )
        assert merged == self.cached
        assert claimed == self.cached_claimed
        assert changes == []
        assert len(retained) == 2

    def test_declarer_leaving_bus_drops_its_group(self):
        bus = {k: v for k, v in FULL_BUS.items() if k != BLE_B}
        fresh, fresh_claimed = derive(bus)
        merged, claimed, changes, retained = topology.merge_topology(
            self.cached, self.cached_claimed, fresh, fresh_claimed, set(bus),
        )
        assert set(merged) == {"pack-a"}
        # SHUNT_B is still on the bus and no longer claimed by any
        # group -> legacy summation may pick it up (correct: its pack
        # has no other observer left).  BLE_B left, so its claim goes.
        assert BLE_B not in claimed
        assert any("dropped group pack-b" in msg for msg in changes)
        assert retained == []

    def test_late_declaration_adds_group(self):
        # Start with only pack-a cached (BLE_B's declarations appeared
        # a few cycles after the service did).
        bus_a_only = dict(FULL_BUS)
        bus_a_only[BLE_B] = {}
        cached, cached_claimed = derive(bus_a_only)
        fresh, fresh_claimed = derive(FULL_BUS)
        merged, claimed, changes, retained = topology.merge_topology(
            cached, cached_claimed, fresh, fresh_claimed, set(FULL_BUS),
        )
        assert set(merged) == {"pack-a", "pack-b"}
        assert SHUNT_B in claimed
        assert any("new group pack-b" in msg for msg in changes)

    def test_fresh_declaration_updates_cached_group(self):
        bus = dict(FULL_BUS)
        bus[BLE_A] = dict(bus[BLE_A])
        bus[BLE_A]["/Measurement/LineAuthority"] = BLE_A
        fresh, fresh_claimed = derive(bus)
        merged, _, changes, _ = topology.merge_topology(
            self.cached, self.cached_claimed, fresh, fresh_claimed, set(bus),
        )
        assert merged["pack-a"]["authority"] == BLE_A
        assert any("group pack-a updated" in msg for msg in changes)

    def test_derived_claim_is_sticky_across_flaky_read(self):
        bus = dict(FULL_BUS)
        bus[AGG] = {"/Measurement/Kind": "derived"}
        cached, cached_claimed = derive(bus)
        assert AGG in cached_claimed
        # Next cycle AGG's Kind reads None, but it's still on the bus.
        flaky = dict(bus)
        flaky[AGG] = {}
        fresh, fresh_claimed = derive(flaky)
        _, claimed, _, _ = topology.merge_topology(
            cached, cached_claimed, fresh, fresh_claimed, set(flaky),
        )
        assert AGG in claimed

    def test_merged_groups_are_independent_copies(self):
        fresh, fresh_claimed = derive(FULL_BUS)
        merged, _, _, _ = topology.merge_topology(
            self.cached, self.cached_claimed, fresh, fresh_claimed,
            set(FULL_BUS),
        )
        merged["pack-a"]["members"].add("mutant")
        assert "mutant" not in fresh["pack-a"]["members"]
        assert "mutant" not in self.cached["pack-a"]["members"]


class TestSelectRepresentative:
    def make_group(self):
        return {
            "declarer": BLE_A,
            "authority": SHUNT_A,
            "members": {BLE_A, SHUNT_A},
        }

    def test_authority_preferred_when_publishing(self):
        rep = topology.select_representative(
            self.make_group(), lambda svc: True
        )
        assert rep == SHUNT_A

    def test_falls_back_to_declarer(self):
        rep = topology.select_representative(
            self.make_group(), lambda svc: svc != SHUNT_A
        )
        assert rep == BLE_A

    def test_falls_back_to_members(self):
        group = self.make_group()
        group["members"].add(SHUNT_B)
        rep = topology.select_representative(
            group, lambda svc: svc == SHUNT_B
        )
        assert rep == SHUNT_B

    def test_none_when_nothing_publishes(self):
        rep = topology.select_representative(
            self.make_group(), lambda svc: False
        )
        assert rep is None

    def test_no_authority_starts_at_declarer(self):
        group = self.make_group()
        group["authority"] = None
        rep = topology.select_representative(group, lambda svc: True)
        assert rep == BLE_A
