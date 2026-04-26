"""Tests for concinno.handoff_resume_hook — on-start §0 inject."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from concinno.handoff_resume_hook import (
    build_resume_inject,
    detect_resume_intent,
    is_section0_stale,
)

# ── Trigger detection ─────────────────────────────────────


def test_slash_trigger_detected():
    assert detect_resume_intent("/handoff resume gaia") == "gaia"


def test_zh_keyword_trigger_detected():
    assert detect_resume_intent("交接 gaia") == "gaia"


def test_zh_full_keyword_detected():
    assert detect_resume_intent("交棒 arb-bot") == "arb-bot"


def test_english_handoff_keyword():
    assert detect_resume_intent("handoff concinno") == "concinno"


def test_english_hand_off_with_space():
    assert detect_resume_intent("hand off concinno") == "concinno"


def test_japanese_keyword():
    assert detect_resume_intent("引き継ぎ gaia") == "gaia"


def test_korean_keyword():
    assert detect_resume_intent("인수인계 gaia") == "gaia"


def test_no_trigger_returns_none():
    assert detect_resume_intent("hello world") is None


def test_empty_returns_none():
    assert detect_resume_intent("") is None
    assert detect_resume_intent("   ") is None


def test_word_boundary_avoids_false_positive():
    # "handoffer" should not match "handoff"
    assert detect_resume_intent("handoffer is not a real word") is None


def test_path_with_slash():
    assert detect_resume_intent("交接 arb-bot/strategy3") == "arb-bot/strategy3"


# ── Staleness ─────────────────────────────────────────────


def test_missing_timestamp_is_stale():
    assert is_section0_stale(None) is True


def test_unparseable_timestamp_is_stale():
    assert is_section0_stale("not-a-date") is True


def test_recent_timestamp_not_stale():
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    assert is_section0_stale(fresh) is False


def test_old_timestamp_is_stale():
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    assert is_section0_stale(old) is True


def test_z_suffix_accepted():
    fresh = "2026-04-26T12:00:00Z"
    now = datetime(2026, 4, 26, 12, 1, 0, tzinfo=timezone.utc)
    assert is_section0_stale(fresh, now=now) is False


def test_custom_max_age():
    """Override ``max_age_seconds`` for fine-grained tests."""
    older = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    assert is_section0_stale(older, max_age_seconds=5) is True
    assert is_section0_stale(older, max_age_seconds=60) is False


# ── build_resume_inject ──────────────────────────────────


def test_missing_handoff_file_returns_advisory(tmp_path):
    out = build_resume_inject("gaia", tmp_path / "missing.md")
    assert "handoff index not found" in out
    assert "gaia" in out


def test_handoff_file_extracts_next_step(tmp_path):
    md = (tmp_path / "h.md")
    md.write_text(
        "## §0 Auto-revival\n\n"
        "| Item | Value |\n|---|---|\n"
        "| pod_id | abc |\n"
        "| populated_at | "
        + datetime.now(timezone.utc).isoformat()
        + " |\n\n"
        "## §1 Status header\n\nfocus: ship\n\n"
        "## §5 next_step\n\nrun pytest\n",
        encoding="utf-8",
    )
    out = build_resume_inject("gaia", md)
    assert "run pytest" in out
    assert "abc" in out


def test_handoff_with_stale_section0_repopulates(tmp_path, monkeypatch):
    md = tmp_path / "h.md"
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    md.write_text(
        "## §0\n\n"
        "| Item | Value |\n|---|---|\n"
        "| populated_at | " + old_ts + " |\n\n"
        "## §5 next_step\n\ndo a thing\n",
        encoding="utf-8",
    )

    called = {}

    def fake_populate(name, **kwargs):
        called["name"] = name
        return {
            "pod_id": "fresh_pod",
            "ssh_cmd": "fresh_ssh",
            "vault_aliases": "x",
            "env_vars": "y",
            "last_commit": "z",
            "last_session_id": "s",
            "last_token_usage": "?",
            "populated_at": datetime.now(timezone.utc).isoformat(),
        }

    monkeypatch.setattr(
        "concinno.handoff_section0.populate_section_0",
        fake_populate,
    )
    out = build_resume_inject("myproj", md)
    assert called == {"name": "myproj"}
    assert "fresh_pod" in out


def test_resume_inject_is_compact(tmp_path):
    """Sanity: not enormous. Spec target ≤50 tokens (~200 chars)."""
    md = tmp_path / "h.md"
    md.write_text(
        "## §0\n\n| Item | Value |\n|---|---|\n"
        "| pod_id | p |\n"
        "| populated_at | "
        + datetime.now(timezone.utc).isoformat()
        + " |\n\n## §5 next_step\n\nx\n",
        encoding="utf-8",
    )
    out = build_resume_inject("p", md)
    # 600 chars is generous head-room above the ≤50 token target
    # (≈200 chars). Tests catch ballooning, not exact byte length.
    assert len(out) < 600
