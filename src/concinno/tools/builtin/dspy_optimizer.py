"""concinno.tools.builtin.dspy_optimizer — DSPy MIPROv2 prompt optimizer.

Wraps Stanford DSPy MIPROv2 Bayesian search to auto-tune CBUA stage prompts
from manual feedback-loop iteration to data-driven optimization.

Feature flag: ``dspy_prompt_optimization`` (default OFF — opt-in via
``concinno features set dspy_prompt_optimization enabled true``).

Supported stage targets (by DSPy adaptability, highest first):

1. **Critic** (``agent/mas_prompts.py:DEFAULT_CRITIC_PROMPT``) — GAIA 3-role
   adversarial MAS. Has exact-match ground truth from GAIA sediment.
2. **Judge** (``agent/mas_prompts.py:DEFAULT_JUDGE_PROMPT``) — adversarial
   answer selection. Same ground truth.
3. **Injection detector** (``security/llm_judge_guard.py``) — injection F1.

C0Router and cognitive/router are pure-heuristic regex — no LLM calls,
no prompt to optimize. They are intentionally excluded.

Usage (offline, dev-only, burns LLM credits)::

    import dspy
    from concinno.tools.builtin.dspy_optimizer import (
        DspyOptimizer, CriticModule, JudgeModule,
        build_critic_examples, gaia_exact_match,
    )

    dspy.configure(lm=dspy.LM("openai/gpt-4o-mini"))
    optimizer = DspyOptimizer()
    examples = build_critic_examples([
        {"question": "Q1", "solver_answer": "A1", "gold": "A1"},
        ...
    ])
    optimized = optimizer.optimize_prompt(
        CriticModule(),
        training_examples=examples,
        metric_fn=gaia_exact_match,
    )

@module concinno.tools.builtin.dspy_optimizer
@responsibility Wrap DSPy MIPROv2 for opt-in CBUA prompt optimization.
    Never touches prod LLM calls — caller supplies the LM configuration.
@dependencies dspy (optional, pip install dspy)
@exports DspyOptimizer, CriticModule, JudgeModule,
    CriticSignature, JudgeSignature,
    build_critic_examples, build_judge_examples,
    gaia_exact_match, normalize_answer
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    pass  # avoid circular at runtime

__all__ = [
    "DspyOptimizer",
    "CriticModule",
    "JudgeModule",
    "CriticSignature",
    "JudgeSignature",
    "build_critic_examples",
    "build_judge_examples",
    "gaia_exact_match",
    "normalize_answer",
]

# ---------------------------------------------------------------------------
# Lazy DSPy import — dspy is an optional dependency
# ---------------------------------------------------------------------------

def _require_dspy():
    """Import dspy or raise ImportError with install hint."""
    try:
        import dspy  # noqa: PLC0415
        return dspy
    except ImportError as exc:
        raise ImportError(
            "dspy is required for prompt optimization. "
            "Install it with: pip install dspy"
        ) from exc


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------

def _build_critic_signature():
    dspy = _require_dspy()

    class CriticSignature(dspy.Signature):
        """Given a question and the solver's answer + trace, produce a single
        concrete final answer. You may agree with or diverge from the solver."""

        question: str = dspy.InputField(desc="The benchmark question to answer")
        solver_answer: str = dspy.InputField(
            desc="The solver agent's final answer (may be empty)"
        )
        solver_trace_summary: str = dspy.InputField(
            desc="Top tool results from the solver (may be empty)"
        )
        final_answer: str = dspy.OutputField(
            desc="Single concrete answer — a number, name, or short phrase. "
                 "No hedging. No 'I cannot'. Commit to one value."
        )

    return CriticSignature


def _build_judge_signature():
    dspy = _require_dspy()

    class JudgeSignature(dspy.Signature):
        """Two candidate answers shown in randomised order. Pick the one more
        likely to be correct. Output the chosen *answer value*, not a label."""

        question: str = dspy.InputField(desc="The benchmark question")
        response_1: str = dspy.InputField(desc="Candidate answer 1")
        response_2: str = dspy.InputField(desc="Candidate answer 2")
        final_answer: str = dspy.OutputField(
            desc="The chosen answer value (not '1' or '2' — the actual value). "
                 "Pick the one with stronger verifiable reasoning."
        )

    return JudgeSignature


# Public access to signatures (lazy construction)
class _SignatureProxy:
    """Lazy-loading proxy so importing this module doesn't fail without dspy."""

    def __init__(self, builder):
        self._builder = builder
        self._cls = None

    def __call__(self, *args, **kwargs):
        return self._get_class()(*args, **kwargs)

    def _get_class(self):
        if self._cls is None:
            self._cls = self._builder()
        return self._cls

    def __getattr__(self, name):
        return getattr(self._get_class(), name)


CriticSignature = _SignatureProxy(_build_critic_signature)
JudgeSignature = _SignatureProxy(_build_judge_signature)


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------

def _make_critic_module():
    """Build CriticModule as a proper dspy.Module subclass (required by MIPROv2)."""
    dspy = _require_dspy()
    sig_cls = _build_critic_signature()

    class CriticModule(dspy.Module):
        """DSPy ChainOfThought module for the MAS critic role.

        Wraps ``CriticSignature`` with CoT reasoning so MIPROv2 can propose
        improved instructions while preserving the reasoning chain.

        Must subclass ``dspy.Module`` — MIPROv2.compile requires a Module.
        """

        def __init__(self):
            super().__init__()
            self.predict = dspy.ChainOfThought(sig_cls)

        def forward(self, question: str, solver_answer: str = "",
                    solver_trace_summary: str = ""):
            return self.predict(
                question=question,
                solver_answer=solver_answer,
                solver_trace_summary=solver_trace_summary,
            )

    return CriticModule


def _make_judge_module():
    """Build JudgeModule as a proper dspy.Module subclass (required by MIPROv2)."""
    dspy = _require_dspy()
    sig_cls = _build_judge_signature()

    class JudgeModule(dspy.Module):
        """DSPy ChainOfThought module for the MAS judge role."""

        def __init__(self):
            super().__init__()
            self.predict = dspy.ChainOfThought(sig_cls)

        def forward(self, question: str, response_1: str, response_2: str):
            return self.predict(
                question=question,
                response_1=response_1,
                response_2=response_2,
            )

    return JudgeModule


class _ModuleProxy:
    """Lazy-loading proxy for dspy.Module subclasses.

    Defers dspy import until first instantiation so importing this
    module doesn't fail in environments without dspy installed.
    """

    def __init__(self, builder):
        self._builder = builder
        self._cls = None

    def _get_class(self):
        if self._cls is None:
            self._cls = self._builder()
        return self._cls

    def __call__(self, *args, **kwargs):
        return self._get_class()(*args, **kwargs)

    def __instancecheck__(self, instance):
        return isinstance(instance, self._get_class())


CriticModule = _ModuleProxy(_make_critic_module)
JudgeModule = _ModuleProxy(_make_judge_module)


# ---------------------------------------------------------------------------
# Training data builders
# ---------------------------------------------------------------------------

def build_critic_examples(
    records: list[dict],
) -> list:
    """Build DSPy Example list for critic optimization.

    Each record must have:
        - ``question`` (str)
        - ``solver_answer`` (str, may be empty)
        - ``gold`` (str) — the ground-truth answer

    Optional:
        - ``solver_trace_summary`` (str)

    Returns:
        List of ``dspy.Example`` with ``gold_answer`` as the label field.
    """
    dspy = _require_dspy()
    examples = []
    for r in records:
        ex = dspy.Example(
            question=r["question"],
            solver_answer=r.get("solver_answer", ""),
            solver_trace_summary=r.get("solver_trace_summary", ""),
            gold_answer=r["gold"],
        ).with_inputs("question", "solver_answer", "solver_trace_summary")
        examples.append(ex)
    return examples


def build_judge_examples(
    records: list[dict],
) -> list:
    """Build DSPy Example list for judge optimization.

    Each record must have:
        - ``question`` (str)
        - ``response_1`` (str)
        - ``response_2`` (str)
        - ``gold`` (str) — the correct answer value (not label '1' or '2')
    """
    dspy = _require_dspy()
    examples = []
    for r in records:
        ex = dspy.Example(
            question=r["question"],
            response_1=r["response_1"],
            response_2=r["response_2"],
            gold_answer=r["gold"],
        ).with_inputs("question", "response_1", "response_2")
        examples.append(ex)
    return examples


# ---------------------------------------------------------------------------
# Metric function
# ---------------------------------------------------------------------------

def normalize_answer(s: str) -> str:
    """Normalize an answer string for comparison.

    Steps (matching GAIA leaderboard normalization):
    1. Unicode NFKC normalization
    2. Strip leading/trailing whitespace
    3. Lower-case
    4. Remove trailing punctuation (.,;:!?)
    5. Collapse internal whitespace
    6. Numeric: strip trailing .0 for whole floats ("42.0" → "42")
    """
    if not s:
        return ""
    # Unicode normalization
    s = unicodedata.normalize("NFKC", s)
    s = s.strip().lower()
    # Strip trailing punctuation
    s = re.sub(r"[.,;:!?]+$", "", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    # Numeric: "42.0" → "42"
    s = re.sub(r"^(-?\d+)\.0+$", r"\1", s)
    return s


def gaia_exact_match(example, pred, trace=None) -> float:
    """DSPy metric: 1.0 if prediction matches gold_answer, else 0.0.

    Compatible with MIPROv2's ``metric`` parameter signature::

        metric(example, prediction, trace=None) -> float | bool

    Uses ``normalize_answer`` for case/whitespace/punctuation tolerance.
    """
    gold = normalize_answer(getattr(example, "gold_answer", ""))
    predicted = normalize_answer(getattr(pred, "final_answer", ""))
    return float(gold == predicted)


# ---------------------------------------------------------------------------
# Optimizer
# ---------------------------------------------------------------------------

class DspyOptimizer:
    """Wrap DSPy MIPROv2 for CBUA prompt optimization.

    Feature-gated: checks ``dspy_prompt_optimization.enabled`` before
    running. When the feature is disabled, ``optimize_prompt`` returns
    the original module unchanged.

    Example::

        import dspy
        from concinno.tools.builtin.dspy_optimizer import (
            DspyOptimizer, CriticModule, build_critic_examples, gaia_exact_match
        )
        dspy.configure(lm=dspy.LM("openai/gpt-4o-mini"))

        opt = DspyOptimizer(num_trials=30)
        examples = build_critic_examples([...])
        optimized_module = opt.optimize_prompt(
            CriticModule(), examples, gaia_exact_match
        )
    """

    def __init__(
        self,
        num_trials: int = 20,
        num_candidates: int = 5,
    ) -> None:
        self._num_trials = num_trials
        self._num_candidates = num_candidates

    def _feature_enabled(self) -> bool:
        """Return True if dspy_prompt_optimization is enabled in feature config."""
        try:
            from concinno.core.config import get_config  # noqa: PLC0415
            cfg = get_config()
            return bool(cfg.feature("dspy_prompt_optimization", "enabled"))
        except Exception:
            return False

    def optimize_prompt(
        self,
        prompt_module,
        training_examples: list,
        metric_fn: Callable,
        *,
        num_trials: int | None = None,
        num_candidates: int | None = None,
    ):
        """Run MIPROv2 Bayesian optimization on ``prompt_module``.

        Args:
            prompt_module: A ``CriticModule()``, ``JudgeModule()``, or any
                ``dspy.Module`` subclass with a ``forward()`` method.
            training_examples: List of ``dspy.Example`` (use
                ``build_critic_examples`` / ``build_judge_examples``).
            metric_fn: Callable with signature
                ``metric(example, prediction, trace=None) -> float``.
            num_trials: Override default trial count.
            num_candidates: Override default candidate count.

        Returns:
            The optimized module (or original if feature is disabled /
            training_examples is empty).

        Raises:
            ImportError: If dspy is not installed.
            ValueError: If training_examples is empty.
        """
        if not self._feature_enabled():
            return prompt_module

        if not training_examples:
            raise ValueError(
                "training_examples must not be empty. "
                "Use build_critic_examples() or build_judge_examples() to build them."
            )

        dspy = _require_dspy()

        # MIPROv2.compile requires a dspy.Module (has forward() + named_parameters()).
        # CriticModule / JudgeModule already subclass dspy.Module.
        # For plain ChainOfThought/Predict passed directly, they also subclass Module.
        if not isinstance(prompt_module, dspy.Module):
            raise TypeError(
                f"prompt_module must be a dspy.Module subclass, got {type(prompt_module)}. "
                "Use CriticModule() or JudgeModule() from this package."
            )

        # Read auto_mode from feature config (default "light").
        # DSPy MIPROv2 constraint: when auto is not None, num_candidates and
        # num_trials must NOT be passed to __init__ or compile — they would be
        # overridden by auto and the call raises ValueError.
        auto_mode: str | None = self._auto_mode()

        if auto_mode is not None:
            # auto controls trial/candidate counts internally.
            teleprompter = dspy.MIPROv2(
                metric=metric_fn,
                auto=auto_mode,
                verbose=False,
            )
            optimized = teleprompter.compile(
                prompt_module,
                trainset=training_examples,
                minibatch=False,
            )
        else:
            # Manual control: caller-supplied or __init__ defaults apply.
            trials = num_trials if num_trials is not None else self._num_trials
            candidates = num_candidates if num_candidates is not None else self._num_candidates
            teleprompter = dspy.MIPROv2(
                metric=metric_fn,
                auto=None,
                num_candidates=candidates,
                verbose=False,
            )
            optimized = teleprompter.compile(
                prompt_module,
                trainset=training_examples,
                num_trials=trials,
                minibatch=False,
            )
        return optimized

    def _auto_mode(self) -> str | None:
        """Read auto_mode param from feature config. Returns None for manual control."""
        try:
            from concinno.core.config import get_config  # noqa: PLC0415
            cfg = get_config()
            val = cfg.feature("dspy_prompt_optimization", "auto_mode")
            if val in ("light", "medium", "heavy"):
                return val
            return "light"  # safe default when feature is on
        except Exception:
            return "light"
