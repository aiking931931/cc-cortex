"""concinno.guards.deterministic_repro_guard — flag non-deterministic APIs
in test / benchmark files so multi-seed ablations stay reproducible.

@module deterministic_repro_guard
@responsibility Detect ``random.random()`` / ``random.choice()`` calls,
    ``time.time()`` passed as a seed, and live HTTP calls
    (``requests.get/post`` / ``httpx.*``) inside ``tests/``,
    ``benchmarks/`` or files named ``test_*.py`` / ``*_test.py`` /
    ``benchmark_*.py``. Advisory only — never blocks.
@dependencies concinno.guards.base (stdlib re/os only)
@exports DeterministicReproGuard
"""

from __future__ import annotations

import os
import re

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# Path hints — anything inside these dirs or matching these basename
# patterns is a candidate for the guard.
_TEST_DIR_MARKERS = ("tests/", "benchmarks/", "benchmark/", "ablations/")
_TEST_BASENAME = re.compile(r"^(test_.*|.*_test|benchmark_.*|.*_benchmark)\.py$", re.IGNORECASE)

# Patterns ordered by specificity. All are signal-only (advisory).
_RANDOM_CALL = re.compile(
    r"\brandom\.(random|choice|randint|randrange|sample|shuffle|uniform|gauss)\s*\(",
)
# `random.seed(...)` counts as an explicit seed set.
_SEED_SET = re.compile(
    r"\b(?:random\.seed|np\.random\.seed|numpy\.random\.seed|torch\.manual_seed"
    r"|tf\.random\.set_seed|tensorflow\.random\.set_seed)\s*\(",
)
# `random.seed(time.time())` — common anti-pattern.
_TIME_AS_SEED = re.compile(
    r"\b(?:random\.seed|np\.random\.seed|numpy\.random\.seed|torch\.manual_seed)"
    r"\s*\(\s*(?:time\.time|time\.perf_counter|time\.monotonic|datetime\.[a-zA-Z_]+)\s*\(",
)
# Live HTTP calls — `requests.get/post/put/...` or `httpx.get/post/...`.
_LIVE_HTTP = re.compile(
    r"\b(?:requests|httpx|urllib\.request)\.(?:get|post|put|patch|delete|request|head)\s*\(",
)
# Patched / mocked HTTP — suppress if we see `@patch` or `mock` referencing
# the same module on the same line.
_MOCK_HINT = re.compile(r"\b(?:mock|Mock|MagicMock|patch|responses|vcr|respx)\b")

# asset-like basename scope (only fire on Python source)
_PYTHON_EXT = (".py",)


def _is_test_or_benchmark_path(path: str) -> bool:
    """True when the path looks like a test / benchmark file."""
    if not path:
        return False
    normalized = path.replace("\\", "/")
    if not normalized.endswith(_PYTHON_EXT):
        return False
    # Directory marker anywhere in the path.
    for marker in _TEST_DIR_MARKERS:
        if marker in normalized:
            return True
    basename = os.path.basename(normalized)
    return bool(_TEST_BASENAME.match(basename))


def _strip_comments_and_strings(content: str) -> str:
    """Rough scrub: kill # comments and triple-quoted blocks.

    Not a real lexer — just enough to avoid flagging ``# random.random()``
    in a docstring. Regex-only so we stay zero-dep.
    """
    # Remove triple-quoted strings (greedy across lines).
    content = re.sub(r'"""[\s\S]*?"""', "", content)
    content = re.sub(r"'''[\s\S]*?'''", "", content)
    # Strip line comments (# to end-of-line).
    content = re.sub(r"(?m)#.*$", "", content)
    return content


def _extract_snippet(content: str, match: re.Match[str], context: int = 20) -> str:
    """Return a ≤60-char snippet around *match* for diagnostic context."""
    start = max(0, match.start() - context)
    end = min(len(content), match.end() + context)
    snippet = content[start:end].replace("\n", " ").strip()
    if len(snippet) > 80:
        snippet = snippet[:77] + "..."
    return snippet


def scan_content(content: str) -> list[str]:
    """Return human-readable findings for *content*. Empty = all good.

    Pure function — no I/O, deterministic, testable.
    """
    if not content:
        return []
    scrubbed = _strip_comments_and_strings(content)
    findings: list[str] = []

    # A. time.time() used as seed — most serious.
    for m in _TIME_AS_SEED.finditer(scrubbed):
        snippet = _extract_snippet(scrubbed, m)
        findings.append(
            f"seed derived from time.time() — reproducibility lost near `{snippet}`",
        )

    # B. random.*() used but no seed_set call anywhere in the module.
    random_calls = list(_RANDOM_CALL.finditer(scrubbed))
    has_seed = bool(_SEED_SET.search(scrubbed))
    if random_calls and not has_seed:
        m = random_calls[0]
        findings.append(
            f"random.{m.group(1)}(...) used but no random.seed/np.random.seed/torch.manual_seed — "
            f"seed once in fixture for reproducibility",
        )

    # C. Live HTTP in test file — mocks preferred.
    for m in _LIVE_HTTP.finditer(scrubbed):
        # Check a ±100 char window around the match for a mock hint.
        start = max(0, m.start() - 200)
        end = min(len(scrubbed), m.end() + 100)
        window = scrubbed[start:end]
        if _MOCK_HINT.search(window):
            continue
        findings.append(
            f"live HTTP call `{m.group(0)}...` in test — consider mock/responses/respx "
            f"(flaky on network outage, test cost ≠ zero)",
        )

    # Deduplicate while preserving order (first-seen wins).
    seen: set[str] = set()
    out: list[str] = []
    for f in findings:
        if f in seen:
            continue
        seen.add(f)
        out.append(f)
    return out


class DeterministicReproGuard(BaseGuard):
    """Flag non-deterministic APIs in test / benchmark files.

    Signal-only: always ALLOW, never DENY. Runs on PreToolUse Write /
    Edit / Bash (Bash for ``python -c`` one-liners). Scope limited to
    paths matching :func:`_is_test_or_benchmark_path`.
    """

    name = "deterministic_repro"
    category = GuardCategory.QUALITY
    feature_name = "deterministic_repro"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name not in ("Write", "Edit", "Bash"):
            return None

        content = ""
        path = ""

        if ctx.tool_name in ("Write", "Edit"):
            path = ctx.tool_input.get("file_path", "")
            if not _is_test_or_benchmark_path(path):
                return None
            content = (
                ctx.tool_input.get("content", "")
                or ctx.tool_input.get("new_string", "")
            )
        else:
            # Bash — look for inline python -c patterns; don't fire on
            # arbitrary shell commands.
            command = ctx.tool_input.get("command", "")
            if "python" not in command and "py -" not in command:
                return None
            # Extract after -c " ... "
            m = re.search(r"python[0-9.]*\s+-c\s+['\"]([\s\S]+?)['\"]", command)
            if not m:
                return None
            content = m.group(1)

        findings = scan_content(content)
        if not findings:
            return None

        where = f"{path}: " if path else "python -c: "
        bullet = "\n".join(f"  - {where}{f}" for f in findings)
        msg = (
            "[deterministic-repro] reproducibility signals:\n"
            f"{bullet}\n"
            "  Fix: set one seed at top of module or pytest fixture, "
            "mock network calls."
        )
        return GuardResult.allow_advisory(context=msg)
