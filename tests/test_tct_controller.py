"""TCT Controller tests — mirrors tct-core/tests/controller.test.ts."""

from __future__ import annotations

from cc_cortex.ziq_control import (
    TctConfig,
    TctSignal,
    compute_riverbed,
    compute_tension,
    create_tct_state,
    detect_oscillation,
    tct_control,
    tct_control_stateful,
)

# ── compute_riverbed ──


class TestComputeRiverbed:
    def test_empty_history(self) -> None:
        assert compute_riverbed([], 20, 0.95) == 0.0

    def test_single_value(self) -> None:
        assert compute_riverbed([5.0], 20, 0.95) == 5.0

    def test_ema_with_decay(self) -> None:
        r = compute_riverbed([1, 2, 3, 4, 5], 20, 0.9)
        assert 1 < r < 5

    def test_respects_window(self) -> None:
        long = list(range(100))
        r = compute_riverbed(long, 5, 0.9)
        assert r > 90


# ── compute_tension ──


class TestComputeTension:
    def test_zero_deviation(self) -> None:
        assert compute_tension(5, 5) == 0

    def test_positive(self) -> None:
        assert compute_tension(7, 5) > 0

    def test_negative(self) -> None:
        assert compute_tension(3, 5) < 0

    def test_zero_riverbed(self) -> None:
        assert compute_tension(0, 0) == 0
        assert compute_tension(1, 0) == 1
        assert compute_tension(-1, 0) == -1


# ── detect_oscillation ──


class TestDetectOscillation:
    def test_short_history(self) -> None:
        assert detect_oscillation([1, 2], 3) is False

    def test_monotonic(self) -> None:
        assert detect_oscillation([1, 2, 3, 4, 5, 6], 3) is False

    def test_alternating(self) -> None:
        assert detect_oscillation([1, 2, 1, 2, 1, 2], 3) is True

    def test_below_threshold(self) -> None:
        assert detect_oscillation([1, 2, 1, 2], 3) is False


# ── tct_control (stateless) ──


class TestTctControl:
    def test_increase_below_floor(self) -> None:
        signal = TctSignal(current=0.1, history=[0.5, 0.4, 0.3, 0.2])
        cfg = TctConfig(floor=0.3, ceiling=0.8)
        r = tct_control(signal, cfg)
        assert r.decision == "increase"
        assert r.frozen is False
        assert r.magnitude > 0

    def test_decrease_above_ceiling(self) -> None:
        signal = TctSignal(current=0.9, history=[0.5, 0.6, 0.7, 0.8])
        cfg = TctConfig(floor=0.3, ceiling=0.8)
        r = tct_control(signal, cfg)
        assert r.decision == "decrease"
        assert r.frozen is False

    def test_freeze_on_oscillation(self) -> None:
        signal = TctSignal(
            current=0.6,
            history=[0.5, 0.6, 0.5, 0.6, 0.5, 0.6],
        )
        cfg = TctConfig(floor=0.0, ceiling=1.0, freeze_oscillation_count=3)
        r = tct_control(signal, cfg)
        assert r.decision == "freeze"
        assert r.frozen is True
        assert r.magnitude == 0

    def test_stable(self) -> None:
        signal = TctSignal(
            current=0.5,
            history=[0.49, 0.50, 0.50, 0.51, 0.50],
        )
        cfg = TctConfig(floor=0.0, ceiling=1.0)
        r = tct_control(signal, cfg)
        assert r.magnitude == 0
        assert r.frozen is False
        assert "stable" in r.reason

    def test_high_tension_within_bounds(self) -> None:
        signal = TctSignal(current=0.8, history=[0.3, 0.3, 0.3, 0.3, 0.3])
        cfg = TctConfig(floor=0.0, ceiling=1.0, tension_threshold=0.3)
        r = tct_control(signal, cfg)
        assert r.decision == "decrease"
        assert r.tension > 0.3
        assert r.magnitude > 0


# ── tct_control_stateful ──


class TestTctControlStateful:
    def test_freeze_duration(self) -> None:
        osc = TctSignal(
            current=0.6,
            history=[0.5, 0.6, 0.5, 0.6, 0.5, 0.6],
        )
        cfg = TctConfig(
            floor=0.0, ceiling=1.0,
            freeze_oscillation_count=3, freeze_duration=3,
        )

        r1, s1 = tct_control_stateful(osc, create_tct_state(), cfg)
        assert r1.frozen is True
        assert s1.freeze_remaining == 3

        normal = TctSignal(current=0.5, history=[0.5, 0.5, 0.5])
        r2, s2 = tct_control_stateful(normal, s1, cfg)
        assert r2.frozen is True
        assert s2.freeze_remaining == 2

        r3, s3 = tct_control_stateful(normal, s2, cfg)
        assert r3.frozen is True
        assert s3.freeze_remaining == 1

        r4, s4 = tct_control_stateful(normal, s3, cfg)
        assert r4.frozen is True
        assert s4.freeze_remaining == 0

        r5, _ = tct_control_stateful(normal, s4, cfg)
        assert r5.frozen is False

    def test_step_counter(self) -> None:
        signal = TctSignal(current=0.5, history=[0.5])
        _, s1 = tct_control_stateful(signal, create_tct_state())
        assert s1.step == 1
        _, s2 = tct_control_stateful(signal, s1)
        assert s2.step == 2


# ── D92 Freeze Principle ──


class TestD92FreezePrinciple:
    def test_freezes_during_spike(self) -> None:
        normal = [100, 102, 98, 101, 99, 103, 97, 100, 102, 98]
        signal = TctSignal(
            current=1000,
            history=[*normal, 1000, 100, 1000, 100, 1000],
        )
        cfg = TctConfig(floor=50, ceiling=200, freeze_oscillation_count=3)
        r = tct_control(signal, cfg)
        assert r.decision == "freeze"
        assert r.frozen is True
