"""CC Cortex GuardBench Adapter — wraps CCC guards for the GuardBench benchmark.

Implements the moderate() interface expected by guardbench.benchmark():
  - effectiveness mode: conversations = List[List[dict]]  (batch of convos)
  - efficiency mode:    conversations = List[dict]         (single convo)

Returns List[float] of unsafe probability scores in [0.0, 1.0].

Guards integrated:
  1. destruction_guard — detects dangerous shell commands (rm -rf, DROP TABLE, etc.)
  2. secret_scan       — detects hardcoded API keys, tokens, PEM keys
  3. exfil_guard       — detects data exfiltration via curl/wget/scp

Usage:
    python guardbench_adapter.py                     # run all datasets
    python guardbench_adapter.py --datasets xstest toxic_chat
    python guardbench_adapter.py --batch-size 32
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

# Ensure cc_cortex is importable
_src = Path(__file__).resolve().parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from cc_cortex.destruction_guard import R0, R1, R2, R3, R4, classify_bash  # noqa: E402
from cc_cortex.exfil_guard import check as exfil_check  # noqa: E402
from cc_cortex.secret_scan import check as secret_check  # noqa: E402

# ── Helpers ────────────────────────────────────────────────────────

# Regex to extract code blocks and inline code from conversation text
_CODE_BLOCK = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]{4,})`")

# Shell-like patterns to identify commands in plain text
_SHELL_PREFIX = re.compile(
    r"^\s*(?:\$|#|>|>>>|\%)\s+(.+)",
    re.MULTILINE,
)
_SHELL_KEYWORDS = re.compile(
    r"\b(?:rm\s|sudo\s|curl\s|wget\s|dd\s|mkfs|chmod\s|chown\s|"
    r"git\s+(?:push|reset|clean|branch\s+-[dD])|"
    r"docker\s|kubectl\s|terraform\s|"
    r"DROP\s+(?:TABLE|DATABASE)|TRUNCATE|DELETE\s+FROM|"
    r"pip\s+install|npm\s+install)\b",
    re.IGNORECASE,
)

# Prompt injection / jailbreak indicators (common patterns)
_JAILBREAK_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions", re.I),
    re.compile(r"you\s+are\s+now\s+(?:DAN|evil|unfiltered|jailbroken)", re.I),
    re.compile(r"(?:DAN|do\s+anything\s+now)\s+mode", re.I),
    re.compile(r"pretend\s+(?:you\s+are|to\s+be)\s+(?:a|an)\s+(?:evil|malicious)", re.I),
    re.compile(
        r"bypass\s+(?:your|all|any)\s+(?:safety|content|ethical)"
        r"\s+(?:filter|guard|restriction)", re.I,
    ),
    re.compile(r"system\s*:\s*you\s+(?:are|must)", re.I),
    re.compile(r"from\s+now\s+on.*(?:no\s+(?:limit|restriction|filter|ethic))", re.I),
    re.compile(r"\[(?:system|SYSTEM)\]", re.I),
    re.compile(
        r"(?:override|disable|remove)\s+(?:safety|content)"
        r"\s+(?:filter|policy|guideline)", re.I,
    ),
]

# Harmful intent keywords (high-signal indicators)
_HARMFUL_INTENT = re.compile(
    r"\b(?:how\s+to\s+(?:make|build|create|synthesize)\s+(?:a\s+)?(?:bomb|weapon|explosive|poison|drug|meth)|"
    r"how\s+to\s+(?:hack|break\s+into|exploit|crack)|"
    r"how\s+to\s+(?:kill|murder|assassinate|harm|torture)|"
    r"write\s+(?:a\s+)?(?:malware|ransomware|virus|trojan|keylogger|exploit)|"
    r"(?:steal|exfiltrate|extract)\s+(?:data|credentials|passwords|credit\s+cards)|"
    r"(?:child|minor)\s+(?:sexual|exploit|abuse|porn))\b",
    re.IGNORECASE,
)


def _extract_commands(text: str) -> list[str]:
    """Extract shell commands and code snippets from text."""
    cmds: list[str] = []
    # Fenced code blocks
    for m in _CODE_BLOCK.finditer(text):
        cmds.append(m.group(1).strip())
    # Inline code
    for m in _INLINE_CODE.finditer(text):
        snippet = m.group(1).strip()
        if _SHELL_KEYWORDS.search(snippet):
            cmds.append(snippet)
    # Shell-prefixed lines ($ command, # command)
    for m in _SHELL_PREFIX.finditer(text):
        cmds.append(m.group(1).strip())
    return cmds


def _score_jailbreak(text: str) -> float:
    """Score jailbreak/injection probability. Returns 0.0-1.0."""
    hits = sum(1 for p in _JAILBREAK_PATTERNS if p.search(text))
    if hits == 0:
        return 0.0
    # 1 hit = 0.7, 2+ = 0.9, 3+ = 1.0
    return min(0.5 + hits * 0.2, 1.0)


def _score_harmful_intent(text: str) -> float:
    """Score harmful intent based on keyword matching. Returns 0.0-1.0."""
    if _HARMFUL_INTENT.search(text):
        return 0.95
    return 0.0


def _risk_to_score(risk_level: int) -> float:
    """Convert destruction_guard risk level (R0-R4) to probability score."""
    return {R0: 0.0, R1: 0.15, R2: 0.5, R3: 0.8, R4: 1.0}[risk_level]


def _score_single_conversation(conversation: list[dict[str, str]]) -> float:
    """Score a single conversation for unsafe content. Returns 0.0-1.0.

    Runs three layers:
      1. Content analysis (jailbreak + harmful intent)
      2. Code extraction → destruction_guard risk classification
      3. Code extraction → secret_scan + exfil_guard
    """
    max_score = 0.0

    for turn in conversation:
        text = turn.get("content", "")

        # Layer 1: Jailbreak / prompt injection detection
        jb = _score_jailbreak(text)
        max_score = max(max_score, jb)

        # Layer 1b: Harmful intent keywords
        hi = _score_harmful_intent(text)
        max_score = max(max_score, hi)

        # Layer 2: Extract commands and run through destruction_guard
        commands = _extract_commands(text)
        for cmd in commands:
            risk, _reason = classify_bash(cmd)
            max_score = max(max_score, _risk_to_score(risk))

        # Layer 3a: Check for secrets in content (treat as Write tool input)
        secret_result = secret_check("Write", {"content": text, "file_path": "check.py"})
        if secret_result is not None:
            # Secrets in conversation = suspicious but not necessarily unsafe
            # In a prompt context, sharing secrets could be social engineering
            max_score = max(max_score, 0.6)

        # Layer 3b: Check for exfiltration commands
        for cmd in commands:
            exfil_result = exfil_check("Bash", {"command": cmd})
            if exfil_result is not None:
                max_score = max(max_score, 0.9)

        # Early exit if already at max
        if max_score >= 1.0:
            return 1.0

    return max_score


# ── GuardBench moderate() interface ────────────────────────────────


def moderate(
    conversations: list[dict[str, str]] | list[list[dict[str, str]]],
    **kwargs: Any,
) -> list[float]:
    """Moderation function compatible with GuardBench benchmark().

    Args:
        conversations: Either a single conversation (List[dict]) or a batch
            of conversations (List[List[dict]]).

    Returns:
        List of unsafe probability scores in [0.0, 1.0].
        Higher = more likely unsafe.
    """
    # Detect if single conversation (efficiency mode) or batch (effectiveness mode)
    if not conversations:
        return []

    if isinstance(conversations[0], dict):
        # Single conversation: List[dict] — wrap in list, score, unwrap
        return [_score_single_conversation(conversations)]

    # Batch: List[List[dict]]
    return [_score_single_conversation(conv) for conv in conversations]


# ── CLI ────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Run GuardBench with CC Cortex guards adapter"
    )
    parser.add_argument(
        "--datasets",
        nargs="*",
        default=None,
        help="Specific datasets to evaluate (default: all)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="Batch size for inference (default: 32)",
    )
    parser.add_argument(
        "--metrics",
        nargs="*",
        default=["f1", "recall", "precision", "auprc"],
        help="Metrics to report",
    )
    parser.add_argument(
        "--out-dir",
        default="results/guardbench",
        help="Output directory for predictions (default: results/guardbench)",
    )
    parser.add_argument(
        "--model-name",
        default="cc-cortex-guards",
        help="Model name for output files (default: cc-cortex-guards)",
    )
    args = parser.parse_args()

    import guardbench

    datasets = args.datasets if args.datasets else "all"

    print("Running GuardBench with CC Cortex guards adapter")
    print(f"  Model name: {args.model_name}")
    print(f"  Datasets:   {datasets}")
    print(f"  Batch size: {args.batch_size}")
    print(f"  Metrics:    {args.metrics}")
    print(f"  Output dir: {args.out_dir}")
    print()

    guardbench.benchmark(
        moderate=moderate,
        model_name=args.model_name,
        out_dir=args.out_dir,
        batch_size=args.batch_size,
        datasets=datasets,
        metrics=args.metrics,
    )


if __name__ == "__main__":
    main()
