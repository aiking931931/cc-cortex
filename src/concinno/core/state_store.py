"""concinno.core.state_store — Unified JSON state management.

@module state_store
@responsibility Namespace-aware JSON state with atomic I/O and
    auto-pruning
@dependencies concinno.core.atomic
@exports StateStore
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import time
from typing import Any

from .atomic import acquire_file_lock, read_json, release_file_lock, write_atomic

logger = logging.getLogger("concinno.state_store")


def _observable_write_failure(
    namespace: str,
    session_id: str,
    exc: Exception,
    *,
    scope: str,
) -> None:
    """Emit a visible warning + audit entry when state persistence fails.

    Previously these failures hit ``logger.debug`` which nobody reads, so
    sentinel / read-log / cooldown corruption went unnoticed until a
    downstream consumer blew up. Surfacing through stderr + the
    destruction_guard audit log keeps the signal loud enough to notice.
    """
    msg = (
        f"\033[93m\u26a0 [state_store] {scope} failed for "
        f"{namespace}/{session_id[:8] if session_id else '-'}: "
        f"{type(exc).__name__}: {exc}\033[0m\n"
    )
    try:
        sys.stderr.write(msg)
        sys.stderr.flush()
    except Exception:
        pass
    try:
        from concinno.destruction_guard import audit

        audit(
            f"state_store:{scope}",
            0,
            "warn",
            f"{namespace}/{session_id[:8] if session_id else '-'} | "
            f"{type(exc).__name__}: {str(exc)[:200]}",
        )
    except Exception:
        pass


class StateStore:
    """Namespace-aware JSON state manager with auto-pruning.

    Args:
        base_dir: Root directory for state files.
            Files are stored as ``{base_dir}/{namespace}/{session_prefix}.json``.
    """

    def __init__(self, base_dir: str) -> None:
        self._base_dir = base_dir

    def _path(self, namespace: str, session_id: str) -> str:
        # BugFix 2026-04-14: 8-char prefix caused cross-session collision
        # when session_ids shared first 8 chars. This was the root cause
        # of a case where a freshly-started session read edit_count=32
        # from a stale collision. Replaced with blake2b 8-byte digest
        # (16 hex chars) for collision-resistant filename keys.
        if not session_id:
            key = "unknown"
        else:
            key = hashlib.blake2b(
                session_id.encode(), digest_size=8,
            ).hexdigest()
        return os.path.join(self._base_dir, namespace, f"{key}.json")

    def prune_stale(
        self,
        namespace: str,
        *,
        ttl_seconds: int = 7 * 86400,
    ) -> int:
        """Delete state files older than *ttl_seconds* under *namespace*.

        Intended for SessionStart hook to run once per session, preventing
        unbounded growth of per-session state files (another symptom of the
        cross-session collision bug fixed in _path()).

        Args:
            namespace: Logical group.
            ttl_seconds: Files older than this are deleted. Default 7 days.

        Returns:
            Number of files deleted.
        """
        ns_dir = os.path.join(self._base_dir, namespace)
        if not os.path.isdir(ns_dir):
            return 0
        now = time.time()
        deleted = 0
        for name in os.listdir(ns_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(ns_dir, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if (now - mtime) > ttl_seconds:
                try:
                    os.unlink(path)
                    deleted += 1
                except OSError as exc:
                    logger.debug(
                        "prune_stale failed: %s — %s", path, exc,
                    )
        return deleted

    def prune_all_stale(
        self,
        namespaces: tuple[str, ...] | None = None,
        *,
        ttl_seconds: int = 7 * 86400,
    ) -> dict[str, int]:
        """Sweep every namespace under *base_dir* in one call.

        Intended for SessionStart hook: a single call clears stale
        per-session state across every namespace, avoiding the need
        for the hook to know which namespaces exist. When *namespaces*
        is None, immediate subdirectories of *base_dir* are auto-discovered
        — a new module that picks a fresh namespace is swept automatically
        with no code change here.

        Args:
            namespaces: Explicit namespace list. Default: auto-discover.
            ttl_seconds: TTL forwarded to :meth:`prune_stale`. Default 7 days.

        Returns:
            Mapping of namespace → number of files deleted.
        """
        if namespaces is None:
            try:
                namespaces = tuple(
                    name for name in os.listdir(self._base_dir)
                    if os.path.isdir(os.path.join(self._base_dir, name))
                )
            except OSError:
                return {}
        return {
            ns: self.prune_stale(ns, ttl_seconds=ttl_seconds)
            for ns in namespaces
        }

    def read(
        self,
        namespace: str,
        session_id: str,
        *,
        default: Any = None,
    ) -> Any:
        """Read state for a namespace + session.

        Args:
            namespace: Logical group (e.g. "sentinel", "read_log", "cooldown").
            session_id: Session identifier (first 8 chars used as filename).
            default: Returned when file doesn't exist or is corrupt.

        Returns:
            Parsed JSON data, or *default*.
        """
        if default is None:
            default = {}
        path = self._path(namespace, session_id)
        result = read_json(path, default=default)
        return result

    def write(
        self,
        namespace: str,
        session_id: str,
        data: Any,
    ) -> None:
        """Write state atomically.

        Args:
            namespace: Logical group.
            session_id: Session identifier.
            data: JSON-serializable data.
        """
        path = self._path(namespace, session_id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_atomic(path, data)
        except Exception as exc:
            # Escalated from logger.debug (invisible) to stderr + audit log —
            # silent write failures previously masked state corruption until
            # a downstream consumer asserted on stale data.
            _observable_write_failure(namespace, session_id, exc, scope="write")

    def read_modify_write(
        self,
        namespace: str,
        session_id: str,
        fn: Any,
        *,
        default: Any = None,
    ) -> Any:
        """Atomic read-modify-write with file lock.

        Acquires a file lock, reads current state, applies *fn*, writes back.
        Prevents concurrent-session data loss.

        Args:
            namespace: Logical group.
            session_id: Session identifier.
            fn: ``fn(data) -> new_data`` transformation callable.
            default: Fallback value when file doesn't exist.

        Returns:
            The new state after applying *fn*.

        Raises:
            RuntimeError: If the file lock cannot be acquired.
        """
        if default is None:
            default = {}
        path = self._path(namespace, session_id)
        # Ensure the namespace directory exists before attempting to
        # create the lock file — acquire_file_lock uses O_EXCL which
        # fails with FileNotFoundError on a missing parent directory.
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lock_path = path + ".lock"
        if not acquire_file_lock(lock_path):
            msg = f"state_store: failed to acquire lock {lock_path}"
            raise RuntimeError(msg)
        try:
            data = self.read(namespace, session_id, default=default)
            data = fn(data)
            self.write(namespace, session_id, data)
            return data
        finally:
            release_file_lock(lock_path)

    def prune_list(
        self,
        namespace: str,
        session_id: str,
        *,
        key: str,
        max_items: int,
    ) -> None:
        """Keep only the last *max_items* entries for a list key.

        Args:
            namespace: Logical group.
            session_id: Session identifier.
            key: Dict key whose value is a list.
            max_items: Maximum items to retain (from end).
        """
        state = self.read(namespace, session_id, default={})
        items = state.get(key)
        if isinstance(items, list) and len(items) > max_items:
            state[key] = items[-max_items:]
            self.write(namespace, session_id, state)

    def read_flat(self, namespace: str, filename: str, *, default: Any = None) -> Any:
        """Read a non-session-scoped state file (e.g. shared cache).

        Args:
            namespace: Logical group.
            filename: Exact filename (not session-derived).
            default: Fallback value.
        """
        if default is None:
            default = {}
        path = os.path.join(self._base_dir, namespace, filename)
        return read_json(path, default=default)

    def write_flat(self, namespace: str, filename: str, data: Any) -> None:
        """Write a non-session-scoped state file atomically."""
        path = os.path.join(self._base_dir, namespace, filename)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_atomic(path, data)
        except Exception as exc:
            _observable_write_failure(
                namespace, filename, exc, scope="write_flat",
            )

    def prune_dict(
        self,
        namespace: str,
        filename: str,
        *,
        max_items: int,
        keep_last: int,
    ) -> None:
        """Keep only the last *keep_last* items in a flat dict (e.g. SHA cache).

        Args:
            namespace: Logical group.
            filename: Exact filename.
            max_items: Threshold that triggers pruning.
            keep_last: How many items to retain after pruning.
        """
        data = self.read_flat(namespace, filename, default={})
        if isinstance(data, dict) and len(data) > max_items:
            keys = list(data.keys())
            pruned = {k: data[k] for k in keys[-keep_last:]}
            self.write_flat(namespace, filename, pruned)
