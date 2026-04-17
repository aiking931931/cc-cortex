"""Abstract base class for multi-instance coordination strategies.

All coordination implementations must subclass CoordinationStrategy
and implement every abstract method.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SessionInfo:
    """Immutable snapshot of a registered session."""

    session_id: str
    pid: int
    started_at: str
    active_files: list[str] = field(default_factory=list)
    project: str = ""  # Optional project claim — see acquire_project()


def normalize_project(name: str) -> str:
    """Canonicalize a project name so ``psyche`` / ``PSYCHE`` / ``  psyche  ``
    all collide on the same lock entry.

    Rules: strip whitespace, lowercase, collapse path separators to '/'.
    Empty string stays empty (callers treat empty as "no project claim").
    """
    if not name:
        return ""
    return name.strip().lower().replace("\\", "/").strip("/")


@dataclass
class LockResult:
    """Result of a file-lock acquisition attempt."""

    success: bool
    holder: Optional[str] = None  # who holds the lock if failed
    message: str = ""


@dataclass
class RenameResult:
    """Result of a ``rename_session`` call.

    ``old_key`` / ``new_key`` are the lock-file keys *before* and *after*
    the rename. ``title`` is the human-readable session title that callers
    can show in a notification or set as the terminal title.
    """

    success: bool
    old_key: str = ""
    new_key: str = ""
    title: str = ""
    message: str = ""


# ── Default project-abbreviation map ───────────────────────────────
#
# Used by ``rename_session`` to canonicalize a project name into a
# short prefix for the session title (e.g. ``evolution`` → ``EVO``).
# Callers can pass their own mapping; anything not in the map falls
# back to ``project.upper()[:4]``.
DEFAULT_PROJECT_ABBR: dict[str, str] = {
    "evolution": "EVO",
    "psyche": "PSY",
    "infinite-agent": "IA",
    "concinno": "CCC",
    "cc-toolkit": "CCF",
    "book": "BOOK",
    "arb-bot": "ARB",
    "aegis": "AGS",
    "strategos": "STR",
    "agi": "AGI",
    "legal": "LAW",
}


def project_abbr(
    project: str,
    overrides: Optional[dict[str, str]] = None,
) -> str:
    """Return the short abbreviation prefix for *project*.

    Resolution order: overrides → DEFAULT_PROJECT_ABBR → uppercase slice.
    Empty project yields ``"GEN"`` (generic).
    """
    if not project:
        return "GEN"
    key = project.strip().lower()
    if overrides and key in overrides:
        return overrides[key]
    if key in DEFAULT_PROJECT_ABBR:
        return DEFAULT_PROJECT_ABBR[key]
    return project.strip().upper()[:4] or "GEN"


class CoordinationStrategy(ABC):
    """Base class for multi-instance coordination strategies.

    Implementations can use file locks, in-memory state, SDK-level
    coordination, or any other mechanism — as long as they honour this
    contract.
    """

    @abstractmethod
    def register_session(self, info: SessionInfo) -> bool:
        """Register a new session.  Returns True on success."""
        ...

    @abstractmethod
    def unregister_session(self, session_id: str) -> bool:
        """Remove a session from the registry.  Returns True if it existed."""
        ...

    @abstractmethod
    def acquire_file_lock(self, session_id: str, file_path: str) -> LockResult:
        """Try to claim *file_path* for *session_id*.

        Returns a ``LockResult`` indicating success or who currently holds
        the lock.
        """
        ...

    @abstractmethod
    def release_file_lock(self, session_id: str, file_path: str) -> bool:
        """Release a previously acquired file lock.  Returns True if released."""
        ...

    @abstractmethod
    def get_active_sessions(self) -> list[SessionInfo]:
        """Return a list of all currently active sessions."""
        ...

    @abstractmethod
    def cleanup_zombies(self, timeout_seconds: int = 1800) -> list[str]:
        """Remove sessions that have been inactive for *timeout_seconds*.

        Returns a list of removed session IDs.
        """
        ...

    @abstractmethod
    def check_conflict(self, session_id: str, file_path: str) -> Optional[str]:
        """Check whether *file_path* conflicts with another session.

        Returns the conflicting session ID, or ``None`` if no conflict.
        """
        ...

    @abstractmethod
    def acquire_project(self, session_id: str, project: str) -> LockResult:
        """Try to claim *project* for *session_id* (project-level mutex).

        Different sessions competing for the same project name must see
        exactly one winner. Project names are normalized via
        ``normalize_project`` before comparison — ``psyche`` and ``PSYCHE``
        collide. Empty project name is rejected.
        """
        ...

    @abstractmethod
    def release_project(self, session_id: str, project: str) -> bool:
        """Release a project claim held by *session_id*.

        Returns True if the claim existed and was released, False otherwise
        (not held, held by someone else, or project name empty).
        """
        ...

    @abstractmethod
    def check_project_conflict(
        self, session_id: str, project: str
    ) -> Optional[str]:
        """Return the key of the session currently holding *project*, or
        ``None`` if the project is free or held by *session_id* itself.
        """
        ...

    @abstractmethod
    def rename_session(
        self,
        session_id: str,
        project: str,
        task: str = "",
        abbr_overrides: Optional[dict[str, str]] = None,
        notify: bool = False,
    ) -> "RenameResult":
        """Rename the lock entry for *session_id* to a project-scoped key.

        The new key follows the canonical format
        ``<ABBR>_<4hex>_<HHMM>`` — the same shape produced by
        ``core.notify.make_session_title`` — where ``ABBR`` is the
        abbreviation of *project* (looked up via ``abbr_overrides`` →
        ``DEFAULT_PROJECT_ABBR`` → uppercase slice).

        This *also* claims *project* as a side-effect (equivalent to
        ``acquire_project``) so the renamed session owns the project.
        When *notify* is True the implementation may show a system toast
        announcing the rename.

        Returns a :class:`RenameResult` with the old/new keys, the
        generated title, and a success flag.
        """
        ...
