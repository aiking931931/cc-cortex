"""Tests for concinno.ziq_outcome_bus."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from concinno.ziq_outcome_bus import (
    Outcome,
    ZIQOutcomeBus,
    emit,
    get_bus,
    is_bus_disabled,
)


@pytest.fixture(autouse=True)
def _isolated_bus(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reset the singleton + redirect pin file to ``tmp_path``.

    Every test starts with a fresh bus and a fresh pin file so the
    suite is order-independent and never touches ``~/.concinno``.
    """
    pin_file = tmp_path / "ziq_pinned.json"
    monkeypatch.setenv("CONCINNO_ZIQ_PIN_FILE", str(pin_file))
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_DISABLED", raising=False)
    ZIQOutcomeBus._reset_for_testing()
    yield
    ZIQOutcomeBus._reset_for_testing()


# ── 1. emit + subscribe round trip ─────────────────────────────


def test_subscribe_and_emit_round_trip() -> None:
    bus = get_bus()
    seen: list[Outcome] = []
    bus.subscribe("test.tunable_a", seen.append)
    bus.emit(Outcome(tunable="test.tunable_a", value=1, reward=0.5))
    assert len(seen) == 1
    assert seen[0].tunable == "test.tunable_a"
    assert seen[0].reward == 0.5
    assert seen[0].value == 1


# ── 2. multi-subscriber fanout ─────────────────────────────────


def test_multi_subscriber_fanout() -> None:
    bus = get_bus()
    a: list[Outcome] = []
    b: list[Outcome] = []
    c: list[Outcome] = []
    bus.subscribe("test.fanout", a.append)
    bus.subscribe("test.fanout", b.append)
    bus.subscribe("test.fanout", c.append)
    assert bus.subscriber_count("test.fanout") == 3
    bus.emit(Outcome(tunable="test.fanout", value=2, reward=1.0))
    assert len(a) == len(b) == len(c) == 1


# ── 3. unsubscribe ─────────────────────────────────────────────


def test_unsubscribe_removes_callback_idempotent() -> None:
    bus = get_bus()
    seen: list[Outcome] = []
    unsub = bus.subscribe("test.unsub", seen.append)
    bus.emit(Outcome(tunable="test.unsub", value=1, reward=1.0))
    assert len(seen) == 1
    unsub()
    bus.emit(Outcome(tunable="test.unsub", value=1, reward=1.0))
    assert len(seen) == 1, "unsubscribe should stop further dispatches"
    # Idempotent: second unsub is a no-op.
    unsub()
    assert bus.subscriber_count("test.unsub") == 0


# ── 4. pin → emit no-dispatch ──────────────────────────────────


def test_pin_blocks_dispatch() -> None:
    bus = get_bus()
    seen: list[Outcome] = []
    bus.subscribe("test.pinned", seen.append)
    bus.pin("test.pinned", 42)
    assert bus.is_pinned("test.pinned") is True
    assert bus.pinned_value("test.pinned") == 42
    bus.emit(Outcome(tunable="test.pinned", value=1, reward=1.0))
    assert seen == [], "pinned tunable must not dispatch"


# ── 5. unpin → emit dispatches ─────────────────────────────────


def test_unpin_restores_dispatch() -> None:
    bus = get_bus()
    seen: list[Outcome] = []
    bus.subscribe("test.unpin", seen.append)
    bus.pin("test.unpin", 99)
    bus.emit(Outcome(tunable="test.unpin", value=1, reward=1.0))
    assert seen == []
    bus.unpin("test.unpin")
    assert bus.is_pinned("test.unpin") is False
    bus.emit(Outcome(tunable="test.unpin", value=1, reward=1.0))
    assert len(seen) == 1


# ── 6. concurrent emit thread-safe ─────────────────────────────


def test_concurrent_emit_is_thread_safe() -> None:
    bus = get_bus()
    seen: list[Outcome] = []
    seen_lock = threading.Lock()

    def collector(o: Outcome) -> None:
        with seen_lock:
            seen.append(o)

    bus.subscribe("test.concurrent", collector)

    threads_n = 5
    per_thread = 100

    def producer() -> None:
        for i in range(per_thread):
            bus.emit(
                Outcome(tunable="test.concurrent", value=i, reward=float(i % 2))
            )

    threads = [threading.Thread(target=producer) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) == threads_n * per_thread


# ── 7. is_pinned reads pin file correctly ──────────────────────


def test_is_pinned_reads_external_file_edits(tmp_path: Path) -> None:
    bus = get_bus()
    # Pin file is at the env-redirected path; verify file is the source
    # of truth (an external CLI editing the file should be observed).
    import os

    pin_path = Path(os.environ["CONCINNO_ZIQ_PIN_FILE"])
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    pin_path.write_text(json.dumps({"test.external": "v"}), encoding="utf-8")
    assert bus.is_pinned("test.external") is True
    assert bus.pinned_value("test.external") == "v"


# ── 8. env disabled → emit no-op ───────────────────────────────


def test_env_disabled_makes_emit_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = get_bus()
    seen: list[Outcome] = []
    bus.subscribe("test.disabled", seen.append)
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_DISABLED", "1")
    assert is_bus_disabled() is True
    bus.emit(Outcome(tunable="test.disabled", value=1, reward=1.0))
    assert seen == []
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_DISABLED", "0")
    assert is_bus_disabled() is False
    bus.emit(Outcome(tunable="test.disabled", value=1, reward=1.0))
    assert len(seen) == 1


# ── 9. invalid Outcome raises ──────────────────────────────────


def test_invalid_outcome_raises() -> None:
    with pytest.raises(ValueError):
        Outcome(tunable="", value=1, reward=1.0)
    with pytest.raises(ValueError):
        Outcome(tunable="test.x", value=1, reward=float("nan"))
    with pytest.raises(ValueError):
        Outcome(tunable="test.x", value=1, reward=float("inf"))
    with pytest.raises(TypeError):
        Outcome(tunable="test.x", value="not numeric", reward=1.0)  # type: ignore[arg-type]


# ── 10. decorator @emit captures return reward ─────────────────


def test_decorator_emit_dict_reward() -> None:
    bus = get_bus()
    seen: list[Outcome] = []
    bus.subscribe("test.deco_dict", seen.append)

    @emit("test.deco_dict", source="unit_test")
    def fn(value: int = 1) -> dict:
        return {"reward": 0.7, "tier": "haiku"}

    fn(value=3)
    assert len(seen) == 1
    assert seen[0].reward == 0.7
    assert seen[0].source == "unit_test"


def test_decorator_emit_numeric_reward() -> None:
    bus = get_bus()
    seen: list[Outcome] = []
    bus.subscribe("test.deco_num", seen.append)

    @emit("test.deco_num")
    def fn() -> float:
        return 0.9

    fn()
    assert seen[0].reward == 0.9


def test_decorator_emit_failure_signals_zero_reward() -> None:
    bus = get_bus()
    seen: list[Outcome] = []
    bus.subscribe("test.deco_fail", seen.append)

    @emit("test.deco_fail")
    def fn() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        fn()
    assert len(seen) == 1
    assert seen[0].reward == 0.0
    assert seen[0].metadata.get("raised") is True


# ── 11. escalation pilot smoke ─────────────────────────────────


def test_escalation_pilot_emits_outcome_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify the wire-in at escalation.py emits a success outcome.

    We use ``force_tier="gemma"`` and stub ``_call_gemma`` so we don't
    touch the network. The bus must receive a single Outcome with
    reward computed from ``retries=0`` → 1.0.
    """
    from concinno.escalation import LLMEscalator, TierResult

    bus = get_bus()
    seen: list[Outcome] = []
    bus.subscribe("escalation.max_retries_per_tier", seen.append)

    esc = LLMEscalator(
        chain=("gemma",),
        cache_dir=str(tmp_path / "esc_cache"),
        max_retries_per_tier=2,
    )

    def _fake_call_gemma(
        messages, *, max_tokens, temperature, retries
    ):  # type: ignore[no-untyped-def]
        return TierResult(
            tier="gemma",
            text="ok",
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
            retries=0,
        )

    monkeypatch.setattr(esc, "_call_gemma", _fake_call_gemma)
    res = esc.escalate([{"role": "user", "content": "hi"}])
    assert res.final.text == "ok"
    assert len(seen) == 1
    assert seen[0].tunable == "escalation.max_retries_per_tier"
    assert seen[0].value == 2  # max_retries we configured
    assert seen[0].reward == 1.0  # retries=0 → 1/(1+0) = 1.0
    assert seen[0].metadata.get("tier") == "gemma"


# ── 12. singleton behaviour ────────────────────────────────────


def test_get_bus_returns_singleton() -> None:
    a = get_bus()
    b = ZIQOutcomeBus.get_bus()
    c = get_bus()
    assert a is b is c
    ZIQOutcomeBus._reset_for_testing()
    d = get_bus()
    assert d is not a, "reset should produce a new instance"
