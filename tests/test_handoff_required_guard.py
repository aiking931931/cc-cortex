"""Tests for cc_cortex.handoff_required_guard."""

from __future__ import annotations

import json
import os
import time

import pytest

from cc_cortex import handoff_required_guard as hrg


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Redirect circuit-breaker state file into tmp_path."""
    state_path = tmp_path / "handoff_required_block.json"
    monkeypatch.setattr(hrg, "_BLOCK_STATE_PATH", str(state_path))
    return state_path


@pytest.fixture
def stub_config(monkeypatch):
    """Stub cc_cortex.core.config.get_config() with adjustable feature()."""
    class _Cfg:
        def __init__(
            self, enabled=True, min_files=3,
            structural_gate_enabled=True,
            min_added_lines=10, min_signal_hits=2,
        ):
            self._enabled = enabled
            self._min_files = min_files
            self._structural_gate_enabled = structural_gate_enabled
            self._min_added_lines = min_added_lines
            self._min_signal_hits = min_signal_hits

        def feature(self, name, key):
            if name != "handoff_required_guard":
                return None
            if key == "enabled":
                return self._enabled
            if key == "min_files":
                return self._min_files
            if key == "structural_gate_enabled":
                return self._structural_gate_enabled
            if key == "min_added_lines":
                return self._min_added_lines
            if key == "min_signal_hits":
                return self._min_signal_hits
            return None

    cfg_holder = {"cfg": _Cfg()}

    def _get_config():
        return cfg_holder["cfg"]

    # Patch the import-site lookup used inside on_stop()
    import cc_cortex.core.config as core_config
    monkeypatch.setattr(core_config, "get_config", _get_config)
    return cfg_holder


def _patch_git(monkeypatch, changed_files: list[str]):
    """Replace _git_session_changed_files to return a fixed list."""
    monkeypatch.setattr(
        hrg, "_git_session_changed_files",
        lambda project_dir, since_seconds=hrg._SESSION_LOOKBACK_S: list(changed_files),
    )


def test_no_session_id_returns_none(isolated_state, stub_config, monkeypatch):
    _patch_git(monkeypatch, ["a.py", "b.py", "c.py"])
    assert hrg.on_stop({}) is None
    assert hrg.on_stop({"session_id": ""}) is None


def test_no_changes_returns_none(isolated_state, stub_config, monkeypatch):
    _patch_git(monkeypatch, [])
    assert hrg.on_stop({"session_id": "s1"}) is None


def test_under_min_files_returns_none(isolated_state, stub_config, monkeypatch):
    _patch_git(monkeypatch, ["only_one.py"])
    assert hrg.on_stop({"session_id": "s-under"}) is None


def test_handoff_present_returns_none(isolated_state, stub_config, monkeypatch):
    files = [
        "src/foo.py", "src/bar.py", "src/baz.py",
        "src/qux.py", "src/quux.py",
        "06_Handoffs/king/交接_King.md",
    ]
    _patch_git(monkeypatch, files)
    # Second-layer gate (1.18.1): also stub structural content so the
    # diff has enough lines + signals to satisfy _has_structural_update.
    monkeypatch.setenv("CC_CORTEX_HANDOFF_MINIMAL", "1")
    assert hrg.on_stop({"session_id": "s-ok"}) is None


def test_blocks_when_work_no_handoff(isolated_state, stub_config, monkeypatch):
    files = [
        "src/a.py", "src/b.py", "src/c.py",
        "src/d.py", "src/e.py",
    ]
    _patch_git(monkeypatch, files)
    result = hrg.on_stop({"session_id": "s-block"})
    assert result is not None
    assert result.startswith("HANDOFF_REQUIRED_BLOCK:")
    assert "5" in result  # source_count surfaced in reason


def test_circuit_breaker_only_blocks_once(isolated_state, stub_config, monkeypatch):
    files = ["a.py", "b.py", "c.py", "d.py"]
    _patch_git(monkeypatch, files)
    sid = "s-cb"
    first = hrg.on_stop({"session_id": sid})
    assert first is not None and first.startswith("HANDOFF_REQUIRED_BLOCK:")
    # Second call: same session, still no handoff → suppressed by breaker
    second = hrg.on_stop({"session_id": sid})
    assert second is None


def test_feature_disabled_returns_none(isolated_state, stub_config, monkeypatch):
    stub_config["cfg"]._enabled = False
    _patch_git(monkeypatch, ["a.py", "b.py", "c.py", "d.py"])
    assert hrg.on_stop({"session_id": "s-disabled"}) is None


def test_min_files_configurable(isolated_state, stub_config, monkeypatch):
    stub_config["cfg"]._min_files = 10
    _patch_git(monkeypatch, ["a.py", "b.py", "c.py", "d.py", "e.py"])
    assert hrg.on_stop({"session_id": "s-minfiles"}) is None


def test_handoff_prefix_detection_chinese(isolated_state, stub_config, monkeypatch):
    files = [
        "a.py", "b.py", "c.py", "d.py",
        "06_Handoffs/交接_test.md",
    ]
    _patch_git(monkeypatch, files)
    # 1.18.1: minimal-update escape keeps the prefix-detection test
    # focused on layer-1 logic without fighting the structural gate.
    monkeypatch.setenv("CC_CORTEX_HANDOFF_MINIMAL", "1")
    assert hrg.on_stop({"session_id": "s-zh"}) is None


def test_handoff_prefix_detection_english(isolated_state, stub_config, monkeypatch):
    files = [
        "a.py", "b.py", "c.py", "d.py",
        "docs/handoff_test.md",
    ]
    # Stub i18n patterns to include the English prefix even if not loaded
    monkeypatch.setattr(hrg, "_handoff_prefixes", lambda: ("交接_", "handoff_"))
    _patch_git(monkeypatch, files)
    monkeypatch.setenv("CC_CORTEX_HANDOFF_MINIMAL", "1")
    assert hrg.on_stop({"session_id": "s-en"}) is None


def test_extension_whitelist_excludes_binary(isolated_state, stub_config, monkeypatch):
    # 5 binary files should not count as source
    files = ["a.png", "b.jpg", "c.bin", "d.zip", "e.pdf"]
    _patch_git(monkeypatch, files)
    assert hrg.on_stop({"session_id": "s-bin"}) is None


def test_count_source_files_helper():
    files = [
        "a.py", "b.ts", "c.md", "d.json",   # 4 source
        "e.png", "f.bin", "g.zip",          # 3 non-source
    ]
    assert hrg._count_source_files(files) == 4


def test_filter_handoff_files_helper():
    prefixes = ("交接_", "handoff_")
    files = [
        "src/a.py",
        "06_Handoffs/交接_King.md",
        "docs/handoff_notes.md",
        "README.md",  # md but no handoff prefix
        "交接_skip.txt",  # wrong extension
    ]
    out = hrg._filter_handoff_files(files, prefixes)
    assert "06_Handoffs/交接_King.md" in out
    assert "docs/handoff_notes.md" in out
    assert "README.md" not in out
    assert "交接_skip.txt" not in out


def test_record_and_detect_block_state(isolated_state):
    assert hrg._already_blocked("sx") is False
    hrg._record_block("sx")
    assert hrg._already_blocked("sx") is True
    # Different session id not affected
    assert hrg._already_blocked("other") is False


def test_cooldown_expires(isolated_state, monkeypatch):
    # Write state with timestamp far in the past
    os.makedirs(os.path.dirname(isolated_state), exist_ok=True)
    with open(isolated_state, "w", encoding="utf-8") as f:
        json.dump(
            {"session_id": "sy", "ts": time.time() - (hrg._BLOCK_COOLDOWN_S + 60)},
            f,
        )
    assert hrg._already_blocked("sy") is False


def test_git_runner_timeout_returns_empty(monkeypatch, tmp_path):
    def _boom(*a, **kw):
        raise TimeoutError("boom")
    monkeypatch.setattr("subprocess.run", _boom)
    assert hrg._run_git(["git", "status"], str(tmp_path)) == ""


def test_git_session_changed_files_dedup(monkeypatch, tmp_path):
    # Simulate three git invocations returning overlapping files.
    outputs = iter([
        "a.py\nb.py\n",           # diff HEAD
        "b.py\nc.py\n",           # diff --cached
        "\nc.py\nd.py\n",         # log (leading blank from pretty=format:)
    ])

    def _fake_run_git(args, project_dir):
        return next(outputs)

    monkeypatch.setattr(hrg, "_run_git", _fake_run_git)
    out = hrg._git_session_changed_files(str(tmp_path))
    assert out == ["a.py", "b.py", "c.py", "d.py"]


def test_git_session_passes_quotepath_false(monkeypatch, tmp_path):
    # Regression: without -c core.quotepath=false, git escapes CJK filenames
    # to octal (e.g. "\\344\\272\\244..."), and _filter_handoff_files then
    # fails to basename-match "交接_" prefix → infinite stop-block loop.
    # Every git invocation must carry the flag.
    calls: list[list[str]] = []

    def _fake_run_git(args, project_dir):
        calls.append(list(args))
        return ""

    monkeypatch.setattr(hrg, "_run_git", _fake_run_git)
    hrg._git_session_changed_files(str(tmp_path))

    assert len(calls) == 3  # diff HEAD, diff --cached, log
    for args in calls:
        assert args[0] == "git"
        # Flag must come BEFORE the git subcommand for -c to take effect.
        assert "-c" in args
        assert "core.quotepath=false" in args
        c_idx = args.index("-c")
        assert args[c_idx + 1] == "core.quotepath=false"
        subcommand_idx = next(
            i for i, a in enumerate(args)
            if i > 0 and a in {"diff", "log", "status", "show"}
        )
        assert c_idx < subcommand_idx


def test_run_git_forces_utf8_encoding(monkeypatch, tmp_path):
    # Regression: on Windows the default subprocess.run encoding is cp936/gbk
    # which mangles git's UTF-8 filename output. _run_git must pin encoding
    # explicitly so CJK handoff files survive the round-trip.
    captured: dict[str, object] = {}

    class _Result:
        stdout = "ok\n"

    def _fake_run(*args, **kwargs):
        captured.update(kwargs)
        return _Result()

    monkeypatch.setattr("subprocess.run", _fake_run)
    out = hrg._run_git(["git", "status"], str(tmp_path))
    assert out == "ok\n"
    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"
    assert captured.get("text") is True


# ── Competition Mode Bypass ──────────────────────────────────


def test_competition_mode_bypasses_handoff_require(
    isolated_state, stub_config, monkeypatch,
):
    """Competition mode short-circuits the handoff-required guard.

    Even when source-file count would normally trigger a block,
    competition mode returns None so the session can stop freely.
    """
    files = ["a.py", "b.py", "c.py", "d.py", "e.py", "f.py"]
    _patch_git(monkeypatch, files)

    from cc_cortex import handoff_engine
    monkeypatch.setattr(
        handoff_engine, "get_handoff_mode", lambda: "competition",
    )

    assert hrg.on_stop({"session_id": "s-comp"}) is None


def test_competition_mode_does_not_record_block_state(
    isolated_state, stub_config, monkeypatch,
):
    """Competition bypass must not write the circuit-breaker state file.

    Skipping the write keeps the breaker clean if the user later
    switches back to phase / save-token mode within the same session.
    """
    files = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    _patch_git(monkeypatch, files)

    from cc_cortex import handoff_engine
    monkeypatch.setattr(
        handoff_engine, "get_handoff_mode", lambda: "competition",
    )

    assert hrg.on_stop({"session_id": "s-comp-state"}) is None
    # Block state file must not exist after competition bypass.
    assert not os.path.isfile(str(isolated_state))


def test_full_mode_still_blocks_when_no_handoff(
    isolated_state, stub_config, monkeypatch,
):
    """Regression: 'full' mode does NOT receive the competition bypass.

    Full mode keeps the existing handoff-required behaviour. Only
    competition mode short-circuits this guard.
    """
    files = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    _patch_git(monkeypatch, files)

    from cc_cortex import handoff_engine
    monkeypatch.setattr(
        handoff_engine, "get_handoff_mode", lambda: "full",
    )

    result = hrg.on_stop({"session_id": "s-full-still-blocks"})
    assert result is not None
    assert result.startswith("HANDOFF_REQUIRED_BLOCK:")


# ── Structural Gate (1.18.1) ─────────────────────────────────
#
# Second-layer gate: a handoff file in git diff is not enough — the
# diff must also show structural content (status markers, next_step,
# section headers, commit hashes, doc links). Frontmatter-only
# `last_updated:` bumps no longer bypass the guard.
# See feedback_handoff_guard_too_lenient.md.


def _patch_added_lines(monkeypatch, per_file: dict[str, list[str]]):
    """Replace _git_added_lines to return fixed added lines per path."""
    def _fake(project_dir, path):
        return list(per_file.get(path, []))
    monkeypatch.setattr(hrg, "_git_added_lines", _fake)


def _with_handoff(files_without_handoff, handoff_path):
    return list(files_without_handoff) + [handoff_path]


def test_structural_gate_passes_rich_handoff(
    isolated_state, stub_config, monkeypatch,
):
    """20 added lines with ✅ + next_step signals → passes second gate."""
    handoff = "06_Handoffs/king/交接_King.md"
    files = _with_handoff(
        ["src/a.py", "src/b.py", "src/c.py", "src/d.py"], handoff,
    )
    _patch_git(monkeypatch, files)
    # 20 added lines with ✅ marker on one line and next_step on another.
    added = ["+prose line " + str(i) for i in range(18)]
    added += ["+- ✅ Session done", "+next_step: continue T2"]
    _patch_added_lines(monkeypatch, {handoff: added})
    assert hrg.on_stop({"session_id": "s-rich"}) is None


def test_structural_gate_blocks_frontmatter_only(
    isolated_state, stub_config, monkeypatch,
):
    """Only `last_updated:` bumped (1 line, no signals) → BLOCK."""
    handoff = "06_Handoffs/king/交接_King.md"
    files = _with_handoff(
        ["src/a.py", "src/b.py", "src/c.py", "src/d.py"], handoff,
    )
    _patch_git(monkeypatch, files)
    _patch_added_lines(
        monkeypatch, {handoff: ["+last_updated: 2026-04-16"]},
    )
    result = hrg.on_stop({"session_id": "s-frontmatter"})
    assert result is not None
    assert result.startswith("HANDOFF_REQUIRED_BLOCK:")
    assert "structure incomplete" in result


def test_structural_gate_blocks_insufficient_lines(
    isolated_state, stub_config, monkeypatch,
):
    """5 added lines with ✅ signal → still BLOCK (lines < 10)."""
    handoff = "06_Handoffs/king/交接_King.md"
    files = _with_handoff(
        ["src/a.py", "src/b.py", "src/c.py", "src/d.py"], handoff,
    )
    _patch_git(monkeypatch, files)
    added = [
        "+- ✅ done",
        "+- ⬜ pending",
        "+next_step: foo",
        "+line 4",
        "+line 5",
    ]
    _patch_added_lines(monkeypatch, {handoff: added})
    result = hrg.on_stop({"session_id": "s-short"})
    assert result is not None
    assert result.startswith("HANDOFF_REQUIRED_BLOCK:")
    assert "only 5 added line" in result


def test_structural_gate_blocks_no_signals(
    isolated_state, stub_config, monkeypatch,
):
    """20 added lines but pure prose, no structural signals → BLOCK."""
    handoff = "06_Handoffs/king/交接_King.md"
    files = _with_handoff(
        ["src/a.py", "src/b.py", "src/c.py", "src/d.py"], handoff,
    )
    _patch_git(monkeypatch, files)
    added = ["+this is narrative line " + str(i) for i in range(20)]
    _patch_added_lines(monkeypatch, {handoff: added})
    result = hrg.on_stop({"session_id": "s-prose"})
    assert result is not None
    assert result.startswith("HANDOFF_REQUIRED_BLOCK:")
    assert "distinct structural signal" in result


def test_structural_gate_bypassed_by_minimal_env(
    isolated_state, stub_config, monkeypatch,
):
    """CC_CORTEX_HANDOFF_MINIMAL=1 + frontmatter-only → pass."""
    handoff = "06_Handoffs/king/交接_King.md"
    files = _with_handoff(
        ["src/a.py", "src/b.py", "src/c.py", "src/d.py"], handoff,
    )
    _patch_git(monkeypatch, files)
    _patch_added_lines(
        monkeypatch, {handoff: ["+last_updated: 2026-04-16"]},
    )
    monkeypatch.setenv("CC_CORTEX_HANDOFF_MINIMAL", "1")
    assert hrg.on_stop({"session_id": "s-minimal-env"}) is None


def test_structural_gate_bypassed_by_feature_config(
    isolated_state, stub_config, monkeypatch,
):
    """structural_gate_enabled=False + frontmatter-only → pass."""
    stub_config["cfg"]._structural_gate_enabled = False
    handoff = "06_Handoffs/king/交接_King.md"
    files = _with_handoff(
        ["src/a.py", "src/b.py", "src/c.py", "src/d.py"], handoff,
    )
    _patch_git(monkeypatch, files)
    _patch_added_lines(
        monkeypatch, {handoff: ["+last_updated: 2026-04-16"]},
    )
    assert hrg.on_stop({"session_id": "s-cfg-disabled"}) is None


def test_structural_gate_counts_signals_across_patterns(
    isolated_state, stub_config, monkeypatch,
):
    """✅ + next_step = 2 distinct patterns → pass (with 10+ lines)."""
    handoff = "06_Handoffs/king/交接_King.md"
    files = _with_handoff(
        ["src/a.py", "src/b.py", "src/c.py", "src/d.py"], handoff,
    )
    _patch_git(monkeypatch, files)
    added = ["+filler " + str(i) for i in range(10)]
    added += ["+- ✅ done", "+next_step: roll"]
    _patch_added_lines(monkeypatch, {handoff: added})
    assert hrg.on_stop({"session_id": "s-two-patterns"}) is None


def test_structural_gate_rejects_duplicate_signal_same_pattern(
    isolated_state, stub_config, monkeypatch,
):
    """Two ✅ lines = 1 distinct pattern → BLOCK (signals < 2)."""
    handoff = "06_Handoffs/king/交接_King.md"
    files = _with_handoff(
        ["src/a.py", "src/b.py", "src/c.py", "src/d.py"], handoff,
    )
    _patch_git(monkeypatch, files)
    added = ["+filler " + str(i) for i in range(10)]
    added += ["+- ✅ task 1", "+- ✅ task 2"]  # same pattern twice
    _patch_added_lines(monkeypatch, {handoff: added})
    result = hrg.on_stop({"session_id": "s-dup-signal"})
    assert result is not None
    assert result.startswith("HANDOFF_REQUIRED_BLOCK:")
    assert "distinct structural signal" in result


def test_structural_gate_counts_multiple_handoff_files(
    isolated_state, stub_config, monkeypatch,
):
    """Lines and signals accumulate across multiple handoff files."""
    h1 = "06_Handoffs/king/交接_King.md"
    h2 = "06_Handoffs/cc-cortex/交接_CCC.md"
    files = list(
        ["src/a.py", "src/b.py", "src/c.py", "src/d.py", h1, h2]
    )
    _patch_git(monkeypatch, files)
    # Each file alone would fail (5 lines, 1 signal). Together they pass.
    _patch_added_lines(monkeypatch, {
        h1: ["+- ✅ done"] + ["+filler a " + str(i) for i in range(4)],
        h2: ["+next_step: go"] + ["+filler b " + str(i) for i in range(4)],
    })
    # 10 total lines, 2 distinct signals → should pass.
    assert hrg.on_stop({"session_id": "s-multi"}) is None


def test_has_structural_update_helper_returns_reasons():
    """_has_structural_update returns (False, [hints...]) when thin."""
    # Stub _git_added_lines via direct call substitute
    calls: dict[str, list[str]] = {"p": ["+only one line"]}

    def _fake(project_dir, path):
        return calls.get(path, [])

    import cc_cortex.handoff_required_guard as mod
    saved = mod._git_added_lines
    try:
        mod._git_added_lines = _fake  # type: ignore[assignment]
        ok, reasons = mod._has_structural_update(
            ".", ["p"], min_added_lines=10, min_signal_hits=2,
        )
        assert ok is False
        assert any("added line" in r for r in reasons)
        assert any("structural signal" in r for r in reasons)
    finally:
        mod._git_added_lines = saved  # type: ignore[assignment]
