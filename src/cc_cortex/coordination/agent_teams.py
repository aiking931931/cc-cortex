"""Agent Teams coordination strategy — placeholder for future Claude Code SDK integration.

When Claude Code ships native Agent Teams coordination (SDK >= X.X),
this module will delegate session management, file locking, and conflict
detection to the SDK's built-in mechanism instead of relying on local
file-based JSON locks.

Until then every method raises ``NotImplementedError``.
"""

from typing import Optional

from .base import CoordinationStrategy, LockResult, RenameResult, SessionInfo

_NOT_READY = "Agent Teams coordination requires Claude Code SDK >= X.X"


class AgentTeamsStrategy(CoordinationStrategy):
    """Placeholder that will wrap the SDK-level coordination layer.

    Integration checklist (for when the SDK ships):
      1. Detect SDK availability at import time.
      2. Map ``SessionInfo`` ↔ SDK session objects.
      3. Translate ``acquire_file_lock`` to SDK-native locking.
      4. Subscribe to SDK heartbeat for ``cleanup_zombies``.
    """

    def register_session(self, info: SessionInfo) -> bool:
        raise NotImplementedError(_NOT_READY)

    def unregister_session(self, session_id: str) -> bool:
        raise NotImplementedError(_NOT_READY)

    def acquire_file_lock(self, session_id: str, file_path: str) -> LockResult:
        raise NotImplementedError(_NOT_READY)

    def release_file_lock(self, session_id: str, file_path: str) -> bool:
        raise NotImplementedError(_NOT_READY)

    def get_active_sessions(self) -> list[SessionInfo]:
        raise NotImplementedError(_NOT_READY)

    def cleanup_zombies(self, timeout_seconds: int = 1800) -> list[str]:
        raise NotImplementedError(_NOT_READY)

    def check_conflict(self, session_id: str, file_path: str) -> Optional[str]:
        raise NotImplementedError(_NOT_READY)

    def acquire_project(self, session_id: str, project: str) -> LockResult:
        raise NotImplementedError(_NOT_READY)

    def release_project(self, session_id: str, project: str) -> bool:
        raise NotImplementedError(_NOT_READY)

    def check_project_conflict(
        self, session_id: str, project: str
    ) -> Optional[str]:
        raise NotImplementedError(_NOT_READY)

    def rename_session(
        self,
        session_id: str,
        project: str,
        task: str = "",
        abbr_overrides: Optional[dict] = None,
        notify: bool = False,
    ) -> RenameResult:
        raise NotImplementedError(_NOT_READY)
