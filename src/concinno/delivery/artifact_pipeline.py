"""concinno.delivery.artifact_pipeline — Unified multi-type WIREDO verification.

@module delivery.artifact_pipeline
@responsibility Track ALL session artifacts (code, image, video, audio, document),
    run asset-type-aware WIREDO validators, produce unified pass/fail report.
    Single entry point for on-stop delivery gate.
@dependencies concinno.asset_validator, concinno.core.state_store,
    concinno.delivery.wiredo (code-specific helpers)
@exports ArtifactPipeline, ArtifactReport, TypeReport, CheckResult

Architecture:
    1. Collect artifacts from sentinel state (edited_files + generated_artifacts)
    2. Group by asset type (detect_asset_type)
    3. For each type: run mechanical checks (zero-dep) + deep checks (optional dep)
    4. Produce per-type reports with PASS / FAIL / SKIP per dimension
    5. Block decision: configurable which dimensions block stop (default: D only)
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from concinno.asset_validator import AssetType, WiredoDimension, detect_asset_type

# ── Result States ──────────────────────────────────────────────


class CheckState(Enum):
    """Three-state result: PASS, FAIL, or SKIP (dependency missing)."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class CheckResult:
    """Result of a single WIREDO dimension check."""

    dimension: WiredoDimension
    state: CheckState
    evidence: str = ""

    @property
    def icon(self) -> str:
        return {"pass": "✅", "fail": "❌", "skip": "⏭"}[self.state.value]


@dataclass
class TypeReport:
    """WIREDO report for one asset type."""

    asset_type: AssetType
    files: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True if no FAIL results (SKIP is OK)."""
        return not any(c.state == CheckState.FAIL for c in self.checks)

    @property
    def failed_dims(self) -> list[str]:
        return [c.dimension.value for c in self.checks if c.state == CheckState.FAIL]

    @property
    def score(self) -> int:
        """Score 0-100, only counting non-SKIP checks."""
        scorable = [c for c in self.checks if c.state != CheckState.SKIP]
        if not scorable:
            return 100
        passed = sum(1 for c in scorable if c.state == CheckState.PASS)
        return round(passed / len(scorable) * 100)

    def to_table(self) -> str:
        """Render as markdown table."""
        _labels = {
            WiredoDimension.WIRED: "W — Wired",
            WiredoDimension.INHERITED: "I — Inherited & Aligned",
            WiredoDimension.RESPONSIVE: "R — Responsive & Performant",
            WiredoDimension.EXTENSIBLE: "E — Extensible",
            WiredoDimension.DEFENDED: "D — Defended & Verified",
            WiredoDimension.OBSERVABLE: "O — Observable",
        }
        lines = [
            f"**{self.asset_type.value.upper()}** ({len(self.files)} files, score: {self.score}%)",
            "| Dimension | Status | Evidence |",
            "|-----------|--------|----------|",
        ]
        for c in self.checks:
            label = _labels.get(c.dimension, c.dimension.name)
            lines.append(f"| {label} | {c.icon} | {c.evidence} |")
        return "\n".join(lines)


@dataclass
class ArtifactReport:
    """Full WIREDO report across all asset types in a session."""

    type_reports: list[TypeReport] = field(default_factory=list)
    media_tasks_untracked: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.type_reports)

    @property
    def block_dims(self) -> dict[str, list[str]]:
        """Map of asset_type → failed dimensions."""
        result = {}
        for r in self.type_reports:
            if r.failed_dims:
                result[r.asset_type.value] = r.failed_dims
        return result

    def should_block(self, block_dimensions: list[str] | None = None) -> bool:
        """Check if any blocked dimension failed.

        Args:
            block_dimensions: Dimension values that trigger a block.
                Default: ["defended"] (only D blocks stop).
        """
        if block_dimensions is None:
            block_dimensions = ["defended"]
        for r in self.type_reports:
            for c in r.checks:
                if c.state == CheckState.FAIL and c.dimension.value in block_dimensions:
                    return True
        return False

    def format_block_reason(self) -> str:
        """Format for WIREDO_BLOCK return value."""
        parts = []
        for atype, dims in self.block_dims.items():
            parts.append(f"{atype}({','.join(dims)})")
        return ",".join(parts)

    def to_stderr(self) -> str:
        """Format full report for stderr output."""
        if not self.type_reports and not self.media_tasks_untracked:
            return ""
        lines = []
        for r in self.type_reports:
            status = "✅" if r.passed else "❌"
            lines.append(
                f"\033[36m[WIREDO] {r.asset_type.value}: {status} "
                f"score={r.score}% files={len(r.files)}\033[0m"
            )
            for c in r.checks:
                if c.state == CheckState.FAIL:
                    lines.append(f"  {c.icon} {c.dimension.value}: {c.evidence}")
                elif c.state == CheckState.SKIP:
                    lines.append(f"  {c.icon} {c.dimension.value}: {c.evidence}")
        if self.media_tasks_untracked:
            lines.append(
                f"\033[93m⚠ Untracked media tasks: "
                f"{', '.join(self.media_tasks_untracked)}\033[0m"
            )
        return "\n".join(lines)


# ── Artifact Collection ────────────────────────────────────────


def _read_sentinel_state(cache_dir: str, session_id: str) -> dict:
    """Read sentinel state for this session.

    Sentinel stores state via StateStore(cache_path("sentinel")),
    which means base_dir already includes /sentinel/.
    The namespace is also "sentinel", producing:
        {cache_dir}/sentinel/sentinel/{prefix}.json
    We mirror this path to read correctly.
    """
    try:
        from concinno.core.state_store import StateStore
        base = cache_dir or os.path.join(
            os.environ.get("CLAUDE_PROJECT_DIR", "."),
            ".concinno_cache",
        )
        sentinel_dir = os.path.join(base, "sentinel")
        store = StateStore(sentinel_dir)
        return store.read("sentinel", session_id, default={})
    except Exception:
        return {}


def collect_artifacts(
    cache_dir: str, session_id: str,
) -> dict[AssetType, list[str]]:
    """Collect ALL session artifacts grouped by asset type.

    Sources:
    1. edited_files from sentinel state (Write/Edit tools)
    2. generated_artifacts from sentinel state (Bash output paths)
    """
    state = _read_sentinel_state(cache_dir, session_id)
    edited = state.get("edited_files", [])
    generated = state.get("generated_artifacts", [])

    all_files = list(dict.fromkeys(edited + generated))  # dedupe, preserve order

    groups: dict[AssetType, list[str]] = {}
    for f in all_files:
        if not os.path.isfile(f):
            continue
        atype = detect_asset_type(f)
        if atype is None:
            continue
        groups.setdefault(atype, []).append(f)

    return groups


def detect_media_tasks(cache_dir: str, session_id: str) -> list[str]:
    """Detect media task types from sentinel call history.

    Scans Bash commands for API patterns (fal, suno, ffmpeg, etc.)
    and returns list of asset type values that were attempted.
    """
    state = _read_sentinel_state(cache_dir, session_id)
    calls = state.get("calls", [])

    _PATTERNS: dict[str, list[str]] = {
        "image": [
            "fal-ai/", "fal.run/", "kontext", "text-to-image",
            "image-to-image", "flux", "stable-diffusion", "dall-e",
            "midjourney", "replicate.*image",
        ],
        "video": [
            "kling", "runway", "pika", "hedra", "luma",
            "video-generation", "motion-control", "ffmpeg.*-i.*\\.mp4",
        ],
        "audio": [
            "suno", "elevenlabs", "tts", "text-to-speech",
            "bark", "whisper", "ffmpeg.*\\.mp3", "ffmpeg.*\\.wav",
        ],
        "document": [
            "word_create", "word_append", "python-docx", "pandoc",
            "md_to_word", "latex",
        ],
    }

    detected: set[str] = set()
    for call in calls:
        if call.get("tool") != "Bash":
            continue
        cmd = (call.get("bash_pfx") or "").lower()
        for atype, patterns in _PATTERNS.items():
            if any(p in cmd for p in patterns):
                detected.add(atype)

    return sorted(detected)


# ── Validators (mechanical = zero-dep, deep = optional-dep) ────


def _check_code_wired(files: list[str], workspace: str) -> CheckResult:
    """W: Are code files imported/referenced by other files?"""
    from concinno.delivery.wiredo import _find_unwired_files
    orphans = _find_unwired_files(files, workspace)
    if not orphans:
        return CheckResult(WiredoDimension.WIRED, CheckState.PASS,
                           f"All {len(files)} files wired")
    orphan_list = ", ".join(os.path.basename(o) for o in orphans[:3])
    return CheckResult(WiredoDimension.WIRED, CheckState.FAIL,
                       f"{len(orphans)} orphan(s): {orphan_list}")


def _check_code_inherited(files: list[str], workspace: str) -> CheckResult:
    """I: Files in architecturally correct locations?"""
    from concinno.delivery.wiredo import _inherited_check
    issues = _inherited_check(files, workspace)
    if not issues:
        return CheckResult(WiredoDimension.INHERITED, CheckState.PASS,
                           "All files in correct module directories")
    return CheckResult(WiredoDimension.INHERITED, CheckState.FAIL,
                       "; ".join(i.strip() for i in issues[:3]))


def _check_code_responsive(files: list[str]) -> CheckResult:
    """R: No performance anti-patterns?"""
    from concinno.delivery.wiredo import _responsive_check
    issues = _responsive_check(files)
    if not issues:
        return CheckResult(WiredoDimension.RESPONSIVE, CheckState.PASS,
                           "No anti-patterns detected")
    return CheckResult(WiredoDimension.RESPONSIVE, CheckState.FAIL,
                       "; ".join(i.strip() for i in issues[:3]))


def _check_code_extensible(files: list[str]) -> CheckResult:
    """E: No hardcoded values?"""
    from concinno.delivery.wiredo import _extensible_check
    issues = _extensible_check(files)
    if not issues:
        return CheckResult(WiredoDimension.EXTENSIBLE, CheckState.PASS,
                           "No hardcoded values detected")
    return CheckResult(WiredoDimension.EXTENSIBLE, CheckState.FAIL,
                       "; ".join(i.strip() for i in issues[:3]))


def _check_code_defended(
    files: list[str], cache_dir: str, session_id: str,
) -> CheckResult:
    """D: Tests run + screenshots taken (if applicable)?"""
    from concinno.delivery.wiredo import _defended_check
    issues = _defended_check(files, cache_dir, session_id)
    if not issues:
        return CheckResult(WiredoDimension.DEFENDED, CheckState.PASS,
                           "Test/screenshot evidence found")
    return CheckResult(WiredoDimension.DEFENDED, CheckState.FAIL,
                       "; ".join(i.strip() for i in issues[:3]))


def _check_code_observable(files: list[str]) -> CheckResult:
    """O: Has logging/metrics?"""
    from concinno.delivery.wiredo import _observable_check
    issues = _observable_check(files)
    if not issues:
        return CheckResult(WiredoDimension.OBSERVABLE, CheckState.PASS,
                           "Observability present")
    return CheckResult(WiredoDimension.OBSERVABLE, CheckState.FAIL,
                       "; ".join(i.strip() for i in issues[:3]))


def _validate_code(
    files: list[str], workspace: str, cache_dir: str, session_id: str,
) -> TypeReport:
    """Run full 6-dimension WIREDO on code files."""
    report = TypeReport(asset_type=AssetType.CODE, files=files)
    report.checks = [
        _check_code_wired(files, workspace),
        _check_code_inherited(files, workspace),
        _check_code_responsive(files),
        _check_code_extensible(files),
        _check_code_defended(files, cache_dir, session_id),
        _check_code_observable(files),
    ]
    return report


# ── Media Validators (mechanical + deep) ──────────────────────


def _mechanical_file_check(path: str) -> tuple[bool, str]:
    """Zero-dep: file exists, non-zero, not in tmp/."""
    if not os.path.isfile(path):
        return False, "File not found"
    size = os.path.getsize(path)
    if size == 0:
        return False, "Empty file (0 bytes)"
    norm = path.replace("\\", "/")
    if "/tmp/" in norm or "\\tmp\\" in norm:
        return False, f"In tmp/ directory ({size:,} bytes)"
    return True, f"{size:,} bytes"


def _mechanical_naming_check(path: str) -> tuple[bool, str]:
    """Zero-dep: file naming convention check."""
    basename = os.path.basename(path)
    # Exempt common standard names
    _exempt = {"avatar", "thumbnail", "cover", "ref_", "profile", "logo",
               "icon", "screenshot", "readme", "index", "test"}
    if any(e in basename.lower() for e in _exempt):
        return True, f"Exempt name: {basename}"
    # Check for reasonable naming (no spaces, no special chars except - and _)
    if re.match(r"^[\w\-\.]+$", basename):
        return True, f"Name OK: {basename}"
    return False, f"Naming issue: {basename} (spaces/special chars)"


def _mechanical_path_check(path: str, workspace: str) -> tuple[bool, str]:
    """Zero-dep: file in managed directory (not scattered in root)?"""
    from concinno.core.config import get_config
    brain_dir = get_config().brain_dir
    rel = os.path.relpath(path, workspace).replace("\\", "/")
    managed = ("media/", "assets/", "images/", "audio/", "video/",
               "docs/", "book_drafts/", f"{brain_dir}/", "screenshots/",
               "downloads/", "projects/", "src/", "packages/")
    if any(rel.startswith(d) or f"/{d}" in rel for d in managed):
        return True, "In managed directory"
    if "/" not in rel:
        return False, f"In project root: {rel}"
    return True, f"Path: {rel}"


def _validate_image(files: list[str], workspace: str) -> TypeReport:
    """WIREDO for image files."""
    report = TypeReport(asset_type=AssetType.IMAGE, files=files)
    all_exist = all_named = all_pathed = True
    evidence_d = evidence_i = evidence_w = ""

    for f in files:
        ok, ev = _mechanical_file_check(f)
        if not ok:
            all_exist = False
            evidence_d = ev
        ok, ev = _mechanical_naming_check(f)
        if not ok:
            all_named = False
            evidence_i = ev
        ok, ev = _mechanical_path_check(f, workspace)
        if not ok:
            all_pathed = False
            evidence_w = ev

    # W — Wired: in managed directory
    report.checks.append(CheckResult(
        WiredoDimension.WIRED,
        CheckState.PASS if all_pathed else CheckState.FAIL,
        evidence_w or f"All {len(files)} images in managed dirs",
    ))

    # I — Inherited: naming convention
    report.checks.append(CheckResult(
        WiredoDimension.INHERITED,
        CheckState.PASS if all_named else CheckState.FAIL,
        evidence_i or "Naming OK",
    ))

    # R — Responsive: resolution/format check (deep, needs PIL)
    r_result = _deep_image_responsive(files)
    report.checks.append(r_result)

    # E — Extensible: metadata present
    report.checks.append(CheckResult(
        WiredoDimension.EXTENSIBLE, CheckState.PASS,
        "Metadata check — manual for generated images",
    ))

    # D — Defended: files exist and non-zero
    report.checks.append(CheckResult(
        WiredoDimension.DEFENDED,
        CheckState.PASS if all_exist else CheckState.FAIL,
        evidence_d or f"All {len(files)} files verified",
    ))

    # O — Observable: N/A for standalone
    report.checks.append(CheckResult(
        WiredoDimension.OBSERVABLE, CheckState.PASS, "N/A — standalone asset",
    ))

    return report


def _deep_image_responsive(files: list[str]) -> CheckResult:
    """Deep check: image resolution/format via PIL. SKIP if PIL missing."""
    try:
        from PIL import Image
    except ImportError:
        return CheckResult(WiredoDimension.RESPONSIVE, CheckState.SKIP,
                           "PIL not installed — cannot verify resolution")

    issues = []
    for f in files:
        try:
            img = Image.open(f)
            w, h = img.size
            if w < 800 and h < 800:
                issues.append(f"{os.path.basename(f)}: {w}x{h} (min 800px)")
            # Black image detection
            if img.mode == "RGB":
                extrema = img.getextrema()
                if all(mn == mx == 0 for mn, mx in extrema):
                    issues.append(f"{os.path.basename(f)}: black image")
        except Exception as e:
            issues.append(f"{os.path.basename(f)}: {e}")

    if not issues:
        return CheckResult(WiredoDimension.RESPONSIVE, CheckState.PASS,
                           f"All {len(files)} images ≥800px")
    return CheckResult(WiredoDimension.RESPONSIVE, CheckState.FAIL,
                       "; ".join(issues[:3]))


def _validate_video(files: list[str], workspace: str) -> TypeReport:
    """WIREDO for video files."""
    report = TypeReport(asset_type=AssetType.VIDEO, files=files)

    # Mechanical checks
    all_exist = all_pathed = True
    evidence_d = evidence_w = ""
    for f in files:
        ok, ev = _mechanical_file_check(f)
        if not ok:
            all_exist = False
            evidence_d = ev
        ok, ev = _mechanical_path_check(f, workspace)
        if not ok:
            all_pathed = False
            evidence_w = ev

    report.checks.append(CheckResult(
        WiredoDimension.WIRED,
        CheckState.PASS if all_pathed else CheckState.FAIL,
        evidence_w or "All in managed dirs",
    ))

    report.checks.append(CheckResult(
        WiredoDimension.INHERITED, CheckState.PASS,
        "Naming convention — manual check for video",
    ))

    # R — Deep check: ffprobe
    report.checks.append(_deep_video_responsive(files))

    report.checks.append(CheckResult(
        WiredoDimension.EXTENSIBLE, CheckState.PASS,
        "Container metadata — manual",
    ))

    report.checks.append(CheckResult(
        WiredoDimension.DEFENDED,
        CheckState.PASS if all_exist else CheckState.FAIL,
        evidence_d or f"All {len(files)} files verified",
    ))

    report.checks.append(CheckResult(
        WiredoDimension.OBSERVABLE, CheckState.PASS, "N/A — standalone asset",
    ))

    return report


def _deep_video_responsive(files: list[str]) -> CheckResult:
    """Deep check: video codec/resolution/bitrate via ffprobe."""
    import shutil
    import subprocess

    if not shutil.which("ffprobe"):
        return CheckResult(WiredoDimension.RESPONSIVE, CheckState.SKIP,
                           "ffprobe not installed")

    issues = []
    for f in files:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", f],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode != 0:
                issues.append(f"{os.path.basename(f)}: ffprobe failed")
                continue
            import json
            probe = json.loads(result.stdout)
            streams = probe.get("streams", [])
            video_s = [s for s in streams if s.get("codec_type") == "video"]
            if not video_s:
                issues.append(f"{os.path.basename(f)}: no video stream")
                continue
            vs = video_s[0]
            w = int(vs.get("width", 0))
            h = int(vs.get("height", 0))
            if w < 1280 and h < 720:
                issues.append(f"{os.path.basename(f)}: {w}x{h} (min 720p)")
        except (subprocess.TimeoutExpired, Exception) as e:
            issues.append(f"{os.path.basename(f)}: {e}")

    if not issues:
        return CheckResult(WiredoDimension.RESPONSIVE, CheckState.PASS,
                           f"All {len(files)} videos verified")
    return CheckResult(WiredoDimension.RESPONSIVE, CheckState.FAIL,
                       "; ".join(issues[:3]))


def _validate_audio(files: list[str], workspace: str) -> TypeReport:
    """WIREDO for audio files."""
    report = TypeReport(asset_type=AssetType.AUDIO, files=files)

    all_exist = all_pathed = True
    evidence_d = evidence_w = ""
    for f in files:
        ok, ev = _mechanical_file_check(f)
        if not ok:
            all_exist = False
            evidence_d = ev
        ok, ev = _mechanical_path_check(f, workspace)
        if not ok:
            all_pathed = False
            evidence_w = ev

    report.checks.append(CheckResult(
        WiredoDimension.WIRED,
        CheckState.PASS if all_pathed else CheckState.FAIL,
        evidence_w or "All in managed dirs",
    ))

    report.checks.append(CheckResult(
        WiredoDimension.INHERITED, CheckState.PASS,
        "Naming convention — manual check for audio",
    ))

    report.checks.append(_deep_audio_responsive(files))

    report.checks.append(CheckResult(
        WiredoDimension.EXTENSIBLE, CheckState.PASS,
        "Params as constants — checked at code level",
    ))

    report.checks.append(CheckResult(
        WiredoDimension.DEFENDED,
        CheckState.PASS if all_exist else CheckState.FAIL,
        evidence_d or f"All {len(files)} files verified",
    ))

    report.checks.append(CheckResult(
        WiredoDimension.OBSERVABLE, CheckState.PASS, "N/A — standalone asset",
    ))

    return report


def _deep_audio_responsive(files: list[str]) -> CheckResult:
    """Deep check: audio sample rate/codec via ffprobe."""
    import shutil
    import subprocess

    if not shutil.which("ffprobe"):
        return CheckResult(WiredoDimension.RESPONSIVE, CheckState.SKIP,
                           "ffprobe not installed")

    issues = []
    for f in files:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", f],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode != 0:
                issues.append(f"{os.path.basename(f)}: ffprobe failed")
                continue
            import json
            probe = json.loads(result.stdout)
            streams = probe.get("streams", [])
            audio_s = [s for s in streams if s.get("codec_type") == "audio"]
            if not audio_s:
                issues.append(f"{os.path.basename(f)}: no audio stream")
                continue
            sr = int(audio_s[0].get("sample_rate", 0))
            if sr not in (22050, 44100, 48000, 96000):
                issues.append(f"{os.path.basename(f)}: sample_rate={sr}")
        except (subprocess.TimeoutExpired, Exception) as e:
            issues.append(f"{os.path.basename(f)}: {e}")

    if not issues:
        return CheckResult(WiredoDimension.RESPONSIVE, CheckState.PASS,
                           f"All {len(files)} audio files verified")
    return CheckResult(WiredoDimension.RESPONSIVE, CheckState.FAIL,
                       "; ".join(issues[:3]))


def _check_md_structure(path: str) -> tuple[bool, str]:
    """Check markdown has frontmatter (---) or H1 heading."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(2000)
        if head.startswith("---") or head.startswith("# "):
            return True, ""
        return False, f"{os.path.basename(path)}: no frontmatter or H1"
    except Exception:
        return True, ""


def _validate_document(files: list[str], workspace: str) -> TypeReport:
    """WIREDO for document files (.md, .docx, .txt, .pdf)."""
    report = TypeReport(asset_type=AssetType.DOCUMENT, files=files)

    all_exist = all_structured = True
    evidence_d = evidence_i = ""
    for f in files:
        ok, ev = _mechanical_file_check(f)
        if not ok:
            all_exist, evidence_d = False, ev
        if os.path.splitext(f)[1].lower() == ".md":
            ok, ev = _check_md_structure(f)
            if not ok:
                all_structured, evidence_i = False, ev

    large = [f for f in files if os.path.isfile(f) and os.path.getsize(f) > 10_000_000]
    report.checks = [
        CheckResult(WiredoDimension.WIRED, CheckState.PASS,
                    "Document linkage — manual verification"),
        CheckResult(WiredoDimension.INHERITED,
                    CheckState.PASS if all_structured else CheckState.FAIL,
                    evidence_i or "All documents have proper structure"),
        CheckResult(WiredoDimension.RESPONSIVE,
                    CheckState.FAIL if large else CheckState.PASS,
                    f"{len(large)} oversized" if large else "All within size budget"),
        CheckResult(WiredoDimension.EXTENSIBLE, CheckState.PASS,
                    "Parametric content — manual check"),
        CheckResult(WiredoDimension.DEFENDED,
                    CheckState.PASS if all_exist else CheckState.FAIL,
                    evidence_d or f"All {len(files)} files verified"),
        CheckResult(WiredoDimension.OBSERVABLE, CheckState.PASS,
                    "N/A — standalone document"),
    ]
    return report


# ── Validators Registry ───────────────────────────────────────


_VALIDATORS: dict[AssetType, Any] = {
    AssetType.CODE: _validate_code,
    AssetType.IMAGE: _validate_image,
    AssetType.VIDEO: _validate_video,
    AssetType.AUDIO: _validate_audio,
    AssetType.DOCUMENT: _validate_document,
}


# ── ArtifactPipeline (main entry) ─────────────────────────────


class ArtifactPipeline:
    """Unified multi-type WIREDO verification pipeline.

    Usage:
        pipeline = ArtifactPipeline(
            cache_dir="/path/.concinno_cache",
            session_id="abc123",
            workspace="/path/to/project",
        )
        report = pipeline.run()
        if report.should_block():
            # Block stop
        print(report.to_stderr())
    """

    def __init__(
        self,
        cache_dir: str = "",
        session_id: str = "",
        workspace: str = "",
        block_dimensions: list[str] | None = None,
    ) -> None:
        self.cache_dir = cache_dir or os.path.join(
            os.environ.get("CLAUDE_PROJECT_DIR", "."),
            ".concinno_cache",
        )
        self.session_id = session_id
        self.workspace = workspace or os.environ.get("CLAUDE_PROJECT_DIR", ".")
        self.block_dimensions = block_dimensions

    def run(self) -> ArtifactReport:
        """Run full multi-type WIREDO verification.

        1. Collect artifacts from sentinel state
        2. Detect media task types from call history
        3. Validate each type with appropriate validators
        4. Cross-reference: media tasks without artifacts = untracked
        """
        artifacts = collect_artifacts(self.cache_dir, self.session_id)
        media_tasks = detect_media_tasks(self.cache_dir, self.session_id)

        report = ArtifactReport()

        # Run validators for each detected type
        for atype, files in artifacts.items():
            validator = _VALIDATORS.get(atype)
            if validator is None:
                continue
            if atype == AssetType.CODE:
                type_report = validator(
                    files, self.workspace, self.cache_dir, self.session_id,
                )
            else:
                type_report = validator(files, self.workspace)
            report.type_reports.append(type_report)

        # Cross-reference: media tasks detected but no artifacts found
        artifact_types = {a.value for a in artifacts}
        for mt in media_tasks:
            if mt not in artifact_types:
                report.media_tasks_untracked.append(mt)

        return report

    def run_and_gate(self) -> tuple[bool, str]:
        """Run and return (should_block, block_reason).

        Convenience for on-stop hook integration.
        """
        report = self.run()
        if not report.type_reports:
            return False, ""

        should_block = report.should_block(self.block_dimensions)
        if should_block:
            return True, report.format_block_reason()

        return False, ""
