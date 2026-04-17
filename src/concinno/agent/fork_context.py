"""concinno.agent.fork_context — Forked subagent context primitive.

@module agent.fork_context
@responsibility Define the data shape + invariants + state-isolation
    primitive for spawning fork subagents that share the parent's
    Anthropic prompt cache prefix. A fork whose CacheSafeParams are
    byte-identical to the parent's hits the server-side KV cache
    instead of forcing a cold rebuild.

    This module does NOT spawn subagents — that belongs to a future
    `agent.parallel_dispatcher`. This is the primitive layer: immutable
    cache-safe params + per-fork isolated FileStateCache + a
    SubagentContext packet that bundles the two.

@dependencies stdlib only (dataclasses, hashlib, json, typing)
@exports CacheSafeParams, FileStateCache, SubagentContext,
    create_cache_safe_params, create_subagent_context,
    is_in_fork_child, ForkDepthExceeded

Ported from Claude Code's TypeScript source:
  - utils/forkedAgent.ts   (CacheSafeParams, createSubagentContext)
  - utils/fileStateCache.ts (cloneFileStateCache semantics)

Design notes:
  - CacheSafeParams is frozen. Mutating any cache-affecting field
    invalidates the Anthropic prompt cache prefix, so we refuse to
    allow accidental mutation at the type level.
  - FileStateCache is a generic per-fork mutable dict. On fork, the
    parent's cache is SHALLOW-cloned so the fork can mutate its own
    copy without corrupting the parent's dedup state (and vice
    versa). Value-level deep-copy is the caller's problem — this
    mirrors the JS/TS behavior where LRU entries are carried by
    reference.
  - `fork_depth` and `parent_session_id` are metadata and are NOT
    part of the cache-affecting field comparison done by
    `CacheSafeParams.identical_to`. They can differ between parent
    and child without invalidating the KV prefix.
  - `ForkDepthExceeded` is defined here as the canonical exception
    type, but the guard that raises it lives in the dispatcher.
    Context creation is always safe; spawning is the guarded step.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Generic, Iterator, TypeVar

__all__ = [
    "ANTHROPIC_MAX_CACHE_BREAKPOINTS",
    "FORK_PLACEHOLDER_RESULT",
    "CacheSafeParams",
    "FileStateCache",
    "RenderedPromptCache",
    "SubagentContext",
    "create_cache_safe_params",
    "create_subagent_context",
    "estimate_cache_savings",
    "insert_cache_breakpoints",
    "is_in_fork_child",
    "normalize_tool_result_for_cache",
    "render_for_fork_byte_exact",
    "ForkDepthExceeded",
]

# Matches Claude Code `forkSubagent.ts` FORK_PLACEHOLDER_RESULT constant —
# all fork siblings MUST carry this exact byte string in their tool_result
# blocks so the prefix is identical across siblings.
FORK_PLACEHOLDER_RESULT = "Fork started — processing in background"

# Anthropic prompt cache supports up to 4 cache_control: ephemeral markers
# per request. Exceeding this is an API-level error, so the helper below
# caps the breakpoint count at this constant.
ANTHROPIC_MAX_CACHE_BREAKPOINTS = 4

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# CacheSafeParams
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CacheSafeParams:
    """The subset of a request that MUST be byte-identical between
    parent and fork for the Anthropic prompt cache prefix to hit.

    Any field changing invalidates the server-side KV prefix. These
    are the fields `forkedAgent.ts` L57-L80 carries forward to a
    fork's request.

    Fields:
        system_prompt: The full system prompt text, frozen at fork
            time. Drifting by even a whitespace character causes a
            cache miss.
        tool_defs_hash: sha256 of the canonicalized tool definitions
            list. Canonicalization is `json.dumps(..., sort_keys=True,
            ensure_ascii=False)` so mere key-order shuffling does NOT
            invalidate.
        tool_names: Tuple of tool names in declared order. This is
            for introspection/logging only and is NOT used by
            `identical_to` — ordering changes are tolerated because
            the server sees the canonicalized JSON blob.
        betas: Tuple of anthropic-beta header values in the order
            the parent used them. Order IS cache-affecting.
        effort_value: Extended-thinking effort tier string (e.g.
            "low" / "medium" / "high"). Part of the thinking config
            that the cache key covers.
        parent_session_id: Metadata pointer back to the parent
            session. NOT compared by `identical_to`.
        fork_depth: 0 for the parent, 1 for a first-level fork,
            N for N-levels-deep. NOT compared by `identical_to`.

    Immutability:
        The dataclass is frozen, so instances are hashable and can be
        used as dict keys or put in sets. Any mutation attempt raises
        `FrozenInstanceError`.
    """

    system_prompt: str
    tool_defs_hash: str
    tool_names: tuple[str, ...]
    betas: tuple[str, ...]
    effort_value: str
    parent_session_id: str
    fork_depth: int

    def identical_to(self, other: "CacheSafeParams") -> bool:
        """Return True iff all cache-affecting fields match.

        `fork_depth` and `parent_session_id` are metadata and are
        intentionally excluded — a first-level fork with the same
        prompt/tools/betas as its parent should still register as
        cache-identical.
        """
        return (
            self.system_prompt == other.system_prompt
            and self.tool_defs_hash == other.tool_defs_hash
            and self.betas == other.betas
            and self.effort_value == other.effort_value
        )


# --------------------------------------------------------------------------- #
# FileStateCache
# --------------------------------------------------------------------------- #


class FileStateCache(Generic[T]):
    """Generic per-fork isolated cache with a shallow `clone()`.

    Purpose:
        When a parent spawns a fork subagent, the parent's file-read
        dedup cache (typically ``path -> {mtime, content, hash}``)
        must be CLONED, not shared by reference. A fork writing into
        a shared cache would pollute the parent's dedup view (and
        vice versa), breaking both sides' read-before-edit
        consistency invariants.

    Clone semantics:
        `clone()` returns a new `FileStateCache` with a NEW backing
        dict that contains the same key/value pairs as self. The
        stored values themselves are NOT deep-copied — if T is a
        mutable structure and both the parent and the fork mutate
        the same value object, they will see each other's changes.

        Callers should either:
          1. Treat T as immutable (recommended — ported JS/TS
             `FileState` records are effectively read-only once
             written), or
          2. Deep-copy T values themselves before storing.

        This mirrors `cloneFileStateCache` in
        `utils/fileStateCache.ts`, which loads an LRU dump into a
        new LRU instance — the entry records are shared by
        reference, but the cache containers are separate.

    Ergonomics:
        Supports `len()`, `in`, and iteration over keys.
    """

    __slots__ = ("_store",)

    def __init__(self) -> None:
        self._store: dict[str, T] = {}

    def get(self, key: str) -> T | None:
        return self._store.get(key)

    def set(self, key: str, value: T) -> None:
        self._store[key] = value

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def keys(self) -> list[str]:
        return list(self._store.keys())

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: object) -> bool:
        return key in self._store

    def __iter__(self) -> Iterator[str]:
        return iter(self._store)

    def clone(self) -> "FileStateCache[T]":
        """Return a new `FileStateCache` with an independent dict.

        The new cache contains the same key/value pairs as self. The
        stored values are NOT deep-copied — see the class docstring
        for the sharing-semantics warning.
        """
        new_cache: FileStateCache[T] = FileStateCache()
        new_cache._store = dict(self._store)
        return new_cache


# --------------------------------------------------------------------------- #
# SubagentContext
# --------------------------------------------------------------------------- #


@dataclass
class SubagentContext:
    """The full packet a fork subagent receives from its parent.

    Combines the prefix-critical immutable `CacheSafeParams` with
    the mutable-but-isolated `FileStateCache` and a transcript
    pointer. This is the Python analogue of the object
    `createSubagentContext` in `utils/forkedAgent.ts` returns.

    Fields:
        params: Cache-safe request fingerprint (frozen).
        file_state: Per-fork isolated file-read dedup cache.
            Mutations here do not leak to the parent.
        parent_transcript_ref: Opaque pointer (path or ID) back to
            the parent's transcript record. Used by dispatchers to
            stitch sidechain logs together.
        metadata: Free-form per-fork metadata (labels, tracing ids,
            analytics counters). Intentionally mutable — callers
            append to it during the fork's run.
    """

    params: CacheSafeParams
    file_state: FileStateCache[Any]
    parent_transcript_ref: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def clone_for_child(self, *, increment_depth: bool = True) -> "SubagentContext":
        """Produce a new context for spawning a grandchild fork.

        Re-clones the file state, copies metadata (shallow), and
        bumps `fork_depth` by 1 unless `increment_depth=False`. The
        depth flag exists for edge cases where a dispatcher wants
        to chain contexts without the child counting as a deeper
        level (e.g. ephemeral speculation passes).

        Returns a new `SubagentContext` — does not mutate self.
        """
        new_depth = self.params.fork_depth + (1 if increment_depth else 0)
        new_params = CacheSafeParams(
            system_prompt=self.params.system_prompt,
            tool_defs_hash=self.params.tool_defs_hash,
            tool_names=self.params.tool_names,
            betas=self.params.betas,
            effort_value=self.params.effort_value,
            parent_session_id=self.params.parent_session_id,
            fork_depth=new_depth,
        )
        return SubagentContext(
            params=new_params,
            file_state=self.file_state.clone(),
            parent_transcript_ref=self.parent_transcript_ref,
            metadata=dict(self.metadata),
        )


# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #


def create_cache_safe_params(
    *,
    system_prompt: str,
    tool_defs: list[dict[str, Any]],
    betas: tuple[str, ...] = (),
    effort: str = "",
    parent_session_id: str = "",
    fork_depth: int = 0,
) -> CacheSafeParams:
    """Build a `CacheSafeParams` from raw request components.

    The tool-defs list is canonicalized via
    ``json.dumps(tool_defs, sort_keys=True, ensure_ascii=False)``
    before hashing so that purely cosmetic re-ordering of keys
    inside a tool def does NOT change `tool_defs_hash`. (The order
    of the tool defs in the OUTER list IS cache-affecting and is
    preserved — we do not sort the list itself.)

    Tool names are extracted in declared order, falling back to
    ``f"tool_{i}"`` when a def has no ``name`` field.
    """
    canonical = json.dumps(tool_defs, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    names: list[str] = []
    for i, td in enumerate(tool_defs):
        raw_name = td.get("name") if isinstance(td, dict) else None
        if isinstance(raw_name, str) and raw_name:
            names.append(raw_name)
        else:
            names.append(f"tool_{i}")

    return CacheSafeParams(
        system_prompt=system_prompt,
        tool_defs_hash=digest,
        tool_names=tuple(names),
        betas=tuple(betas),
        effort_value=effort,
        parent_session_id=parent_session_id,
        fork_depth=fork_depth,
    )


def create_subagent_context(
    *,
    parent_params: CacheSafeParams,
    parent_file_state: FileStateCache[Any],
    parent_transcript_ref: str,
    metadata: dict[str, Any] | None = None,
) -> SubagentContext:
    """Factory that clones parent state into a fresh `SubagentContext`.

    This is the single public entry point callers should use to
    build the context packet for a fork subagent. It:

      1. Clones `parent_file_state` so the fork's writes do not
         leak back into the parent.
      2. Wraps the (immutable) `parent_params` as-is — the caller
         is responsible for bumping `fork_depth` if they want the
         new context to appear at a deeper level.
      3. Makes a NEW metadata dict rather than sharing the caller's
         reference, so metadata mutations don't leak either.

    It does NOT mutate `parent_params` or `parent_file_state`.
    """
    return SubagentContext(
        params=parent_params,
        file_state=parent_file_state.clone(),
        parent_transcript_ref=parent_transcript_ref,
        metadata=dict(metadata) if metadata is not None else {},
    )


# --------------------------------------------------------------------------- #
# Helpers & exceptions
# --------------------------------------------------------------------------- #


def is_in_fork_child(context: SubagentContext | None) -> bool:
    """Return True iff `context` is a fork child (depth > 0).

    Mirrors `isInForkChild` on the TypeScript side. A fork child
    that tries to fork further should be rejected by the
    dispatcher based on this check plus a `max_fork_depth`
    constant. This helper does not raise — it only reports.
    """
    return context is not None and context.params.fork_depth > 0


class ForkDepthExceeded(RuntimeError):
    """Raised when a spawn would exceed the configured max fork depth.

    Defined here as the canonical exception type so both the
    dispatcher and any test fixtures can import it from one place.
    This module itself NEVER raises it — context creation is always
    safe. The guard lives at the spawn call site.
    """


# --------------------------------------------------------------------------- #
# Byte-exact prompt cache rendering
# --------------------------------------------------------------------------- #
#
# Background: Claude Code's `forkSubagent.ts` (lines 60-171) implements the
# fork prompt cache strategy on the TS side. Ten sibling fork children are
# cheaper than one independent subagent because Anthropic's prompt cache
# matches prefixes by BYTE, not by semantic equality. Any non-deterministic
# rendering — UUIDs, timestamps, random call_ids — breaks the prefix match
# and forces a cold KV rebuild per sibling.
#
# This section ports the strategy:
#   1. RenderedPromptCache snapshots the parent's rendered bytes.
#   2. normalize_tool_result_for_cache strips non-deterministic fields from
#      tool_result blocks so siblings share identical bytes.
#   3. insert_cache_breakpoints marks up to 4 ephemeral cache positions at
#      strategic prefix boundaries (system end, tools end, history end).
#   4. render_for_fork_byte_exact assembles an Anthropic API params dict
#      whose only per-sibling delta is the final directive text block.
#   5. estimate_cache_savings gives a back-of-envelope cost delta vs
#      running N independent subagents.


@dataclass(frozen=True)
class RenderedPromptCache:
    """Snapshot of the parent's rendered prompt bytes for fork reuse.

    Captures the byte-exact materialization of the parent's system
    prompt, tool definitions pool, and conversation tool_result
    placeholders so each fork child can re-use the same prefix bytes
    without re-rendering.

    Fields:
        system_bytes: UTF-8 bytes of the fully rendered system prompt.
            Drifting by even one whitespace character forces a cold
            cache rebuild, so the caller is responsible for freezing
            these bytes at parent render time.
        tools_pool_bytes: UTF-8 bytes of the canonicalized tool
            definitions section. Canonicalization uses the same
            ``json.dumps(..., sort_keys=True, ensure_ascii=False)``
            convention as :func:`create_cache_safe_params` so key
            shuffling does not bust the prefix.
        placeholder_tool_results: Ordered list of UTF-8 bytes, one
            entry per tool_use block the parent emitted. Every entry
            uses the identical placeholder text (see
            :data:`FORK_PLACEHOLDER_RESULT`) so all siblings share
            the same conversation replay prefix.
        cache_breakpoints: Byte offsets at which the caller inserted
            ``cache_control: ephemeral`` markers. Informational —
            used by the dispatcher's observability layer and by
            :func:`estimate_cache_savings` to show where the savings
            land. The list has at most
            :data:`ANTHROPIC_MAX_CACHE_BREAKPOINTS` entries.

    Immutability:
        The dataclass is frozen. Mutation raises
        ``FrozenInstanceError``. The ``list`` and ``bytes`` fields are
        treated as immutable by convention — callers must not mutate
        the list in place after construction.
    """

    system_bytes: bytes
    tools_pool_bytes: bytes
    placeholder_tool_results: list[bytes]
    cache_breakpoints: list[int]


def normalize_tool_result_for_cache(tool_result: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``tool_result`` with non-deterministic fields scrubbed.

    Replaces the following fields with deterministic placeholders so
    all sibling fork children produce the same bytes:

    ====================  =======================================
    Field                 Replacement
    ====================  =======================================
    ``tool_use_id``       ``"tool_use_fork_placeholder"``
    ``id``                ``"fork_placeholder_id"``
    ``timestamp``         ``0``
    ``created_at``        ``0``
    ``call_id``           ``"call_fork_placeholder"``
    ====================  =======================================

    The block's ``content`` is rewritten so any text child is replaced
    with :data:`FORK_PLACEHOLDER_RESULT`. Non-text children (images,
    etc.) are dropped because they carry variable binary data that
    almost always breaks byte equality — callers that need to preserve
    them should pre-hash and deduplicate before calling this function.

    Returns a NEW dict — the input is not mutated.
    """
    out: dict[str, Any] = dict(tool_result)

    if "tool_use_id" in out:
        out["tool_use_id"] = "tool_use_fork_placeholder"
    if "id" in out:
        out["id"] = "fork_placeholder_id"
    if "call_id" in out:
        out["call_id"] = "call_fork_placeholder"
    if "timestamp" in out:
        out["timestamp"] = 0
    if "created_at" in out:
        out["created_at"] = 0

    # Force the block type to tool_result if missing so downstream
    # renderers key off a stable field.
    out.setdefault("type", "tool_result")

    # Rewrite content to a single placeholder text block. This mirrors
    # forkSubagent.ts lines 141-151 exactly.
    out["content"] = [
        {"type": "text", "text": FORK_PLACEHOLDER_RESULT},
    ]
    return out


def insert_cache_breakpoints(
    blocks: list[dict[str, Any]],
    *,
    max_breakpoints: int = ANTHROPIC_MAX_CACHE_BREAKPOINTS,
) -> list[dict[str, Any]]:
    """Return a copy of ``blocks`` with ``cache_control: ephemeral`` markers.

    Anthropic's prompt cache supports up to
    :data:`ANTHROPIC_MAX_CACHE_BREAKPOINTS` cache_control markers per
    request. Each marker tells the server "cache everything up to
    here"; subsequent identical prefixes then hit cheap.

    Strategy — place markers at these prefix boundaries, in order of
    priority, until the budget is exhausted:

      1. End of the last ``system`` / ``text`` block (the rendered
         system prompt boundary)
      2. End of the last block whose ``type`` is ``"tool"`` or
         ``"tool_definition"`` (tool pool boundary)
      3. End of the last ``tool_result`` block (conversation replay
         boundary, right before the per-child directive)
      4. End of the last block regardless of type (final fallback)

    The strategy is intentionally conservative: if the same boundary
    position is selected twice, the second attempt is skipped so
    adjacent markers do not waste budget on the same byte offset.

    Parameters:
        blocks: Ordered list of Anthropic content blocks (as dicts).
        max_breakpoints: Upper bound on markers inserted. Defaults to
            the API ceiling; tests can lower it.

    Returns a NEW list of NEW dicts — the input blocks are not
    mutated. Each marked block gets a ``cache_control`` field equal to
    ``{"type": "ephemeral"}``.
    """
    if max_breakpoints <= 0 or not blocks:
        return [dict(b) for b in blocks]

    cap = min(max_breakpoints, ANTHROPIC_MAX_CACHE_BREAKPOINTS)

    def _last_index_where(predicate: Any) -> int:
        for i in range(len(blocks) - 1, -1, -1):
            if predicate(blocks[i]):
                return i
        return -1

    system_idx = _last_index_where(
        lambda b: b.get("type") == "system" or b.get("role") == "system",
    )
    tools_idx = _last_index_where(
        lambda b: b.get("type") in {"tool", "tool_definition"},
    )
    history_idx = _last_index_where(
        lambda b: b.get("type") == "tool_result",
    )
    fallback_idx = len(blocks) - 1

    candidate_order = [system_idx, tools_idx, history_idx, fallback_idx]

    chosen: list[int] = []
    for idx in candidate_order:
        if idx < 0 or idx in chosen:
            continue
        chosen.append(idx)
        if len(chosen) >= cap:
            break

    # Copy blocks and attach cache_control on chosen indices.
    out: list[dict[str, Any]] = [dict(b) for b in blocks]
    for idx in chosen:
        out[idx]["cache_control"] = {"type": "ephemeral"}
    return out


def render_for_fork_byte_exact(
    context: SubagentContext,
    parent_rendered: RenderedPromptCache,
    child_directive: str,
    *,
    model: str = "inherit",
    max_tokens: int = 8192,
) -> dict[str, Any]:
    """Render a fork child request using the parent's exact prefix bytes.

    This is the Python port of Claude Code's
    ``buildForkedMessages`` + ``override.systemPrompt`` path
    (``forkSubagent.ts`` lines 60-171). The returned dict is shaped
    for the Anthropic Messages API and has three design invariants:

      1. The ``system`` field is a list of blocks whose text is the
         parent's already-rendered system prompt bytes (decoded back
         to str since the Messages API takes str, not bytes). The
         final block carries ``cache_control: ephemeral`` so the
         server caches the whole system prefix.
      2. The ``messages`` list contains one ``user`` turn whose
         content is the parent's placeholder tool_results (identical
         bytes across siblings) followed by exactly one text block
         containing ``child_directive``. The directive is the ONLY
         thing that differs between siblings.
      3. No non-deterministic fields leak into the prefix — call_ids,
         timestamps, UUIDs have all been scrubbed by
         :func:`normalize_tool_result_for_cache` upstream.

    Parameters:
        context: The :class:`SubagentContext` this fork child belongs
            to. Used to read ``params`` for betas / effort.
        parent_rendered: Snapshot of the parent's rendered prefix
            bytes. See :class:`RenderedPromptCache`.
        child_directive: The per-sibling directive string. This is
            the only byte range that differs between siblings.
        model: Anthropic model id. Defaults to ``"inherit"`` —
            callers running the real API should pass an actual id.
        max_tokens: Response cap. The API requires this field.

    Returns an Anthropic API params dict with keys: ``model``,
    ``max_tokens``, ``system``, ``messages``. When ``context.params``
    carries betas or a thinking effort, they are forwarded as
    ``betas`` and ``thinking`` respectively. The dict is safe to feed
    to the Messages API after adding authentication headers.
    """
    # Decode the parent's system bytes back to str. The caller froze
    # them at render time so decode is lossless.
    system_text = parent_rendered.system_bytes.decode("utf-8")

    system_blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        },
    ]

    # Rebuild placeholder tool_results from the byte snapshot. Each
    # entry is already byte-identical across siblings.
    tool_result_blocks: list[dict[str, Any]] = []
    for i, raw in enumerate(parent_rendered.placeholder_tool_results):
        placeholder_text = raw.decode("utf-8") if raw else FORK_PLACEHOLDER_RESULT
        tool_result_blocks.append(
            {
                "type": "tool_result",
                "tool_use_id": f"tool_use_fork_placeholder_{i}",
                "content": [{"type": "text", "text": placeholder_text}],
            }
        )

    # Final block: the per-child directive. This is the only
    # sibling-unique byte range.
    user_content: list[dict[str, Any]] = [
        *tool_result_blocks,
        {"type": "text", "text": child_directive},
    ]

    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": [
            {"role": "user", "content": user_content},
        ],
    }

    if context.params.betas:
        params["betas"] = list(context.params.betas)
    if context.params.effort_value:
        params["thinking"] = {"type": "enabled", "effort": context.params.effort_value}

    return params


def estimate_cache_savings(
    n_siblings: int,
    shared_prefix_tokens: int,
    unique_directive_tokens: int,
    *,
    input_price_per_mtok: float = 3.0,
    cache_read_discount: float = 0.10,
) -> dict[str, float]:
    """Estimate the prompt cache savings from running N fork siblings.

    Back-of-envelope cost model aligned with Anthropic's public
    pricing (March 2026): cache reads are billed at 10% of the normal
    input price. Cache writes (the first sibling that populates the
    prefix) are billed at the normal input rate — the discount only
    applies to subsequent reads.

    Parameters:
        n_siblings: Number of fork children that share the same
            prefix. Must be >= 1.
        shared_prefix_tokens: Tokens in the prefix that is byte-equal
            across siblings (system prompt + tools + placeholder
            tool_results).
        unique_directive_tokens: Tokens in the per-child directive
            text block. Paid in full for every sibling.
        input_price_per_mtok: USD per million input tokens at the
            normal rate. Defaults to $3/Mtok (Sonnet ballpark).
        cache_read_discount: Multiplier applied to the input price
            for cache reads. Defaults to 0.10 (90% off).

    Returns a dict with:

      - ``baseline_usd``: Cost of running ``n_siblings`` independent
        subagents that each re-render the prefix from scratch.
      - ``fork_usd``: Cost of running them as fork siblings where the
        first pays full price and siblings 2..N hit the cache.
      - ``saved_usd``: ``baseline_usd - fork_usd``.
      - ``saved_tokens``: Token count avoided via cache hits.
      - ``hit_ratio``: ``saved_tokens`` / (``n_siblings`` *
        ``shared_prefix_tokens``). 0.0 when ``n_siblings == 1``.

    Raises ``ValueError`` when inputs are negative or ``n_siblings``
    is zero.
    """
    if n_siblings < 1:
        raise ValueError(f"n_siblings must be >= 1 (got {n_siblings})")
    if shared_prefix_tokens < 0 or unique_directive_tokens < 0:
        raise ValueError("token counts must be non-negative")

    per_mtok = input_price_per_mtok / 1_000_000.0

    baseline_prefix_tokens = n_siblings * shared_prefix_tokens
    fork_prefix_tokens = shared_prefix_tokens  # one cache write
    cache_read_tokens = (n_siblings - 1) * shared_prefix_tokens
    directive_tokens = n_siblings * unique_directive_tokens

    baseline_usd = (baseline_prefix_tokens + directive_tokens) * per_mtok
    fork_usd = (
        fork_prefix_tokens * per_mtok
        + cache_read_tokens * per_mtok * cache_read_discount
        + directive_tokens * per_mtok
    )
    saved_usd = baseline_usd - fork_usd
    saved_tokens = float(baseline_prefix_tokens - fork_prefix_tokens - cache_read_tokens)

    if n_siblings <= 1 or shared_prefix_tokens == 0:
        hit_ratio = 0.0
    else:
        # Fraction of prefix tokens that were avoided entirely (the
        # cache-read tokens still cost 10% but are "hit" for the ratio).
        hit_ratio = (n_siblings - 1) / n_siblings

    return {
        "baseline_usd": baseline_usd,
        "fork_usd": fork_usd,
        "saved_usd": saved_usd,
        "saved_tokens": saved_tokens,
        "hit_ratio": hit_ratio,
    }
