"""Tests for concinno.persona_prompt."""

from __future__ import annotations

from concinno.persona_prompt import (
    A_COMM,
    C_COMM,
    E_COMM,
    N_COMM,
    O_COMM,
    O_THINK,
    DimBehavior,
    build_behavior_injection,
    pick_behavior,
)

# ── DimBehavior dataclass ──────────────────────────────────────


def test_dim_behavior_fields() -> None:
    dim = DimBehavior(low="L", mid="M", high="H")
    assert dim.low == "L"
    assert dim.mid == "M"
    assert dim.high == "H"


def test_dim_behavior_frozen() -> None:
    dim = DimBehavior(low="L", mid="M", high="H")
    try:
        dim.low = "X"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("DimBehavior should be frozen")


def test_module_constants_are_dim_behaviors() -> None:
    for dim in (O_COMM, O_THINK, C_COMM, E_COMM, A_COMM, N_COMM):
        assert isinstance(dim, DimBehavior)
        assert dim.low and dim.mid and dim.high


# ── pick_behavior threshold logic ──────────────────────────────


def test_pick_behavior_low() -> None:
    dim = DimBehavior(low="LOW", mid="MID", high="HIGH")
    assert pick_behavior(0.0, dim) == "LOW"
    assert pick_behavior(0.34, dim) == "LOW"


def test_pick_behavior_mid() -> None:
    dim = DimBehavior(low="LOW", mid="MID", high="HIGH")
    assert pick_behavior(0.35, dim) == "MID"
    assert pick_behavior(0.5, dim) == "MID"
    assert pick_behavior(0.65, dim) == "MID"


def test_pick_behavior_high() -> None:
    dim = DimBehavior(low="LOW", mid="MID", high="HIGH")
    assert pick_behavior(0.66, dim) == "HIGH"
    assert pick_behavior(1.0, dim) == "HIGH"


def test_pick_behavior_exact_thresholds() -> None:
    dim = DimBehavior(low="L", mid="M", high="H")
    # 0.35 is >= 0.35, not < → mid
    assert pick_behavior(0.35, dim) == "M"
    # 0.65 is not > 0.65 → mid
    assert pick_behavior(0.65, dim) == "M"


# ── build_behavior_injection output shape ──────────────────────


def test_build_injection_valid_five_dims() -> None:
    out = build_behavior_injection([0.5, 0.5, 0.5, 0.5, 0.5])
    assert "[Persona Behavior Profile]" in out
    # Four lines total: header + communication + thinking + expression
    assert out.count("\n") == 3


def test_build_injection_line_structure() -> None:
    out = build_behavior_injection([0.5, 0.5, 0.5, 0.5, 0.5])
    lines = out.split("\n")
    assert lines[0] == "[Persona Behavior Profile]"
    assert lines[1].startswith("Communication: ")
    assert lines[2].startswith("Thinking: ")
    assert lines[3].startswith("Expression: ")


def test_build_injection_high_openness_uses_high_comm() -> None:
    # O at 0.9 should pull high openness phrases for O_COMM/O_THINK
    out = build_behavior_injection([0.9, 0.5, 0.5, 0.5, 0.5])
    assert "Rich vocabulary" in out or "Abstract thinker" in out


def test_build_injection_low_openness_uses_low_comm() -> None:
    out = build_behavior_injection([0.1, 0.5, 0.5, 0.5, 0.5])
    assert "Concrete and direct" in out or "fact-based" in out


def test_build_injection_wrong_dim_count_returns_empty() -> None:
    assert build_behavior_injection([0.5, 0.5, 0.5]) == ""
    assert build_behavior_injection([]) == ""
    assert build_behavior_injection([0.5] * 6) == ""


def test_build_injection_custom_ocean_dims_arg() -> None:
    # Non-standard dim count can be opted into by caller
    # (though 3-dim won't unpack into o,c,e,a,n — should still
    # match length check then fail at unpack; this test just
    # confirms the length gate honors ocean_dims=5 default).
    out = build_behavior_injection(
        [0.5, 0.5, 0.5, 0.5, 0.5],
        ocean_dims=5,
    )
    assert "[Persona Behavior Profile]" in out


def test_build_injection_custom_ocean_dims_mismatch() -> None:
    # Caller passes ocean_dims=4 but ocean has 5 → empty
    assert build_behavior_injection(
        [0.5, 0.5, 0.5, 0.5, 0.5],
        ocean_dims=4,
    ) == ""


def test_build_injection_header_byte_exact() -> None:
    out = build_behavior_injection([0.5, 0.5, 0.5, 0.5, 0.5])
    # Header must be byte-identical for Aegis parity
    assert out.startswith("[Persona Behavior Profile]\n")
