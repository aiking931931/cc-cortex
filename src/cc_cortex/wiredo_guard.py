"""cc_cortex.wiredo_guard — Re-export shim (merged into cc_cortex.wiredo_guards).

All functionality moved to cc_cortex.wiredo_guards. This shim preserves
backward compatibility for existing imports.
"""

from cc_cortex.wiredo_guards import (  # noqa: F401
    WiredoGuard,
    _build_checklist,
    _detect_project,
    _detect_task_type,
    _get_cascade_note,
)
