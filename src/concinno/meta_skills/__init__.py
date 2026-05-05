"""concinno.meta_skills — Phase 0 獨家 meta-skill 四件套 (2.15.0).

@module meta_skills
@responsibility Four orchestrator primitives that compose over Concinno's
    existing cognitive layer (guards / CBUA router / handoff engine /
    ZIQ retrieval) to provide capabilities competing frameworks
    (LangChain / OpenAI Agents SDK / Claude Skills / OpenClaw) cannot
    replicate without reimplementing the Concinno substrate.
@dependencies concinno.tool_executor.Tool (protocol, hard), plus
    best-effort soft imports of concinno cognitive modules (see each
    sub-module for its own grep-verified wiring).
@exports SelfAuditedSkill, self_audited, SelfAuditedWrapper,
    ZIQRoutedSkillPack, CrossChannelMemoryBridge,
    CBUAWorkflowEngine, WorkflowNode, WorkflowResult,
    PermissionDenied

The four skills are deliberately thin — each one reuses rather than
duplicates concinno primitives so the entire edge of the framework
(guards / decision_journal / handoff three-tier / ZIQ posterior /
CBUA DAG) remains a single source of truth.
"""

from __future__ import annotations

from .cross_channel import CrossChannelMemoryBridge
from .self_audited import (
    PermissionDenied,
    SelfAuditedSkill,
    SelfAuditedWrapper,
    self_audited,
)
from .workflow import (
    CBUAWorkflowEngine,
    WorkflowNode,
    WorkflowResult,
)
from .ziq_pack import ZIQRoutedSkillPack

__all__ = [
    "CBUAWorkflowEngine",
    "CrossChannelMemoryBridge",
    "PermissionDenied",
    "SelfAuditedSkill",
    "SelfAuditedWrapper",
    "WorkflowNode",
    "WorkflowResult",
    "ZIQRoutedSkillPack",
    "self_audited",
]
