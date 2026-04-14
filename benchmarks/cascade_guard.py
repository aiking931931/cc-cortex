"""Cascade Guard — two-layer moderation: CCC regex first, then HF safety model.

Layer 1 (fast): CC Cortex regex guards (destruction, secret, exfil, jailbreak).
  If regex catches it → score=1.0 immediately, skip model.
Layer 2 (slow): Small HuggingFace safety classifier as fallback.
  Default: protectai/deberta-v3-base-prompt-injection-v2

Implements the same moderate() interface as guardbench_adapter.py.

Usage:
    python cascade_guard.py                        # regex-only (no model)
    python cascade_guard.py --use-model            # regex + HF model
    python cascade_guard.py --model-id <hf_model>  # custom model
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

from cc_cortex.destruction_guard import (  # noqa: E402
    R0,
    R1,
    R2,
    R3,
    R4,
    classify_bash,
)
from cc_cortex.exfil_guard import check as exfil_check  # noqa: E402
from cc_cortex.secret_scan import check as secret_check  # noqa: E402

# ── Layer 1: Regex patterns (reuse from guardbench_adapter) ────────

_CODE_BLOCK = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]{4,})`")
_SHELL_PREFIX = re.compile(
    r"^\s*(?:\$|#|>|>>>|\%)\s+(.+)", re.MULTILINE,
)
_SHELL_KEYWORDS = re.compile(
    r"\b(?:rm\s|sudo\s|curl\s|wget\s|dd\s|mkfs|chmod\s|chown\s|"
    r"git\s+(?:push|reset|clean|branch\s+-[dD])|"
    r"docker\s|kubectl\s|terraform\s|"
    r"DROP\s+(?:TABLE|DATABASE)|TRUNCATE|DELETE\s+FROM|"
    r"pip\s+install|npm\s+install)\b",
    re.IGNORECASE,
)

_JAILBREAK_PATTERNS = [
    re.compile(
        r"ignore\s+(?:all\s+)?(?:previous|prior|above)"
        r"\s+instructions", re.I,
    ),
    re.compile(
        r"you\s+are\s+now\s+(?:DAN|evil|unfiltered|jailbroken)",
        re.I,
    ),
    re.compile(r"(?:DAN|do\s+anything\s+now)\s+mode", re.I),
    re.compile(
        r"pretend\s+(?:you\s+are|to\s+be)"
        r"\s+(?:a|an)\s+(?:evil|malicious)", re.I,
    ),
    re.compile(
        r"bypass\s+(?:your|all|any)\s+(?:safety|content|ethical)"
        r"\s+(?:filter|guard|restriction)", re.I,
    ),
    re.compile(r"system\s*:\s*you\s+(?:are|must)", re.I),
    re.compile(
        r"from\s+now\s+on.*(?:no\s+(?:limit|restriction|filter))",
        re.I,
    ),
    re.compile(r"\[(?:system|SYSTEM)\]", re.I),
    re.compile(
        r"(?:override|disable|remove)\s+(?:safety|content)"
        r"\s+(?:filter|policy|guideline)", re.I,
    ),
]

_HARMFUL_INTENT = re.compile(
    r"\b(?:"
    r"how\s+to\s+(?:make|build|create|synthesize)"
    r"\s+(?:a\s+)?(?:bomb|weapon|explosive|poison|drug|meth)|"
    r"how\s+to\s+(?:hack|break\s+into|exploit|crack)|"
    r"how\s+to\s+(?:kill|murder|assassinate|harm|torture)|"
    r"write\s+(?:a\s+)?(?:malware|ransomware|virus|trojan|"
    r"keylogger|exploit)|"
    r"(?:steal|exfiltrate|extract)"
    r"\s+(?:data|credentials|passwords|credit\s+cards)|"
    r"(?:child|minor)\s+(?:sexual|exploit|abuse|porn)"
    r")\b",
    re.IGNORECASE,
)

RISK_SCORE = {R0: 0.0, R1: 0.15, R2: 0.5, R3: 0.8, R4: 1.0}

# Threshold: if regex score >= this, skip model inference
CASCADE_THRESHOLD = 0.7


def _extract_commands(text: str) -> list[str]:
    """Extract shell commands from text."""
    cmds: list[str] = []
    for m in _CODE_BLOCK.finditer(text):
        cmds.append(m.group(1).strip())
    for m in _INLINE_CODE.finditer(text):
        snippet = m.group(1).strip()
        if _SHELL_KEYWORDS.search(snippet):
            cmds.append(snippet)
    for m in _SHELL_PREFIX.finditer(text):
        cmds.append(m.group(1).strip())
    return cmds


def _regex_score(conversation: list[dict[str, str]]) -> float:
    """Score using CCC regex guards only. Returns 0.0-1.0."""
    mx = 0.0
    for turn in conversation:
        text = turn.get("content", "")

        # Jailbreak patterns
        hits = sum(1 for p in _JAILBREAK_PATTERNS if p.search(text))
        if hits > 0:
            mx = max(mx, min(0.5 + hits * 0.2, 1.0))

        # Harmful intent
        if _HARMFUL_INTENT.search(text):
            mx = max(mx, 0.95)

        # Destruction guard on extracted commands
        for cmd in _extract_commands(text):
            risk, _ = classify_bash(cmd)
            mx = max(mx, RISK_SCORE[risk])

        # Secret scan
        result = secret_check(
            "Write", {"content": text, "file_path": "check.py"},
        )
        if result is not None:
            mx = max(mx, 0.6)

        # Exfil guard
        for cmd in _extract_commands(text):
            if exfil_check("Bash", {"command": cmd}) is not None:
                mx = max(mx, 0.9)

        if mx >= 1.0:
            return 1.0
    return mx


# ── Layer 2: HuggingFace model (lazy-loaded) ──────────────────────

_model_pipeline = None
_model_id: str | None = None


def _load_model(model_id: str) -> Any:
    """Lazy-load a HuggingFace text-classification pipeline."""
    global _model_pipeline, _model_id
    if _model_pipeline is not None and _model_id == model_id:
        return _model_pipeline

    try:
        from transformers import pipeline  # noqa: E402
    except ImportError as exc:
        raise ImportError(
            "Layer 2 requires `transformers` and `torch`. "
            "Install: pip install transformers torch"
        ) from exc

    _model_pipeline = pipeline(
        "text-classification",
        model=model_id,
        truncation=True,
        max_length=512,
    )
    _model_id = model_id
    return _model_pipeline


def _model_score(
    text: str,
    model_id: str,
) -> float:
    """Score text using HF safety classifier. Returns 0.0-1.0."""
    pipe = _load_model(model_id)
    result = pipe(text[:2048])[0]  # truncate to avoid OOM

    label = result["label"].upper()
    score = result["score"]

    # Map model output to unsafe probability.
    # protectai/deberta-v3-base-prompt-injection-v2 outputs:
    #   INJECTION / SAFE (or LABEL_1 / LABEL_0)
    if label in ("INJECTION", "LABEL_1", "UNSAFE", "TOXIC", "1"):
        return score
    if label in ("SAFE", "LABEL_0", "BENIGN", "0"):
        return 1.0 - score

    # Unknown label — use raw score as-is (conservative)
    return score


def _flatten_conversation(
    conversation: list[dict[str, str]],
) -> str:
    """Flatten conversation turns into single text for model input."""
    parts = []
    for turn in conversation:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        parts.append(f"[{role}]: {content}")
    return "\n".join(parts)


# ── Cascade moderate() ─────────────────────────────────────────────

DEFAULT_MODEL_ID = (
    "protectai/deberta-v3-base-prompt-injection-v2"
)


def _score_cascade(
    conversation: list[dict[str, str]],
    use_model: bool = False,
    model_id: str = DEFAULT_MODEL_ID,
) -> float:
    """Two-layer cascade scoring for a single conversation."""
    # Layer 1: Fast regex
    regex = _regex_score(conversation)
    if regex >= CASCADE_THRESHOLD:
        return regex

    if not use_model:
        return regex

    # Layer 2: HF model
    text = _flatten_conversation(conversation)
    model = _model_score(text, model_id)

    # Combine: take max of regex partial score and model score
    return max(regex, model)


def moderate(
    conversations: (
        list[dict[str, str]] | list[list[dict[str, str]]]
    ),
    *,
    use_model: bool = False,
    model_id: str = DEFAULT_MODEL_ID,
    **kwargs: Any,
) -> list[float]:
    """Moderation function compatible with GuardBench benchmark().

    Args:
        conversations: Single conversation or batch of conversations.
        use_model: If True, enable Layer 2 HF model for ambiguous cases.
        model_id: HuggingFace model ID for Layer 2.

    Returns:
        List of unsafe probability scores in [0.0, 1.0].
    """
    if not conversations:
        return []

    if isinstance(conversations[0], dict):
        # Single conversation (efficiency mode)
        return [_score_cascade(
            conversations, use_model=use_model, model_id=model_id,
        )]

    # Batch (effectiveness mode)
    return [
        _score_cascade(
            conv, use_model=use_model, model_id=model_id,
        )
        for conv in conversations
    ]


# ── CLI ────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Run GuardBench with cascade guard "
        "(CCC regex + optional HF model)",
    )
    parser.add_argument(
        "--datasets", nargs="*", default=None,
        help="Specific datasets (default: all)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=32,
        help="Batch size (default: 32)",
    )
    parser.add_argument(
        "--metrics", nargs="*",
        default=["f1", "recall", "precision", "auprc"],
        help="Metrics to report",
    )
    parser.add_argument(
        "--out-dir", default="results/guardbench",
        help="Output directory (default: results/guardbench)",
    )
    parser.add_argument(
        "--use-model", action="store_true",
        help="Enable Layer 2 HF model for ambiguous cases",
    )
    parser.add_argument(
        "--model-id", default=DEFAULT_MODEL_ID,
        help=f"HF model ID (default: {DEFAULT_MODEL_ID})",
    )
    args = parser.parse_args()

    import guardbench

    model_name = "cascade-guard"
    if args.use_model:
        short_id = args.model_id.split("/")[-1]
        model_name = f"cascade-{short_id}"

    datasets = args.datasets if args.datasets else "all"

    print("Running GuardBench with Cascade Guard")
    print(f"  Model name:  {model_name}")
    print(f"  Layer 2:     {args.use_model} ({args.model_id})")
    print(f"  Datasets:    {datasets}")
    print(f"  Batch size:  {args.batch_size}")
    print()

    # Pass use_model and model_id as kwargs to moderate()
    guardbench.benchmark(
        moderate=moderate,
        model_name=model_name,
        out_dir=args.out_dir,
        batch_size=args.batch_size,
        datasets=datasets,
        metrics=args.metrics,
        use_model=args.use_model,
        model_id=args.model_id,
    )


if __name__ == "__main__":
    main()
