"""Regression pin for ``notify._get_locale`` multi-source fusion.

Mode 3 ("locale 斷鏈") has regressed twice after the 2026-04-21 fix. Both
times the function was simplified back to reading only ``Config.raw("locale")``
— which ignores the ``CC_UX_LANG`` env that ``.claude/hooks/on-stop.py``
sets, so the stop-event toast shows ``Response ready`` instead of
``已生成回應``.

These tests pin the ordered-source contract documented in
``~/.claude/skills/kb_notify_health/regression_modes.md``:

    CC_UX_LANG env → concinno.i18n.get_locale → Config.raw("locale") → "en"

plus loose-form normalization (``zh_TW`` / ``zh-tw`` / ``zh`` / ``zh-hant``
all map to the ``zh-TW`` key in ``_STRINGS``).
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_locale_sources(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    for k in list(os.environ.keys()):
        if k.startswith("CONCINNO_") or k == "CC_UX_LANG":
            monkeypatch.delenv(k, raising=False)


class TestCCUXLangEnvPriority:
    def test_zh_resolves_to_zh_tw(self, monkeypatch):
        from concinno.core.notify import _get_locale
        monkeypatch.setenv("CC_UX_LANG", "zh")
        assert _get_locale() == "zh-TW"

    def test_zh_underscore_tw_normalizes(self, monkeypatch):
        from concinno.core.notify import _get_locale
        monkeypatch.setenv("CC_UX_LANG", "zh_TW")
        assert _get_locale() == "zh-TW"

    def test_zh_hyphen_tw_normalizes(self, monkeypatch):
        from concinno.core.notify import _get_locale
        monkeypatch.setenv("CC_UX_LANG", "zh-tw")
        assert _get_locale() == "zh-TW"

    def test_zh_hant_normalizes(self, monkeypatch):
        from concinno.core.notify import _get_locale
        monkeypatch.setenv("CC_UX_LANG", "zh-Hant")
        assert _get_locale() == "zh-TW"

    def test_zh_cn_normalizes(self, monkeypatch):
        from concinno.core.notify import _get_locale
        monkeypatch.setenv("CC_UX_LANG", "zh_CN")
        assert _get_locale() == "zh-CN"

    def test_ja_prefix_normalizes(self, monkeypatch):
        from concinno.core.notify import _get_locale
        monkeypatch.setenv("CC_UX_LANG", "ja-JP")
        assert _get_locale() == "ja"

    def test_unknown_falls_back_to_en(self, monkeypatch):
        from concinno.core.notify import _get_locale
        monkeypatch.setenv("CC_UX_LANG", "fr")
        assert _get_locale() == "en"

    def test_empty_env_falls_through(self, monkeypatch):
        from concinno.core.notify import _get_locale
        monkeypatch.setenv("CC_UX_LANG", "")
        # Empty env must not mask later sources; with no other source set,
        # default is "en".
        assert _get_locale() == "en"


class TestTranslationAppliedForZhTW:
    """Invariant #1 from kb_notify_health: zh_TW must resolve `response_ready`
    to `已生成回應`. This is the end-to-end contract the stop-event toast
    depends on.
    """

    def test_response_ready_zh_tw(self, monkeypatch):
        from concinno.core.notify import _t
        monkeypatch.setenv("CC_UX_LANG", "zh")
        assert _t("response_ready") == "已生成回應"

    def test_response_ready_en_default(self):
        from concinno.core.notify import _t
        assert _t("response_ready") == "Response ready"
