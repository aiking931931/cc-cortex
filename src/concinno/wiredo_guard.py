"""concinno.wiredo_guard — Re-export shim (merged into concinno.wiredo_guards).

All functionality moved to concinno.wiredo_guards. This shim preserves
backward compatibility for existing imports.
"""

from concinno.wiredo_guards import (  # noqa: F401
    WiredoGuard,
    _build_checklist,
    _detect_project,
    _detect_task_type,
    _get_cascade_note,
)
