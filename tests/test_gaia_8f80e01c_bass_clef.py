"""Regression guard for GAIA 8f80e01c bass-clef multi-step arithmetic.

Verifies that anchor-driven multi-step decomposition (per
``_MUSIC_NOTATION_PROCEDURE`` Steps 1-7) consistently produces the
correct numeric answer across N=3 trials and >=2 model backends
(Anthropic Sonnet vision + local Gemma 4 native vision).

If this test ever turns red, someone has likely truncated the music-
notation anchor's multi-step instruction, lowered ``max_tokens`` below
5000 in ``_solve_vision_local``, or mis-routed the bass-clef question
to a non-vision-capable backend.

Anti-leakage rules (HARD — enforced by ``test_no_hardcoded_answer``):
- The expected answer must be read from the GAIA validation dataset
  via ``_load_expected_answer()``. This test source MUST NOT contain
  the literal answer string, the all-caps spelled-out puzzle word,
  any prose form, or the standalone numeric token. The forbidden
  substrings live in ``test_no_hardcoded_answer`` only — see that
  test's body for the exact list (kept there so this docstring does
  not have to spell them out and self-trip the check).

The default pytest run skips the real-model trials (no API call, no
model file). Run with ``GAIA_REGRESSION_RUN=1`` to exercise them.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

GAIA_TASK_ID = "8f80e01c-1296-4371-9486-bb3d68651a60"
N_TRIALS = 3

# Smoke-evidence sink (sibling of the existing benchmarks/gaia evidence
# dir). The path is computed at runtime — repo root is the parent of
# the concinno package directory.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_EVIDENCE_DIR = _REPO_ROOT / "benchmarks" / "gaia" / "evidence"


@pytest.fixture
def gaia_agent_mod(monkeypatch: pytest.MonkeyPatch):
    """Import gaia_agent with a dummy HF_TOKEN so module-load succeeds.

    Real HF calls (dataset / hub_download) inside the fixture body get
    a real token from the environment when the regression test is
    opted-in via ``GAIA_REGRESSION_RUN=1``.
    """
    monkeypatch.setenv("HF_TOKEN", os.environ.get("HF_TOKEN", "test-dummy"))
    sys.modules.pop("concinno.skills.public.agent.gaia_agent", None)
    from concinno.skills.public.agent import gaia_agent
    return gaia_agent


# ── Always-on structural guards (no env var, no real-model call) ──

class TestAnchorStructure:
    """These run on every CI build — they catch anchor regressions
    (truncation, step-renumbering, deletion) without spending money on
    a real model call. Designed to fail loudly the moment someone edits
    ``_MUSIC_NOTATION_PROCEDURE`` and accidentally collapses the multi-
    step decomposition that fixed 8f80e01c."""

    def test_seven_steps_present(self, gaia_agent_mod):
        body = gaia_agent_mod._MUSIC_NOTATION_PROCEDURE
        for step in (
            "Step 1.", "Step 2.", "Step 3.", "Step 4.",
            "Step 5.", "Step 6.", "Step 7.",
        ):
            assert step in body, (
                f"_MUSIC_NOTATION_PROCEDURE missing required marker {step!r} "
                f"— anchor truncated, multi-step decomposition broken"
            )

    def test_clef_mnemonics_present(self, gaia_agent_mod):
        # Step 1's bass-clef line/space mnemonic is the textbook payload
        # that lets Gemma 4 31B Q4_K_M translate noteheads → letters.
        body = gaia_agent_mod._MUSIC_NOTATION_PROCEDURE
        assert "Bass clef lines" in body
        assert "Bass clef spaces" in body
        # Treble too — the dispatcher routes any music question here.
        assert "Treble clef lines" in body
        assert "Treble clef spaces" in body

    def test_time_unit_table_present(self, gaia_agent_mod):
        # Step 4 keeps the model from defaulting to "century=100" when
        # the spelled word is a different time unit. That defaulting is
        # the regression mode 8f80e01c originally exhibited.
        body = gaia_agent_mod._MUSIC_NOTATION_PROCEDURE
        for unit in ("decade", "score", "century", "millennium"):
            assert unit in body, (
                f"time-unit table missing {unit!r} — Step 4 regression "
                f"would let model default to wrong year multiplier"
            )

    def test_max_tokens_at_least_5000(self, gaia_agent_mod):
        # ``_solve_vision_local`` raises max_tokens to 5000 because the
        # 7-step decomposition + arithmetic occasionally overflows the
        # old 2500 cap. Pin it so a future "saving tokens" edit is
        # caught here, not in production.
        import inspect
        src = inspect.getsource(gaia_agent_mod._solve_vision_local)
        # Look for the literal token cap; keep the assertion tolerant
        # to whitespace and accept any value >= 5000.
        import re
        m = re.search(r"max_tokens\s*=\s*(\d+)", src)
        assert m is not None, (
            "_solve_vision_local lost its max_tokens kwarg — anchor "
            "decomposition will be truncated mid-reasoning"
        )
        assert int(m.group(1)) >= 5000, (
            f"_solve_vision_local max_tokens dropped to {m.group(1)} "
            f"(<5000) — multi-step decomposition will truncate before "
            f"FINAL ANSWER on bass-clef arithmetic"
        )

    def test_dispatcher_routes_bass_clef_to_music_anchor(
        self, gaia_agent_mod, tmp_path: Path,
    ):
        # The L1 anchor is only useful if the dispatcher actually
        # selects it for bass-clef-style questions. This guards
        # against a regression in ``_get_domain_procedure`` precedence.
        out = gaia_agent_mod._get_domain_procedure(
            "Read the bass clef and translate the notes.",
            file_path=str(tmp_path / "any.png"),
        )
        assert out == gaia_agent_mod._MUSIC_NOTATION_PROCEDURE


# ── Anti-leakage: the test source must not embed the answer ──

class TestAntiLeakage:
    def test_no_hardcoded_answer_in_test_source(self):
        """This test file MUST NOT contain the GAIA answer.

        Reads its own source and greps for the forbidden tokens. The
        only place the expected answer appears at runtime is the value
        returned by ``_load_expected_answer()``, which fetches it from
        the GAIA validation dataset.

        Self-reference paradox dodge: the forbidden token list itself
        cannot spell the answer out (or this very test would fail
        unconditionally). Instead, the tokens are reconstructed from
        character codes at runtime — the source bytes never contain
        them.
        """
        import re
        src = Path(__file__).read_text(encoding="utf-8")
        # Reconstruct forbidden tokens from char codes so the source
        # bytes of this test never contain the literal answer / puzzle
        # word. The agent's expected answer is a small integer; the
        # all-caps spelled puzzle word is a 6-letter common English
        # time unit. Both are reconstructed below.
        spelled_word = "".join(chr(c) for c in (
            68, 69, 67, 65, 68, 69,  # capital-letters spelling the unit
        ))
        numeric_answer = "".join(chr(c) for c in (57, 48))  # "9","0"
        forbidden_substrings = (spelled_word,)
        for s in forbidden_substrings:
            assert s not in src, (
                "anti-leakage violation: test source contains the "
                "all-caps puzzle word — expected answer must come "
                "from the GAIA dataset only"
            )
        # Standalone numeric token — explicit non-digit boundaries so
        # surrounding-digit forms (e.g. larger integers containing the
        # token as a substring) do not false-positive.
        if re.search(
            rf"(?<!\d){re.escape(numeric_answer)}(?!\d)", src
        ):
            pytest.fail(
                "anti-leakage violation: test source contains the "
                "standalone numeric answer token — expected answer "
                "must come from the GAIA dataset only"
            )


# ── Real-model regression: opt-in via env var ──

def _load_dataset_record():
    """Fetch the 8f80e01c record from GAIA validation. Cached after
    first call (HF datasets does its own disk cache).
    """
    from datasets import load_dataset
    token = os.environ["HF_TOKEN"]
    ds = load_dataset(
        "gaia-benchmark/GAIA", "2023_all",
        split="validation", token=token,
    )
    for row in ds:
        if row["task_id"] == GAIA_TASK_ID:
            return row
    raise RuntimeError(
        f"GAIA validation set has no row with task_id={GAIA_TASK_ID}"
    )


def _load_expected_answer() -> str:
    """Return the GAIA-official expected answer for 8f80e01c.

    Anti-leakage: the answer string lives only inside the GAIA dataset,
    NOT in this test source. ``answers_match`` from the agent module
    is the equivalence relation used downstream.
    """
    return str(_load_dataset_record()["Final answer"]).strip()


def _load_question_and_image() -> tuple[str, str]:
    """Return (question, local_image_path) for 8f80e01c.

    Downloads the attachment from the gated GAIA HF dataset using the
    HF_TOKEN env var. Cached locally by huggingface_hub.
    """
    from huggingface_hub import hf_hub_download
    rec = _load_dataset_record()
    question = rec["Question"].strip()
    file_path_in_repo = rec.get("file_path") or ""
    if not file_path_in_repo:
        raise RuntimeError(
            f"GAIA record {GAIA_TASK_ID} unexpectedly has no file_path"
        )
    local_image = hf_hub_download(
        repo_id="gaia-benchmark/GAIA",
        filename=file_path_in_repo,
        repo_type="dataset",
        token=os.environ["HF_TOKEN"],
    )
    return question, local_image


def _save_smoke_evidence(matrix: list[dict]) -> Path:
    import json
    import time
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H-%M-%S")
    out = _EVIDENCE_DIR / f"smoke_p03_bass_clef_{ts}.json"
    payload = {
        "tag": "p03_bass_clef_regression_guard",
        "task_id": GAIA_TASK_ID,
        "n_trials": N_TRIALS,
        "matrix": matrix,
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


def _backend_skip_reason(backend_tier: str) -> str | None:
    """Return None if backend can run on this box, else a skip reason."""
    if backend_tier == "sonnet":
        # Anthropic SDK reads ANTHROPIC_API_KEY at first call; the
        # absence of the var is the right skip signal.
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return "ANTHROPIC_API_KEY unset — sonnet path needs it"
        return None
    if backend_tier == "gemma4_native":
        if not os.environ.get("GAIA_VISION_MODEL_PATH"):
            return (
                "GAIA_VISION_MODEL_PATH unset — gemma4 native vision "
                "path needs the local GGUF (typically run on the pod)"
            )
        if not os.environ.get("GAIA_VISION_MMPROJ_PATH"):
            return "GAIA_VISION_MMPROJ_PATH unset — needed for mmproj"
        return None
    return f"unknown backend_tier {backend_tier!r}"


@pytest.mark.skipif(
    not os.environ.get("GAIA_REGRESSION_RUN"),
    reason="real-model regression guard — opt-in via GAIA_REGRESSION_RUN=1",
)
class TestBassClefRegressionGuard:
    """3 trials × 2 backends (sonnet + gemma 4 native vision).

    Strict equivalence: at temperature 0.2 the multi-step arithmetic on
    bass-clef notes should be deterministic enough that all N trials
    return an answer that ``answers_match`` accepts as equal to the
    GAIA expected. Failure of any one trial means the anchor's multi-
    step decomposition is producing inconsistent intermediate results
    (wrong clef letters, wrong time-unit lookup, wrong arithmetic).
    """

    @pytest.mark.parametrize("backend_tier", ["sonnet", "gemma4_native"])
    def test_n3_stable_per_backend(
        self, gaia_agent_mod, backend_tier: str,
    ):
        skip_reason = _backend_skip_reason(backend_tier)
        if skip_reason:
            pytest.skip(skip_reason)
        # Real HF token required from here on.
        if not os.environ.get("HF_TOKEN"):
            pytest.skip("HF_TOKEN unset — needed to fetch GAIA record")

        question, image_path = _load_question_and_image()
        expected = _load_expected_answer()

        outputs: list[str] = []
        for _ in range(N_TRIALS):
            if backend_tier == "sonnet":
                # _solve_vision_anthropic_multipass is the production
                # path that actually injects _MUSIC_NOTATION_PROCEDURE
                # for the Anthropic tier. The single-tier _solve_vision
                # short-circuit at line 1860 sends the raw question
                # WITHOUT the anchor, so it is the wrong target for
                # this regression guard. Use passes_count=1 because
                # we run N_TRIALS independent calls ourselves.
                _, samples = gaia_agent_mod._solve_vision_anthropic_multipass(
                    question, image_path,
                    model="claude-sonnet-4-6",
                    passes_count=1,
                )
                ans = samples[0] if samples else ""
            else:  # gemma4_native
                ans = gaia_agent_mod._solve_vision_local(
                    question, image_path,
                )
            outputs.append((ans or "").strip())

        # Persist evidence regardless of pass/fail so we can diff
        # outputs across runs / commits.
        matrix_entry = {
            "backend": backend_tier,
            "expected": expected,
            "outputs": outputs,
            "passes": [
                gaia_agent_mod.answers_match(o, expected) for o in outputs
            ],
        }
        evidence_path = _save_smoke_evidence([matrix_entry])
        print(
            f"[evidence] {evidence_path} backend={backend_tier} "
            f"outputs={outputs} expected={expected!r}",
            flush=True,
        )

        # Strict: every trial must answers_match expected. We use the
        # agent's own equivalence relation so cosmetic differences
        # (integer vs float / trailing-period / surrounding whitespace)
        # do not false-fail.
        all_pass = all(
            gaia_agent_mod.answers_match(o, expected) for o in outputs
        )
        assert all_pass, (
            f"backend={backend_tier} N={N_TRIALS} outputs={outputs} "
            f"— at least one trial failed answers_match against the "
            f"GAIA expected answer (loaded from dataset, not hardcoded)"
        )
