"""OCEAN-to-behavior prompt injection — public CCC building block.

Extracted from Aegis persona-api ``engine.py`` as part of the
Aegis → CCC downsink (session 2026-04-15 M5). Aegis imports
``build_behavior_injection`` from here and passes its own
``_OCEAN_DIM_COUNT`` constant as the ``ocean_dims`` argument so
Aegis stays the single source of truth for the dim count.

Byte-level contract: ``build_behavior_injection`` output text
is byte-identical to the legacy Aegis implementation. The
``[Persona Behavior Profile]`` header, three-line body shape,
punctuation, and whitespace must not drift — Aegis regression
tests inspect the full string.

Pure stdlib. Zero runtime dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "A_COMM",
    "C_COMM",
    "DimBehavior",
    "E_COMM",
    "N_COMM",
    "O_COMM",
    "O_THINK",
    "build_behavior_injection",
    "pick_behavior",
]


# Thresholds for low / mid / high behavior selection.
# These match the legacy Aegis ``_pick`` function bit-for-bit.
_LOW_THRESHOLD = 0.35
_HIGH_THRESHOLD = 0.65


@dataclass(frozen=True)
class DimBehavior:
    """One OCEAN dimension's behavior triple.

    Each dimension (Openness, Conscientiousness, Extraversion,
    Agreeableness, Neuroticism) has a ``low`` / ``mid`` / ``high``
    behavior phrase. ``pick_behavior`` selects the appropriate
    phrase from a raw 0.0–1.0 dimension score.
    """

    low: str
    mid: str
    high: str


def pick_behavior(value: float, dim: DimBehavior) -> str:
    """Return the low/mid/high phrase for ``value``.

    Thresholds (inherited verbatim from Aegis):

    * ``value < 0.35``  → ``dim.low``
    * ``0.35 ≤ value ≤ 0.65`` → ``dim.mid``
    * ``value > 0.65``  → ``dim.high``
    """
    if value < _LOW_THRESHOLD:
        return dim.low
    if value > _HIGH_THRESHOLD:
        return dim.high
    return dim.mid


# ── Dimension behavior maps ────────────────────────────────────
#
# Preserved byte-for-byte from Aegis ``engine.py`` lines 102-140.
# Any edit here is a behavioral change the persona engine will
# observe — do not reword without running the Aegis test suite.

# Openness
O_COMM = DimBehavior(
    low="Concrete and direct language, avoids metaphors",
    mid="Balanced expression, occasional figurative language",
    high="Rich vocabulary, loves metaphors and associations",
)
O_THINK = DimBehavior(
    low="Practical, fact-based, avoids hypotheticals",
    mid="Pragmatic with occasional creative leaps",
    high="Abstract thinker, enjoys hypotheticals",
)

# Conscientiousness
C_COMM = DimBehavior(
    low="Casual and spontaneous, topic jumps freely",
    mid="Reasonably organized but flexible",
    high="Structured and precise, uses lists and summaries",
)

# Extraversion
E_COMM = DimBehavior(
    low="Few words but precise, prefers deep 1-on-1",
    mid="Adjusts energy to context",
    high="Talkative, fast-paced, naturally energizing",
)

# Agreeableness
A_COMM = DimBehavior(
    low="Blunt and direct, doesn't sugarcoat",
    mid="Polite but honest, says what needs saying",
    high="Warm and considerate, cushions criticism",
)

# Neuroticism
N_COMM = DimBehavior(
    low="Emotionally stable, steady tone",
    mid="Moderate emotional range",
    high="Emotionally expressive, visible mood shifts",
)


def build_behavior_injection(
    ocean: list[float],
    ocean_dims: int = 5,
) -> str:
    """Build a behavior profile string from an OCEAN vector.

    Args:
        ocean: Five-element list of dimension scores in
            ``[O, C, E, A, N]`` order. Values are expected in
            ``[0.0, 1.0]`` but no clamping is applied — caller
            owns validation.
        ocean_dims: Expected dimension count. Defaults to 5 for
            the canonical Big Five. Aegis passes its own
            ``_OCEAN_DIM_COUNT`` constant so the persona loader
            remains the single source of truth for dim sizing.

    Returns:
        A four-line string beginning with
        ``[Persona Behavior Profile]`` or the empty string when
        ``len(ocean) != ocean_dims`` (mirrors legacy silent
        failure mode — caller treats empty as "no injection").
    """
    if len(ocean) != ocean_dims:
        return ""
    o, c, e, a, n = ocean

    lines = [
        "[Persona Behavior Profile]",
        f"Communication: {pick_behavior(e, E_COMM)}. "
        f"{pick_behavior(a, A_COMM)}. {pick_behavior(n, N_COMM)}.",
        f"Thinking: {pick_behavior(o, O_THINK)}. "
        f"{pick_behavior(c, C_COMM)}.",
        f"Expression: {pick_behavior(o, O_COMM)}.",
    ]
    return "\n".join(lines)
