"""cc_cortex.core.defer_loader — Lazy module loading with failure tracking.

@module defer_loader
@responsibility Lazy import registry with caching, failure counting,
    auto-disable, and audit trail
@dependencies (none — stdlib only)
@exports DeferLoader, ModuleEntry, RecoveryResult, try_with_fallback,
    truncate_output
"""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

__all__ = ["DeferLoader", "ModuleEntry", "RecoveryResult", "try_with_fallback"]

_DEFAULT_MAX_FAILURES = 5


@dataclass
class ModuleEntry:
    """Registry entry for a deferred module."""

    module_path: str
    func_name: str = ""
    critical: bool = False
    max_failures: int = _DEFAULT_MAX_FAILURES

    # Runtime state (not constructor args)
    _cached: Any = field(default=None, repr=False)
    _loaded: bool = field(default=False, repr=False)
    _failure_count: int = field(default=0, repr=False)
    _disabled: bool = field(default=False, repr=False)
    _last_error: str = field(default="", repr=False)
    _load_time_ms: float = field(default=0.0, repr=False)


class DeferLoader:
    """Lazy module loader with failure tracking and auto-disable.

    Thread-safe enough for single-process hooks (no concurrent access).
    Fail-open: if a module can't load, returns None (never raises).
    """

    def __init__(self, max_failures: int = _DEFAULT_MAX_FAILURES) -> None:
        self._registry: dict[str, ModuleEntry] = {}
        self._default_max_failures = max_failures
        self._audit: list[dict[str, Any]] = []

    # ── Registration ─────────────────────────────────────────

    def register(
        self,
        name: str,
        module_path: str,
        *,
        func: str = "",
        critical: bool = False,
        max_failures: int | None = None,
    ) -> "DeferLoader":
        """Register a module for deferred loading.

        Args:
            name: Short identifier (e.g. "destruction_guard").
            module_path: Full Python import path (e.g. "cc_cortex.destruction_guard").
            func: Optional function name to extract from the module.
            critical: If True, failure is logged as critical in audit.
            max_failures: Override per-module failure threshold.

        Returns:
            self (for chaining).
        """
        self._registry[name] = ModuleEntry(
            module_path=module_path,
            func_name=func,
            critical=critical,
            max_failures=max_failures or self._default_max_failures,
        )
        return self

    # ── Loading ──────────────────────────────────────────────

    def get(self, name: str) -> Any | None:
        """Get the deferred module/function. Returns None on failure.

        - First call: imports and caches.
        - Subsequent calls: returns cached value.
        - After max_failures: returns None without attempting import.
        """
        entry = self._registry.get(name)
        if entry is None:
            return None

        if entry._disabled:
            return None

        if entry._loaded:
            return entry._cached

        return self._try_load(name, entry)

    def _try_load(self, name: str, entry: ModuleEntry) -> Any | None:
        """Attempt to import and cache a module."""
        t0 = time.monotonic()
        try:
            mod = importlib.import_module(entry.module_path)
            if entry.func_name:
                result = getattr(mod, entry.func_name)
            else:
                result = mod

            entry._cached = result
            entry._loaded = True
            entry._load_time_ms = (time.monotonic() - t0) * 1000

            self._audit.append({
                "event": "loaded",
                "name": name,
                "time_ms": round(entry._load_time_ms, 2),
            })
            return result

        except Exception as exc:
            entry._failure_count += 1
            entry._last_error = str(exc)[:200]
            entry._load_time_ms = (time.monotonic() - t0) * 1000

            if entry._failure_count >= entry.max_failures:
                entry._disabled = True
                self._audit.append({
                    "event": "disabled",
                    "name": name,
                    "reason": f"max_failures ({entry.max_failures}) reached",
                    "last_error": entry._last_error,
                })
            else:
                self._audit.append({
                    "event": "load_failed",
                    "name": name,
                    "attempt": entry._failure_count,
                    "error": entry._last_error,
                })
            return None

    # ── Management ───────────────────────────────────────────

    def reset(self, name: str) -> bool:
        """Reset a disabled module so it can be retried.

        Returns True if the module was found and reset.
        """
        entry = self._registry.get(name)
        if entry is None:
            return False
        entry._disabled = False
        entry._failure_count = 0
        entry._loaded = False
        entry._cached = None
        entry._last_error = ""
        self._audit.append({"event": "reset", "name": name})
        return True

    def disable(self, name: str) -> bool:
        """Manually disable a module."""
        entry = self._registry.get(name)
        if entry is None:
            return False
        entry._disabled = True
        self._audit.append({"event": "manual_disable", "name": name})
        return True

    # ── Introspection ────────────────────────────────────────

    def health_report(self) -> dict[str, dict[str, Any]]:
        """Return health status for all registered modules."""
        report: dict[str, dict[str, Any]] = {}
        for name, entry in self._registry.items():
            report[name] = {
                "loaded": entry._loaded,
                "failures": entry._failure_count,
                "disabled": entry._disabled,
                "critical": entry.critical,
                "load_time_ms": round(entry._load_time_ms, 2),
            }
            if entry._last_error:
                report[name]["last_error"] = entry._last_error
        return report

    def audit_log(self) -> list[dict[str, Any]]:
        """Return the full audit trail."""
        return list(self._audit)

    @property
    def loaded_count(self) -> int:
        return sum(1 for e in self._registry.values() if e._loaded)

    @property
    def disabled_count(self) -> int:
        return sum(1 for e in self._registry.values() if e._disabled)

    @property
    def total_count(self) -> int:
        return len(self._registry)

    # ── Persistence (cross-process failure tracking) ──────

    def save_health(self, path: str) -> None:
        """Persist failure counts to JSON for cross-process recovery."""
        import json
        import os

        data: dict[str, dict[str, Any]] = {}
        for name, entry in self._registry.items():
            if entry._failure_count > 0 or entry._disabled:
                data[name] = {
                    "failures": entry._failure_count,
                    "disabled": entry._disabled,
                    "last_error": entry._last_error,
                }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def load_health(self, path: str) -> None:
        """Restore failure counts from persisted JSON."""
        import json
        import os

        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        for name, state in data.items():
            entry = self._registry.get(name)
            if entry is None:
                continue
            entry._failure_count = state.get("failures", 0)
            entry._disabled = state.get("disabled", False)
            entry._last_error = state.get("last_error", "")


# ── Output Truncation ────────────────────────────────────────


def truncate_output(
    text: str,
    *,
    max_chars: int = 2000,
    max_lines: int = 50,
    suffix: str = "\n…[truncated]",
) -> str:
    """Truncate output text to prevent context overflow.

    Applies two limits (whichever hits first):
    - max_chars: total character count
    - max_lines: total line count

    Returns the original text if within limits.
    """
    if not text:
        return text

    lines = text.split("\n")
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines]) + suffix
    if len(text) > max_chars:
        text = text[:max_chars - len(suffix)] + suffix
    return text


# ── Error Recovery Template ──────────────────────────────────


@dataclass
class RecoveryResult:
    """Result of an error recovery attempt."""

    succeeded: bool
    value: Any = None
    error: str = ""
    fallback_used: bool = False


def try_with_fallback(
    primary: Callable[[], Any],
    fallback: Optional[Callable[[], Any]] = None,
    *,
    default: Any = None,
) -> RecoveryResult:
    """Execute primary callable with optional fallback.

    Three-stage recovery:
      1. Try primary → return result
      2. Try fallback → return result (marked fallback_used=True)
      3. Return default (marked succeeded=False)

    Fail-open: never raises.
    """
    try:
        result = primary()
        return RecoveryResult(succeeded=True, value=result)
    except Exception as exc1:
        if fallback is not None:
            try:
                result = fallback()
                return RecoveryResult(
                    succeeded=True,
                    value=result,
                    fallback_used=True,
                    error=str(exc1)[:200],
                )
            except Exception as exc2:
                return RecoveryResult(
                    succeeded=False,
                    value=default,
                    error=f"primary: {str(exc1)[:100]}; fallback: {str(exc2)[:100]}",
                )
        return RecoveryResult(
            succeeded=False,
            value=default,
            error=str(exc1)[:200],
        )
