"""concinno.core.atomic — Atomic file I/O with file locking.

@module atomic
@responsibility Safe JSON read/write via tmp+rename; file locking
@dependencies (none — leaf module)
@exports write_atomic, read_json, acquire_file_lock, release_file_lock
"""

import json
import os
import time
from typing import Any


def write_atomic(path: str, data: Any, indent: int = 2) -> None:
    """Write JSON data to file atomically using tmp+rename.

    Args:
        path: Target file path.
        data: JSON-serializable data.
        indent: JSON indentation level.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    os.replace(tmp, path)


def read_json(path: str, default: Any = None) -> Any:
    """Read JSON file with fallback default.

    Args:
        path: File to read.
        default: Value to return if file doesn't exist or is invalid.

    Returns:
        Parsed JSON data, or default.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def acquire_file_lock(lock_path: str, timeout: float = 5.0) -> bool:
    """Acquire a simple file-based lock using exclusive creation.

    Args:
        lock_path: Path for the lock file (e.g., "data.json.lock").
        timeout: Max seconds to wait for the lock.

    Returns:
        True if lock acquired, False on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            # Check if lock is stale (older than 30s)
            try:
                age = time.time() - os.path.getmtime(lock_path)
                if age > 30:
                    os.remove(lock_path)
                    continue
            except Exception:
                pass
            time.sleep(0.05)
        except Exception:
            return False
    return False


def release_file_lock(lock_path: str) -> None:
    """Release a file-based lock.

    Args:
        lock_path: Path of the lock file to remove.
    """
    try:
        os.remove(lock_path)
    except Exception:
        pass
