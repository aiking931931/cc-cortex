"""cc_cortex.delivery — Enterprise Delivery Gate.

@module delivery
@responsibility Ensure agents deliver verified results, not unverified guesses.
    Define-done criteria, evidence-based verification, honest reporting,
    append-only audit trail, and orphan export detection.
@dependencies cc_cortex.guards.base
@exports DeliveryGate, ExitCriteria, VerificationResult, DeliveryReport,
    DeliveryState, OrphanExport, check_orphan_exports, scan_orphans_batch,
    save_state, load_state, on_stop_check, DeliveryGuard
"""

from ._base import (
    Criterion,
    CriterionType,
    DeliveryReport,
    DeliveryState,
    ExitCriteria,
    VerificationResult,
)
from .adapter import DeliveryGuard
from .artifact_pipeline import (
    ArtifactPipeline,
    ArtifactReport,
    CheckResult,
    CheckState,
    TypeReport,
    collect_artifacts,
    detect_media_tasks,
)
from .gate import DeliveryGate
from .orphan import (
    OrphanExport,
    _detect_language,
    _extract_exports,
    _is_barrel_file,
    _is_symbol_imported,
    _parse_comma_names,
    check_orphan_exports,
    scan_orphans_batch,
)
from .persistence import load_state, on_stop_check, save_state
from .wiredo import (
    _defended_check,
    _find_unwired_files,
    _get_session_code_files,
    _has_backend_files,
    _has_frontend_files,
    _has_screenshot_evidence,
    _has_test_evidence,
    _is_wired,
    auto_delivery_gate,
    wired_check,  # backward compat alias
    wiredo_check,
    wiredo_full_check,
)

__all__ = [
    # Models
    "Criterion",
    "CriterionType",
    "DeliveryState",
    "ExitCriteria",
    "VerificationResult",
    "DeliveryReport",
    "DeliveryGuard",
    "DeliveryGate",
    "OrphanExport",
    # Public API
    "check_orphan_exports",
    "scan_orphans_batch",
    "save_state",
    "load_state",
    "on_stop_check",
    "wiredo_check",
    "wired_check",  # backward compat alias
    "auto_delivery_gate",
    # Internal (used by tests)
    "_detect_language",
    "_extract_exports",
    "_is_barrel_file",
    "_is_symbol_imported",
    "_parse_comma_names",
    "_defended_check",
    "_find_unwired_files",
    "_get_session_code_files",
    "_has_frontend_files",
    "_has_backend_files",
    "_has_screenshot_evidence",
    "_has_test_evidence",
    "_is_wired",
    "wiredo_full_check",
    # ArtifactPipeline (multi-type WIREDO)
    "ArtifactPipeline",
    "ArtifactReport",
    "CheckResult",
    "CheckState",
    "TypeReport",
    "collect_artifacts",
    "detect_media_tasks",
]
