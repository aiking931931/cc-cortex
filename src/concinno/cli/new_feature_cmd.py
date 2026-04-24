"""concinno.cli.new_feature_cmd — scaffold a new feature with 9-phase design doc.

@module new_feature_cmd
@responsibility Turn the manual "build a new skill / sub-package / guard /
    CLI / module" ritual into one command. Scaffolds a directory + drops
    a ``docs/<name>-design.md`` file prefilled with the 9-phase pipeline
    (think / PRD / design / red-blue / TDD / impl / review / QA / ship +
    ecosystem-integration), the 6-point DoD checklist (L0 rule #6), and
    the 5-axis commander verdict (真做完 / 接線 / 功能正常 / AI 能力提升
    / UX 方便).

Design rationale (AI King 2026-04-23 directive, MEMORY #61/#62):

    Every new feature had to redo the same boilerplate: create dirs,
    write skeleton files, copy/paste the 9-phase outline from the last
    one, and remember the 6-point DoD. 90% of the time the DoD
    checklist gets skipped and the feature ships missing a switch /
    missing ZIQ auto-tune metadata. Single command moves it to zero
    effort.

    CP picks (cheapest high-impact):
      - Scaffold is dumb string templates — no Jinja, no extra dep.
      - Radius-aware: Chaotic forces red-blue phase to "mandatory",
        Simple marks it "optional-skip".
      - --dry-run prints the plan, writes nothing. Safe to preview.
      - Existing target-dir = exit 2 + clear error (never clobber).

@dependencies stdlib only (argparse / pathlib / datetime / textwrap)
@exports cmd_new_feature, register, scaffold_skill, scaffold_subpackage,
    scaffold_guard, scaffold_cli, scaffold_module, render_design_doc
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_KINDS = ("skill", "subpackage", "guard", "cli", "module")
_VALID_RADII = ("simple", "complicated", "complex", "chaotic")

# 9-phase names — fixed order.
_PHASES: tuple[tuple[str, str, str], ...] = (
    ("think", "/think skill", "Challenge requirements; decide what to build."),
    ("prd", "/prd skill", "Write PRD + GitHub issue."),
    ("rfc", "/rfc skill", "Architecture proposal / design doc."),
    ("redteam", "/redteam-cycle skill", "Red/Blue architect attack + defense."),
    ("tdd", "/tdd skill", "Red-green-refactor; tests first."),
    ("impl", "(edit code)", "Implement until tests green."),
    ("review", "/review skill", "Staff-engineer code review."),
    ("qa", "/qa skill", "Diff-aware QA + visual verify if UI."),
    (
        "ship",
        "/ship skill",
        "CHANGELOG + version bump + artifacts + publish. Ecosystem: "
        "consumer wiring, docs, handoff.",
    ),
)

# 6-point DoD, L0 rule #6 + MEMORY #62.
_DOD_POINTS: tuple[tuple[str, str], ...] = (
    ("Switchable", "enabled flag + FEATURE_META entry + env var override"),
    ("ZIQ", "ziq_autotunable / cosmetic labels on every param"),
    ("3-layer", "Index / Summary / Full file classification"),
    ("Lazy", "Heavy deps import inside functions, not at module scope"),
    (
        "CP/SOTA/logic-max",
        "CP-optimal path picked OR SOTA brick reused OR logical ceiling hit",
    ),
    ("CBUA", "C0 route recorded, B1+ think trace, WIREDO D verified"),
)

# 5-axis commander verdict.
_COMMANDER_AXES: tuple[tuple[str, str], ...] = (
    ("真做完", "Feature fully implemented, not a stub / TODO / half-mock."),
    ("接線", "Wired to ≥1 consumer: CLI subparser / entry_point / import."),
    ("功能正常", "Functional verify (D-dim): runs end-to-end, correct output."),
    ("AI 能力提升", "Measurable: step-count drops / new capability / fewer errors."),
    ("UX 方便", "Time saving estimate vs manual; quote before/after if possible."),
)

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class ScaffoldPlan:
    """A scaffold plan — list of (path, content) pairs."""

    root: Path
    name: str
    kind: str
    radius: str
    files: list[tuple[Path, str]]

    def describe(self) -> str:
        """Human-readable dry-run summary."""
        lines = [
            f"Scaffold plan: kind={self.kind} radius={self.radius}",
            f"Root: {self.root}",
            f"Files ({len(self.files)}):",
        ]
        for path, content in self.files:
            rel = path.relative_to(self.root) if path.is_absolute() else path
            lines.append(f"  + {rel}  ({len(content)} bytes)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Design-doc renderer
# ---------------------------------------------------------------------------


def render_design_doc(name: str, kind: str, radius: str) -> str:
    """Render the 9-phase design doc for ``<name>-design.md``.

    Radius governs whether the red-blue phase is **mandatory** (Chaotic)
    or optional-skip (Simple/Complicated). MEMORY #45 naming rules apply
    to the scaffold output but not to the user-chosen ``name`` argument.
    """
    today = date.today().isoformat()

    lines: list[str] = []
    lines.append(f"# {name} — 9-phase design doc")
    lines.append("")
    lines.append(f"> Scaffolded by `concinno new-feature {name}` on {today}.")
    lines.append(f"> Kind: `{kind}` — Radius: `{radius}`")
    lines.append("")
    lines.append("> Follow AI King L0 rule #6 (6-point DoD) + CBUA task-execution")
    lines.append("> 9-phase pipeline. Update each phase as you go.")
    lines.append("")
    lines.append("## Phase 0 — Intent anchor")
    lines.append("")
    lines.append("- [ ] Original prompt / user directive quoted verbatim")
    lines.append("- [ ] One-sentence recap in my own words")
    lines.append(f"- [ ] Explosion radius locked: **{radius}**")
    lines.append("")

    for idx, (phase, invoke, summary) in enumerate(_PHASES, start=1):
        mandatory = _phase_mandatory(phase, radius)
        tag = " **(mandatory)**" if mandatory else " (optional — radius-skip allowed)"
        lines.append(f"## Phase {idx} — {phase}{tag}")
        lines.append("")
        lines.append(f"_Invoke:_ `{invoke}` — {summary}")
        lines.append("")
        lines.append("- [ ] Phase entered")
        lines.append("- [ ] Verification gate met before moving on")
        lines.append("- [ ] Notes:")
        lines.append("")

    lines.append("## Phase 10 — Ecosystem integration")
    lines.append("")
    lines.append("- [ ] Downstream consumers wired (list them)")
    lines.append("- [ ] Docs updated (`docs/`, README, CHANGELOG entry)")
    lines.append("- [ ] Handoff snippet written for next session")
    lines.append("")

    lines.append("## 6-point DoD checklist (L0 rule #6)")
    lines.append("")
    lines.append("| # | Point | Notes |")
    lines.append("|---|---|---|")
    for i, (label, hint) in enumerate(_DOD_POINTS, start=1):
        lines.append(f"| {i} | **{label}** | {hint} |")
    lines.append("")
    lines.append("> Skip any row without a filed reason = review block (L0 rule #6).")
    lines.append("")

    lines.append("## Commander 5-axis verdict")
    lines.append("")
    lines.append("| # | Axis | Verdict | Evidence |")
    lines.append("|---|---|---|---|")
    for i, (axis, descr) in enumerate(_COMMANDER_AXES, start=1):
        lines.append(f"| {i} | **{axis}** | ⬜ | {descr} |")
    lines.append("")
    lines.append("> Mark ✅ only after evidence filed in the Evidence column.")
    lines.append("")

    return "\n".join(lines) + "\n"


def _phase_mandatory(phase: str, radius: str) -> bool:
    """Decide if a given phase is mandatory for the given radius.

    Red-blue is mandatory only when ``radius == "chaotic"``. Other phases
    (think/PRD/RFC/TDD/review/QA/ship) are structurally always mandatory;
    impl trivially so. But the doc still tags them so that "optional" is
    the explicit opt-out path.
    """
    if phase == "redteam":
        return radius == "chaotic"
    return True


# ---------------------------------------------------------------------------
# Scaffolders (one per kind)
# ---------------------------------------------------------------------------


def scaffold_skill(root: Path, name: str, radius: str) -> list[tuple[Path, str]]:
    """Skill scaffold: SKILL.md + pipeline.md + dod-checklist.md.

    The ``<dir>/<name>/`` subfolder pattern mirrors the
    ``~/.claude/skills/<name>/`` convention used across the global skills
    (credentials / windows / browser / agent).
    """
    base = root / name
    return [
        (base / "SKILL.md", _skill_skill_md(name, radius)),
        (base / "pipeline.md", _skill_pipeline_md(name)),
        (base / "dod-checklist.md", _skill_dod_md(name)),
    ]


def scaffold_subpackage(root: Path, name: str, radius: str) -> list[tuple[Path, str]]:
    """Sub-package scaffold: PEP 621 pyproject + src layout + tests.

    2.33.0 extended: ships ``features.py`` / ``tools.py`` /
    ``skills/__init__.py`` / ``skills/example/SKILL.md`` so the
    scaffold aligns with 2.31.0's four entry-points groups
    (``concinno.tools`` / ``concinno.features`` / ``concinno.skills`` /
    ``concinno.guards``). Every generated file is empty-but-valid so
    the freshly-scaffolded package installs and passes its smoke test
    without the author touching anything.
    """
    pkg_root = root / f"concinno-skills-{name}"
    src_pkg = pkg_root / "src" / f"concinno_skills_{name.replace('-', '_')}"
    return [
        (pkg_root / "pyproject.toml", _subpkg_pyproject(name)),
        (src_pkg / "__init__.py", _subpkg_init(name)),
        # 2.33.0: four new scaffold files wiring up the 2.31.0
        # entry-points groups (features / skills / tools).
        (src_pkg / "features.py", _subpkg_features_py(name)),
        (src_pkg / "tools.py", _subpkg_tools_py(name)),
        (src_pkg / "skills" / "__init__.py", _subpkg_skills_init_py(name)),
        (
            src_pkg / "skills" / "example" / "SKILL.md",
            _subpkg_skills_example_md(name),
        ),
        (pkg_root / "tests" / "__init__.py", ""),
        (
            pkg_root / "tests" / f"test_{name.replace('-', '_')}_smoke.py",
            _subpkg_test(name),
        ),
        (pkg_root / "README.md", _subpkg_readme(name, radius)),
        (pkg_root / "CHANGELOG.md", _subpkg_changelog(name)),
        (pkg_root / "LICENSE", _subpkg_license()),
    ]


def scaffold_guard(root: Path, name: str, radius: str) -> list[tuple[Path, str]]:
    """Guard scaffold: single .py in guards/ + test stub."""
    snake = name.replace("-", "_")
    return [
        (
            root / "src" / "concinno" / "guards" / f"{snake}_guard.py",
            _guard_module(name, radius),
        ),
        (
            root / "tests" / f"test_{snake}_guard.py",
            _guard_test(name),
        ),
    ]


def scaffold_cli(root: Path, name: str, radius: str) -> list[tuple[Path, str]]:
    """CLI scaffold: one subcommand file + test stub."""
    snake = name.replace("-", "_")
    return [
        (
            root / "src" / "concinno" / "cli" / f"{snake}_cmd.py",
            _cli_module(name, radius),
        ),
        (
            root / "tests" / f"test_{snake}_cmd.py",
            _cli_test(name),
        ),
    ]


def scaffold_module(root: Path, name: str, radius: str) -> list[tuple[Path, str]]:
    """Plain module scaffold: one .py + test stub."""
    snake = name.replace("-", "_")
    return [
        (root / "src" / "concinno" / f"{snake}.py", _module_stub(name, radius)),
        (root / "tests" / f"test_{snake}.py", _module_test(name)),
    ]


_SCAFFOLDERS: dict[str, Callable[[Path, str, str], list[tuple[Path, str]]]] = {
    "skill": scaffold_skill,
    "subpackage": scaffold_subpackage,
    "guard": scaffold_guard,
    "cli": scaffold_cli,
    "module": scaffold_module,
}


# ---------------------------------------------------------------------------
# Per-kind template renderers
# ---------------------------------------------------------------------------


def _skill_skill_md(name: str, radius: str) -> str:
    mandatory_note = (
        "⛔ **Phase 0 gate**: radius=`chaotic` → red-blue phase is mandatory."
        if radius == "chaotic"
        else "⛔ **Phase 0 gate**: simple radius → skip bureaucratic phases, go direct."
    )
    return f"""---
name: {name}
description: TODO — one-liner (<200 chars) that names the use case + 5-6 trigger keywords.
triggers:
  - {name}
  - TODO trigger 2
  - TODO trigger 3
user-invocable: true
---

# {name} — TODO short title

> TODO one-sentence tagline. What this skill does + why it exists.

> **You MUST** follow the 9-phase pipeline in `pipeline.md`. Skip any
> phase without a filed reason = review block.
> **You MUST** fill the `dod-checklist.md` before claiming complete.

{mandatory_note}

## Quickstart

```bash
# TODO: smallest runnable example
```

## Workflow router

| User says … | Jump to |
|---|---|
| TODO trigger | `pipeline.md` §phase-N |

## Don'ts

1. TODO anti-pattern 1
2. TODO anti-pattern 2
"""


def _skill_pipeline_md(name: str) -> str:
    header = (
        f"# {name} — 9-phase pipeline\n\n"
        "> Scaffolded by `concinno new-feature`. Each phase has prerequisites,\n"
        "> the existing skill to invoke, and a verification gate before moving on.\n\n"
    )
    body_lines = []
    for i, (phase, invoke, _) in enumerate(_PHASES):
        body_lines.append(f"## Phase {i+1} — {phase}\n")
        body_lines.append(f"_Invoke:_ `{invoke}`\n")
        body_lines.append("- [ ] Entered")
        body_lines.append("- [ ] Gate met")
        body_lines.append("- [ ] Notes:\n")
    return header + "\n".join(body_lines)


def _skill_dod_md(name: str) -> str:
    rows = "\n".join(
        f"| {i+1} | **{label}** | {hint} | ⬜ |"
        for i, (label, hint) in enumerate(_DOD_POINTS)
    )
    axes = "\n".join(
        f"| {i+1} | **{axis}** | ⬜ | {descr} |"
        for i, (axis, descr) in enumerate(_COMMANDER_AXES)
    )
    return f"""# {name} — DoD checklist

## 6-point DoD (L0 rule #6)

| # | Point | Hint | State |
|---|---|---|---|
{rows}

## Commander 5-axis verdict

| # | Axis | Verdict | Evidence |
|---|---|---|---|
{axes}
"""


def _subpkg_pyproject(name: str) -> str:
    pyname = name.replace("-", "_")
    return f"""[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "concinno-skills-{name}"
version = "0.1.0"
description = "TODO — what this skill does"
readme = "README.md"
license = "Apache-2.0"
requires-python = ">=3.10"
dependencies = [
    # 2.33.0 ships scaffolds with all four entry-points groups declared,
    # so consumers must be on 2.33.0+ to discover features/skills/tools.
    "concinno>=2.33.0",
]

[project.optional-dependencies]
dev = ["pytest>=7", "ruff>=0.4"]

# ── Entry-points (pick the ones you need; leave others empty) ────────
#
# Concinno 2.31.0+ discovers third-party plugins through four groups.
# Uncomment + fill each line for every name your package contributes.

[project.entry-points."concinno.tools"]
# Each key is the tool name surfaced via concinno.tools.registry.
# Value points at a Tool subclass via "module:attr".
# example_tool = "concinno_skills_{pyname}.tools:ExampleTool"

[project.entry-points."concinno.features"]
# Each key is a feature namespace. Value resolves to a dict[str, dict]
# keyed by feature name (FEATURE_META-shaped, schema_version=1).
# core = "concinno_skills_{pyname}.features:FEATURE_META"

[project.entry-points."concinno.skills"]
# Each value resolves to a directory containing SKILL.md subdirs.
# root = "concinno_skills_{pyname}.skills:SKILLS_DIR"

[project.entry-points."concinno.guards"]
# BaseGuard subclasses (pre-2.31.0 mechanism). Leave empty when the
# package doesn't ship runtime guards.

[tool.hatch.build.targets.wheel]
packages = ["src/concinno_skills_{pyname}"]

[tool.ruff]
line-length = 100
"""


def _subpkg_features_py(name: str) -> str:
    pyname = name.replace("-", "_")
    return f'''"""FEATURE_META for concinno_skills_{pyname}.

Exported via the ``concinno.features`` entry-point so Concinno's
:func:`concinno.feature_config.iter_all_features_with_origin` discovers
these rows at process start.

Each key is a switchable feature name. Each value is a dict matching
the FEATURE_META schema used by :mod:`concinno.feature_config`::

    FEATURE_META = {{
        "my_gate": {{
            "category": "plugin_gate",
            "description": "one-line English description",
            "enabled": True,
            "schema_version": 1,
            "ziq_autotunable": False,
            "cosmetic": False,
            "params": {{}},
        }},
    }}

An empty dict is valid — the package will load without contributing
feature rows. Wire ``features`` in ``pyproject.toml`` only once you
have at least one entry here.
"""
from __future__ import annotations

FEATURE_META: dict[str, dict] = {{
    # Add feature entries here.
}}
'''


def _subpkg_tools_py(name: str) -> str:
    pyname = name.replace("-", "_")
    return f'''"""Tools for concinno_skills_{pyname}.

Exported via the ``concinno.tools`` entry-point. Each tool inherits
from :class:`concinno.tools.base.Tool` and implements ``call(**kwargs)``.
Delete this file (and the ``concinno.tools`` section in
``pyproject.toml``) if the package contributes no tools.

Example skeleton::

    from concinno.tools.base import Tool


    class ExampleTool(Tool):
        name = "example_tool"
        description = "TODO: one-line description surfaced in the registry."
        is_concurrency_safe = True

        def call(self, **kwargs: object) -> dict:
            return {{"status": "todo"}}
"""
from __future__ import annotations
'''


def _subpkg_skills_init_py(name: str) -> str:
    pyname = name.replace("-", "_")
    return f'''"""Skill roots for concinno_skills_{pyname}.

Exported via the ``concinno.skills`` entry-point. ``SKILLS_DIR``
resolves to the directory containing one subdirectory per skill,
each with its own ``SKILL.md`` frontmatter + body.

Delete the ``concinno.skills`` line in ``pyproject.toml`` if this
package ships no skills.
"""
from __future__ import annotations

from importlib.resources import files

SKILLS_DIR = str(files(__package__))
'''


def _subpkg_skills_example_md(name: str) -> str:
    return f"""---
name: example
description: Example skill scaffolded by `concinno new-feature`. Rename or delete.
triggers: [example, template, {name}]
user-invocable: false
---

# example

I am a placeholder skill. Rename my directory, rewrite my frontmatter,
and replace this body with a real skill description before shipping.

See `docs/how-to-ship-a-skills-package.md` in the Concinno repo for the
full SKILL.md schema and how Concinno's `concinno plugins list` surfaces
packaged skills in the GUI.
"""


def _subpkg_init(name: str) -> str:
    pyname = name.replace("-", "_")
    return f'''"""concinno_skills_{pyname} — TODO one-liner.

Public API::

    from concinno_skills_{pyname} import TODO
"""

__version__ = "0.1.0"

__all__: list[str] = []
'''


def _subpkg_test(name: str) -> str:
    pyname = name.replace("-", "_")
    return f'''"""Smoke test for concinno_skills_{pyname}."""

from __future__ import annotations


def test_import_works() -> None:
    import concinno_skills_{pyname}

    assert concinno_skills_{pyname}.__version__


def test_entry_points_modules_load() -> None:
    """Each 2.31.0 entry-points group has a loadable module.

    Scaffold ships empty-but-valid shapes so the freshly generated
    package installs cleanly. Empty FEATURE_META dict + bare
    ``SKILLS_DIR`` attribute + import-safe ``tools`` module all pass.
    """
    from pathlib import Path

    from concinno_skills_{pyname} import features as _feat
    from concinno_skills_{pyname} import skills as _skills
    from concinno_skills_{pyname} import tools as _tools  # noqa: F401

    assert isinstance(_feat.FEATURE_META, dict)
    assert Path(_skills.SKILLS_DIR).is_dir()
'''


def _subpkg_readme(name: str, radius: str) -> str:
    return f"""# concinno-skills-{name}

TODO — what this skill does.

Scaffolded with radius=`{radius}`. See `docs/{name}-design.md` for the
9-phase design trace.

## Install

```bash
pip install concinno-skills-{name}
```
"""


def _subpkg_changelog(name: str) -> str:
    return f"""# Changelog

All notable changes to `concinno-skills-{name}` will be documented here.

## [0.1.0] - {date.today().isoformat()}

- Initial scaffold (via `concinno new-feature {name} --kind=subpackage`).
"""


def _subpkg_license() -> str:
    return "Apache-2.0 — see ../concinno/LICENSE for the full text.\n"


def _guard_module(name: str, radius: str) -> str:
    snake = name.replace("-", "_")
    camel = "".join(w.capitalize() for w in snake.split("_")) + "Guard"
    return f'''"""concinno.guards.{snake}_guard — TODO one-liner.

Scaffolded with radius=`{radius}`. See ``docs/{name}-design.md`` for the
9-phase design trace + 6-point DoD.
"""

from __future__ import annotations

from concinno.guards.base import BaseGuard, GuardCategory, GuardDecision


class {camel}(BaseGuard):
    """TODO describe what the guard enforces."""

    name = "{snake}"
    category = GuardCategory.SAFETY  # TODO: pick correct category

    def check(self, context: object) -> GuardDecision:
        # TODO: real logic
        return GuardDecision.allow()


__all__ = ["{camel}"]
'''


def _guard_test(name: str) -> str:
    snake = name.replace("-", "_")
    camel = "".join(w.capitalize() for w in snake.split("_")) + "Guard"
    return f'''"""Tests for {camel}."""

from __future__ import annotations


def test_guard_allows_by_default() -> None:
    from concinno.guards.{snake}_guard import {camel}

    g = {camel}()
    decision = g.check(context=object())
    assert decision.is_allow()
'''


def _cli_module(name: str, radius: str) -> str:
    snake = name.replace("-", "_")
    return f'''"""concinno.cli.{snake}_cmd — ``concinno {name}`` subcommand.

Scaffolded with radius=`{radius}`. Follow ``session_switches_cmd.py`` as
the reference implementation pattern (``register(sub) -> None`` + a
module-level ``_handle(args) -> int``).
"""

from __future__ import annotations

import argparse


def _handle(args: argparse.Namespace) -> int:
    """Run the ``concinno {name}`` subcommand. Return exit code."""
    # TODO: real logic
    print("TODO: implement `concinno {name}`")
    return 0


def cmd_{snake}(args: argparse.Namespace) -> None:
    raise SystemExit(_handle(args))


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``{name}`` subcommand."""
    p = subparsers.add_parser("{name}", help="TODO — one-line help for `{name}`")
    p.set_defaults(func=cmd_{snake})


__all__ = ["_handle", "cmd_{snake}", "register"]
'''


def _cli_test(name: str) -> str:
    snake = name.replace("-", "_")
    return f'''"""Tests for the ``concinno {name}`` subcommand."""

from __future__ import annotations

import argparse


def test_handle_returns_zero() -> None:
    from concinno.cli.{snake}_cmd import _handle

    ns = argparse.Namespace()
    assert _handle(ns) == 0
'''


def _module_stub(name: str, radius: str) -> str:
    snake = name.replace("-", "_")
    return f'''"""concinno.{snake} — TODO one-liner.

Scaffolded with radius=`{radius}`. See ``docs/{name}-design.md`` for
design trace.
"""

from __future__ import annotations


def hello() -> str:
    return "hello from {name}"


__all__ = ["hello"]
'''


def _module_test(name: str) -> str:
    snake = name.replace("-", "_")
    return f'''"""Tests for concinno.{snake}."""

from __future__ import annotations


def test_hello_returns_greeting() -> None:
    from concinno.{snake} import hello

    assert "{name}" in hello()
'''


# ---------------------------------------------------------------------------
# Plan building / execution
# ---------------------------------------------------------------------------


def build_plan(name: str, kind: str, radius: str, target_dir: Path) -> ScaffoldPlan:
    """Build a ScaffoldPlan for given arguments. Pure function — no I/O."""
    scaffolder = _SCAFFOLDERS[kind]
    files = scaffolder(target_dir, name, radius)

    # The design doc lives at ``<target>/docs/<name>-design.md`` for any
    # kind — single convention.
    design_path = target_dir / "docs" / f"{name}-design.md"
    files.append((design_path, render_design_doc(name, kind, radius)))

    return ScaffoldPlan(root=target_dir, name=name, kind=kind, radius=radius, files=files)


def execute_plan(plan: ScaffoldPlan) -> None:
    """Materialise a ScaffoldPlan on disk. Raises if any target exists."""
    # Pre-flight: ensure no file target already exists.
    for path, _ in plan.files:
        if path.exists():
            raise FileExistsError(f"target already exists: {path}")

    for path, content in plan.files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def _handle(args: argparse.Namespace) -> int:
    """Core ``concinno new-feature`` handler. Returns exit code."""
    name: str = getattr(args, "name", "")
    kind: str = getattr(args, "kind", "skill") or "skill"
    radius: str = getattr(args, "radius", "complicated") or "complicated"
    dry_run: bool = bool(getattr(args, "dry_run", False))
    dir_arg: str = getattr(args, "dir", "") or ""

    if not name:
        print("error: <name> is required", file=sys.stderr)
        return 2
    if kind not in _VALID_KINDS:
        print(f"error: --kind must be one of {_VALID_KINDS}", file=sys.stderr)
        return 2
    if radius not in _VALID_RADII:
        print(f"error: --radius must be one of {_VALID_RADII}", file=sys.stderr)
        return 2

    target_dir = Path(dir_arg).resolve() if dir_arg else Path.cwd()
    plan = build_plan(name, kind, radius, target_dir)

    if dry_run:
        print(plan.describe())
        return 0

    try:
        execute_plan(plan)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Scaffolded {kind} '{name}' ({len(plan.files)} file(s)) under {target_dir}")
    print(f"  design doc: docs/{name}-design.md")
    print("  Next: fill Phase 0 intent anchor, then run `/think {name}`.".format(name=name))
    return 0


def cmd_new_feature(args: argparse.Namespace) -> None:
    raise SystemExit(_handle(args))


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``new-feature`` subcommand on an argparse parent."""
    p = subparsers.add_parser(
        "new-feature",
        help="Scaffold a new skill / subpackage / guard / CLI / module with 9-phase design doc",
    )
    p.add_argument("name", help="Feature name (kebab-case recommended)")
    p.add_argument(
        "--kind",
        choices=_VALID_KINDS,
        default="skill",
        help="What to scaffold (default: skill)",
    )
    p.add_argument(
        "--radius",
        choices=_VALID_RADII,
        default="complicated",
        help="Explosion radius — governs red-blue phase mandatory/optional (default: complicated)",
    )
    p.add_argument(
        "--dir",
        default="",
        help="Target directory (default: cwd)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan; write nothing",
    )
    p.set_defaults(func=cmd_new_feature)


__all__ = [
    "build_plan",
    "execute_plan",
    "render_design_doc",
    "scaffold_skill",
    "scaffold_subpackage",
    "scaffold_guard",
    "scaffold_cli",
    "scaffold_module",
    "cmd_new_feature",
    "register",
    "ScaffoldPlan",
    "_handle",
]
