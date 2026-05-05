"""concinno.handoff_templates — Generic + specialized handoff template registry.

@module handoff_templates
@responsibility Bundled markdown templates for handoff composer; resolved by
    name via :func:`load_template`. Templates ship as plain ``.md`` files
    next to this module so users can edit them in place without re-installing.
@dependencies stdlib only (``importlib.resources``, ``pathlib``)
@exports load_template, list_templates, GENERIC_TEMPLATE_NAME,
    SPECIALIZED_TEMPLATE_NAMES, TEMPLATES_DIR

Layout:
    handoff_templates/
        __init__.py           ← this file (resolver only)
        generic_v2.md         ← every project gets this skeleton
        specialized/
            benchmark.md      ← extends generic with §B1-§B4
            release.md        ← extends generic with §R1-§R3
            research.md       ← extends generic with §S1-§S3
            build.md          ← extends generic with §I1-§I3

Adding a new specialized template:
    1. Drop ``my_type.md`` under ``specialized/``.
    2. Add ``"my_type"`` to ``SPECIALIZED_TEMPLATE_NAMES``.
    3. Wire keyword aliases in ``concinno.template_router.DEFAULT_ALIASES``.
    4. (Optional) document under ``kb_handoff/template_system.md``.

The composer concatenates ``generic_v2.md`` first, then each specialized
section in the order returned by the router. Section IDs (§B1, §R1, §S1,
§I1, …) are non-overlapping by design so two specialized templates can
coexist on the same handoff (e.g. a project that is *both* ``release``
and ``research``).
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent
"""Filesystem location of the bundled template ``.md`` files."""

GENERIC_TEMPLATE_NAME = "generic_v2"
"""Name of the always-included generic template (no ``.md`` suffix)."""

SPECIALIZED_TEMPLATE_NAMES = (
    "benchmark",
    "release",
    "research",
    "build",
)
"""Tuple of supported specialized template names. Order is stable for tests."""


class TemplateNotFoundError(LookupError):
    """Raised when :func:`load_template` cannot find the requested template.

    Inherits :class:`LookupError` so callers can catch the broader category
    if they prefer (``except LookupError``).
    """


def load_template(name: str) -> str:
    """Return the markdown content of the named template.

    Resolution order:
      1. ``name == GENERIC_TEMPLATE_NAME`` → ``generic_v2.md`` at top level.
      2. ``name in SPECIALIZED_TEMPLATE_NAMES`` → ``specialized/<name>.md``.
      3. Otherwise → :class:`TemplateNotFoundError`.

    Args:
        name: Template short name (no ``.md`` extension).

    Returns:
        UTF-8 decoded markdown content.

    Raises:
        TemplateNotFoundError: If ``name`` does not match a bundled template.
    """
    if name == GENERIC_TEMPLATE_NAME:
        path = TEMPLATES_DIR / f"{name}.md"
    elif name in SPECIALIZED_TEMPLATE_NAMES:
        path = TEMPLATES_DIR / "specialized" / f"{name}.md"
    else:
        raise TemplateNotFoundError(
            f"unknown template: {name!r} "
            f"(known: {GENERIC_TEMPLATE_NAME!r} or one of "
            f"{SPECIALIZED_TEMPLATE_NAMES!r})"
        )
    if not path.is_file():
        raise TemplateNotFoundError(
            f"template file missing on disk: {path}"
        )
    return path.read_text(encoding="utf-8")


def list_templates() -> tuple[str, ...]:
    """Return the names of all bundled templates (generic + specialized)."""
    return (GENERIC_TEMPLATE_NAME, *SPECIALIZED_TEMPLATE_NAMES)


__all__ = [
    "GENERIC_TEMPLATE_NAME",
    "SPECIALIZED_TEMPLATE_NAMES",
    "TEMPLATES_DIR",
    "TemplateNotFoundError",
    "list_templates",
    "load_template",
]
