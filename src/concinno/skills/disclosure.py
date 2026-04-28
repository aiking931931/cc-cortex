"""concinno.skills.disclosure — Three-layer Skill progressive disclosure.

@module skills.disclosure
@responsibility Surface registered Claude Code skills via three lazy-loaded
    layers so the agent only pays L2/L3 token cost when a skill is actually
    relevant. Layer split:

    * **L1** (always-loaded) — frontmatter only: ``name`` + ``description`` +
      ``triggers``. ~50 tokens per skill, indexed up front.
    * **L2** (load-on-route) — first ≤50 lines of ``SKILL.md`` summary.
      ~500 tokens, fetched only when :meth:`SkillDisclosure.route` returns
      a hit above ``min_route_score``.
    * **L3** (load-on-explicit-invoke) — full ``SKILL.md`` + sibling files
      under the skill directory. Unbounded, fetched only when the user
      explicitly invokes the skill.

@dependencies stdlib only. ``concinno.ziq_emit_helpers`` is best-effort and
    every emit call is swallowed via the helper's own try/except so a broken
    bus cannot break a routing call.
@exports SkillDisclosure, SkillL1, RouteResult, default_skills_dir

Routing math (ZIQ-aligned):

    P(skill | query) ∝ SPS(skill_description, query) × FTRL_weight(skill)

    * **SPS** — token-overlap cosine between the L1 description+triggers and
      the user query. Cheap, no external dep. SPS substitution-ready: a
      future SBERT or Voyage embedding can drop in via the
      ``sps_scorer`` constructor kwarg without changing the public API.
    * **FTRL** — per-skill exponentially-decayed reward stored in-process.
      ``observe_use(name, helpful=True)`` bumps the weight, ``False``
      decays it. Reward signals also fan out via
      ``concinno.ziq_emit_helpers.emit_classification_outcome`` so the
      ZIQ outcome bus can fold the data into the global FTRL learner.

Caching:

    L1 index is cached for ``l1_cache_ttl_sec`` seconds (default 300 s).
    Cheaper than re-walking the skills dir on every prompt; refresh on TTL
    expiry or via :meth:`refresh_l1`.

Thread safety:

    A single ``threading.RLock`` guards mutable state (cache + FTRL
    weights). Reads acquire the lock briefly; the SPS scorer and L2/L3
    file reads run outside the lock.

OSS-cleanroom note:

    The neighbouring ``concinno-skills-memory`` package ships a module
    also named ``progressive_disclosure``. That module governs *memory*
    snippet promotion and shares no schema, file format, or routing
    semantics with this one. We do not import or alias it here.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

__all__ = [
    "DEFAULT_FTRL_ALPHA",
    "DEFAULT_FTRL_DECAY",
    "DEFAULT_L1_CACHE_TTL_SEC",
    "DEFAULT_MIN_ROUTE_SCORE",
    "DEFAULT_TOP_K",
    "RouteResult",
    "SkillDisclosure",
    "SkillL1",
    "default_skills_dir",
]


# ── tunables (mirrored in feature_config.FEATURE_META["skill_disclosure"]) ──

DEFAULT_TOP_K = 5
DEFAULT_L1_CACHE_TTL_SEC = 300
DEFAULT_MIN_ROUTE_SCORE = 0.15
DEFAULT_FTRL_ALPHA = 0.1
DEFAULT_FTRL_DECAY = 0.99

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<body>.*?)\n---\s*\n",
    re.DOTALL,
)
_FRONTMATTER_KEY_RE = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(?P<value>.*?)\s*$",
)
_TOKEN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9_\-]*"
    r"|[一-鿿]"
    r"|[가-힯]+"
    r"|[぀-ヿ]+",
)


def default_skills_dir() -> Path:
    """Resolve the skills directory.

    Order of precedence:
        1. ``CONCINNO_SKILLS_DIR`` env override.
        2. ``$HOME/.claude/skills`` (Claude Code default).
    """
    env = os.environ.get("CONCINNO_SKILLS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "skills"


# ── data classes ───────────────────────────────────────────


@dataclass(frozen=True)
class SkillL1:
    """Always-on L1 record — frontmatter snapshot.

    ``triggers`` are kept English-canonical per
    ``rules/L1/multilingual_triggers.md``; semantic match across languages
    happens in :meth:`SkillDisclosure.route` via the SPS scorer, not in
    the index file.
    """

    name: str
    description: str = ""
    triggers: tuple[str, ...] = field(default_factory=tuple)
    path: Optional[Path] = None


@dataclass(frozen=True)
class RouteResult:
    """One ranked candidate from :meth:`SkillDisclosure.route`."""

    name: str
    score: float
    sps: float
    ftrl: float


# ── tokeniser / SPS ────────────────────────────────────────


def _tokenise(text: str) -> list[str]:
    """Lower-case Latin words + per-CJK-char + Hangul / Kana chunks.

    Matches the cheap-stage tokeniser in
    :mod:`concinno.skill_proactive_router` so SPS scores between the two
    routers stay comparable.
    """
    if not text:
        return []
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text)]


def _cosine_overlap(a: Sequence[str], b: Sequence[str]) -> float:
    """Token-overlap cosine in ``[0, 1]``.

    Returns 0.0 for either side empty. No external embedding dep — a
    swap-in scorer can be passed via ``SkillDisclosure(sps_scorer=...)``
    when sentence-bert / Voyage is available.
    """
    if not a or not b:
        return 0.0
    set_a: set[str] = set(a)
    set_b: set[str] = set(b)
    inter = len(set_a & set_b)
    if inter == 0:
        return 0.0
    return inter / math.sqrt(len(set_a) * len(set_b))


def default_sps_scorer(query: str, l1: SkillL1) -> float:
    """Default SPS scorer: token cosine over (description + triggers).

    Empty query / empty description -> 0.0.
    """
    q_tokens = _tokenise(query)
    if not q_tokens:
        return 0.0
    skill_text = l1.description + " " + " ".join(l1.triggers)
    return _cosine_overlap(q_tokens, _tokenise(skill_text))


# ── frontmatter parser (cheap, no PyYAML dep) ───────────────


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse the YAML-ish frontmatter block at the head of a SKILL.md.

    We only need flat ``key: value`` pairs — the upstream skill-creator
    schema defines ``name``, ``description``, and (free-form) trigger
    strings inline in the description. Multi-line scalars and nested
    structures are not supported here on purpose: the L1 layer is meant
    to stay dirt-cheap; complex parsing belongs in L2/L3.
    """
    if not text:
        return {}
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    out: dict[str, str] = {}
    for line in match.group("body").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _FRONTMATTER_KEY_RE.match(line)
        if not m:
            continue
        value = m.group("value").strip().strip('"').strip("'")
        out[m.group("key").lower()] = value
    return out


def _extract_triggers(description: str) -> tuple[str, ...]:
    """Pull comma-separated trigger phrases out of the description.

    The Claude Code skill-creator convention is to append
    ``Triggers on "X", "Y", "Z"`` or ``觸發詞：X、Y、Z`` to the
    description. We extract whatever we find; absent any markers we
    return an empty tuple and the SPS scorer falls back to the
    description body.
    """
    if not description:
        return ()
    candidates: list[str] = []
    for pattern in (
        r'Triggers? on\s+(.*?)(?:\.|$)',
        r'觸發詞[：:]\s*(.*?)(?:\.|$)',
        r'触发词[：:]\s*(.*?)(?:\.|$)',
    ):
        for m in re.finditer(pattern, description, flags=re.IGNORECASE):
            chunk = m.group(1)
            for raw in re.split(r'[",、，;]+', chunk):
                t = raw.strip().strip('"').strip("'").strip()
                if t and len(t) <= 60:
                    candidates.append(t)
    return tuple(dict.fromkeys(candidates))


# ── main class ─────────────────────────────────────────────


class SkillDisclosure:
    """Three-layer Skill progressive disclosure router.

    Public surface (per HP7 spec):

    * :meth:`list_l1` — always-on L1 index snapshot (cheap).
    * :meth:`route` — top-k ranking of L1 candidates for a user query.
    * :meth:`load_l2` — promote one skill to L2 (≤50-line summary).
    * :meth:`load_l3` — promote one skill to L3 (full SKILL.md + sibs).
    * :meth:`observe_use` — FTRL signal from downstream invocation.
    * :meth:`refresh_l1` — force a re-scan ignoring cache TTL.
    """

    def __init__(
        self,
        *,
        skills_dir: Optional[Path] = None,
        top_k: int = DEFAULT_TOP_K,
        l1_cache_ttl_sec: int = DEFAULT_L1_CACHE_TTL_SEC,
        min_route_score: float = DEFAULT_MIN_ROUTE_SCORE,
        ftrl_alpha: float = DEFAULT_FTRL_ALPHA,
        ftrl_decay: float = DEFAULT_FTRL_DECAY,
        sps_scorer: Optional[Callable[[str, SkillL1], float]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._skills_dir = skills_dir or default_skills_dir()
        self._top_k = max(1, int(top_k))
        self._ttl = max(1, int(l1_cache_ttl_sec))
        self._min_score = max(0.0, min(1.0, float(min_route_score)))
        self._alpha = max(0.0, min(1.0, float(ftrl_alpha)))
        self._decay = max(0.0, min(1.0, float(ftrl_decay)))
        self._sps = sps_scorer or default_sps_scorer
        self._clock = clock
        self._lock = threading.RLock()
        self._l1_cache: list[SkillL1] = []
        self._l1_cache_ts: float = 0.0
        self._ftrl: dict[str, float] = {}

    # ── L1 ──────────────────────────────────────────────

    def list_l1(self) -> list[SkillL1]:
        """Return the always-on L1 index, refreshing the cache if stale."""
        with self._lock:
            now = self._clock()
            if (
                self._l1_cache
                and (now - self._l1_cache_ts) < self._ttl
            ):
                return list(self._l1_cache)
            self._l1_cache = self._scan_l1()
            self._l1_cache_ts = now
            return list(self._l1_cache)

    def refresh_l1(self) -> list[SkillL1]:
        """Force a re-scan ignoring cache TTL; return the fresh index."""
        with self._lock:
            self._l1_cache = self._scan_l1()
            self._l1_cache_ts = self._clock()
            return list(self._l1_cache)

    def _scan_l1(self) -> list[SkillL1]:
        """Walk ``skills_dir`` for ``<skill>/SKILL.md`` and parse frontmatter.

        Skipped silently:
            * non-existent directory
            * subdirs without ``SKILL.md``
            * files without parseable frontmatter (we still index by name
              when possible — empty ``description`` is harmless to SPS)
            * files exceeding ``MAX_FRONTMATTER_BYTES``
              (4096 — frontmatter alone is never that big)
        """
        out: list[SkillL1] = []
        root = self._skills_dir
        if not root.is_dir():
            return out
        max_bytes = 4096
        for child in sorted(root.iterdir()):
            try:
                if not child.is_dir():
                    continue
                skill_md = child / "SKILL.md"
                if not skill_md.is_file():
                    continue
                head = skill_md.read_text(
                    encoding="utf-8",
                    errors="replace",
                )[:max_bytes]
                fm = _parse_frontmatter(head)
                name = fm.get("name") or child.name
                description = fm.get("description", "")
                triggers = _extract_triggers(description)
                out.append(
                    SkillL1(
                        name=str(name),
                        description=description,
                        triggers=triggers,
                        path=child,
                    )
                )
            except Exception:
                continue
        return out

    # ── routing ─────────────────────────────────────────

    def route(
        self,
        query: str,
        top_k: Optional[int] = None,
    ) -> list[RouteResult]:
        """Rank L1 skills by ``SPS × FTRL`` for the given query.

        Filters scores below ``min_route_score`` *after* combining SPS and
        FTRL. ``top_k`` defaults to the constructor value; it is capped at
        the live skill count so callers can't ask for more than exist.
        """
        if not query or not query.strip():
            return []
        index = self.list_l1()
        if not index:
            return []
        k = self._top_k if top_k is None else max(1, int(top_k))
        k = min(k, len(index))
        ranked: list[RouteResult] = []
        for l1 in index:
            try:
                sps = float(self._sps(query, l1))
            except Exception:
                sps = 0.0
            sps = max(0.0, min(1.0, sps))
            ftrl = self._ftrl_weight(l1.name)
            score = sps * ftrl
            if score < self._min_score:
                continue
            ranked.append(
                RouteResult(name=l1.name, score=score, sps=sps, ftrl=ftrl),
            )
        ranked.sort(key=lambda r: (-r.score, r.name))
        return ranked[:k]

    # ── L2 / L3 promotion ───────────────────────────────

    def load_l2(self, skill_name: str) -> str:
        """Return the first ≤50-line summary of ``<skill>/SKILL.md``.

        Returns ``""`` for missing / unreadable skills — callers must
        treat empty as "no L2 available" and fall back to L1 metadata.
        """
        target = self._resolve(skill_name)
        if target is None:
            return ""
        skill_md = target / "SKILL.md"
        if not skill_md.is_file():
            return ""
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        # Strip frontmatter so the L2 summary is body-only.
        m = _FRONTMATTER_RE.match(text)
        if m:
            text = text[m.end():]
        lines = text.splitlines()
        return "\n".join(lines[:50])

    def load_l3(self, skill_name: str) -> dict[str, str]:
        """Return the full skill bundle: ``{relative_path: content}``.

        Walks the skill directory recursively but caps total size at
        ~512 KiB and per-file at 64 KiB so a misplaced binary cannot
        balloon the prompt. Files we cannot decode are skipped.
        """
        target = self._resolve(skill_name)
        if target is None:
            return {}
        out: dict[str, str] = {}
        total = 0
        per_file_cap = 64 * 1024
        total_cap = 512 * 1024
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() in {".png", ".jpg", ".gif", ".pdf", ".bin"}:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > per_file_cap:
                continue
            if total + size > total_cap:
                break
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            out[str(path.relative_to(target).as_posix())] = content
            total += size
        return out

    def _resolve(self, skill_name: str) -> Optional[Path]:
        """Map ``skill_name`` to its directory via the L1 index."""
        if not skill_name:
            return None
        for l1 in self.list_l1():
            if l1.name == skill_name and l1.path is not None:
                return l1.path
        return None

    # ── FTRL telemetry ──────────────────────────────────

    def observe_use(self, skill_name: str, helpful: bool) -> None:
        """Update per-skill FTRL weight + emit a ZIQ classification outcome.

        ``helpful=True``  → reward 1.0 toward the "useful" class.
        ``helpful=False`` → reward 0.0 (decay only).

        The local in-process weight is updated immediately so a
        subsequent :meth:`route` call reflects the signal without
        round-tripping through the bus. The bus emit is best-effort.
        """
        if not skill_name:
            return
        with self._lock:
            current = self._ftrl.get(skill_name, 1.0)
            target = 1.0 if helpful else 0.0
            updated = self._decay * current + self._alpha * (target - current)
            updated = max(0.0, min(2.0, updated))
            self._ftrl[skill_name] = updated
        try:
            from concinno.ziq_emit_helpers import emit_classification_outcome

            emit_classification_outcome(
                "skill_disclosure.helpful",
                value=skill_name,
                correct=bool(helpful),
                source="concinno.skills.disclosure.observe_use",
                metadata={"skill": skill_name},
            )
        except Exception:
            pass

    def _ftrl_weight(self, skill_name: str) -> float:
        with self._lock:
            return self._ftrl.get(skill_name, 1.0)

    # ── helpers for callers (hooks etc.) ────────────────

    def names(self) -> Iterable[str]:
        return (l1.name for l1 in self.list_l1())
