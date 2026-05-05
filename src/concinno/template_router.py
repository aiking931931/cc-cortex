"""concinno.template_router — Cold-start classifier for handoff templates.

@module template_router
@responsibility Map a project name (and optional metadata) to one or more
    bundled specialized handoff templates from
    :mod:`concinno.handoff_templates`. The generic template is always
    implied — this router only decides which *additional* specialized
    sections to extend it with.
@dependencies concinno.handoff_templates (resolution; same package family)
@exports DEFAULT_ALIASES, classify, register_alias, reset_aliases

Design constraints:
  - **Cold-start friendly**: pure keyword matching against project name +
    optional meta dict. No model load, no network call, no FTRL. ZIQ-grade
    structural prior; SPS slot can be swapped later if a learned router
    proves to do better, but the cold-start version covers the common
    case (project name strongly hints at type).
  - **Multi-template support**: a project can extend the generic with
    multiple specialized templates (e.g. ``concinno`` itself is both a
    ``release`` target *and* a ``build`` artefact). Output preserves
    deterministic insertion order so handoffs render the same way every
    run.
  - **Empty match → empty list**: callers receive ``[]`` (not the generic
    name) when nothing matches. The composer adds ``generic`` itself.
  - **No personal paths, no env reads**: project-name keyword tables only.
    A power-user can override defaults via ``register_alias``; the meta
    dict is the runtime-data escape hatch.

Aliases (default keyword → template name):
  - benchmark words → ``benchmark``
  - release-engineering words → ``release``
  - research / experiment words → ``research``
  - build / packaging / docs-site words → ``build``

The matching is case-insensitive and substring-based (``"my-bench-2"``
matches the ``"bench"`` keyword). Substring is preferred over exact
match for the cold-start case because real project names are noisy
("gaia-bench", "nq-benchmark-runner", "concinno-build-2026"); a strict
equality check would miss almost everything.

For projects whose name is ambiguous (e.g. literally ``concinno``),
the meta dict carries hints — recent commit messages with the word
``release`` will pull in the release template even when the project
name doesn't say so.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from concinno.handoff_templates import SPECIALIZED_TEMPLATE_NAMES

# ── Default keyword aliases ────────────────────────────────

DEFAULT_ALIASES: dict[str, list[str]] = {
    "benchmark": [
        "bench", "benchmark", "gaia", "osworld", "webarena",
        "swe-bench", "humaneval", "mmlu", "beir", "leaderboard",
        "scoreboard", "submission", "bright", "impliret",
        "nq", "msmarco", "trec",
    ],
    "release": [
        "concinno", "sancio", "redigo", "perpetuo", "invoco",
        "release", "publish", "pypi", "npm", "cargo",
        "ship", "package", "registry",
    ],
    "research": [
        "experiment", "experiments", "ablation", "research",
        "study", "exploration", "explore", "hypothesis",
        "ziq", "field-read", "fieldread", "investigation",
        "rosetta", "psyche-research", "deepresearch",
    ],
    "build": [
        "build", "compile", "binary", "wheel", "docker",
        "container", "image", "site", "docs-site", "static-site",
        "bundle", "webpack", "vite",
    ],
}
"""Default keyword → template-name mapping.

Each value list contains case-insensitive substrings that, when found
in the project name, indicate the corresponding specialized template
should be extended.

Power users can mutate at process start via :func:`register_alias`.
Tests use :func:`reset_aliases` to return to the shipped defaults.
"""

# Module-level mutable copy so register_alias / reset_aliases work
# without the global stays-frozen problem.
_active_aliases: dict[str, list[str]] = {
    k: list(v) for k, v in DEFAULT_ALIASES.items()
}


def _normalize_keyword(kw: str) -> str:
    """Lowercase + strip a keyword for case-insensitive substring match."""
    return kw.strip().lower()


def _matches_keyword(haystack: str, needle: str) -> bool:
    """Case-insensitive substring containment."""
    return _normalize_keyword(needle) in _normalize_keyword(haystack)


def classify(
    project_name: str,
    meta: Mapping[str, object] | None = None,
) -> list[str]:
    """Return ordered list of specialized template names to apply.

    Args:
        project_name: The handoff project key (folder name under
            ``_AI_BRAIN/06_Handoffs/``). Empty / whitespace-only names
            yield an empty list (caller falls back to generic-only).
        meta: Optional dict with extra hints. Recognised keys:
            * ``recent_commits`` (sequence of str) — commit subject lines
              scanned with the same keyword tables.
            * ``recent_paths`` (sequence of str) — file paths whose
              segments are scanned.
            Unknown keys are ignored; pass anything you like, the router
            won't crash on extras.

    Returns:
        List of specialized template names in the deterministic order of
        :data:`concinno.handoff_templates.SPECIALIZED_TEMPLATE_NAMES`,
        filtered to those that match. Possibly empty.

    Notes:
        - The generic template is implied — never returned in this list.
        - Multi-match is normal: ``["release", "build"]`` for a project
          that ships both packages and docs.
        - Order is stable across calls so tests can assert exact output.
    """
    if not project_name or not project_name.strip():
        return []

    # Aggregate searchable text from all sources.
    haystacks: list[str] = [project_name]
    if meta:
        commits = meta.get("recent_commits")
        if isinstance(commits, (list, tuple)):
            haystacks.extend(str(c) for c in commits if c)
        paths = meta.get("recent_paths")
        if isinstance(paths, (list, tuple)):
            haystacks.extend(str(p) for p in paths if p)

    matched: set[str] = set()
    for tmpl_name, keywords in _active_aliases.items():
        if tmpl_name not in SPECIALIZED_TEMPLATE_NAMES:
            # Defensive: ignore registered aliases that point at a
            # template the package doesn't ship.
            continue
        for kw in keywords:
            if any(_matches_keyword(h, kw) for h in haystacks):
                matched.add(tmpl_name)
                break

    # Stable order: follow SPECIALIZED_TEMPLATE_NAMES.
    return [t for t in SPECIALIZED_TEMPLATE_NAMES if t in matched]


def register_alias(template_name: str, keyword: str) -> None:
    """Add a runtime keyword alias for the given template name.

    Args:
        template_name: One of :data:`SPECIALIZED_TEMPLATE_NAMES`.
        keyword: New substring to match (case-insensitive).

    Raises:
        ValueError: If ``template_name`` is not a known specialized
            template.
    """
    if template_name not in SPECIALIZED_TEMPLATE_NAMES:
        raise ValueError(
            f"unknown template: {template_name!r} "
            f"(known: {SPECIALIZED_TEMPLATE_NAMES!r})"
        )
    bucket = _active_aliases.setdefault(template_name, [])
    norm = _normalize_keyword(keyword)
    if not norm:
        return
    if norm not in (_normalize_keyword(k) for k in bucket):
        bucket.append(keyword)


def reset_aliases() -> None:
    """Restore the alias table to the shipped defaults.

    Used by tests to isolate state, and by users who want to undo a
    bad ``register_alias`` call.
    """
    global _active_aliases
    _active_aliases = {k: list(v) for k, v in DEFAULT_ALIASES.items()}


def current_aliases() -> Mapping[str, Sequence[str]]:
    """Return a read-only snapshot of the active alias table."""
    # Wrap each list in tuple for immutability of the view.
    return {k: tuple(v) for k, v in _active_aliases.items()}


__all__ = [
    "DEFAULT_ALIASES",
    "classify",
    "current_aliases",
    "register_alias",
    "reset_aliases",
]
