"""Tests for concinno.core.notify.notify_waiting_on_user.

Origin: 2026-04-24 user directive — ``ask_user_toast`` hook only fires
when an ``AskUserQuestion`` tool call goes through the Claude Code
PreToolUse pipeline, so blocking paths that sit behind a chat-string
gate (release_authorization STRING_MATCH, destruction_guard, CLI
wizards) never reach the user's attention. ``notify_waiting_on_user``
is the reusable helper those paths call directly.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def notify_mod():
    from concinno.core import notify
    return notify


def test_notify_waiting_on_user_sync_fires(notify_mod, monkeypatch):
    calls = []

    def _fake_show_toast(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(notify_mod, "show_toast", _fake_show_toast)
    out = notify_mod.notify_waiting_on_user(
        "publish concinno 2.21.0 needs 'go publish ...'",
        async_fire=False,
    )
    assert out is True
    assert len(calls) == 1
    payload = calls[0]
    assert "2.21.0" in payload["message"]
    assert payload["tag"] == "concinno-waiting-on-user"
    assert payload["group"] == "concinno-waiting-on-user"


def test_notify_waiting_on_user_default_title_locale_en(
    notify_mod, monkeypatch,
):
    monkeypatch.setenv("CC_UX_LANG", "en")
    calls = []
    monkeypatch.setattr(
        notify_mod, "show_toast",
        lambda **kw: (calls.append(kw), True)[1],
    )
    notify_mod.notify_waiting_on_user("something", async_fire=False)
    assert "waiting" in calls[0]["title"].lower()


def test_notify_waiting_on_user_default_title_locale_zh_tw(
    notify_mod, monkeypatch,
):
    monkeypatch.setenv("CC_UX_LANG", "zh-TW")
    calls = []
    monkeypatch.setattr(
        notify_mod, "show_toast",
        lambda **kw: (calls.append(kw), True)[1],
    )
    notify_mod.notify_waiting_on_user("something", async_fire=False)
    assert "等你" in calls[0]["title"]


def test_notify_waiting_on_user_title_override(notify_mod, monkeypatch):
    calls = []
    monkeypatch.setattr(
        notify_mod, "show_toast",
        lambda **kw: (calls.append(kw), True)[1],
    )
    notify_mod.notify_waiting_on_user(
        "ctx", title="Publish blocked", async_fire=False,
    )
    assert calls[0]["title"] == "Publish blocked"


def test_notify_waiting_on_user_truncates_long_context(
    notify_mod, monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        notify_mod, "show_toast",
        lambda **kw: (calls.append(kw), True)[1],
    )
    long_ctx = "x" * 500
    notify_mod.notify_waiting_on_user(long_ctx, async_fire=False)
    assert len(calls[0]["message"]) <= 120


def test_notify_waiting_on_user_empty_context_falls_back_to_title(
    notify_mod, monkeypatch,
):
    monkeypatch.setenv("CC_UX_LANG", "en")
    calls = []
    monkeypatch.setattr(
        notify_mod, "show_toast",
        lambda **kw: (calls.append(kw), True)[1],
    )
    notify_mod.notify_waiting_on_user("   ", async_fire=False)
    assert calls[0]["message"] == calls[0]["title"]


def test_notify_waiting_on_user_show_toast_failure_returns_false(
    notify_mod, monkeypatch,
):
    def _raise(**_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(notify_mod, "show_toast", _raise)
    assert notify_mod.notify_waiting_on_user(
        "x", async_fire=False,
    ) is False


def test_notify_waiting_on_user_async_returns_true_on_queue(
    notify_mod, monkeypatch,
):
    # Default async_fire=True — helper returns True after queueing,
    # not after toast actually fires. Verify no deadlock / no sync
    # await of the daemon thread.
    monkeypatch.setattr(
        notify_mod, "show_toast", lambda **_kw: True,
    )
    out = notify_mod.notify_waiting_on_user("quick")
    assert out is True


def test_notify_waiting_on_user_custom_tag_group(notify_mod, monkeypatch):
    calls = []
    monkeypatch.setattr(
        notify_mod, "show_toast",
        lambda **kw: (calls.append(kw), True)[1],
    )
    notify_mod.notify_waiting_on_user(
        "ctx", tag="release-auth", group="release", async_fire=False,
    )
    assert calls[0]["tag"] == "release-auth"
    assert calls[0]["group"] == "release"


class TestReleaseAuthIntegration:
    """release_authorization.check_authorization deny path must toast."""

    def test_string_match_deny_fires_toast(self, monkeypatch):
        from concinno import release_authorization as ra
        from concinno.core import notify

        calls = []
        monkeypatch.setattr(
            notify, "notify_waiting_on_user",
            lambda ctx, **kw: (calls.append((ctx, kw)), True)[1],
        )
        cfg = ra.AuthorizationConfig(
            mode=ra.AuthorizationMode.STRING_MATCH, disabled=False,
        )
        allowed, reason = ra.check_authorization(
            "twine_upload", "concinno", "2.21.0",
            transcript_text="", config=cfg,
        )
        assert allowed is False
        assert "2.21.0" in reason
        assert len(calls) == 1
        ctx, kwargs = calls[0]
        assert "concinno@2.21.0" in ctx
        assert kwargs.get("tag") == "concinno-release-auth"

    def test_string_match_allow_does_not_toast(self, monkeypatch):
        from concinno import release_authorization as ra
        from concinno.core import notify

        calls = []
        monkeypatch.setattr(
            notify, "notify_waiting_on_user",
            lambda ctx, **kw: calls.append((ctx, kw)),
        )
        cfg = ra.AuthorizationConfig(
            mode=ra.AuthorizationMode.STRING_MATCH, disabled=False,
        )
        allowed, reason = ra.check_authorization(
            "twine_upload", "concinno", "2.21.0",
            transcript_text="go publish concinno 2.21.0",
            config=cfg,
        )
        assert allowed is True
        assert reason == ""
        assert calls == []

    def test_disabled_gate_does_not_toast(self, monkeypatch):
        from concinno import release_authorization as ra
        from concinno.core import notify

        calls = []
        monkeypatch.setattr(
            notify, "notify_waiting_on_user",
            lambda ctx, **kw: calls.append((ctx, kw)),
        )
        cfg = ra.AuthorizationConfig(
            mode=ra.AuthorizationMode.STRING_MATCH, disabled=True,
        )
        allowed, _ = ra.check_authorization(
            "twine_upload", "concinno", "2.21.0",
            transcript_text="", config=cfg,
        )
        assert allowed is True
        assert calls == []

    def test_askuser_mode_deny_also_toasts(self, monkeypatch):
        from concinno import release_authorization as ra
        from concinno.core import notify

        calls = []
        monkeypatch.setattr(
            notify, "notify_waiting_on_user",
            lambda ctx, **kw: (calls.append((ctx, kw)), True)[1],
        )
        cfg = ra.AuthorizationConfig(
            mode=ra.AuthorizationMode.ASKUSER_ANSWER, disabled=False,
        )
        allowed, _ = ra.check_authorization(
            "twine_upload", "concinno", "2.21.0",
            transcript_text="",
            askuser_answers=["No"],
            config=cfg,
        )
        assert allowed is False
        assert len(calls) == 1
        assert "ASKUSER_ANSWER" in calls[0][0]
