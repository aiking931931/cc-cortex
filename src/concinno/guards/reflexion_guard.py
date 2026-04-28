"""concinno.guards.reflexion_guard — Reflexion: language-level failure analysis.

@module reflexion_guard
@responsibility Close the C5 Reflexion gap left by ``ConsecutiveFailGuard``.
    On PostToolUse failure, synthesise a short natural-language ``why_failed``
    narrative from sentinel error signature + tool/path context and persist it
    to ``StateStore`` namespace ``reflexion``. On the *next* PreToolUse,
    surface the stored narrative via ``additionalContext`` so the model sees
    its own prior-turn failure analysis verbatim and can break out of
    blind-retry loops.
@dependencies concinno.guards.base, concinno.core.state_store, concinno.sentinel
@exports ReflexionGuard, build_failure_narrative

Design rationale
----------------
``concinno.sentinel`` already extracts machine-readable error signatures
(``edit:old_string_not_found`` / ``bash:tsc:TS2304`` / ``bash:python:TypeError``)
and counts consecutive failures. What it does NOT do is keep a *language*
representation of why the call failed that the LLM can read on the *next*
turn. The CBUA spec ``認知行為統一架構.md`` line 221 calls this out
explicitly: "失敗 → 語言化分析（為什麼錯？）→ 存入 session 記憶 → 重試".

This guard is intentionally cheap:

* No LLM-judge call — narratives are template-rendered from the error
  signature, last-edited file path, and a small lookup table of
  "why this kind of failure happens".
* TTL-counted — narrative is shown for at most ``injection_ttl_calls``
  subsequent calls, then expires. Stale failure context is worse than
  no context.
* Single StateStore namespace ``reflexion`` keyed by session_id.

This is *not* a replacement for sentinel. ConsecutiveFailGuard hard-denies
on N consecutive failures; ReflexionGuard whispers a narrative on every
failure so the next attempt has more context. They run independently.
"""

from __future__ import annotations

from typing import Optional

from concinno.core.state_store import StateStore
from concinno.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)

_NS = "reflexion"

# Default ZIQ-tunable params (mirrored in ziq_autotune_registry.py).
_DEFAULT_MAX_WORDS = 80
_DEFAULT_INJECTION_TTL = 2

# Mapping from sentinel error signature prefix → human-readable hypothesis.
# Kept narrow on purpose: we synthesise a *hint*, not a full diagnosis.
_REASON_TABLE: dict[str, str] = {
    "edit:old_string_not_found": (
        "the exact string was not present in the file — "
        "re-read the current file before retrying the edit"
    ),
    "edit:not_unique": (
        "the old_string matched multiple locations — "
        "include more surrounding context to disambiguate"
    ),
    "edit:no_match": (
        "the edit pattern did not match any region — "
        "the file may have been modified by another tool call"
    ),
    "bash:tsc": (
        "TypeScript compilation failed — "
        "investigate the type error before re-running the same build command"
    ),
    "bash:python": (
        "Python execution raised an exception — "
        "fix the underlying error before retrying"
    ),
    "bash:cmd_not_found": (
        "the command was not on PATH — "
        "install the binary or use a different invocation path"
    ),
    "bash:permission_denied": (
        "the operation was rejected by filesystem or sandbox permissions — "
        "do NOT retry the identical command; choose a different path or scope"
    ),
    "bash:nonzero_exit": (
        "the command returned a non-zero exit — "
        "inspect stderr before re-running"
    ),
    "write:error": (
        "the write failed — "
        "the target path may be locked or the directory may not exist"
    ),
}


def build_failure_narrative(
    *,
    tool_name: str,
    error_sig: str,
    file_path: str = "",
    max_words: int = _DEFAULT_MAX_WORDS,
) -> str:
    """Render a one-line natural-language failure narrative.

    Args:
        tool_name: Tool that failed (Edit / Write / Bash / ...).
        error_sig: Signature from ``sentinel._extract_error_signature``,
            e.g. ``"edit:old_string_not_found"`` or ``"bash:tsc:TS2304"``.
        file_path: Optional file path for additional anchoring.
        max_words: Word cap (ZIQ-tunable).

    Returns:
        A short narrative like
        ``"Edit on src/foo.py failed because the exact string was not
        present in the file — re-read the current file before retrying."``
        Capped to ``max_words`` words.
    """
    if not error_sig:
        # No signature means we have no diagnostic; skip narrative.
        return ""

    # Look up the longest matching prefix.
    reason = ""
    for prefix in sorted(_REASON_TABLE.keys(), key=len, reverse=True):
        if error_sig.startswith(prefix):
            reason = _REASON_TABLE[prefix]
            break
    if not reason:
        reason = (
            f"the call returned signature {error_sig!r} — "
            "treat this as a new problem and stop the blind-retry loop"
        )

    target = f" on {file_path}" if file_path else ""
    narrative = (
        f"Reflexion: prior-turn {tool_name}{target} failed because {reason}."
    )
    words = narrative.split()
    if len(words) > max_words:
        narrative = " ".join(words[:max_words]).rstrip(",.;:") + "..."
    return narrative


def _read_state(ctx: GuardContext) -> dict:
    if not ctx.session_id or not ctx.cache_dir:
        return {}
    store = StateStore(ctx.cache_dir)
    return store.read(_NS, ctx.session_id, default={}) or {}


def _write_state(ctx: GuardContext, state: dict) -> None:
    if not ctx.session_id or not ctx.cache_dir:
        return
    store = StateStore(ctx.cache_dir)
    store.write(_NS, ctx.session_id, state)


class ReflexionGuard(BaseGuard):
    """C5 Reflexion: persist a failure narrative + replay on next PreToolUse.

    Behaviour summary
    -----------------
    * **PostToolUse** — when the tool call failed AND a non-empty error
      signature can be extracted, synthesise a ``why_failed`` narrative
      and store it with ``ttl_remaining = injection_ttl_calls``.
    * **PreToolUse** — when stored state shows ``ttl_remaining > 0``,
      surface the narrative as ``allow_advisory`` context and decrement
      the TTL. When TTL hits zero the narrative is discarded.
    * **Never deny** — pure advisory layer. ConsecutiveFailGuard handles
      the hard-deny path; this is the soft-coaching companion.
    """

    name = "reflexion"
    feature_name = "reflexion_guard"
    category = GuardCategory.COGNITIVE

    def __init__(
        self,
        *,
        max_words: int = _DEFAULT_MAX_WORDS,
        injection_ttl_calls: int = _DEFAULT_INJECTION_TTL,
    ) -> None:
        self._max_words = max(30, min(200, int(max_words)))
        self._ttl = max(1, min(5, int(injection_ttl_calls)))

    # ── PreToolUse: replay stored narrative if TTL > 0 ──
    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        state = _read_state(ctx)
        ttl = int(state.get("ttl_remaining", 0))
        narrative = state.get("why_failed", "")
        if ttl <= 0 or not narrative:
            return None

        # Decrement TTL on replay so the message ages out.
        state["ttl_remaining"] = ttl - 1
        expired = state["ttl_remaining"] <= 0
        if expired:
            # Clear when expired so a stale narrative cannot leak forward.
            state.pop("why_failed", None)
        _write_state(ctx, state)

        # ZIQ outcome wire (4.4.0 — sub-agent K wave-2). The TTL
        # cap controls how many subsequent calls see the narrative.
        # iterations_used = how many replays this narrative has now
        # served (self._ttl - new_ttl). succeeded=True on every
        # replay (the narrative reached its consumer); succeeded=
        # False would correspond to "narrative expired without ever
        # being needed" — that case lives in on_post_tool when the
        # next failure overwrites a not-yet-expired prior narrative.
        try:
            from concinno.ziq_emit_helpers import emit_iteration_outcome

            replays_so_far = int(self._ttl) - state["ttl_remaining"]
            emit_iteration_outcome(
                "reflexion.injection_ttl_calls",
                value=int(self._ttl),
                iterations_used=int(replays_so_far),
                succeeded=True,
                source="concinno.guards.reflexion_guard.ReflexionGuard.check",
                metadata={
                    "expired_after_replay": expired,
                    "ttl_remaining": state["ttl_remaining"],
                },
            )
        except Exception:
            pass

        return GuardResult.allow_advisory(
            context=str(narrative),
            reason="reflexion_replay",
        )

    # ── PostToolUse: capture narrative on failure ──
    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        from concinno.sentinel import _extract_error_signature

        if not ctx.tool_result:
            return None
        error_sig = _extract_error_signature(ctx.tool_name, ctx.tool_result)
        if not error_sig:
            return None

        file_path = ""
        if isinstance(ctx.tool_input, dict):
            file_path = str(
                ctx.tool_input.get("file_path", "")
                or ctx.tool_input.get("path", "")
                or "",
            )

        narrative = build_failure_narrative(
            tool_name=ctx.tool_name,
            error_sig=error_sig,
            file_path=file_path,
            max_words=self._max_words,
        )
        if not narrative:
            return None

        state = _read_state(ctx)
        state["why_failed"] = narrative
        state["error_sig"] = error_sig
        state["ttl_remaining"] = self._ttl
        _write_state(ctx, state)

        # ZIQ outcome wire (4.4.0 — sub-agent K wave-2). The
        # max_words cap controls how aggressive the truncation is.
        # observed = actual word count of the rendered narrative;
        # tripped=True when the narrative had to be truncated to
        # the cap (headroom exhausted = signal the cap may be too
        # tight). Emitting on every successful narrative build
        # gives the FTRL learner enough signal density.
        try:
            from concinno.ziq_emit_helpers import emit_threshold_outcome

            word_count = len(narrative.split())
            truncated = narrative.endswith("...")
            emit_threshold_outcome(
                "reflexion.max_words",
                value=int(self._max_words),
                observed=float(word_count),
                tripped=truncated,
                source="concinno.guards.reflexion_guard.ReflexionGuard",
                metadata={
                    "word_count": word_count,
                    "truncated": truncated,
                },
            )
        except Exception:
            pass

        # PostToolUse signal only — no context injection here.
        return None


__all__ = [
    "ReflexionGuard",
    "build_failure_narrative",
]
