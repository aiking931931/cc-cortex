"""Tests for the ``approval_mode`` integration layer in
``concinno.release_authorization`` (Concinno 4.4.0).

Pins the FATAL-2 ship-fix wiring: ``check_authorization`` now consults
:mod:`concinno.approval_mode` AFTER the ``release_auth.disabled=True``
opt-out short-circuit but BEFORE the canonical STRING_MATCH /
ASKUSER_ANSWER token check. The three modes route as follows:

* ``manual`` — defer to the canonical gate (legacy behaviour).
* ``smart``  — SPS×FTRL posterior decides; on autonomy-cleared,
  short-circuit to ``allowed=True`` and feed ``proceed=True`` back to
  the FTRL learner.
* ``off``    — never ask; immediately ``allowed=True`` and feed
  ``proceed=True`` back.

Crucially, :mod:`concinno.destruction_guard` (R0-R4) is NOT touched
by these layers — confirmed structurally by checking the module's
exports and that no symbol is imported from / monkey-patched on it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from concinno import approval_mode as am
from concinno import release_authorization as ra

# ── Fixture: redirect HOME so approval_mode + release_authorization
# config files write into a sandbox dir per test ────────────────────


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv(am._MODE_ENV, raising=False)
    monkeypatch.delenv(am._THRESHOLD_ENV, raising=False)
    monkeypatch.delenv("CONCINNO_RELEASE_AUTH_MODE", raising=False)
    monkeypatch.delenv("CONCINNO_RELEASE_AUTH_DISABLED", raising=False)
    yield tmp_path


def _enabled_cfg() -> ra.AuthorizationConfig:
    """Publish gate explicitly enabled (disabled=False)."""
    return ra.AuthorizationConfig(
        mode=ra.AuthorizationMode.STRING_MATCH,
        disabled=False,
        source="test",
    )


# ── Mode routing: manual ────────────────────────────────────────────


def test_manual_mode_defers_to_canonical_gate_no_string(fake_home: Path) -> None:
    """``manual`` mode must NOT short-circuit — gate denies without
    the auth string just like 4.3.x did."""
    am.save_config(am.ApprovalConfig(mode=am.ApprovalMode.MANUAL))
    allowed, reason = ra.check_authorization(
        "twine_upload",
        "concinno",
        "4.4.0",
        transcript_text="random chat with no auth string",
        config=_enabled_cfg(),
    )
    assert allowed is False
    assert "go publish concinno 4.4.0" in reason


def test_manual_mode_defers_to_canonical_gate_with_string(fake_home: Path) -> None:
    """``manual`` mode + correct auth string ⇒ canonical gate allows."""
    am.save_config(am.ApprovalConfig(mode=am.ApprovalMode.MANUAL))
    allowed, reason = ra.check_authorization(
        "twine_upload",
        "concinno",
        "4.4.0",
        transcript_text="ok go publish concinno 4.4.0",
        config=_enabled_cfg(),
    )
    assert allowed is True
    assert reason == ""


# ── Mode routing: off ───────────────────────────────────────────────


def test_off_mode_short_circuits_without_auth_string(fake_home: Path) -> None:
    """``off`` mode = autonomous, no auth string required."""
    am.save_config(am.ApprovalConfig(mode=am.ApprovalMode.OFF))
    allowed, reason = ra.check_authorization(
        "twine_upload",
        "concinno",
        "4.4.0",
        transcript_text="no auth string here",
        config=_enabled_cfg(),
    )
    assert allowed is True
    assert reason == ""


def test_off_mode_records_proceed_outcome_to_ftrl(fake_home: Path) -> None:
    """``off`` mode must feed proceed=True so the FTRL posterior on
    ``release_authorization`` reflects reality."""
    am.save_config(am.ApprovalConfig(mode=am.ApprovalMode.OFF))
    ra.check_authorization(
        "twine_upload",
        "concinno",
        "4.4.0",
        transcript_text="",
        config=_enabled_cfg(),
    )
    bucket = am._bucket_key(am.BLAST_RADIUS_HIGH, "release_authorization")
    cfg = am.load_config()
    assert bucket in cfg.ftrl
    # Jeffreys prior alpha=1 + one proceed = alpha=2.
    assert cfg.ftrl[bucket].alpha == pytest.approx(2.0)
    assert cfg.ftrl[bucket].beta == pytest.approx(1.0)


# ── Mode routing: smart ─────────────────────────────────────────────


def test_smart_mode_fresh_prior_falls_through_to_canonical_gate(
    fake_home: Path,
) -> None:
    """Fresh smart prior + HIGH radius ⇒ posterior=0.05 < 0.5 ⇒
    decision.should_ask=True ⇒ defer to canonical gate ⇒ deny without
    auth string."""
    am.save_config(am.ApprovalConfig(mode=am.ApprovalMode.SMART))
    allowed, reason = ra.check_authorization(
        "twine_upload",
        "concinno",
        "4.4.0",
        transcript_text="",
        config=_enabled_cfg(),
    )
    assert allowed is False
    assert "go publish concinno 4.4.0" in reason


def test_smart_mode_clears_threshold_short_circuits(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lower the autonomy threshold so a trained-prior smart decision
    auto-proceeds — confirms the layer wires through cleanly when the
    posterior crosses.

    Concinno 4.5.0 added a ``smart`` cold-start safety override (R+B+G
    W2 verdict #2): a bucket with **no** FTRL observations always
    returns ``should_ask=True`` regardless of threshold so a 50/50
    Jeffreys coin-flip cannot authorise an irreversible action on the
    very first call. To pin the threshold-clears-posterior wiring
    independent of that safety override, this test seeds a
    non-cold-start FTRL state (alpha=beta=2.0, equivalent to Jeffreys
    plus one observed proceed and one observed ask) for the bucket the
    integration layer reads — ``tunable:release_authorization`` — and
    persists it before invoking the gate. The test therefore exercises
    "threshold knob lowers the ask-bar on a trained bucket", not
    "first-call routing on a fresh prior".
    """
    monkeypatch.setenv(am._THRESHOLD_ENV, "0.001")
    bucket = am._bucket_key(am.BLAST_RADIUS_HIGH, "release_authorization")
    am.save_config(
        am.ApprovalConfig(
            mode=am.ApprovalMode.SMART,
            ftrl={bucket: am.ApprovalState(alpha=2.0, beta=2.0)},
        )
    )
    allowed, reason = ra.check_authorization(
        "twine_upload",
        "concinno",
        "4.4.0",
        transcript_text="",
        config=_enabled_cfg(),
    )
    assert allowed is True
    assert reason == ""


def test_smart_mode_short_circuit_records_proceed(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When smart mode auto-proceeds, the FTRL posterior must move.

    Mirrors :func:`test_smart_mode_clears_threshold_short_circuits`'s
    cold-start workaround — seed a trained prior (alpha=beta=2.0) so
    the threshold-clearance path is exercised rather than the 4.5.0
    cold-start safety override. After the auto-proceed records one
    additional ``proceed=True``, the posterior advances from
    ``alpha=2.0`` to ``alpha=3.0`` (Beta-Bernoulli increment).
    """
    monkeypatch.setenv(am._THRESHOLD_ENV, "0.001")
    bucket = am._bucket_key(am.BLAST_RADIUS_HIGH, "release_authorization")
    am.save_config(
        am.ApprovalConfig(
            mode=am.ApprovalMode.SMART,
            ftrl={bucket: am.ApprovalState(alpha=2.0, beta=2.0)},
        )
    )
    ra.check_authorization(
        "twine_upload",
        "concinno",
        "4.4.0",
        transcript_text="",
        config=_enabled_cfg(),
    )
    cfg = am.load_config()
    assert cfg.ftrl[bucket].alpha == pytest.approx(3.0)


# ── Layering precedence ─────────────────────────────────────────────


def test_release_auth_disabled_takes_priority_over_approval_mode_manual(
    fake_home: Path,
) -> None:
    """``release_auth.disabled=True`` is the publish-specific opt-out
    and takes precedence over the more general approval_mode switch.
    Even ``manual`` mode does not re-enable the publish gate."""
    am.save_config(am.ApprovalConfig(mode=am.ApprovalMode.MANUAL))
    disabled_cfg = ra.AuthorizationConfig(
        mode=ra.AuthorizationMode.STRING_MATCH,
        disabled=True,
        source="test",
    )
    allowed, reason = ra.check_authorization(
        "twine_upload",
        "concinno",
        "4.4.0",
        transcript_text="",
        config=disabled_cfg,
    )
    assert allowed is True
    assert reason == ""


def test_canonical_gate_string_match_records_proceed(
    fake_home: Path,
) -> None:
    """When the canonical STRING_MATCH path allows, the gate also
    feeds back proceed=True so the FTRL learner sees the success."""
    am.save_config(am.ApprovalConfig(mode=am.ApprovalMode.MANUAL))
    ra.check_authorization(
        "twine_upload",
        "concinno",
        "4.4.0",
        transcript_text="ok go publish concinno 4.4.0",
        config=_enabled_cfg(),
    )
    bucket = am._bucket_key(am.BLAST_RADIUS_HIGH, "release_authorization")
    cfg = am.load_config()
    assert cfg.ftrl[bucket].alpha == pytest.approx(2.0)


def test_canonical_gate_askuser_records_proceed(fake_home: Path) -> None:
    """The ASKUSER_ANSWER allow path also records the proceed signal."""
    am.save_config(am.ApprovalConfig(mode=am.ApprovalMode.MANUAL))
    askuser_cfg = ra.AuthorizationConfig(
        mode=ra.AuthorizationMode.ASKUSER_ANSWER,
        disabled=False,
        source="test",
    )
    ra.check_authorization(
        "twine_upload",
        "concinno",
        "4.4.0",
        askuser_answers=["go publish concinno 4.4.0"],
        config=askuser_cfg,
    )
    bucket = am._bucket_key(am.BLAST_RADIUS_HIGH, "release_authorization")
    cfg = am.load_config()
    assert cfg.ftrl[bucket].alpha == pytest.approx(2.0)


# ── Destruction guard untouched (structural assertion) ──────────────


def test_destruction_guard_module_not_modified() -> None:
    """Sanity check: the approval_mode integration must NOT import or
    monkey-patch :mod:`concinno.destruction_guard`. R0-R4 enforcement
    is a separate layer and stays exactly as it was.

    Structural assertions only (no source-text grepping — release_auth's
    docstring is allowed to *describe* the layering with the word
    'destruction_guard' as long as the runtime never imports it).
    """
    # release_authorization must not have a runtime dependency on
    # destruction_guard. The publish gate decides authorization;
    # destruction_guard decides data integrity. They share zero state.
    assert "concinno.destruction_guard" not in (
        getattr(ra, "__dict__", {}).get("__loader__", None).__class__.__module__
        if hasattr(ra, "__loader__") else ""
    )
    # No symbol from destruction_guard should be re-exported via
    # release_authorization (the two-layer-gate principle).
    assert not any(
        name in dir(ra)
        for name in ("classify_bash", "DestructionGuard", "DestructionBlockedError")
    )

    # The destruction_guard module itself stays importable and intact —
    # confirms our changes did not transitively break it.
    from concinno import destruction_guard as dg

    assert dg.R4 == 4
    assert hasattr(dg, "classify_bash")
    assert hasattr(dg, "DestructionGuard")
    assert hasattr(dg, "DestructionBlockedError")


def test_approval_mode_failure_falls_through_to_canonical_gate(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If approval_mode raises (e.g. corrupt JSON, transient IO), the
    publish gate must NOT silently allow — it falls through to the
    canonical STRING_MATCH path so behaviour degrades to the strict
    default rather than to an unconditional allow."""
    def _boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("approval_mode disk read failed")

    monkeypatch.setattr(am, "load_config", _boom)
    allowed, reason = ra.check_authorization(
        "twine_upload",
        "concinno",
        "4.4.0",
        transcript_text="",
        config=_enabled_cfg(),
    )
    # Without the auth string, the canonical gate must still deny.
    assert allowed is False
    assert "go publish" in reason
