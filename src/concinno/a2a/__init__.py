"""concinno.a2a — A2A protocol wrapper for CCC guard pipeline.

Exposes the full 40+ guard pipeline as an A2A-compatible agent
for AgentBeats competition (Berkeley RDI AgentX).
"""

from concinno.a2a.agent import GuardAgent
from concinno.a2a.server import create_app

__all__ = ["GuardAgent", "create_app"]
