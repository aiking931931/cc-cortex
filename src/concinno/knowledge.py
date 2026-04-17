"""concinno.knowledge — Automatic learning extraction from transcripts.

@module knowledge
@responsibility Extract corrections, deduplicate learnings, track counts
@dependencies (none — standalone)
@exports is_correction, extract_corrections, update_learnings,
    get_pending_promotions, detect_skill_candidates

Scans Claude Code transcripts for user corrections, extracts learning pairs,
deduplicates against existing learnings, and tracks recurrence counts.

Extracted from: extract-learnings.py
"""

import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# ── Correction Detection Patterns (loaded from i18n locale files) ──

def _load_correction_patterns() -> (
    tuple[list["re.Pattern[str]"], list["re.Pattern[str]"], list["re.Pattern[str]"]]
):
    """Load L1/L2 correction patterns from all active locales."""
    from concinno.i18n import patterns as i18n_patterns

    l1 = [re.compile(p, re.IGNORECASE) for p in i18n_patterns("correction_l1")]
    l2 = [re.compile(p, re.IGNORECASE) for p in i18n_patterns("correction_l2")]
    combined = l1 + l2
    return l1, l2, combined


# Lazy-compiled pattern caches
_compiled_l1: list[re.Pattern[str]] | None = None
_compiled_l2: list[re.Pattern[str]] | None = None
_compiled_all: list[re.Pattern[str]] | None = None


def _get_l1() -> list[re.Pattern[str]]:
    global _compiled_l1, _compiled_l2, _compiled_all
    if _compiled_l1 is None:
        _compiled_l1, _compiled_l2, _compiled_all = _load_correction_patterns()
    return _compiled_l1


def _get_l2() -> list[re.Pattern[str]]:
    global _compiled_l1, _compiled_l2, _compiled_all
    if _compiled_l2 is None:
        _compiled_l1, _compiled_l2, _compiled_all = _load_correction_patterns()
    return _compiled_l2


def _get_all() -> list[re.Pattern[str]]:
    global _compiled_l1, _compiled_l2, _compiled_all
    if _compiled_all is None:
        _compiled_l1, _compiled_l2, _compiled_all = _load_correction_patterns()
    return _compiled_all

# Max text length for correction detection (raised from 300 to 500)
MAX_CORRECTION_LEN = 500

def _get_skip_prefixes() -> tuple[str, ...]:
    """Load skip prefixes from all active locales."""
    from concinno.i18n import patterns as i18n_patterns

    return tuple(i18n_patterns("skip_prefixes"))


def is_correction(text: str, return_confidence: bool = False) -> bool | tuple[bool, float]:
    """Check if text matches correction patterns.

    Short messages (4-500 chars) are more likely corrections;
    long messages are new tasks.

    Args:
        text: The user message to check.
        return_confidence: If True, return (bool, confidence) tuple.

    Returns:
        bool or (bool, confidence) where confidence is 0.0-1.0.
    """
    if len(text) > MAX_CORRECTION_LEN or len(text) < 4:
        return (False, 0.0) if return_confidence else False

    # L1: explicit correction keywords → high confidence
    if any(p.search(text) for p in _get_l1()):
        return (True, 1.0) if return_confidence else True

    # L2: implicit correction patterns → medium confidence
    if any(p.search(text) for p in _get_l2()):
        return (True, 0.6) if return_confidence else True

    return (False, 0.0) if return_confidence else False


def extract_corrections(
    transcript_path: str,
    tz: Any = None,
    max_corrections: int = 10,
    max_file_size: int = 10 * 1024 * 1024,
) -> list[dict]:
    """Read transcript JSONL, find user corrections with preceding context.

    Args:
        transcript_path: Path to the transcript JSONL file.
        tz: Timezone for timestamps.
        max_corrections: Max corrections to extract per session.
        max_file_size: Skip transcripts larger than this (bytes).

    Returns:
        List of correction dicts with assistant_before, user_correction, timestamp.
    """
    path = os.path.expanduser(transcript_path)
    if not os.path.isfile(path):
        return []
    try:
        if os.path.getsize(path) > max_file_size:
            return []
    except OSError:
        return []

    corrections = []
    prev_assistant_text = ""

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = entry.get("type", "")
                message = entry.get("message", {})
                if not isinstance(message, dict):
                    continue

                content = message.get("content", "")
                text = _extract_text(content)
                if not text:
                    continue
                if any(text.startswith(p) for p in _get_skip_prefixes()):
                    continue

                if msg_type == "assistant":
                    prev_assistant_text = text[-500:]
                elif msg_type == "user":
                    if prev_assistant_text:
                        matched, confidence = is_correction(text, return_confidence=True)
                        if matched:
                            corrections.append(
                                {
                                    "assistant_before": prev_assistant_text[:300],
                                    "user_correction": text[:500],
                                    "confidence": confidence,
                                    "timestamp": (
                                        datetime.now(tz).isoformat()
                                        if tz
                                        else datetime.now().isoformat()
                                    ),
                                }
                            )
                            if len(corrections) >= max_corrections:
                                break
    except Exception:
        pass

    return corrections


def log_corrections(corrections: list[dict], session_id: str, log_path: str) -> None:
    """Append raw corrections to a JSONL queue file (audit trail)."""
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            for c in corrections:
                c["session_id"] = session_id[:8]
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
    except Exception:
        pass


def update_learnings(
    corrections: list[dict],
    learnings_path: str,
    max_stored: int = 50,
) -> None:
    """Update learnings.json with new corrections. Dedup + increment count.

    Args:
        corrections: List of correction dicts from extract_corrections.
        learnings_path: Path to learnings.json.
        max_stored: Maximum learnings to keep.
    """
    learnings = _read_learnings(learnings_path)
    existing = learnings.get("learnings", [])

    for c in corrections:
        key_text = c["user_correction"][:100]
        match = _find_similar(existing, key_text)

        if match:
            match["count"] += 1
            match["last_seen"] = c["timestamp"]
            if len(c.get("assistant_before", "")) > len(match.get("context", "")):
                match["context"] = c["assistant_before"][:300]
        else:
            combined = c["user_correction"][:200] + " " + c.get("assistant_before", "")[:200]
            existing.append(
                {
                    "id": hashlib.sha256(key_text.encode()).hexdigest()[:8],
                    "correction_text": c["user_correction"][:200],
                    "context": c.get("assistant_before", "")[:200],
                    "pattern_key": classify_pattern_key(combined),
                    "count": 1,
                    "confidence": c.get("confidence", 1.0),
                    "first_seen": c["timestamp"],
                    "last_seen": c["timestamp"],
                    "promoted": False,
                }
            )

    existing.sort(key=lambda x: x.get("last_seen", ""), reverse=True)
    learnings["learnings"] = existing[:max_stored]
    learnings["last_updated"] = corrections[-1]["timestamp"] if corrections else ""

    _write_learnings(learnings, learnings_path)


def check_staleness(
    learnings_path: str,
    stale_days: int = 90,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Find learnings not seen in >stale_days and mark them stale.

    Args:
        learnings_path: Path to learnings.json.
        stale_days: Days since last_seen to consider stale.
        now: Override current time (for testing).

    Returns:
        List of learnings newly marked as stale.
    """
    data = _read_learnings(learnings_path)
    items = data.get("learnings", [])
    if not items:
        return []

    ref = now or datetime.now(timezone.utc)
    cutoff = ref - timedelta(days=stale_days)
    newly_stale: list[dict] = []

    for item in items:
        if item.get("stale"):
            continue
        last_seen = item.get("last_seen", "")
        if not last_seen:
            continue
        try:
            ts = datetime.fromisoformat(last_seen)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                item["stale"] = True
                newly_stale.append(item)
        except (ValueError, TypeError):
            continue

    if newly_stale:
        _write_learnings(data, learnings_path)

    return newly_stale


def check_conflicts(learnings_path: str, similarity_chars: int = 30) -> list[tuple[dict, dict]]:
    """Detect contradictory learnings (similar context, different corrections).

    Two learnings conflict when their context prefixes match but their
    correction texts differ — indicating the user gave opposite guidance
    on similar situations.

    Args:
        learnings_path: Path to learnings.json.
        similarity_chars: Number of leading chars to compare for context match.

    Returns:
        List of (learning_a, learning_b) conflict pairs.
    """
    data = _read_learnings(learnings_path)
    items = data.get("learnings", [])
    if len(items) < 2:
        return []

    conflicts: list[tuple[dict, dict]] = []
    for i, a in enumerate(items):
        ctx_a = a.get("context", "")[:similarity_chars].lower().strip()
        corr_a = a.get("correction_text", "")[:similarity_chars].lower().strip()
        if not ctx_a:
            continue
        for b in items[i + 1 :]:
            ctx_b = b.get("context", "")[:similarity_chars].lower().strip()
            corr_b = b.get("correction_text", "")[:similarity_chars].lower().strip()
            if not ctx_b:
                continue
            # Same context prefix but different correction prefix = conflict
            if ctx_a == ctx_b and corr_a != corr_b:
                conflicts.append((a, b))

    return conflicts


def ftrl_weight(
    count: int,
    last_seen_iso: str,
    now: datetime | None = None,
    decay_lambda: float = 0.1,
) -> float:
    """FTRL-inspired learning weight. Higher = more urgent to promote.

    Weight = count * exp(-λ * days_since_last_seen).
    λ=0.1 gives ~7-day half-life: recent repeated corrections score highest.

    Args:
        count: How many times this correction occurred.
        last_seen_iso: ISO timestamp of last occurrence.
        decay_lambda: Exponential decay rate (default 0.1).
        now: Override current time (for testing).

    Returns:
        Float weight. Typical promotion threshold: 5.0.
    """
    ref = now or datetime.now(timezone.utc)
    try:
        ts = datetime.fromisoformat(last_seen_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return float(count)
    delta_seconds = (ref - ts).total_seconds()
    if delta_seconds < 0:
        return 0.0  # future timestamps get zero weight (clock skew / bad data)
    days = max(0.01, delta_seconds / 86400)
    return count * math.exp(-decay_lambda * days)


def get_pending_promotions(
    learnings_path: str,
    threshold: int = 3,
    use_ftrl: bool = False,
    ftrl_threshold: float = 5.0,
) -> list[dict]:
    """Get learnings ready for promotion.

    When use_ftrl=True, uses FTRL weight (recency × count) instead of
    raw count threshold. This prioritizes recent repeated corrections.

    Args:
        learnings_path: Path to learnings.json.
        threshold: Minimum count for legacy mode.
        use_ftrl: Use FTRL weighting (default True).
        ftrl_threshold: Weight threshold for FTRL mode.

    Returns:
        List of learning dicts ready for promotion.
    """
    data = _read_learnings(learnings_path)
    items = data.get("learnings", [])
    if not isinstance(items, list):
        return []

    if use_ftrl:
        return [
            item
            for item in items
            if isinstance(item, dict)
            and not item.get("promoted", False)
            and ftrl_weight(
                item.get("count", 0),
                item.get("last_seen", ""),
            ) >= ftrl_threshold
        ]

    return [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("count", 0) >= threshold
        and not item.get("promoted", False)
    ]


def suggest_rule_promotions(
    learnings_path: str,
    threshold: int = 3,
) -> list[dict]:
    """Generate concrete rule suggestions from high-frequency learnings.

    Each suggestion includes the learning text, a proposed rule snippet,
    and the target rules directory. Users review and approve before writing.

    Args:
        learnings_path: Path to learnings.json.
        threshold: Minimum recurrence count to suggest.

    Returns:
        List of suggestion dicts with keys:
        - learning_id, correction_text, count, confidence
        - suggested_rule: one-line rule text derived from correction
        - target_file: proposed .md filename slug
    """
    pending = get_pending_promotions(learnings_path, threshold)
    if not pending:
        return []

    suggestions: list[dict] = []
    for item in pending:
        correction = item.get("correction_text", "").strip()
        if not correction:
            continue

        # Derive a rule slug from first 40 chars
        slug = _slugify(correction[:40])
        if not slug:
            slug = item.get("id", "rule")

        suggestions.append(
            {
                "learning_id": item.get("id", ""),
                "correction_text": correction,
                "count": item.get("count", 0),
                "confidence": item.get("confidence", 0.0),
                "context": item.get("context", "")[:150],
                "suggested_rule": correction[:200],
                "target_file": f"learned-{slug}.md",
            }
        )

    # Sort by count descending (most recurring = most important)
    suggestions.sort(key=lambda x: x["count"], reverse=True)
    return suggestions


def mark_promoted(learnings_path: str, learning_id: str) -> bool:
    """Mark a learning as promoted (rule created).

    Args:
        learnings_path: Path to learnings.json.
        learning_id: The learning ID to mark.

    Returns:
        True if found and marked.
    """
    data = _read_learnings(learnings_path)
    items = data.get("learnings", [])
    for item in items:
        if item.get("id") == learning_id:
            item["promoted"] = True
            item["promoted_at"] = datetime.now(timezone.utc).isoformat()
            _write_learnings(data, learnings_path)
            return True
    return False


def _slugify(text: str) -> str:
    """Convert text to a safe filename slug."""
    # Keep alphanumeric, CJK, hyphens
    slug = re.sub(r"[^\w\u4e00-\u9fff-]", "-", text.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:30]


# ── Internal helpers ──────────────────────────────────────


def _extract_text(content) -> str:
    """Extract plain text from content (str or list of content blocks)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                t = p.get("text", "").strip()
                if t:
                    texts.append(t)
        return " ".join(texts).strip()
    return ""


def _read_learnings(path: str) -> dict:
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "learnings" in data:
                    return data
        except Exception:
            pass
    return {"version": "1.0", "learnings": []}


def _write_learnings(learnings: dict, path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(learnings, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(learnings, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


# ── V7 Pattern-Key Semantic Clustering ────────────────────
# Keywords → pattern_key mapping for semantic dedup.
# Same root cause with different wording → same pattern_key.
_PATTERN_KEY_NAMES = (
    "toast-notification", "cc-ccc-boundary", "image-generation",
    "book-content", "token-efficiency", "ux-branding",
    "product-design", "check-before-modify", "process-guard",
)

_pattern_keywords_cache: dict[str, list[str]] | None = None


def _get_pattern_keywords() -> dict[str, list[str]]:
    """Load semantic clustering keywords from i18n locales."""
    global _pattern_keywords_cache
    if _pattern_keywords_cache is not None:
        return _pattern_keywords_cache

    from concinno.i18n import patterns as i18n_patterns

    result: dict[str, list[str]] = {}
    for name in _PATTERN_KEY_NAMES:
        kws = i18n_patterns(f"pattern_keywords.{name}")
        if kws:
            result[name] = kws
    _pattern_keywords_cache = result
    return result


def classify_pattern_key(text: str) -> str:
    """Classify correction text into a semantic pattern_key.

    Uses keyword matching on the combined correction_text + context.
    Returns the best-matching pattern_key or 'uncategorized'.
    """
    text_lower = text.lower()
    scores: dict[str, int] = {}
    for key, keywords in _get_pattern_keywords().items():
        score = sum(1 for kw in keywords if kw.lower() in text_lower)
        if score > 0:
            scores[key] = score
    if not scores:
        return "uncategorized"
    return max(scores, key=scores.get)  # type: ignore[arg-type]


def _find_similar(existing: list, key_text: str) -> Optional[dict]:
    """Dedup: first 50 chars match OR same pattern_key = same learning."""
    key_prefix = key_text[:50].lower()
    for item in existing:
        if item.get("correction_text", "")[:50].lower() == key_prefix:
            return item
    # V7: pattern_key semantic matching — find existing with same pattern
    new_pattern = classify_pattern_key(key_text)
    if new_pattern != "uncategorized":
        for item in existing:
            if item.get("pattern_key") == new_pattern and not item.get("promoted"):
                return item
    return None


def detect_skill_candidates(
    learnings_path: str,
    drive_state_path: str,
    pattern_threshold: int = 3,
) -> list[dict]:
    """Detect repeating patterns that should become skill candidates.

    Groups learnings by pattern_key, sums counts per group. If a group's
    total count >= threshold and isn't already a skill candidate, adds it.

    Args:
        learnings_path: Path to learnings.json.
        drive_state_path: Path to drive-state.json.
        pattern_threshold: Min total count per pattern_key to trigger.

    Returns:
        List of newly added skill candidate dicts.
    """
    data = _read_learnings(learnings_path)
    items = data.get("learnings", [])
    if not items:
        return []

    # Group by pattern_key, sum counts
    pattern_counts: dict[str, int] = {}
    pattern_examples: dict[str, list[str]] = {}
    for item in items:
        pk = item.get("pattern_key", "uncategorized")
        if pk == "uncategorized":
            continue
        pattern_counts[pk] = pattern_counts.get(pk, 0) + item.get("count", 1)
        examples = pattern_examples.setdefault(pk, [])
        if len(examples) < 3:
            examples.append(item.get("correction_text", "")[:80])

    # Load drive-state
    drive_state = {}
    if os.path.isfile(drive_state_path):
        try:
            with open(drive_state_path, "r", encoding="utf-8") as f:
                drive_state = json.load(f)
        except Exception:
            drive_state = {}

    tracker = drive_state.setdefault("learning_tracker", {})
    existing_candidates = tracker.setdefault("skill_candidates", [])
    existing_patterns = {c.get("pattern_key") for c in existing_candidates}

    newly_added: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for pk, total_count in pattern_counts.items():
        if total_count >= pattern_threshold and pk not in existing_patterns:
            candidate = {
                "pattern_key": pk,
                "total_count": total_count,
                "examples": pattern_examples.get(pk, []),
                "status": "detected",
                "detected_at": now_iso,
            }
            existing_candidates.append(candidate)
            newly_added.append(candidate)

    if newly_added:
        try:
            tmp = drive_state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(drive_state, f, indent=2, ensure_ascii=False)
            os.replace(tmp, drive_state_path)
        except Exception:
            try:
                with open(drive_state_path, "w", encoding="utf-8") as f:
                    json.dump(drive_state, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

    return newly_added


# ── Learnings Query API (consolidated from hooks) ─────────


def get_recent_corrections(
    learnings_path: str,
    max_items: int = 3,
    min_count: int = 2,
) -> list[str]:
    """Top unpromoted corrections sorted by count desc.

    Returns formatted lines like ``[2x|pattern-key] correction text``.

    Args:
        learnings_path: Path to learnings.json.
        max_items: Max items to return.
        min_count: Minimum recurrence count to include.
    """
    data = _read_learnings(learnings_path)
    items = data.get("learnings", [])
    repeated = [
        it for it in items
        if not it.get("promoted") and it.get("count", 0) >= min_count
    ]
    repeated.sort(key=lambda x: x.get("count", 0), reverse=True)
    lines: list[str] = []
    for it in repeated[:max_items]:
        txt = it.get("correction_text", "")[:60]
        cnt = it.get("count", 0)
        pk = it.get("pattern_key", "")
        lines.append(f"[{cnt}x|{pk}] {txt}")
    return lines


def get_preference_summary(learnings_path: str) -> str:
    """TADS-2 L1: Generate user preference summary from learnings.

    Classifies corrections into three categories:
    - 做前必問 (ask before doing)
    - 直接做不問 (just do it)
    - 反模式 (anti-patterns to avoid)

    Returns formatted string ~100 tokens, or "" if nothing useful.
    """
    data = _read_learnings(learnings_path)
    items = data.get("learnings", [])
    if not items:
        return ""

    ask_first: list[str] = []
    just_do: list[str] = []
    anti: list[str] = []

    ask_keywords = (
        "刪除", "刪掉", "部署", "deploy",
        "改設定", "改架構", "push", "reset",
    )
    do_keywords = (
        "不要問", "直接做", "別問",
        "不用問", "廢話", "stop asking",
    )
    anti_keywords = (
        "不要", "禁止", "別",
        "don't", "stop", "避免",
    )

    for it in items:
        txt = it.get("correction_text", "")
        count = it.get("count", 0)
        if count < 2:
            continue
        lower = txt.lower()

        if any(k in lower for k in do_keywords):
            just_do.append(txt[:40])
        elif any(k in lower for k in ask_keywords):
            ask_first.append(txt[:40])
        elif any(k in lower for k in anti_keywords):
            anti.append(txt[:40])

    if not ask_first and not just_do and not anti:
        return ""

    lines = ["\U0001f3af 用戶偏好摘要（自動提煉）："]
    if ask_first:
        lines.append(f"  做前必問：{'; '.join(ask_first[:3])}")
    if just_do:
        lines.append(f"  直接做：{'; '.join(just_do[:3])}")
    if anti:
        lines.append(f"  反模式：{'; '.join(anti[:3])}")
    return "\n".join(lines)


def scan_pending_distill(
    learnings_path: str,
    pending_path: str,
    *,
    threshold: int = 3,
    tz: Any = None,
) -> int:
    """Scan learnings for count>=threshold unpromoted → pending_distill.json.

    Args:
        learnings_path: Path to learnings.json.
        pending_path: Path to pending_distill.json.
        threshold: Min count to consider for distillation.
        tz: Timezone for timestamps.

    Returns:
        Number of newly added pending items.
    """
    data = _read_learnings(learnings_path)
    learnings = data.get("learnings", [])
    if not learnings:
        return 0

    candidates = []
    for item in learnings:
        if item.get("count", 0) >= threshold and not item.get("promoted", False):
            candidates.append({
                "id": item.get("id", ""),
                "correction_text": item.get("correction_text", "")[:100],
                "count": item.get("count", 0),
                "first_seen": item.get("first_seen", ""),
                "last_seen": item.get("last_seen", ""),
            })

    if not candidates:
        return 0

    pending_data: dict = {"pending": [], "last_scan": None, "version": "1.0"}
    if os.path.isfile(pending_path):
        try:
            with open(pending_path, "r", encoding="utf-8") as f:
                pending_data = json.load(f)
        except Exception:
            pass

    existing_ids = {p.get("id") for p in pending_data.get("pending", [])}
    added = 0
    for c in candidates:
        if c["id"] not in existing_ids:
            pending_data.setdefault("pending", []).append(c)
            existing_ids.add(c["id"])
            added += 1

    if added == 0:
        return 0

    now = datetime.now(tz) if tz else datetime.now(timezone.utc)
    pending_data["last_scan"] = now.isoformat()

    try:
        os.makedirs(os.path.dirname(pending_path), exist_ok=True)
        tmp = pending_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pending_data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, pending_path)
    except Exception:
        try:
            with open(pending_path, "w", encoding="utf-8") as f:
                json.dump(pending_data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    return added


def calibrate_preferences(
    learnings_path: str,
    pref_model_path: str,
    *,
    tz: Any = None,
) -> bool:
    """TADS-5 L3: Analyze correction causes to calibrate clarity thresholds.

    Checks today's corrections. If cause = "didn't ask" → lower clarity
    threshold; if "asked too much" → raise threshold. Writes preference_model.json.

    Args:
        learnings_path: Path to learnings.json.
        pref_model_path: Path to preference_model.json.
        tz: Timezone for date comparison.

    Returns:
        True if preferences were updated.
    """
    data = _read_learnings(learnings_path)
    learnings_list = data.get("learnings", [])
    if not learnings_list:
        return False

    now = datetime.now(tz) if tz else datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    today_items = [
        it for it in learnings_list
        if it.get("last_seen", "").startswith(today)
    ]
    if not today_items:
        return False

    pref: dict = {
        "should_have_asked_patterns": [],
        "asked_too_much_patterns": [],
        "last_calibrated": "",
    }
    if os.path.isfile(pref_model_path):
        try:
            with open(pref_model_path, "r", encoding="utf-8") as f:
                pref = json.load(f)
        except Exception:
            pass

    changed = False
    ask_kw = ("沒問", "沒確認", "應該問", "didn't ask")
    too_much_kw = ("問太多", "不要問", "直接做", "stop ask")

    for it in today_items:
        txt = it.get("correction_text", "").lower()
        pk = it.get("pattern_key", "")
        if not pk:
            continue

        should_ask = pref.get("should_have_asked_patterns", [])
        too_much = pref.get("asked_too_much_patterns", [])

        if any(k in txt for k in ask_kw):
            if pk not in should_ask:
                should_ask.append(pk)
                changed = True
        elif any(k in txt for k in too_much_kw):
            if pk not in too_much:
                too_much.append(pk)
                changed = True

        pref["should_have_asked_patterns"] = should_ask[-20:]
        pref["asked_too_much_patterns"] = too_much[-20:]

    if changed:
        pref["last_calibrated"] = today
        try:
            os.makedirs(os.path.dirname(pref_model_path), exist_ok=True)
            tmp = pref_model_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(pref, f, indent=2, ensure_ascii=False)
            os.replace(tmp, pref_model_path)
        except Exception:
            try:
                with open(pref_model_path, "w", encoding="utf-8") as f:
                    json.dump(pref, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

    return changed


def auto_promote(
    learnings_path: str,
    rules_dir: str,
    *,
    threshold: int = 5,
    max_per_run: int = 3,
) -> list[dict]:
    """Auto-promote high-frequency learnings to rule files.

    Scans learnings with count >= threshold, writes rule .md files,
    and marks them as promoted. Designed to be called from on-stop hook.

    Args:
        learnings_path: Path to learnings.json.
        rules_dir: Directory to write rule files (e.g. .claude/rules/).
        threshold: Minimum count to auto-promote.
        max_per_run: Max promotions per invocation (prevent spam).

    Returns:
        List of promoted suggestion dicts.
    """
    suggestions = suggest_rule_promotions(learnings_path, threshold)
    if not suggestions:
        return []

    promoted: list[dict] = []
    for s in suggestions[:max_per_run]:
        target = s.get("target_file", "")
        if not target:
            continue
        rule_path = os.path.join(rules_dir, target)

        # Skip if rule file already exists
        if os.path.isfile(rule_path):
            mark_promoted(learnings_path, s["learning_id"])
            promoted.append(s)
            continue

        # Write rule file
        correction = s.get("correction_text", "")
        context = s.get("context", "")
        count = s.get("count", 0)
        content = (
            f"# Learned: {correction[:80]}\n\n"
            f"> Auto-promoted from corrections (count={count})\n\n"
            f"{correction}\n"
        )
        if context:
            content += f"\n## Context\n\n{context[:200]}\n"

        try:
            os.makedirs(rules_dir, exist_ok=True)
            tmp = rule_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, rule_path)
        except Exception:
            continue

        mark_promoted(learnings_path, s["learning_id"])
        promoted.append(s)

    return promoted


def on_stop(hook_data: dict) -> None:
    """Hook entry: Stop event — extract corrections, log, update learnings.

    Called by concinno.hooks.on_stop pipeline. Fail-open: any error is swallowed.
    """
    session_id = os.environ.get("CC_SESSION_ID", "")
    if not session_id:
        return

    # Locate transcript
    transcript_path = _find_transcript(session_id)
    if not transcript_path:
        return

    tz = None
    try:
        from concinno.hooks.io_utils import get_local_tz
        tz = get_local_tz()
    except Exception:
        pass

    corrections = extract_corrections(transcript_path, tz=tz)
    if not corrections:
        return

    # Paths
    home = os.path.expanduser("~")
    cognitive_dir = os.path.join(home, ".claude", "cognitive")
    learnings_path = os.path.join(cognitive_dir, "learnings.json")
    log_path = os.path.join(cognitive_dir, "corrections_queue.jsonl")

    log_corrections(corrections, session_id, log_path)
    update_learnings(corrections, learnings_path)


def on_post_tool(hook_data: dict) -> Optional[str]:
    """Hook entry: PostToolUse — detect correction in latest user message.

    Returns additionalContext string if a correction is detected, else None.
    Currently a no-op placeholder; the real correction extraction happens
    in on_stop when the full transcript is available.
    """
    return None


def _find_transcript(session_id: str) -> str:
    """Delegate to unified transcript lookup in core.path_utils."""
    from concinno.core.path_utils import find_transcript
    return find_transcript(session_id)


__all__ = [
    # External callers confirmed (CC hooks, on_stop.py, on-failure.py)
    "extract_corrections",
    "suggest_rule_promotions",
    "mark_promoted",
    "auto_promote",
    "get_recent_corrections",
    "get_preference_summary",
    "scan_pending_distill",
    "calibrate_preferences",
    "on_stop",
    "on_post_tool",
    # Internal-only (not exported, used within module):
    # is_correction, log_corrections, classify_pattern_key,
    # update_learnings, get_pending_promotions, check_staleness,
    # check_conflicts, detect_skill_candidates
]
