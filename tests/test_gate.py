"""Tests for the emit-gating helpers in ``gate.py``.

The gating logic decides whether ``_do_update()`` should write to D-Bus
on a given cycle.  Wrong semantics here means either spurious emits
(perf regression — every downstream listener has more work) or missed
updates (consumers see a stale DC system load value, which affects
DVCC's charge-current allocation).
"""

from __future__ import annotations

import pytest

import gate


# Convenient test-only thresholds — easier to reason about than the
# production values.  Tests against production thresholds use
# ``gate.DC_SYSTEM_THRESHOLDS`` directly.
T = {
    "/P": 25.0,
    "/I": 2.0,
    "/V": 0.2,
}


class TestNoLastValues:
    """First call (empty last_values dict) — any non-None new value is substantial."""

    def test_empty_last_with_value(self):
        assert gate._is_substantial({"/P": 100.0}, {}, T) is True

    def test_empty_last_all_none(self):
        assert gate._is_substantial({"/P": None, "/V": None}, {}, T) is False

    def test_empty_last_some_none(self):
        assert gate._is_substantial({"/P": None, "/V": 13.0}, {}, T) is True


class TestThresholdBoundary:
    """The comparison is ``>=`` so a value exactly at the threshold triggers."""

    def test_exactly_at_threshold(self):
        # /P threshold is 25.  Delta of exactly 25 → substantial.
        assert gate._is_substantial({"/P": 125.0}, {"/P": 100.0}, T) is True

    def test_just_under_threshold(self):
        assert gate._is_substantial({"/P": 124.99}, {"/P": 100.0}, T) is False

    def test_just_over_threshold(self):
        assert gate._is_substantial({"/P": 125.01}, {"/P": 100.0}, T) is True

    def test_exact_equal(self):
        assert gate._is_substantial({"/P": 100.0}, {"/P": 100.0}, T) is False


class TestSignedDelta:
    """Use ``abs()`` so moves in either direction trigger equally."""

    def test_negative_move_at_threshold(self):
        assert gate._is_substantial({"/P": 75.0}, {"/P": 100.0}, T) is True

    def test_negative_move_under_threshold(self):
        assert gate._is_substantial({"/P": 76.0}, {"/P": 100.0}, T) is False

    def test_zero_crossing(self):
        # Voltage transient — bus voltage drops from 13.2 to 12.9, delta 0.3 → substantial.
        assert gate._is_substantial({"/V": 12.9}, {"/V": 13.2}, T) is True


class TestNoneAndMissing:
    """None values are skipped; ungated paths are skipped."""

    def test_none_new_is_skipped(self):
        assert gate._is_substantial({"/P": None}, {"/P": 100.0}, T) is False

    def test_path_without_threshold_is_skipped(self):
        # Test thresholds dict has only /P, /I, /V — /Other has no
        # threshold, so should never trigger on its own.
        assert gate._is_substantial({"/Other": 999.0}, {}, T) is False

    def test_path_without_threshold_does_not_mask_gated_path(self):
        # /Other looks unchanged, but /P crossed → substantial.
        assert gate._is_substantial(
            {"/Other": 1.0, "/P": 200.0},
            {"/Other": 1.0, "/P": 100.0},
            T,
        ) is True


class TestMultiplePaths:
    """Any one path crossing its threshold is enough — short-circuit semantics."""

    def test_one_of_three_crosses(self):
        new = {"/P": 100.0, "/I": 5.0, "/V": 13.5}
        last = {"/P": 100.0, "/I": 5.0, "/V": 13.2}  # /V moved 0.3 ≥ 0.2
        assert gate._is_substantial(new, last, T) is True

    def test_none_cross(self):
        # All three within their thresholds:
        # /P 100 → 110 (delta 10 < 25), /I 5 → 6 (delta 1 < 2), /V 13 → 13.1 (delta 0.1 < 0.2)
        new = {"/P": 110.0, "/I": 6.0, "/V": 13.1}
        last = {"/P": 100.0, "/I": 5.0, "/V": 13.0}
        assert gate._is_substantial(new, last, T) is False


class TestProductionThresholds:
    """Smoke tests against ``DC_SYSTEM_THRESHOLDS`` so a future tweak
    to those values doesn't silently flip behaviour on a real scenario."""

    def test_steady_idle_no_emit(self):
        # Typical idle: DC load 0-5 W noise, voltage 13.80 V steady, current 0.
        # All sub-threshold.
        new = {
            "/Dc/0/Power":       5.0,
            "/Dc/0/Current":     0.3,
            "/Dc/0/Voltage":     13.81,
            "/History/EnergyIn": 12.4567,
        }
        last = {
            "/Dc/0/Power":       0.0,
            "/Dc/0/Current":     0.0,
            "/Dc/0/Voltage":     13.80,
            "/History/EnergyIn": 12.4560,  # +0.7 mWh, well below 10 Wh threshold
        }
        assert gate._is_substantial(new, last, gate.DC_SYSTEM_THRESHOLDS) is False

    def test_fridge_kicks_on(self):
        # DC load jumps from 5 W to 200 W → 195 W delta ≥ 25 → substantial.
        new = {"/Dc/0/Power": 200.0}
        last = {"/Dc/0/Power": 5.0}
        assert gate._is_substantial(new, last, gate.DC_SYSTEM_THRESHOLDS) is True

    def test_energy_accumulates_to_threshold(self):
        # 0.01 kWh is the EnergyIn threshold.  10 Wh = exactly the threshold.
        new = {"/History/EnergyIn": 1.010}
        last = {"/History/EnergyIn": 1.000}
        assert gate._is_substantial(new, last, gate.DC_SYSTEM_THRESHOLDS) is True

    def test_energy_just_under_threshold(self):
        new = {"/History/EnergyIn": 1.009}
        last = {"/History/EnergyIn": 1.000}
        assert gate._is_substantial(new, last, gate.DC_SYSTEM_THRESHOLDS) is False


class TestThresholdCoverage:
    """Lock in the set of paths gated so future additions are a forced decision."""

    def test_expected_paths_in_thresholds(self):
        expected = {
            "/Dc/0/Power", "/Dc/0/Current", "/Dc/0/Voltage",
            "/History/EnergyIn",
        }
        assert set(gate.DC_SYSTEM_THRESHOLDS.keys()) == expected

    def test_energyout_intentionally_not_gated(self):
        # /History/EnergyOut is always 0 in this service, so gating it
        # would never trigger — exclude it explicitly so reviewers see
        # the choice was deliberate.
        assert "/History/EnergyOut" not in gate.DC_SYSTEM_THRESHOLDS


class TestHeartbeatConstant:
    def test_heartbeat_is_fifteen_minutes(self):
        assert gate.HEARTBEAT_INTERVAL_S == 900
