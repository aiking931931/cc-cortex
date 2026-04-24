"""Plain-English examples for every FEATURE_META key — surfaced in the
GUI via the ``?`` tooltip so an operator unfamiliar with a switch can
read one concrete scenario instead of deciphering the risk metadata.

Each entry should:
  * Lead with a one-line summary of what the switch does
  * Give one concrete example (real-world CLI / action / outcome)
  * Note the common wrong setting and its symptom, where relevant

Entries are plain strings (Markdown-safe, no HTML). Features not listed
here fall back to the ``description`` field from FEATURE_META.
"""

from __future__ import annotations

EXAMPLES: dict[str, str] = {
    "token_gate": (
        "Blocks the Agent tool from spawning when the session is "
        "running out of context.\n\n"
        "Example: session at 145 000 tokens, you try to spawn another "
        "Explore agent → gate intercepts and asks you to step back "
        "(`mode=step_back_first`) or hard-denies (`mode=hard_deny`).\n\n"
        "Typical wrong setting: turning it off on a long session → "
        "agent spawn succeeds but later tool-calls corrupt the context."
    ),
    "read_first_gate": (
        "Refuses Edit/Write on any file you have not Read in this "
        "session, preventing blind edits.\n\n"
        "Example: you (or the LLM) try `Edit(src/foo.py, …)` without "
        "a prior `Read(src/foo.py)` → gate denies. Fix: Read the file, "
        "then retry. Skip the gate for tiny files by setting "
        "`min_lines=500+`."
    ),
    "agent_cap": (
        "Hard-caps execution-type sub-agents per session to stop the "
        "LLM spamming parallel spawns when stuck.\n\n"
        "Example: `max_spawns=4`. After the 4th Agent tool use the "
        "gate denies; research agents (Explore / Plan / read-only) are "
        "exempt. Raise to 10 for benchmark runs; lower to 2 for "
        "tightly budgeted sessions."
    ),
    "sentinel_gate": (
        "Detects edit loops — the same file touched N times in a row.\n\n"
        "Example: `max_repeats=5`. After 5 edits on `foo.py` the gate "
        "forces a pause so the LLM steps back instead of thrashing. "
        "`lint_exception=True` (default) lets legitimate lint-fix "
        "iterations slip through."
    ),
    "code_guard": (
        "Runs the per-language code-quality checker "
        "(Ruff / Cargo / go vet / tsc) after Write/Edit and blocks on "
        "new errors.\n\n"
        "Example: you Edit a Python file with an unused import → "
        "Ruff flags F401 → guard surfaces the warning and (depending "
        "on mode) hard-denies the follow-up tool until fixed."
    ),
    "structural_guard": (
        "Flags functions that balloon past a sane length so the "
        "agent splits them before they become unmaintainable.\n\n"
        "Example: a 150-line ``def main()`` triggers "
        "``[func_length] main is 150 lines (max 120)``. The guard "
        "surfaces this as a hint, not a deny — quality signal, not "
        "a hard gate."
    ),
    "typescript": (
        "Runs ``tsc --noEmit`` after TS/TSX edits to catch type "
        "errors the agent otherwise misses.\n\n"
        "Example: edit `component.tsx` with a bad prop type → guard "
        "surfaces the TS error so the next turn fixes it rather than "
        "asserting the code compiles.\n\n"
        "Requires: TypeScript installed in the project or globally "
        "(`tsc` on PATH) and a `tsconfig.json` somewhere above the "
        "edited file. No-op when the project is pure JavaScript."
    ),
    "linting": (
        "ESLint integration for JS/JSX/TS/TSX. Same shape as code_guard "
        "but for JavaScript stacks.\n\n"
        "Example: `no-unused-vars` violation after an edit → guard "
        "surfaces it.\n\n"
        "Requires: `eslint` in `node_modules` or globally, plus "
        "an ESLint config (`.eslintrc*`, `eslint.config.js`, or "
        "`package.json::eslintConfig`). Skips cleanly when absent."
    ),
    "handoff_format": (
        "Validates handoff files follow the 7-section shape (Overview, "
        "Constraints, Unresolved, Next step, Recent sessions, History, "
        "Pointers) + max-line budget.\n\n"
        "Example: handoff exceeds 300 lines → guard refuses the write "
        "and asks you to compress."
    ),
    "prompt_guard": (
        "Injects cognitive prompts (CBUA reminders, thinking-depth "
        "nudges) at SessionStart.\n\n"
        "Example: a session starts with Opus 1M selected → prompt_guard "
        "injects budget + ZIQ anchors so the model calibrates depth "
        "to complexity."
    ),
    "handoff_required_guard": (
        "Blocks the Stop event when the session produced ≥N touched "
        "files with no handoff update.\n\n"
        "Example: you edited 5 files this session, try to close → "
        "guard refuses, tells you to write / update the handoff first."
    ),
    "insight_engine": (
        "Surface scheduled-reflection insights in SessionStart injects. "
        "Read-heavy — inject templates are captured once at session "
        "boot.\n\n"
        "Example: last night's Sonnet reflection flagged a repeated "
        "mistake → today's session opens with that insight pinned."
    ),
    "streak_ux": (
        "Celebration toast on milestones (N green test runs, commits, "
        "ship events).\n\n"
        "Example: 10 passing `pytest` runs in a row → subtle streak "
        "toast. Turn off (``enabled=False``) in CI / benchmark modes."
    ),
    "session_summary": (
        "End-of-session visual summary box printed to the terminal.\n\n"
        "Example: session ends → summary shows files touched, commits, "
        "tools used. Turn off in benchmark / automation."
    ),
    "deny_marker": (
        "Prefixes hook deny messages with a visible emoji + colour so "
        "the agent notices the refusal rather than retrying blindly."
    ),
    "token_display": (
        "Injects `[ctx N/800k]` snippets so the LLM sees where it is "
        "in its context budget on every tool call."
    ),
    "boundary_guard": (
        "Enforces the Concinno / CC boundary — CC files must stay out "
        "of Concinno's `src/`; Concinno must not contain personal "
        "paths.\n\n"
        "Example: CC hook trying to import `concinno._Z.…` with >20 "
        "lines of business logic → deny."
    ),
    "publish_scan": (
        "Pre-publish artifact scan — refuses `twine upload` / "
        "`npm publish` if the dist contains credentials / private "
        "paths / uncommitted TODOs."
    ),
    "identity_guard": (
        "Checks Git commit author identity on commit so rotated "
        "credentials / shared accounts don't slip through."
    ),
    "bash_background_gate": (
        "Forces long-running Bash (>30s) through `run_in_background` "
        "so the LLM doesn't block its own turn.\n\n"
        "Example: `pytest -q` (>30s) → suggests / enforces "
        "`run_in_background=true`."
    ),
    "python_c_gate": (
        "Blocks `python -c '…'` blobs larger than a threshold — "
        "forces the LLM to drop them into a file with `Write` "
        "instead so the shell / auditor can see them."
    ),
    "whitepaper_guard": (
        "Guards against unverified whitepaper / benchmark claims in "
        "written artifacts."
    ),
    "clarity_gate": (
        "Hard-gate for document-writing tools — refuses output that "
        "reads like marketing boilerplate."
    ),
    "hijack_gate": (
        "Protects against prompt-injection hijack patterns in "
        "read-back content."
    ),
    "proposal_guard": (
        "Refuses to let the LLM silently ship a proposal-shaped task "
        "(design doc, RFC) without an explicit confirmation step."
    ),
    "ui_verify": (
        "After UI edits (React / HTML / CSS) demand a visual "
        "verification step — screenshot or manual confirm — before "
        "marking the task done."
    ),
    "delivery_gate": (
        "Exits the WIREDO six-dimension check before marking a task "
        "delivered (Wired / Inherited / Responsive / Extensible / "
        "Defended / Observable)."
    ),
    "consecutive_fail_gate": (
        "After N consecutive tool failures force the agent to RAG / "
        "look up docs instead of hammering the same path.\n\n"
        "Example: `max_fails=3`. 3 `pytest` runs red in a row → gate "
        "redirects to knowledge search."
    ),
    "cognitive_anchor": (
        "Session-wide anchoring phrases so the LLM stays on the "
        "original intent when context gets noisy."
    ),
    "design_theory": (
        "Surfaces design-principle nudges (SOLID, Law of Demeter, "
        "etc.) during architecture-shaped tool calls."
    ),
    "butterfly_guard": (
        "Enforces iron-law #1: discovered issues — including "
        "pre-existing — must be handled this session, not deferred.\n\n"
        "Example: during a feature edit you notice a stale comment, "
        "guard reminds you to fix it now, not add it to a TODO."
    ),
    "pipeline_mode": (
        "Toggles the full CBUA guard pipeline (dynamic, default) vs a "
        "pure-prompt pipeline (static) — static disables learning + "
        "all guards."
    ),
    "session_switches": (
        "At SessionStart, surfaces any non-default switch values as a "
        "compact summary so the LLM is aware of user opt-outs before "
        "primacy bias kicks in.\n\n"
        "Example: `top_n=10`. Session opens with `release_auth: "
        "disabled=True (user opt-out)` injected into the first system "
        "prompt."
    ),
    "configure_permissions": (
        "One-shot allowlist bootstrap for `~/.claude/settings.json` "
        "— adds ~100 safe Bash patterns so the operator stops getting "
        "prompted for routine ops (`pytest`, `ruff`, `git status`, "
        "etc.)."
    ),
    "language_enforce": (
        "Injects a language hint on every tool call so the model "
        "writes + thinks in the configured language.\n\n"
        "Example: `language=\"English\"` → model defaults to English "
        "output even if the system prompt is multilingual."
    ),
    "gaia_tool_router": (
        "Route GAIA questions by the Annotator-Metadata Tools field "
        "instead of self-regex heuristic.\n\n"
        "Example: GAIA 076c8171 has Tools=[openpyxl]. Router reads "
        "that field from the dataset and dispatches to the xlsx "
        "pipeline; no need for the agent to self-classify.\n\n"
        "Requires: a GAIA-shaped record with an "
        "`Annotator Metadata.Tools` list available to the caller. "
        "Outside GAIA (other benchmarks / arbitrary agent tasks) "
        "the feature is a no-op and the legacy regex classifier "
        "takes over."
    ),
    "unified_inprocess": (
        "Use one in-process Llama instance for both text and vision "
        "so they share KV cache and skip the HTTP :9000 hop.\n\n"
        "Example: Gemma 4 31B + vision handler running in-process "
        "= single GGUF load, no relay strip bugs.\n\n"
        "Requires: llama-cpp-python installed, local GGUF + mmproj "
        "reachable, GEMMA_UNIFIED_INPROCESS=1 env. No effect on "
        "Anthropic-hosted sonnet/opus or pure-HTTP backends — the "
        "toggle is read only inside `ModelBackend._gemma_chat`."
    ),

    "gemma4_vision": (
        "Enable Gemma 4 native vision (`Gemma4VisionChatHandler`) in "
        "place of the Qwen2.5-VL fallback.\n\n"
        "Example: polygon / staff-notation image → Gemma 4 reads "
        "pixels directly instead of OCR + text-LLM.\n\n"
        "Requires: Gemma 4 GGUF + mmproj loaded (set "
        "GAIA_VISION_HANDLER=Gemma4VisionChatHandler + "
        "GAIA_VISION_MODEL_PATH). No-op when backend.tier is "
        "sonnet/opus (those use Anthropic native vision) or when no "
        "GGUF is wired.\n\n"
        "Typical wrong setting: left on for an Anthropic-only "
        "deployment → wastes a feature-toggle lookup per vision call; "
        "enable only when running local multimodal."
    ),
    "binary_extractor": (
        "Inline-extract tabular attachments (xlsx / csv / tsv) into "
        "the prompt so weak models do not have to invoke tool-use.\n\n"
        "Example: GAIA xlsx attachment → attachment text embedded "
        "directly → Gemma 4 answers without a code_exec call.\n\n"
        "Requires: `openpyxl` for .xlsx, `pandas` for .csv/.tsv. "
        "Install via `pip install concinno[data]` or manually. "
        "Turn OFF when running strong reasoners (Opus / Sonnet) "
        "that handle tool-use reliably — embedding the whole file "
        "wastes tokens for them."
    ),
    "image_upscale_4x": (
        "Auto-upscale small images 4× LANCZOS before vision "
        "inference so fine detail (small noteheads, tiny labels) "
        "lands on enough encoder pixels.\n\n"
        "Example: 120×80 px puzzle image → upscaled to 480×320.\n\n"
        "Requires: Pillow (PIL) installed — already a dependency of "
        "the vision path, so this is effectively always available. "
        "Triggers only when the longer image side is below "
        "`min_side` (default 800 px); bigger images pass through "
        "untouched."
    ),
    "bassclef_wordreverse": (
        "When a question mentions a musical staff (bass clef / "
        "treble clef / notes / staff), inject a GENERIC 4-step "
        "visual-reasoning scaffold before the question: "
        "(1) describe what you see, (2) separate content from "
        "metadata, (3) restate the question in image vocabulary, "
        "(4) reason step by step.\n\n"
        "History: the 2.21–2.23 builds shipped a bass-clef-"
        "specific mnemonic + DECADE reversal + time-unit table — "
        "that was effectively hardcoding the GAIA 8f80e01c "
        "solution into the prompt (test-set leakage). 2.24.0 "
        "replaces it with the generic scaffold; no solution paths "
        "are encoded.\n\n"
        "Requires: the target LLM benefits from explicit step-by-"
        "step scaffolding. Strong reasoners (Opus / Sonnet) gain "
        "little; small / local models see the largest lift. No "
        "model-family dependency."
    ),
    "polygon_counting_hint": (
        "When a question mentions a polygon / edges / sides / "
        "vertices, inject the same GENERIC visual-reasoning "
        "scaffold used by `bassclef_wordreverse` (toggles are kept "
        "separate so users can enable one question family and not "
        "the other).\n\n"
        "History: 2.22–2.23 shipped a polygon-specific "
        "\"walk the boundary\" + \"purple labels are distractors\" "
        "hint — solution leakage; removed in 2.24.0.\n\n"
        "Requires: same as `bassclef_wordreverse`."
    ),
    "ocr_fallback": (
        "Route text-heavy images through OCR + text-LLM reasoning "
        "before vision when OCR yields enough text.\n\n"
        "Example: chart / headstone / document scan → Tesseract → "
        "text model; vision model only if OCR yields ≥ `min_chars` "
        "characters.\n\n"
        "Requires: `pytesseract` Python package + the Tesseract "
        "binary on PATH. No-op when either is missing — caller "
        "falls back to the vision path automatically."
    ),
}


def example_for(name: str) -> str | None:
    """Return the example markdown for ``name`` or ``None`` if none defined."""
    return EXAMPLES.get(name)
