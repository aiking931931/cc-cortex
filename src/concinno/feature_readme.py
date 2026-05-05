"""concinno.feature_readme — render FEATURE_META to Markdown and sync it
into README.md between anchor comments.

@module concinno.feature_readme
@responsibility Maintain a single source of truth: FEATURE_META is
    canonical, the GUI lists it sorted category→name, and the README
    shows the identical list in the identical order. This module
    renders that list as a Markdown table and (optionally) injects it
    into a README between ``<!-- BEGIN: feature-index -->`` and
    ``<!-- END: feature-index -->`` anchors.

Usage::

    concinno features export-readme              # print to stdout
    concinno features sync-readme [path]         # inject into file

Enterprise invariant: GUI sort order and README sort order MUST match.
If one changes, change both via the shared SORT_KEY below.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

__all__ = [
    "render_markdown",
    "sync_readme",
    "ANCHOR_BEGIN",
    "ANCHOR_END",
    "SORT_KEY",
]

ANCHOR_BEGIN = "<!-- BEGIN: feature-index -->"
ANCHOR_END = "<!-- END: feature-index -->"


def SORT_KEY(entry: tuple[str, dict]) -> tuple[str, str]:
    """Deterministic category → name sort identical to the GUI default."""
    name, meta = entry
    return (meta.get("category") or "zz", name)


def _escape_md(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_markdown(
    features: Iterable[tuple[str, dict]] | None = None,
    *,
    effect_scopes: dict[str, str] | None = None,
) -> str:
    """Render a Markdown feature table in GUI sort order.

    Parameters let callers inject a stub registry for tests — production
    path reads from :data:`concinno.feature_config.FEATURE_META` and
    :func:`concinno.gui.server._effect_scope` when arguments are omitted.
    """
    if features is None:
        from concinno.feature_config import FEATURE_META
        features = list(FEATURE_META.items())
    if effect_scopes is None:
        try:
            from concinno.gui.server import _effect_scope as _es
            effect_scopes = {n: _es(n) for n, _ in features}
        except Exception:
            effect_scopes = {}
    entries = sorted(features, key=SORT_KEY)
    lines = [
        "| Feature | Category | Effect scope | ZIQ-tunable | Description |",
        "|---------|----------|--------------|-------------|-------------|",
    ]
    for name, meta in entries:
        cat = meta.get("category") or "?"
        desc = _escape_md(meta.get("description") or "")
        ziq = "✓" if meta.get("ziq_autotunable") else ""
        scope = effect_scopes.get(name, "immediate")
        lines.append(
            f"| `{name}` | {cat} | {scope} | {ziq} | {desc} |"
        )
    return "\n".join(lines) + "\n"


def sync_readme(path: str | Path | None = None) -> bool:
    """Inject rendered table between anchor comments in ``path``.

    Returns True when the file was modified, False when content was
    already in sync. Creates the anchor block after a ``## Feature
    Switches`` heading when absent.
    """
    readme = Path(path) if path else _default_readme()
    if not readme.is_file():
        raise FileNotFoundError(readme)
    text = readme.read_text(encoding="utf-8")
    table = render_markdown()
    new_block = (
        f"{ANCHOR_BEGIN}\n"
        f"<!-- Auto-generated from FEATURE_META. Run "
        f"`concinno features sync-readme` to refresh. -->\n\n"
        f"{table}\n"
        f"{ANCHOR_END}"
    )
    if ANCHOR_BEGIN in text and ANCHOR_END in text:
        before, _, rest = text.partition(ANCHOR_BEGIN)
        _, _, after = rest.partition(ANCHOR_END)
        updated = f"{before}{new_block}{after}"
    else:
        section = f"\n\n## Feature Switches\n\n{new_block}\n"
        updated = _insert_before_anchor(text, "## CLI", section)
    if updated == text:
        return False
    readme.write_text(updated, encoding="utf-8")
    return True


def _insert_before_anchor(text: str, anchor: str, section: str) -> str:
    idx = text.find(anchor)
    if idx < 0:
        return text + section
    return text[:idx] + section + text[idx:]


def _default_readme() -> Path:
    # src/concinno/feature_readme.py → ../../../README.md
    return Path(__file__).resolve().parents[2] / "README.md"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``concinno features …``."""
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: concinno features {export-readme|sync-readme [path]}")
        return 0
    cmd = argv[0]
    if cmd == "export-readme":
        sys.stdout.write(render_markdown())
        return 0
    if cmd == "sync-readme":
        target = argv[1] if len(argv) > 1 else None
        changed = sync_readme(target)
        print(f"{'updated' if changed else 'already in sync'}: "
              f"{target or _default_readme()}")
        return 0
    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
