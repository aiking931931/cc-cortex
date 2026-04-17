"""Cognitive Skill Router — discover, override, classify, and track cognitive skills.

The router provides a two-tier skill system:
- **Builtin skills**: Ship with concinno (``_cognitive/`` package data).
  High-quality defaults covering common thinking patterns.
- **User skills**: Live in ``.claude/skills/<name>/SKILL.md``.
  Override builtins by matching name.

Override rule: user skill > builtin skill (same name → user wins).

Usage::

    from concinno.skill_router import SkillRouter

    router = SkillRouter(user_skills_dir=".claude/skills")
    skill = router.get("three_layer")        # user override or builtin
    suggested = router.classify("I'm stuck") # → ["debug_loop", "three_layer"]
    router.record_outcome("debug_loop", success=True)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ── Builtin skills directory (shipped with package) ──────────

_BUILTIN_DIR = Path(__file__).parent / "_cognitive"

# ── Classifier signal patterns (loaded from i18n locale files) ────

_SIGNAL_SKILLS = (
    "three_layer", "first_principles", "debug_loop", "prompt_select",
    "decision_journal", "pdca", "judgment", "awareness", "learning_loop",
)

_signals_cache: dict[str, list[re.Pattern[str]]] | None = None


def _get_signals() -> dict[str, list[re.Pattern[str]]]:
    """Load skill signal patterns from all active locales."""
    global _signals_cache
    if _signals_cache is not None:
        return _signals_cache

    from concinno.i18n import patterns as i18n_patterns

    result: dict[str, list[re.Pattern[str]]] = {}
    for skill in _SIGNAL_SKILLS:
        raw = i18n_patterns(f"skill_signals.{skill}")
        if raw:
            # Combine all patterns into one alternation per skill
            combined = "|".join(raw)
            result[skill] = [re.compile(f"({combined})", re.I)]
    _signals_cache = result
    return result


@dataclass
class SkillInfo:
    """Metadata for a discovered skill."""

    name: str
    path: Path
    is_builtin: bool
    description: str = ""
    content: str = ""
    auto_apply: list[str] | None = None  # CC native globs


@dataclass
class SkillOutcome:
    """Track how effective a skill was."""

    skill_name: str
    uses: int = 0
    successes: int = 0
    failures: int = 0

    @property
    def success_rate(self) -> float:
        return self.successes / self.uses if self.uses > 0 else 0.0


class SkillRouter:
    """Discover, resolve, classify, and track cognitive skills.

    Parameters
    ----------
    user_skills_dir : str | Path | None
        User's skill directory (e.g. ``.claude/skills``).
        Skills here override builtins with the same name.
    builtin_dir : str | Path | None
        Builtin skills directory. Defaults to package ``_cognitive/``.
    tracking_path : str | Path | None
        JSON file for outcome tracking. ``None`` disables tracking.
    """

    def __init__(
        self,
        user_skills_dir: str | Path | None = None,
        builtin_dir: str | Path | None = None,
        tracking_path: str | Path | None = None,
    ):
        self.user_dir = Path(user_skills_dir) if user_skills_dir else None
        self.builtin_dir = Path(builtin_dir) if builtin_dir else _BUILTIN_DIR
        self.tracking_path = Path(tracking_path) if tracking_path else None
        self._cache: dict[str, SkillInfo] = {}
        self._outcomes: dict[str, SkillOutcome] = {}
        if self.tracking_path and self.tracking_path.exists():
            self._load_tracking()

    # ── Discovery ─────────────────────────────────────────────

    def discover(self) -> dict[str, SkillInfo]:
        """Scan builtin and user directories, return all available skills.

        User skills override builtins with the same name.
        """
        skills: dict[str, SkillInfo] = {}

        # 1. Builtin skills (lower priority)
        if self.builtin_dir.is_dir():
            for md in sorted(self.builtin_dir.glob("*.md")):
                name = md.stem
                desc, content, aa = _parse_skill_md(md)
                skills[name] = SkillInfo(
                    name=name,
                    path=md,
                    is_builtin=True,
                    description=desc,
                    content=content,
                    auto_apply=aa,
                )

        # 2. User skills (higher priority — override builtins)
        if self.user_dir and self.user_dir.is_dir():
            for skill_dir in sorted(self.user_dir.iterdir()):
                skill_md = skill_dir / "SKILL.md"
                if skill_md.is_file():
                    name = skill_dir.name
                    desc, content, aa = _parse_skill_md(skill_md)
                    skills[name] = SkillInfo(
                        name=name,
                        path=skill_md,
                        is_builtin=False,
                        description=desc,
                        content=content,
                        auto_apply=aa,
                    )

        self._cache = skills
        return skills

    def get(self, name: str) -> Optional[SkillInfo]:
        """Get a skill by name. User override > builtin."""
        if not self._cache:
            self.discover()
        return self._cache.get(name)

    def list_names(self) -> list[str]:
        """List all available skill names."""
        if not self._cache:
            self.discover()
        return list(self._cache.keys())

    def list_builtins(self) -> list[str]:
        """List builtin skill names (for customization guidance)."""
        if not self._cache:
            self.discover()
        return [n for n, s in self._cache.items() if s.is_builtin]

    def list_overridden(self) -> list[str]:
        """List builtin skills that have user overrides."""
        if not self._cache:
            self.discover()
        user_names = set()
        if self.user_dir and self.user_dir.is_dir():
            for d in self.user_dir.iterdir():
                if (d / "SKILL.md").is_file():
                    user_names.add(d.name)
        builtin_names = set()
        if self.builtin_dir.is_dir():
            builtin_names = {md.stem for md in self.builtin_dir.glob("*.md")}
        return sorted(user_names & builtin_names)

    # ── Path matching (CC native auto_apply) ───────────────────

    def match_path(self, filepath: str) -> list[str]:
        """Return skill names whose auto_apply globs match filepath.

        This enables CC-native dual-track triggering: CCC semantic
        classify() + CC auto_apply path globs.
        """
        import fnmatch

        if not self._cache:
            self.discover()
        matched = []
        for name, skill in self._cache.items():
            if not skill.auto_apply:
                continue
            for pattern in skill.auto_apply:
                if fnmatch.fnmatch(filepath, pattern):
                    matched.append(name)
                    break
        return matched

    # ── Classification ────────────────────────────────────────

    def classify(self, context: str, top_k: int = 2) -> list[str]:
        """Classify context and suggest appropriate cognitive skills.

        Scores each skill by how many signal patterns match the context.
        Returns top_k skill names sorted by relevance.
        """
        if not self._cache:
            self.discover()

        scores: dict[str, int] = {}
        for skill_name, patterns in _get_signals().items():
            if skill_name not in self._cache:
                continue
            score = sum(1 for p in patterns if p.search(context))
            if score > 0:
                scores[skill_name] = score

        ranked = sorted(scores, key=lambda k: scores[k], reverse=True)
        return ranked[:top_k]

    # ── Outcome tracking ──────────────────────────────────────

    def record_outcome(self, skill_name: str, success: bool) -> None:
        """Record whether a skill use was successful."""
        if skill_name not in self._outcomes:
            self._outcomes[skill_name] = SkillOutcome(skill_name=skill_name)
        outcome = self._outcomes[skill_name]
        outcome.uses += 1
        if success:
            outcome.successes += 1
        else:
            outcome.failures += 1
        if self.tracking_path:
            self._save_tracking()

    def get_stats(self) -> dict[str, dict]:
        """Return effectiveness stats for all tracked skills."""
        return {
            name: {
                "uses": o.uses,
                "successes": o.successes,
                "failures": o.failures,
                "success_rate": round(o.success_rate, 2),
            }
            for name, o in self._outcomes.items()
        }

    # ── Persistence ───────────────────────────────────────────

    def _load_tracking(self) -> None:
        try:
            data = json.loads(self.tracking_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
            for name, vals in data.items():
                self._outcomes[name] = SkillOutcome(
                    skill_name=name,
                    uses=vals.get("uses", 0),
                    successes=vals.get("successes", 0),
                    failures=vals.get("failures", 0),
                )
        except (json.JSONDecodeError, OSError):
            pass

    def _save_tracking(self) -> None:
        if not self.tracking_path:
            return
        self.tracking_path.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
        data = {
            name: {
                "uses": o.uses,
                "successes": o.successes,
                "failures": o.failures,
            }
            for name, o in self._outcomes.items()
        }
        self.tracking_path.write_text(  # type: ignore[union-attr]
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ── Helpers ───────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)
_AUTO_APPLY_RE = re.compile(
    r"^auto_apply:\s*\[(.+?)\]$", re.MULTILINE
)


def _parse_skill_md(
    path: Path,
) -> tuple[str, str, list[str] | None]:
    """Parse skill markdown, return (desc, content, auto_apply)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ("", "", None)
    desc = ""
    auto_apply: list[str] | None = None
    match = _FRONTMATTER_RE.match(text)
    if match:
        fm = match.group(1)
        desc_match = _DESC_RE.search(fm)
        if desc_match:
            desc = desc_match.group(1).strip()
        aa_match = _AUTO_APPLY_RE.search(fm)
        if aa_match:
            raw = aa_match.group(1)
            auto_apply = [
                s.strip().strip("\"'")
                for s in raw.split(",")
                if s.strip()
            ]
    return (desc, text, auto_apply)
