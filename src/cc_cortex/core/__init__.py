"""cc_cortex.core — Foundation layer re-exported for convenience.

@module core
@responsibility Central re-exports: config, atomic I/O, session,
    notifications, logging, state management
@dependencies cc_cortex.core.* (all submodules)
@exports Config, get_config, write_atomic, read_json, StateStore,
    get_logger, play_sound, show_toast, DeferLoader, ...
"""

from .atomic import acquire_file_lock, read_json, release_file_lock, write_atomic
from .compact import extract_recent_user_messages, save_compact_state
from .config import Config, get_config
from .defer_loader import DeferLoader, RecoveryResult, truncate_output, try_with_fallback
from .log import get_logger
from .notify import play_sound, show_toast
from .path_utils import extract_file_path, find_latest_transcript, find_transcript, normalize_path
from .session import cleanup_session_marker, generate_session_id, save_session_marker
from .state_store import StateStore

__all__ = [
    "Config",
    "get_config",
    "write_atomic",
    "read_json",
    "acquire_file_lock",
    "release_file_lock",
    "generate_session_id",
    "save_session_marker",
    "cleanup_session_marker",
    "play_sound",
    "show_toast",
    "save_compact_state",
    "extract_recent_user_messages",
    "DeferLoader",
    "RecoveryResult",
    "truncate_output",
    "try_with_fallback",
    "StateStore",
    "extract_file_path",
    "normalize_path",
    "find_transcript",
    "find_latest_transcript",
    "get_logger",
]
