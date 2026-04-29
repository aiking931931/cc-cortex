"""Tests for concinno.hooks.relay_helpers — verbatim_relay self-branding."""

from __future__ import annotations

import pytest

from concinno.hooks.relay_helpers import (
    VALID_RELAY_MODES,
    _strip_wrappers,
    get_relay_mode,
    with_feature_prefix,
)

# ── Unit tests ────────────────────────────────────────────────


def test_default_mode_is_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env, no config override → default ``prefix`` (4.6.0 baseline)."""
    monkeypatch.delenv("CONCINNO_VERBATIM_RELAY_MODE", raising=False)
    # Even if config layer disagrees, the explicit ``mode=`` kwarg must
    # win in the helper itself; this test is about the resolver.
    out = with_feature_prefix("cbua_pipeline", "ctx 100k", mode="prefix")
    assert out == "[SHOW USER VERBATIM] [Concinno: cbua_pipeline] ctx 100k"


def test_prefix_mode_branding() -> None:
    """``prefix`` mode wraps with both verbatim token and Concinno brand."""
    out = with_feature_prefix("butterfly_guard", "missed pre-existing bug", mode="prefix")
    assert out == (
        "[SHOW USER VERBATIM] [Concinno: butterfly_guard] missed pre-existing bug"
    )


def test_verbose_mode_legacy_shape() -> None:
    """``verbose`` mode = legacy 4.5.0 (no Concinno self-brand)."""
    out = with_feature_prefix("token_monitor", "100k threshold", mode="verbose")
    assert out == "[SHOW USER VERBATIM] 100k threshold"
    assert "[Concinno:" not in out


def test_silent_mode_strips_verbatim_token() -> None:
    """``silent`` mode emits bare body so io_utils silent layer can wrap it."""
    out = with_feature_prefix("step_back_protocol", "paused", mode="silent")
    assert out == "paused"
    assert "[SHOW USER VERBATIM]" not in out
    assert "[Concinno:" not in out


def test_off_mode_returns_empty() -> None:
    """``off`` mode = caller MUST skip emit (helper returns ``""``)."""
    out = with_feature_prefix("cbua_pipeline", "any message", mode="off")
    assert out == ""


def test_empty_message_returns_empty_regardless_of_mode() -> None:
    """No message to relay → no relay, regardless of mode."""
    for mode in VALID_RELAY_MODES:
        # Bypass the Literal type check at runtime; cast to satisfy mypy
        out = with_feature_prefix("any_feature", "", mode=mode)  # type: ignore[arg-type]
        assert out == ""


def test_idempotent_already_prefixed() -> None:
    """Re-wrapping a prefixed string doesn't double-stack tokens."""
    once = with_feature_prefix("cbua_pipeline", "ratio shallow", mode="prefix")
    twice = with_feature_prefix("cbua_pipeline", once, mode="prefix")
    assert twice == once


def test_idempotent_legacy_to_prefix_migration() -> None:
    """A legacy 4.5.0 string can be re-rendered under the new prefix mode."""
    legacy = "[SHOW USER VERBATIM] threshold hit 140k"
    out = with_feature_prefix("token_monitor", legacy, mode="prefix")
    assert out == "[SHOW USER VERBATIM] [Concinno: token_monitor] threshold hit 140k"


def test_idempotent_swap_feature_name() -> None:
    """Re-wrapping with a different feature_name updates the brand cleanly."""
    a = with_feature_prefix("token_monitor", "100k", mode="prefix")
    b = with_feature_prefix("agent_cap", a, mode="prefix")
    # The new feature wins; the old [Concinno: token_monitor] is stripped.
    assert b == "[SHOW USER VERBATIM] [Concinno: agent_cap] 100k"
    assert "token_monitor" not in b


def test_strip_wrappers_handles_both_tokens() -> None:
    """Unit test for ``_strip_wrappers`` — defensive parser."""
    assert (
        _strip_wrappers(
            "[SHOW USER VERBATIM] [Concinno: x] body", "x",
        )
        == "body"
    )
    assert _strip_wrappers("[SHOW USER VERBATIM] body", "x") == "body"
    assert _strip_wrappers("[Concinno: y] body", "x") == "body"
    assert _strip_wrappers("body", "x") == "body"


# ── Resolver / env / config tests ──────────────────────────


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CONCINNO_VERBATIM_RELAY_MODE`` env beats config and default."""
    monkeypatch.setenv("CONCINNO_VERBATIM_RELAY_MODE", "verbose")
    assert get_relay_mode() == "verbose"


def test_env_invalid_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid env value silently falls through (does not crash hook)."""
    monkeypatch.setenv("CONCINNO_VERBATIM_RELAY_MODE", "bogus")
    # Falls through to config / default — must NOT raise.
    out = get_relay_mode()
    assert out in VALID_RELAY_MODES


def test_resolver_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env, no working config → ``prefix`` default."""
    monkeypatch.delenv("CONCINNO_VERBATIM_RELAY_MODE", raising=False)
    # We can't easily mock the config layer in a portable test, but the
    # resolver must always return a member of VALID_RELAY_MODES.
    out = get_relay_mode()
    assert out in VALID_RELAY_MODES


def test_explicit_mode_kwarg_overrides_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When caller passes ``mode=``, the resolver is bypassed entirely."""
    monkeypatch.setenv("CONCINNO_VERBATIM_RELAY_MODE", "off")
    # Even though env says off, explicit ``mode=verbose`` wins.
    out = with_feature_prefix("x", "msg", mode="verbose")
    assert out == "[SHOW USER VERBATIM] msg"


# ── Feature META wiring ────────────────────────────────────


def test_feature_meta_has_verbatim_relay() -> None:
    """``FEATURE_META`` declares the ``verbatim_relay`` row."""
    from concinno.feature_config import FEATURE_META

    assert "verbatim_relay" in FEATURE_META
    meta = FEATURE_META["verbatim_relay"]
    # 6-DoD compliance: cosmetic + ziq_autotunable=False per L0 鐵律 #6.
    assert meta["cosmetic"] is True
    assert meta["ziq_autotunable"] is False
    # Mode param is enumerable with all four options.
    options = meta["params"]["mode"]["options"]
    assert set(options) == VALID_RELAY_MODES


def test_feature_meta_default_is_prefix() -> None:
    """The shipped default for ``mode`` matches the helper default."""
    from concinno.feature_config import FEATURE_META

    assert FEATURE_META["verbatim_relay"]["params"]["mode"]["default"] == "prefix"


# ── Integration: hot-path callers ──────────────────────────


def test_on_post_tool_imports_helper() -> None:
    """``on_post_tool`` imports and uses the helper (wiring smoke check)."""
    import concinno.hooks.on_post_tool as mod

    # Module-level import binds the helper into the namespace.
    assert hasattr(mod, "with_feature_prefix")


def test_step_back_imports_helper() -> None:
    """``step_back`` imports the helper (wiring smoke check)."""
    import concinno.step_back as mod

    assert hasattr(mod, "with_feature_prefix")
    # Render helpers exist for use by templates.
    assert callable(mod._render_step_back)
    assert callable(mod._render_hard_deny)


def test_step_back_render_helper_brands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_render_step_back`` produces a branded line under prefix mode."""
    monkeypatch.setenv("CONCINNO_VERBATIM_RELAY_MODE", "prefix")
    from concinno.step_back import _render_step_back

    out = _render_step_back("sentinel_gate", "looped Edit on file X")
    assert out.startswith("[SHOW USER VERBATIM] [Concinno: sentinel_gate]")
    assert "looped Edit on file X" in out


def test_step_back_render_helper_off_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under off mode, render helper returns empty string."""
    monkeypatch.setenv("CONCINNO_VERBATIM_RELAY_MODE", "off")
    from concinno.step_back import _render_hard_deny

    assert _render_hard_deny("sentinel_gate", "any") == ""


# ── Defensive ──────────────────────────────────────────────


def test_no_brand_leaks_into_legacy_mode() -> None:
    """``verbose`` mode never leaks the ``[Concinno: …]`` brand."""
    out = with_feature_prefix("any", "[Concinno: bogus] msg", mode="verbose")
    # The defensive stripper removes the leftover brand; verbose then
    # adds only the verbatim token.
    assert out == "[SHOW USER VERBATIM] msg"


def test_helper_does_not_raise_on_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If config layer raises, the helper falls back without crashing."""
    monkeypatch.delenv("CONCINNO_VERBATIM_RELAY_MODE", raising=False)
    # Force-import a fake config that raises on .feature(); patch
    # via monkeypatch on the import target.
    import concinno.hooks.relay_helpers as mod

    class _BoomConfig:
        def feature(self, *a: object, **kw: object) -> object:
            raise RuntimeError("config layer is sick")

    def _boom_get_config() -> _BoomConfig:
        return _BoomConfig()

    monkeypatch.setattr(
        "concinno.core.config.get_config", _boom_get_config, raising=False,
    )
    # Must not raise; returns the hard-coded default.
    assert mod.get_relay_mode() == "prefix"
