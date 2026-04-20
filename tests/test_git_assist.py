"""Tests for concinno.git_assist — Git status report generation."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from concinno.git_assist import (
    _clear_stale_index_lock,
    _format_section,
    _gt,
    _is_large_unignored,
    _is_secret,
    _is_trivial_path,
    _large_file_threshold,
    _parse_status,
    _resolve_index_lock_path,
    auto_commit,
    generate_report,
)

# ── i18n helper ──────────────────────────────────────────


class TestGt:
    def test_english_default(self):
        assert _gt("staged") == "Staged"

    def test_unknown_locale_still_returns(self):
        # _gt now uses i18n system; locale param is vestigial
        result = _gt("staged", "fr")
        assert isinstance(result, str) and len(result) > 0

    def test_unknown_key_returns_key(self):
        assert _gt("nonexistent") == "git_assist.nonexistent"


# ── _parse_status ────────────────────────────────────────


class TestParseStatus:
    def test_staged(self):
        s, u, ut = _parse_status("A  new.py")
        assert s == ["new.py"]
        assert u == [] and ut == []

    def test_modified(self):
        s, u, ut = _parse_status(" M changed.py")
        assert u == ["changed.py"]
        assert s == [] and ut == []

    def test_untracked(self):
        s, u, ut = _parse_status("?? temp.txt")
        assert ut == ["temp.txt"]
        assert s == [] and u == []

    def test_mixed(self):
        s, u, ut = _parse_status("A  new.py\n M mod.py\n?? extra.txt")
        assert s == ["new.py"]
        assert u == ["mod.py"]
        assert ut == ["extra.txt"]

    def test_short_line_skipped(self):
        s, u, ut = _parse_status("AB")
        assert s == [] and u == [] and ut == []


# ── _format_section ──────────────────────────────────────


class TestFormatSection:
    def test_basic(self):
        result = _format_section("🟡", "Staged", ["a.py", "b.py"], 5, "more")
        assert "Staged (2)" in result
        assert "a.py, b.py" in result

    def test_truncation(self):
        items = [f"f{i}.py" for i in range(7)]
        result = _format_section("🟡", "Staged", items, 5, "more")
        assert "+2 more" in result


# ── generate_report ──────────────────────────────────────


def _mock_git(responses: dict):
    """Create a mock for _git that returns responses based on args."""
    def fake_git(args, cwd, timeout=10):
        key = " ".join(args)
        return responses.get(key)
    return fake_git


class TestGenerateReport:
    def test_not_a_git_repo(self):
        with patch("concinno.git_assist._git", return_value=None):
            assert generate_report("/tmp/no-repo") is None

    def test_clean_repo(self):
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": "",
            "branch --show-current": "main",
        }
        with patch("concinno.git_assist._git", side_effect=_mock_git(responses)):
            assert generate_report("/tmp/clean") is None

    def test_staged_only(self):
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": "A  new_file.py",
            "branch --show-current": "main",
        }
        with patch("concinno.git_assist._git", side_effect=_mock_git(responses)):
            report = generate_report("/tmp/staged", locale="en")
        assert report is not None
        assert "1 " in report
        assert "new_file.py" in report
        assert "main" in report

    def test_modified_only(self):
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": " M changed.py",
            "branch --show-current": "dev",
        }
        with patch("concinno.git_assist._git", side_effect=_mock_git(responses)):
            report = generate_report("/tmp/mod", locale="en")
        assert report is not None
        assert "changed.py" in report

    def test_untracked_only(self):
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": "?? temp.txt",
            "branch --show-current": "main",
        }
        with patch("concinno.git_assist._git", side_effect=_mock_git(responses)):
            report = generate_report("/tmp/unt", locale="en")
        assert report is not None
        assert "temp.txt" in report

    def test_mixed_status(self):
        status = "A  new.py\n M mod.py\n?? extra.txt"
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": status,
            "branch --show-current": "feature",
        }
        with patch("concinno.git_assist._git", side_effect=_mock_git(responses)):
            report = generate_report("/tmp/mix", locale="en")
        assert report is not None
        assert "3 " in report

    def test_truncation_staged_gt_5(self):
        lines = "\n".join(f"A  file{i}.py" for i in range(7))
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": lines,
            "branch --show-current": "main",
        }
        with patch("concinno.git_assist._git", side_effect=_mock_git(responses)):
            report = generate_report("/tmp/many", locale="en")
        assert "+2 " in report

    def test_truncation_untracked_gt_3(self):
        lines = "\n".join(f"?? u{i}.txt" for i in range(5))
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": lines,
            "branch --show-current": "main",
        }
        with patch("concinno.git_assist._git", side_effect=_mock_git(responses)):
            report = generate_report("/tmp/many-u", locale="en")
        assert "+2 " in report

    def test_default_cwd(self):
        """generate_report with cwd=None uses env or os.getcwd()."""
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": " M x.py",
            "branch --show-current": "main",
        }
        with (
            patch("concinno.git_assist._git", side_effect=_mock_git(responses)),
            patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": "/env/dir"}),
        ):
            report = generate_report(locale="en")
        assert report is not None

    def test_short_line_skipped(self):
        """Lines < 3 chars are skipped; if all skipped, returns None."""
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": "AB",
            "branch --show-current": "main",
        }
        with patch("concinno.git_assist._git", side_effect=_mock_git(responses)):
            report = generate_report("/tmp/short")
            assert report is None


# ── _is_secret ───────────────────────────────────────────


class TestIsSecret:
    """Basename + word-boundary secret detector.

    Previous implementation was substring-on-whole-path, which
    false-positived on test fixtures, scanner source files, and any
    module whose name talked ABOUT secrets. The new matcher keys on
    os.path.basename() with word-boundary tokens and explicit
    test-dir / test-naming whitelists.
    """

    def test_env_file(self):
        assert _is_secret(".env")
        assert _is_secret("path/to/.env.local")

    def test_credentials(self):
        assert _is_secret("aws_credentials.json")
        assert _is_secret("path/credentials.txt")

    def test_keys(self):
        assert _is_secret("server.pem")
        assert _is_secret("client.p12")
        assert _is_secret(".ssh/id_rsa")
        assert _is_secret(".ssh/id_ed25519")

    def test_token_json(self):
        assert _is_secret("oauth/token.json")

    def test_safe_files(self):
        assert not _is_secret("README.md")
        assert not _is_secret("src/main.py")

    # ── parametrised true-positive suite ─────────────────
    # Real credential files that MUST still be detected.
    @pytest.mark.parametrize(
        "path",
        [
            # credential-word basenames
            "_AI_BRAIN/00_System/keys/google_credentials.json",
            "aws_credentials.json",
            "~/.aws/credentials",
            "config/credentials.yml",
            # api_key / service_account / access_token variants
            "secrets/api_key.json",
            "service_account.json",
            "google_service-account.json",
            "access_token.txt",
            "private_key.pem",
            # classic key/secret file extensions
            "server.pem",
            "client.p12",
            "release.pfx",
            "secrets.gpg",
            "deploy.key",
            "app.keystore",
            # SSH key pairs
            "id_rsa",
            ".ssh/id_rsa",
            ".ssh/id_ed25519",
            ".ssh/id_ecdsa",
            ".ssh/id_dsa",
            "id_rsa.pub",
            # .env family
            ".env",
            ".env.local",
            ".env.production",
            # login / publish credentials
            ".pypirc",
            ".netrc",
            "oauth/token.json",
            # High-signal compound secret tokens
            "jwt_secret_key.txt",
            "app_secret_token.env",
            "secret.key",
            "my_secret_key.pem",
        ],
    )
    def test_true_positive_still_detected(self, path):
        assert _is_secret(path), f"expected secret: {path}"

    # ── parametrised true-negative suite ─────────────────
    # Source / test / scanner files that the OLD substring matcher
    # mis-flagged. Each case is the regression root for a real-world
    # false positive.
    @pytest.mark.parametrize(
        "path",
        [
            # Scanner source code — talks ABOUT secrets, is not one
            "tests/test_secret_scan.py",
            "projects/concinno/tests/test_secret_scan.py",
            "src/services/secretScanner.ts",
            "web/src/scanners/secretScanner.ts",
            "downloads/src/services/teamMemorySync/secretScanner.ts",
            "downloads/src/services/teamMemorySync/teamMemSecretGuard.ts",
            "projects/concinno/src/concinno/secret_scan.py",
            # Test fixtures — NEVER real credentials
            "__tests__/credentials.test.ts",
            "tests/fixtures/fake_credentials.json",
            "examples/api_key_example.json",
            "samples/service_account_sample.json",
            "mocks/mock_credentials.json",
            "spec/token_spec.py",
            # pytest / jest naming convention at basename level
            "test_credentials.py",
            "test_api_key.py",
            "_test_credentials.py",
            "credentials.test.ts",
            "api_key.test.tsx",
            "token.spec.js",
            # Source files whose module talks ABOUT secrets
            "concinno/git_assist.py",
            "src/concinno/secret_scan.py",
            "kb_security.md",
            "docs/security-best-practices.md",
            # camelCase scanner class — no word separator, no match
            "SecretScanner.java",
            "ApiKeyHandler.java",
            # generic README / CHANGELOG
            "README.md",
            "CHANGELOG.md",
            "src/main.py",
        ],
    )
    def test_true_negative_not_flagged(self, path):
        assert not _is_secret(path), f"false positive: {path}"

    def test_empty_path(self):
        assert not _is_secret("")

    def test_windows_backslash_normalised(self):
        # Windows separators should still resolve to the correct basename
        assert _is_secret(r"C:\Users\me\.aws\credentials")
        assert not _is_secret(r"projects\tests\test_secret_scan.py")

    def test_case_insensitive(self):
        assert _is_secret("AWS_CREDENTIALS.JSON")
        assert _is_secret("ID_RSA")
        assert not _is_secret("TESTS/TEST_SECRET_SCAN.PY")

    def test_scanner_fixture_in_handoff(self):
        """Exact paths from the handoff backlog — regression guards."""
        # True positives the old matcher caught AND new matcher still catches
        assert _is_secret("_AI_BRAIN/00_System/keys/google_calendar_credentials.json")
        assert _is_secret("_AI_BRAIN/00_System/keys/aiking_line_credentials.txt")
        assert _is_secret("_AI_BRAIN/00_System/keys/line_channel_credentials.txt")
        assert _is_secret("_AI_BRAIN/00_System/keys/token.json")
        # False positives the old matcher wrongly caught — now fixed
        assert not _is_secret("projects/concinno/tests/test_secret_scan.py")
        assert not _is_secret(
            "downloads/src/services/teamMemorySync/secretScanner.ts"
        )


# ── auto_commit ──────────────────────────────────────────


class TestAutoCommit:
    """Tests for the batch-staging auto_commit flow.

    Verifies the L0 rule "git add -A, never per-file": one stage shot
    for the whole working tree plus one defensive secret unstage.
    """

    def test_not_a_git_repo_returns_none(self):
        with patch("concinno.git_assist._git", return_value=None):
            assert auto_commit("/tmp/no-repo") is None

    def test_clean_repo_returns_none(self):
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": "",
        }
        with patch(
            "concinno.git_assist._git",
            side_effect=_mock_git(responses),
        ):
            assert auto_commit("/tmp/clean") is None

    def test_batch_stage_then_commit(self):
        """957-file working tree should issue one `add -A`, not 957 adds."""
        # Build a status with many modified files
        status_lines = "\n".join(f" M file{i}.py" for i in range(50))
        calls: list[list[str]] = []

        def recording_git(args, cwd, timeout=10):
            calls.append(list(args))
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if args[:2] == ["status", "--short"]:
                return status_lines
            if args[:2] == ["add", "-A"]:
                return ""
            if args[:2] == ["commit", "-m"]:
                return "ok"
            if args[:2] == ["rev-list", "--count"]:
                return "1"
            return ""

        with patch(
            "concinno.git_assist._git",
            side_effect=recording_git,
        ):
            msg = auto_commit("/tmp/big")

        assert msg is not None
        assert "auto: update 50 files" in msg
        # Critical L0 invariant: exactly one `add` call, and it's `-A`.
        add_calls = [c for c in calls if c[:1] == ["add"]]
        assert len(add_calls) == 1
        assert add_calls[0] == ["add", "-A"]
        # No per-file `add -- <file>` calls.
        per_file_adds = [c for c in calls if c[:2] == ["add", "--"]]
        assert per_file_adds == []

    def test_secret_files_unstaged_after_batch(self):
        """Defensive: secret-like files get `git reset HEAD --` after add."""
        status_lines = (
            " M src/main.py\n"
            " M aws_credentials.json\n"
            " M secret_token.json"
        )
        calls: list[list[str]] = []

        def recording_git(args, cwd, timeout=10):
            calls.append(list(args))
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if args[:2] == ["status", "--short"]:
                return status_lines
            if args[:2] == ["add", "-A"]:
                return ""
            if args[:3] == ["reset", "HEAD", "--"]:
                return ""
            if args[:2] == ["commit", "-m"]:
                return "ok"
            if args[:2] == ["rev-list", "--count"]:
                return "1"
            return ""

        with patch(
            "concinno.git_assist._git",
            side_effect=recording_git,
        ):
            auto_commit("/tmp/mixed")

        # One reset, listing both secret files
        reset_calls = [c for c in calls if c[:1] == ["reset"]]
        assert len(reset_calls) == 1
        assert "aws_credentials.json" in reset_calls[0]
        assert "secret_token.json" in reset_calls[0]
        # main.py not in reset (it's safe)
        assert "src/main.py" not in reset_calls[0]

    def test_all_files_secret_returns_none(self):
        """If every file is a secret, nothing safe to commit."""
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": " M .env\n M id_rsa",
        }
        with patch(
            "concinno.git_assist._git",
            side_effect=_mock_git(responses),
        ):
            assert auto_commit("/tmp/all-secret") is None

    def test_batch_add_failure_returns_none(self):
        """If `add -A` fails, return None — don't commit."""
        def failing_git(args, cwd, timeout=10):
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if args[:2] == ["status", "--short"]:
                return " M file.py"
            if args[:2] == ["add", "-A"]:
                return None  # simulate add failure
            return ""

        with patch(
            "concinno.git_assist._git",
            side_effect=failing_git,
        ):
            assert auto_commit("/tmp/add-fail") is None

    def test_timeout_floor_60s(self):
        """op_timeout = max(60, caller_timeout). Verify floor enforced."""
        captured_timeouts: list[int] = []

        def capturing_git(args, cwd, timeout=10):
            if args[:2] == ["add", "-A"]:
                captured_timeouts.append(timeout)
                return ""
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if args[:2] == ["status", "--short"]:
                return " M x.py"
            if args[:2] == ["commit", "-m"]:
                return "ok"
            if args[:2] == ["rev-list", "--count"]:
                return "1"
            return ""

        with patch(
            "concinno.git_assist._git",
            side_effect=capturing_git,
        ):
            auto_commit("/tmp/timeout", timeout=5)  # caller asks 5s

        # Floor is 60s — caller's 5s should be ignored for `add -A`.
        assert captured_timeouts == [60]


# ── _is_trivial_path ─────────────────────────────────────


class TestLargeFileThreshold:
    """``CONCINNO_LARGE_FILE_THRESHOLD`` env var + default."""

    def test_default_is_10_mib(self, monkeypatch):
        monkeypatch.delenv("CONCINNO_LARGE_FILE_THRESHOLD", raising=False)
        assert _large_file_threshold() == 10_485_760

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CONCINNO_LARGE_FILE_THRESHOLD", "1024")
        assert _large_file_threshold() == 1024

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CONCINNO_LARGE_FILE_THRESHOLD", "not-a-number")
        assert _large_file_threshold() == 10_485_760

    def test_zero_or_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv("CONCINNO_LARGE_FILE_THRESHOLD", "0")
        assert _large_file_threshold() == 10_485_760
        monkeypatch.setenv("CONCINNO_LARGE_FILE_THRESHOLD", "-1")
        assert _large_file_threshold() == 10_485_760


class TestIsLargeUnignored:
    """Size-based filter used by ``auto_commit`` to unstage bulked blobs
    before they hit outer .git history (MEMORY #77 / 2.10.3 治本)."""

    def test_small_file_is_not_large(self, tmp_path):
        f = tmp_path / "small.txt"
        f.write_bytes(b"x" * 100)
        assert not _is_large_unignored("small.txt", str(tmp_path))

    def test_file_above_threshold_is_large(self, tmp_path):
        f = tmp_path / "big.bin"
        f.write_bytes(b"x" * 2048)
        assert _is_large_unignored("big.bin", str(tmp_path), threshold=1024)

    def test_missing_file_is_not_large(self, tmp_path):
        # Deleted between stage and check — should not count as large.
        assert not _is_large_unignored("ghost.bin", str(tmp_path))

    def test_custom_threshold_via_env(self, tmp_path, monkeypatch):
        f = tmp_path / "medium.bin"
        f.write_bytes(b"x" * 5000)
        monkeypatch.setenv("CONCINNO_LARGE_FILE_THRESHOLD", "1000")
        assert _is_large_unignored("medium.bin", str(tmp_path))
        monkeypatch.setenv("CONCINNO_LARGE_FILE_THRESHOLD", "10000")
        assert not _is_large_unignored("medium.bin", str(tmp_path))

    def test_directory_is_not_large(self, tmp_path):
        # Dir entries are not regular files; never flag them even if
        # something inside happens to be huge.
        (tmp_path / "subdir").mkdir()
        assert not _is_large_unignored("subdir", str(tmp_path))


class TestIsTrivialPath:
    def test_cache_dir(self):
        assert _is_trivial_path(".concinno_cache/audit/guard_denies.jsonl")
        assert _is_trivial_path("projects/concinno/.concinno_cache/x.json")

    def test_marker_dir(self):
        assert _is_trivial_path(
            ".claude-forge/brain/cognition_shared/markers/abc.active",
        )

    def test_instance_lock(self):
        assert _is_trivial_path(
            ".claude-forge/brain/cognition_shared/instance_lock.json",
        )

    def test_transcript_path_text(self):
        assert _is_trivial_path(".concinno_cache/transcript_path.txt")

    def test_streak_ux(self):
        assert _is_trivial_path(".concinno_cache/streak_ux.json")

    def test_windows_backslash_normalised(self):
        assert _is_trivial_path(
            r".concinno_cache\audit\guard_denies.jsonl",
        )

    def test_real_source_not_trivial(self):
        assert not _is_trivial_path("src/concinno/git_assist.py")
        assert not _is_trivial_path("README.md")
        assert not _is_trivial_path("tests/test_git_assist.py")

    def test_empty_path(self):
        assert not _is_trivial_path("")


# ── auto_commit skip gates ───────────────────────────────


class TestAutoCommitSkipGates:
    """Polling/benchmark sessions must not pollute git history.

    Without these gates, every Stop event would commit cache writes /
    session heartbeat markers, racing the .git/index.lock with sibling
    sessions that are doing real work. Verified end-to-end by the
    polling-session-burns-5min-on-heartbeat regression that motivated
    this change.
    """

    def test_env_override_skips_everything(self, monkeypatch):
        """`CONCINNO_NO_AUTOCOMMIT=1` short-circuits before any git call."""
        monkeypatch.setenv("CONCINNO_NO_AUTOCOMMIT", "1")
        called: list[list[str]] = []

        def recording_git(args, cwd, timeout=10):
            called.append(list(args))
            return ""

        with patch(
            "concinno.git_assist._git",
            side_effect=recording_git,
        ):
            result = auto_commit("/tmp/optout")

        assert result is None
        assert called == []  # no git invocations at all

    def test_skips_when_only_cache_dirty(self):
        """A cache-only working tree should not commit anything."""
        status = (
            " M .concinno_cache/audit/guard_denies.jsonl\n"
            " M .concinno_cache/streak_ux.json\n"
            " M .concinno_cache/transcript_path.txt"
        )
        calls: list[list[str]] = []

        def recording_git(args, cwd, timeout=10):
            calls.append(list(args))
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if args[:2] == ["status", "--short"]:
                return status
            return ""

        with patch(
            "concinno.git_assist._git",
            side_effect=recording_git,
        ):
            result = auto_commit("/tmp/cache-only")

        assert result is None
        # status was queried, but no add / commit ever ran
        assert not any(c[:1] == ["add"] for c in calls)
        assert not any(c[:2] == ["commit", "-m"] for c in calls)

    def test_skips_when_only_marker_dirty(self):
        """Marker `.active` heartbeats are noise."""
        status = (
            " M .claude-forge/brain/cognition_shared/markers/sess1.active\n"
            " M .claude-forge/brain/cognition_shared/instance_lock.json"
        )
        calls: list[list[str]] = []

        def recording_git(args, cwd, timeout=10):
            calls.append(list(args))
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if args[:2] == ["status", "--short"]:
                return status
            return ""

        with patch(
            "concinno.git_assist._git",
            side_effect=recording_git,
        ):
            result = auto_commit("/tmp/marker-only")

        assert result is None
        assert not any(c[:1] == ["add"] for c in calls)

    def test_commits_when_mixed_real_and_trivial(self):
        """Real source change alongside cache noise must still commit."""
        status = (
            " M .concinno_cache/audit/guard_denies.jsonl\n"
            " M src/concinno/git_assist.py"
        )
        calls: list[list[str]] = []

        def recording_git(args, cwd, timeout=10):
            calls.append(list(args))
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if args[:2] == ["status", "--short"]:
                return status
            if args[:2] == ["add", "-A"]:
                return ""
            if args[:2] == ["commit", "-m"]:
                return "ok"
            if args[:2] == ["rev-list", "--count"]:
                return "1"
            return ""

        with patch(
            "concinno.git_assist._git",
            side_effect=recording_git,
        ):
            result = auto_commit("/tmp/mixed")

        assert result is not None
        # batch -A still ran exactly once
        add_calls = [c for c in calls if c[:1] == ["add"]]
        assert add_calls == [["add", "-A"]]

    def test_commits_when_only_real_source(self):
        """No trivial files at all → commit normally."""
        status = " M src/main.py\n M tests/test_main.py"
        calls: list[list[str]] = []

        def recording_git(args, cwd, timeout=10):
            calls.append(list(args))
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if args[:2] == ["status", "--short"]:
                return status
            if args[:2] == ["add", "-A"]:
                return ""
            if args[:2] == ["commit", "-m"]:
                return "ok"
            if args[:2] == ["rev-list", "--count"]:
                return "1"
            return ""

        with patch(
            "concinno.git_assist._git",
            side_effect=recording_git,
        ):
            result = auto_commit("/tmp/real")

        assert result is not None
        assert "auto: update 2 files" in result


# ── stale .git/index.lock recovery ────────────────────────


class TestResolveIndexLockPath:
    """``.git`` layout resolution — normal dir vs worktree gitdir-file."""

    def test_normal_repo_layout(self, tmp_path):
        (tmp_path / ".git").mkdir()
        resolved = _resolve_index_lock_path(str(tmp_path))
        assert resolved.endswith(".git" + __import__("os").sep + "index.lock") or \
               resolved.endswith(".git/index.lock")

    def test_no_dotgit_falls_back_safely(self, tmp_path):
        # no .git anywhere — helper must not explode; caller's stat will miss.
        resolved = _resolve_index_lock_path(str(tmp_path))
        assert resolved.endswith("index.lock")

    def test_worktree_gitdir_file(self, tmp_path):
        """Worktrees and submodules write ``.git`` as a file of form ``gitdir: <abs>``."""
        import os as _os
        real_gitdir = tmp_path / "real_gitdir"
        real_gitdir.mkdir()
        dot_git_file = tmp_path / "wt" / ".git"
        dot_git_file.parent.mkdir()
        dot_git_file.write_text(f"gitdir: {real_gitdir}\n", encoding="utf-8")
        resolved = _resolve_index_lock_path(str(dot_git_file.parent))
        assert resolved == _os.path.join(str(real_gitdir), "index.lock")


class TestClearStaleIndexLock:
    """Orphan lock recovery — the 2.8.1 root cause fix.

    Prior failure: a killed ``git commit`` leaves ``.git/index.lock`` on
    disk; every subsequent commit returns
    ``fatal: Unable to create '.git/index.lock': File exists`` until a
    human intervenes. Sub-agents misread this as "pre-commit hook
    recreating the lock" and reach for ``--no-verify``, which bypasses
    nothing because no hook is involved. This test suite locks down the
    recovery contract so the misdiagnosis can't recur.
    """

    def _make_lock(self, tmp_path, age_sec: float) -> str:
        """Create a .git/index.lock with mtime == now - age_sec."""
        import os as _os
        import time as _time
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        lock = git_dir / "index.lock"
        lock.write_text("", encoding="utf-8")  # zero-byte, like real git
        when = _time.time() - age_sec
        _os.utime(str(lock), (when, when))
        return str(lock)

    def test_no_lock_returns_true(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert _clear_stale_index_lock(str(tmp_path)) is True

    def test_missing_dotgit_returns_true(self, tmp_path):
        # not a repo at all → nothing to clear, caller may proceed
        assert _clear_stale_index_lock(str(tmp_path)) is True

    def test_fresh_lock_bails(self, tmp_path):
        """A lock 5s old means a sibling is mid-op — must NOT remove."""
        import os as _os
        lock = self._make_lock(tmp_path, age_sec=5)
        assert _clear_stale_index_lock(str(tmp_path), max_age=60) is False
        assert _os.path.exists(lock), "fresh lock must survive"

    def test_stale_lock_removed(self, tmp_path):
        """A lock 120s old is orphaned — remove and return True."""
        import os as _os
        lock = self._make_lock(tmp_path, age_sec=120)
        assert _clear_stale_index_lock(str(tmp_path), max_age=60) is True
        assert not _os.path.exists(lock), "stale lock must be cleared"

    def test_env_threshold_override(self, tmp_path, monkeypatch):
        """``CONCINNO_LOCK_STALE_SEC`` reconfigures staleness threshold."""
        import os as _os
        lock = self._make_lock(tmp_path, age_sec=15)
        # default 60 → still fresh
        assert _clear_stale_index_lock(str(tmp_path)) is False
        assert _os.path.exists(lock)
        # tighten to 10s → now stale
        monkeypatch.setenv("CONCINNO_LOCK_STALE_SEC", "10")
        assert _clear_stale_index_lock(str(tmp_path)) is True
        assert not _os.path.exists(lock)

    def test_auto_commit_clears_stale_lock_then_proceeds(self, tmp_path):
        """End-to-end: auto_commit runs lock-clear before any write op."""
        import os as _os

        # Set up a stale orphan lock
        lock = self._make_lock(tmp_path, age_sec=120)

        status = " M src/main.py"
        calls: list[list[str]] = []

        def recording_git(args, cwd, timeout=10):
            calls.append(list(args))
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if args[:2] == ["status", "--short"]:
                return status
            if args[:2] == ["add", "-A"]:
                # By the time add runs, the lock must be gone
                assert not _os.path.exists(lock), (
                    "auto_commit tried to stage while stale lock present"
                )
                return ""
            if args[:2] == ["commit", "-m"]:
                return "ok"
            if args[:2] == ["rev-list", "--count"]:
                return "1"
            return ""

        with patch("concinno.git_assist._git", side_effect=recording_git):
            result = auto_commit(str(tmp_path))

        assert result is not None
        assert not _os.path.exists(lock), "stale lock should be cleared"
        # rev-parse ran first, then status, then the clear happened, then add
        assert any(c[:1] == ["add"] for c in calls)

    def test_auto_commit_bails_on_fresh_lock_no_race(self, tmp_path):
        """Fresh lock → auto_commit bails early without racing ``add``."""
        self._make_lock(tmp_path, age_sec=5)

        status = " M src/main.py"
        calls: list[list[str]] = []

        def recording_git(args, cwd, timeout=10):
            calls.append(list(args))
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if args[:2] == ["status", "--short"]:
                return status
            return ""

        with patch("concinno.git_assist._git", side_effect=recording_git):
            result = auto_commit(str(tmp_path))

        assert result is None
        # We must NOT have invoked add / commit while a peer was active
        assert not any(c[:1] == ["add"] for c in calls)
        assert not any(c[:2] == ["commit", "-m"] for c in calls)
