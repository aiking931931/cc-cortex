"""concinno.meta_skills.self_audited — Guard-wrapped Tool + decision journal.

@module meta_skills.self_audited
@responsibility Wrap any :class:`concinno.tool_executor.Tool` so every
    call runs through a configurable PreToolUse guard pipeline, then
    appends ``{tool_name, args, result, verdict}`` to
    ``~/.concinno/decision_journal.jsonl``. Guards that DENY raise
    :class:`PermissionDenied`.
@dependencies concinno.tool_executor.Tool (hard),
    concinno.guards.base (soft — only imported when caller supplies
    real BaseGuard instances; wrapper also accepts plain callables).
@exports PermissionDenied, SelfAuditedSkill, SelfAuditedWrapper,
    self_audited

Design
------
The canonical Concinno guard pipeline is driven by a
:class:`concinno.guards.base.GuardContext` which requires a hook-event
payload (session_id, cache_dir, hook_event, …). In a library consumer's
code the ``Tool.call`` boundary does NOT have a CC hook envelope, so
wiring BaseGuard instances naively would require synthesising fake
contexts.

Instead, this wrapper defines a lightweight guard protocol local to the
meta-skill layer::

    GuardFn = Callable[[tool_name: str, args: dict], None | str]

Returning ``None`` = allow; returning a non-empty string = deny with that
reason. Callers may also register real ``BaseGuard`` instances — the
wrapper auto-adapts them by building a minimal ``GuardContext`` and
mapping the resulting ``GuardResult.action`` back to this protocol.

A small registry of known-by-name guards
(``butterfly`` / ``premise`` / ``sentinel`` / ``destruction``) is
resolved lazily: if the underlying module imports cleanly the wrapper
uses them; if not, they're silently skipped (missing-optional semantics
consistent with the rest of Concinno).

The decision journal lives under ``~/.concinno/`` — never under the
workspace — so library consumers sharing a home directory accumulate a
single append-only trace per user.
"""

from __future__ import annotations

import importlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..tool_executor import Tool

logger = logging.getLogger("concinno.meta_skills.self_audited")

# ── Journal location ─────────────────────────────────────────────────
# Library-safe: always under Path.home(), never inside a workspace.
_JOURNAL_DIR = Path.home() / ".concinno"
_JOURNAL_FILE = "decision_journal.jsonl"


def _journal_path() -> Path:
    """Resolve the journal file path, creating the parent on demand.

    Factored out so tests can monkeypatch ``Path.home`` via
    ``monkeypatch.setenv("HOME", ...)`` (POSIX) or the equivalent on
    Windows without reaching into this module.
    """
    _JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    return _JOURNAL_DIR / _JOURNAL_FILE


# ── Exceptions ───────────────────────────────────────────────────────


class PermissionDenied(RuntimeError):
    """Raised when any wrapped guard returns a DENY verdict.

    Attributes:
        tool_name: Name of the tool whose call was blocked.
        guard_name: Guard that produced the DENY.
        reason: Non-empty human-readable reason.
    """

    def __init__(self, tool_name: str, guard_name: str, reason: str) -> None:
        self.tool_name = tool_name
        self.guard_name = guard_name
        self.reason = reason
        super().__init__(f"[{guard_name}] denied {tool_name}: {reason}")


# ── Guard adapters ───────────────────────────────────────────────────

# A guard function returns None=allow, or a non-empty str=deny reason.
GuardFn = Callable[[str, dict[str, Any]], "str | None"]


@dataclass(frozen=True)
class _NamedGuard:
    name: str
    fn: GuardFn


def _adapt_base_guard(base_guard: Any) -> GuardFn:
    """Wrap a ``concinno.guards.base.BaseGuard`` into a :data:`GuardFn`.

    Builds a minimal :class:`GuardContext` and maps the ``check`` result.
    If the guard raises we treat that as ALLOW (safety-critical guards
    should never raise in steady state; raising is a bug, not a deny).
    """
    try:
        from ..guards.base import GuardAction, GuardContext
    except Exception:  # pragma: no cover - guards pkg always present
        return lambda _tn, _ar: None

    def _adapted(tool_name: str, tool_input: dict[str, Any]) -> str | None:
        ctx = GuardContext(
            tool_name=tool_name,
            tool_input=dict(tool_input),
            session_id="meta_skills",
            cache_dir="",
            hook_event="PreToolUse",
        )
        try:
            result = base_guard.check(ctx)
        except Exception as exc:  # noqa: BLE001 - guard bug, not a deny
            logger.debug(
                "self_audited: guard %r raised %s; treating as allow",
                getattr(base_guard, "name", type(base_guard).__name__),
                exc,
            )
            return None
        if result is None:
            return None
        if result.action == GuardAction.DENY:
            return result.reason or "denied"
        return None

    return _adapted


# ── Known-guard registry ─────────────────────────────────────────────

# Lazy-imported. Each entry is (module, attribute, ctor_kwargs).
# Verified via ``grep -rln "class.*Guard" projects/concinno/src/concinno``:
#   - concinno.butterfly_guard:ButterflyGuard          (confirmed)
#   - concinno.premise_gate:PremiseGate                (confirmed)
#   - concinno.sentinel:ConsecutiveFailGuard           (confirmed)
#   - concinno.destruction_guard:DestructionGuard      (confirmed)
_KNOWN_GUARDS: dict[str, tuple[str, str]] = {
    "butterfly": ("concinno.butterfly_guard", "ButterflyGuard"),
    "premise": ("concinno.premise_gate", "PremiseGate"),
    "sentinel": ("concinno.sentinel", "ConsecutiveFailGuard"),
    "destruction": ("concinno.destruction_guard", "DestructionGuard"),
}


def _resolve_known_guard(key: str) -> GuardFn | None:
    """Import + instantiate a known Concinno guard by short name.

    Returns ``None`` silently if:
      - the key is not in :data:`_KNOWN_GUARDS`
      - the module/attribute is missing (version skew)
      - the constructor requires unsupported args

    Missing-optional-is-OK matches the rest of Concinno's wiring.
    """
    target = _KNOWN_GUARDS.get(key)
    if target is None:
        return None
    module_name, attr = target
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        logger.debug("self_audited: %s unavailable (%s)", module_name, exc)
        return None
    cls = getattr(module, attr, None)
    if cls is None:
        logger.debug("self_audited: %s.%s not found", module_name, attr)
        return None
    try:
        instance = cls()
    except Exception as exc:  # noqa: BLE001 - optional init
        logger.debug("self_audited: %s() raised %s; skipping", attr, exc)
        return None
    return _adapt_base_guard(instance)


# ── Wrapper ──────────────────────────────────────────────────────────


class SelfAuditedWrapper:
    """Guard-wrapped Tool adapter.

    Satisfies the :class:`Tool` protocol by exposing ``name`` /
    ``description`` / ``is_concurrency_safe`` attributes and a ``call``
    method. Each call:

      1. Runs every guard in insertion order. First DENY raises
         :class:`PermissionDenied`.
      2. Invokes ``inner.call(**kwargs)``.
      3. Appends a JSON line to the decision journal.

    Exceptions raised by ``inner.call`` are recorded in the journal
    (``verdict="error"``) and re-raised so the caller's control flow
    stays honest.
    """

    def __init__(
        self,
        inner: Tool,
        *,
        guards: list[str] | list[GuardFn] | None = None,
    ) -> None:
        self._inner = inner
        self._guards: list[_NamedGuard] = []
        self.name = inner.name
        self.description = inner.description
        self.is_concurrency_safe = getattr(inner, "is_concurrency_safe", False)

        for g in guards or []:
            if isinstance(g, str):
                fn = _resolve_known_guard(g)
                if fn is None:
                    continue
                self._guards.append(_NamedGuard(name=g, fn=fn))
            elif callable(g):
                self._guards.append(
                    _NamedGuard(name=getattr(g, "__name__", "anon"), fn=g)
                )
            else:
                logger.warning(
                    "self_audited: ignoring non-str/non-callable guard %r", g
                )

    def call(self, **kwargs: Any) -> Any:
        """Run guards then invoke the inner tool.

        Raises:
            PermissionDenied: first guard that returns a non-empty reason.
        """
        for g in self._guards:
            try:
                reason = g.fn(self.name, dict(kwargs))
            except Exception as exc:  # noqa: BLE001 - guard bug
                logger.debug(
                    "self_audited: guard %s raised %s; treating as allow",
                    g.name,
                    exc,
                )
                continue
            if reason:
                self._write_journal(kwargs, verdict="denied", extra={
                    "guard": g.name, "reason": reason,
                })
                raise PermissionDenied(self.name, g.name, reason)

        try:
            result = self._inner.call(**kwargs)
        except Exception as exc:
            self._write_journal(kwargs, verdict="error", extra={
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            })
            raise

        self._write_journal(kwargs, verdict="allowed", result=result)
        return result

    def _write_journal(
        self,
        args: dict[str, Any],
        *,
        verdict: str,
        result: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": time.time(),
            "tool_name": self.name,
            "args": _to_jsonable(args),
            "verdict": verdict,
        }
        if result is not None:
            entry["result"] = _to_jsonable(result)
        if extra:
            entry.update(extra)
        try:
            with _journal_path().open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False))
                fh.write("\n")
        except OSError as exc:  # pragma: no cover - disk-full edge
            logger.debug("self_audited: journal write failed %s", exc)


# Public name preferred by the brief — alias keeps API discoverable.
SelfAuditedSkill = SelfAuditedWrapper


# ── Decorator form ───────────────────────────────────────────────────


def self_audited(
    guards: list[str] | list[GuardFn] | None = None,
) -> Callable[[type], type]:
    """Class decorator: wraps a Tool-class' ``__init__`` so instances are
    pre-wrapped.

    Usage::

        @self_audited(guards=["butterfly", "premise"])
        class MyTool:
            name = "my_tool"
            description = "…"
            is_concurrency_safe = True
            def call(self, **kwargs):
                ...

        tool = MyTool()       # → SelfAuditedWrapper around MyTool()
        tool.call(q="hi")     # guard-checked + journaled

    Returning a wrapper from ``__new__`` (not ``__init__``) keeps the
    decorator transparent to downstream type-checkers: ``MyTool()`` is
    typed as ``MyTool`` but at runtime becomes
    :class:`SelfAuditedWrapper`.
    """

    def _decorator(cls: type) -> type:
        original_new = cls.__new__

        def _new(new_cls: type, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG001
            # Build the real instance using the ORIGINAL __new__ (avoid recursion).
            raw: Any
            if original_new is object.__new__:
                raw = object.__new__(new_cls)
            else:
                raw = original_new(new_cls, *args, **kwargs)
            # Run original __init__ explicitly since __new__ short-circuits
            # the auto-call path when we return a different type.
            raw.__init__(*args, **kwargs)  # noqa: PLC2801
            return SelfAuditedWrapper(raw, guards=guards)

        cls.__new__ = _new  # type: ignore[assignment,method-assign]
        return cls

    return _decorator


# ── Helpers ──────────────────────────────────────────────────────────


def _to_jsonable(obj: Any) -> Any:
    """Best-effort coerce arbitrary object to JSON-safe form."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    try:
        return repr(obj)
    except Exception:  # noqa: BLE001
        return "<unrepresentable>"
