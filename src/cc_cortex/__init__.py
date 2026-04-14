"""cc-cortex — The Cognitive Layer for Claude Code.

Public API (v0.6+, recommended)::

    from cc_cortex import BaseGuard, GuardPipeline, create_default_pipeline

Legacy API (v0.5, deprecated — removed in v1.0)::

    from cc_cortex import HookResult, Pipeline

Migration guide: https://github.com/anthropics-community/cc-cortex/blob/main/docs/migration-v05-v06.md
"""

__version__ = "1.17.4"

import warnings as _warnings

from cc_cortex.anti_bloat import (  # noqa: F401
    HitTracker,
    ProtectionLevel,
    Pruner,
    StaleDetector,
)
from cc_cortex.backup_manager import BackupManager  # noqa: F401
from cc_cortex.cbua_ux import (  # noqa: F401
    CbuaCode,
    cbua_clean_write,
    cbua_format,
    cbua_label,
    cbua_redteam_verdict,
    cbua_three_layer,
    cbua_wiredo,
)
from cc_cortex.convention_engine import (  # noqa: F401
    ConventionEngine,
    check_naming,
    suggest_path,
)
from cc_cortex.field_read import (  # noqa: F401
    COMPRESS_BREAKEVEN_TOKENS,
    FieldReadConfig,
    build_field_context,
    read_handoff_fields,
    read_memory_fields,
)

# ── v0.6+ Guard Pipeline API (recommended) ───────────────
from cc_cortex.guards.base import (  # noqa: F401
    BaseGuard,
    GuardAction,
    GuardCategory,
    GuardContext,
    GuardResult,
)
from cc_cortex.guards.pipeline import GuardPipeline  # noqa: F401
from cc_cortex.guards.registry import create_default_pipeline  # noqa: F401
from cc_cortex.guards.rewrite_guards import (  # noqa: F401
    BashDryRunRewriter,
    BashPipeToShellRewriter,
    WriteSecretFileRewriter,
)
from cc_cortex.prompt_hooks import (  # noqa: F401
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
from cc_cortex.riverbed import (  # noqa: F401
    RecallResult,
    Riverbed,
    RiverbedMemory,
    Stake,
)
from cc_cortex.skill_router import SkillRouter  # noqa: F401
from cc_cortex.star import (  # noqa: F401
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
from cc_cortex.structured_handoff import (  # noqa: F401
    HandoffRecord,
    HandoffTemplate,
)
from cc_cortex.subagent_identity import (  # noqa: F401
    Identity,
    IdentityProfile,
    assign_identity,
    build_identity_context,
)
from cc_cortex.token_zone import (  # noqa: F401
    Zone,
    detect_zone,
    read_zone_file,
    should_gate_tool,
    write_zone_file,
    zone_injection,
)
from cc_cortex.library import CortexLibrary, LibraryResult  # noqa: F401

# ── 1.14.0 LLM gateway + few-shot + tool loop ────────────
from cc_cortex.escalation import (  # noqa: F401
    DEFAULT_CHAIN,
    EscalationExhausted,
    EscalationResult,
    EscalationTier,
    LLMEscalator,
    TierResult,
    escalate,
)
from cc_cortex.fewshot import (  # noqa: F401
    DEFAULT_STOP_WORDS,
    FewshotBank,
    FewshotCase,
    load_bank,
    retrieve_fewshot,
)
from cc_cortex.tool_executor import (  # noqa: F401
    ExecutionState,
    Tool,
    ToolExecutor,
    ToolStep,
)

# ── v0.5 legacy API (deprecated since v0.6, removed in v1.0) ─


def __getattr__(name: str):
    """Lazy deprecation warning for v0.5 legacy imports."""
    _LEGACY = {"HookResult", "Pipeline", "GuardFn"}
    if name in _LEGACY:
        _warnings.warn(
            f"cc_cortex.{name} is deprecated since v0.6. "
            f"Use GuardPipeline + BaseGuard instead. Removed in v1.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        from cc_cortex import hook_api  # noqa: F811

        return getattr(hook_api, name)
    raise AttributeError(f"module 'cc_cortex' has no attribute {name!r}")


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
    # v0.5 legacy (deprecated, triggers DeprecationWarning)
    "HookResult",
    "Pipeline",
    "GuardFn",
]
