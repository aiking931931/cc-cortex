"""Sentinel-based answer extraction for agent benchmarks.

@module concinno.agent.sentinel_parser
@responsibility Extract ``<value>`` from ``SENTINEL: <value>`` patterns
    in free-form agent output, robust to the model quoting the
    sentinel rule in its thinking (earlier false matches).
@dependencies stdlib only (re)
@exports extract_sentinel_answer
"""

from __future__ import annotations

import re

_TRAILING_BACKTICKS = "` "


def extract_sentinel_answer(
    text: str,
    sentinel: str = "FINAL ANSWER:",
    take_last: bool = True,
    max_len: int = 200,
) -> str | None:
    """Extract ``<value>`` from a ``<sentinel> <value>`` pattern.

    Agent outputs often quote the sentinel rule in thinking
    (e.g. ``preceded by \\`FINAL ANSWER:\\```); the quoted occurrence
    should not be treated as the answer. ``take_last=True`` returns
    the final match, which empirically corresponds to the real
    answer emission at the end of the generation.

    :param text: full agent output, may contain multiple sentinel
        occurrences (rule quote + real answer).
    :param sentinel: the sentinel literal, case-insensitive matched.
    :param take_last: take last match (True) or first (False).
    :param max_len: truncate captured value at this many chars,
        keeping the leading sentence boundary when possible.
    :returns: raw captured string with trailing backticks / spaces
        stripped, or ``None`` if no sentinel found.
    """
    pattern = re.escape(sentinel) + r"\s*(.+?)(?:\n|$)"
    matches = re.findall(pattern, text, re.I)
    if not matches:
        return None
    idx = -1 if take_last else 0
    ans = matches[idx].strip().strip(_TRAILING_BACKTICKS)
    if len(ans) > max_len:
        ans = ans[:max_len].rsplit(".", 1)[0] or ans[:max_len]
    return ans or None
