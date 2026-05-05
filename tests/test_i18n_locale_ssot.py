"""Tests for 2.6.1 hotfix F4 — i18n `_BUILTIN_LOCALES` derived from config.

Before 2.6.1, ``i18n._BUILTIN_LOCALES`` was a hand-maintained tuple that
drifted from ``config._VALID_LOCALES`` — a user could set
``concinno config set locale fr`` (accepted because ``fr`` was in
``_VALID_LOCALES``) and silently see English forever (i18n never loaded
``fr``). This suite pins the fix:

  1. The set of locale filenames i18n loads is derived from
     ``config._VALID_LOCALES`` (single source of truth).
  2. BCP-47 hyphen form (``zh-TW``) and underscore filename form
     (``zh_TW``) both resolve cleanly.
  3. A declared-but-missing translation file falls back to English
     AND emits one stderr warning (never silent).
"""

from __future__ import annotations

import importlib
import os

import pytest

from concinno import config as cfg


def _fresh_i18n():
    """Reload i18n to re-derive ``_BUILTIN_LOCALES`` after any
    monkey-patch of ``config._VALID_LOCALES``.
    """
    from concinno import i18n as _i18n
    importlib.reload(_i18n)
    return _i18n


@pytest.fixture(autouse=True)
def _wipe_env(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    for k in list(os.environ.keys()):
        if k.startswith("CONCINNO_") or k == "CC_UX_LANG":
            monkeypatch.delenv(k, raising=False)


class TestBuiltinLocalesDerivedFromConfig:
    def test_builtin_matches_valid_locale_count(self):
        i18n = _fresh_i18n()
        # One locale per valid entry, plus no extras.
        assert len(i18n._BUILTIN_LOCALES) == len(cfg._VALID_LOCALES)

    def test_hyphen_normalized_to_underscore(self):
        i18n = _fresh_i18n()
        # ``zh-TW`` in config becomes ``zh_TW`` on disk.
        assert "zh_TW" in i18n._BUILTIN_LOCALES
        assert "zh-TW" not in i18n._BUILTIN_LOCALES

    def test_all_plain_locales_present(self):
        i18n = _fresh_i18n()
        for loc in ("en", "ja", "ko", "fr", "de", "es"):
            assert loc in i18n._BUILTIN_LOCALES


class TestMissingLocaleEmitsWarning:
    """When a locale is declared in config but the JSON file is
    missing on disk, we fall back to English AND warn once — never
    silent."""

    def test_fr_no_translation_file_warns(self, monkeypatch, tmp_path, capsys):
        # fr.json is NOT shipped today — this is the canonical UX trap.
        monkeypatch.setenv("CC_UX_LANG", "fr")
        i18n = _fresh_i18n()
        # Trigger load.
        i18n.msg("some.key.that.does.not.exist")
        err = capsys.readouterr().err
        assert "fr" in err
        assert "no translation file" in err or "falling back" in err

    def test_warning_fires_once_per_locale(self, monkeypatch, capsys):
        monkeypatch.setenv("CC_UX_LANG", "fr")
        i18n = _fresh_i18n()
        i18n.msg("x")
        first = capsys.readouterr().err
        i18n.msg("y")  # should NOT warn again
        second = capsys.readouterr().err
        assert "fr" in first
        assert second == ""

    def test_en_never_warns(self, monkeypatch, capsys):
        monkeypatch.setenv("CC_UX_LANG", "en")
        i18n = _fresh_i18n()
        i18n.msg("x")
        assert capsys.readouterr().err == ""


# ── H1: expand() path traversal defense ────────────────────────


class TestExpandPathTraversal:
    """`expand()` must reject paths outside ``workspace_root`` when
    the caller opts in — protects against ``../../../etc/passwd``
    style reads via an untrusted ``source_path``.
    """

    def test_workspace_root_rejects_parent_traversal(self, tmp_path):
        from concinno.field_read import expand

        # Create a real file INSIDE workspace so traversal-relative
        # pointer can actually target something concrete.
        ws = tmp_path / "workspace"
        ws.mkdir()
        outside = tmp_path / "secret.md"
        outside.write_text("## secret\n\ntopsecret", encoding="utf-8")

        with pytest.raises(ValueError, match="outside workspace"):
            expand(str(outside), "secret", workspace_root=ws)

    def test_workspace_root_allows_inside(self, tmp_path):
        from concinno.field_read import expand

        ws = tmp_path / "workspace"
        ws.mkdir()
        good = ws / "handoff.md"
        good.write_text("## Section A\n\nbody", encoding="utf-8")

        result = expand(str(good), "section-a", workspace_root=ws)
        assert "body" in result
        assert result.startswith("## Section A")

    def test_absolute_outside_raises(self, tmp_path):
        from concinno.field_read import expand

        ws = tmp_path / "workspace"
        ws.mkdir()
        with pytest.raises(ValueError, match="outside workspace"):
            expand("/etc/passwd", "any", workspace_root=ws)

    def test_symlink_escape_rejected(self, tmp_path):
        """A symlink planted inside the workspace that points outside
        must still be caught — we resolve before comparing.
        """
        from concinno.field_read import expand

        ws = tmp_path / "workspace"
        ws.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("## x\n\ny", encoding="utf-8")

        link = ws / "trap.md"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlink not supported on this platform/permissions")

        with pytest.raises(ValueError, match="outside workspace"):
            expand(str(link), "x", workspace_root=ws)

    def test_default_no_workspace_root_backcompat(self, tmp_path):
        """Legacy callers that pass just (path, id) — no workspace_root
        — keep working for any reachable file. No ValueError, no
        traversal check.
        """
        from concinno.field_read import expand

        f = tmp_path / "legacy.md"
        f.write_text("## L\n\nlegacy body", encoding="utf-8")
        result = expand(str(f), "l")
        # Either hits the id or returns "" — both are legal; the point
        # is it doesn't raise on an absolute tmp_path outside cwd.
        assert isinstance(result, str)

    def test_explicit_none_disables_check(self, tmp_path):
        """Callers can opt OUT of the check by passing ``None`` —
        matches how test/tooling code signals ``I know what I'm doing``.
        """
        from concinno.field_read import expand

        f = tmp_path / "trust.md"
        f.write_text("## T\n\ntrust body", encoding="utf-8")
        result = expand(str(f), "t", workspace_root=None)
        assert isinstance(result, str)
