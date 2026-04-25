"""Death command #1 enforcement.

Forbids any PAT-003 marketing string from leaking into the OSS
persona module surface (code / docs / tests / changelog persona
section). This is a CI gate — failure here means we're about to
self-disclose proprietary algorithm naming and lose the 12-month
prior-art protection window.

The forbidden list is intentionally hard-coded here (not loaded
from config) so an attacker / careless contributor cannot weaken
the enforcement by editing a config file.
"""

from __future__ import annotations

import re
from pathlib import Path

# Hard-coded forbidden tokens. Match case-insensitively.
FORBIDDEN_PATTERNS: list[str] = [
    r"\btension_field\b",
    r"\briverbed\b",
    r"\bstake_anchor\b",
    r"\bCTEE\b",
    r"\bRMT\b",
    r"\bSAMD\b",
    r"\bemotional_emergence\b",
    r"\bconsciousness_tension\b",
    r"意識張力",
    r"河床",
    r"樁錨定",
    r"情緒湧現",
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _scan_paths() -> list[Path]:
    """Paths the death command #1 grep gate covers."""
    persona_src = REPO_ROOT / "src" / "concinno" / "persona"
    persona_tests = REPO_ROOT / "tests" / "persona"
    persona_cli = REPO_ROOT / "src" / "concinno" / "cli" / "persona_cmd.py"
    docs_dir = REPO_ROOT / "src" / "concinno" / "docs"

    paths: list[Path] = []
    if persona_src.is_dir():
        paths.extend(p for p in persona_src.rglob("*.py") if p.is_file())
    if persona_tests.is_dir():
        paths.extend(p for p in persona_tests.rglob("*.py") if p.is_file())
    if persona_cli.is_file():
        paths.append(persona_cli)
    if docs_dir.is_dir():
        for p in docs_dir.rglob("*"):
            if p.is_file() and "persona" in p.name.lower():
                paths.append(p)
    return paths


def test_no_pat003_marketing_strings_in_persona_module() -> None:
    """Every persona-related file must be free of forbidden marketing strings.

    This is the explicit gate referenced in the Track 1 spec
    (death command #1). Adding any FORBIDDEN_PATTERNS hit to OSS
    triggers a 12-month prior-art clock and breaks the PCT
    international filing path.
    """
    paths = _scan_paths()
    assert paths, "no persona files discovered — scan path may be wrong"

    violations: list[tuple[str, str, int, str]] = []
    compiled = [re.compile(p, re.IGNORECASE) for p in FORBIDDEN_PATTERNS]
    pattern_for = dict(zip(compiled, FORBIDDEN_PATTERNS, strict=True))

    for path in paths:
        # Skip the enforcement file itself (it has to mention the
        # forbidden tokens by definition to be the regex source).
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for cre in compiled:
            for m in cre.finditer(text):
                line_no = text[: m.start()].count("\n") + 1
                violations.append((str(path), pattern_for[cre], line_no, m.group(0)))

    assert not violations, (
        "DEATH COMMAND #1 VIOLATED — forbidden PAT-003 marketing strings "
        "found in OSS persona surface:\n"
        + "\n".join(
            f"  {path}:{line} matches {pattern!r} ({hit!r})"
            for path, pattern, line, hit in violations
        )
    )


def test_persona_schema_forbids_extra_fields() -> None:
    """PersonaSchema must reject unknown fields (defence in depth).

    Even if a contributor sneaks a forbidden field name into
    schema.py, this test would only fail when something tried to
    use it. The grep gate above is the primary guard; this one
    catches future accidents.
    """
    from pydantic import ValidationError

    from concinno.persona.schema import PersonaSchema

    try:
        PersonaSchema(name="x", tension_field=0.5)  # type: ignore[call-arg]
    except ValidationError:
        return
    except TypeError:
        return
    raise AssertionError(
        "PersonaSchema accepted a 'tension_field' kwarg — extra='forbid' regression"
    )
