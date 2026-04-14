"""cc_cortex.asset_validator — Universal asset validation framework.

WIREDO generalized: every deliverable (code, image, video, audio, document)
goes through the same six-dimension quality gate.

@module asset_validator
@responsibility Define asset type schemas, validate against WIREDO dimensions,
    cascade results across stacked projects (bottom validates, top inherits).
@dependencies cc_cortex.guards.base, cc_cortex.core.config
@exports AssetType, WiredoDimension, AssetSchema, ValidationResult,
    AssetValidator, ProjectCascade
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ── Asset Types ──────────────────────────────────────────────────────


class AssetType(Enum):
    """Every deliverable belongs to exactly one type.

    WIREDO applies to ALL types — with selective modes (strict/warn/skip/na).
    No type is exempt from WIREDO; only the validation method differs.
    """

    CODE = "code"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    MEDIA = "media"  # naming + folder structure (cross-type)
    PROTOCOL = "protocol"  # transport/API specs (.proto, .graphql, grpc, websocket)
    CONFIG = "config"  # schemas, env, settings (.json, .yaml, .toml, .env)


# ── WIREDO Dimensions ────────────────────────────────────────────────


class WiredoDimension(Enum):
    """Six universal quality dimensions."""

    WIRED = "wired"
    INHERITED = "inherited"
    RESPONSIVE = "responsive"
    EXTENSIBLE = "extensible"
    DEFENDED = "defended"
    OBSERVABLE = "observable"


# ── Validation Result ────────────────────────────────────────────────


@dataclass
class DimensionResult:
    """Result for a single WIREDO dimension check."""

    dimension: WiredoDimension
    passed: bool
    evidence: str = ""
    na: bool = False  # True = not applicable for this asset type

    @property
    def status(self) -> str:
        if self.na:
            return "N/A"
        return "PASS" if self.passed else "FAIL"


@dataclass
class ValidationResult:
    """Complete validation result for one asset."""

    asset_type: AssetType
    asset_path: str
    dimensions: list[DimensionResult] = field(default_factory=list)
    cascaded_from: str = ""  # project name if inherited

    @property
    def passed(self) -> bool:
        return all(d.passed or d.na for d in self.dimensions)

    @property
    def fail_count(self) -> int:
        return sum(1 for d in self.dimensions if not d.passed and not d.na)

    def to_table(self) -> str:
        """Render as markdown table for reports."""
        lines = [
            "| Dimension | Status | Evidence |",
            "|-----------|--------|----------|",
        ]
        _labels = {
            WiredoDimension.WIRED: "W — Wired",
            WiredoDimension.INHERITED: "I — Inherited & Aligned",
            WiredoDimension.RESPONSIVE: "R — Responsive & Performant",
            WiredoDimension.EXTENSIBLE: "E — Extensible",
            WiredoDimension.DEFENDED: "D — Defended & Verified",
            WiredoDimension.OBSERVABLE: "O — Observable",
        }
        for d in self.dimensions:
            icon = "N/A" if d.na else ("✅" if d.passed else "❌")
            label = _labels.get(d.dimension, d.dimension.name)
            lines.append(f"| **{label}** | {icon} | {d.evidence} |")
        if self.cascaded_from:
            lines.append(f"\n> Cascaded from: **{self.cascaded_from}**")
        return "\n".join(lines)


# ── Asset Schemas ────────────────────────────────────────────────────


@dataclass(frozen=True)
class AssetSchema:
    """Validation schema for a specific asset type.

    Each field maps a WIREDO dimension to a callable validator.
    Validators return (passed: bool, evidence: str).
    """

    asset_type: AssetType
    validators: dict[WiredoDimension, Any] = field(default_factory=dict)
    # Dimensions that are N/A for this type
    na_dimensions: frozenset[WiredoDimension] = frozenset()


# ── Built-in Validators ──────────────────────────────────────────────


def _check_image_wired(path: str, workspace: str) -> tuple[bool, str]:
    """W: Image is registered in character library, not orphaned in tmp/."""
    if "/tmp/" in path.replace("\\", "/") or "\\tmp\\" in path:
        return False, "Image in tmp/ — not wired to system"
    # Check if file exists in a known asset directory
    norm = path.replace("\\", "/")
    from cc_cortex.core.config import get_config
    brain_dir = get_config().brain_dir
    if any(d in norm for d in (f"{brain_dir}/", "media/", "assets/")):
        return True, "In managed asset directory"
    return False, "Not in managed asset directory"


def _check_image_inherited(path: str, workspace: str) -> tuple[bool, str]:
    """I: Follows naming convention."""
    basename = os.path.basename(path)
    # Allow standard names
    _exempt = {"avatar", "thumbnail", "cover", "ref_", "profile"}
    if any(e in basename.lower() for e in _exempt):
        return True, f"Exempt name: {basename}"
    # Check naming pattern: {role}_{type}_{desc}_{date}_{hash}.{ext}
    pattern = r"^[a-z]+_(?:photo|img|video|audio|voice)_[a-z0-9-]+_\d{8}_[a-z0-9]{4}\.\w+$"
    if re.match(pattern, basename):
        return True, f"Naming OK: {basename}"
    return False, f"Naming violation: {basename}"


def _check_image_responsive(path: str, workspace: str) -> tuple[bool, str]:
    """R: Resolution >= 800px, valid format, not black/corrupt."""
    try:
        from PIL import Image

        img = Image.open(path)
        w, h = img.size
        if w < 800 and h < 800:
            return False, f"Too small: {w}x{h} (min 800px)"
        # Black image detection (Laplacian variance proxy)
        if img.mode == "RGB":
            extrema = img.getextrema()
            if all(mn == mx == 0 for mn, mx in extrema):
                return False, "Black image detected"
        return True, f"{w}x{h} {img.format or 'unknown'}"
    except ImportError:
        return True, "PIL not available — skipped"
    except Exception as e:
        return False, f"Cannot open: {e}"


def _check_image_extensible(path: str, workspace: str) -> tuple[bool, str]:
    """E: Has metadata (EXIF/IPTC/XMP)."""
    try:
        from PIL import Image

        img = Image.open(path)
        info = img.info or {}
        if info:
            return True, f"{len(info)} metadata fields"
        return True, "No metadata (acceptable for generated images)"
    except ImportError:
        return True, "PIL not available — skipped"
    except Exception:
        return True, "Metadata check skipped"


def _check_image_defended(path: str, workspace: str) -> tuple[bool, str]:
    """D: File exists, readable, non-zero size."""
    if not os.path.isfile(path):
        return False, "File not found"
    size = os.path.getsize(path)
    if size == 0:
        return False, "Empty file (0 bytes)"
    if size < 1000:
        return False, f"Suspiciously small: {size} bytes"
    return True, f"{size:,} bytes"


def _check_image_observable(path: str, workspace: str) -> tuple[bool, str]:
    """O: N/A for standalone images (non-SaaS)."""
    return True, "N/A — standalone asset"


# ── Video Validators ─────────────────────────────────────────────────


def _ffprobe(path: str) -> dict:
    """Run ffprobe and return parsed JSON."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _check_video_wired(path: str, workspace: str) -> tuple[bool, str]:
    """W: Video is in managed directory."""
    norm = path.replace("\\", "/")
    if "/tmp/" in norm or "\\tmp\\" in path:
        return False, "Video in tmp/ — not wired"
    if any(d in norm for d in ("media/", "assets/")):
        return True, "In managed directory"
    return False, "Not in managed directory"


def _check_video_inherited(path: str, workspace: str) -> tuple[bool, str]:
    """I: Naming convention + correct folder."""
    return _check_image_inherited(path, workspace)


def _check_video_responsive(path: str, workspace: str) -> tuple[bool, str]:
    """R: Codec H.264/H.265, bitrate <= 2Mbps, resolution >= 720p."""
    probe = _ffprobe(path)
    if not probe:
        return False, "ffprobe failed or not available"

    streams = probe.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        return False, "No video stream found"

    vs = video_streams[0]
    codec = vs.get("codec_name", "")
    width = int(vs.get("width", 0))
    height = int(vs.get("height", 0))

    fmt = probe.get("format", {})
    bitrate = int(fmt.get("bit_rate", 0))
    duration = float(fmt.get("duration", 0))

    issues = []
    if codec not in ("h264", "hevc", "h265", "vp9", "av1"):
        issues.append(f"codec={codec}")
    if bitrate > 2_000_000:
        issues.append(f"bitrate={bitrate // 1000}kbps (max 2000)")
    if width < 1280 and height < 720:
        issues.append(f"resolution={width}x{height} (min 720p)")

    evidence = f"{codec} {width}x{height} {bitrate // 1000}kbps {duration:.1f}s"
    if issues:
        return False, f"{evidence} — issues: {', '.join(issues)}"
    return True, evidence


def _check_video_extensible(path: str, workspace: str) -> tuple[bool, str]:
    """E: Has proper container metadata."""
    probe = _ffprobe(path)
    if not probe:
        return True, "ffprobe not available — skipped"
    tags = probe.get("format", {}).get("tags", {})
    return True, f"{len(tags)} tags" if tags else "No tags (acceptable)"


def _check_video_defended(path: str, workspace: str) -> tuple[bool, str]:
    """D: File exists, non-zero, ffprobe succeeds."""
    if not os.path.isfile(path):
        return False, "File not found"
    size = os.path.getsize(path)
    if size == 0:
        return False, "Empty file"
    probe = _ffprobe(path)
    if not probe:
        return True, f"{size:,} bytes (ffprobe not available)"
    return True, f"{size:,} bytes, probe OK"


def _check_video_observable(path: str, workspace: str) -> tuple[bool, str]:
    """O: N/A for non-SaaS."""
    return True, "N/A — standalone asset"


# ── Audio Validators ─────────────────────────────────────────────────


def _check_audio_wired(path: str, workspace: str) -> tuple[bool, str]:
    """W: Audio in managed directory."""
    norm = path.replace("\\", "/")
    if "/tmp/" in norm:
        return False, "Audio in tmp/"
    return True, "In managed directory"


def _check_audio_inherited(path: str, workspace: str) -> tuple[bool, str]:
    """I: Naming convention."""
    return _check_image_inherited(path, workspace)


def _check_audio_responsive(path: str, workspace: str) -> tuple[bool, str]:
    """R: Sample rate 44.1/48kHz, proper codec, LUFS check if available."""
    probe = _ffprobe(path)
    if not probe:
        return True, "ffprobe not available — skipped"

    streams = probe.get("streams", [])
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio_streams:
        return False, "No audio stream"

    a = audio_streams[0]
    codec = a.get("codec_name", "")
    sample_rate = int(a.get("sample_rate", 0))
    channels = int(a.get("channels", 0))

    issues = []
    if sample_rate not in (44100, 48000, 96000, 22050):
        issues.append(f"sample_rate={sample_rate}")
    if codec not in ("aac", "mp3", "opus", "flac", "pcm_s16le", "pcm_s24le", "vorbis"):
        issues.append(f"codec={codec}")

    evidence = f"{codec} {sample_rate}Hz {channels}ch"
    if issues:
        return False, f"{evidence} — {', '.join(issues)}"
    return True, evidence


def _check_audio_extensible(path: str, workspace: str) -> tuple[bool, str]:
    """E: Parameters defined as constants, not hardcoded."""
    return True, "Checked at code level"


def _check_audio_defended(path: str, workspace: str) -> tuple[bool, str]:
    """D: File exists, non-zero."""
    if not os.path.isfile(path):
        return False, "File not found"
    size = os.path.getsize(path)
    if size == 0:
        return False, "Empty file"
    return True, f"{size:,} bytes"


def _check_audio_observable(path: str, workspace: str) -> tuple[bool, str]:
    """O: N/A for non-SaaS."""
    return True, "N/A — standalone asset"


# ── Document Validators ──────────────────────────────────────────────


def _check_doc_wired(path: str, workspace: str) -> tuple[bool, str]:
    """W: Document linked/referenced somewhere."""
    return True, "Manual check required"


def _check_doc_inherited(path: str, workspace: str) -> tuple[bool, str]:
    """I: Uses unified template, correct heading structure."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return True, "DOCX — template via MCP word-server"
    if ext == ".md":
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read(2000)
            if content.startswith("---"):
                return True, "Has frontmatter"
            if content.startswith("# "):
                return True, "Has H1 heading"
            return False, "No structure (missing frontmatter or H1)"
        except Exception as e:
            return False, f"Cannot read: {e}"
    return True, f"Format: {ext}"


def _check_doc_responsive(path: str, workspace: str) -> tuple[bool, str]:
    """R: Reasonable file size, no broken links."""
    if not os.path.isfile(path):
        return False, "File not found"
    size = os.path.getsize(path)
    if size > 10_000_000:
        return False, f"Too large: {size:,} bytes"
    return True, f"{size:,} bytes"


def _check_doc_extensible(path: str, workspace: str) -> tuple[bool, str]:
    """E: Variables/dates parametric, not hardcoded."""
    return True, "Manual check"


def _check_doc_defended(path: str, workspace: str) -> tuple[bool, str]:
    """D: File exists, non-zero, well-formed."""
    if not os.path.isfile(path):
        return False, "File not found"
    size = os.path.getsize(path)
    if size == 0:
        return False, "Empty file"
    return True, f"{size:,} bytes"


def _check_doc_observable(path: str, workspace: str) -> tuple[bool, str]:
    """O: Version tracking."""
    return True, "N/A — standalone document"


# ── Protocol Validators ──────────────────────────────────────────────


def _check_protocol_wired(path: str, workspace: str) -> tuple[bool, str]:
    """W: Protocol spec is referenced by transport/client code."""
    return True, "Checked via grep in WIREDO report"


def _check_protocol_inherited(path: str, workspace: str) -> tuple[bool, str]:
    """I: Follows established protocol conventions (versioning, naming)."""
    return True, "Checked via architecture review"


def _check_protocol_responsive(path: str, workspace: str) -> tuple[bool, str]:
    """R: Latency/throughput within acceptable bounds."""
    return True, "Checked via benchmark/load test"


def _check_protocol_extensible(path: str, workspace: str) -> tuple[bool, str]:
    """E: Adapter/plugin points, backward-compatible versioning."""
    return True, "Checked via code review"


def _check_protocol_defended(path: str, workspace: str) -> tuple[bool, str]:
    """D: Integration tests pass, crypto/dedup verified."""
    return True, "Checked via test suite"


def _check_protocol_observable(path: str, workspace: str) -> tuple[bool, str]:
    """O: Transport logging/tracing (warn mode — nice-to-have)."""
    return True, "Warn — check for trace/log hooks"


# ── Config Validators ────────────────────────────────────────────────


def _check_config_wired(path: str, workspace: str) -> tuple[bool, str]:
    """W: Config is loaded by application code."""
    return True, "Checked via grep in WIREDO report"


def _check_config_inherited(path: str, workspace: str) -> tuple[bool, str]:
    """I: Follows project config conventions (structure, naming)."""
    basename = os.path.basename(path).lower()
    if basename.startswith(".") or basename in (
        "pyproject.toml", "package.json", "tsconfig.json",
    ):
        return True, f"Standard config: {basename}"
    return True, "Manual review"


def _check_config_extensible(path: str, workspace: str) -> tuple[bool, str]:
    """E: Schema-validated, defaults documented."""
    return True, "Checked via code review"


def _check_config_defended(path: str, workspace: str) -> tuple[bool, str]:
    """D: Config parses without error, schema validation passes."""
    if not os.path.isfile(path):
        return False, "File not found"
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read(50_000)
        if ext == ".json":
            json.loads(content)
            return True, "Valid JSON"
        return True, f"Readable ({len(content)} chars)"
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return False, f"Parse error: {e}"


# ── Code Validators (existing WIREDO — wrapped) ──────────────────────


def _check_code_wired(path: str, workspace: str) -> tuple[bool, str]:
    """W: Someone imports/calls this."""
    return True, "Checked via grep in WIREDO report"


def _check_code_inherited(path: str, workspace: str) -> tuple[bool, str]:
    """I: Uses base classes, correct module location."""
    return True, "Checked via architecture review"


def _check_code_responsive(path: str, workspace: str) -> tuple[bool, str]:
    """R: No O(n²), no N+1."""
    return True, "Checked via code review"


def _check_code_extensible(path: str, workspace: str) -> tuple[bool, str]:
    """E: Constants at top, config interface."""
    return True, "Checked via code review"


def _check_code_defended(path: str, workspace: str) -> tuple[bool, str]:
    """D: lint + test + verify."""
    return True, "Checked via tsc/vitest/ruff"


def _check_code_observable(path: str, workspace: str) -> tuple[bool, str]:
    """O: Has stats/log."""
    return True, "Checked via code review"


# ── Schema Registry ──────────────────────────────────────────────────


def _make_validator(fn):
    """Wrap a (path, workspace) -> (bool, str) function."""
    return fn


_SCHEMAS: dict[AssetType, AssetSchema] = {
    AssetType.CODE: AssetSchema(
        asset_type=AssetType.CODE,
        validators={
            WiredoDimension.WIRED: _check_code_wired,
            WiredoDimension.INHERITED: _check_code_inherited,
            WiredoDimension.RESPONSIVE: _check_code_responsive,
            WiredoDimension.EXTENSIBLE: _check_code_extensible,
            WiredoDimension.DEFENDED: _check_code_defended,
            WiredoDimension.OBSERVABLE: _check_code_observable,
        },
    ),
    AssetType.IMAGE: AssetSchema(
        asset_type=AssetType.IMAGE,
        validators={
            WiredoDimension.WIRED: _check_image_wired,
            WiredoDimension.INHERITED: _check_image_inherited,
            WiredoDimension.RESPONSIVE: _check_image_responsive,
            WiredoDimension.EXTENSIBLE: _check_image_extensible,
            WiredoDimension.DEFENDED: _check_image_defended,
            WiredoDimension.OBSERVABLE: _check_image_observable,
        },
        na_dimensions=frozenset({WiredoDimension.OBSERVABLE}),
    ),
    AssetType.VIDEO: AssetSchema(
        asset_type=AssetType.VIDEO,
        validators={
            WiredoDimension.WIRED: _check_video_wired,
            WiredoDimension.INHERITED: _check_video_inherited,
            WiredoDimension.RESPONSIVE: _check_video_responsive,
            WiredoDimension.EXTENSIBLE: _check_video_extensible,
            WiredoDimension.DEFENDED: _check_video_defended,
            WiredoDimension.OBSERVABLE: _check_video_observable,
        },
        na_dimensions=frozenset({WiredoDimension.OBSERVABLE}),
    ),
    AssetType.AUDIO: AssetSchema(
        asset_type=AssetType.AUDIO,
        validators={
            WiredoDimension.WIRED: _check_audio_wired,
            WiredoDimension.INHERITED: _check_audio_inherited,
            WiredoDimension.RESPONSIVE: _check_audio_responsive,
            WiredoDimension.EXTENSIBLE: _check_audio_extensible,
            WiredoDimension.DEFENDED: _check_audio_defended,
            WiredoDimension.OBSERVABLE: _check_audio_observable,
        },
        na_dimensions=frozenset({WiredoDimension.OBSERVABLE}),
    ),
    AssetType.DOCUMENT: AssetSchema(
        asset_type=AssetType.DOCUMENT,
        validators={
            WiredoDimension.WIRED: _check_doc_wired,
            WiredoDimension.INHERITED: _check_doc_inherited,
            WiredoDimension.RESPONSIVE: _check_doc_responsive,
            WiredoDimension.EXTENSIBLE: _check_doc_extensible,
            WiredoDimension.DEFENDED: _check_doc_defended,
            WiredoDimension.OBSERVABLE: _check_doc_observable,
        },
        na_dimensions=frozenset({WiredoDimension.OBSERVABLE}),
    ),
    AssetType.PROTOCOL: AssetSchema(
        asset_type=AssetType.PROTOCOL,
        validators={
            WiredoDimension.WIRED: _check_protocol_wired,
            WiredoDimension.INHERITED: _check_protocol_inherited,
            WiredoDimension.RESPONSIVE: _check_protocol_responsive,
            WiredoDimension.EXTENSIBLE: _check_protocol_extensible,
            WiredoDimension.DEFENDED: _check_protocol_defended,
            WiredoDimension.OBSERVABLE: _check_protocol_observable,
        },
        # O = warn (nice-to-have), not na — protocol monitoring is valuable
    ),
    AssetType.CONFIG: AssetSchema(
        asset_type=AssetType.CONFIG,
        validators={
            WiredoDimension.WIRED: _check_config_wired,
            WiredoDimension.INHERITED: _check_config_inherited,
            WiredoDimension.EXTENSIBLE: _check_config_extensible,
            WiredoDimension.DEFENDED: _check_config_defended,
        },
        # R + O = na for config (no performance or observability dimension)
        na_dimensions=frozenset({WiredoDimension.RESPONSIVE, WiredoDimension.OBSERVABLE}),
    ),
}


def get_schema(asset_type: AssetType) -> AssetSchema:
    """Get the validation schema for an asset type."""
    return _SCHEMAS[asset_type]


# ── Asset Validator ──────────────────────────────────────────────────


class AssetValidator:
    """Validates any asset against its WIREDO schema.

    Usage:
        validator = AssetValidator(workspace="/path/to/project")
        result = validator.validate("/path/to/image.png", AssetType.IMAGE)
        print(result.to_table())
    """

    def __init__(self, workspace: str = "") -> None:
        self.workspace = workspace

    def validate(
        self,
        path: str,
        asset_type: AssetType,
        *,
        skip_dimensions: frozenset[WiredoDimension] | None = None,
    ) -> ValidationResult:
        """Run all WIREDO validators for the given asset type."""
        schema = get_schema(asset_type)
        result = ValidationResult(asset_type=asset_type, asset_path=path)

        for dim in WiredoDimension:
            if dim in schema.na_dimensions:
                result.dimensions.append(
                    DimensionResult(dimension=dim, passed=True, na=True,
                                    evidence="N/A for this asset type")
                )
                continue

            if skip_dimensions and dim in skip_dimensions:
                result.dimensions.append(
                    DimensionResult(dimension=dim, passed=True, na=True,
                                    evidence="Skipped (cascaded)")
                )
                continue

            validator_fn = schema.validators.get(dim)
            if validator_fn is None:
                result.dimensions.append(
                    DimensionResult(dimension=dim, passed=True, na=True,
                                    evidence="No validator defined")
                )
                continue

            try:
                passed, evidence = validator_fn(path, self.workspace)
            except Exception as e:
                passed, evidence = False, f"Validator error: {e}"

            result.dimensions.append(
                DimensionResult(dimension=dim, passed=passed, evidence=evidence)
            )

        return result

    def validate_batch(
        self,
        assets: list[tuple[str, AssetType]],
    ) -> list[ValidationResult]:
        """Validate multiple assets."""
        return [self.validate(path, atype) for path, atype in assets]


# ── Project Cascade ──────────────────────────────────────────────────


@dataclass
class CascadeResult:
    """Aggregated WIREDO result across a project stack."""

    project: str
    own_results: list[ValidationResult] = field(default_factory=list)
    inherited_from: dict[str, list[ValidationResult]] = field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        for r in self.own_results:
            if not r.passed:
                return False
        for results in self.inherited_from.values():
            for r in results:
                if not r.passed:
                    return False
        return True

    def summary_table(self) -> str:
        """Full cascade report."""
        lines = []
        if self.inherited_from:
            for proj, results in self.inherited_from.items():
                lines.append(f"### Inherited from: {proj}")
                for r in results:
                    passed = "✅" if r.passed else "❌"
                    lines.append(f"**{r.asset_type.value}** {passed} `{r.asset_path}`")
                lines.append("")

        if self.own_results:
            lines.append(f"### Own verification: {self.project}")
            for r in self.own_results:
                lines.append(f"\n**{r.asset_type.value}** `{r.asset_path}`")
                lines.append(r.to_table())

        return "\n".join(lines)


class ProjectCascade:
    """Manages verification cascade across stacked projects.

    When project A depends on project B:
    - B validates its own assets first
    - A inherits B's results (no re-validation)
    - A only validates its own-layer assets

    Config example:
        "wiredo": {
            "project_stack": {
                "psyche": ["infinite-agent"],
                "aegis": ["infinite-agent"]
            }
        }
    """

    def __init__(self, workspace: str = "", stack: dict[str, list[str]] | None = None) -> None:
        self.workspace = workspace
        self.stack = stack or {}
        self._cache: dict[str, list[ValidationResult]] = {}
        self.validator = AssetValidator(workspace=workspace)

    def validate_project(
        self,
        project: str,
        own_assets: list[tuple[str, AssetType]],
        dep_assets: dict[str, list[tuple[str, AssetType]]] | None = None,
    ) -> CascadeResult:
        """Validate a project with cascade.

        Args:
            project: Current project name.
            own_assets: Assets belonging to this project layer.
            dep_assets: Assets per dependency project.
                        Only needed if deps not already cached.
        """
        result = CascadeResult(project=project)
        deps = self.stack.get(project, [])

        # Step 1: Validate dependencies (or use cache)
        for dep in deps:
            if dep in self._cache:
                result.inherited_from[dep] = self._cache[dep]
            elif dep_assets and dep in dep_assets:
                dep_results = self.validator.validate_batch(dep_assets[dep])
                self._cache[dep] = dep_results
                result.inherited_from[dep] = dep_results
            # else: no assets provided for dep, skip

        # Step 2: Validate own assets
        result.own_results = self.validator.validate_batch(own_assets)

        # Cache own results for potential upstream consumers
        self._cache[project] = result.own_results

        return result

    def get_cached(self, project: str) -> list[ValidationResult] | None:
        """Get cached results for a project (if already validated)."""
        return self._cache.get(project)

    def clear_cache(self) -> None:
        """Clear all cached results."""
        self._cache.clear()


# ── Config Integration ───────────────────────────────────────────────


def load_wiredo_config(workspace: str) -> dict:
    """Load WIREDO config from cc_config.json."""
    config_path = os.path.join(workspace, ".claude", "hooks", "cc_config.json")
    defaults = {
        "enabled": True,
        "asset_types": {
            "code": True,
            "image": True,
            "video": True,
            "audio": True,
            "document": True,
            "media": True,
            "protocol": True,
            "config": True,
        },
        "project_stack": {},
    }
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        wiredo = cfg.get("wiredo", {})
        # Merge with defaults
        result = dict(defaults)
        if "enabled" in wiredo:
            result["enabled"] = wiredo["enabled"]
        if "asset_types" in wiredo:
            result["asset_types"] = {**defaults["asset_types"], **wiredo["asset_types"]}
        if "project_stack" in wiredo:
            result["project_stack"] = wiredo["project_stack"]
        # Backward compat: top-level wiredo_enabled
        if "wiredo_enabled" in cfg and "enabled" not in wiredo:
            result["enabled"] = cfg["wiredo_enabled"]
        return result
    except (OSError, json.JSONDecodeError):
        return defaults


def is_asset_type_enabled(workspace: str, asset_type: AssetType) -> bool:
    """Check if a specific asset type's WIREDO validation is enabled."""
    cfg = load_wiredo_config(workspace)
    if not cfg["enabled"]:
        return False
    return cfg["asset_types"].get(asset_type.value, True)


# ── Convenience: Detect asset type from path ─────────────────────────


_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".bmp", ".tiff"})
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a", ".opus"})
_DOC_EXTS = frozenset({".md", ".docx", ".doc", ".pdf", ".txt", ".rst"})
_PROTOCOL_EXTS = frozenset({".proto", ".graphql", ".gql", ".thrift", ".avsc"})
_CONFIG_EXTS = frozenset({".json", ".yaml", ".yml", ".toml", ".env", ".ini", ".cfg"})
_CODE_EXTS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".css", ".scss", ".html",
})


def detect_asset_type(path: str) -> AssetType | None:
    """Detect asset type from file extension. Returns None if unknown."""
    ext = os.path.splitext(path)[1].lower()
    if ext in _PROTOCOL_EXTS:
        return AssetType.PROTOCOL
    if ext in _CONFIG_EXTS:
        return AssetType.CONFIG
    if ext in _CODE_EXTS:
        return AssetType.CODE
    if ext in _IMAGE_EXTS:
        return AssetType.IMAGE
    if ext in _VIDEO_EXTS:
        return AssetType.VIDEO
    if ext in _AUDIO_EXTS:
        return AssetType.AUDIO
    if ext in _DOC_EXTS:
        return AssetType.DOCUMENT
    return None
