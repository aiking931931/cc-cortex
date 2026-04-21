"""concinno — The Cognitive Layer for Claude Code.

Public API (v0.6+, recommended)::

    from concinno import BaseGuard, GuardPipeline, create_default_pipeline

Legacy API (v0.5, deprecated — removed in v1.0)::

    from concinno import HookResult, Pipeline

Migration guide: https://github.com/aiking931931/concinno/blob/main/docs/migration-v05-v06.md
"""

__version__ = "2.12.2"

import warnings as _warnings

from concinno.anti_bloat import (  # noqa: F401
    HitTracker,
    ProtectionLevel,
    Pruner,
    StaleDetector,
)
from concinno.backup_manager import BackupManager  # noqa: F401
from concinno.cbua_ux import (  # noqa: F401
    CbuaCode,
    cbua_clean_write,
    cbua_format,
    cbua_label,
    cbua_redteam_verdict,
    cbua_three_layer,
    cbua_wiredo,
)
from concinno.convention_engine import (  # noqa: F401
    ConventionEngine,
    check_naming,
    suggest_path,
)

# ── 1.14.0 LLM gateway + few-shot + tool loop ────────────
from concinno.escalation import (  # noqa: F401
    DEFAULT_CHAIN,
    EscalationExhausted,
    EscalationResult,
    EscalationTier,
    LLMEscalator,
    TierResult,
    escalate,
)
from concinno.fewshot import (  # noqa: F401
    DEFAULT_STOP_WORDS,
    FewshotBank,
    FewshotCase,
    load_bank,
    retrieve_fewshot,
)
from concinno.field_read import (  # noqa: F401
    COMPRESS_BREAKEVEN_TOKENS,
    FieldReadConfig,
    build_field_context,
    read_handoff_fields,
    read_memory_fields,
)
from concinno.gaia_meta_router import (  # noqa: F401
    ARMS,
    Arm,
    ArmFTRL,
    record_arm_outcome,
    select_arm,
    sps_arm_scores,
)

# ── v0.6+ Guard Pipeline API (recommended) ───────────────
from concinno.guards.base import (  # noqa: F401
    BaseGuard,
    GuardAction,
    GuardCategory,
    GuardContext,
    GuardResult,
)
from concinno.guards.pipeline import GuardPipeline  # noqa: F401
from concinno.guards.registry import create_default_pipeline  # noqa: F401
from concinno.guards.rewrite_guards import (  # noqa: F401
    BashDryRunRewriter,
    BashPipeToShellRewriter,
    WriteSecretFileRewriter,
)
from concinno.library import CortexLibrary, LibraryResult  # noqa: F401
from concinno.prompt_hooks import (  # noqa: F401
    ALL_JUDGES,
    CODE_QUALITY_JUDGE,
    EXCUSE_SCANNER_JUDGE,
    HALLUCINATION_JUDGE,
    PromptJudge,
    build_hook_config,
    install_prompt_hooks,
    list_installed_judges,
    uninstall_prompt_hooks,
)
from concinno.release_authorization import (  # noqa: F401
    PUBLISH_PATTERNS,
    AuthorizationConfig,
    AuthorizationMode,
    check_authorization,
    describe_current_config,
    detect_publish_operation,
    format_required_string,
    load_config,
)
from concinno.riverbed import (  # noqa: F401
    RecallResult,
    Riverbed,
    RiverbedMemory,
    Stake,
)
from concinno.skill_router import SkillRouter  # noqa: F401
from concinno.star import (  # noqa: F401
    PROFILE_CONFIG,
    AdaptiveForgetter,
    AssociativeIndex,
    BM25Index,
    ConfidenceVerdict,
    ConfluencePath,
    ConfluencePoint,
    ConfluenceRAG,
    DivergentResult,
    DivergentSearch,
    ForgetCandidate,
    FreshnessScorer,
    RetrievalProfile,
    RetrievalResult,
    RetrievalTier,
    SearchMode,
    SessionCache,
    SharedMemoryBus,
    STAREngine,
    create_star_engine,
)
from concinno.structured_handoff import (  # noqa: F401
    HandoffRecord,
    HandoffTemplate,
)
from concinno.subagent_identity import (  # noqa: F401
    Identity,
    IdentityProfile,
    assign_identity,
    build_identity_context,
)
from concinno.token_zone import (  # noqa: F401
    Zone,
    detect_zone,
    read_zone_file,
    should_gate_tool,
    write_zone_file,
    zone_injection,
)
from concinno.tool_executor import (  # noqa: F401
    ExecutionState,
    Tool,
    ToolExecutor,
    ToolStep,
)
from concinno.ziq_autotune_registry import (  # noqa: F401
    TUNABLE_REGISTRY,
    TunableSpec,
    get_tuner,
    list_targets,
)
from concinno.ziq_autotuner import (  # noqa: F401
    AutoTuneObservation,
    AutoTuneRegime,
    ZIQAutoTuner,
    is_autotune_enabled,
)

# ── v0.5 legacy API (deprecated since v0.6, removed in v1.0) ─


def __getattr__(name: str):
    """Lazy deprecation warning for v0.5 legacy imports."""
    _LEGACY = {"HookResult", "Pipeline", "GuardFn"}
    if name in _LEGACY:
        _warnings.warn(
            f"concinno.{name} is deprecated since v0.6. "
            f"Use GuardPipeline + BaseGuard instead. Removed in v1.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        from concinno import hook_api  # noqa: F811

        return getattr(hook_api, name)
    raise AttributeError(f"module 'concinno' has no attribute {name!r}")


__all__ = [
    "__version__",
    # v0.6+ (recommended)
    "BaseGuard",
    "GuardAction",
    "GuardCategory",
    "GuardContext",
    "GuardResult",
    "GuardPipeline",
    "create_default_pipeline",
    # 1.4.0 PreToolUse input rewriters
    "BashDryRunRewriter",
    "BashPipeToShellRewriter",
    "WriteSecretFileRewriter",
    # 1.4.0 prompt_hooks (LLM-as-Judge via CC type:prompt hooks)
    "PromptJudge",
    "ALL_JUDGES",
    "HALLUCINATION_JUDGE",
    "EXCUSE_SCANNER_JUDGE",
    "CODE_QUALITY_JUDGE",
    "build_hook_config",
    "install_prompt_hooks",
    "uninstall_prompt_hooks",
    "list_installed_judges",
    "BackupManager",
    "HitTracker",
    "ProtectionLevel",
    "Pruner",
    "StaleDetector",
    "RiverbedMemory",
    "Riverbed",
    "Stake",
    "RecallResult",
    "SkillRouter",
    "STAREngine",
    "create_star_engine",
    "RetrievalTier",
    "RetrievalResult",
    "ConfidenceVerdict",
    "FreshnessScorer",
    "AssociativeIndex",
    "SessionCache",
    "SharedMemoryBus",
    "AdaptiveForgetter",
    "ForgetCandidate",
    "SearchMode",
    "DivergentSearch",
    "DivergentResult",
    "BM25Index",
    "ConfluenceRAG",
    "ConfluencePath",
    "ConfluencePoint",
    "RetrievalProfile",
    "PROFILE_CONFIG",
    "Identity",
    "IdentityProfile",
    "assign_identity",
    "build_identity_context",
    # 1.9.0 FieldRead (selective field extraction)
    "COMPRESS_BREAKEVEN_TOKENS",
    "FieldReadConfig",
    "build_field_context",
    "read_handoff_fields",
    "read_memory_fields",
    # 1.10.0 CBUA UX display codes
    "CbuaCode",
    "cbua_format",
    "cbua_label",
    "cbua_wiredo",
    "cbua_three_layer",
    "cbua_redteam_verdict",
    "cbua_clean_write",
    # 1.14.0 LLM gateway + few-shot + tool loop
    "LLMEscalator",
    "EscalationResult",
    "EscalationExhausted",
    "EscalationTier",
    "TierResult",
    "DEFAULT_CHAIN",
    "escalate",
    "FewshotBank",
    "FewshotCase",
    "DEFAULT_STOP_WORDS",
    "load_bank",
    "retrieve_fewshot",
    "ToolExecutor",
    "Tool",
    "ExecutionState",
    "ToolStep",
    # 2.12.1 release_authorization (separate from destruction_guard)
    "AuthorizationConfig",
    "AuthorizationMode",
    "PUBLISH_PATTERNS",
    "check_authorization",
    "describe_current_config",
    "detect_publish_operation",
    "format_required_string",
    "load_config",
    # 2.12.0 ZIQ auto-tune + GAIA SAS/MAS meta-router
    "ZIQAutoTuner",
    "AutoTuneObservation",
    "AutoTuneRegime",
    "is_autotune_enabled",
    "TUNABLE_REGISTRY",
    "TunableSpec",
    "get_tuner",
    "list_targets",
    "select_arm",
    "record_arm_outcome",
    "sps_arm_scores",
    "ArmFTRL",
    "ARMS",
    "Arm",
    # v0.5 legacy (deprecated, triggers DeprecationWarning)
    "HookResult",
    "Pipeline",
    "GuardFn",
]
