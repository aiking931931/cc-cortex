"""Granite Guardian 2B adapter for GuardBench — official template + vllm.

Cascade architecture:
  Layer 1: CCC regex guards (fast, <1ms)
  Layer 2: Granite Guardian 2B via vllm (official apply_chat_template)
"""
from __future__ import annotations

import re
import sys
import threading
from pathlib import Path
from typing import Any

_src = Path(__file__).resolve().parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from concinno.destruction_guard import (  # noqa: E402
    R0,
    R1,
    R2,
    R3,
    R4,
    classify_bash,
)
from concinno.exfil_guard import check as exfil_check  # noqa: E402
from concinno.secret_scan import check as secret_check  # noqa: E402

# ── Layer 1: CCC regex ───────────────────────────────────────

_CODE_BLOCK = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]{4,})`")
_SHELL_KW = re.compile(
    r"\b(?:rm\s|sudo\s|curl\s|wget\s|dd\s|mkfs|chmod\s|"
    r"git\s+(?:push|reset|clean|branch\s+-[dD])|"
    r"docker\s|kubectl\s|terraform\s|"
    r"DROP\s+(?:TABLE|DATABASE)|TRUNCATE|DELETE\s+FROM)\b",
    re.IGNORECASE,
)
_JAILBREAK = [
    re.compile(
        r"ignore\s+(?:all\s+)?(?:previous|prior|above)"
        r"\s+instructions",
        re.I,
    ),
    re.compile(
        r"you\s+are\s+now\s+(?:DAN|evil|unfiltered)", re.I,
    ),
    re.compile(r"(?:DAN|do\s+anything\s+now)\s+mode", re.I),
    re.compile(
        r"bypass\s+(?:your|all|any)\s+(?:safety|content)"
        r"\s+(?:filter|guard)",
        re.I,
    ),
    re.compile(
        r"(?:override|disable|remove)\s+(?:safety|content)"
        r"\s+(?:filter|policy)",
        re.I,
    ),
]
_HARMFUL = re.compile(
    r"\b(?:"
    r"how\s+to\s+(?:make|build|create|synthesize)"
    r"\s+(?:a\s+)?(?:bomb|weapon|explosive|poison|drug)|"
    r"how\s+to\s+(?:hack|break\s+into|exploit|crack)|"
    r"how\s+to\s+(?:kill|murder|assassinate|harm|torture)|"
    r"write\s+(?:a\s+)?(?:malware|ransomware|virus|trojan)|"
    r"(?:steal|exfiltrate)\s+(?:data|credentials|passwords)|"
    r"(?:child|minor)\s+(?:sexual|exploit|abuse|porn)"
    r")\b",
    re.IGNORECASE,
)
RISK_SCORE = {R0: 0.0, R1: 0.15, R2: 0.5, R3: 0.8, R4: 1.0}
CASCADE_TH = 0.95  # Only skip model for certain regex hits


def _extract_cmds(text: str) -> list[str]:
    cmds: list[str] = []
    for m in _CODE_BLOCK.finditer(text):
        cmds.append(m.group(1).strip())
    for m in _INLINE_CODE.finditer(text):
        if _SHELL_KW.search(m.group(1)):
            cmds.append(m.group(1).strip())
    return cmds


def _regex_score(conv: list[dict[str, str]]) -> float:
    mx = 0.0
    for turn in conv:
        text = turn.get("content", "")
        hits = sum(1 for p in _JAILBREAK if p.search(text))
        if hits > 0:
            mx = max(mx, min(0.5 + hits * 0.2, 1.0))
        if _HARMFUL.search(text):
            mx = max(mx, 0.95)
        for cmd in _extract_cmds(text):
            risk, _ = classify_bash(cmd)
            mx = max(mx, RISK_SCORE[risk])
        r = secret_check(
            "Write", {"content": text, "file_path": "c.py"},
        )
        if r is not None:
            mx = max(mx, 0.6)
        for cmd in _extract_cmds(text):
            if exfil_check("Bash", {"command": cmd}) is not None:
                mx = max(mx, 0.9)
        if mx >= 1.0:
            return 1.0
    return mx


# ── Layer 2: Granite Guardian 2B — official template + vllm ──

MODEL_ID = "ibm-granite/granite-guardian-3.1-8b"
_llm = None
_tokenizer = None
_lock = threading.Lock()


def _load():
    global _llm, _tokenizer
    if _llm is not None:
        return _llm, _tokenizer
    with _lock:
        if _llm is not None:
            return _llm, _tokenizer
        from transformers import AutoTokenizer
        from vllm import LLM

        print(f"Loading {MODEL_ID} via vllm...")
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        _llm = LLM(
            model=MODEL_ID,
            dtype="bfloat16",
            max_model_len=2048,
            gpu_memory_utilization=0.9,
        )
        print("Loaded.")
    return _llm, _tokenizer


def _build_prompts(
    convs: list[list[dict[str, str]]],
) -> list[str]:
    """Build prompts using official apply_chat_template."""
    _, tokenizer = _load()
    prompts = []
    for conv in convs:
        # Build messages in Guardian format
        msgs = []
        for turn in conv:
            role = turn.get("role", "user")
            content = turn.get("content", "")[:1500]
            if role in ("user", "assistant", "system"):
                msgs.append({"role": role, "content": content})
            else:
                msgs.append({"role": "user", "content": content})

        # If no assistant response, just check user prompt
        guardian_config = {"risk_name": "harm"}

        try:
            prompt = tokenizer.apply_chat_template(
                msgs,
                guardian_config=guardian_config,
                add_generation_prompt=True,
                tokenize=False,
            )
        except Exception:
            # Fallback: single user message
            last = msgs[-1]["content"] if msgs else ""
            prompt = tokenizer.apply_chat_template(
                [{"role": "user", "content": last}],
                guardian_config=guardian_config,
                add_generation_prompt=True,
                tokenize=False,
            )
        prompts.append(prompt)
    return prompts


def _extract_yes_prob(output) -> float:
    """Extract P('Yes') directly from vllm logprobs.

    Direct P(Yes) works better than P(Yes)/(P(Yes)+P(No))
    normalization because Guardian's token distribution has
    significant probability mass on non-Yes/No tokens.
    Normalizing distorts the calibration.
    """
    import math

    lp = output.outputs[0].logprobs
    if not lp or len(lp) == 0:
        t = output.outputs[0].text.strip().lower()
        return 0.8 if t.startswith("yes") else 0.2

    first_token_logprobs = lp[0]
    for _token_id, logprob_obj in first_token_logprobs.items():
        token_str = logprob_obj.decoded_token.strip()
        if token_str.lower() == "yes":
            return min(math.exp(logprob_obj.logprob), 1.0)

    # "Yes" not in top logprobs → model strongly says "No"
    generated = output.outputs[0].text.strip().lower()
    if generated.startswith("yes"):
        return 0.7
    return 0.1


def _batch_guardian(
    convs: list[list[dict[str, str]]],
) -> list[float]:
    """Batch score via vllm logprobs — official method."""
    from vllm import SamplingParams

    llm, _ = _load()
    prompts = _build_prompts(convs)
    params = SamplingParams(
        max_tokens=1,
        temperature=0,
        logprobs=20,
    )
    outputs = llm.generate(prompts, params)
    return [_extract_yes_prob(o) for o in outputs]


# ── Cascade moderate() ───────────────────────────────────────


def moderate(
    conversations: (
        list[dict[str, str]] | list[list[dict[str, str]]]
    ),
    **kwargs: Any,
) -> list[float]:
    """GuardBench-compatible moderate — batch cascade."""
    if not conversations:
        return []

    if isinstance(conversations[0], dict):
        conversations = [conversations]

    # Layer 1: fast regex
    regex_scores = [_regex_score(c) for c in conversations]

    # Layer 2: vllm batch on uncaught
    needs = []
    idx = []
    for i, (s, c) in enumerate(
        zip(regex_scores, conversations),
    ):
        if s < CASCADE_TH:
            needs.append(c)
            idx.append(i)

    if needs:
        model_scores = _batch_guardian(needs)
        # Calibrate: sigmoid((p - 0.12) * 2) shifts decision
        # boundary to optimal point for F1 at threshold=0.5
        import math

        for i, ms in zip(idx, model_scores):
            calibrated = 1.0 / (1.0 + math.exp(-(ms - 0.12) * 2))
            regex_scores[i] = max(regex_scores[i], calibrated)

    return regex_scores
