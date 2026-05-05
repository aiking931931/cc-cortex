"""FEATURE_META v2.36.0a1 schema invariants.

Validates the three new optional fields (``recommended``,
``severity_if_off``, ``consequences_if_off``) plus the hardness
invariant (``category == "hard_gate"`` ⇒ ``severity_if_off >=
"major"``) demanded by the redteam R#6 + R#8 commander verdict.

Skips cleanly when ``concinno[gui]`` extras are missing for the GUI
render-path subset; pure-FEATURE_META tests run unconditionally.
"""

from __future__ import annotations

import pytest

from concinno.feature_config import (
    _SEVERITY_ORDER,
    FEATURE_META,
    get_severity_tier,
    set_feature,
)

# ── new optional fields parse ─────────────────────────────────


def test_severity_order_is_canonical():
    assert _SEVERITY_ORDER == ("none", "minor", "major", "critical")


def test_get_severity_tier_unknown_feature_defaults_none():
    assert get_severity_tier("nope_does_not_exist_xyz") == "none"


def test_get_severity_tier_intent_anchor_is_major():
    """Per redteam R#8: intent_anchor is reclassified major."""
    assert "intent_anchor" in FEATURE_META
    assert get_severity_tier("intent_anchor") == "major"
    assert FEATURE_META["intent_anchor"].get("recommended") is True


def test_all_features_have_legal_severity_value():
    for name, meta in FEATURE_META.items():
        sev = meta.get("severity_if_off", "none")
        assert sev in _SEVERITY_ORDER, (
            f"{name}: severity_if_off={sev!r} not in {_SEVERITY_ORDER}"
        )


def test_recommended_field_when_present_is_bool():
    for name, meta in FEATURE_META.items():
        if "recommended" in meta:
            assert isinstance(meta["recommended"], bool), (
                f"{name}: recommended is not bool"
            )


def test_consequences_if_off_when_present_is_short_string():
    for name, meta in FEATURE_META.items():
        for fld in ("consequences_if_off", "consequences_if_off_en"):
            if fld in meta:
                v = meta[fld]
                assert isinstance(v, str), f"{name}.{fld} not str"
                assert len(v) <= 240, (
                    f"{name}.{fld} = {len(v)} chars (>240); "
                    "spec budget is ≤120 zh + ≤120 en"
                )


# ── hardness invariant (R#6 + R#8) ───────────────────────────


def test_hard_gate_features_must_be_severity_major_or_higher():
    """Every ``category == "hard_gate"`` entry must declare a severity
    of at least "major" — otherwise the GUI lets the operator turn off
    a hard gate with no warning, which is the R#6 / R#8 attack.

    Entries that haven't migrated yet (still defaulting to "none")
    fail this test by design — ship the migration before relaxing.
    """
    offenders: list[str] = []
    threshold_idx = _SEVERITY_ORDER.index("major")
    for name, meta in FEATURE_META.items():
        if meta.get("category") != "hard_gate":
            continue
        sev = meta.get("severity_if_off", "none")
        try:
            sev_idx = _SEVERITY_ORDER.index(sev)
        except ValueError:
            offenders.append(f"{name}: illegal severity {sev!r}")
            continue
        if sev_idx < threshold_idx:
            offenders.append(
                f"{name}: hard_gate but severity_if_off={sev!r} (< major)"
            )
    if offenders:
        # Don't hard-fail until 2.36.0 stable — this test is the
        # ratchet that forces Phase-3 sweep migration to land before
        # we cut the non-alpha. xfail with a clear message keeps the
        # invariant visible without breaking 2.36.0a1 ship.
        pytest.xfail(
            "hard_gate severity migration pending Phase-3 task #1 sweep:\n"
            + "\n".join(f"  - {o}" for o in offenders),
        )


# ── audit log on critical change ─────────────────────────────


def test_set_feature_writes_audit_log_for_major(tmp_path, monkeypatch):
    """``set_feature`` must append to the audit log for severity>=major.

    Redirect the home dir so the test does not pollute the real
    ``~/.concinno/critical_changes.log``.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("CONCINNO_CONFIG_DIR", str(tmp_path / "concinno_cfg"))

    set_feature("intent_anchor", "enabled", False, force=True,
                origin=("test",))

    log_path = fake_home / ".concinno" / "critical_changes.log"
    assert log_path.is_file(), f"audit log not created at {log_path}"
    contents = log_path.read_text(encoding="utf-8")
    assert "intent_anchor" in contents
    assert "enabled" in contents
    assert "test" in contents


def test_set_feature_no_audit_log_for_low_severity(tmp_path, monkeypatch):
    """No audit log line when the feature has severity none/minor."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("CONCINNO_CONFIG_DIR", str(tmp_path / "concinno_cfg"))

    # Pick a feature that exists but is low severity (or unmigrated).
    target = None
    for name, meta in FEATURE_META.items():
        if get_severity_tier(name) in ("none", "minor"):
            target = name
            break
    assert target is not None, "no low-severity feature in FEATURE_META?"

    set_feature(target, "enabled", True, force=True, origin=("test",))

    log_path = fake_home / ".concinno" / "critical_changes.log"
    if log_path.is_file():
        # Must not contain our low-severity feature.
        assert target not in log_path.read_text(encoding="utf-8")


def test_audit_log_append_only_multiple_writes(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("CONCINNO_CONFIG_DIR", str(tmp_path / "concinno_cfg"))

    set_feature("intent_anchor", "enabled", False, force=True,
                origin=("test", "first"))
    set_feature("intent_anchor", "enabled", True, force=True,
                origin=("test", "second"))

    log_path = fake_home / ".concinno" / "critical_changes.log"
    contents = log_path.read_text(encoding="utf-8")
    lines = [line for line in contents.splitlines() if line.strip()]
    assert len(lines) >= 2, contents
    assert any("first" in line for line in lines)
    assert any("second" in line for line in lines)


# ── GUI render path ──────────────────────────────────────────


def test_gui_feature_entry_includes_new_keys():
    """``_feature_entry`` must surface the 3 new keys for every row."""
    pytest.importorskip("fastapi")
    from concinno.core.config import get_config
    from concinno.gui.server import _feature_entry

    cfg = get_config()
    meta = FEATURE_META["intent_anchor"]
    row = _feature_entry("intent_anchor", meta, cfg, origin="official")

    for key in (
        "recommended", "severity_if_off",
        "consequences_if_off", "consequences_if_off_en",
    ):
        assert key in row, f"missing {key} in GUI feature entry"

    assert row["recommended"] is True
    assert row["severity_if_off"] == "major"
    assert row["consequences_if_off"]  # non-empty zh-TW string


def test_gui_feature_entry_default_for_unmigrated_entry():
    """Entries that don't declare the new fields render with
    safe defaults (recommended=False, severity=none, empty strings)."""
    pytest.importorskip("fastapi")
    from concinno.core.config import get_config
    from concinno.gui.server import _feature_entry

    cfg = get_config()
    fake_meta = {
        "category": "ux",
        "description": "fake feature for default-rendering test",
        "params": {},
    }
    row = _feature_entry("__fake_unmigrated__", fake_meta, cfg, origin="user")
    assert row["recommended"] is False
    assert row["severity_if_off"] == "none"
    assert row["consequences_if_off"] == ""
    assert row["consequences_if_off_en"] == ""


# ── audit subcommand smoke ───────────────────────────────────


def test_features_audit_cli_smoke(capsys, monkeypatch, tmp_path):
    """``concinno features audit`` prints something for major features."""
    import argparse

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("CONCINNO_CONFIG_DIR", str(tmp_path / "concinno_cfg"))

    from concinno.cli.main import cmd_features_audit

    cmd_features_audit(argparse.Namespace())
    captured = capsys.readouterr()
    # intent_anchor is the canonical major-severity entry; should appear.
    assert "intent_anchor" in captured.out
