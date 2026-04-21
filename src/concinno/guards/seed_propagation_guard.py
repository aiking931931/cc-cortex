"""concinno.guards.seed_propagation_guard — warn when stochastic libs
are used without a seed in experiment / ablation / benchmark files.

@module seed_propagation_guard
@responsibility Detect imports of ``random`` / ``numpy`` / ``torch``
    (or their ``np.random`` / ``torch.*`` usages) in files under
    ``experiments/``, ``ablations/``, ``benchmarks/``, ``tests/`` or
    matching ``ablation_*.py`` / ``experiment_*.py``, while no
    corresponding ``seed`` call is present. Advisory only.
@dependencies concinno.guards.base (stdlib re/os only)
@exports SeedPropagationGuard
"""

from __future__ import annotations

import os
import re

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# Path marker — directories and basename hints.
_PATH_MARKERS = (
    "experiments/",
    "ablations/",
    "ablation/",
    "benchmarks/",
    "benchmark/",
    "tests/",
)
_BASENAME = re.compile(
    r"^(ablation_.*|experiment_.*|test_.*|.*_test|benchmark_.*|.*_benchmark)\.py$",
    re.IGNORECASE,
)

# Library usage detection — looks for actual calls / attribute access,
# not just import (since someone could ``import numpy as np`` but never
# touch stochastic state). Minimizes false positives.
_USAGE_PATTERNS: dict[str, re.Pattern[str]] = {
    "random": re.compile(
        r"\brandom\.(random|choice|randint|randrange|sample|shuffle|uniform|gauss|"
        r"betavariate|expovariate|lognormvariate|normalvariate)\s*\(",
    ),
    "numpy": re.compile(
        r"\b(?:np|numpy)\.random\.(?:rand|randn|randint|choice|permutation|shuffle|"
        r"sample|standard_normal|uniform|normal|poisson|binomial|seed|default_rng)\s*\(",
    ),
    "torch": re.compile(
        r"\btorch\.(?:rand|randn|randint|randperm|bernoulli|normal|poisson|"
        r"multinomial|manual_seed|initial_seed|seed)\b|"
        r"\btorch\.cuda\.manual_seed(?:_all)?\s*\(",
    ),
    "tensorflow": re.compile(
        r"\b(?:tf|tensorflow)\.random\.(?:uniform|normal|shuffle|set_seed|"
        r"set_random_seed|stateless_uniform|stateless_normal)\s*\(",
    ),
}

# Seed-set detection — the "reproducibility" signal.
_SEED_SET_PATTERNS: dict[str, re.Pattern[str]] = {
    "random": re.compile(r"\brandom\.seed\s*\("),
    "numpy": re.compile(
        r"\b(?:np|numpy)\.random\.(?:seed|default_rng)\s*\(",
    ),
    "torch": re.compile(
        r"\btorch\.manual_seed\s*\(|\btorch\.cuda\.manual_seed(?:_all)?\s*\(",
    ),
    "tensorflow": re.compile(
        r"\b(?:tf|tensorflow)\.random\.(?:set_seed|set_random_seed)\s*\(",
    ),
}

# Strip comments and docstrings before scanning.
_TRIPLE_RE = re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'')
_COMMENT_RE = re.compile(r"(?m)#.*$")


def _in_scope_path(path: str) -> bool:
    """True when the path is experiment-like and Python source."""
    if not path:
        return False
    normalized = path.replace("\\", "/")
    if not normalized.endswith(".py"):
        return False
    for marker in _PATH_MARKERS:
        if marker in normalized:
            return True
    basename = os.path.basename(normalized)
    return bool(_BASENAME.match(basename))


def _scrub(content: str) -> str:
    """Remove triple-strings and comments (approximate)."""
    content = _TRIPLE_RE.sub("", content)
    content = _COMMENT_RE.sub("", content)
    return content


def find_missing_seeds(content: str) -> list[str]:
    """Return library names used without a matching seed call.

    Pure function — deterministic, testable. Library names are returned
    in declaration order from ``_USAGE_PATTERNS``.
    """
    if not content:
        return []
    scrubbed = _scrub(content)
    missing: list[str] = []
    for lib, use_re in _USAGE_PATTERNS.items():
        if not use_re.search(scrubbed):
            continue
        seed_re = _SEED_SET_PATTERNS[lib]
        if seed_re.search(scrubbed):
            continue
        missing.append(lib)
    return missing


class SeedPropagationGuard(BaseGuard):
    """Warn when stochastic libs are used without a seed.

    Signal-only. Fires on PreToolUse Write / Edit for Python files in
    experiment / ablation / benchmark scope. Not intended to DENY —
    experiments that deliberately don't seed (e.g. serving RNG) are
    legitimate and will ignore the advisory.
    """

    name = "seed_propagation"
    category = GuardCategory.QUALITY
    feature_name = "seed_propagation"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name not in ("Write", "Edit"):
            return None

        path = ctx.tool_input.get("file_path", "") or ""
        if not _in_scope_path(path):
            return None

        content = (
            ctx.tool_input.get("content", "")
            or ctx.tool_input.get("new_string", "")
            or ""
        )
        if not content:
            return None

        missing = find_missing_seeds(content)
        if not missing:
            return None

        libs_str = ", ".join(missing)
        fix_hints = []
        if "random" in missing:
            fix_hints.append("`random.seed(SEED)`")
        if "numpy" in missing:
            fix_hints.append("`np.random.seed(SEED)` (or `np.random.default_rng(SEED)`)")
        if "torch" in missing:
            fix_hints.append(
                "`torch.manual_seed(SEED)` (+ `torch.cuda.manual_seed_all(SEED)` if GPU)",
            )
        if "tensorflow" in missing:
            fix_hints.append("`tf.random.set_seed(SEED)`")
        fix_str = ", ".join(fix_hints)

        msg = (
            f"[seed-propagation] {path}: stochastic APIs used without a seed — "
            f"libraries: {libs_str}. Reproducibility lost. "
            f"Add near top of module or pytest fixture: {fix_str}."
        )
        return GuardResult.allow_advisory(context=msg)
