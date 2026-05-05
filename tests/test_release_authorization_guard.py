"""Tests for the 3.1.3 ReleaseAuthorizationGuard wiring + version
extraction fixes.

Background: until 3.1.3 ``release_authorization.check_authorization``
was a function only the user-side ``concinno publish`` CLI ever called.
The agent's own ``Bash(twine upload …)`` calls were ungated, so the
``release_auth.disabled`` toggle had no observable effect at hook time.
The wiring audit on 2026-04-26 caught this; these tests pin the fix.
"""
from __future__ import annotations

import json

import pytest

from concinno.guards.base import GuardAction, GuardContext
from concinno.release_authorization import (
    ReleaseAuthorizationGuard,
    _extract_pkg_version_from_command,
    detect_publish_operation,
)

# ── version extraction (PEP 427/440/625 shaped names) ───────────────


@pytest.mark.parametrize(
    "command,expected_pkg,expected_ver",
    [
        # wheel — both `python -m twine` and bare `twine`, both path styles
        (
            "python -m twine upload dist/concinno-3.1.3-py3-none-any.whl",
            "concinno",
            "3.1.3",
        ),
        (
            "twine upload dist\\concinno-3.1.3-py3-none-any.whl",
            "concinno",
            "3.1.3",
        ),
        # sdist .tar.gz
        (
            "twine upload dist/concinno-3.1.3.tar.gz",
            "concinno",
            "3.1.3",
        ),
        # shell-glob shortcut
        (
            "twine upload dist/concinno-3.1.3*",
            "concinno",
            "3.1.3",
        ),
        # PEP 440 pre-release tag
        (
            "twine upload dist/concinno-3.1.3rc1-py3-none-any.whl",
            "concinno",
            "3.1.3rc1",
        ),
        # PEP 440 post-release tag
        (
            "twine upload dist/concinno-3.1.3.post1.tar.gz",
            "concinno",
            "3.1.3.post1",
        ),
        # PEP 503 normalization (underscore → hyphen, lowercase)
        (
            "twine upload dist/Concinno_Skills_Auth-2.0.0-py3-none-any.whl",
            "concinno-skills-auth",
            "2.0.0",
        ),
    ],
)
def test_extract_pkg_version_known_shapes(command, expected_pkg, expected_ver):
    """Each shape recognised by twine maps cleanly to (pkg, version)."""
    pkg, ver = _extract_pkg_version_from_command(command, "twine_upload")
    assert pkg == expected_pkg
    assert ver == expected_ver


def test_extract_pkg_version_unrecognised_returns_blank():
    """An unrecognised publish op (e.g. npm) returns (\"\", \"\") so the
    guard skips rather than overblocks the call."""
    assert _extract_pkg_version_from_command(
        "npm publish", "npm_publish",
    ) == ("", "")


# ── operation detection (echo-strip immunity) ───────────────────────


def test_detect_publish_operation_echo_strip():
    """``echo "twine upload …"`` should not register as a real publish."""
    assert detect_publish_operation('echo "twine upload dist/foo"') is None


def test_detect_publish_operation_real_twine():
    assert (
        detect_publish_operation("python -m twine upload dist/foo")
        == "twine_upload"
    )


def test_detect_publish_operation_git_tag_push():
    assert (
        detect_publish_operation("git push origin v3.1.3")
        == "git_tag_push_remote"
    )


# ── ReleaseAuthorizationGuard wiring ────────────────────────────────


def _ctx(command: str, session_id: str = "") -> GuardContext:
    return GuardContext(
        tool_name="Bash",
        tool_input={"command": command},
        session_id=session_id,
        cache_dir="",
        hook_event="PreToolUse",
    )


def test_guard_skips_non_bash():
    g = ReleaseAuthorizationGuard()
    ctx = GuardContext(
        tool_name="Read",
        tool_input={"file_path": "/tmp/x"},
        session_id="",
        cache_dir="",
        hook_event="PreToolUse",
    )
    assert g.check(ctx) is None


def test_guard_skips_when_no_publish_op():
    g = ReleaseAuthorizationGuard()
    assert g.check(_ctx("ls dist/")) is None


def test_guard_skips_when_disabled(monkeypatch):
    """``release_auth.disabled=True`` short-circuits to ALLOW so harness
    permissions become the only check (two-layer-gate principle)."""
    monkeypatch.setenv("CONCINNO_RELEASE_AUTH_DISABLED", "1")
    g = ReleaseAuthorizationGuard()
    cmd = "twine upload dist/concinno-3.1.3-py3-none-any.whl"
    assert g.check(_ctx(cmd)) is None


def test_guard_skips_when_extraction_fails(monkeypatch):
    """Unparseable target → skip (don't overblock npm publish scripts)."""
    monkeypatch.setenv("CONCINNO_RELEASE_AUTH_DISABLED", "0")
    g = ReleaseAuthorizationGuard()
    # No dist/<pkg>-<ver> shape → extraction returns ("", "") → guard skips
    assert g.check(_ctx("npm publish")) is None


def test_guard_denies_without_authorization(monkeypatch, tmp_path):
    """Default mode (STRING_MATCH) + no auth string in transcript → deny."""
    monkeypatch.setenv("CONCINNO_RELEASE_AUTH_DISABLED", "0")
    monkeypatch.delenv("CONCINNO_RELEASE_AUTH_MODE", raising=False)
    g = ReleaseAuthorizationGuard()
    cmd = "twine upload dist/concinno-3.1.3-py3-none-any.whl"
    # Empty session_id → transcript reader returns "" → deny
    result = g.check(_ctx(cmd, session_id=""))
    assert result is not None
    assert result.action == GuardAction.DENY
    assert "go publish concinno 3.1.3" in result.reason
    # Metadata should carry op + pkg + ver for downstream renderers
    assert result.metadata.get("operation") == "twine_upload"
    assert result.metadata.get("package") == "concinno"
    assert result.metadata.get("version") == "3.1.3"


def test_guard_allows_when_transcript_has_authorization(monkeypatch, tmp_path):
    """Transcript scan finds ``go publish concinno 3.1.3`` → allow."""
    monkeypatch.setenv("CONCINNO_RELEASE_AUTH_DISABLED", "0")

    # Build a fake transcript JSONL containing the auth string.
    fake_transcript = tmp_path / "fake-transcript.jsonl"
    fake_transcript.write_text(
        json.dumps({
            "type": "user",
            "message": {"content": "ok go publish concinno 3.1.3"},
        }) + "\n",
        encoding="utf-8",
    )

    # Monkeypatch find_transcript to point at our fake file.
    import concinno.core.path_utils as path_utils

    monkeypatch.setattr(
        path_utils, "find_transcript", lambda sid: str(fake_transcript),
    )

    g = ReleaseAuthorizationGuard()
    cmd = "twine upload dist/concinno-3.1.3-py3-none-any.whl"
    assert g.check(_ctx(cmd, session_id="any-session-id")) is None


# ── Pipeline registration smoke test ────────────────────────────────


def test_release_authorization_guard_registered_in_default_pipeline():
    """Wiring audit: 3.1.3 must register the guard in
    ``_register_security`` so the publish gate actually runs at
    PreToolUse. Before 3.1.3 the class existed but was never registered.
    """
    from concinno.guards.registry import create_default_pipeline

    pipe = create_default_pipeline()
    names = {g.name for g in pipe._guards}
    assert "release_authorization" in names
    assert "publish_scan" in names  # other audit-orphan also fixed in 3.1.3
    assert "semver_gate" in names
