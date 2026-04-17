"""concinno.wiredo_enforcement — Re-export shim (merged into concinno.wiredo_guards).

All functionality moved to concinno.wiredo_guards. This shim preserves
backward compatibility for existing imports.
"""

from concinno.wiredo_guards import (  # noqa: F401
    WiredoEnforcementGuard,
    _has_wiredo_table,
    _is_handoff_file,
    _is_wiredo_enabled,
    _session_has_code_edits,
)
