"""Tests for ``concinno session-switches`` CLI command."""

from __future__ import annotations

import argparse
import json

import pytest

from concinno.cli import session_switches_cmd as mod


def _fake_release_auth(disabled: bool):
    """Factory: monkeypatch load_config to return a fake config."""

    class FakeCfg:
        def __init__(self) -> None:
            self.disabled = disabled
            self.source = "test"

    def _load_config():
        return FakeCfg()

    return _load_config


class TestBuildSummary:
    def test_returns_all_rows(self, monkeypatch, tmp_path) -> None:
        # isolate ~/.concinno so local user config doesn't leak in
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        rows = mod.build_summary()
        assert len(rows) >= 10

    def test_each_row_has_schema(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        for row in mod.build_summary():
            assert row.key
            assert row.default
            assert row.source
            assert isinstance(row.is_default, bool)


class TestResolvers:
    def test_release_auth_disabled_reads_from_load_config(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "concinno.release_authorization.load_config",
            _fake_release_auth(disabled=True),
        )
        val, source = mod._read_release_auth()
        assert val == "True"
        assert source == "test"

    def test_feature_enabled_falls_back_to_true(self, monkeypatch, tmp_path) -> None:
        # Point config at a nonexistent file so feature() returns default.
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        val, _ = mod._read_feature_enabled("some_unknown_feature")
        assert val == "True"


class TestFormatters:
    def _make_rows(self, *, non_default: bool = True) -> list[mod.SwitchRow]:
        return [
            mod.SwitchRow(
                key="release_auth.disabled",
                value="True" if non_default else "False",
                default="False",
                is_default=not non_default,
                source="test",
            ),
            mod.SwitchRow(
                key="handoff_mode",
                value="phase",
                default="phase",
                is_default=True,
                source="default",
            ),
        ]

    def test_format_text_only_non_default_by_default(self) -> None:
        out = mod.format_text(self._make_rows(non_default=True))
        assert "release_auth.disabled" in out
        # handoff_mode was default — should be hidden
        assert "handoff_mode" not in out

    def test_format_text_all_rows_with_show_all(self) -> None:
        out = mod.format_text(self._make_rows(), show_all=True)
        assert "release_auth.disabled" in out
        assert "handoff_mode" in out

    def test_format_text_all_default_says_nothing_to_show(self) -> None:
        rows = [
            mod.SwitchRow(
                key="x", value="y", default="y", is_default=True, source="s",
            ),
        ]
        out = mod.format_text(rows)
        assert "nothing to show" in out.lower() or "all defaults" in out.lower()

    def test_format_json_is_valid(self) -> None:
        rows = self._make_rows()
        out = mod.format_json(rows)
        payload = json.loads(out)
        assert payload["schema"] == "concinno.session_switches.v1"
        assert isinstance(payload["switches"], list)
        assert len(payload["switches"]) == 1  # only non-default

    def test_format_hook_single_line_no_control_chars(self) -> None:
        out = mod.format_hook(self._make_rows())
        assert out.startswith("concinno: active switches — ")
        assert "release_auth.disabled=True" in out
        assert "\r" not in out
        # \n is fine; hook format doesn't mandate newline

    def test_format_hook_empty_when_all_default(self) -> None:
        rows = [
            mod.SwitchRow(
                key="x", value="y", default="y", is_default=True, source="s",
            ),
        ]
        assert mod.format_hook(rows) == ""


class TestCLIEntry:
    def test_text_default(self, monkeypatch, tmp_path, capsys) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        ns = argparse.Namespace(format="text", all=False)
        mod.cmd_session_switches(ns)
        out = capsys.readouterr().out
        # Something sane is printed (either non-default or "nothing to show")
        assert out.strip()

    def test_json_format_is_parseable(
        self, monkeypatch, tmp_path, capsys,
    ) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        ns = argparse.Namespace(format="json", all=True)
        mod.cmd_session_switches(ns)
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["schema"] == "concinno.session_switches.v1"

    def test_hook_format_writes_to_stderr_not_stdout(
        self, monkeypatch, tmp_path, capsys,
    ) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        # Force a non-default so we get output
        monkeypatch.setattr(
            mod,
            "_read_release_auth",
            lambda: ("True", "test"),
        )
        ns = argparse.Namespace(format="hook", all=False)
        mod.cmd_session_switches(ns)
        cap = capsys.readouterr()
        assert "concinno: active switches" in cap.err
        assert cap.out == ""  # stdout stays empty for pipeline safety

    def test_feature_disabled_silences_output(
        self, monkeypatch, tmp_path, capsys,
    ) -> None:
        monkeypatch.setenv("CONCINNO_SESSION_SWITCHES_ENABLED", "0")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        ns = argparse.Namespace(format="hook", all=False)
        mod.cmd_session_switches(ns)
        cap = capsys.readouterr()
        assert cap.out == "" and cap.err == ""


class TestUserConfigPath:
    def test_user_top_n_is_respected(self, monkeypatch, tmp_path) -> None:
        cfg_dir = tmp_path / ".concinno"
        cfg_dir.mkdir()
        (cfg_dir / "session_switches.json").write_text(
            json.dumps({"top_n": 3}),
            encoding="utf-8",
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        rows = mod.build_summary()
        # top_n=3 → we get up to TOP_SWITCHES[:4] (3+1 window)
        assert len(rows) <= 4

    def test_malformed_user_config_falls_back_gracefully(
        self, monkeypatch, tmp_path,
    ) -> None:
        cfg_dir = tmp_path / ".concinno"
        cfg_dir.mkdir()
        (cfg_dir / "session_switches.json").write_text(
            "garbage {",
            encoding="utf-8",
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        # Should not raise
        rows = mod.build_summary()
        assert len(rows) >= 10


class TestRegister:
    def test_adds_subcommand(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        mod.register(sub)
        # Parsing the subcommand should succeed
        args = parser.parse_args(["session-switches"])
        assert args.format == "text"

    def test_accepts_format_flag(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        mod.register(sub)
        for fmt in ("text", "json", "hook"):
            args = parser.parse_args(["session-switches", "--format", fmt])
            assert args.format == fmt

    def test_rejects_invalid_format(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        mod.register(sub)
        with pytest.raises(SystemExit):
            parser.parse_args(["session-switches", "--format", "xml"])
