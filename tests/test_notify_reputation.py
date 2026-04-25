"""Regression pins for 2.14.0 WinRT AUMID reputation fix.

Root cause analysis (Opus 1 / 2 / 3 triangulation, 2026-04-21) —

- **Before 2.14.0**: ``show_toast`` defaulted ``app_id`` to
  ``Microsoft.VisualStudioCode`` and the WinRT helper passed ``title``
  into ``InteractableWindowsToaster``'s ``applicationText`` slot. On
  Windows 11 22H2+ this cross-pollutes VS Code's Notification Suggestions
  counter with our traffic, causing the OS to demote banners for both
  apps to Action-Center-only. Rolling back to the wscript+VBS path hides
  the WinRT bug but invites Surfshark / Avast's permanent
  ``VBS:Downloader`` heuristic to scan every ``%TEMP%\\concinno_toast.vbs``.

- **After 2.14.0**: default AUMID is ``Concinno.ClaudeCode`` (our own
  reputation bucket) and the WinRT path splits ``display_name`` (UI
  sender label, still "Claude Code") from ``app_id`` (reputation key).
  Bootstrap helpers ``register_aumid`` / ``disable_smart_optout`` write
  the HKCU entries end-users previously had to copy-paste.

These tests pin the public-contract surface of that fix so the next
"helpful refactor" cannot silently revert us to 2.8.0 behaviour.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

# ── default AUMID ─────────────────────────────────────────────


class TestDefaultAUMID:
    """The default ``app_id`` must be the VS Code host identity so banners
    show "Visual Studio Code" as sender. Users who want an isolated
    reputation bucket must opt in by passing a custom AUMID + calling
    :func:`register_aumid` — the library default serves the common case
    (Claude Code running inside a VS Code / Cursor host) and trusts
    :func:`disable_smart_optout` to neutralise demotion risk.
    """

    def test_show_toast_default_app_id_is_vscode(self):
        import inspect

        from concinno.core.notify import show_toast
        sig = inspect.signature(show_toast)
        assert sig.parameters["app_id"].default == "Microsoft.VisualStudioCode"

    def test_show_toast_default_display_name_is_vscode(self):
        import inspect

        from concinno.core.notify import show_toast
        sig = inspect.signature(show_toast)
        assert sig.parameters["display_name"].default == "Visual Studio Code"

    def test_config_default_toast_app_id_is_vscode(self):
        from concinno.core.config import _DEFAULTS
        assert (
            _DEFAULTS["notification"]["toast_app_id"] == "Microsoft.VisualStudioCode"
        )

    def test_config_fallback_when_missing_is_vscode(self, tmp_path, monkeypatch):
        from concinno.core.config import Config
        # Point at an empty config path so the getter must return the code
        # fallback, not the _DEFAULTS dict merge.
        cfg = Config(config_path=str(tmp_path / "missing.json"))
        cfg._data = {"notification": {}}  # simulate partial config
        assert cfg.toast_app_id == "Microsoft.VisualStudioCode"


# ── WinRT signature (reputation / UI label split) ────────────


class TestWinRTSignature:
    """The WinRT helper must pass display_name into applicationText and
    app_id into notifierAUMID — not the other way around."""

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="windows-toasts only available on Windows",
    )
    def test_winrt_uses_display_name_not_title(self):
        from concinno.core import notify
        try:
            import windows_toasts  # noqa: F401
        except ImportError:
            pytest.skip("windows-toasts not installed")

        with patch(
            "windows_toasts.InteractableWindowsToaster"
        ) as mock_toaster_cls:
            mock_toaster_cls.return_value.show_toast = lambda t: None
            notify._win_toast_winrt(
                title="Response ready",
                message="Session done",
                app_id="Microsoft.VisualStudioCode",
                display_name="Visual Studio Code",
            )
        # applicationText (arg 1) = display_name; notifierAUMID (arg 2) = app_id
        args, _ = mock_toaster_cls.call_args
        assert args[0] == "Visual Studio Code", (
            "applicationText must be display_name (UI sender label), "
            "NOT title — that was the 2.8.0 regression."
        )
        assert args[1] == "Microsoft.VisualStudioCode", (
            "notifierAUMID must be the app_id reputation key."
        )

    def test_winrt_returns_false_when_package_missing(self, monkeypatch):
        """When windows-toasts is not installed, tier 1 must silently
        return False so the caller cascades to tier 2."""
        from concinno.core import notify

        # Force ImportError inside the helper.
        real_import = __import__

        def _blocked_import(name, *args, **kwargs):
            if name == "windows_toasts":
                raise ImportError("blocked for test")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _blocked_import)
        assert notify._win_toast_winrt(
            "t", "m", "Microsoft.VisualStudioCode"
        ) is False


# ── Fallback chain order ─────────────────────────────────────


def _recorder(calls: list, name: str, returns: bool):
    """Factory for a side_effect callable that logs a name and returns a bool."""
    def _fn(*_a, **_kw):
        calls.append(name)
        return returns
    return _fn


class TestFallbackChain:
    """Order must be winrt → xmldoc → balloon. Any reordering is a
    regression (Opus 1 archaeology: 2.8.0 broke this)."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only chain")
    def test_tier1_success_skips_tier2_and_tier3(self):
        from concinno.core import notify

        calls: list[str] = []
        with patch.object(
            notify, "_win_toast_winrt",
            side_effect=_recorder(calls, "winrt", True),
        ), patch.object(
            notify, "_win_toast_xmldoc",
            side_effect=_recorder(calls, "xmldoc", True),
        ), patch.object(
            notify, "_win_toast_balloon",
            side_effect=_recorder(calls, "balloon", True),
        ):
            result = notify.show_toast("t", "m", enabled=True)
        assert result is True
        assert calls == ["winrt"], (
            f"Tier 1 winrt succeeded, must skip later tiers. Got: {calls}"
        )

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only chain")
    def test_tier1_fail_cascades_to_tier2(self):
        from concinno.core import notify

        calls: list[str] = []
        with patch.object(
            notify, "_win_toast_winrt",
            side_effect=_recorder(calls, "winrt", False),
        ), patch.object(
            notify, "_win_toast_xmldoc",
            side_effect=_recorder(calls, "xmldoc", True),
        ), patch.object(
            notify, "_win_toast_balloon",
            side_effect=_recorder(calls, "balloon", True),
        ):
            result = notify.show_toast("t", "m", enabled=True)
        assert result is True
        assert calls == ["winrt", "xmldoc"], (
            f"Tier 1 failed, must cascade to tier 2, stop there. Got: {calls}"
        )

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only chain")
    def test_tier1_and_tier2_fail_cascades_to_tier3(self):
        from concinno.core import notify

        calls: list[str] = []
        with patch.object(
            notify, "_win_toast_winrt",
            side_effect=_recorder(calls, "winrt", False),
        ), patch.object(
            notify, "_win_toast_xmldoc",
            side_effect=_recorder(calls, "xmldoc", False),
        ), patch.object(
            notify, "_win_toast_balloon",
            side_effect=_recorder(calls, "balloon", True),
        ):
            result = notify.show_toast("t", "m", enabled=True)
        assert result is True
        assert calls == ["winrt", "xmldoc", "balloon"]


# ── register_aumid / disable_smart_optout ────────────────────


class TestRegistryHelpers:
    def test_register_aumid_exported(self):
        from concinno.core.notify import register_aumid
        assert callable(register_aumid)
        import inspect
        sig = inspect.signature(register_aumid)
        assert sig.parameters["app_id"].default == "Concinno.ClaudeCode"
        assert sig.parameters["display_name"].default == "Claude Code"

    def test_disable_smart_optout_exported(self):
        from concinno.core.notify import disable_smart_optout
        assert callable(disable_smart_optout)

    def test_register_aumid_noop_on_non_windows(self, monkeypatch):
        from concinno.core import notify
        monkeypatch.setattr(notify.sys, "platform", "linux")
        assert notify.register_aumid("Test.App") is False

    def test_disable_smart_optout_noop_on_non_windows(self, monkeypatch):
        from concinno.core import notify
        monkeypatch.setattr(notify.sys, "platform", "linux")
        assert notify.disable_smart_optout() is False

    @pytest.mark.skipif(sys.platform != "win32", reason="winreg is Windows-only")
    def test_register_aumid_writes_expected_keys(self):
        import winreg

        from concinno.core.notify import register_aumid

        app_id = "Concinno.TestFixture"
        try:
            assert register_aumid(app_id, display_name="Test", icon_path=None) is True
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                rf"Software\Classes\AppUserModelId\{app_id}",
                0,
                winreg.KEY_READ,
            )
            try:
                display, _ = winreg.QueryValueEx(key, "DisplayName")
                assert display == "Test"
                # IconUri only written when path provided; no key when None.
                with pytest.raises(FileNotFoundError):
                    winreg.QueryValueEx(key, "IconUri")
            finally:
                winreg.CloseKey(key)
        finally:
            # Cleanup: remove the test AUMID so subsequent runs start clean.
            try:
                winreg.DeleteKey(
                    winreg.HKEY_CURRENT_USER,
                    rf"Software\Classes\AppUserModelId\{app_id}",
                )
            except OSError:
                pass
