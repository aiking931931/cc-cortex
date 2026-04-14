"""Tests for cc_cortex.prompt_hooks — LLM-as-Judge installer.

Covers:
  - PromptJudge dataclass: marker, rendered_prompt, build_spec
  - Judge constants: non-empty, correctly tagged, three shipped
  - build_hook_config: single, multi, grouping by (event, matcher)
  - install_prompt_hooks: new file, existing file, preserves other
    hooks, idempotent, dry_run, parent dir creation, atomic write
  - uninstall_prompt_hooks: removes CCC-owned only, cleans empty
    entries, refuses to touch user-owned specs
  - list_installed_judges: correct names, ignores non-CCC specs
  - _load_settings: empty / missing / invalid-JSON / non-dict error
"""

from __future__ import annotations

import json

import pytest

from cc_cortex.prompt_hooks import (
    ALL_JUDGES,
    CODE_QUALITY_JUDGE,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    EXCUSE_SCANNER_JUDGE,
    HALLUCINATION_JUDGE,
    MARKER_PREFIX,
    WIREDO_JUDGE,
    PromptJudge,
    build_hook_config,
    install_prompt_hooks,
    list_installed_judges,
    uninstall_prompt_hooks,
)

# ── PromptJudge basics ─────────────────────────────────────


class TestPromptJudge:
    def test_marker_includes_name(self):
        assert HALLUCINATION_JUDGE.marker() == "[cc-cortex:hallucination_judge]"

    def test_rendered_prompt_starts_with_marker(self):
        p = HALLUCINATION_JUDGE.rendered_prompt()
        assert p.startswith(HALLUCINATION_JUDGE.marker())

    def test_rendered_prompt_contains_arguments_placeholder(self):
        # Judges whose body has no $ARGUMENTS get one appended
        for judge in ALL_JUDGES:
            assert "$ARGUMENTS" in judge.rendered_prompt()

    def test_rendered_prompt_does_not_double_inject_arguments(self):
        custom = PromptJudge(
            name="custom",
            event="PostToolUse",
            matcher="Write",
            prompt_body="Review $ARGUMENTS and reply.",
            description="test",
        )
        assert custom.rendered_prompt().count("$ARGUMENTS") == 1

    def test_status_message_tagged(self):
        msg = HALLUCINATION_JUDGE.rendered_status_message()
        assert msg.startswith(HALLUCINATION_JUDGE.marker())
        assert "hallucination check" in msg

    def test_status_message_default_when_empty(self):
        j = PromptJudge(
            name="x", event="Stop", matcher="",
            prompt_body="do a thing", description="",
        )
        assert j.rendered_status_message().endswith("running…")

    def test_build_spec_shape(self):
        spec = HALLUCINATION_JUDGE.build_spec()
        assert spec["type"] == "prompt"
        assert spec["model"] == DEFAULT_MODEL
        assert spec["timeout"] == DEFAULT_TIMEOUT
        assert spec["prompt"].startswith(MARKER_PREFIX)
        assert spec["statusMessage"].startswith(MARKER_PREFIX)


# ── Shipped judge constants ─────────────────────────────────


class TestShippedJudges:
    def test_judges_shipped(self):
        names = {j.name for j in ALL_JUDGES}
        assert names == {
            "hallucination_judge",
            "excuse_scanner_judge",
            "code_quality_judge",
            "wiredo_judge",
        }

    def test_events_match_intent(self):
        assert HALLUCINATION_JUDGE.event == "PostToolUse"
        assert EXCUSE_SCANNER_JUDGE.event == "Stop"
        assert CODE_QUALITY_JUDGE.event == "PostToolUse"
        assert WIREDO_JUDGE.event == "Stop"

    def test_write_edit_matchers(self):
        assert HALLUCINATION_JUDGE.matcher == "Write|Edit"
        assert CODE_QUALITY_JUDGE.matcher == "Write|Edit"

    def test_stop_has_empty_matcher(self):
        assert EXCUSE_SCANNER_JUDGE.matcher == ""
        assert WIREDO_JUDGE.matcher == ""

    def test_every_judge_body_non_empty(self):
        for judge in ALL_JUDGES:
            assert len(judge.prompt_body.strip()) > 50
            assert "decision" in judge.prompt_body.lower()

    def test_wiredo_judge_strongest_d_dimension(self):
        """WIREDO is CBUA Law #4 — D (functional verification) is the
        strongest dimension. The prompt must explicitly call it out
        and reject 'tsc green / lint clean' as D evidence.
        """
        body = WIREDO_JUDGE.prompt_body
        assert "WIREDO" in body
        assert "D (Defended)" in body
        # The D-as-strongest framing is the load-bearing claim
        assert "STRONGEST" in body
        # Explicit rejection of the most common false-D claim
        assert "tsc green" in body
        assert "lint clean" in body
        # Auto-pass for pure docs (so this guard does not block this
        # very session's handoff/feedback edits)
        assert "docstring" in body.lower() or "comment" in body.lower()


# ── build_hook_config ──────────────────────────────────────


class TestBuildHookConfig:
    def test_all_judges_grouped_by_event(self):
        cfg = build_hook_config(ALL_JUDGES)
        assert set(cfg.keys()) == {"PostToolUse", "Stop"}

    def test_write_edit_judges_share_matcher_entry(self):
        """Hallucination + CodeQuality both target Write|Edit."""
        cfg = build_hook_config(ALL_JUDGES)
        post = cfg["PostToolUse"]
        assert len(post) == 1  # one matcher group
        entry = post[0]
        assert entry["matcher"] == "Write|Edit"
        assert len(entry["hooks"]) == 2

    def test_stop_entry_has_no_matcher(self):
        cfg = build_hook_config([EXCUSE_SCANNER_JUDGE])
        stop = cfg["Stop"]
        assert len(stop) == 1
        assert "matcher" not in stop[0]
        assert len(stop[0]["hooks"]) == 1

    def test_order_preserved(self):
        cfg = build_hook_config(
            [EXCUSE_SCANNER_JUDGE, HALLUCINATION_JUDGE],
        )
        # Stop came first, so Stop appears in the dict first in iter
        # (Python 3.7+ dict preserves insertion order)
        assert list(cfg.keys()) == ["Stop", "PostToolUse"]

    def test_different_matchers_get_separate_entries(self):
        j1 = PromptJudge(
            name="a", event="PostToolUse", matcher="Bash",
            prompt_body="check $ARGUMENTS", description="",
        )
        j2 = PromptJudge(
            name="b", event="PostToolUse", matcher="Write",
            prompt_body="check $ARGUMENTS", description="",
        )
        cfg = build_hook_config([j1, j2])
        post = cfg["PostToolUse"]
        assert len(post) == 2
        assert {e["matcher"] for e in post} == {"Bash", "Write"}


# ── install_prompt_hooks ───────────────────────────────────


class TestInstallPromptHooks:
    def test_install_new_file(self, tmp_path):
        settings = tmp_path / "settings.json"
        data = install_prompt_hooks(settings)
        assert settings.exists()
        on_disk = json.loads(settings.read_text(encoding="utf-8"))
        assert on_disk == data
        assert "hooks" in on_disk
        assert set(on_disk["hooks"].keys()) == {"PostToolUse", "Stop"}

    def test_install_creates_parent_dir(self, tmp_path):
        settings = tmp_path / "sub" / "dir" / "settings.json"
        install_prompt_hooks(settings)
        assert settings.exists()

    def test_install_dry_run_no_write(self, tmp_path):
        settings = tmp_path / "settings.json"
        data = install_prompt_hooks(settings, dry_run=True)
        assert "hooks" in data
        assert not settings.exists()

    def test_idempotent(self, tmp_path):
        settings = tmp_path / "settings.json"
        first = install_prompt_hooks(settings)
        second = install_prompt_hooks(settings)
        assert first == second
        # Double-check on disk: no duplicate specs
        disk = json.loads(settings.read_text(encoding="utf-8"))
        post_hooks = disk["hooks"]["PostToolUse"][0]["hooks"]
        assert len(post_hooks) == 2  # hallucination + code quality

    def test_preserves_existing_unrelated_hooks(self, tmp_path):
        settings = tmp_path / "settings.json"
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "echo hi"},
                        ],
                    },
                ],
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "echo bye"},
                        ],
                    },
                ],
            },
            "unrelatedKey": {"keep": "me"},
        }
        settings.write_text(json.dumps(existing), encoding="utf-8")

        install_prompt_hooks(settings)

        disk = json.loads(settings.read_text(encoding="utf-8"))
        # unrelated top-level key preserved
        assert disk["unrelatedKey"] == {"keep": "me"}
        # PreToolUse untouched
        assert disk["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
        # PostToolUse: Bash entry + new Write|Edit entry
        post = disk["hooks"]["PostToolUse"]
        assert len(post) == 2
        matchers = {e.get("matcher") for e in post}
        assert matchers == {"Bash", "Write|Edit"}

    def test_preserves_existing_user_prompt_in_same_matcher(self, tmp_path):
        """A user's own Write|Edit hook must survive CCC install."""
        settings = tmp_path / "settings.json"
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "my-own-linter",
                            },
                        ],
                    },
                ],
            },
        }
        settings.write_text(json.dumps(existing), encoding="utf-8")

        install_prompt_hooks(settings)
        disk = json.loads(settings.read_text(encoding="utf-8"))
        post = disk["hooks"]["PostToolUse"]
        assert len(post) == 1  # same matcher entry
        specs = post[0]["hooks"]
        # User spec at index 0, two CCC specs appended
        assert specs[0] == {"type": "command", "command": "my-own-linter"}
        assert len(specs) == 3

    def test_install_subset(self, tmp_path):
        settings = tmp_path / "settings.json"
        install_prompt_hooks(settings, judges=[HALLUCINATION_JUDGE])
        disk = json.loads(settings.read_text(encoding="utf-8"))
        assert "Stop" not in disk["hooks"]
        post_hooks = disk["hooks"]["PostToolUse"][0]["hooks"]
        assert len(post_hooks) == 1

    def test_install_refreshes_prompt_on_reinstall(self, tmp_path):
        """Re-installing replaces the stored spec (for prompt updates)."""
        settings = tmp_path / "settings.json"
        install_prompt_hooks(settings, judges=[HALLUCINATION_JUDGE])
        # Simulate a stale prompt by mutating the file
        disk = json.loads(settings.read_text(encoding="utf-8"))
        disk["hooks"]["PostToolUse"][0]["hooks"][0]["prompt"] = (
            f"{HALLUCINATION_JUDGE.marker()} OLD STALE PROMPT"
        )
        settings.write_text(json.dumps(disk), encoding="utf-8")

        install_prompt_hooks(settings, judges=[HALLUCINATION_JUDGE])
        disk2 = json.loads(settings.read_text(encoding="utf-8"))
        fresh_prompt = disk2["hooks"]["PostToolUse"][0]["hooks"][0]["prompt"]
        assert "OLD STALE PROMPT" not in fresh_prompt
        assert fresh_prompt == HALLUCINATION_JUDGE.rendered_prompt()
        # Still exactly one spec — no duplication
        assert len(disk2["hooks"]["PostToolUse"][0]["hooks"]) == 1


# ── uninstall_prompt_hooks ─────────────────────────────────


class TestUninstallPromptHooks:
    def test_uninstall_roundtrip(self, tmp_path):
        settings = tmp_path / "settings.json"
        install_prompt_hooks(settings)
        uninstall_prompt_hooks(settings)
        disk = json.loads(settings.read_text(encoding="utf-8"))
        # hooks removed entirely when everything CCC-owned is gone
        assert "hooks" not in disk

    def test_uninstall_missing_file_noop(self, tmp_path):
        settings = tmp_path / "nope.json"
        result = uninstall_prompt_hooks(settings)
        assert result == {}
        assert not settings.exists()

    def test_uninstall_preserves_user_hooks(self, tmp_path):
        settings = tmp_path / "settings.json"
        # Start with user content
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "my-linter"},
                        ],
                    },
                ],
            },
        }
        settings.write_text(json.dumps(existing), encoding="utf-8")
        install_prompt_hooks(settings)
        uninstall_prompt_hooks(settings)

        disk = json.loads(settings.read_text(encoding="utf-8"))
        assert disk["hooks"]["PostToolUse"] == existing["hooks"]["PostToolUse"]

    def test_uninstall_subset_leaves_other_judges(self, tmp_path):
        settings = tmp_path / "settings.json"
        install_prompt_hooks(settings)
        # Both Stop-event judges removed → Stop event entry disappears
        uninstall_prompt_hooks(
            settings, judges=[EXCUSE_SCANNER_JUDGE, WIREDO_JUDGE],
        )
        disk = json.loads(settings.read_text(encoding="utf-8"))
        assert "Stop" not in disk["hooks"]
        # PostToolUse still has the two Write|Edit judges
        post = disk["hooks"]["PostToolUse"][0]["hooks"]
        assert len(post) == 2

    def test_uninstall_ignores_user_modified_marker(self, tmp_path):
        """If a user edits the prompt header away, we leave the spec alone."""
        settings = tmp_path / "settings.json"
        install_prompt_hooks(settings, judges=[HALLUCINATION_JUDGE])
        # Strip the marker so the spec looks user-owned
        disk = json.loads(settings.read_text(encoding="utf-8"))
        disk["hooks"]["PostToolUse"][0]["hooks"][0]["prompt"] = (
            "I edited this by hand — no marker anymore."
        )
        disk["hooks"]["PostToolUse"][0]["hooks"][0]["statusMessage"] = "mine"
        settings.write_text(json.dumps(disk), encoding="utf-8")

        uninstall_prompt_hooks(settings, judges=[HALLUCINATION_JUDGE])
        disk2 = json.loads(settings.read_text(encoding="utf-8"))
        # The user-modified spec survives
        survivor = disk2["hooks"]["PostToolUse"][0]["hooks"][0]
        assert survivor["prompt"].startswith("I edited this")

    def test_uninstall_dry_run(self, tmp_path):
        settings = tmp_path / "settings.json"
        install_prompt_hooks(settings)
        before = settings.read_text(encoding="utf-8")
        uninstall_prompt_hooks(settings, dry_run=True)
        after = settings.read_text(encoding="utf-8")
        assert before == after


# ── list_installed_judges ──────────────────────────────────


class TestListInstalledJudges:
    def test_missing_file(self, tmp_path):
        assert list_installed_judges(tmp_path / "nope.json") == []

    def test_lists_all_after_install(self, tmp_path):
        settings = tmp_path / "settings.json"
        install_prompt_hooks(settings)
        names = list_installed_judges(settings)
        assert set(names) == {
            "hallucination_judge",
            "excuse_scanner_judge",
            "code_quality_judge",
            "wiredo_judge",
        }

    def test_ignores_non_ccc_specs(self, tmp_path):
        settings = tmp_path / "settings.json"
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write",
                        "hooks": [
                            {"type": "command", "command": "x"},
                            {
                                "type": "prompt",
                                "prompt": "user-owned prompt, no marker",
                                "statusMessage": "mine",
                            },
                        ],
                    },
                ],
            },
        }
        settings.write_text(json.dumps(existing), encoding="utf-8")
        assert list_installed_judges(settings) == []


# ── Error handling ─────────────────────────────────────────


class TestErrorHandling:
    def test_invalid_json_raises(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON"):
            install_prompt_hooks(settings)

    def test_non_dict_top_level_raises(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="top level"):
            install_prompt_hooks(settings)

    def test_non_dict_hooks_section_raises(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text('{"hooks": "oops"}', encoding="utf-8")
        with pytest.raises(ValueError, match="non-dict `hooks`"):
            install_prompt_hooks(settings)

    def test_non_list_event_raises(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(
            '{"hooks": {"PostToolUse": "oops"}}',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="must be a list"):
            install_prompt_hooks(settings)

    def test_empty_file_treated_as_empty_dict(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text("   \n  ", encoding="utf-8")
        data = install_prompt_hooks(settings)
        assert "hooks" in data
