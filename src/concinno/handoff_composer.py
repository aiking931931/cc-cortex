"""concinno.handoff_composer — Assemble full handoff markdown.

@module handoff_composer
@responsibility Glue layer: classify project type → load generic + specialized
    templates → run :func:`handoff_section0.populate_section_0` →
    substitute manual sections → return composed markdown ready to write
    into ``_AI_BRAIN/06_Handoffs/<project>/``.
@dependencies concinno.handoff_templates, concinno.template_router,
    concinno.handoff_section0
@exports compose_handoff, MANUAL_SECTION_KEYS

Composition order:
    1. Generic template (`generic_v2.md`) — always.
    2. Specialized templates returned by :func:`template_router.classify`,
       concatenated in the router's deterministic order.
    3. §0 placeholders substituted with live values from
       :func:`populate_section_0`.
    4. Manual sections (§2/§3/§4/§5) substituted with the caller-supplied
       ``manual_sections`` dict; missing keys leave the placeholder
       comments intact so the user can see what's still needed.

Manual section keys (recognised in ``manual_sections``):
    - ``decision_rationale``: replaces the §2 body
    - ``pitfalls``: replaces the §3 body
    - ``pointer_table``: replaces the §4 body
    - ``next_step``: replaces the §5 body

Future-friendly placeholders:
    Status fields (``open_count``, ``paused_count``, ``done_count``,
    ``current_focus``) are filled from ``status`` if provided in
    ``manual_sections`` (a dict-of-dicts). When absent, defaults to
    ``"?"`` so the user sees they are unfilled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from concinno.handoff_section0 import (
    SECTION0_FIELDS,
    populate_section_0,
)
from concinno.handoff_templates import (
    GENERIC_TEMPLATE_NAME,
    load_template,
)
from concinno.template_router import classify

MANUAL_SECTION_KEYS: tuple[str, ...] = (
    "decision_rationale",
    "pitfalls",
    "pointer_table",
    "next_step",
    "status",  # nested dict, see docstring
)


# Lightweight in-template tag pairs we replace when manual content
# is provided. We do not attempt to parse the markdown — we just
# substitute the well-known stub line. This keeps the templates
# editable as plain markdown without a templating engine.
_MANUAL_BODY_MARKERS = {
    "decision_rationale": (
        "<!-- Why this approach, not the alternatives? Short bullet list. -->",
        "{{decision_rationale_body}}",
    ),
    "pitfalls": (
        "<!-- Specific traps already encountered. Cite feedback file or MEMORY # if relevant. -->",
        "{{pitfalls_body}}",
    ),
    "pointer_table": (
        "| Topic | Read this file |\n|---|---|\n| Architecture | <relative-path-from-handoff> |",
        "{{pointer_table_body}}",
    ),
    "next_step": (
        "<!-- Single concrete actionable command or task name. -->",
        "{{next_step_body}}",
    ),
}


def _apply_manual_body(template: str, key: str, body: str) -> str:
    """Replace the placeholder comment for ``key`` with ``body`` markdown.

    Two-step: first turn the well-known marker into a unique sentinel,
    then replace the sentinel. This way arbitrary user content (which
    might contain HTML comments) cannot collide with the marker.
    """
    if key not in _MANUAL_BODY_MARKERS:
        return template
    placeholder, sentinel = _MANUAL_BODY_MARKERS[key]
    if placeholder not in template:
        return template
    template = template.replace(placeholder, sentinel, 1)
    return template.replace(sentinel, body)


def _apply_status(template: str, status: Mapping[str, Any]) -> str:
    """Substitute §1 status placeholders from a status dict.

    Recognised keys: ``open_count``, ``paused_count``, ``done_count``,
    ``current_focus``, ``last_session_id``. Missing entries fall back
    to ``"?"`` so the user notices.
    """
    fields = (
        "open_count",
        "paused_count",
        "done_count",
        "current_focus",
        "last_session_id",
    )
    out = template
    for f in fields:
        out = out.replace(
            "{{" + f + "}}",
            str(status.get(f, "?")),
        )
    return out


def _substitute_section0(template: str, values: Mapping[str, str]) -> str:
    """Substitute §0 placeholders without overwriting status fields.

    Uses the contracted SECTION0_FIELDS list so we only touch the
    intended placeholders even if the template happens to use a
    name-collision elsewhere (none today, but defensive).
    """
    out = template
    for key in SECTION0_FIELDS:
        out = out.replace("{{" + key + "}}", values.get(key, ""))
    return out


def compose_handoff(
    project_name: str,
    manual_sections: Mapping[str, Any] | None = None,
    *,
    project_root: Path | str | None = None,
    extra_template_meta: Mapping[str, Any] | None = None,
    section0_overrides: Mapping[str, str] | None = None,
) -> str:
    """Build a full handoff markdown for ``project_name``.

    Args:
        project_name: Handoff project key (folder name under
            ``_AI_BRAIN/06_Handoffs/``).
        manual_sections: Optional dict with the manual-fill content.
            Recognised keys are listed in :data:`MANUAL_SECTION_KEYS`.
        project_root: Override for the git repo root used by the §0
            populator. Defaults to ``Path.cwd()``.
        extra_template_meta: Hints passed to
            :func:`template_router.classify` so projects whose name
            doesn't fully describe them (e.g. literally ``concinno``
            being both release + build) still pull in the right
            templates.
        section0_overrides: Optional dict of pre-computed §0 values.
            Used by tests / callers that already populated §0 and
            don't want a duplicate live fetch.

    Returns:
        Composed handoff markdown as a single string. Always non-empty.

    Notes:
        - Specialized templates are appended *after* the generic body,
          separated by a blank line and a horizontal rule (``---``).
        - §0 placeholders without a live value get the literal
          ``"<unknown — source failed: ...>"`` placeholder, never
          a blank.
    """
    manual_sections = dict(manual_sections or {})

    # 1. Determine which templates apply.
    specialized = classify(project_name, dict(extra_template_meta or {}))

    # 2. Load and concat.
    body = load_template(GENERIC_TEMPLATE_NAME)
    for tmpl_name in specialized:
        body = body + "\n\n---\n\n" + load_template(tmpl_name)

    # 3. §0 substitution.
    if section0_overrides is None:
        s0 = populate_section_0(project_name, project_root=project_root)
    else:
        s0 = dict(section0_overrides)
    body = _substitute_section0(body, s0)

    # 4. Status header (§1) substitution from manual_sections["status"].
    status = manual_sections.get("status")
    if isinstance(status, Mapping):
        body = _apply_status(body, status)
    else:
        body = _apply_status(body, {})

    # 5. Manual body sections.
    for key in ("decision_rationale", "pitfalls", "pointer_table", "next_step"):
        body_text = manual_sections.get(key)
        if isinstance(body_text, str) and body_text.strip():
            body = _apply_manual_body(body, key, body_text)

    return body


def supported_specialized_for(
    project_name: str,
    extra_template_meta: Mapping[str, Any] | None = None,
) -> Sequence[str]:
    """Return the specialized template names that would be applied.

    Convenience wrapper for callers that want to preview the routing
    without running the full composer.
    """
    return classify(project_name, dict(extra_template_meta or {}))


__all__ = [
    "MANUAL_SECTION_KEYS",
    "compose_handoff",
    "supported_specialized_for",
]
