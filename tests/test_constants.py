"""Tests for cc_cortex.constants — tool sets and gate response factories."""

from __future__ import annotations

from cc_cortex.constants import READ_TOOLS, WRITE_TOOLS, WRITE_TOOLS_EXT, make_allow, make_deny

# ── Tool Sets ───────────────────────────────────────────────


def test_write_tools_contains_expected():
    assert "Edit" in WRITE_TOOLS
    assert "Write" in WRITE_TOOLS
    assert "NotebookEdit" in WRITE_TOOLS


def test_write_tools_is_frozenset():
    assert isinstance(WRITE_TOOLS, frozenset)


def test_write_tools_ext_superset_of_write_tools():
    assert WRITE_TOOLS.issubset(WRITE_TOOLS_EXT)
    assert "MultiEdit" in WRITE_TOOLS_EXT


def test_read_tools_contains_expected():
    for name in ("Read", "Grep", "Glob", "WebSearch", "WebFetch"):
        assert name in READ_TOOLS


def test_read_tools_is_frozenset():
    assert isinstance(READ_TOOLS, frozenset)


def test_no_overlap_between_read_and_write():
    assert WRITE_TOOLS & READ_TOOLS == frozenset()


# ── make_deny ───────────────────────────────────────────────


def test_make_deny_basic():
    result = make_deny("bad stuff")
    assert result == {"permissionDecision": "deny", "reason": "bad stuff"}


def test_make_deny_with_additional_context():
    result = make_deny("nope", additionalContext="hint")
    assert result["permissionDecision"] == "deny"
    assert result["reason"] == "nope"
    assert result["additionalContext"] == "hint"


def test_make_deny_extra_keys():
    result = make_deny("x", foo="bar", num=42)
    assert result["foo"] == "bar"
    assert result["num"] == 42


# ── make_allow ──────────────────────────────────────────────


def test_make_allow_basic():
    result = make_allow()
    assert result == {"permissionDecision": "allow"}


def test_make_allow_with_context():
    result = make_allow(additionalContext="info")
    assert result["permissionDecision"] == "allow"
    assert result["additionalContext"] == "info"


def test_make_allow_extra_keys():
    result = make_allow(x=1, y="z")
    assert result["x"] == 1
    assert result["y"] == "z"
