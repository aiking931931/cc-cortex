"""concinno.ziq_autotune_registry — DEPRECATED legacy alias.

Forwards every attribute access (including private ``_x`` names) to
:mod:`concinno.ziq.autotune_registry` (the canonical module under the
subpackage layout introduced in 5.2.0). Emits a single
:class:`DeprecationWarning` per process on first import. Removed in
concinno 6.0.

Migration::

    # OLD
    from concinno.ziq_autotune_registry import X

    # NEW
    from concinno.ziq.autotune_registry import X
"""

from __future__ import annotations

import warnings as _warnings

from concinno.ziq import autotune_registry as _canon

_warnings.warn(
    "concinno.ziq_autotune_registry is deprecated; import from concinno.ziq.autotune_registry "
    "instead. The legacy alias will be removed in concinno 6.0.",
    DeprecationWarning,
    stacklevel=2,
)


def __getattr__(name):
    # Lazy forward — covers public, private, and dynamically-added attrs.
    try:
        return getattr(_canon, name)
    except AttributeError as exc:
        raise AttributeError(
            f"module {__name__!r} (deprecated alias for "
            f"{_canon.__name__!r}) has no attribute {name!r}"
        ) from exc


def __dir__():
    return sorted(set(dir(_canon)) | set(globals().keys()))
