"""Tests for cc_cortex.git_assist — Git status report generation."""

from __future__ import annotations

from unittest.mock import patch

from cc_cortex.git_assist import (
    _format_section,
    _gt,
    _is_secret,
    _is_trivial_path,
    _parse_status,
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
        with patch("cc_cortex.git_assist._git", return_value=None):
            assert generate_report("/tmp/no-repo") is None

    def test_clean_repo(self):
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": "",
            "branch --show-current": "main",
        }
        with patch("cc_cortex.git_assist._git", side_effect=_mock_git(responses)):
            assert generate_report("/tmp/clean") is None

    def test_staged_only(self):
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": "A  new_file.py",
            "branch --show-current": "main",
        }
        with patch("cc_cortex.git_assist._git", side_effect=_mock_git(responses)):
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
        with patch("cc_cortex.git_assist._git", side_effect=_mock_git(responses)):
            report = generate_report("/tmp/mod", locale="en")
        assert report is not None
        assert "changed.py" in report

    def test_untracked_only(self):
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": "?? temp.txt",
            "branch --show-current": "main",
        }
        with patch("cc_cortex.git_assist._git", side_effect=_mock_git(responses)):
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
        with patch("cc_cortex.git_assist._git", side_effect=_mock_git(responses)):
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
        with patch("cc_cortex.git_assist._git", side_effect=_mock_git(responses)):
            report = generate_report("/tmp/many", locale="en")
        assert "+2 " in report

    def test_truncation_untracked_gt_3(self):
        lines = "\n".join(f"?? u{i}.txt" for i in range(5))
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": lines,
            "branch --show-current": "main",
        }
        with patch("cc_cortex.git_assist._git", side_effect=_mock_git(responses)):
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
            patch("cc_cortex.git_assist._git", side_effect=_mock_git(responses)),
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
        with patch("cc_cortex.git_assist._git", side_effect=_mock_git(responses)):
            report = generate_report("/tmp/short")
            assert report is None


# ── _is_secret ───────────────────────────────────────────


class TestIsSecret:
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

    def test_substring_match_is_aggressive(self):
        # Known false positive: substring matching flags any path that
        # contains a secret keyword anywhere, even legitimate source.
        # Backlog: tighten to basename + word-boundary matching so files
        # like test_secret_scan.py / secretScanner.ts stop being filtered.
        assert _is_secret("test_secret_scan.py")
        assert _is_secret("services/secretScanner.ts")


# ── auto_commit ──────────────────────────────────────────


class TestAutoCommit:
    """Tests for the batch-staging auto_commit flow.

    Verifies the L0 rule "git add -A, never per-file": one stage shot
    for the whole working tree plus one defensive secret unstage.
    """

    def test_not_a_git_repo_returns_none(self):
        with patch("cc_cortex.git_assist._git", return_value=None):
            assert auto_commit("/tmp/no-repo") is None

    def test_clean_repo_returns_none(self):
        responses = {
            "rev-parse --is-inside-work-tree": "true",
            "status --short": "",
        }
        with patch(
            "cc_cortex.git_assist._git",
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
            "cc_cortex.git_assist._git",
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
            "cc_cortex.git_assist._git",
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
            "cc_cortex.git_assist._git",
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
            "cc_cortex.git_assist._git",
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
            "cc_cortex.git_assist._git",
            side_effect=capturing_git,
        ):
            auto_commit("/tmp/timeout", timeout=5)  # caller asks 5s

        # Floor is 60s — caller's 5s should be ignored for `add -A`.
        assert captured_timeouts == [60]


# ── _is_trivial_path ─────────────────────────────────────


class TestIsTrivialPath:
    def test_cache_dir(self):
        assert _is_trivial_path(".cc_cortex_cache/audit/guard_denies.jsonl")
        assert _is_trivial_path("projects/cc-cortex/.cc_cortex_cache/x.json")

    def test_marker_dir(self):
        assert _is_trivial_path(
            ".claude-forge/brain/cognition_shared/markers/abc.active",
        )

    def test_instance_lock(self):
        assert _is_trivial_path(
            ".claude-forge/brain/cognition_shared/instance_lock.json",
        )

    def test_transcript_path_text(self):
        assert _is_trivial_path(".cc_cortex_cache/transcript_path.txt")

    def test_streak_ux(self):
        assert _is_trivial_path(".cc_cortex_cache/streak_ux.json")

    def test_windows_backslash_normalised(self):
        assert _is_trivial_path(
            r".cc_cortex_cache\audit\guard_denies.jsonl",
        )

    def test_real_source_not_trivial(self):
        assert not _is_trivial_path("src/cc_cortex/git_assist.py")
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
        """`CC_CORTEX_NO_AUTOCOMMIT=1` short-circuits before any git call."""
        monkeypatch.setenv("CC_CORTEX_NO_AUTOCOMMIT", "1")
        called: list[list[str]] = []

        def recording_git(args, cwd, timeout=10):
            called.append(list(args))
            return ""

        with patch(
            "cc_cortex.git_assist._git",
            side_effect=recording_git,
        ):
            result = auto_commit("/tmp/optout")

        assert result is None
        assert called == []  # no git invocations at all

    def test_skips_when_only_cache_dirty(self):
        """A cache-only working tree should not commit anything."""
        status = (
            " M .cc_cortex_cache/audit/guard_denies.jsonl\n"
            " M .cc_cortex_cache/streak_ux.json\n"
            " M .cc_cortex_cache/transcript_path.txt"
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
            "cc_cortex.git_assist._git",
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
            "cc_cortex.git_assist._git",
            side_effect=recording_git,
        ):
            result = auto_commit("/tmp/marker-only")

        assert result is None
        assert not any(c[:1] == ["add"] for c in calls)

    def test_commits_when_mixed_real_and_trivial(self):
        """Real source change alongside cache noise must still commit."""
        status = (
            " M .cc_cortex_cache/audit/guard_denies.jsonl\n"
            " M src/cc_cortex/git_assist.py"
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
            "cc_cortex.git_assist._git",
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
            "cc_cortex.git_assist._git",
            side_effect=recording_git,
        ):
            result = auto_commit("/tmp/real")

        assert result is not None
        assert "auto: update 2 files" in result
