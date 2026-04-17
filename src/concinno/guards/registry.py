"""concinno.guards.registry — Default pipeline configuration.

@module registry
@responsibility Single source of truth for guard registration order;
    lazy imports to avoid import-time crashes
@dependencies concinno.guards.pipeline, concinno.* (all guards)
@exports create_default_pipeline
"""

from __future__ import annotations

from concinno.guards.pipeline import GuardPipeline


def _register_security(pipe: GuardPipeline) -> None:
    """Layer 1: SECURITY — hard deny, no step-back."""
    from concinno.dep_audit import DepAuditGuard
    from concinno.destruction_guard import DestructionGuard
    from concinno.exfil_guard import ExfilGuard
    from concinno.git_safety import GitSafetyGuard
    from concinno.identity_guard import IdentityGuard
    from concinno.prompt_injection_guard import PromptInjectionGuard
    from concinno.secret_scan import SecretScanGuard

    pipe.register(PromptInjectionGuard())
    pipe.register(SecretScanGuard())
    pipe.register(GitSafetyGuard())
    pipe.register(DepAuditGuard())
    pipe.register(ExfilGuard())
    pipe.register(IdentityGuard())
    pipe.register(DestructionGuard())


def _register_quality(pipe: GuardPipeline) -> None:
    """Layer 2: QUALITY — step-back + hard deny."""
    from concinno.agent_gate import AgentGateGuard
    from concinno.boundary_guard import BoundaryGuard
    from concinno.butterfly_guard import ButterflyGuard
    from concinno.code_guard import CodeGuard
    from concinno.delivery import DeliveryGuard
    from concinno.design_theory import DesignTheoryGuard
    from concinno.equilibrium_guard import EquilibriumGuard
    from concinno.file_tracker import FileTrackerGuard

    # Input rewriters run early in the QUALITY layer so later guards
    # see the already-rewritten ctx.tool_input (e.g. SecretScanGuard
    # will see .env.example, not .env). Rewrite guards are ALLOW-only
    # — they never DENY, so registering them up front is safe.
    from concinno.guards.rewrite_guards import (
        BashDryRunRewriter,
        BashPipeToShellRewriter,
        WriteSecretFileRewriter,
    )
    from concinno.handoff_validator import HandoffGuard
    from concinno.honesty_gate import HonestyGate
    from concinno.linting import LintGuard
    from concinno.multipath_gate import MultiPathGate
    from concinno.orientation_gate import OrientationGate
    from concinno.overflow_gate import OverflowGate
    from concinno.pre_tool_guards import BashPythonGuard, ReadBudgetGuard, ReadFirstGuard
    from concinno.premise_gate import PremiseGate
    from concinno.proposal_guard import ProposalGuard
    from concinno.sentinel import (
        ConsecutiveFailGuard,
        HijackGuard,
        SentinelGuard,
    )
    from concinno.ssot_guard import SSOTGuard
    from concinno.structural_guard import StructuralGuard
    from concinno.token_monitor import TokenGuard
    from concinno.ui_verify import UIVerifyGuard
    from concinno.window_guard import WindowGuard
    pipe.register(BashDryRunRewriter())
    pipe.register(WriteSecretFileRewriter())
    pipe.register(BashPipeToShellRewriter())

    from concinno.threat_patterns_guard import ThreatPatternsGuard
    pipe.register(ThreatPatternsGuard())

    # PreToolUse guards (PremiseGate imported above in sorted block)
    pipe.register(PremiseGate())
    pipe.register(WindowGuard())
    pipe.register(TokenGuard())
    pipe.register(AgentGateGuard())
    pipe.register(ReadFirstGuard())
    pipe.register(ReadBudgetGuard())
    pipe.register(BashPythonGuard())
    pipe.register(HijackGuard())
    pipe.register(ConsecutiveFailGuard())
    pipe.register(SentinelGuard())
    pipe.register(FileTrackerGuard())
    pipe.register(BoundaryGuard())
    pipe.register(ProposalGuard())
    pipe.register(UIVerifyGuard())
    pipe.register(ButterflyGuard())
    # RLHF Side-Effect gates (2026-03-26)
    pipe.register(OverflowGate())
    pipe.register(OrientationGate())
    pipe.register(HonestyGate())
    pipe.register(MultiPathGate())
    # PostToolUse guards
    from concinno.agent_artifact_guard import AgentArtifactGuard
    from concinno.hallucination_guard import HallucinationGuard
    pipe.register(AgentArtifactGuard())
    pipe.register(HallucinationGuard())
    # CBUA Law #3+#6: verify external references before writing (2026-04-10)
    from concinno.verify_before_write import VerifyBeforeWriteGuard
    pipe.register(VerifyBeforeWriteGuard())
    pipe.register(CodeGuard())
    pipe.register(LintGuard())
    pipe.register(StructuralGuard())
    pipe.register(SSOTGuard())
    pipe.register(HandoffGuard())
    pipe.register(EquilibriumGuard())
    pipe.register(DeliveryGuard())
    pipe.register(DesignTheoryGuard())
    from concinno.sibling_scan import SiblingScanGuard
    from concinno.structured_handoff import StructuredHandoffGuard
    from concinno.wiredo_enforcement import WiredoEnforcementGuard
    pipe.register(SiblingScanGuard())
    pipe.register(StructuredHandoffGuard())
    pipe.register(WiredoEnforcementGuard())
    from concinno.guards.convention_guard import ConventionGuard
    pipe.register(ConventionGuard())
    # 2.2.0: edit-time version-drift gate (pairs with CI test_version_sync).
    from concinno.version_sync_guard import VersionSyncGuard
    pipe.register(VersionSyncGuard())


def _register_cognitive(pipe: GuardPipeline) -> None:
    """Layer 3: COGNITIVE — knowledge injection."""
    from concinno.cognitive import CognitiveGuard
    from concinno.cognitive_anchor import CognitiveAnchorGuard
    from concinno.confidence_gate import ConfidenceGate
    from concinno.hypothesis_tracker import HypothesisTrackerGuard
    from concinno.milestone_gate import MilestoneGate
    from concinno.think_inject import ThinkInjectGuard
    from concinno.wiredo_guard import WiredoGuard

    pipe.register(CognitiveGuard())
    pipe.register(ConfidenceGate())
    pipe.register(HypothesisTrackerGuard())
    pipe.register(CognitiveAnchorGuard())
    # CBUA v2: intent anchoring (2026-04-10)
    from concinno.intent_anchor_guard import IntentAnchorGuard
    pipe.register(IntentAnchorGuard())
    # CBUA A0: root purpose probe for Complicated+ tasks (2026-04-10)
    from concinno.initial_intent_probe import InitialIntentProbe
    pipe.register(InitialIntentProbe())
    pipe.register(ThinkInjectGuard())
    pipe.register(WiredoGuard())
    # D3 SOP drift prevention (2026-03-26)
    pipe.register(MilestoneGate())
    # CC weakness mitigation (2026-04-03)
    from concinno.guards.cc_weakness_guards import (
        CompactFailureGuard,
        LargeFileReadGuard,
        McpCleanupGuard,
        RenameScopeGuard,
        TruncationAwareGuard,
    )
    pipe.register(TruncationAwareGuard())
    pipe.register(LargeFileReadGuard())
    pipe.register(RenameScopeGuard())
    pipe.register(CompactFailureGuard())
    pipe.register(McpCleanupGuard())
    # Token-aware subagent dispatch (2026-04-03)
    from concinno.guards.agent_dispatch_guard import AgentDispatchGuard
    pipe.register(AgentDispatchGuard())
    # CBUA pipeline enforcement — hardened B1/C1/U1/A4/A5 (2026-04-13)
    from concinno.guards.cbua_pipeline_guard import CbuaPipelineGuard
    pipe.register(CbuaPipelineGuard())


def create_default_pipeline(
    *,
    step_back_state_dir: str = "",
) -> GuardPipeline:
    """Create the default guard pipeline with all registered guards.

    Guard order = execution SOP. Security first, cognitive last.
    """
    pipe = GuardPipeline(step_back_state_dir=step_back_state_dir)
    _register_security(pipe)
    _register_quality(pipe)
    _register_cognitive(pipe)
    return pipe


def create_extended_pipeline(
    *,
    step_back_state_dir: str = "",
    plugin_paths: list[str] | None = None,
    use_entrypoints: bool = True,
) -> tuple[GuardPipeline, list]:
    """Create a pipeline with built-in guards + discovered plugins.

    Returns (pipeline, plugin_metas) so callers can inspect load results.
    Invalid plugins are skipped (not registered).
    """
    from concinno.plugin_loader import discover_plugins

    pipe = create_default_pipeline(step_back_state_dir=step_back_state_dir)

    # Collect existing guard names for dedup
    existing = {g.name for g in pipe._guards}

    metas = discover_plugins(
        paths=plugin_paths,
        use_entrypoints=use_entrypoints,
        existing_names=existing,
    )

    for meta in metas:
        if meta.valid and meta.guard is not None:
            pipe.register(meta.guard)

    return pipe, metas
