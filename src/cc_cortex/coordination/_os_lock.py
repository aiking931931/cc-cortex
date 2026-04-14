"""Cross-platform OS-level advisory file lock.

Uses ``msvcrt.locking`` on Windows and ``fcntl.flock`` on POSIX to provide
a true inter-process mutex — something a pure JSON read/modify/write cannot
guarantee (two processes can race and the last writer wins).

The lock operates on a *sentinel* file next to the real state file (not on
the state file itself) because the state file is replaced atomically via
``os.replace`` and any fd held on it would become orphaned.

No third-party deps — CCC core stays zero-runtime-dep.
"""

from __future__ import annotations

import os
import sys
import time
from typing import IO, Optional

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    import msvcrt
else:
    import fcntl  # type: ignore[import-not-found,unused-ignore]


class LockAcquireTimeout(TimeoutError):
    """Raised when OS lock cannot be acquired within the timeout window."""


class OSFileLock:
    """Advisory inter-process file lock.

    Usage::

        with OSFileLock("/path/to/state.json.lockfile", timeout=5.0):
            data = read_state()
            data["x"] = 1
            write_state(data)

    Parameters
    ----------
    path : str
        Path to a sentinel file used purely for locking. Created on demand.
        Never the real state file.
    timeout : float
        Max seconds to wait for the lock. Raises ``LockAcquireTimeout`` on
        expiry. Default 5s — long enough for normal contention, short enough
        to surface deadlocks.
    poll_interval : float
        Sleep between retries while waiting. Default 0.02s.
    """

    def __init__(
        self,
        path: str,
        timeout: float = 5.0,
        poll_interval: float = 0.02,
    ) -> None:
        self.path = path
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._fh: Optional[IO[bytes]] = None

    def _try_lock(self) -> bool:
        """One non-blocking lock attempt. True on success, False on contention."""
        assert self._fh is not None
        fh = self._fh
        try:
            if _IS_WIN:
                # Lock the first byte — standard msvcrt pattern.
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(  # type: ignore[attr-defined,unused-ignore]
                    fh.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined,unused-ignore]
                )
            return True
        except (OSError, IOError):
            return False

    def _close_fh(self) -> None:
        """Best-effort close of the sentinel fd."""
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass
        self._fh = None

    def __enter__(self) -> "OSFileLock":
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 'a+b' keeps the file if it exists, creates if not, and doesn't truncate.
        self._fh = open(self.path, "a+b")
        deadline = time.time() + self.timeout
        while True:
            if self._try_lock():
                return self
            if time.time() >= deadline:
                self._close_fh()
                raise LockAcquireTimeout(
                    f"Could not acquire {self.path} within {self.timeout}s"
                )
            time.sleep(self.poll_interval)

    def __exit__(self, exc_type, exc_val, exc_tb) -> Optional[bool]:
        fh = self._fh
        if fh is None:
            return None
        try:
            if _IS_WIN:
                try:
                    # Must seek back to start before unlocking the same byte.
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                try:
                    fcntl.flock(  # type: ignore[attr-defined,unused-ignore]
                        fh.fileno(),
                        fcntl.LOCK_UN,  # type: ignore[attr-defined,unused-ignore]
                    )
                except OSError:
                    pass
        finally:
            try:
                fh.close()
            except Exception:
                pass
            self._fh = None
        return None


__all__ = ["OSFileLock", "LockAcquireTimeout"]
