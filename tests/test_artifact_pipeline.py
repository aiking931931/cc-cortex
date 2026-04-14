"""Tests for cc_cortex.delivery.artifact_pipeline — multi-type WIREDO verification.

Covers:
- CheckState / CheckResult / TypeReport / ArtifactReport data models
- collect_artifacts() grouping by asset type
- detect_media_tasks() API pattern detection
- Per-type validators (code, image, video, audio, document)
- ArtifactPipeline.run() and run_and_gate()
- Block decision logic (configurable dimensions)
- Mechanical checks (file exists, naming, path)
- Sentinel media tracking (_track_media_artifacts)
"""

from __future__ import annotations

from pathlib import Path

from cc_cortex.asset_validator import AssetType, WiredoDimension
from cc_cortex.delivery.artifact_pipeline import (
    ArtifactPipeline,
    ArtifactReport,
    CheckResult,
    CheckState,
    TypeReport,
    _check_md_structure,
    _mechanical_file_check,
    _mechanical_naming_check,
    _mechanical_path_check,
    collect_artifacts,
    detect_media_tasks,
)

# ── Data Model Tests ──────────────────────────────────────────


class TestCheckState:
    def test_values(self):
        assert CheckState.PASS.value == "pass"
        assert CheckState.FAIL.value == "fail"
        assert CheckState.SKIP.value == "skip"


class TestCheckResult:
    def test_icons(self):
        assert CheckResult(WiredoDimension.WIRED, CheckState.PASS).icon == "✅"
        assert CheckResult(WiredoDimension.WIRED, CheckState.FAIL).icon == "❌"
        assert CheckResult(WiredoDimension.WIRED, CheckState.SKIP).icon == "⏭"

    def test_evidence(self):
        r = CheckResult(WiredoDimension.DEFENDED, CheckState.FAIL, "No tests run")
        assert r.evidence == "No tests run"
        assert r.dimension == WiredoDimension.DEFENDED


class TestTypeReport:
    def _make_report(self, states: list[CheckState]) -> TypeReport:
        dims = list(WiredoDimension)
        checks = [CheckResult(dims[i], s, f"ev{i}") for i, s in enumerate(states)]
        return TypeReport(asset_type=AssetType.CODE, files=["a.py"], checks=checks)

    def test_all_pass(self):
        r = self._make_report([CheckState.PASS] * 6)
        assert r.passed is True
        assert r.score == 100
        assert r.failed_dims == []

    def test_one_fail(self):
        states = [CheckState.PASS] * 5 + [CheckState.FAIL]
        r = self._make_report(states)
        assert r.passed is False
        assert r.score == 83  # 5/6
        assert len(r.failed_dims) == 1

    def test_skip_not_counted(self):
        states = [CheckState.PASS, CheckState.PASS, CheckState.SKIP,
                  CheckState.SKIP, CheckState.SKIP, CheckState.SKIP]
        r = self._make_report(states)
        assert r.passed is True
        assert r.score == 100  # 2/2 scorable

    def test_all_skip(self):
        r = self._make_report([CheckState.SKIP] * 6)
        assert r.passed is True
        assert r.score == 100

    def test_to_table(self):
        r = self._make_report([CheckState.PASS] * 6)
        table = r.to_table()
        assert "CODE" in table
        assert "score: 100%" in table
        assert "✅" in table


class TestArtifactReport:
    def test_empty_report(self):
        r = ArtifactReport()
        assert r.all_passed is True
        assert r.block_dims == {}
        assert r.should_block() is False

    def test_should_block_default_defended(self):
        """Default: only D(defended) dimension blocks."""
        tr = TypeReport(asset_type=AssetType.CODE, files=["a.py"])
        tr.checks = [
            CheckResult(WiredoDimension.WIRED, CheckState.FAIL, "orphan"),
            CheckResult(WiredoDimension.DEFENDED, CheckState.PASS, "ok"),
        ]
        r = ArtifactReport(type_reports=[tr])
        # W failed but D passed → no block (default blocks only D)
        assert r.should_block() is False

    def test_should_block_defended_fail(self):
        tr = TypeReport(asset_type=AssetType.CODE, files=["a.py"])
        tr.checks = [
            CheckResult(WiredoDimension.DEFENDED, CheckState.FAIL, "no tests"),
        ]
        r = ArtifactReport(type_reports=[tr])
        assert r.should_block() is True

    def test_should_block_custom_dimensions(self):
        tr = TypeReport(asset_type=AssetType.IMAGE, files=["x.png"])
        tr.checks = [
            CheckResult(WiredoDimension.WIRED, CheckState.FAIL, "orphan"),
        ]
        r = ArtifactReport(type_reports=[tr])
        # Custom: block on W too
        assert r.should_block(["defended", "wired"]) is True

    def test_format_block_reason(self):
        tr = TypeReport(asset_type=AssetType.CODE, files=["a.py"])
        tr.checks = [
            CheckResult(WiredoDimension.DEFENDED, CheckState.FAIL, "no tests"),
            CheckResult(WiredoDimension.OBSERVABLE, CheckState.FAIL, "no logs"),
        ]
        r = ArtifactReport(type_reports=[tr])
        reason = r.format_block_reason()
        assert "code" in reason
        assert "defended" in reason

    def test_to_stderr(self):
        tr = TypeReport(asset_type=AssetType.IMAGE, files=["x.png"])
        tr.checks = [
            CheckResult(WiredoDimension.DEFENDED, CheckState.PASS, "ok"),
        ]
        r = ArtifactReport(type_reports=[tr])
        stderr = r.to_stderr()
        assert "image" in stderr
        assert "✅" in stderr

    def test_untracked_media_tasks(self):
        r = ArtifactReport(media_tasks_untracked=["video", "audio"])
        stderr = r.to_stderr()
        assert "video" in stderr
        assert "audio" in stderr

    def test_multi_type_report(self):
        tr1 = TypeReport(asset_type=AssetType.CODE, files=["a.py"])
        tr1.checks = [CheckResult(WiredoDimension.DEFENDED, CheckState.PASS, "ok")]
        tr2 = TypeReport(asset_type=AssetType.IMAGE, files=["b.png"])
        tr2.checks = [CheckResult(WiredoDimension.DEFENDED, CheckState.FAIL, "empty")]
        r = ArtifactReport(type_reports=[tr1, tr2])
        assert r.all_passed is False
        assert r.should_block() is True  # image D failed


# ── Mechanical Check Tests ────────────────────────────────────


class TestMechanicalChecks:
    def test_file_check_exists(self, tmp_path: Path):
        f = tmp_path / "test.png"
        f.write_bytes(b"\x89PNG" + b"\x00" * 1000)
        ok, ev = _mechanical_file_check(str(f))
        assert ok is True
        assert "1,004 bytes" in ev

    def test_file_check_missing(self, tmp_path: Path):
        ok, ev = _mechanical_file_check(str(tmp_path / "nope.png"))
        assert ok is False
        assert "not found" in ev.lower()

    def test_file_check_empty(self, tmp_path: Path):
        f = tmp_path / "empty.png"
        f.write_bytes(b"")
        ok, ev = _mechanical_file_check(str(f))
        assert ok is False
        assert "Empty" in ev

    def test_file_check_tmp_dir(self, tmp_path: Path):
        d = tmp_path / "tmp"
        d.mkdir()
        f = d / "x.png"
        f.write_bytes(b"\x00" * 100)
        ok, ev = _mechanical_file_check(str(f))
        assert ok is False
        assert "tmp/" in ev.lower()

    def test_naming_check_good(self):
        ok, _ = _mechanical_naming_check("my-file_01.png")
        assert ok is True

    def test_naming_check_exempt(self):
        ok, _ = _mechanical_naming_check("avatar.png")
        assert ok is True

    def test_naming_check_spaces(self):
        ok, _ = _mechanical_naming_check("my file (1).png")
        assert ok is False

    def test_path_check_managed(self, tmp_path: Path):
        ok, _ = _mechanical_path_check(
            str(tmp_path / "media" / "x.png"), str(tmp_path),
        )
        assert ok is True

    def test_path_check_root(self, tmp_path: Path):
        ok, _ = _mechanical_path_check(
            str(tmp_path / "orphan.png"), str(tmp_path),
        )
        assert ok is False


# ── Document Structure Check ─────────────────────────────────


class TestMdStructure:
    def test_frontmatter(self, tmp_path: Path):
        f = tmp_path / "doc.md"
        f.write_text("---\ntitle: test\n---\n# Hello", encoding="utf-8")
        ok, _ = _check_md_structure(str(f))
        assert ok is True

    def test_h1_heading(self, tmp_path: Path):
        f = tmp_path / "doc.md"
        f.write_text("# Hello World\n\nContent", encoding="utf-8")
        ok, _ = _check_md_structure(str(f))
        assert ok is True

    def test_no_structure(self, tmp_path: Path):
        f = tmp_path / "doc.md"
        f.write_text("just some text without heading", encoding="utf-8")
        ok, ev = _check_md_structure(str(f))
        assert ok is False
        assert "no frontmatter" in ev


# ── Collect Artifacts Tests ──────────────────────────────────


class TestCollectArtifacts:
    def _setup_sentinel(self, tmp_path: Path, edited: list[str],
                        generated: list[str] | None = None) -> str:
        # Sentinel reads via StateStore which hashes session_id with
        # blake2b. Writing through the public API means the test never
        # has to know the on-disk filename — the previous hardcoded
        # `test_ses.json` path silently broke when state_store switched
        # from 8-char truncation to 16-hex blake2b digests.
        from cc_cortex.core.state_store import StateStore
        cache = tmp_path / "cache"
        cache.mkdir()
        state = {"edited_files": edited, "calls": []}
        if generated:
            state["generated_artifacts"] = generated
        StateStore(str(cache / "sentinel")).write(
            "sentinel", "test_session", state,
        )
        return str(cache)

    def test_groups_by_type(self, tmp_path: Path):
        py = tmp_path / "a.py"
        py.write_text("x = 1")
        md = tmp_path / "b.md"
        md.write_text("# doc")
        png = tmp_path / "c.png"
        png.write_bytes(b"\x89PNG" + b"\x00" * 100)

        cache = self._setup_sentinel(tmp_path, [str(py), str(md), str(png)])
        groups = collect_artifacts(cache, "test_session")

        assert AssetType.CODE in groups
        assert AssetType.DOCUMENT in groups
        assert AssetType.IMAGE in groups

    def test_skips_missing_files(self, tmp_path: Path):
        cache = self._setup_sentinel(tmp_path, ["/nonexistent/x.py"])
        groups = collect_artifacts(cache, "test_session")
        assert len(groups) == 0

    def test_includes_generated(self, tmp_path: Path):
        img = tmp_path / "gen.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 100)
        cache = self._setup_sentinel(tmp_path, [], [str(img)])
        groups = collect_artifacts(cache, "test_session")
        assert AssetType.IMAGE in groups

    def test_deduplicates(self, tmp_path: Path):
        py = tmp_path / "a.py"
        py.write_text("x = 1")
        cache = self._setup_sentinel(tmp_path, [str(py)], [str(py)])
        groups = collect_artifacts(cache, "test_session")
        assert len(groups[AssetType.CODE]) == 1


# ── Detect Media Tasks Tests ─────────────────────────────────


class TestDetectMediaTasks:
    def _setup(self, tmp_path: Path, calls: list[dict]) -> str:
        from cc_cortex.core.state_store import StateStore
        cache = tmp_path / "cache"
        cache.mkdir()
        state = {"calls": calls, "edited_files": []}
        StateStore(str(cache / "sentinel")).write(
            "sentinel", "test_session", state,
        )
        return str(cache)

    def test_detects_image_api(self, tmp_path: Path):
        calls = [{"tool": "Bash", "bash_pfx": "python gen.py --model fal-ai/flux"}]
        cache = self._setup(tmp_path, calls)
        tasks = detect_media_tasks(cache, "test_session")
        assert "image" in tasks

    def test_detects_video_api(self, tmp_path: Path):
        calls = [{"tool": "Bash", "bash_pfx": "python gen.py --api kling"}]
        cache = self._setup(tmp_path, calls)
        tasks = detect_media_tasks(cache, "test_session")
        assert "video" in tasks

    def test_detects_audio_api(self, tmp_path: Path):
        calls = [{"tool": "Bash", "bash_pfx": "python gen.py elevenlabs tts"}]
        cache = self._setup(tmp_path, calls)
        tasks = detect_media_tasks(cache, "test_session")
        assert "audio" in tasks

    def test_ignores_non_bash(self, tmp_path: Path):
        calls = [{"tool": "Edit", "bash_pfx": "fal-ai/flux"}]
        cache = self._setup(tmp_path, calls)
        tasks = detect_media_tasks(cache, "test_session")
        assert tasks == []

    def test_multiple_types(self, tmp_path: Path):
        calls = [
            {"tool": "Bash", "bash_pfx": "python img.py fal-ai/flux"},
            {"tool": "Bash", "bash_pfx": "python audio.py suno"},
        ]
        cache = self._setup(tmp_path, calls)
        tasks = detect_media_tasks(cache, "test_session")
        assert "image" in tasks
        assert "audio" in tasks


# ── Validator Tests ───────────────────────────────────────────


class TestValidateDocument:
    def test_valid_md(self, tmp_path: Path):
        f = tmp_path / "doc.md"
        f.write_text("# Title\n\nContent here", encoding="utf-8")

        from cc_cortex.delivery.artifact_pipeline import _validate_document
        report = _validate_document([str(f)], str(tmp_path))

        assert report.asset_type == AssetType.DOCUMENT
        assert report.passed is True
        assert report.score == 100

    def test_missing_structure(self, tmp_path: Path):
        f = tmp_path / "bad.md"
        f.write_text("no heading, no frontmatter", encoding="utf-8")

        from cc_cortex.delivery.artifact_pipeline import _validate_document
        report = _validate_document([str(f)], str(tmp_path))

        inherited = [c for c in report.checks
                     if c.dimension == WiredoDimension.INHERITED]
        assert inherited[0].state == CheckState.FAIL

    def test_oversized_file(self, tmp_path: Path):
        f = tmp_path / "huge.md"
        f.write_text("# OK\n" + "x" * 11_000_000, encoding="utf-8")

        from cc_cortex.delivery.artifact_pipeline import _validate_document
        report = _validate_document([str(f)], str(tmp_path))

        responsive = [c for c in report.checks
                      if c.dimension == WiredoDimension.RESPONSIVE]
        assert responsive[0].state == CheckState.FAIL


class TestValidateImage:
    def test_valid_image(self, tmp_path: Path):
        f = tmp_path / "media" / "photo.png"
        f.parent.mkdir()
        f.write_bytes(b"\x89PNG" + b"\x00" * 2000)

        from cc_cortex.delivery.artifact_pipeline import _validate_image
        report = _validate_image([str(f)], str(tmp_path))

        assert report.asset_type == AssetType.IMAGE
        # D should pass (file exists, non-zero)
        defended = [c for c in report.checks
                    if c.dimension == WiredoDimension.DEFENDED]
        assert defended[0].state == CheckState.PASS

    def test_empty_image(self, tmp_path: Path):
        f = tmp_path / "media" / "bad.png"
        f.parent.mkdir(exist_ok=True)
        f.write_bytes(b"")

        from cc_cortex.delivery.artifact_pipeline import _validate_image
        report = _validate_image([str(f)], str(tmp_path))

        defended = [c for c in report.checks
                    if c.dimension == WiredoDimension.DEFENDED]
        assert defended[0].state == CheckState.FAIL

    def test_orphan_image(self, tmp_path: Path):
        f = tmp_path / "random.png"
        f.write_bytes(b"\x89PNG" + b"\x00" * 2000)

        from cc_cortex.delivery.artifact_pipeline import _validate_image
        report = _validate_image([str(f)], str(tmp_path))

        wired = [c for c in report.checks
                 if c.dimension == WiredoDimension.WIRED]
        assert wired[0].state == CheckState.FAIL


class TestValidateVideo:
    def test_valid_video_file(self, tmp_path: Path):
        f = tmp_path / "media" / "clip.mp4"
        f.parent.mkdir()
        f.write_bytes(b"\x00" * 5000)

        from cc_cortex.delivery.artifact_pipeline import _validate_video
        report = _validate_video([str(f)], str(tmp_path))

        assert report.asset_type == AssetType.VIDEO
        defended = [c for c in report.checks
                    if c.dimension == WiredoDimension.DEFENDED]
        assert defended[0].state == CheckState.PASS


class TestValidateAudio:
    def test_valid_audio_file(self, tmp_path: Path):
        f = tmp_path / "media" / "voice.mp3"
        f.parent.mkdir()
        f.write_bytes(b"\xff\xfb\x90" + b"\x00" * 5000)

        from cc_cortex.delivery.artifact_pipeline import _validate_audio
        report = _validate_audio([str(f)], str(tmp_path))

        assert report.asset_type == AssetType.AUDIO
        defended = [c for c in report.checks
                    if c.dimension == WiredoDimension.DEFENDED]
        assert defended[0].state == CheckState.PASS


# ── ArtifactPipeline Integration Tests ────────────────────────


class TestArtifactPipeline:
    def _setup_state(self, tmp_path: Path, edited: list[str],
                     calls: list[dict] | None = None,
                     generated: list[str] | None = None) -> str:
        from cc_cortex.core.state_store import StateStore
        cache = tmp_path / "cache"
        cache.mkdir(exist_ok=True)
        state: dict = {"edited_files": edited, "calls": calls or []}
        if generated:
            state["generated_artifacts"] = generated
        StateStore(str(cache / "sentinel")).write(
            "sentinel", "test_sess", state,
        )
        return str(cache)

    def test_empty_session(self, tmp_path: Path):
        cache = self._setup_state(tmp_path, [])
        pipeline = ArtifactPipeline(
            cache_dir=cache, session_id="test_sess",
            workspace=str(tmp_path),
        )
        report = pipeline.run()
        assert report.all_passed is True
        assert len(report.type_reports) == 0

    def test_code_only(self, tmp_path: Path):
        py = tmp_path / "src" / "mod.py"
        py.parent.mkdir()
        py.write_text("def hello(): pass\n")
        cache = self._setup_state(tmp_path, [str(py)])

        pipeline = ArtifactPipeline(
            cache_dir=cache, session_id="test_sess",
            workspace=str(tmp_path),
        )
        report = pipeline.run()
        assert len(report.type_reports) == 1
        assert report.type_reports[0].asset_type == AssetType.CODE

    def test_mixed_types(self, tmp_path: Path):
        py = tmp_path / "src" / "mod.py"
        py.parent.mkdir()
        py.write_text("x = 1\n")
        md = tmp_path / "docs" / "readme.md"
        md.parent.mkdir()
        md.write_text("# README\n")
        png = tmp_path / "media" / "logo.png"
        png.parent.mkdir()
        png.write_bytes(b"\x89PNG" + b"\x00" * 2000)

        cache = self._setup_state(tmp_path, [str(py), str(md), str(png)])
        pipeline = ArtifactPipeline(
            cache_dir=cache, session_id="test_sess",
            workspace=str(tmp_path),
        )
        report = pipeline.run()
        types = {r.asset_type for r in report.type_reports}
        assert AssetType.CODE in types
        assert AssetType.DOCUMENT in types
        assert AssetType.IMAGE in types

    def test_untracked_media_tasks(self, tmp_path: Path):
        calls = [{"tool": "Bash", "bash_pfx": "python gen.py fal-ai/flux"}]
        cache = self._setup_state(tmp_path, [], calls)

        pipeline = ArtifactPipeline(
            cache_dir=cache, session_id="test_sess",
            workspace=str(tmp_path),
        )
        report = pipeline.run()
        assert "image" in report.media_tasks_untracked

    def test_run_and_gate_no_block(self, tmp_path: Path):
        md = tmp_path / "docs" / "test.md"
        md.parent.mkdir()
        md.write_text("# Test\n")
        cache = self._setup_state(tmp_path, [str(md)])

        pipeline = ArtifactPipeline(
            cache_dir=cache, session_id="test_sess",
            workspace=str(tmp_path),
        )
        should_block, reason = pipeline.run_and_gate()
        assert should_block is False

    def test_run_and_gate_block_on_defended(self, tmp_path: Path):
        # Empty file → D(defended) fails
        md = tmp_path / "docs" / "empty.md"
        md.parent.mkdir()
        md.write_bytes(b"")
        cache = self._setup_state(tmp_path, [str(md)])

        pipeline = ArtifactPipeline(
            cache_dir=cache, session_id="test_sess",
            workspace=str(tmp_path),
        )
        should_block, reason = pipeline.run_and_gate()
        assert should_block is True
        assert "document" in reason

    def test_custom_block_dimensions(self, tmp_path: Path):
        # File in root → W(wired) fails for image
        png = tmp_path / "orphan.png"
        png.write_bytes(b"\x89PNG" + b"\x00" * 2000)
        cache = self._setup_state(tmp_path, [str(png)])

        # Default: only D blocks → no block
        pipeline = ArtifactPipeline(
            cache_dir=cache, session_id="test_sess",
            workspace=str(tmp_path),
        )
        should_block, _ = pipeline.run_and_gate()
        assert should_block is False

        # Custom: W also blocks
        pipeline2 = ArtifactPipeline(
            cache_dir=cache, session_id="test_sess",
            workspace=str(tmp_path),
            block_dimensions=["defended", "wired"],
        )
        should_block2, reason2 = pipeline2.run_and_gate()
        assert should_block2 is True
        assert "wired" in reason2


# ── Sentinel Media Tracking Tests ─────────────────────────────


class TestSentinelMediaTracking:
    def test_track_image_api(self):
        from cc_cortex.sentinel import _track_media_artifacts

        state: dict = {}
        tool_input = {"command": "python gen.py --model fal-ai/flux-pro"}
        _track_media_artifacts(state, tool_input, "")
        assert "image" in state.get("media_tasks", [])

    def test_track_audio_api(self):
        from cc_cortex.sentinel import _track_media_artifacts

        state: dict = {}
        tool_input = {"command": "python gen.py elevenlabs tts"}
        _track_media_artifacts(state, tool_input, "")
        assert "audio" in state.get("media_tasks", [])

    def test_extract_file_path(self, tmp_path: Path):
        from cc_cortex.sentinel import _track_media_artifacts

        img = tmp_path / "output.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 100)

        state: dict = {}
        tool_input = {"command": "python gen.py"}
        result = f"Generated image saved to {img}"
        _track_media_artifacts(state, tool_input, result)
        assert len(state.get("generated_artifacts", [])) == 1

    def test_no_track_nonexistent(self):
        from cc_cortex.sentinel import _track_media_artifacts

        state: dict = {}
        tool_input = {"command": "python gen.py"}
        result = "saved to /nonexistent/path/image.png"
        _track_media_artifacts(state, tool_input, result)
        assert len(state.get("generated_artifacts", [])) == 0

    def test_dedup(self, tmp_path: Path):
        from cc_cortex.sentinel import _track_media_artifacts

        img = tmp_path / "out.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 100)

        state: dict = {}
        tool_input = {"command": "python gen.py"}
        result = f"saved to {img}"
        _track_media_artifacts(state, tool_input, result)
        _track_media_artifacts(state, tool_input, result)
        assert len(state.get("generated_artifacts", [])) == 1

    def test_media_task_dedup(self):
        from cc_cortex.sentinel import _track_media_artifacts

        state: dict = {}
        tool_input = {"command": "python gen.py fal-ai/flux"}
        _track_media_artifacts(state, tool_input, "")
        _track_media_artifacts(state, tool_input, "")
        assert state["media_tasks"].count("image") == 1
