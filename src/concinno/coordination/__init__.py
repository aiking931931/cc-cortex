"""Pluggable coordination strategies for multi-instance Claude Code.

Usage::

    from concinno.coordination import get_strategy

    strategy = get_strategy("file_lock")   # default, JSON-based
    strategy = get_strategy("agent_teams") # future SDK integration
"""

from .base import CoordinationStrategy, LockResult, SessionInfo


def get_strategy(name: str = "file_lock") -> CoordinationStrategy:
    """Return a coordination strategy instance by name.

    Parameters
    ----------
    name : str
        ``"file_lock"`` (default) — file-based JSON lock.
        ``"agent_teams"`` — placeholder for future Claude Code SDK integration.

    Raises
    ------
    ValueError
        If *name* is not a recognised strategy.
    """
    if name == "file_lock":
        from .file_lock import FileLockStrategy

        return FileLockStrategy()
    if name == "agent_teams":
        from .agent_teams import AgentTeamsStrategy

        return AgentTeamsStrategy()
    raise ValueError(f"Unknown coordination strategy: {name}")


__all__ = [
    "CoordinationStrategy",
    "LockResult",
    "SessionInfo",
    "get_strategy",
]
