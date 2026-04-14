"""cc_cortex.guards.registry — Default pipeline configuration.

@module registry
@responsibility Single source of truth for guard registration order;
    lazy imports to avoid import-time crashes
@dependencies cc_cortex.guards.pipeline, cc_cortex.* (all guards)
@exports create_default_pipeline
"""

from __future__ import annotations

from cc_cortex.guards.pipeline import GuardPipeline


def _register_security(pipe: GuardPipeline) -> None:
    """Layer 1: SECURITY — hard deny, no step-back."""
    from cc_cortex.dep_audit import DepAuditGuard
    from cc_cortex.destruction_guard import DestructionGuard
    from cc_cortex.exfil_guard import ExfilGuard
    from cc_cortex.git_safety import GitSafetyGuard
    from cc_cortex.identity_guard import IdentityGuard
    from cc_cortex.prompt_injection_guard import PromptInjectionGuard
    from cc_cortex.secret_scan import SecretScanGuard

    pipe.register(PromptInjectionGuard())
    pipe.register(SecretScanGuard())
    pipe.register(GitSafetyGuard())
    pipe.register(DepAuditGuard())
    pipe.register(ExfilGuard())
    pipe.register(IdentityGuard())
    pipe.register(DestructionGuard())


def _register_quality(pipe: GuardPipeline) -> None:
    """Layer 2: QUALITY — step-back + hard deny."""
    from cc_cortex.agent_gate import AgentGateGuard
    from cc_cortex.boundary_guard import BoundaryGuard
    from cc_cortex.butterfly_guard import ButterflyGuard
    from cc_cortex.code_guard import CodeGuard
    from cc_cortex.delivery import DeliveryGuard
    from cc_cortex.design_theory import DesignTheoryGuard
    from cc_cortex.equilibrium_guard import EquilibriumGuard
    from cc_cortex.file_tracker import FileTrackerGuard

    # Input rewriters run early in the QUALITY layer so later guards
    # see the already-rewritten ctx.tool_input (e.g. SecretScanGuard
    # will see .env.example, not .env). Rewrite guards are ALLOW-only
    # — they never DENY, so registering them up front is safe.
    from cc_cortex.guards.rewrite_guards import (
        BashDryRunRewriter,
        BashPipeToShellRewriter,
        WriteSecretFileRewriter,
    )
    from cc_cortex.handoff_validator import HandoffGuard
    from cc_cortex.honesty_gate import HonestyGate
    from cc_cortex.linting import LintGuard
    from cc_cortex.multipath_gate import MultiPathGate
    from cc_cortex.orientation_gate import OrientationGate
    from cc_cortex.overflow_gate import OverflowGate
    from cc_cortex.pre_tool_guards import BashPythonGuard, ReadBudgetGuard, ReadFirstGuard
    from cc_cortex.premise_gate import PremiseGate
    from cc_cortex.proposal_guard import ProposalGuard
    from cc_cortex.sentinel import (
        ConsecutiveFailGuard,
        HijackGuard,
        SentinelGuard,
    )
    from cc_cortex.ssot_guard import SSOTGuard
    from cc_cortex.structural_guard import StructuralGuard
    from cc_cortex.token_monitor import TokenGuard
    from cc_cortex.ui_verify import UIVerifyGuard
    from cc_cortex.window_guard import WindowGuard
    pipe.register(BashDryRunRewriter())
    pipe.register(WriteSecretFileRewriter())
    pipe.register(BashPipeToShellRewriter())

    from cc_cortex.threat_patterns_guard import ThreatPatternsGuard
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
    from cc_cortex.agent_artifact_guard import AgentArtifactGuard
    from cc_cortex.hallucination_guard import HallucinationGuard
    pipe.register(AgentArtifactGuard())
    pipe.register(HallucinationGuard())
    # CBUA Law #3+#6: verify external references before writing (2026-04-10)
    from cc_cortex.verify_before_write import VerifyBeforeWriteGuard
    pipe.register(VerifyBeforeWriteGuard())
    pipe.register(CodeGuard())
    pipe.register(LintGuard())
    pipe.register(StructuralGuard())
    pipe.register(SSOTGuard())
    pipe.register(HandoffGuard())
    pipe.register(EquilibriumGuard())
    pipe.register(DeliveryGuard())
    pipe.register(DesignTheoryGuard())
    from cc_cortex.sibling_scan import SiblingScanGuard
    from cc_cortex.structured_handoff import StructuredHandoffGuard
    from cc_cortex.wiredo_enforcement import WiredoEnforcementGuard
    pipe.register(SiblingScanGuard())
    pipe.register(StructuredHandoffGuard())
    pipe.register(WiredoEnforcementGuard())
    from cc_cortex.guards.convention_guard import ConventionGuard
    pipe.register(ConventionGuard())


def _register_cognitive(pipe: GuardPipeline) -> None:
    """Layer 3: COGNITIVE — knowledge injection."""
    from cc_cortex.cognitive import CognitiveGuard
    from cc_cortex.cognitive_anchor import CognitiveAnchorGuard
    from cc_cortex.confidence_gate import ConfidenceGate
    from cc_cortex.hypothesis_tracker import HypothesisTrackerGuard
    from cc_cortex.milestone_gate import MilestoneGate
    from cc_cortex.think_inject import ThinkInjectGuard
    from cc_cortex.wiredo_guard import WiredoGuard

    pipe.register(CognitiveGuard())
    pipe.register(ConfidenceGate())
    pipe.register(HypothesisTrackerGuard())
    pipe.register(CognitiveAnchorGuard())
    # CBUA v2: intent anchoring (2026-04-10)
    from cc_cortex.intent_anchor_guard import IntentAnchorGuard
    pipe.register(IntentAnchorGuard())
    # CBUA A0: root purpose probe for Complicated+ tasks (2026-04-10)
    from cc_cortex.initial_intent_probe import InitialIntentProbe
    pipe.register(InitialIntentProbe())
    pipe.register(ThinkInjectGuard())
    pipe.register(WiredoGuard())
    # D3 SOP drift prevention (2026-03-26)
    pipe.register(MilestoneGate())
    # CC weakness mitigation (2026-04-03)
    from cc_cortex.guards.cc_weakness_guards import (
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
    from cc_cortex.guards.agent_dispatch_guard import AgentDispatchGuard
    pipe.register(AgentDispatchGuard())
    # CBUA pipeline enforcement — hardened B1/C1/U1/A4/A5 (2026-04-13)
    from cc_cortex.guards.cbua_pipeline_guard import CbuaPipelineGuard
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
    from cc_cortex.plugin_loader import discover_plugins

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
