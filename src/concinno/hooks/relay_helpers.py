"""concinno.hooks.relay_helpers — Self-branding for hook warnings.

@module relay_helpers
@responsibility Wrap hook warnings with ``[Concinno: <feature>]`` prefix so
    the user can distinguish Concinno-controlled warnings from genuine
    Claude Code platform anomalies. Eliminates false-alarm panic when
    users see ``[SHOW USER VERBATIM] ⚠ ...`` in transcripts and assume
    the harness is hallucinating.
@dependencies concinno.hooks.io_utils (silent-mode detection)
@exports with_feature_prefix, get_relay_mode, RelayMode

Why this exists
---------------
The ``[SHOW USER VERBATIM]`` marker is a Concinno-internal protocol
token telling the LLM "relay this string to the user verbatim". When a
user looks at their Claude Code transcript and sees a flood of those
warnings without a self-identifying brand, they reasonably assume the
warnings are CC platform errors / hallucinations / prompt-injection
artifacts. They are in fact Concinno hook output behaving correctly.

Per AI King 2026-04-29 directive: every Concinno hook warning relayed
to the LLM context must carry a ``[Concinno: <feature_name>]`` prefix
so the source is unambiguous.

Four modes (FEATURE_META ``verbatim_relay.mode``):

* ``off``     — caller-suppressed; helper returns empty string and the
                caller skips emitting entirely.
* ``silent``  — strip ``[SHOW USER VERBATIM]`` (delegated to existing
                io_utils silent-mode plumbing — relay still produces
                bare text, the silent layer handles suppression).
* ``prefix``  — DEFAULT. Output shape:
                ``[SHOW USER VERBATIM] [Concinno: <feature>] ⚠ <msg>``.
* ``verbose`` — Legacy 4.5.0 shape (no Concinno self-brand) for users
                who genuinely prefer the old behaviour.

Resolution chain (highest priority first):

1. ``CONCINNO_VERBATIM_RELAY_MODE`` env var (one-shot CLI override)
2. ``cfg.feature('verbatim_relay', 'mode')`` from FEATURE_META + user
   ``cc_config.json`` / ``~/.concinno/`` overrides
3. Hard-coded default ``"prefix"``

Why ``cosmetic=True`` in FEATURE_META
-------------------------------------
This is a UX preference (how the warning is *displayed*), not a
quality / safety knob. Per L0 鐵律 #6 ZIQ-vs-manual priority order,
ZIQ FTRL must NOT autotune cosmetic params — the user picks once and
the value sticks until they change it.
"""

from __future__ import annotations

import os
from typing import Literal

# Public type alias so guard authors can annotate cleanly.
RelayMode = Literal["off", "silent", "prefix", "verbose"]

VALID_RELAY_MODES: frozenset[str] = frozenset({"off", "silent", "prefix", "verbose"})

_DEFAULT_MODE: RelayMode = "prefix"
_VERBATIM_TOKEN = "[SHOW USER VERBATIM]"


def get_relay_mode() -> RelayMode:
    """Resolve current verbatim_relay mode.

    Lookup order:

    1. ``CONCINNO_VERBATIM_RELAY_MODE`` env var
    2. ``cfg.feature('verbatim_relay', 'mode')``
    3. ``"prefix"`` fallback

    Invalid values fall through to the next layer (so a typo in env or
    config never crashes a hook). All returns are guaranteed to be in
    ``VALID_RELAY_MODES``.
    """
    env_val = os.environ.get("CONCINNO_VERBATIM_RELAY_MODE")
    if env_val and env_val in VALID_RELAY_MODES:
        # ``in VALID_RELAY_MODES`` already narrowed; cast for the type checker.
        return env_val  # type: ignore[return-value]

    try:
        from concinno.core.config import get_config

        cfg_val = get_config().feature("verbatim_relay", "mode")
        if isinstance(cfg_val, str) and cfg_val in VALID_RELAY_MODES:
            return cfg_val  # type: ignore[return-value]
    except Exception:
        # Config layer unavailable (e.g. partial install / test) → fall
        # through to the safe default. Hook warnings must never crash
        # because the cognitive layer can't bootstrap its own config.
        pass

    return _DEFAULT_MODE


def _is_feature_enabled() -> bool:
    """Single source for the master ``enabled`` switch.

    Even in ``mode=prefix`` the user can disable the feature entirely
    via ``cfg.feature('verbatim_relay', 'enabled') = False``. When
    disabled we behave like ``mode=verbose`` (legacy passthrough) — we
    do NOT silently drop the warning, because that would hide critical
    safety signal. ``mode=off`` is the explicit "drop" path.
    """
    try:
        from concinno.core.config import get_config

        return bool(get_config().feature("verbatim_relay", "enabled"))
    except Exception:
        return True  # Fail-open: unknown state → behave like 4.5.0.


def with_feature_prefix(
    feature_name: str,
    raw_msg: str,
    *,
    mode: RelayMode | None = None,
) -> str:
    """Wrap a hook warning with the ``[Concinno: <feature>]`` self-brand.

    Args:
        feature_name: Source feature (e.g. ``"cbua_pipeline"``,
            ``"butterfly_guard"``, ``"step_back_protocol"``). Should
            match a FEATURE_META key when one exists, but free-form
            strings are accepted for ad-hoc warnings (e.g. context-
            compression detection has no FEATURE_META row).
        raw_msg: The warning text the LLM should relay. May or may
            not already start with ``[SHOW USER VERBATIM]`` — the
            helper normalises both shapes.
        mode: Force a specific mode. ``None`` (default) resolves via
            :func:`get_relay_mode`. Tests use this to lock behaviour.

    Returns:
        The wrapped string ready to drop into ``additionalContext``.
        ``""`` when mode is ``"off"`` (caller MUST skip emit).

    Output shapes
    -------------
    * ``off``     → ``""``
    * ``silent``  → ``"<msg>"`` (no verbatim token; io_utils strips
                                 the rest in silent mode)
    * ``prefix``  → ``"[SHOW USER VERBATIM] [Concinno: <feature>] <msg>"``
    * ``verbose`` → ``"[SHOW USER VERBATIM] <msg>"`` (legacy 4.5.0)

    The helper is idempotent: calling it twice on the same string
    won't double-prefix. Already-prefixed strings are detected and
    re-rendered cleanly under the requested mode (used by tests that
    want to migrate legacy hard-coded strings without rewriting every
    call site at once).
    """
    if not raw_msg:
        return ""

    resolved: RelayMode = mode if mode in VALID_RELAY_MODES else get_relay_mode()

    # Treat disabled-but-on (master switch off) as a legacy passthrough
    # so we never silently drop. ``off`` mode is the explicit drop.
    if mode is None and not _is_feature_enabled():
        resolved = "verbose"

    # Strip any existing wrappers so the helper is idempotent.
    body = _strip_wrappers(raw_msg, feature_name)

    if resolved == "off":
        return ""
    if resolved == "silent":
        # Bare text — io_utils._silence_context() will further wrap
        # this with the suppression directive. We just don't emit
        # the verbatim token (silent mode strips it anyway).
        return body
    if resolved == "verbose":
        return f"{_VERBATIM_TOKEN} {body}"
    # prefix (default)
    return f"{_VERBATIM_TOKEN} [Concinno: {feature_name}] {body}"


def emit_with_habituation(
    feature_name: str,
    raw_msg: str,
    *,
    session_id: str = "",
    mode: RelayMode | None = None,
) -> str:
    """軌 B-aware wrapper: dedup + auto-demote + brand prefix in one call.

    Composes the four 4.6.0 layers in the canonical order so that callers
    do not have to wire them by hand:

    1. **件 1 dedup** (``concinno.hooks.dedup_layer.should_dedup``) —
       returns ``""`` when the same ``(feature, msg)`` already fired in
       this session (caller MUST treat empty string as "skip emit").
    2. **件 2 auto-demote** (``concinno.hooks.auto_demote.current_tier``)
       — when the tier has been demoted to ``SILENT_LOG``, the helper
       also returns ``""``. ``CRITICAL`` / ``HIGH`` / ``NORMAL`` all
       still emit in this version (tier-aware shortening is a 4.7.0
       refinement once we have FTRL training data).
    3. **件 3 FTRL accept-rate** (``concinno.ziq_hook_ignore_rate.
       record_emit``) — every successful emit registers a pending
       verdict so the next ``record_user_accept_signal`` can update
       per-feature accept-rate.
    4. **軌 A verbatim_relay prefix** (``with_feature_prefix``) — same
       as the legacy 4.5.0 helper.

    Best-effort: any internal failure (state file unreadable, FTRL
    bus dead) silently degrades to the legacy
    :func:`with_feature_prefix` shape so a downstream warning is never
    swallowed by 軌 B infrastructure failure.

    Args:
        feature_name: Source feature key.
        raw_msg: Hook warning body.
        session_id: Claude Code session id (empty string falls back to
            TTL-bounded global window per :mod:`dedup_layer`).
        mode: Forced relay mode (passes through to
            :func:`with_feature_prefix`). ``None`` resolves via
            :func:`get_relay_mode`.

    Returns:
        The wrapped string ready to drop into ``additionalContext``,
        or ``""`` when the caller MUST skip emit (dedup hit OR
        ``SILENT_LOG`` tier OR mode==``"off"``).
    """
    # Step 1 & 2 — soft imports so the relay layer cannot break when
    # 軌 B state is missing or hook layout shifts in a partial install.
    try:
        from concinno.hooks.dedup_layer import mark_emitted, should_dedup
    except Exception:
        should_dedup = None  # type: ignore[assignment]
        mark_emitted = None  # type: ignore[assignment]
    try:
        from concinno.hooks.auto_demote import current_tier
    except Exception:
        current_tier = None  # type: ignore[assignment]

    if should_dedup is not None:
        try:
            if should_dedup(feature_name, raw_msg, session_id=session_id):
                return ""
        except Exception:
            pass

    if current_tier is not None:
        try:
            if current_tier(feature_name) == "SILENT_LOG":
                return ""
        except Exception:
            pass

    wrapped = with_feature_prefix(feature_name, raw_msg, mode=mode)
    if not wrapped:
        return wrapped

    # Step 3 — register pending FTRL verdict + dedup mark.
    if mark_emitted is not None:
        try:
            mark_emitted(feature_name, raw_msg, session_id=session_id)
        except Exception:
            pass
    try:
        from concinno.ziq_hook_ignore_rate import record_emit

        record_emit(feature_name, session_id=session_id)
    except Exception:
        pass
    return wrapped


def _strip_wrappers(msg: str, feature_name: str) -> str:
    """Remove existing ``[SHOW USER VERBATIM]`` and ``[Concinno: …]`` prefixes.

    Idempotency helper: callers can pass either a raw string or a
    string that already wears 4.5.0 / 4.6.0 prefixes. We always
    return the bare body so the caller's mode choice wins.
    """
    s = msg.strip()

    # Strip ``[SHOW USER VERBATIM]`` (with or without trailing space).
    if s.startswith(_VERBATIM_TOKEN):
        s = s[len(_VERBATIM_TOKEN):].lstrip()

    # Strip ``[Concinno: <anything>]`` (defensive — accept any feature
    # name in case the user reuses a wrapped string under a new
    # feature).
    if s.startswith("[Concinno:"):
        end = s.find("]")
        if end != -1:
            s = s[end + 1:].lstrip()

    return s
