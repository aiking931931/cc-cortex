"""Red+Blue architect decision flow — data structures + prompt templates.

Blueprint from redteam.md: Red and Blue are the same architect role anchored
to opposing stances. Commander adjudicates. Evidence-based red claims are
accepted; Goodhart-style framing attacks are rejected. Novelty-kill claims
require a prior-art URL.

Concinno does NOT call LLMs here. The consumer wires in anthropic/openai.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["FATAL", "HIGH", "MED", "WEAK"]
Radius = Literal["low", "medium", "high"]
Verdict = Literal["GO", "CONDITIONAL_GO", "KILL"]
Role = Literal["red", "blue"]

_VALID_SEVERITIES = {"FATAL", "HIGH", "MED", "WEAK"}
_VALID_RADII = {"low", "medium", "high"}
_VALID_VERDICTS = {"GO", "CONDITIONAL_GO", "KILL"}


@dataclass
class RedBlueDecision:
    """High-blast-radius decision record: proposal + radius + red/blue +
    commander verdict + must-run experiments + prior-art URLs + notes.

    red_attacks items: ``{attack, evidence, severity}`` where severity is
    FATAL|HIGH|MED|WEAK. blue_defense items: ``{claim, evidence}``.
    """

    proposal: str
    radius: Radius
    red_attacks: list[dict[str, Any]] = field(default_factory=list)
    blue_defense: list[dict[str, Any]] = field(default_factory=list)
    commander_verdict: Verdict = "CONDITIONAL_GO"
    must_run_experiments: list[str] = field(default_factory=list)
    prior_art_urls: list[str] = field(default_factory=list)
    notes: str = ""

    def __post_init__(self) -> None:
        if self.radius not in _VALID_RADII:
            raise ValueError(
                f"radius must be in {sorted(_VALID_RADII)}, got {self.radius!r}"
            )
        if self.commander_verdict not in _VALID_VERDICTS:
            raise ValueError(
                f"commander_verdict must be in {sorted(_VALID_VERDICTS)}, "
                f"got {self.commander_verdict!r}"
            )
        for i, a in enumerate(self.red_attacks):
            if not isinstance(a, dict):
                raise TypeError(f"red_attacks[{i}] must be dict, got {type(a)!r}")
            missing = {"attack", "evidence", "severity"} - a.keys()
            if missing:
                raise ValueError(
                    f"red_attacks[{i}] missing keys: {sorted(missing)}"
                )
            if a["severity"] not in _VALID_SEVERITIES:
                raise ValueError(
                    f"red_attacks[{i}].severity must be in "
                    f"{sorted(_VALID_SEVERITIES)}, got {a['severity']!r}"
                )
        for i, d in enumerate(self.blue_defense):
            if not isinstance(d, dict):
                raise TypeError(f"blue_defense[{i}] must be dict, got {type(d)!r}")
            missing = {"claim", "evidence"} - d.keys()
            if missing:
                raise ValueError(
                    f"blue_defense[{i}] missing keys: {sorted(missing)}"
                )


_RED_TEMPLATE = """\
You are an Opus architecture attacker. Same identity as the defender — only
anchored to attack.

Do NOT attack code bugs. Attack the design premises:
  - Should this exist at all?
  - Is it placed at the right layer?
  - Does the user actually need it?
  - Goodhart's Law: when a measure becomes a target, it stops being a good
    measure. Find every path by which the proposal's measure can be gamed.

Ground every claim in evidence. Read the files referenced in context, quote
line numbers, grade severity FATAL / HIGH / MED / WEAK. Do not hand-wave.

Return a JSON list of attacks, each with fields:
  - attack (one sentence, specific)
  - evidence (path:line, URL, or concrete scenario — no vibes)
  - severity (FATAL|HIGH|MED|WEAK)

Proposal under attack:
{proposal}

Context:
{context_block}
"""

_BLUE_TEMPLATE = """\
You are an Opus architecture defender. Same identity as the attacker — only
anchored to defend.

First, verify the system actually runs — read the code and confirm the wiring.
Then argue the design is sound. List:
  1. What the system actually enforces (with code evidence).
  2. Honest weaknesses (do NOT protect the indefensible).
  3. Which weaknesses are design choices, not bugs.
  4. Pre-emptive rebuttals to likely red-team attacks (use evidence, not
     rhetoric).

Return a JSON list of defenses, each with fields:
  - claim (one sentence)
  - evidence (path:line, URL, benchmark number — concrete)

Proposal under defense:
{proposal}

Context:
{context_block}
"""


def build_redblue_prompt(
    role: Role,
    proposal: str,
    context: dict[str, Any] | None = None,
) -> str:
    """Generate the Opus prompt for the red or blue architect. ``context``
    renders as ``key: value`` lines (e.g. files, prior_art, budget)."""
    if role not in ("red", "blue"):
        raise ValueError(f"role must be 'red' or 'blue', got {role!r}")
    if not proposal or not proposal.strip():
        raise ValueError("proposal must be non-empty")

    context = context or {}
    if context:
        context_block = "\n".join(f"  {k}: {v}" for k, v in context.items())
    else:
        context_block = "  (none provided)"

    template = _RED_TEMPLATE if role == "red" else _BLUE_TEMPLATE
    return template.format(proposal=proposal.strip(), context_block=context_block)


def adjudicate(decision: RedBlueDecision) -> str:
    """Commander summary. Rules: red attacks with evidence >= 8 chars are
    accepted, shorter/absent evidence rejected as Goodhart/vibes. KILL
    verdicts without prior_art_urls AND no evidence-based FATAL attack
    emit a WARNING (not a hard raise — override via notes)."""
    lines: list[str] = []
    lines.append(f"PROPOSAL: {decision.proposal.strip()[:200]}")
    lines.append(f"RADIUS: {decision.radius.upper()}")
    lines.append(f"VERDICT: {decision.commander_verdict}")
    lines.append("")

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for a in decision.red_attacks:
        ev = str(a.get("evidence", "")).strip()
        if len(ev) >= 8:
            accepted.append(a)
        else:
            rejected.append(a)

    lines.append(f"RED ATTACKS — accepted ({len(accepted)}):")
    for a in accepted:
        lines.append(f"  [{a['severity']}] {a['attack']}")
        lines.append(f"      evidence: {a['evidence']}")
    if rejected:
        lines.append(f"RED ATTACKS — rejected ({len(rejected)}, Goodhart/vibes):")
        for a in rejected:
            lines.append(f"  [{a['severity']}] {a['attack']}")

    lines.append("")
    lines.append(f"BLUE DEFENSE ({len(decision.blue_defense)}):")
    for d in decision.blue_defense:
        lines.append(f"  - {d['claim']}")
        lines.append(f"      evidence: {d['evidence']}")

    # Novelty-kill warning
    if decision.commander_verdict == "KILL":
        has_prior_art = bool(decision.prior_art_urls)
        has_fatal = any(
            a["severity"] == "FATAL" and len(str(a.get("evidence", ""))) >= 8
            for a in decision.red_attacks
        )
        if not (has_prior_art or has_fatal):
            lines.append("")
            lines.append(
                "WARNING: KILL verdict lacks prior_art_urls AND evidence-based "
                "FATAL red attack. Commander should document override in notes."
            )

    if decision.must_run_experiments:
        lines.append("")
        lines.append("MUST-RUN EXPERIMENTS (blocking):")
        for e in decision.must_run_experiments:
            lines.append(f"  - {e}")

    if decision.prior_art_urls:
        lines.append("")
        lines.append("PRIOR ART:")
        for u in decision.prior_art_urls:
            lines.append(f"  - {u}")

    if decision.notes:
        lines.append("")
        lines.append(f"NOTES: {decision.notes}")

    return "\n".join(lines)
