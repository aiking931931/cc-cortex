"""concinno.prompt_hooks — LLM-as-Judge via Claude Code prompt hooks.

@module prompt_hooks
@responsibility Curated judge prompts + settings.json installer targeting
    the prompt-type hook runtime documented at
    https://docs.anthropic.com/en/docs/claude-code/hooks. Concinno never
    calls an LLM directly (core is zero-dep and L3 forbids hook-side LLM
    calls); instead this module emits ``hooks`` config that the Claude
    Code CLI executes with its own evaluator.
@dependencies stdlib only (``json``, ``pathlib``, ``dataclasses``)
@exports PromptJudge, HALLUCINATION_JUDGE, EXCUSE_SCANNER_JUDGE,
    CODE_QUALITY_JUDGE, ALL_JUDGES, build_hook_config,
    install_prompt_hooks, uninstall_prompt_hooks

Rationale (1.4.0 C6 — H1 reopen):
  The H1 ``LLM-as-Judge`` idea had previously been shelved in 1.3.0
  because Concinno's core cannot import an LLM SDK (zero runtime deps +
  L3 hook-side LLM ban). Per the public hooks documentation above, the
  prompt-type hook runs a short single-turn evaluation inside the user's
  CLI runtime, which removes the original blocker.

  Concinno's role is narrow on purpose: ship *well-written judge prompts*
  as module constants plus a settings.json installer. The user's CLI
  runtime does the actual evaluation. Judges and installer are fully
  tested; integration with the live runtime is the user's choice.

Design constraints:
  - Pure stdlib, no runtime deps.
  - Settings writes are atomic (temp file + replace) so a crash mid-
    write does not corrupt the user's config.
  - Installer is idempotent: each judge is tagged with a marker token
    in ``statusMessage`` and the prompt header, so running ``install``
    twice leaves the file unchanged and ``uninstall`` is unambiguous.
  - Never touches hook specs the user (or another tool) added — we
    only append, match, and remove by marker.
  - No personal paths; the caller passes the settings.json location.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

MARKER_PREFIX = "[concinno:"
"""Marker token embedded at the start of CCC-owned prompts / statusMessage.

Downstream detection reads this prefix to tell CCC-owned hook specs
apart from specs the user wrote by hand. Changing this value is a
breaking change for existing installations — bump the module contract
and provide a migration shim if it ever happens.
"""

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
"""Default evaluation model.

Haiku 4.5 is the 2026-04 fast-path default: strong enough for
single-turn yes/no evaluation, cheap enough to run on every tool call
without budget anxiety. Callers can override per judge via
``PromptJudge.model``.
"""

DEFAULT_TIMEOUT = 30
"""Default prompt-hook timeout in seconds, matching CC docs."""


VALID_DECISIONS = frozenset({"block", "allow", "route"})
"""Enum of known ``decision`` string values a judge may emit.

Contract (2.11.0+): the ``decision`` enum is *open*. Any caller that
parses judge-emitted ``decision`` MUST treat an unknown value as
``allow`` (fail-open) rather than raising. CC hook runtime itself
already does this — only ``block`` actively denies; everything else
passes. Downstream Concinno code that inspects ``decision`` SHOULD
use ``match / case`` with a ``_`` fallthrough, never ``assert
decision in {"block","allow"}``.

``route`` (new in 2.11.0) signals "allow the tool call AND emit a
side-channel advisory" — the CC runtime treats it as allow, and
Concinno's optional dispatcher (see ``concinno.prompt_hooks.routes``)
can read the advisory payload for information-preserving escalation
(e.g. write stderr, append to log, spawn subagent). 2.11.0 ships
advisory-only; active cross-process dispatch lands in later versions
once CC exposes hook-output channels (or Sancio supervises at the
process layer).
"""


# ── PromptJudge dataclass ──────────────────────────────────


@dataclass(frozen=True)
class PromptJudge:
    """A single judge prompt + the hook event it attaches to.

    Every judge is identified by ``name``; CCC tags ownership using
    ``[concinno:<name>]`` in both the rendered prompt and the
    ``statusMessage`` so install / uninstall can find the right spec
    without guessing.

    Attributes:
        name: unique identifier (snake_case). Used in the marker.
        event: Claude Code hook event (``PostToolUse``, ``Stop``, ...).
        matcher: tool matcher regex string. Empty string means no
            matcher — the CC runtime treats that as "all tools" for
            tool events. Non-tool events (``Stop``, ``UserPromptSubmit``)
            should leave this empty.
        prompt_body: the actual judge prompt. ``build_spec`` prepends
            the marker header and appends the ``$ARGUMENTS`` slot if
            the body does not already include one.
        description: short human description used in documentation.
        model: evaluation model. Defaults to Haiku.
        timeout: seconds before CC cancels the evaluation.
        status_message: spinner label shown while the hook runs; the
            installer prepends the marker automatically.
    """

    name: str
    event: str
    matcher: str
    prompt_body: str
    description: str
    model: str = DEFAULT_MODEL
    timeout: int = DEFAULT_TIMEOUT
    status_message: str = ""

    def marker(self) -> str:
        """Return the ownership marker token for this judge."""
        return f"{MARKER_PREFIX}{self.name}]"

    def rendered_prompt(self) -> str:
        """Return the full prompt string the hook spec should carry."""
        header = f"{self.marker()} CCC judge prompt — do not edit inline.\n\n"
        body = self.prompt_body
        # Ensure the body tells the runtime where to splice the hook
        # JSON input. CC replaces $ARGUMENTS before dispatch.
        if "$ARGUMENTS" not in body:
            body = body.rstrip() + "\n\nHook input JSON:\n$ARGUMENTS\n"
        return header + body

    def rendered_status_message(self) -> str:
        """Return the spinner label, always tagged with the marker."""
        if self.status_message:
            return f"{self.marker()} {self.status_message}"
        return f"{self.marker()} running…"

    def build_spec(self) -> dict[str, Any]:
        """Return the hook spec dict CC expects inside ``hooks: [...]``."""
        spec: dict[str, Any] = {
            "type": "prompt",
            "prompt": self.rendered_prompt(),
            "model": self.model,
            "timeout": self.timeout,
            "statusMessage": self.rendered_status_message(),
        }
        return spec


# ── Judge prompt constants ─────────────────────────────────


_HALLUCINATION_BODY = """You are HallucinationJudge, a terse reviewer that inspects the last
written artifact for unsourced factual claims.

A claim is unsourced when the writer states something as fact
(specific numbers, historical events, API behavior, library
versions, research results) without citing a source the reader
can verify — code path, commit, file, URL, or explicit "I inferred".

For each unsourced claim, note it. If the artifact is pure code,
prose that only describes the diff, or references only files that
exist in the repo, return allow.

Return a JSON object:
  {"decision": "block", "reason": "<one sentence>"}  if any
    unsourced factual claim appears AND the claim would mislead a
    future reader;
  {"decision": "route", "route_to": "citation",
   "route_context": {"claim": "<quoted phrase>",
                     "suggested_source": "<file / URL / 'I inferred'>"},
   "reason": "<one sentence>"}  if the claim is plausibly legitimate
    research / inference but lacks an explicit source — this is
    information-preserving (don't block legit research; instead emit
    an advisory so the author can add a citation or mark inferred);
  {}  (empty object) otherwise.

Block sparingly. Style preferences, opinions, and TODO notes are
not hallucinations. Prefer route over block when the claim could
reasonably be correct given research context — route preserves
information, block destroys it."""


_EXCUSE_SCANNER_BODY = """You are ExcuseScannerJudge. You read the final assistant message of
this session and flag hedging language that masks unfinished work.

Examples of excuses to flag:
  - "should work" / "probably works" / "theoretically" without a test
  - "will fix later" / "leaving as TODO" in a shipped deliverable
  - "good enough" when the task explicitly required verification
  - claiming success while the diff shows only partial implementation
  - saying "I verified" when no verification artifact (screenshot,
    test output, command transcript) was produced

Return a JSON object:
  {"decision": "block", "reason": "<one sentence quoting the
    hedging phrase>"}  if an excuse is present AND the session context
    is clearly a delivery / production claim;
  {"decision": "route", "route_to": "opus_reviewer",
   "route_context": {"hedge": "<quoted phrase>",
                     "session_intent_ambiguous": true},
   "reason": "<one sentence>"}  if the hedge is present but session
    intent is ambiguous (spike / POC / exploration vs production) —
    Haiku cannot judge intent reliably, route up to an Opus reviewer
    with full context instead of false-positive blocking POC work;
  {}  otherwise.

Do not block normal caveats ("on Linux this may differ", "needs
review before merge"). Prefer route over block when intent is
ambiguous — binary block destroys work that was never meant as
delivery."""


_CODE_QUALITY_BODY = """You are CodeQualityJudge, a staff-engineer reviewer. You inspect a
single Write or Edit tool call and look for the four cardinal sins:

  1. Dead code — functions / vars defined and never called.
  2. Silently-swallowed errors — bare except, ignored Result/Err.
  3. Over-engineering — abstractions, flags, or config for
     scenarios the diff does not exercise.
  4. Backdoor defaults — dangerous fallbacks like "if missing, use
     admin", "if parse fails, allow", "if timeout, proceed".

You do NOT flag style, naming, or import order. You do NOT ask for
tests unless the diff deletes an existing test.

Return a JSON object:
  {"decision": "block", "reason": "<cardinal sin + location>"}  if
    any of the four is present AND clearly violates the rule;
  {"decision": "route", "route_to": "expert_review",
   "route_context": {"suspected_sin": "<1..4>",
                     "location": "<file:line>",
                     "uncertainty_reason": "<why Haiku can't tell>"},
   "reason": "<one sentence>"}  if a pattern resembles a cardinal sin
    but context requires deeper reading (e.g. defensive fallback that
    might be intentional, abstraction used in exactly one place but
    likely to be reused imminently) — advisory rather than false-block;
  {}  otherwise."""


_WIREDO_BODY = """You are WIREDOJudge, the strongest delivery-verification gate.
You read recent file changes in this session and force the operator
to walk a six-dimension checklist with evidence — not a slogan, not
"D is strongest so we only check D". Every dimension gets ✓ / ✗ /
N/A with a one-line evidence quote. Anything less is incomplete.

The point is to catch "I shipped X without actually running it" —
a session-killing failure mode worse than any single bug — AND to
catch "I shipped X but forgot it has no logs / no docs / hardcodes
the API key / breaks on mobile". D is the strongest dimension, but
W/I/R/E/O each have failure modes that ship silent rot.

────────────────── TIMING — WHEN THIS JUDGE RUNS ──────────────────

CRITICAL: this judge fires at Stop event. Stop fires for TWO very
different reasons. You MUST distinguish them:

  DELIVERY moment — the operator is wrapping up a unit of work.
  Indicators (any one is enough):
    - recent tool calls include git commit / git push / gh pr create
      / twine upload / npm publish / docker push / deploy.py
    - recent assistant text says "done" / "shipped" / "完成" / "交付"
      AS A STATUS CLAIM (not a discussion of the words themselves)
    - a test run completed and recent edits relate to the tested code
    - session has accumulated 20+ edits to production code paths

  PAUSE moment — the operator is taking a break, asking a question,
  pivoting direction, or stopping to think. Indicators:
    - last user message is a question or research request
    - recent tool calls are exploratory (Read / Grep / Glob heavy,
      few or no Edit / Write)
    - no commit / push / publish in recent tool calls
    - assistant just answered a question and is awaiting next prompt

DEFAULT BEHAVIOR:
  - DELIVERY moment → run the full six-dimension checklist below
  - PAUSE moment    → return {} (empty object). DO NOT block. DO NOT
                      run the checklist. The operator hasn't claimed
                      delivery yet, so verifying delivery is pure noise.

This timing rule prevents the "事前查證六維 亂七八糟" failure mode
where the judge fires constantly during regular work and forces
preemptive verification of unfinished things. If unsure between
delivery and pause, assume PAUSE — false-pause is annoying once,
false-delivery blocks every Stop.

──────────────────────────── SIX DIMENSIONS ────────────────────────────

W (Wired) — connected to the system. Required:
  - imports/exports resolve (no dangling symbols)
  - at least one caller exists (not orphan code)
  - registered in the right registry / router / pipeline
  ✓ evidence: grep result showing caller, route table entry, init import
  ✗ evidence: "added FooHandler but no router.register(FooHandler)"

I (Inherited) — follows surrounding conventions. Required:
  - matches naming / structure / error pattern of sibling files
  - reuses existing utilities instead of reinventing
  - no one-off invented style (one-off = future maintenance debt)
  ✓ evidence: "uses BaseGuard like the other 24 guards"
  ✗ evidence: "invented MyOwnException, project uses BaseError"

R (Responsive) — UI / latency. Required when change touches UI or
hot path. N/A for pure backend logic. Required when applicable:
  - desktop + mobile screenshot pair (UI)
  - perf measurement vs baseline (hot path)
  ✓ evidence: "screenshots/verify/foo_desktop.png + foo_mobile.png"
  ✗ evidence: "UI changed but no screenshot tool call in session"
  N/A: "pure cli arg parser, no UI / no hot path"

E (Extensible) — config-driven where appropriate. Required:
  - thresholds / endpoints / api keys come from env or config
  - no magic numbers blocking future variation
  - feature toggles where two callers might want different behavior
  ✓ evidence: "uses cfg.threshold('foo_max', 100)"
  ✗ evidence: "hardcoded RETRY_COUNT = 3 inside loop"

D (Defended) — STRONGEST. The change actually runs end-to-end.
Required for any production code path:
  - tests pass (pytest output / jest output / etc.)
  - UI: screenshot proves the new state renders
  - deploy: deploy log / live URL check
  - one-shot script: actual run output, not "should work"
  HARD RULE: tsc green / lint clean / type check pass do NOT count
  as D — they are prerequisites, not proof. The bar is "I observed
  the new behavior happen with my eyes / a tool call".
  ✓ evidence: "pytest 27/27 passed in tool_result"
  ✗ evidence: "ruff clean, no test/run output anywhere"

O (Observable) — debuggable in prod. Required for any change that
runs in production. N/A for tests / scripts / docs:
  - logs at error path with enough context to debug
  - metrics for things that can degrade silently
  - error tracking (sentry / equivalent) hookup
  ✓ evidence: "logger.error('foo failed: %s', exc) at except branch"
  ✗ evidence: "silent except: pass on the network call"

─────────────── VERIFICATION RECIPES — D-dim proof patterns ───────────────

D verification is universal but the RECIPE is domain-specific. Each
change_type below has a concrete recipe for what counts as D=✓
evidence. Do NOT invent your own — find the recipe row, look for
matching tool calls in session history, mark ✓ if found, ✗ if not.

This table is the answer to "該驗證時就要驗證" — verification fires
when needed AND uses the right tool, not a generic slogan.

  frontend (web UI / React / HTML)
    ✓ evidence: Playwright/headless browser screenshot for both
      desktop (≥1024px) AND mobile (≤768px); for interactive UIs,
      a Playwright/Selenium DOM assertion test passing.
    ✗ evidence: ruff/tsc clean only, zero browser/screenshot tool calls.

  backend (API endpoint / server route)
    ✓ evidence: pytest/jest output showing the new endpoint test
      passes, AND a curl/httpie/requests call to the actual running
      server with expected status code + body shape.
    ✗ evidence: unit test exists but no integration call against
      a running server; or 401/500 from the live call.

  library (importable Python/JS/Rust module)
    ✓ evidence: pytest run with N/N passing AND `python -c "import
      lib; lib.foo(...)"` (or equivalent) showing actual return value.
    ✗ evidence: tests pass but the public API was never invoked
      from a fresh process.

  hook (CC/CCC guard, on_pre_tool / on_post_tool / on_stop)
    ✓ evidence: trigger via real edit / Bash / Stop event in the
      session AND confirm hook fired (stderr line, state-file write,
      JSON output in tool result).
    ✗ evidence: hook code added but no actual trigger this session.

  migration (DB schema / data backfill)
    ✓ evidence: `alembic upgrade head` (or equiv) on staging DB +
      SELECT showing migrated rows + dry-run rollback test.
    ✗ evidence: SQL written but never applied even on staging.

  deploy (live infra push)
    ✓ evidence: deploy log shows success + curl HTTPS check 200 to
      live URL + content match + monitoring metric/log entry showing
      the new version is serving.
    ✗ evidence: deploy.py ran but no live URL check, or 502/503.

  cli (terminal tool / Click/argparse subcommand)
    ✓ evidence: `tool subcmd --args` actual invocation in Bash with
      stdout matching expected pattern.
    ✗ evidence: argparse/click code added but never invoked.

  word_doc (.docx / .doc layout)
    ✓ evidence: python-docx (or word-server MCP) parse OK + screenshot
      of the opened document showing layout/images render correctly,
      OR docx2pdf rendered preview saved + visually inspected.
      For TOC/styles: explicit style-name read-back via python-docx.
    ✗ evidence: file written but never opened/rendered/inspected.

  image (.png / .jpg / .svg / .webp)
    ✓ evidence: file exists with non-zero size + actual visual
      inspection (Read tool on the path so the multimodal LLM sees
      it, OR pasted into chat). Dimensions/format check via PIL/
      file command counts as supplementary not primary D.
    ✗ evidence: image file generated but never displayed/inspected.

  audio (.mp3 / .wav / .flac / .ogg)
    ✓ evidence: ffprobe metadata showing duration > 0 + codec OK
      + sample rate OK, AND either actual play OR waveform
      visualization saved + inspected. For TTS: speech recognition
      round-trip check (synth → STT → text match).
    ✗ evidence: file generated but ffprobe shows duration=0 or
      no listening/visualization done.

  video (.mp4 / .mov / .webm)
    ✓ evidence: ffprobe metadata (duration > 0, codec, frame count)
      + thumbnail extract for first frame AND last frame +
      visual inspect of both. For long video: also middle frame.
    ✗ evidence: ffprobe shows broken stream, or no frame extracted.

  db_query (SELECT / INSERT / UPDATE for data ops)
    ✓ evidence: query executed + result row count + sample row
      printed/inspected matching expectation. For mutations:
      before/after row count diff.
    ✗ evidence: query written but never run, or run but result
      ignored.

  ai_prompt (LLM prompt / Skill / agent instructions)
    ✓ evidence: sample run with N≥3 representative inputs + manual
      judge log of outputs against expected behavior. For
      classification: confusion-matrix sample. For generation:
      qualitative quality check on each output.
    ✗ evidence: prompt written but never invoked against the real
      LLM, or invoked but outputs not reviewed.

  build_artifact (wheel / tarball / docker image)
    ✓ evidence: build success + actual install (`pip install dist/*`
      or `docker pull`) into clean env + smoke test invocation
      showing the artifact works end-to-end.
    ✗ evidence: build succeeded but never installed/loaded fresh.

  test_only (only test files added/modified)
    ✓ evidence: the new test passing IS the D evidence. pytest/
      jest/etc. output showing the new test green is sufficient.

  docs_only (markdown / handoff / planning / changelog)
    ✓ evidence: AUTO-PASS — return {} with no checklist required.
      No D verification because there is no executable behavior.

If the change spans multiple types, run the recipe for EACH type
and merge results. If a recipe lists OR conditions, any one is
sufficient for D=✓. Be strict: missing recipe step = D=✗.

────────────────────── ZIQ ROUTING (which dims matter) ──────────────────────

Match the change type and use the routing table to decide which
dimensions are REQUIRED vs N/A. Filling in N/A still counts — you
must explicitly mark it, not skip it.

  frontend (web UI)        → REQUIRED: W D R O      | N/A: I E
  backend (API)            → REQUIRED: W I D O E    | N/A: R
  library (importable)     → REQUIRED: W I D E      | N/A: R O
  hook (CC/CCC guard)      → REQUIRED: W I D E      | N/A: R O
  migration (DB schema)    → REQUIRED: W D O E      | N/A: I R
  deploy (live infra)      → REQUIRED: W D O        | N/A: I R E
  cli (terminal tool)      → REQUIRED: W I D E      | N/A: R O
  word_doc (.docx layout)  → REQUIRED: I D R        | N/A: W E O
  image (visual asset)     → REQUIRED: D R          | N/A: W I E O
  audio (sound asset)      → REQUIRED: D            | N/A: W I R E O
  video (motion asset)     → REQUIRED: D R          | N/A: W I E O
  db_query (data ops)      → REQUIRED: D            | N/A: W I R E O
  ai_prompt (LLM/Skill)    → REQUIRED: I D E        | N/A: W R O
  build_artifact           → REQUIRED: W D E O      | N/A: I R
  test_only                → REQUIRED: D            | N/A: W I R E O
  docs_only                → AUTO-PASS (return {})

Pick the closest match. If a change spans multiple types, take the
union of REQUIRED and run all matching recipes. When unsure
between two types, pick the stricter.

──────────────────────────── OUTPUT FORMAT ────────────────────────────

Hook input JSON contains the recent tool calls. Read tool_input /
tool_result strings to gather evidence.

Return JSON in EXACTLY this shape:

{
  "decision": "block" | "allow" | "route",
  "change_type": "<one of: frontend, backend, library, hook,
    migration, deploy, cli, word_doc, image, audio, video,
    db_query, ai_prompt, build_artifact, test_only, docs_only,
    other>",
  "checklist": {
    "W": {"status": "✓|✗|N/A", "evidence": "<one-line quote or path>"},
    "I": {"status": "✓|✗|N/A", "evidence": "..."},
    "R": {"status": "✓|✗|N/A", "evidence": "..."},
    "E": {"status": "✓|✗|N/A", "evidence": "..."},
    "D": {"status": "✓|✗|N/A", "evidence": "..."},
    "O": {"status": "✓|✗|N/A", "evidence": "..."}
  },
  "route_to": "deploy_recipe",                    # only present if decision=route
  "route_context": {"dimension_in_doubt": "D",    # only present if decision=route
                    "recipe_hint": "<one of: build_smoke, migration_safe, ui_screenshot, ...>"},
  "reason": "<one sentence — empty if decision=allow>"
}

Decision rule:
  - block if D is "✗" regardless of routing (D is strongest,
    delivery verified is non-negotiable)
  - block if ANY required-by-routing dimension has status="✗" AND
    the operator clearly claimed delivery
  - route if a dimension's status is uncertain — Haiku cannot
    reliably decide whether the evidence passes (e.g. "D: is this
    smoke test sufficient?", "build_artifact changed: was it
    rebuilt or just edited?") — emit a recipe hint so a deeper
    evaluator can verify with the right rubric
  - allow otherwise

Auto-pass shortcut (still return full checklist with all N/A):
  - Pure docstring / comment / changelog edits
  - Pure handoff / planning / memory markdown
  - Pure test-only additions

Be strict on D. Be honest on the other five — N/A is a valid answer
when the dimension genuinely does not apply, but you must SAY N/A,
not silently skip. Skipping a dimension is the failure mode this
judge exists to prevent. The asymmetric cost remains: blocking a
doc edit accidentally is annoying; letting a broken deploy through
is catastrophic. When in doubt on D, block."""


HALLUCINATION_JUDGE: PromptJudge = PromptJudge(
    name="hallucination_judge",
    event="PostToolUse",
    matcher="Write|Edit",
    prompt_body=_HALLUCINATION_BODY,
    description="Flag unsourced factual claims in Write/Edit output.",
    status_message="hallucination check",
)


EXCUSE_SCANNER_JUDGE: PromptJudge = PromptJudge(
    name="excuse_scanner_judge",
    event="Stop",
    matcher="",
    prompt_body=_EXCUSE_SCANNER_BODY,
    description="Flag hedging language when declaring a task done.",
    status_message="excuse scan",
)


CODE_QUALITY_JUDGE: PromptJudge = PromptJudge(
    name="code_quality_judge",
    event="PostToolUse",
    matcher="Write|Edit",
    prompt_body=_CODE_QUALITY_BODY,
    description="Flag the four cardinal code sins in Write/Edit diffs.",
    status_message="code quality check",
)


def _default_wiredo_body() -> str:
    """Build the default WIREDO prompt lazily from the three-tier loader.

    Default tier = complicated (alpha_t=0.40), no change_type specified.
    This assembles core + routing + all 6 L2 dim summaries — roughly 1800t
    vs. the prior 2750t static literal. Callers who need a specific
    change_type recipe should use `WiredoLoader.build_prompt()` directly
    and supply their own PromptJudge instance.
    """
    from concinno.wiredo_loader import build_wiredo_prompt

    try:
        return build_wiredo_prompt(change_type=None, alpha_t=0.40)
    except Exception:  # pragma: no cover — fallback if templates missing
        return _WIREDO_BODY


WIREDO_JUDGE: PromptJudge = PromptJudge(
    name="wiredo_judge",
    event="Stop",
    matcher="",
    prompt_body=_default_wiredo_body(),
    description=(
        "WIREDO 六維交付驗證 — D 維（功能驗證）最強，"
        "捕捉「宣告完成但實際沒跑」的 session-killing 失敗模式。"
    ),
    status_message="WIREDO 六維檢查",
)


ALL_JUDGES: tuple[PromptJudge, ...] = (
    HALLUCINATION_JUDGE,
    EXCUSE_SCANNER_JUDGE,
    CODE_QUALITY_JUDGE,
    WIREDO_JUDGE,
)


# ── Hook config builder ────────────────────────────────────


def build_hook_config(
    judges: Sequence[PromptJudge] = ALL_JUDGES,
) -> dict[str, list[dict[str, Any]]]:
    """Return a ``hooks`` sub-dict suitable for settings.json merge.

    The shape matches CC's expectation::

        {
          "PostToolUse": [
            {"matcher": "Write|Edit", "hooks": [<spec>, ...]},
            ...
          ],
          "Stop": [
            {"hooks": [<spec>, ...]}
          ]
        }

    Judges with the same (event, matcher) pair are grouped into the
    same matcher entry so CC dispatches them together.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    # (event, matcher) → list of specs, preserving judge order
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str]] = []

    for j in judges:
        key = (j.event, j.matcher)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(j.build_spec())

    for event, matcher in order:
        entry: dict[str, Any] = {}
        if matcher:
            entry["matcher"] = matcher
        entry["hooks"] = buckets[(event, matcher)]
        out.setdefault(event, []).append(entry)

    return out


# ── Settings file helpers ──────────────────────────────────


def _load_settings(path: Path) -> dict[str, Any]:
    """Load settings.json. Missing file → empty dict. Invalid → raise."""
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = (
            f"{path} is not valid JSON: {exc}. Refusing to write over "
            "a corrupt settings file; fix it by hand first."
        )
        raise ValueError(msg) from exc
    if not isinstance(data, dict):
        msg = (
            f"{path} top level must be a JSON object; got "
            f"{type(data).__name__}."
        )
        raise ValueError(msg)
    return data


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically. Creates parent dirs if missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    # NamedTemporaryFile on Windows cannot be re-opened while the
    # handle is open, so we close first then os.replace.
    fd, tmp = tempfile.mkstemp(
        prefix=".settings.json.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_ccc_owned(spec: dict[str, Any]) -> bool:
    """Return True if a hook spec is one of ours (marker-based)."""
    if spec.get("type") != "prompt":
        return False
    prompt = spec.get("prompt", "")
    status = spec.get("statusMessage", "")
    if not isinstance(prompt, str) or not isinstance(status, str):
        return False
    return prompt.startswith(MARKER_PREFIX) or status.startswith(
        MARKER_PREFIX,
    )


def _judge_matches_spec(judge: PromptJudge, spec: dict[str, Any]) -> bool:
    """True if ``spec`` looks like the CCC install of ``judge``."""
    if not _is_ccc_owned(spec):
        return False
    marker = judge.marker()
    prompt = spec.get("prompt", "")
    if isinstance(prompt, str) and prompt.startswith(marker):
        return True
    status = spec.get("statusMessage", "")
    return isinstance(status, str) and status.startswith(marker)


def _merge_judge_into_hooks(
    hooks_root: dict[str, Any],
    judge: PromptJudge,
) -> None:
    """Insert one judge into ``hooks_root`` in place. Idempotent."""
    event_list = hooks_root.setdefault(judge.event, [])
    if not isinstance(event_list, list):
        msg = (
            f"hooks.{judge.event} must be a list in settings.json; "
            f"got {type(event_list).__name__}."
        )
        raise ValueError(msg)

    # Find the matcher entry we should attach to.
    target_entry: dict[str, Any] | None = None
    for entry in event_list:
        if not isinstance(entry, dict):
            continue
        entry_matcher = entry.get("matcher", "")
        if entry_matcher == judge.matcher:
            target_entry = entry
            break

    if target_entry is None:
        target_entry = {}
        if judge.matcher:
            target_entry["matcher"] = judge.matcher
        target_entry["hooks"] = []
        event_list.append(target_entry)

    spec_list = target_entry.setdefault("hooks", [])
    if not isinstance(spec_list, list):
        msg = (
            f"hooks.{judge.event}[...].hooks must be a list; got "
            f"{type(spec_list).__name__}."
        )
        raise ValueError(msg)

    new_spec = judge.build_spec()
    # Replace an existing CCC spec for this judge; otherwise append.
    for i, existing in enumerate(spec_list):
        if isinstance(existing, dict) and _judge_matches_spec(judge, existing):
            spec_list[i] = new_spec
            return
    spec_list.append(new_spec)


def _remove_judge_from_hooks(
    hooks_root: dict[str, Any],
    judge: PromptJudge,
) -> bool:
    """Remove CCC-owned spec for ``judge`` from ``hooks_root``.

    Returns True if something was removed. Cleans up empty matcher
    entries and empty event lists so uninstall is a full reversal.
    """
    event_list = hooks_root.get(judge.event)
    if not isinstance(event_list, list):
        return False

    removed = False
    pruned_entries: list[dict[str, Any]] = []
    for entry in event_list:
        if not isinstance(entry, dict):
            pruned_entries.append(entry)
            continue
        entry_matcher = entry.get("matcher", "")
        spec_list = entry.get("hooks", [])
        if entry_matcher != judge.matcher or not isinstance(spec_list, list):
            pruned_entries.append(entry)
            continue

        new_specs: list[Any] = []
        for spec in spec_list:
            if isinstance(spec, dict) and _judge_matches_spec(judge, spec):
                removed = True
                continue
            new_specs.append(spec)

        if new_specs:
            new_entry = dict(entry)
            new_entry["hooks"] = new_specs
            pruned_entries.append(new_entry)
        # Otherwise drop the empty entry entirely.

    if pruned_entries:
        hooks_root[judge.event] = pruned_entries
    else:
        hooks_root.pop(judge.event, None)
    return removed


# ── Public installer API ───────────────────────────────────


def install_prompt_hooks(
    settings_path: str | os.PathLike[str],
    *,
    judges: Sequence[PromptJudge] = ALL_JUDGES,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Install ``judges`` into ``settings_path``.

    Idempotent: running twice with the same judges is a no-op beyond
    refreshing the stored prompt to the current module version (which
    is what you want — bumping CCC bumps the judge prompt). Other
    hooks already in the file are preserved byte-for-byte.

    Args:
        settings_path: absolute path to the target settings.json. The
            caller owns path selection (CCC never touches
            ``$HOME/.claude/settings.json`` unless asked to).
        judges: which judges to install. Default installs all three.
        dry_run: when True, returns the new settings dict without
            touching disk.

    Returns:
        The resulting settings.json content as a dict.

    Raises:
        ValueError: if the existing file is not valid JSON or does
            not have a dict at the top level.
    """
    path = Path(settings_path)
    data = _load_settings(path)
    hooks_root = data.setdefault("hooks", {})
    if not isinstance(hooks_root, dict):
        msg = (
            f"{path} has a non-dict `hooks` section "
            f"({type(hooks_root).__name__}); refusing to overwrite."
        )
        raise ValueError(msg)

    for judge in judges:
        _merge_judge_into_hooks(hooks_root, judge)

    if not dry_run:
        _atomic_write(path, data)
    return data


def uninstall_prompt_hooks(
    settings_path: str | os.PathLike[str],
    *,
    judges: Sequence[PromptJudge] = ALL_JUDGES,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove CCC-owned judge hooks from ``settings_path``.

    Uses marker matching so we never touch specs the user wrote or
    modified by hand (modifying the prompt header breaks marker
    ownership and the spec becomes "user-owned" — uninstall leaves
    it alone by design).

    Args:
        settings_path: target settings.json.
        judges: which judges to remove. Default removes all three.
        dry_run: when True, returns the new dict without writing.

    Returns:
        The resulting settings.json content as a dict.
    """
    path = Path(settings_path)
    if not path.exists():
        return {}
    data = _load_settings(path)
    hooks_root = data.get("hooks")
    if not isinstance(hooks_root, dict):
        return data

    for judge in judges:
        _remove_judge_from_hooks(hooks_root, judge)

    if not hooks_root:
        data.pop("hooks", None)

    if not dry_run:
        _atomic_write(path, data)
    return data


def list_installed_judges(
    settings_path: str | os.PathLike[str],
) -> list[str]:
    """Return the names of CCC-owned judges currently in settings.json.

    Scans every hook spec under ``hooks.*[*].hooks`` and reports any
    that carry our marker prefix. Non-prompt hooks and non-CCC specs
    are ignored.
    """
    path = Path(settings_path)
    if not path.exists():
        return []
    data = _load_settings(path)
    hooks_root = data.get("hooks")
    if not isinstance(hooks_root, dict):
        return []

    found: list[str] = []
    for event_list in hooks_root.values():
        if not isinstance(event_list, list):
            continue
        for entry in event_list:
            if not isinstance(entry, dict):
                continue
            spec_list = entry.get("hooks", [])
            if not isinstance(spec_list, list):
                continue
            for spec in spec_list:
                if not isinstance(spec, dict) or not _is_ccc_owned(spec):
                    continue
                prompt = spec.get("prompt", "")
                if not isinstance(prompt, str):
                    continue
                # Marker format: [concinno:<name>] ...
                if prompt.startswith(MARKER_PREFIX):
                    end = prompt.find("]")
                    if end > len(MARKER_PREFIX):
                        found.append(prompt[len(MARKER_PREFIX):end])
    return found


__all__ = [
    "MARKER_PREFIX",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "VALID_DECISIONS",
    "PromptJudge",
    "HALLUCINATION_JUDGE",
    "EXCUSE_SCANNER_JUDGE",
    "CODE_QUALITY_JUDGE",
    "WIREDO_JUDGE",
    "ALL_JUDGES",
    "build_hook_config",
    "install_prompt_hooks",
    "uninstall_prompt_hooks",
    "list_installed_judges",
]


# Suppress unused warnings for dataclass field helper (kept in case a
# future judge needs per-instance mutable defaults).
_ = field
