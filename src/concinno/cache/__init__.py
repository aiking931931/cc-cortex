"""concinno prompt cache subsystem.

Re-exports microcompact and cache-break detection primitives.

PEP 562 lazy re-export (2026-04-25):
    Top-level eager imports previously triggered a ~0.6s chain on every
    cold Python process — ``session_memory`` → ``agent.fork_context`` →
    ``agent.mas_loop`` — which every PreToolUse hook invocation paid
    afresh (no daemon mode). Hot-path import ``from concinno.cache.ux_gate
    import is_ux_enabled`` alone triggered the full chain via this
    ``__init__`` side-effect, dominating tool-call latency.

    The public surface below (identical to the previous eager version) is
    now resolved lazily via :pep:`562` ``__getattr__`` — unused symbols
    cost nothing, but ``from concinno.cache import X`` still works.

    See ``MEMORY #110`` / ``feedback_on_pre_tool_hot_path.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# ── Lazy symbol → submodule map ─────────────────────────────────────
# Format: ``symbol_name: (submodule_suffix, attribute_name_in_submodule)``
# ``attribute_name`` differs from symbol name only for aliased re-exports
# (``APPEND_LOG_SCHEMA_VERSION`` → ``SCHEMA_VERSION`` / ``L2DistillSink``
# → ``DistillSink``).

_LAZY_SYMBOLS: dict[str, tuple[str, str]] = {
    # anthropic_sink
    "AnthropicCacheEditSink": ("anthropic_sink", "AnthropicCacheEditSink"),
    "MessageTransform": ("anthropic_sink", "MessageTransform"),
    # append_only_log
    "APPEND_LOG_SCHEMA_VERSION": ("append_only_log", "SCHEMA_VERSION"),
    "AppendOnlyLog": ("append_only_log", "AppendOnlyLog"),
    "LogEvent": ("append_only_log", "LogEvent"),
    # autocompact
    "AUTOCOMPACT_BUFFER_TOKENS": ("autocompact", "AUTOCOMPACT_BUFFER_TOKENS"),
    "DEFAULT_MODEL_BUDGETS": ("autocompact", "DEFAULT_MODEL_BUDGETS"),
    "ERROR_THRESHOLD_BUFFER_TOKENS": ("autocompact", "ERROR_THRESHOLD_BUFFER_TOKENS"),
    "MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES": ("autocompact", "MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES"),
    "RECURSION_GUARDED_SOURCES": ("autocompact", "RECURSION_GUARDED_SOURCES"),
    "WARNING_THRESHOLD_BUFFER_TOKENS": ("autocompact", "WARNING_THRESHOLD_BUFFER_TOKENS"),
    "AutoCompactCircuitBreaker": ("autocompact", "AutoCompactCircuitBreaker"),
    "AutoCompactExhausted": ("autocompact", "AutoCompactExhausted"),
    "AutoCompactor": ("autocompact", "AutoCompactor"),
    "AutoCompactState": ("autocompact", "AutoCompactState"),
    "CompactRequest": ("autocompact", "CompactRequest"),
    "CompactResult": ("autocompact", "CompactResult"),
    "CompactSink": ("autocompact", "CompactSink"),
    "ContextCollapseActive": ("autocompact", "ContextCollapseActive"),
    "QuerySource": ("autocompact", "QuerySource"),
    # cache_break_detector
    "BreakReport": ("cache_break_detector", "BreakReport"),
    "CacheBreakDetector": ("cache_break_detector", "CacheBreakDetector"),
    "CacheBreakReason": ("cache_break_detector", "CacheBreakReason"),
    "PreviousState": ("cache_break_detector", "PreviousState"),
    "hash_field": ("cache_break_detector", "hash_field"),
    "hash_per_tool": ("cache_break_detector", "hash_per_tool"),
    # cognitive_pool
    "DEFAULT_MAX_SECTION_BYTES": ("cognitive_pool", "DEFAULT_MAX_SECTION_BYTES"),
    "DEFAULT_MAX_SECTIONS": ("cognitive_pool", "DEFAULT_MAX_SECTIONS"),
    "DEFAULT_POOL_FILENAME": ("cognitive_pool", "DEFAULT_POOL_FILENAME"),
    "DEFAULT_SECTION_TTL_S": ("cognitive_pool", "DEFAULT_SECTION_TTL_S"),
    "SECTION_FOOTER": ("cognitive_pool", "SECTION_FOOTER"),
    "SECTION_HEADER_PREFIX": ("cognitive_pool", "SECTION_HEADER_PREFIX"),
    "SECTION_HEADER_SUFFIX": ("cognitive_pool", "SECTION_HEADER_SUFFIX"),
    "CognitivePool": ("cognitive_pool", "CognitivePool"),
    "PoolCorrupt": ("cognitive_pool", "PoolCorrupt"),
    "PoolFull": ("cognitive_pool", "PoolFull"),
    "PoolSection": ("cognitive_pool", "PoolSection"),
    "PoolStats": ("cognitive_pool", "PoolStats"),
    # l2_distill
    "DistillationFailed": ("l2_distill", "DistillationFailed"),
    "DistillCandidate": ("l2_distill", "DistillCandidate"),
    "DistillRequest": ("l2_distill", "DistillRequest"),
    "DistillResult": ("l2_distill", "DistillResult"),
    "EvolveRecord": ("l2_distill", "EvolveRecord"),
    "L2Distiller": ("l2_distill", "L2Distiller"),
    "L2Stats": ("l2_distill", "L2Stats"),
    "RawHit": ("l2_distill", "RawHit"),
    "L2DistillSink": ("l2_distill", "DistillSink"),
    # memdir
    "DEFAULT_MAX_BYTES_PER_FILE": ("memdir", "DEFAULT_MAX_BYTES_PER_FILE"),
    "DEFAULT_MAX_ENTRYPOINT_BYTES": ("memdir", "DEFAULT_MAX_ENTRYPOINT_BYTES"),
    "DEFAULT_MAX_ENTRYPOINT_LINES": ("memdir", "DEFAULT_MAX_ENTRYPOINT_LINES"),
    "DEFAULT_MAX_LINES_PER_FILE": ("memdir", "DEFAULT_MAX_LINES_PER_FILE"),
    "DEFAULT_MEMDIR_NAME": ("memdir", "DEFAULT_MEMDIR_NAME"),
    "ENTRYPOINT_FILENAME": ("memdir", "ENTRYPOINT_FILENAME"),
    "Memdir": ("memdir", "Memdir"),
    "MemdirStats": ("memdir", "MemdirStats"),
    "MemoryEntry": ("memdir", "MemoryEntry"),
    # microcompact
    "COMPACTABLE_TOOLS": ("microcompact", "COMPACTABLE_TOOLS"),
    "POST_COMPACT_MAX_FILES_TO_RESTORE": ("microcompact", "POST_COMPACT_MAX_FILES_TO_RESTORE"),
    "POST_COMPACT_MAX_TOKENS_PER_FILE": ("microcompact", "POST_COMPACT_MAX_TOKENS_PER_FILE"),
    "POST_COMPACT_MAX_TOKENS_PER_SKILL": ("microcompact", "POST_COMPACT_MAX_TOKENS_PER_SKILL"),
    "POST_COMPACT_SKILLS_TOKEN_BUDGET": ("microcompact", "POST_COMPACT_SKILLS_TOKEN_BUDGET"),
    "POST_COMPACT_TOKEN_BUDGET": ("microcompact", "POST_COMPACT_TOKEN_BUDGET"),
    "SPARSE_TRUNCATION_MARKER": ("microcompact", "SPARSE_TRUNCATION_MARKER"),
    "TIME_BASED_MC_CLEARED_MESSAGE": ("microcompact", "TIME_BASED_MC_CLEARED_MESSAGE"),
    "CachedMCState": ("microcompact", "CachedMCState"),
    "CacheEdit": ("microcompact", "CacheEdit"),
    "CacheEditAction": ("microcompact", "CacheEditAction"),
    "CacheEditSink": ("microcompact", "CacheEditSink"),
    "FileAccessRecord": ("microcompact", "FileAccessRecord"),
    "FileSparseEntry": ("microcompact", "FileSparseEntry"),
    "Microcompactor": ("microcompact", "Microcompactor"),
    "SectionEdit": ("microcompact", "SectionEdit"),
    "SkillEntry": ("microcompact", "SkillEntry"),
    "SkillSparseEntry": ("microcompact", "SkillSparseEntry"),
    "SparseRestoreConfig": ("microcompact", "SparseRestoreConfig"),
    "ToolCall": ("microcompact", "ToolCall"),
    "compact_all": ("microcompact", "compact_all"),
    "compact_if_needed": ("microcompact", "compact_if_needed"),
    # session_memory
    "DEFAULT_INIT_TOOL_COUNT": ("session_memory", "DEFAULT_INIT_TOOL_COUNT"),
    "DEFAULT_MAX_MD_BYTES": ("session_memory", "DEFAULT_MAX_MD_BYTES"),
    "DEFAULT_MAX_MD_LINES": ("session_memory", "DEFAULT_MAX_MD_LINES"),
    "DEFAULT_MD_FILENAME": ("session_memory", "DEFAULT_MD_FILENAME"),
    "DEFAULT_UPDATE_TOOL_COUNT": ("session_memory", "DEFAULT_UPDATE_TOOL_COUNT"),
    "DistillInput": ("session_memory", "DistillInput"),
    "DistillOutput": ("session_memory", "DistillOutput"),
    "DistillSink": ("session_memory", "DistillSink"),
    "SessionMemory": ("session_memory", "SessionMemory"),
    "SessionMemoryState": ("session_memory", "SessionMemoryState"),
}


def __getattr__(name: str) -> Any:
    """PEP 562 lazy re-export — load submodule only on first access.

    Unknown names raise ``AttributeError`` per the PEP. Successful lookups
    cache the resolved symbol on the package module so subsequent accesses
    short-circuit the dict lookup and import call.
    """
    entry = _LAZY_SYMBOLS.get(name)
    if entry is None:
        raise AttributeError(f"module 'concinno.cache' has no attribute {name!r}")
    submod_suffix, attr_name = entry
    import importlib

    submodule = importlib.import_module(f"concinno.cache.{submod_suffix}")
    value = getattr(submodule, attr_name)
    globals()[name] = value  # cache on package for fast repeat access
    return value


def __dir__() -> list[str]:
    """Include lazy symbols in ``dir(concinno.cache)`` for discoverability."""
    return sorted(set(list(globals().keys()) + list(_LAZY_SYMBOLS.keys())))


if TYPE_CHECKING:  # pragma: no cover - IDE / mypy only
    # Static imports for type-checkers. Never executed at runtime.
    from concinno.cache.anthropic_sink import AnthropicCacheEditSink, MessageTransform  # noqa: F401
    from concinno.cache.append_only_log import (  # noqa: F401
        SCHEMA_VERSION as APPEND_LOG_SCHEMA_VERSION,
    )
    from concinno.cache.append_only_log import AppendOnlyLog, LogEvent  # noqa: F401
    from concinno.cache.autocompact import (  # noqa: F401
        AUTOCOMPACT_BUFFER_TOKENS,
        DEFAULT_MODEL_BUDGETS,
        ERROR_THRESHOLD_BUFFER_TOKENS,
        MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
        RECURSION_GUARDED_SOURCES,
        WARNING_THRESHOLD_BUFFER_TOKENS,
        AutoCompactCircuitBreaker,
        AutoCompactExhausted,
        AutoCompactor,
        AutoCompactState,
        CompactRequest,
        CompactResult,
        CompactSink,
        ContextCollapseActive,
        QuerySource,
    )
    from concinno.cache.cache_break_detector import (  # noqa: F401
        BreakReport,
        CacheBreakDetector,
        CacheBreakReason,
        PreviousState,
        hash_field,
        hash_per_tool,
    )
    from concinno.cache.cognitive_pool import (  # noqa: F401
        DEFAULT_MAX_SECTION_BYTES,
        DEFAULT_MAX_SECTIONS,
        DEFAULT_POOL_FILENAME,
        DEFAULT_SECTION_TTL_S,
        SECTION_FOOTER,
        SECTION_HEADER_PREFIX,
        SECTION_HEADER_SUFFIX,
        CognitivePool,
        PoolCorrupt,
        PoolFull,
        PoolSection,
        PoolStats,
    )
    from concinno.cache.l2_distill import (  # noqa: F401
        DistillationFailed,
        DistillCandidate,
        DistillRequest,
        DistillResult,
        EvolveRecord,
        L2Distiller,
        L2Stats,
        RawHit,
    )
    from concinno.cache.l2_distill import DistillSink as L2DistillSink  # noqa: F401
    from concinno.cache.memdir import (  # noqa: F401
        DEFAULT_MAX_BYTES_PER_FILE,
        DEFAULT_MAX_ENTRYPOINT_BYTES,
        DEFAULT_MAX_ENTRYPOINT_LINES,
        DEFAULT_MAX_LINES_PER_FILE,
        DEFAULT_MEMDIR_NAME,
        ENTRYPOINT_FILENAME,
        Memdir,
        MemdirStats,
        MemoryEntry,
    )
    from concinno.cache.microcompact import (  # noqa: F401
        COMPACTABLE_TOOLS,
        POST_COMPACT_MAX_FILES_TO_RESTORE,
        POST_COMPACT_MAX_TOKENS_PER_FILE,
        POST_COMPACT_MAX_TOKENS_PER_SKILL,
        POST_COMPACT_SKILLS_TOKEN_BUDGET,
        POST_COMPACT_TOKEN_BUDGET,
        SPARSE_TRUNCATION_MARKER,
        TIME_BASED_MC_CLEARED_MESSAGE,
        CachedMCState,
        CacheEdit,
        CacheEditAction,
        CacheEditSink,
        FileAccessRecord,
        FileSparseEntry,
        Microcompactor,
        SectionEdit,
        SkillEntry,
        SkillSparseEntry,
        SparseRestoreConfig,
        ToolCall,
        compact_all,
        compact_if_needed,
    )
    from concinno.cache.session_memory import (  # noqa: F401
        DEFAULT_INIT_TOOL_COUNT,
        DEFAULT_MAX_MD_BYTES,
        DEFAULT_MAX_MD_LINES,
        DEFAULT_MD_FILENAME,
        DEFAULT_UPDATE_TOOL_COUNT,
        DistillInput,
        DistillOutput,
        DistillSink,
        SessionMemory,
        SessionMemoryState,
    )


__all__ = sorted(_LAZY_SYMBOLS.keys())
