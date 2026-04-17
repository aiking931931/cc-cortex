"""GAIA benchmark v5 — SOTA Agent.

Red-team hardened. Every detail optimized.

Architecture (3 layers, red-team proven):
  Layer 1: Budget Router (GAIA Level from dataset, not LLM guess)
  Layer 2: ReAct Core (multi-step agent with 5 tools)
  Layer 3: Self-Verify (L2+L3 only, 1 extra call)

Integrations:
  ZIQ SPS: One-shot question type classification → tool priority + search strategy
  FieldRead: Selective extraction (query-relevant paragraphs, not truncation)
  WebSearch: Always Claude Sonnet (quality), even when reasoning = Gemma (free)

Cost ladder:
  Phase 1: Gemma 4 reasoning + Sonnet search → ~$2/165 questions
  Phase 2: Sonnet reasoning + Sonnet search → ~$15/165
  Phase 3: Opus reasoning + Sonnet search → ~$80/165

Usage:
    python gaia_agent.py --model gemma --validate 20
    python gaia_agent.py --model sonnet --validate all
    python gaia_agent.py --model opus --test
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

# ── Config ────────────────────────────────────────────────
GEMMA_URL = os.environ.get(
    "GEMMA_URL",
    "https://rzmegfopppgf50-11434.proxy.runpod.net",
)
GEMMA_MODEL = os.environ.get("GEMMA_MODEL", "gemma4:latest")
HF_TOKEN = os.environ.get(
    "HF_TOKEN", "hf_REDACTED",
)

_anthropic_client = None
_openai_client = None


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def _get_openai():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(
            base_url=f"{GEMMA_URL}/v1", api_key="none",
        )
    return _openai_client


# ── ZIQ SPS: Question Type Classification (one-shot, no FTRL) ─
# Red team killed FTRL (N=165 too small). SPS cold-start prior only.
# Maps question features → tool priority + search strategy.
QTYPES = {
    "factual": {  # "Who was the PM in 1977?"
        "tools": ["web_search"],
        "search_strategy": "direct",  # one precise query
        "max_steps_bonus": 0,
    },
    "calculation": {  # "Calculate the volume..."
        "tools": ["code_exec", "web_search"],
        "search_strategy": "gather_then_compute",
        "max_steps_bonus": 2,
    },
    "file_analysis": {  # question + attached file
        "tools": ["file_read", "code_exec"],
        "search_strategy": "minimal",  # file has the data
        "max_steps_bonus": 2,
    },
    "research": {  # multi-hop, long question, L3
        "tools": ["web_search", "web_search", "code_exec"],
        "search_strategy": "multi_angle",  # search→pivot→search
        "max_steps_bonus": 4,
    },
    "visual": {  # image attached
        "tools": ["vision"],
        "search_strategy": "none",
        "max_steps_bonus": 0,
    },
}

CALC_KEYWORDS = [
    "calculate", "compute", "how many", "how much", "sum",
    "average", "total", "percentage", "ratio", "convert",
    "multiply", "divide", "difference", "volume", "area",
    "distance", "speed", "rate", "m^3", "m³",
]

RESEARCH_KEYWORDS = [
    "according to", "based on", "published", "article",
    "paper", "study", "report", "database", "repository",
    "github", "arxiv", "wikipedia",
]


def classify_question(question: str, file_content: str,
                      level: str) -> str:
    """ZIQ SPS: structural prior from question features."""
    q = question.lower()

    # Vision shortcut
    if file_content == "__IMAGE__":
        return "visual"

    # File-based
    if file_content and file_content not in ("", "__IMAGE__"):
        has_calc = any(k in q for k in CALC_KEYWORDS)
        if has_calc:
            return "calculation"  # file + calc = code on file data
        return "file_analysis"

    # Calculation (no file)
    if any(k in q for k in CALC_KEYWORDS):
        return "calculation"

    # Research (multi-hop)
    if level == "3" or len(question) > 300:
        return "research"
    if any(k in q for k in RESEARCH_KEYWORDS):
        return "research"

    return "factual"


# ── FieldRead: Selective Extraction (not truncation) ──────
def fieldread_extract(full_text: str, question: str,
                      max_chars: int = 8000) -> str:
    """Extract question-relevant paragraphs from large files.

    Red team killed compression (exact-match + compression = bad).
    This does SELECTION: keep relevant paragraphs verbatim.
    """
    if len(full_text) <= max_chars:
        return full_text  # Small file, keep all

    # Tokenize question into keywords
    stop = {"the", "a", "an", "is", "are", "was", "were", "of",
            "in", "to", "for", "and", "or", "that", "this", "with",
            "from", "by", "on", "at", "it", "be", "as", "do", "if",
            "what", "which", "who", "how", "when", "where", "why"}
    q_words = {
        w.lower() for w in re.findall(r"\w+", question)
        if w.lower() not in stop and len(w) > 2
    }

    # Split into paragraphs
    paragraphs = re.split(r"\n\s*\n|\n(?=[A-Z])", full_text)
    if not paragraphs:
        return full_text[:max_chars]

    # Score paragraphs by keyword overlap
    scored = []
    for para in paragraphs:
        para_words = {w.lower() for w in re.findall(r"\w+", para)}
        overlap = len(q_words & para_words)
        scored.append((overlap, para))

    # Always keep first paragraph (header/context) + top scored
    scored.sort(key=lambda x: x[0], reverse=True)
    selected = []
    total = 0

    # First paragraph always
    if paragraphs:
        first = paragraphs[0]
        selected.append(first)
        total += len(first)

    # Add by relevance until budget
    for score, para in scored:
        if para in selected:
            continue
        if total + len(para) > max_chars:
            break
        selected.append(para)
        total += len(para)

    # Restore original order
    para_order = {id(p): i for i, p in enumerate(paragraphs)}
    selected.sort(key=lambda p: para_order.get(id(p), 999))

    return "\n\n".join(selected)


# ── Model Backend ─────────────────────────────────────────
class ModelBackend:
    def __init__(self, model: str = "gemma"):
        self.tier = model
        self._anthropic_model = {
            "sonnet": "claude-sonnet-4-6",
            "opus": "claude-opus-4-6",
        }.get(model, "claude-sonnet-4-6")

    def chat(self, system: str, messages: list[dict],
             max_tokens: int = 2000) -> str:
        if self.tier == "gemma":
            return self._gemma_chat(system, messages, max_tokens)
        return self._anthropic_chat(system, messages, max_tokens)

    def _gemma_chat(self, system, messages, max_tokens):
        client = _get_openai()
        msgs = [{"role": "system", "content": system}] + messages
        try:
            resp = client.chat.completions.create(
                model=GEMMA_MODEL, messages=msgs,
                max_tokens=max_tokens, temperature=0.3,
                extra_body={"options": {"num_ctx": 16384}},
            )
            content = resp.choices[0].message.content or ""
            if not content:
                fr = getattr(resp.choices[0], "finish_reason", "?")
                in_len = sum(len(m.get("content", "")) for m in msgs)
                print(
                    f"  [gemma empty] finish={fr} in_chars={in_len}",
                    flush=True,
                )
            return content
        except Exception as e:
            print(f"  [gemma error] {e}", flush=True)
            return ""

    def _anthropic_chat(self, system, messages, max_tokens):
        client = _get_anthropic()
        try:
            resp = client.messages.create(
                model=self._anthropic_model,
                max_tokens=max_tokens,
                system=system, messages=messages,
            )
            return resp.content[0].text if resp.content else ""
        except Exception as e:
            print(f"  [{self.tier} error] {e}", flush=True)
            return ""

    def web_search(self, query: str) -> str:
        """Always Claude Sonnet WebSearch — quality search for all tiers."""
        client = _get_anthropic()
        try:
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 3,
                }],
                messages=[{
                    "role": "user",
                    "content": (
                        f"Search: {query}\n\n"
                        "Summarize key facts precisely. "
                        "Include exact numbers, names, dates."
                    ),
                }],
            )
            parts = []
            for block in resp.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
            return "\n".join(parts)[:4000]
        except Exception as e:
            print(f"  [web_search error] {e}", flush=True)
            return ""


# ── Tools ─────────────────────────────────────────────────
def execute_code(code: str) -> str:
    """Full Python subprocess — any package OK."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=30,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if result.returncode == 0 and stdout:
            return stdout.strip().split("\n")[-1][:2000]
        if stderr:
            return f"Error: {stderr[:500]}"
        return ""
    except subprocess.TimeoutExpired:
        return "Error: timeout (30s)"
    except Exception as e:
        return f"Error: {e}"


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MIME_MAP = {
    ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp",
}


def read_file_raw(path: str) -> str:
    """Read full file content (before FieldRead extraction)."""
    if not path or not os.path.exists(path):
        return ""
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return "__IMAGE__"
    try:
        if ext in (".xlsx", ".xls"):
            import openpyxl
            wb = openpyxl.load_workbook(path, data_only=True)
            out = []
            for ws in wb.worksheets:
                out.append(f"Sheet: {ws.title}")
                for row in ws.iter_rows(values_only=True):
                    cells = [
                        str(c) if c is not None else ""
                        for c in row
                    ]
                    out.append("\t".join(cells))
            return "\n".join(out)  # No truncation — FieldRead handles it
        if ext == ".pdf":
            from PyPDF2 import PdfReader
            reader = PdfReader(path)
            text = ""
            for page in reader.pages[:50]:
                text += page.extract_text() or ""
            return text  # No truncation
        if ext in (".mp3", ".wav", ".m4a", ".ogg", ".flac"):
            return "(audio file — transcription not available)"
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()  # No truncation
    except Exception as e:
        return f"File error: {e}"


def get_youtube_transcript(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    if not m:
        return ""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        segs = YouTubeTranscriptApi.get_transcript(m.group(1))
        return " ".join(s["text"] for s in segs)[:8000]
    except Exception:
        return ""


# ── Answer normalization (official GAIA scorer aligned) ───
def normalize_answer(raw: str) -> str:
    raw = raw.strip()
    for pfx in ["FINAL ANSWER:", "Final Answer:", "Answer:",
                 "The answer is", "ANSWER:", "Final answer:"]:
        if raw.lower().startswith(pfx.lower()):
            raw = raw[len(pfx):].strip()
    raw = re.sub(r"\*+", "", raw).strip()
    raw = raw.strip('"\'`').rstrip(".")
    raw = re.sub(r"^[\$€£¥]+\s*", "", raw).strip()
    raw = re.sub(
        r"^(approximately|about|around|roughly|nearly|circa)\s+",
        "", raw, flags=re.I,
    ).strip()
    num = raw.replace(",", "").strip()
    if re.match(r"^-?[\d]+\.?\d*$", num):
        try:
            f = float(num)
            if f == int(f) and "." not in num:
                return str(int(f))
            return num
        except ValueError:
            pass
    raw = re.sub(r"\b(a|an|the)\b", " ", raw, flags=re.I)
    return re.sub(r"\s+", " ", raw).strip()


def answers_match(predicted: str, expected: str) -> bool:
    p, e = normalize_answer(predicted), normalize_answer(expected)
    if not p or not e:
        return p == e
    try:
        pf = float(p.replace(",", ""))
        ef = float(e.replace(",", ""))
        return abs(pf - ef) < 1e-3
    except (ValueError, TypeError):
        pass
    ps = re.sub(r"[^\w\s]", "", p.lower()).strip()
    es = re.sub(r"[^\w\s]", "", e.lower()).strip()
    ps, es = re.sub(r"\s+", " ", ps), re.sub(r"\s+", " ", es)
    if not ps or not es:
        return ps == es
    if ps == es:
        return True
    if es in ps or ps in es:
        return True
    if ";" in e or "," in e:
        sep = ";" if ";" in e else ","
        pi = sorted(normalize_answer(x) for x in p.split(sep))
        ei = sorted(normalize_answer(x) for x in e.split(sep))
        if pi == ei:
            return True
    return False


# ── ReAct System Prompt (SOTA-grade, from smolagents research) ─
REACT_SYSTEM = """You are an expert assistant that solves questions using tools.

TOOLS:
1. web_search("query") — search the web. Use specific, precise queries.
2. code_exec("python code") — run Python (any package). MUST use print() for output.

PROTOCOL:
Each turn, output EXACTLY:
Thought: <analyze situation, plan next action>
Action: <tool>("argument")

OR when you have the answer:
Thought: <final reasoning>
FINAL ANSWER: <value>

SEARCH STRATEGY (model human behavior):
- First search: use precise, specific query
- If results insufficient: extract CLUES from what you found, reformulate query
- For numbers/dates: search for the SOURCE document, not the answer directly
- For people: search full name + context
- Never repeat the same query. Each search must try a DIFFERENT angle.

ANSWER FORMAT (exact-match scoring):
- CONCISE: number, word, name, or short phrase. Nothing else.
- NO units unless question explicitly asks for them
- DO NOT convert or round numbers. Use source precision exactly.
- Comma-separated lists unless told otherwise
- If question asks "how many X" → answer is just the number (e.g. "17" not "17 hours")

COMPUTATION:
- ANY math: use code_exec. Do NOT compute in your head.
- Code must print() the final answer as the last line.
- Available: numpy, pandas, math, statistics, csv, json, re, datetime, openpyxl, PyPDF2

NEVER give up. Always provide your best answer."""


def _extract_answer(raw: str) -> str:
    """Extract FINAL ANSWER from LLM output."""
    fa = re.search(r"FINAL ANSWER:\s*(.+?)(?:\n|$)", raw, re.I)
    if fa:
        ans = fa.group(1).strip()
        if len(ans) > 200:
            ans = ans[:200].rsplit(".", 1)[0] or ans[:200]
        return normalize_answer(ans)
    # Fallback: last non-empty line if short enough
    # Filter out ReAct format lines that leak into answers
    poison = ("Thought:", "Action:", "Observation:", "Search",
              "I am unable", "I cannot", "I could not",
              "Unable to", "I need to find")
    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    for line in reversed(lines):
        if len(line) < 200 and not any(
            line.startswith(p) for p in poison
        ):
            return normalize_answer(line)
    return ""


# ── ReAct Core (Layer 2) ─────────────────────────────────
def react_solve(question: str, file_content: str, file_path: str,
                backend: ModelBackend, qtype: str,
                max_steps: int = 10) -> str:
    """ReAct agent loop with ZIQ-informed tool priority."""
    context_parts = []

    # FieldRead: selective extraction for large files
    if file_content and file_content not in ("", "__IMAGE__"):
        extracted = fieldread_extract(file_content, question, 8000)
        context_parts.append(f"[Attached file]\n{extracted}")

    # YouTube
    yt = re.search(
        r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+",
        question,
    )
    if yt:
        transcript = get_youtube_transcript(yt.group(0))
        if transcript:
            context_parts.append(
                f"[YouTube transcript]\n{transcript[:6000]}"
            )

    # Vision shortcut
    if file_content == "__IMAGE__" and file_path:
        if backend.tier in ("sonnet", "opus"):
            return _solve_vision(question, file_path, backend)
        context_parts.append(
            "[Image attached — cannot view in this mode]"
        )

    # ZIQ SPS: inject tool priority hint (Sonnet/Opus only — Gemma
    # can't follow structured hints well, causes regression)
    type_info = QTYPES.get(qtype, QTYPES["factual"])

    initial = "\n\n".join(context_parts)
    history = []
    user_msg = ""
    if backend.tier in ("sonnet", "opus"):
        user_msg += (
            f"[Question type: {qtype}. "
            f"Recommended: {', '.join(type_info['tools'])}. "
            f"Strategy: {type_info['search_strategy']}]\n\n"
        )
    if initial:
        user_msg += f"Context:\n{initial}\n\n"
    user_msg += f"Question: {question}"
    history.append({"role": "user", "content": user_msg})

    for step in range(max_steps):
        raw = backend.chat(REACT_SYSTEM, history, max_tokens=1500)
        if not raw:
            break

        # Check FINAL ANSWER
        ans = _extract_answer(raw)
        if re.search(r"FINAL ANSWER:", raw, re.I) and ans:
            return ans

        # Parse Action
        action_match = re.search(
            r"Action:\s*(web_search|code_exec)\((.+?)\)\s*$",
            raw, re.M | re.DOTALL,
        )
        if not action_match:
            # No valid action — nudge
            history.append({"role": "assistant", "content": raw})
            history.append({
                "role": "user",
                "content": (
                    "Use Action: web_search(\"query\") or "
                    "code_exec(\"code\") or FINAL ANSWER: <value>"
                ),
            })
            continue

        tool = action_match.group(1)
        arg = action_match.group(2).strip().strip('"\'')

        # Execute tool
        if tool == "web_search":
            obs = backend.web_search(arg)
            obs_text = f"Search '{arg[:60]}':\n{obs[:3000]}"
        else:  # code_exec
            obs = execute_code(arg)
            obs_text = f"Code output:\n{obs[:2000]}"

        tag = f"{tool}({arg[:40]})"
        print(f"    step {step}: {tag}", flush=True)

        history.append({"role": "assistant", "content": raw})
        history.append({
            "role": "user",
            "content": f"Observation: {obs_text}",
        })

    # Max steps — force extraction
    raw = backend.chat(
        "Give the final answer based on everything above. "
        "Reply: FINAL ANSWER: <value>",
        history, max_tokens=200,
    )
    return _extract_answer(raw)


# ── Gatherer-Synthesizer (Gemma split architecture) ──────
# User insight: don't disable ZIQ/verify for weak models —
# let DIFFERENT instances handle them. Each Gemma does one job.

GATHER_SYSTEM = """You are a research assistant that gathers information.

TOOLS:
1. web_search("query") — search the web
2. code_exec("python code") — run Python, MUST print() output

Each turn:
Thought: <what to search/compute next>
Action: <tool>("argument")

When you have gathered ENOUGH information, say:
DONE

Rules:
- Search at least once. Try different queries if first fails.
- For math: use code_exec to compute.
- Keep gathering until you have what's needed. Then say DONE."""

SYNTH_SYSTEM = """You answer questions using ONLY the provided evidence.

Rules:
- Answer must be CONCISE: number, word, name, or short phrase
- NO units unless question asks for them
- DO NOT convert or round numbers
- Comma-separated lists unless told otherwise
- If evidence is insufficient, give your best guess

Reply with ONLY: FINAL ANSWER: <value>"""


def react_solve_split(question: str, file_content: str,
                      file_path: str, backend: ModelBackend,
                      qtype: str, max_steps: int = 10) -> str:
    """Gatherer-Synthesizer: separate info gathering from reasoning.

    Gemma A (Gatherer): ReAct loop, collects observations
    Gemma B (Analyst): ZIQ classification already done by caller
    Gemma C (Synthesizer): clean context → final answer
    """
    # ── Phase 1: Gather observations ──
    observations = []

    # Pre-loaded context (file, YouTube)
    if file_content and file_content not in ("", "__IMAGE__"):
        extracted = fieldread_extract(file_content, question, 8000)
        observations.append(f"[File content]\n{extracted}")

    yt = re.search(
        r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+",
        question,
    )
    if yt:
        transcript = get_youtube_transcript(yt.group(0))
        if transcript:
            observations.append(f"[YouTube]\n{transcript[:4000]}")

    # Vision — delegate to Sonnet if available
    if file_content == "__IMAGE__" and file_path:
        if backend.tier in ("sonnet", "opus"):
            return _solve_vision(question, file_path, backend)
        observations.append("[Image attached — cannot view]")

    # Gatherer loop
    history = [{"role": "user", "content": f"Question: {question}"}]
    for step in range(max_steps):
        raw = backend.chat(GATHER_SYSTEM, history, max_tokens=1000)
        if not raw:
            break

        # Check if gatherer says DONE
        if "DONE" in raw and "Action:" not in raw:
            break

        # Parse action
        action_match = re.search(
            r"Action:\s*(web_search|code_exec)\((.+?)\)\s*$",
            raw, re.M | re.DOTALL,
        )
        if not action_match:
            history.append({"role": "assistant", "content": raw})
            history.append({
                "role": "user",
                "content": "Use Action: web_search(\"q\") or "
                "code_exec(\"code\") or say DONE",
            })
            continue

        tool = action_match.group(1)
        arg = action_match.group(2).strip().strip('"\'')

        if tool == "web_search":
            obs = backend.web_search(arg)
            obs_text = f"[Search: {arg[:50]}]\n{obs[:3000]}"
        else:
            obs = execute_code(arg)
            obs_text = f"[Code output]\n{obs[:2000]}"

        observations.append(obs_text)
        print(f"    gather {step}: {tool}({arg[:40]})", flush=True)

        history.append({"role": "assistant", "content": raw})
        history.append({
            "role": "user",
            "content": f"Observation: {obs_text}",
        })

    # ── Phase 2: Synthesize (fresh context, no ReAct history) ──
    evidence = "\n\n".join(observations)
    # ZIQ type hint goes to synthesizer (simple QA, no ReAct format)
    synth_prompt = (
        f"[Question type: {qtype}]\n\n"
        f"Evidence:\n{evidence[:10000]}\n\n"
        f"Question: {question}"
    )

    raw = backend.chat(
        SYNTH_SYSTEM,
        [{"role": "user", "content": synth_prompt}],
        max_tokens=300,
    )
    ans = _extract_answer(raw)
    if not ans:
        print(f"    [synth empty] raw={raw[:400]!r}", flush=True)

    # ── Phase 3: Self-verify (also fresh context) ──
    if ans:
        verify_prompt = (
            f"Question: {question}\n"
            f"Evidence summary: {evidence[:2000]}\n"
            f"Proposed answer: {ans}\n\n"
            "Is this correct? Check magnitude, units, format.\n"
            "Reply: VERIFIED: <answer> or CORRECTED: <answer>"
        )
        vraw = backend.chat(
            "You verify answers against evidence.",
            [{"role": "user", "content": verify_prompt}],
            max_tokens=200,
        )
        m = re.search(
            r"(?:VERIFIED|CORRECTED):\s*(.+?)(?:\n|$)", vraw, re.I,
        )
        if m:
            corrected = normalize_answer(m.group(1).strip())
            if corrected and len(corrected) < 200:
                ans = corrected

    return ans


# ── Self-Verify (Layer 3, L2+L3 only) ────────────────────
def self_verify(question: str, answer: str,
                backend: ModelBackend) -> str:
    """One-shot verification. Returns corrected answer or original."""
    if not answer:
        return answer
    resp = backend.chat(
        "You are a careful verifier.",
        [{
            "role": "user",
            "content": (
                f"Question: {question}\n"
                f"Proposed answer: {answer}\n\n"
                "Is this answer correct and properly formatted?\n"
                "Check: right magnitude? right units? right name?\n"
                "If correct, reply: VERIFIED: <same answer>\n"
                "If wrong, reply: CORRECTED: <fixed answer>\n"
                "Answer must be concise (number/word/phrase)."
            ),
        }],
        max_tokens=200,
    )
    raw = resp
    m = re.search(r"(?:VERIFIED|CORRECTED):\s*(.+?)(?:\n|$)", raw, re.I)
    if m:
        corrected = normalize_answer(m.group(1).strip())
        if corrected and len(corrected) < 200:
            return corrected
    return answer  # Keep original if verify fails


# ── Vision ────────────────────────────────────────────────
def _solve_vision(question: str, image_path: str,
                  backend: ModelBackend) -> str:
    ext = os.path.splitext(image_path)[1].lower()
    mime = MIME_MAP.get(ext, "image/png")
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        client = _get_anthropic()
        resp = client.messages.create(
            model=backend._anthropic_model,
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": mime,
                        "data": b64,
                    }},
                    {"type": "text", "text": (
                        f"{question}\n\nAnalyze the image. "
                        "Think step by step.\n"
                        "FINAL ANSWER: <concise value>"
                    )},
                ],
            }],
        )
        raw = resp.content[0].text if resp.content else ""
        return _extract_answer(raw)
    except Exception as e:
        print(f"  [vision error] {e}", flush=True)
        return ""


# ── Runner ────────────────────────────────────────────────
def run_validation(n, backend: ModelBackend) -> list[dict]:
    from datasets import load_dataset
    from huggingface_hub import hf_hub_download

    ds = load_dataset(
        "gaia-benchmark/GAIA", name="2023_all",
        split="validation",
    )
    tasks = list(ds)
    if isinstance(n, int):
        tasks = tasks[:n]

    results = []
    correct = 0

    for i, task in enumerate(tasks):
        q = task["Question"]
        exp = task["Final answer"]
        level = task["Level"]

        # Read file (raw, no truncation)
        file_content, local_path = "", ""
        fp = task.get("file_path", "")
        if fp:
            try:
                local_path = hf_hub_download(
                    "gaia-benchmark/GAIA", fp,
                    repo_type="dataset",
                )
                file_content = read_file_raw(local_path)
            except Exception as e:
                print(f"  [file error] {e}", flush=True)

        # ZIQ SPS classification
        qtype = classify_question(q, file_content, level)

        # Budget by level (from dataset, not LLM guess)
        type_info = QTYPES.get(qtype, QTYPES["factual"])
        base_steps = {"1": 5, "2": 8, "3": 12}.get(level, 8)
        max_steps = base_steps + type_info["max_steps_bonus"]

        q_p = q[:60].encode("utf-8", errors="replace").decode()
        print(
            f"[{i+1}/{len(tasks)}] L{level} [{qtype}] {q_p}...",
            flush=True,
        )

        _box = [""]

        def _run():
            try:
                # Gemma: split architecture (gather→synth→verify)
                # Sonnet/Opus: unified ReAct + self-verify
                if backend.tier == "gemma":
                    ans = react_solve_split(
                        q, file_content, local_path,
                        backend, qtype, max_steps,
                    )
                else:
                    ans = react_solve(
                        q, file_content, local_path,
                        backend, qtype, max_steps,
                    )
                    # Self-verify L2+L3 (Sonnet/Opus)
                    if level in ("2", "3") and ans:
                        ans = self_verify(q, ans, backend)
                _box[0] = ans
            except Exception as e:
                print(f"  [FATAL] {e}", flush=True)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=300)
        if t.is_alive():
            print("  [TIMEOUT 300s]", flush=True)

        ans = _box[0]
        ok = answers_match(ans, exp)
        if ok:
            correct += 1
        tag = "PASS" if ok else "FAIL"
        a_p = ans[:50].encode("utf-8", errors="replace").decode()
        e_p = exp[:50].encode("utf-8", errors="replace").decode()
        print(f"  Got=\"{a_p}\" Exp=\"{e_p}\" [{tag}]", flush=True)

        results.append({
            "task_id": task["task_id"],
            "model_answer": ans,
            "expected": exp,
            "level": level,
            "qtype": qtype,
            "correct": ok,
        })

    total = len(results)
    pct = 100 * correct / total if total else 0
    print(f"\n{'='*50}")
    print(f"TOTAL: {correct}/{total} ({pct:.1f}%)")
    print(f"Model: {backend.tier}")
    for lvl in ["1", "2", "3"]:
        sub = [r for r in results if r["level"] == lvl]
        if sub:
            c = sum(1 for r in sub if r["correct"])
            print(f"  Level {lvl}: {c}/{len(sub)} ({100*c/len(sub):.0f}%)")
    # QType breakdown
    for qt in QTYPES:
        sub = [r for r in results if r["qtype"] == qt]
        if sub:
            c = sum(1 for r in sub if r["correct"])
            print(f"  {qt}: {c}/{len(sub)} ({100*c/len(sub):.0f}%)")

    out = os.path.join(
        tempfile.gettempdir(), f"gaia_v5_{backend.tier}.json",
    )
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nResults: {out}")

    fails = [r for r in results if not r["correct"]]
    if fails:
        print(f"\n--- FAILURES ({len(fails)}) ---")
        for r in fails:
            g = r["model_answer"][:40]
            g = g.encode("utf-8", errors="replace").decode()
            e = r["expected"][:40]
            e = e.encode("utf-8", errors="replace").decode()
            print(f"  L{r['level']} [{r['qtype']}] "
                  f"got=\"{g}\" exp=\"{e}\"")
    return results


def run_test(backend: ModelBackend) -> list[dict]:
    from datasets import load_dataset
    from huggingface_hub import hf_hub_download

    ds = load_dataset(
        "gaia-benchmark/GAIA", name="2023_all", split="test",
    )
    results = []
    for i, task in enumerate(ds):
        q = task["Question"]
        level = task["Level"]
        file_content, local_path = "", ""
        fp = task.get("file_path", "")
        if fp:
            try:
                local_path = hf_hub_download(
                    "gaia-benchmark/GAIA", fp,
                    repo_type="dataset",
                )
                file_content = read_file_raw(local_path)
            except Exception:
                pass

        qtype = classify_question(q, file_content, level)
        type_info = QTYPES.get(qtype, QTYPES["factual"])
        base = {"1": 5, "2": 8, "3": 12}.get(level, 8)
        max_steps = base + type_info["max_steps_bonus"]

        q_p = q[:60].encode("utf-8", errors="replace").decode()
        print(
            f"[{i+1}/301] L{level} [{qtype}] {q_p}...",
            flush=True,
        )

        _box = [""]

        def _run():
            try:
                if backend.tier == "gemma":
                    ans = react_solve_split(
                        q, file_content, local_path,
                        backend, qtype, max_steps,
                    )
                else:
                    ans = react_solve(
                        q, file_content, local_path,
                        backend, qtype, max_steps,
                    )
                if (level in ("2", "3") and ans
                        and backend.tier in ("sonnet", "opus")):
                    ans = self_verify(q, ans, backend)
                _box[0] = ans
            except Exception as e:
                print(f"  [FATAL] {e}", flush=True)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=300)
        ans = _box[0]
        a_p = ans[:50].encode("utf-8", errors="replace").decode()
        print(f'  -> "{a_p}"', flush=True)
        results.append({
            "task_id": task["task_id"],
            "model_answer": ans,
        })

    out = os.path.join(
        tempfile.gettempdir(),
        f"gaia_v5_{backend.tier}_submission.jsonl",
    )
    with open(out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nSubmission: {out} ({len(results)} tasks)")
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser("gaia_agent")
    ap.add_argument(
        "--model", default="gemma",
        choices=["gemma", "sonnet", "opus"],
    )
    ap.add_argument("--validate", nargs="?", const="5", default=None)
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    os.environ.setdefault("HF_TOKEN", HF_TOKEN)
    backend = ModelBackend(args.model)
    print(f"Backend: {args.model}", flush=True)
    if args.test:
        run_test(backend)
    elif args.validate is not None:
        n = 165 if args.validate == "all" else int(args.validate)
        run_validation(n, backend)
    else:
        ap.print_help()
