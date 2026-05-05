"""Tests for ``concinno configure-permissions`` CLI command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from concinno.cli import configure_permissions_cmd as mod

# ── plan_merge ─────────────────────────────────────────────────────


class TestPlanMerge:
    def test_empty_settings_gets_safe_patterns(self) -> None:
        new, added, skipped = mod.plan_merge({}, include_publish=False)
        assert new["permissions"]["allow"] == added
        assert "Bash(pytest*)" in added
        assert "Bash(ruff check*)" in added
        assert skipped == []

    def test_preserve_existing_allow_entries(self) -> None:
        existing = {
            "permissions": {
                "allow": ["Bash(my-custom-cmd*)"],
                "ask": ["Bash(something*)"],
                "deny": ["Bash(rm -rf /*)"],
            }
        }
        new, added, _ = mod.plan_merge(existing, include_publish=False)
        # Custom pre-existing entry must survive
        assert "Bash(my-custom-cmd*)" in new["permissions"]["allow"]
        # ask[] and deny[] must be untouched
        assert new["permissions"]["ask"] == ["Bash(something*)"]
        assert new["permissions"]["deny"] == ["Bash(rm -rf /*)"]

    def test_no_duplicate_on_rerun(self) -> None:
        existing = {"permissions": {"allow": ["Bash(pytest*)"]}}
        _, added, _ = mod.plan_merge(existing, include_publish=False)
        # Should NOT re-add pytest*
        assert "Bash(pytest*)" not in added

    def test_publish_opt_in_adds_twine_upload(self) -> None:
        _, added, _ = mod.plan_merge({}, include_publish=True)
        assert "Bash(twine upload*)" in added
        assert "Bash(npm publish*)" in added

    def test_publish_off_by_default_excludes_uploads(self) -> None:
        _, added, _ = mod.plan_merge({}, include_publish=False)
        assert "Bash(twine upload*)" not in added
        assert "Bash(npm publish*)" not in added

    def test_preserve_destructive_always_blocks_rm_rf(self) -> None:
        # Even if someone adds rm -rf to SAFE list externally, plan_merge
        # blocks via DESTRUCTIVE_PATTERNS cross-check.
        assert "Bash(rm -rf*)" in mod.DESTRUCTIVE_PATTERNS
        _, added, skipped = mod.plan_merge({}, preserve_destructive=True)
        assert "Bash(rm -rf*)" not in added

    def test_malformed_allow_list_reset(self) -> None:
        existing = {"permissions": {"allow": "not-a-list"}}
        new, added, _ = mod.plan_merge(existing)
        assert isinstance(new["permissions"]["allow"], list)
        assert len(added) > 0

    def test_input_settings_not_mutated(self) -> None:
        existing = {"permissions": {"allow": ["Bash(x*)"]}}
        original_copy = json.dumps(existing)
        mod.plan_merge(existing)
        assert json.dumps(existing) == original_copy


# ── Settings file I/O ──────────────────────────────────────────────


class TestSettingsIO:
    def test_read_missing_file_returns_empty(self, tmp_path: Path) -> None:
        data, warn = mod._read_settings(tmp_path / "nope.json")
        assert data == {} and warn is None

    def test_read_malformed_returns_warning(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json", encoding="utf-8")
        data, warn = mod._read_settings(p)
        assert data == {} and warn is not None
        assert "malformed" in warn.lower()

    def test_backup_copies_original(self, tmp_path: Path) -> None:
        p = tmp_path / "settings.json"
        p.write_text('{"x": 1}', encoding="utf-8")
        backup = mod._backup_settings(p)
        assert backup is not None and backup.is_file()
        assert backup.read_text(encoding="utf-8") == '{"x": 1}'

    def test_backup_returns_none_when_no_original(self, tmp_path: Path) -> None:
        assert mod._backup_settings(tmp_path / "nope.json") is None

    def test_atomic_write_creates_file(self, tmp_path: Path) -> None:
        p = tmp_path / "s.json"
        mod._atomic_write_settings(p, {"x": 1})
        assert json.loads(p.read_text(encoding="utf-8")) == {"x": 1}


# ── CLI entry ──────────────────────────────────────────────────────


class TestCLIEntry:
    def _run(
        self,
        tmp_path: Path,
        *,
        publish: bool = False,
        dry_run: bool = False,
        preserve_destructive: bool = True,
        starting_content: dict | None = None,
    ) -> tuple[Path, str, str]:
        settings = tmp_path / "settings.json"
        if starting_content is not None:
            settings.write_text(
                json.dumps(starting_content),
                encoding="utf-8",
            )
        ns = argparse.Namespace(
            publish=publish,
            dry_run=dry_run,
            preserve_destructive=preserve_destructive,
            path=str(settings),
        )
        return settings, ns, ""

    def test_default_adds_safe_patterns(
        self, tmp_path: Path, capsys,
    ) -> None:
        settings, ns, _ = self._run(tmp_path)
        mod.cmd_configure_permissions(ns)
        out = capsys.readouterr().out
        assert settings.is_file()
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert "Bash(pytest*)" in data["permissions"]["allow"]
        assert "added" in out

    def test_dry_run_does_not_write(self, tmp_path: Path, capsys) -> None:
        settings, ns, _ = self._run(tmp_path, dry_run=True)
        mod.cmd_configure_permissions(ns)
        out = capsys.readouterr().out
        assert not settings.is_file()
        assert "DRY RUN" in out

    def test_backup_created_on_actual_write(
        self, tmp_path: Path, capsys,
    ) -> None:
        settings, ns, _ = self._run(
            tmp_path, starting_content={"permissions": {"allow": []}},
        )
        mod.cmd_configure_permissions(ns)
        backups = list(tmp_path.glob("settings.json.backup-*"))
        assert len(backups) == 1

    def test_publish_flag_propagates(self, tmp_path: Path, capsys) -> None:
        settings, ns, _ = self._run(tmp_path, publish=True)
        mod.cmd_configure_permissions(ns)
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert "Bash(twine upload*)" in data["permissions"]["allow"]

    def test_malformed_existing_exits_nonzero(
        self, tmp_path: Path, capsys,
    ) -> None:
        p = tmp_path / "settings.json"
        p.write_text("{ broken", encoding="utf-8")
        ns = argparse.Namespace(
            publish=False,
            dry_run=False,
            preserve_destructive=True,
            path=str(p),
        )
        with pytest.raises(SystemExit) as exc:
            mod.cmd_configure_permissions(ns)
        assert exc.value.code == 2

    def test_idempotent_second_run(self, tmp_path: Path, capsys) -> None:
        settings, ns, _ = self._run(tmp_path)
        mod.cmd_configure_permissions(ns)
        capsys.readouterr()
        # Second run should be a no-op
        mod.cmd_configure_permissions(ns)
        out = capsys.readouterr().out
        assert "already present" in out


class TestRegister:
    def test_adds_subcommand(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        mod.register(sub)
        args = parser.parse_args(["configure-permissions"])
        assert args.publish is False
        assert args.preserve_destructive is True
        assert args.dry_run is False

    def test_publish_flag(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        mod.register(sub)
        args = parser.parse_args(["configure-permissions", "--publish"])
        assert args.publish is True
