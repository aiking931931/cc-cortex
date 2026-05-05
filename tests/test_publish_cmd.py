"""Tests for ``concinno publish`` CLI command."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from concinno.cli import publish_cmd as mod

# ── detect_package_name ────────────────────────────────────────────


class TestDetectPackageName:
    def test_reads_from_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "foo-bar"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        assert mod.detect_package_name(tmp_path) == "foo-bar"

    def test_returns_empty_if_no_pyproject(self, tmp_path: Path) -> None:
        assert mod.detect_package_name(tmp_path) == ""

    def test_returns_empty_if_no_name_key(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        assert mod.detect_package_name(tmp_path) == ""

    def test_handles_multiline_project_section(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[build-system]\n'
            'requires = ["hatchling"]\n\n'
            '[project]\n'
            '# comment\n'
            'description = "desc"\n'
            'name = "concinno-skills-google"\n',
            encoding="utf-8",
        )
        assert mod.detect_package_name(tmp_path) == "concinno-skills-google"


# ── find_queue_record ─────────────────────────────────────────────


class TestFindQueueRecord:
    def _write_coord(self, tmp_path: Path, body: str) -> Path:
        coord = tmp_path / "RELEASE_COORDINATION.md"
        coord.write_text(body, encoding="utf-8")
        return coord

    def test_finds_matching_version(self, tmp_path: Path) -> None:
        coord = self._write_coord(
            tmp_path,
            '```yaml\n- version: "2.16.0"\n  state: ready-to-publish\n```\n',
        )
        rec, warn = mod.find_queue_record(coord, "2.16.0")
        assert warn is None
        assert rec is not None
        assert rec["version"] == "2.16.0"
        assert rec["state"] == "ready-to-publish"

    def test_missing_version_returns_warning(self, tmp_path: Path) -> None:
        coord = self._write_coord(
            tmp_path,
            '- version: "2.15.0"\n  state: published\n',
        )
        rec, warn = mod.find_queue_record(coord, "2.16.0")
        assert rec is None and warn and "2.16.0" in warn

    def test_missing_coord_file_returns_warning(self, tmp_path: Path) -> None:
        rec, warn = mod.find_queue_record(
            tmp_path / "RELEASE_COORDINATION.md",
            "2.16.0",
        )
        assert rec is None and warn is not None


# ── verify_artifacts ──────────────────────────────────────────────


class TestVerifyArtifacts:
    def _make_artifacts(
        self, tmp_path: Path, package: str, version: str,
    ) -> None:
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / f"{package}-{version}-py3-none-any.whl").write_bytes(b"w")
        (dist / f"{package}-{version}.tar.gz").write_bytes(b"s")

    def test_finds_wheel_and_sdist(self, tmp_path: Path) -> None:
        self._make_artifacts(tmp_path, "concinno", "2.16.0")
        arts, err = mod.verify_artifacts(tmp_path, "concinno", "2.16.0")
        assert err is None
        assert len(arts) >= 2

    def test_normalized_package_name(self, tmp_path: Path) -> None:
        dist = tmp_path / "dist"
        dist.mkdir()
        # Package "concinno-skills-google" may be built as underscored
        (dist / "concinno_skills_google-0.1.0-py3-none-any.whl").write_bytes(b"w")
        (dist / "concinno_skills_google-0.1.0.tar.gz").write_bytes(b"s")
        arts, err = mod.verify_artifacts(
            tmp_path, "concinno-skills-google", "0.1.0",
        )
        assert err is None, err
        assert len(arts) >= 2

    def test_missing_dist_returns_error(self, tmp_path: Path) -> None:
        _, err = mod.verify_artifacts(tmp_path, "concinno", "2.16.0")
        assert err is not None and "does not exist" in err

    def test_missing_wheel_reports_error(self, tmp_path: Path) -> None:
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "concinno-2.16.0.tar.gz").write_bytes(b"s")
        arts, err = mod.verify_artifacts(tmp_path, "concinno", "2.16.0")
        assert err is not None and "wheel" in err.lower()

    def test_missing_sdist_reports_error(self, tmp_path: Path) -> None:
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "concinno-2.16.0-py3-none-any.whl").write_bytes(b"w")
        arts, err = mod.verify_artifacts(tmp_path, "concinno", "2.16.0")
        assert err is not None and "sdist" in err.lower()


# ── Full publish flow (mocked twine) ──────────────────────────────


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Build a minimal publishable project layout."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "concinno"\nversion = "2.16.0"\n',
        encoding="utf-8",
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "concinno-2.16.0-py3-none-any.whl").write_bytes(b"w")
    (dist / "concinno-2.16.0.tar.gz").write_bytes(b"s")
    (tmp_path / "RELEASE_COORDINATION.md").write_text(
        '```yaml\n- version: "2.16.0"\n  state: ready-to-publish\n```\n',
        encoding="utf-8",
    )
    return tmp_path


class TestPublishFlow:
    def test_package_mismatch_blocked(self, project: Path) -> None:
        result = mod._run_publish_flow(
            project_dir=project,
            requested_pkg="wrong-name",
            version="2.16.0",
            dry_run=True,
        )
        assert result.success is False
        assert "mismatch" in result.reason

    def test_dry_run_succeeds(self, project: Path, monkeypatch) -> None:
        monkeypatch.setattr(mod, "run_twine_check", lambda arts: (True, ""))
        result = mod._run_publish_flow(
            project_dir=project,
            requested_pkg="concinno",
            version="2.16.0",
            dry_run=True,
        )
        assert result.success, result.reason
        assert "DRY RUN" in result.reason
        assert len(result.uploaded_artifacts) >= 2

    def test_missing_queue_record_blocked(
        self, project: Path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(mod, "run_twine_check", lambda arts: (True, ""))
        result = mod._run_publish_flow(
            project_dir=project,
            requested_pkg="concinno",
            version="9.9.9",
            dry_run=True,
        )
        assert not result.success
        assert "No Queue record" in result.reason or "9.9.9" in result.reason

    def test_twine_check_failure_blocks(
        self, project: Path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            mod, "run_twine_check", lambda arts: (False, "distribution invalid"),
        )
        result = mod._run_publish_flow(
            project_dir=project,
            requested_pkg="concinno",
            version="2.16.0",
            dry_run=True,
        )
        assert not result.success
        assert "twine check FAILED" in result.reason

    def test_disabled_true_skips_confirmation(
        self, project: Path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(mod, "run_twine_check", lambda arts: (True, ""))
        monkeypatch.setattr(mod, "_release_auth_disabled", lambda: True)

        uploads: list[list[str]] = []

        def _fake_upload(artifacts, **_kwargs):
            uploads.append(list(artifacts))
            return True, "uploaded"

        monkeypatch.setattr(mod, "execute_twine_upload", _fake_upload)
        result = mod._run_publish_flow(
            project_dir=project,
            requested_pkg="concinno",
            version="2.16.0",
            dry_run=False,
        )
        assert result.success, result.reason
        assert len(uploads) == 1

    def test_disabled_false_requires_confirmation(
        self, project: Path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(mod, "run_twine_check", lambda arts: (True, ""))
        monkeypatch.setattr(mod, "_release_auth_disabled", lambda: False)
        monkeypatch.setattr(mod, "_confirm_from_tty", lambda pkg, ver: False)

        called = {"uploaded": False}

        def _block_upload(artifacts, **_kw):
            called["uploaded"] = True
            return True, "should not run"

        monkeypatch.setattr(mod, "execute_twine_upload", _block_upload)
        result = mod._run_publish_flow(
            project_dir=project,
            requested_pkg="concinno",
            version="2.16.0",
            dry_run=False,
        )
        assert not result.success
        assert "declined" in result.reason
        assert called["uploaded"] is False

    def test_upload_failure_reported(
        self, project: Path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(mod, "run_twine_check", lambda arts: (True, ""))
        monkeypatch.setattr(mod, "_release_auth_disabled", lambda: True)
        monkeypatch.setattr(
            mod,
            "execute_twine_upload",
            lambda arts, **k: (False, "403 Forbidden"),
        )
        result = mod._run_publish_flow(
            project_dir=project,
            requested_pkg="concinno",
            version="2.16.0",
            dry_run=False,
        )
        assert not result.success
        assert "twine upload FAILED" in result.reason


class TestCLIEntry:
    def test_cli_exits_with_code_1_on_failure(
        self, project: Path, monkeypatch,
    ) -> None:
        ns = argparse.Namespace(
            package="wrong",
            version="2.16.0",
            dry_run=True,
            path=str(project),
        )
        with pytest.raises(SystemExit) as exc:
            mod.cmd_publish(ns)
        assert exc.value.code == 1

    def test_cli_success_path_returns_cleanly(
        self, project: Path, monkeypatch, capsys,
    ) -> None:
        monkeypatch.setattr(mod, "run_twine_check", lambda arts: (True, ""))
        ns = argparse.Namespace(
            package="concinno",
            version="2.16.0",
            dry_run=True,
            path=str(project),
        )
        mod.cmd_publish(ns)  # should NOT raise
        out = capsys.readouterr().out
        assert "OK" in out


class TestRegister:
    def test_register_adds_subcommand(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        mod.register(sub)
        args = parser.parse_args(["publish", "concinno", "2.16.0"])
        assert args.package == "concinno"
        assert args.version == "2.16.0"
        assert args.dry_run is False
