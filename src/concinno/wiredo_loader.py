"""Three-tier on-demand loader for the WIREDO delivery-verification prompt.

@module: concinno.wiredo_loader
@responsibility: Assemble the WIREDO judge prompt from layered templates
    (core / dim summaries / change-type recipes) routed by ZIQ alpha_t.
@dependencies: stdlib only. ZIQFeatureRouter is imported lazily inside
    build_prompt() to keep module import cheap and avoid circular imports.
@exports: WiredoLoader, build_wiredo_prompt, estimate_tokens,
    DIMENSIONS, CHANGE_TYPES, tier_for_alpha

The prior `_WIREDO_BODY` in prompt_hooks.py was a ~2750-token static blob
injected on every Stop event. This loader splits it into 22 markdown
files under templates/wiredo/ and rebuilds the prompt on demand, routed
by ZIQ complexity tier and (optional) detected change_type.

Routing (ZIQ alpha_t tiers from ziq_router):
  simple      (alpha_t < 0.20)  → core only                    ~500 t
  complicated (alpha_t < 0.55)  → core + routing + all L2 dims ~1000 t
  complex     (alpha_t < 0.90)  → above + L3 recipe(change)    ~1200 t
  chaotic     (alpha_t >= 0.90) → above + ALL L3 recipes        ~2500 t

Callers specify `change_type` when the detector has classified recent
edits; otherwise the loader picks the lowest-risk assumption (include all
L2 dims in complicated+ tiers so no dim is silently dropped).

Token budget enforcement (max_tokens, default 2500):
  if the assembled prompt exceeds budget, layers are dropped in order
  L3 recipes → L2 dim summaries → routing table. `core` is never dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

Tier = Literal["simple", "complicated", "complex", "chaotic"]

DIMENSIONS: tuple[str, ...] = ("W", "I", "R", "E", "D", "O")

CHANGE_TYPES: tuple[str, ...] = (
    "frontend",
    "backend",
    "library",
    "hook",
    "migration",
    "deploy",
    "cli",
    "word_doc",
    "image",
    "audio",
    "video",
    "db_query",
    "ai_prompt",
    "build_artifact",
    "vscode_extension",
    "test_only",
    "docs_only",
)

# ZIQ alpha_t thresholds — mirror ziq_router.ALPHA_*_MAX constants.
ALPHA_SIMPLE_MAX = 0.20
ALPHA_COMPLICATED_MAX = 0.55
ALPHA_COMPLEX_MAX = 0.90

# Routing table: change_type → required dims (everything else N/A).
# Mirrors the routing.md table so callers can check dim requirements
# without parsing markdown.
ROUTING: dict[str, tuple[str, ...]] = {
    "frontend": ("W", "D", "R", "O"),
    "backend": ("W", "I", "D", "O", "E"),
    "library": ("W", "I", "D", "E"),
    "hook": ("W", "I", "D", "E"),
    "migration": ("W", "D", "O", "E"),
    "deploy": ("W", "D", "O"),
    "cli": ("W", "I", "D", "E"),
    "word_doc": ("I", "D", "R"),
    "image": ("D", "R"),
    "audio": ("D",),
    "video": ("D", "R"),
    "db_query": ("D",),
    "ai_prompt": ("I", "D", "E"),
    "build_artifact": ("W", "D", "E", "O"),
    "vscode_extension": ("W", "I", "D", "E", "O"),
    "test_only": ("D",),
    "docs_only": (),
}


def tier_for_alpha(alpha_t: float) -> Tier:
    """Classify an alpha_t value into a ZIQ tier name."""
    if alpha_t < ALPHA_SIMPLE_MAX:
        return "simple"
    if alpha_t < ALPHA_COMPLICATED_MAX:
        return "complicated"
    if alpha_t < ALPHA_COMPLEX_MAX:
        return "complex"
    return "chaotic"


def estimate_tokens(text: str) -> int:
    """Cheap token count estimate: len // 4. Good enough for budgeting."""
    return len(text) // 4


def _templates_root() -> Path:
    return Path(__file__).parent / "templates" / "wiredo"


@dataclass
class WiredoLoader:
    """Loads and assembles WIREDO prompt layers from markdown templates.

    The loader caches file reads via `functools.lru_cache` on the
    internal loader functions. Call `clear_cache()` to reload from disk
    (useful in tests).
    """

    templates_dir: Path = field(default_factory=_templates_root)

    def __post_init__(self) -> None:
        if not isinstance(self.templates_dir, Path):
            self.templates_dir = Path(self.templates_dir)
        if not self.templates_dir.exists():
            raise FileNotFoundError(
                f"WIREDO templates dir not found: {self.templates_dir}"
            )

    # ── Individual template readers (cached) ─────────────────────

    def load_core(self) -> str:
        return self._read("core.md")

    def load_routing(self) -> str:
        return self._read("routing.md")

    def load_dim(self, dim: str) -> str:
        if dim not in DIMENSIONS:
            raise ValueError(f"unknown dimension: {dim!r}")
        return self._read(f"dims/{dim}.md")

    def load_recipe(self, change_type: str) -> str:
        if change_type not in CHANGE_TYPES:
            raise ValueError(f"unknown change_type: {change_type!r}")
        return self._read(f"recipes/{change_type}.md")

    def load_all_dims(self) -> dict[str, str]:
        return {d: self.load_dim(d) for d in DIMENSIONS}

    def load_all_recipes(self) -> dict[str, str]:
        return {c: self.load_recipe(c) for c in CHANGE_TYPES}

    # ── Build pipeline ───────────────────────────────────────────

    def build_prompt(
        self,
        *,
        change_type: str | None = None,
        alpha_t: float = 0.55,
        max_tokens: int = 2500,
    ) -> str:
        """Assemble a WIREDO prompt for the given tier and change_type.

        Args:
            change_type: if known, only the matching L3 recipe is included.
                None in complicated+ tiers means 'include all L2 dims so
                nothing is silently dropped'. None in chaotic tier means
                'include all L3 recipes'.
            alpha_t: ZIQ complexity score in [0, 1]. Determines which
                tiers of content are included.
            max_tokens: hard budget ceiling. If exceeded, layers are
                dropped in order L3 recipes → L2 dims → routing table.
                `core` is never dropped.

        Returns:
            Assembled prompt string.
        """
        if change_type is not None and change_type not in CHANGE_TYPES:
            raise ValueError(f"unknown change_type: {change_type!r}")
        if not 0.0 <= alpha_t <= 1.0:
            raise ValueError(f"alpha_t must be in [0, 1], got {alpha_t}")

        tier = tier_for_alpha(alpha_t)
        core = self.load_core()
        routing = self.load_routing()

        # Selected L2 dims: in complicated+ include all 6; in simple skip.
        if tier == "simple":
            dims_block = ""
        else:
            dims_block = self._format_dims_block(self.load_all_dims())

        # Selected L3 recipes: specific change_type in complex,
        # all recipes in chaotic, none in lower tiers.
        recipes_block = ""
        if tier == "complex":
            if change_type is not None:
                recipes_block = self._format_recipes_block(
                    {change_type: self.load_recipe(change_type)}
                )
        elif tier == "chaotic":
            if change_type is not None:
                recipes_block = self._format_recipes_block(
                    {change_type: self.load_recipe(change_type)}
                )
            else:
                recipes_block = self._format_recipes_block(self.load_all_recipes())

        # Assemble.
        parts: list[str] = [core]
        if tier != "simple":
            parts.append(routing)
        if dims_block:
            parts.append(dims_block)
        if recipes_block:
            parts.append(recipes_block)
        assembled = "\n\n".join(parts)

        # Budget enforcement: drop from the tail inward.
        if estimate_tokens(assembled) > max_tokens:
            assembled = self._shrink_to_budget(
                core=core,
                routing=routing,
                dims_block=dims_block,
                recipes_block=recipes_block,
                max_tokens=max_tokens,
            )
        return assembled

    # ── Helpers ──────────────────────────────────────────────────

    def _format_dims_block(self, dims: dict[str, str]) -> str:
        header = (
            "──────────────────────────── SIX DIMENSIONS "
            "────────────────────────────"
        )
        return header + "\n\n" + "\n\n".join(dims[d] for d in DIMENSIONS if d in dims)

    def _format_recipes_block(self, recipes: dict[str, str]) -> str:
        header = (
            "─────────────── VERIFICATION RECIPES — D-dim proof patterns "
            "───────────────"
        )
        body = "\n\n".join(recipes[c] for c in CHANGE_TYPES if c in recipes)
        return header + "\n\n" + body

    def _shrink_to_budget(
        self,
        *,
        core: str,
        routing: str,
        dims_block: str,
        recipes_block: str,
        max_tokens: int,
    ) -> str:
        """Progressively drop tail layers until under budget.

        Order: recipes → dims → routing. `core` is never dropped.
        """
        # Attempt 1: drop recipes.
        parts = [core]
        if routing:
            parts.append(routing)
        if dims_block:
            parts.append(dims_block)
        candidate = "\n\n".join(parts)
        if estimate_tokens(candidate) <= max_tokens:
            return candidate

        # Attempt 2: drop dims too.
        parts = [core]
        if routing:
            parts.append(routing)
        candidate = "\n\n".join(parts)
        if estimate_tokens(candidate) <= max_tokens:
            return candidate

        # Attempt 3: core only.
        return core

    def _read(self, rel_path: str) -> str:
        return _cached_read(str(self.templates_dir / rel_path))

    def clear_cache(self) -> None:
        _cached_read.cache_clear()


@lru_cache(maxsize=64)
def _cached_read(abs_path: str) -> str:
    return Path(abs_path).read_text(encoding="utf-8").strip()


# Module-level convenience wrapper.
def build_wiredo_prompt(
    *,
    change_type: str | None = None,
    alpha_t: float = 0.55,
    max_tokens: int = 2500,
    templates_dir: Path | str | None = None,
) -> str:
    """Build a WIREDO prompt with default loader settings."""
    loader = WiredoLoader(
        templates_dir=Path(templates_dir) if templates_dir else _templates_root()
    )
    return loader.build_prompt(
        change_type=change_type, alpha_t=alpha_t, max_tokens=max_tokens
    )
