"""Tests for ZIQ outcome bus rate-limit guard (race-condition fix).

Covers the rate limiter added in plan §244 (Plan C 2026-04-28):
emits beyond ``CONCINNO_ZIQ_BUS_MAX_HZ`` events/sec/tunable are
silently dropped and counted via ``dropped_count(tunable)``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from concinno.ziq_outcome_bus import Outcome, ZIQOutcomeBus, get_bus


@pytest.fixture(autouse=True)
def _isolated_bus(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pin_file = tmp_path / "ziq_pinned.json"
    monkeypatch.setenv("CONCINNO_ZIQ_PIN_FILE", str(pin_file))
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_DISABLED", raising=False)
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_MAX_HZ", raising=False)
    ZIQOutcomeBus._reset_for_testing()
    yield
    ZIQOutcomeBus._reset_for_testing()


def test_default_rate_limit_admits_below_threshold() -> None:
    bus = get_bus()
    seen: list[Outcome] = []
    bus.subscribe("rate.test", seen.append)
    # Default = 10 000 Hz (raised from 100 Hz in 4.4.0 to keep the
    # FTRL learning signal from being silently dropped under realistic
    # producer bursts — see module docstring). Five emits trivially
    # pass.
    for _ in range(5):
        bus.emit(Outcome(tunable="rate.test", value=1, reward=1.0))
    assert len(seen) == 5
    assert bus.dropped_count("rate.test") == 0


def test_rate_limit_drops_beyond_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_MAX_HZ", "3")
    bus = get_bus()
    seen: list[Outcome] = []
    bus.subscribe("rate.test", seen.append)
    for _ in range(10):
        bus.emit(Outcome(tunable="rate.test", value=1, reward=1.0))
    # Window is 1 second, max 3 → 3 admitted, 7 dropped.
    assert len(seen) == 3
    assert bus.dropped_count("rate.test") == 7


def test_rate_limit_independent_per_tunable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_MAX_HZ", "2")
    bus = get_bus()
    seen_a: list[Outcome] = []
    seen_b: list[Outcome] = []
    bus.subscribe("rate.tunable_a", seen_a.append)
    bus.subscribe("rate.tunable_b", seen_b.append)
    for _ in range(5):
        bus.emit(Outcome(tunable="rate.tunable_a", value=1, reward=1.0))
        bus.emit(Outcome(tunable="rate.tunable_b", value=1, reward=1.0))
    # Each tunable gets its own 2 Hz budget.
    assert len(seen_a) == 2
    assert len(seen_b) == 2
    assert bus.dropped_count("rate.tunable_a") == 3
    assert bus.dropped_count("rate.tunable_b") == 3


def test_invalid_max_hz_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_MAX_HZ", "not_a_number")
    bus = get_bus()
    seen: list[Outcome] = []
    bus.subscribe("rate.test", seen.append)
    # Bad env → fallback to default (10 000), so 50 emits all admitted.
    for _ in range(50):
        bus.emit(Outcome(tunable="rate.test", value=1, reward=1.0))
    assert len(seen) == 50


def test_zero_or_negative_max_hz_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_MAX_HZ", "0")
    bus = get_bus()
    seen: list[Outcome] = []
    bus.subscribe("rate.test", seen.append)
    bus.emit(Outcome(tunable="rate.test", value=1, reward=1.0))
    assert len(seen) == 1


def test_reset_rate_state_clears_window_and_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_MAX_HZ", "2")
    bus = get_bus()
    seen: list[Outcome] = []
    bus.subscribe("rate.test", seen.append)
    for _ in range(5):
        bus.emit(Outcome(tunable="rate.test", value=1, reward=1.0))
    assert len(seen) == 2
    assert bus.dropped_count("rate.test") == 3
    bus.reset_rate_state("rate.test")
    assert bus.dropped_count("rate.test") == 0
    # Budget refreshed — next 2 admitted.
    bus.emit(Outcome(tunable="rate.test", value=1, reward=1.0))
    bus.emit(Outcome(tunable="rate.test", value=1, reward=1.0))
    assert len(seen) == 4


def test_reset_rate_state_global() -> None:
    bus = get_bus()
    bus._dropped["a"] = 5
    bus._dropped["b"] = 3
    bus.reset_rate_state(None)
    assert bus.dropped_count("a") == 0
    assert bus.dropped_count("b") == 0


def test_dropped_emits_do_not_dispatch_to_subscribers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_MAX_HZ", "1")
    bus = get_bus()
    call_count = {"n": 0}

    def cb(_: Outcome) -> None:
        call_count["n"] += 1

    bus.subscribe("rate.test", cb)
    for _ in range(5):
        bus.emit(Outcome(tunable="rate.test", value=1, reward=1.0))
    assert call_count["n"] == 1


def test_default_rate_limit_is_high_enough_for_realistic_workload() -> None:
    """4.4.0 ship-gate FATAL-5 regression — realistic burst stays admitted.

    The pre-4.4.0 default of 100 Hz/tunable silently dropped > 90 % of
    outcome emits in production traces of the escalation retry chain
    (~50 kHz steady-state) and the sentinel fail loop (~5 kHz). With
    the new 10 000 Hz/tunable default, 5 producer threads emitting
    1 000 outcomes apiece into a single tunable (5 000 events in
    < 1 s, well under 10 kHz) all land. If this test ever drops back
    to a single-digit-percent admit rate the default has regressed.
    """
    import threading

    bus = get_bus()
    seen: list[Outcome] = []
    seen_lock = threading.Lock()

    def collector(o: Outcome) -> None:
        with seen_lock:
            seen.append(o)

    bus.subscribe("realistic.workload", collector)

    threads_n = 5
    per_thread = 1000

    def producer() -> None:
        for i in range(per_thread):
            bus.emit(
                Outcome(
                    tunable="realistic.workload",
                    value=i,
                    reward=float(i % 2),
                )
            )

    threads = [threading.Thread(target=producer) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All 5 000 emits should land — proves the 10 000 Hz default sits
    # comfortably above realistic burst rates and that no signal is
    # silently dropped.
    expected = threads_n * per_thread
    assert len(seen) == expected, (
        f"realistic-workload regression: only {len(seen)}/{expected} "
        f"emits admitted (dropped={bus.dropped_count('realistic.workload')})"
    )
    assert bus.dropped_count("realistic.workload") == 0
