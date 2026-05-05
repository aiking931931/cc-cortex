"""concinno.guards.magic_number_guard — suggest extracting recurring
literal integers and flagging magic timeouts.

@module magic_number_guard
@responsibility On PreToolUse Write / Edit, parse Python via ``ast``
    (fallback regex for TS/JS). Flag two cases:
      (a) the same literal integer >=5 appears >=3 times — suggest
          extracting a named constant.
      (b) magic timeout / sleep values like ``sleep(60)``,
          ``timeout=30``, ``setInterval(x, 5000)`` — flag even when
          they only appear once.
    Signal-only. Ignores 0, 1, -1, 2 (too common to bother with).
@dependencies concinno.guards.base (stdlib ast/re only)
@exports MagicNumberGuard
"""

from __future__ import annotations

import ast
import re
from collections import Counter

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# Thresholds.
_MIN_VALUE = 5  # ignore constants <5
_MIN_COUNT = 3  # must appear at least this many times

_IGNORE_LITERALS = frozenset({-1, 0, 1, 2})

# File extensions we attempt to parse.
_PY_EXT = (".py",)
_JS_TS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

# Regex fallbacks for TS/JS — crude but enough to spot obvious magic.
_JS_INT_LITERAL = re.compile(r"(?<![\w.])(-?\d+)(?![\w.])")

# Timeout / sleep pattern — detects common blocking calls with literal args.
_TIMEOUT_PATTERNS = (
    # Python: time.sleep(60), sleep(60), await asyncio.sleep(5)
    re.compile(r"\b(?:time\.sleep|asyncio\.sleep|sleep)\s*\(\s*(\d+(?:\.\d+)?)\s*\)"),
    # Python: timeout=30
    re.compile(r"\btimeout\s*=\s*(\d+(?:\.\d+)?)"),
    # JS: setTimeout(fn, 5000), setInterval(fn, 5000)
    re.compile(r"\bset(?:Timeout|Interval)\s*\(\s*[^,]+,\s*(\d+)\s*\)"),
    # Requests: requests.get(..., timeout=30)
    re.compile(r"\bread_?timeout\s*=\s*(\d+(?:\.\d+)?)"),
)


def _collect_python_ints(source: str) -> list[int]:
    """Return all integer constants in *source* using AST."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    ints: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(
            node.value, bool,
        ):
            ints.append(node.value)
    return ints


def _collect_js_ints(source: str) -> list[int]:
    """Regex fallback — returns integer literals from *source*."""
    ints: list[int] = []
    for m in _JS_INT_LITERAL.finditer(source):
        try:
            ints.append(int(m.group(1)))
        except ValueError:
            continue
    return ints


def find_repeated_literals(
    ints: list[int],
    *,
    min_value: int = _MIN_VALUE,
    min_count: int = _MIN_COUNT,
) -> list[tuple[int, int]]:
    """Return ``(value, count)`` for literals meeting both thresholds.

    Sorted by count descending, then value descending — stable.
    """
    counter: Counter[int] = Counter()
    for v in ints:
        if v in _IGNORE_LITERALS:
            continue
        if abs(v) < min_value:
            continue
        counter[v] += 1
    repeated = [(v, c) for v, c in counter.items() if c >= min_count]
    repeated.sort(key=lambda x: (-x[1], -x[0]))
    return repeated


def find_magic_timeouts(source: str) -> list[tuple[str, str]]:
    """Return ``(pattern_desc, literal_text)`` for timeout magic numbers.

    Deduplicates via a (desc, literal) set so the same match isn't
    reported twice if the source has duplicates.
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    descs = (
        "sleep() call",
        "timeout= kwarg",
        "setTimeout/setInterval",
        "read_timeout= kwarg",
    )
    for desc, rx in zip(descs, _TIMEOUT_PATTERNS):
        for m in rx.finditer(source):
            val = m.group(1)
            key = (desc, val)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


class MagicNumberGuard(BaseGuard):
    """Suggest extracting recurring magic numbers and flag magic timeouts.

    Signal-only. Uses AST for Python, regex fallback for TS/JS.
    """

    name = "magic_number"
    category = GuardCategory.QUALITY
    feature_name = "magic_number"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name not in ("Write", "Edit"):
            return None
        path = ctx.tool_input.get("file_path", "") or ""
        low = path.lower()
        content = (
            ctx.tool_input.get("content", "")
            or ctx.tool_input.get("new_string", "")
            or ""
        )
        if not content:
            return None

        if low.endswith(_PY_EXT):
            ints = _collect_python_ints(content)
        elif low.endswith(_JS_TS_EXT):
            ints = _collect_js_ints(content)
        else:
            return None

        repeated = find_repeated_literals(ints)
        timeouts = find_magic_timeouts(content)

        if not repeated and not timeouts:
            return None

        lines: list[str] = []
        if repeated:
            lines.append("Repeated literals (extract named constant):")
            for v, c in repeated[:5]:
                lines.append(f"  - `{v}` appears {c}× — e.g. `MY_CONSTANT = {v}`")
        if timeouts:
            lines.append("Magic timeout / sleep values:")
            for desc, val in timeouts[:5]:
                lines.append(f"  - {desc}: `{val}` — extract to `TIMEOUT_SECS = {val}`")
        body = "\n".join(lines)
        msg = (
            f"[magic-number] hardcoded constants in `{path}`:\n"
            f"{body}\n"
            "  Signal only — write proceeds."
        )
        return GuardResult.allow_advisory(context=msg)
