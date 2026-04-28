"""Tests for ``concinno.approval_mode``.

Coverage:
    * Three modes route correctly (manual always asks, off never asks,
      smart consults SPS × FTRL).
    * SPS scores match the documented blast-radius bands.
    * FTRL Beta-Bernoulli posterior moves with ``record_outcome``.
    * Bucket key prioritises ``tunable`` over radius (so per-feature
      learning is preferred).
    * Persistence round-trip via :func:`save_config` + :func:`load_config`.
    * Env override + threshold tuning honoured.
    * Bounded clamps keep alpha/beta strictly positive.
    * Off mode reason explicitly mentions destruction_guard /
      release_authorization separation.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from concinno import approval_mode as am

# ── Fixture: redirect HOME to a sandbox dir ────────────────────────


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv(am._MODE_ENV, raising=False)
    monkeypatch.delenv(am._THRESHOLD_ENV, raising=False)
    yield tmp_path


# ── Mode parser ─────────────────────────────────────────────────────


def test_mode_from_raw_known_values() -> None:
    assert am.ApprovalMode.from_raw("manual") is am.ApprovalMode.MANUAL
    assert am.ApprovalMode.from_raw("smart") is am.ApprovalMode.SMART
    assert am.ApprovalMode.from_raw("off") is am.ApprovalMode.OFF


def test_mode_from_raw_unknown_falls_back_to_smart() -> None:
    assert am.ApprovalMode.from_raw(None) is am.ApprovalMode.SMART
    assert am.ApprovalMode.from_raw("yo") is am.ApprovalMode.SMART
    assert am.ApprovalMode.from_raw(123) is am.ApprovalMode.SMART


# ── SPS by blast radius ────────────────────────────────────────────


def test_sps_low_smaller_than_medium_smaller_than_high() -> None:
    assert (
        am.compute_sps_score("low")
        < am.compute_sps_score("medium")
        < am.compute_sps_score("high")
    )


def test_sps_unknown_radius_defaults_to_medium() -> None:
    assert am.compute_sps_score("nonsense") == am.compute_sps_score("medium")


# ── Mode routing ──────────────────────────────────────────────────


def test_manual_always_asks(fake_home: Path) -> None:
    cfg = am.ApprovalConfig(mode=am.ApprovalMode.MANUAL)
    for radius in ("low", "medium", "high"):
        d = am.decide(radius, config=cfg)
        assert d.should_ask is True
        assert d.mode is am.ApprovalMode.MANUAL


def test_off_never_asks(fake_home: Path) -> None:
    cfg = am.ApprovalConfig(mode=am.ApprovalMode.OFF)
    for radius in ("low", "medium", "high"):
        d = am.decide(radius, config=cfg)
        assert d.should_ask is False
        assert d.mode is am.ApprovalMode.OFF


def test_off_mode_reason_mentions_destruction_and_release_separation(
    fake_home: Path,
) -> None:
    """Operator MUST be able to read the reason and confirm that
    HP6 ``off`` mode does not weaken destruction_guard or
    release_authorization (per scope docstring)."""
    cfg = am.ApprovalConfig(mode=am.ApprovalMode.OFF)
    d = am.decide("medium", config=cfg)
    assert "destruction_guard" in d.reason
    assert "release_authorization" in d.reason


def test_smart_high_radius_asks_with_fresh_prior(fake_home: Path) -> None:
    """Beta(1,1) prior + SPS=0.9 ⇒ posterior = 0.10 * 0.5 = 0.05 < 0.5
    ⇒ should_ask=True."""
    cfg = am.ApprovalConfig(mode=am.ApprovalMode.SMART)
    d = am.decide("high", config=cfg)
    assert d.should_ask is True
    assert d.sps == am.compute_sps_score("high")


def test_smart_low_radius_proceeds_after_a_few_proceeds(
    fake_home: Path,
) -> None:
    """After enough proceed clicks, SPS=0.10 + high FTRL ⇒ posterior
    crosses threshold → autonomous."""
    cfg = am.ApprovalConfig(mode=am.ApprovalMode.SMART)
    # Stack the FTRL bucket so proceed_prob is very high.
    bucket = am._bucket_key("low", None)
    cfg = am.ApprovalConfig(
        mode=am.ApprovalMode.SMART,
        ftrl={bucket: am.ApprovalState(alpha=20.0, beta=1.0)},
    )
    d = am.decide("low", config=cfg)
    # posterior = (1-0.10) * (20/21) ≈ 0.857 > 0.5 ⇒ should_ask=False
    assert d.should_ask is False
    assert d.ftrl_proceed_prob > 0.9


def test_smart_routing_respects_per_tunable_bucket(
    fake_home: Path,
) -> None:
    """A high alpha on tunable:foo should NOT bleed over to the radius
    bucket — and vice versa.

    Use ``low`` radius (SPS=0.10) so the posterior cleanly clears the
    0.5 threshold: posterior = 0.90 * (50/51) ≈ 0.882 ≥ 0.5.
    """
    cfg = am.ApprovalConfig(
        mode=am.ApprovalMode.SMART,
        ftrl={"tunable:foo": am.ApprovalState(alpha=50.0, beta=1.0)},
    )
    d_foo = am.decide("low", tunable="foo", config=cfg)
    d_bar = am.decide("low", tunable="bar", config=cfg)
    # foo has rich history → autonomous; bar is fresh → asks.
    assert d_foo.should_ask is False
    assert d_bar.should_ask is True
    # And foo bucket didn't leak into bar (different posteriors).
    assert d_foo.ftrl_proceed_prob > d_bar.ftrl_proceed_prob


# ── Persistence ────────────────────────────────────────────────────


def test_save_then_load_round_trip(fake_home: Path) -> None:
    cfg = am.ApprovalConfig(
        mode=am.ApprovalMode.OFF,
        ftrl={"blast:high": am.ApprovalState(alpha=3.0, beta=2.0)},
    )
    am.save_config(cfg)
    loaded = am.load_config()
    assert loaded.mode is am.ApprovalMode.OFF
    assert "blast:high" in loaded.ftrl
    state = loaded.ftrl["blast:high"]
    assert pytest.approx(state.alpha) == 3.0
    assert pytest.approx(state.beta) == 2.0


def test_load_returns_default_when_no_file(fake_home: Path) -> None:
    cfg = am.load_config()
    assert cfg.mode is am.ApprovalMode.SMART
    assert cfg.ftrl == {}


def test_load_handles_malformed_json(fake_home: Path) -> None:
    p = fake_home / ".concinno" / "approval_mode.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not valid json", encoding="utf-8")
    cfg = am.load_config()
    assert cfg.mode is am.ApprovalMode.SMART
    assert any("malformed" in w.lower() for w in cfg.warnings)


def test_env_mode_overrides_file(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    am.save_config(am.ApprovalConfig(mode=am.ApprovalMode.MANUAL))
    monkeypatch.setenv(am._MODE_ENV, "off")
    cfg = am.load_config()
    assert cfg.mode is am.ApprovalMode.OFF


def test_explicit_mode_beats_env_and_file(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    am.save_config(am.ApprovalConfig(mode=am.ApprovalMode.MANUAL))
    monkeypatch.setenv(am._MODE_ENV, "off")
    cfg = am.load_config(explicit_mode=am.ApprovalMode.SMART)
    assert cfg.mode is am.ApprovalMode.SMART


# ── FTRL update ────────────────────────────────────────────────────


def test_record_outcome_proceed_increments_alpha(fake_home: Path) -> None:
    cfg = am.ApprovalConfig(mode=am.ApprovalMode.SMART)
    new_cfg = am.record_outcome("low", proceed=True, config=cfg)
    bucket = am._bucket_key("low", None)
    assert new_cfg.ftrl[bucket].alpha == pytest.approx(2.0)
    assert new_cfg.ftrl[bucket].beta == pytest.approx(1.0)


def test_record_outcome_ask_increments_beta(fake_home: Path) -> None:
    cfg = am.ApprovalConfig(mode=am.ApprovalMode.SMART)
    new_cfg = am.record_outcome("medium", proceed=False, config=cfg)
    bucket = am._bucket_key("medium", None)
    assert new_cfg.ftrl[bucket].alpha == pytest.approx(1.0)
    assert new_cfg.ftrl[bucket].beta == pytest.approx(2.0)


def test_record_outcome_persists_to_disk(fake_home: Path) -> None:
    cfg = am.ApprovalConfig(mode=am.ApprovalMode.SMART)
    am.record_outcome("high", proceed=True, config=cfg)
    on_disk = am.load_config()
    bucket = am._bucket_key("high", None)
    assert on_disk.ftrl[bucket].alpha == pytest.approx(2.0)


def test_state_proceed_probability_jeffreys_prior() -> None:
    s = am.ApprovalState(alpha=1.0, beta=1.0)
    assert s.proceed_probability() == pytest.approx(0.5)


def test_state_proceed_probability_safe_when_zero() -> None:
    s = am.ApprovalState(alpha=0.0, beta=0.0)
    # Defensive fallback — should not raise / divide-by-zero.
    assert s.proceed_probability() == pytest.approx(0.5)


def test_load_clamps_negative_state_to_positive(fake_home: Path) -> None:
    p = fake_home / ".concinno" / "approval_mode.json"
    p.parent.mkdir(parents=True)
    p.write_text(
        '{"mode": "smart", "ftrl": {"blast:low": '
        '{"alpha": -5, "beta": 0}}}',
        encoding="utf-8",
    )
    cfg = am.load_config()
    state = cfg.ftrl["blast:low"]
    assert state.alpha > 0
    assert state.beta > 0


# ── describe_current_config ────────────────────────────────────────


def test_describe_includes_mode_and_threshold(fake_home: Path) -> None:
    out = am.describe_current_config()
    assert "mode=" in out
    assert "threshold=" in out


def test_describe_lists_ftrl_buckets(fake_home: Path) -> None:
    am.save_config(
        am.ApprovalConfig(
            mode=am.ApprovalMode.SMART,
            ftrl={"blast:medium": am.ApprovalState(alpha=4.0, beta=2.0)},
        )
    )
    out = am.describe_current_config()
    assert "blast:medium" in out
    assert "alpha=" in out


# ── Threshold knob ─────────────────────────────────────────────────


def test_threshold_env_lowers_ask_bar(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting threshold near 0 ⇒ smart mode rarely asks."""
    monkeypatch.setenv(am._THRESHOLD_ENV, "0.001")
    cfg = am.ApprovalConfig(mode=am.ApprovalMode.SMART)
    d = am.decide("medium", config=cfg)
    assert d.should_ask is False


def test_threshold_env_at_1_always_asks(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting threshold to 1.0 ⇒ smart mode always asks (posterior
    can never reach the bar)."""
    monkeypatch.setenv(am._THRESHOLD_ENV, "1.0")
    cfg = am.ApprovalConfig(
        mode=am.ApprovalMode.SMART,
        ftrl={"blast:low": am.ApprovalState(alpha=100.0, beta=1.0)},
    )
    d = am.decide("low", config=cfg)
    assert d.should_ask is True


def test_threshold_bad_env_falls_back_to_default(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(am._THRESHOLD_ENV, "not-a-number")
    # Simply verifying decide() does not crash + uses the default.
    cfg = am.ApprovalConfig(mode=am.ApprovalMode.SMART)
    d = am.decide("medium", config=cfg)
    assert d.threshold == pytest.approx(am._DEFAULT_THRESHOLD)
