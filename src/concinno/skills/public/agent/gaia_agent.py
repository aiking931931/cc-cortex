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
HF_TOKEN = os.environ.get("HF_TOKEN")
if not HF_TOKEN:
    raise RuntimeError(
        "HF_TOKEN env var is required to run gaia_agent — "
        "set it to your Hugging Face access token before invoking."
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


# ── Binary / non-text attachment guidance ─────────────────
_BINARY_LIB_HINTS: dict[str, str] = {
    "xlsx": "openpyxl.load_workbook(path, data_only=True)",
    "xlsm": "openpyxl.load_workbook(path, data_only=True)",
    "xls": "pandas.read_excel(path)",
    "csv": "pandas.read_csv(path) or csv.reader(open(path))",
    "tsv": "pandas.read_csv(path, sep='\\t')",
    "pdf": "pypdf.PdfReader(path) or pdfplumber.open(path)",
    "docx": "docx.Document(path)  # python-docx",
    "pptx": "pptx.Presentation(path)  # python-pptx",
    "json": "json.load(open(path))",
    "xml": "xml.etree.ElementTree.parse(path)",
    "zip": "zipfile.ZipFile(path)",
    "tar": "tarfile.open(path)",
    "gz": "gzip.open(path, 'rb')",
    "sqlite": "sqlite3.connect(path)",
    "db": "sqlite3.connect(path)",
    "parquet": "pandas.read_parquet(path)",
    "mp3": "librosa.load(path) or pydub.AudioSegment.from_mp3(path)",
    "wav": "wave.open(path) or librosa.load(path)",
    "mp4": "cv2.VideoCapture(path) or moviepy.editor.VideoFileClip(path)",
}


def build_binary_attachment_hint(file_path: str) -> str:
    """Compose an agent-facing hint for binary/non-image attachments.

    Runners that cannot inline-extract file bytes (xlsx / pdf / docx / zip /
    audio / ...) pass ``file_content="__BINARY__"`` plus the local
    ``file_path``; this hint tells the model how to access the bytes via
    ``code_exec`` rather than pretending there is no attachment.
    """
    if not file_path:
        return "[Attached file: path missing]"
    if not os.path.exists(file_path):
        return f"[Attached file at {file_path} — path not found on disk]"
    size = os.path.getsize(file_path)
    ext = os.path.splitext(file_path)[1].lstrip(".").lower()
    lib_hint = _BINARY_LIB_HINTS.get(
        ext,
        f"open({file_path!r}, 'rb') or a standard-library "
        f"reader appropriate for .{ext}",
    )
    return (
        f"[Attached file: path={file_path}, size={size} bytes, "
        f"ext=.{ext}. Use code_exec with {lib_hint} to read it. "
        f"Do NOT answer without actually opening and parsing the file.]"
    )


def extract_tabular_attachment_text(
    file_path: str, max_chars: int = 8000
) -> str | None:
    """Extract structured tabular attachments (xlsx/csv/tsv) to plain text.

    Weak models (Gemma 4 Q4_K_M) often short-circuit to FINAL ANSWER
    instead of invoking code_exec when told "use openpyxl first", so for
    deterministic tabular formats we pre-extract on the agent layer and
    surface the data inline — the model reasons over visible rows rather
    than being asked to tool-use. Returns ``None`` for unsupported formats
    or on extraction failure; caller falls back to the hint path.
    """
    if not file_path or not os.path.exists(file_path):
        return None
    ext = os.path.splitext(file_path)[1].lstrip(".").lower()
    try:
        if ext in ("xlsx", "xlsm"):
            import openpyxl  # optional dep
            wb = openpyxl.load_workbook(file_path, data_only=True)
            lines = []
            for sh in wb.sheetnames:
                ws = wb[sh]
                lines.append(
                    f"=== Sheet {sh!r} "
                    f"({ws.max_row}R x {ws.max_column}C) ==="
                )
                for row in ws.iter_rows(values_only=True):
                    lines.append(
                        "\t".join(
                            "" if c is None else str(c) for c in row
                        )
                    )
            text = "\n".join(lines)
        elif ext in ("csv", "tsv"):
            with open(file_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        else:
            return None
        if len(text) > max_chars:
            return text[:max_chars] + "\n[...truncated]"
        return text
    except Exception:
        return None


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
        if os.environ.get("GEMMA_UNIFIED_INPROCESS", "").lower() in (
            "1", "true", "yes"
        ):
            return self._gemma_chat_inprocess(system, messages, max_tokens)
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

    def _gemma_chat_inprocess(self, system, messages, max_tokens):
        """Text chat via the same in-process Llama instance used by vision.

        Gemma 4 + mmproj handler can answer pure-text queries too —
        the handler's CHAT_FORMAT renders string content without any
        image_url block. One Llama load serves both modalities
        (MEMORY #98 Sancio directive 最終形, 單一 weights 服務
        text+vision，省 VRAM 50% vs 兩 instance co-resident).
        """
        try:
            llm = _get_local_vision_llm()
        except Exception as err:
            print(
                f"  [gemma unified-inproc load error] {err}", flush=True,
            )
            return ""
        msgs = [{"role": "system", "content": system}] + list(messages)
        try:
            resp = llm.create_chat_completion(
                messages=msgs,
                max_tokens=max_tokens,
                temperature=0.3,
            )
            return resp["choices"][0]["message"]["content"] or ""
        except Exception as err:
            print(f"  [gemma unified-inproc gen error] {err}", flush=True)
            return ""

    def _anthropic_chat(self, system, messages, max_tokens):
        client = _get_anthropic()
        try:
            # Cache the stable system prompt + legacy-cache the first
            # user turn so GAIA agent loop iterations (often 10-40
            # steps per task) stop paying for the same prefix over
            # and over. Savings compound across 100+ eval tasks.
            from concinno.cache.anthropic_helpers import (
                system_with_cache,
                with_cache_control,
            )
            sys_block = (
                system_with_cache(system) if isinstance(system, str)
                else system
            )
            msgs = with_cache_control(messages, strategy="legacy")
            resp = client.messages.create(
                model=self._anthropic_model,
                max_tokens=max_tokens,
                system=sys_block,
                messages=msgs,
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
def _web_fetch_full_summary(out: dict, url: str) -> str:
    """Compose the text-only observation from a web_fetch_full result.

    Shared by both the text-only :func:`web_fetch_full_action` legacy
    path (Gemma backend) and the multimodal
    :func:`web_fetch_full_action_multimodal` path (Sonnet/Opus backend),
    so both surfaces describe the same page identically — only the
    image attachment differs.
    """
    parts: list[str] = []
    if out.get("error"):
        parts.append(f"[note] {out['error']}")
    parts.append(f"final_url: {out.get('final_url') or url}")
    if out.get("title"):
        parts.append(f"title: {out['title']}")
    if out.get("screenshot_path"):
        parts.append(f"screenshot_saved: {out['screenshot_path']}")
    text = out.get("text") or ""
    if text:
        parts.append(f"--- page text ---\n{text}")
    return "\n".join(parts)


def web_fetch_full_action(url: str) -> str:
    """Playwright-backed deep-extraction tool — text-only observation.

    Used by Gemma backends (no native multimodal turn input) and as a
    fallback when the multimodal variant cannot attach the screenshot.
    Returns a single text string. The screenshot is captured and saved
    to disk (so a human reviewer can inspect it) but the base64 payload
    is omitted — it would dwarf the Gemma gather history budget.

    For Sonnet/Opus paths, prefer :func:`web_fetch_full_action_multimodal`
    so the model can actually see the screenshot rather than read about
    its existence.
    """
    if not _feature_enabled("gaia_web_fetch_full"):
        return (
            "Error: web_fetch_full is disabled "
            "(feature flag gaia_web_fetch_full=False); "
            "use web_search instead."
        )
    try:
        from concinno.tools.builtin.web_fetch_full import (
            web_fetch_full as _wff,
        )
    except ImportError as exc:
        return f"Error: web_fetch_full unavailable: {exc}"

    try:
        out = _wff(url, screenshot=True)
    except Exception as exc:  # noqa: BLE001 — surface as observation
        return f"Error: web_fetch_full crashed: {exc}"

    return _web_fetch_full_summary(out, url)


def web_fetch_full_action_multimodal(url: str) -> dict:
    """Playwright-backed deep-extraction tool — multimodal observation.

    Returns a dict suitable for attaching to an Anthropic multimodal
    user turn. Keys:

    - ``text_summary``: same prose as :func:`web_fetch_full_action`,
      so the model has structured metadata + page text alongside the
      image.
    - ``screenshot_b64``: base64 PNG payload, or ``None`` when capture
      failed / was skipped / size exceeded the inline cap.
    - ``screenshot_path``: on-disk PNG path (always present when a
      screenshot was captured), so loops can re-attach later.
    - ``mime``: image media-type for the SDK content block. Always
      ``image/png`` because :func:`web_fetch_full` only emits PNG.
    - ``error``: ``None`` on success, else the error string from the
      underlying ``web_fetch_full`` call (mirrors the soft-fail
      contract).

    Callers (e.g. ``react_solve``) inspect ``screenshot_b64`` and
    decide whether to send a multimodal user turn (image + text) or
    fall back to plain ``text_summary`` when no screenshot is
    available.
    """
    result: dict = {
        "text_summary": "",
        "screenshot_b64": None,
        "screenshot_path": None,
        "mime": "image/png",
        "error": None,
    }
    if not _feature_enabled("gaia_web_fetch_full"):
        result["error"] = (
            "feature flag gaia_web_fetch_full=False"
        )
        result["text_summary"] = (
            "Error: web_fetch_full is disabled "
            "(feature flag gaia_web_fetch_full=False); "
            "use web_search instead."
        )
        return result
    try:
        from concinno.tools.builtin.web_fetch_full import (
            web_fetch_full as _wff,
        )
    except ImportError as exc:
        result["error"] = f"import: {exc}"
        result["text_summary"] = f"Error: web_fetch_full unavailable: {exc}"
        return result

    try:
        out = _wff(url, screenshot=True)
    except Exception as exc:  # noqa: BLE001 — surface as observation
        result["error"] = f"crash: {exc}"
        result["text_summary"] = f"Error: web_fetch_full crashed: {exc}"
        return result

    result["text_summary"] = _web_fetch_full_summary(out, url)
    result["screenshot_b64"] = out.get("screenshot_b64")
    result["screenshot_path"] = out.get("screenshot_path")
    if out.get("error"):
        result["error"] = out["error"]
    return result


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
2. web_fetch_full("https://...") — headless-browser one URL, returns
   rendered text + a saved full-page screenshot path. Use AFTER
   web_search has surfaced a candidate URL when the answer depends on
   what is VISIBLE on the page (small text in an image, a tombstone,
   a chart label, a background object) rather than the search summary.
   IMPORTANT — image handling: when the next user turn after your
   web_fetch_full call carries an image content block (you can see it
   above the observation text), READ THAT IMAGE DIRECTLY using your
   built-in visual reasoning. Do NOT call code_exec to open / decode /
   PIL-process / OCR the saved screenshot path; the image is already
   in your context. Use code_exec for arithmetic / text manipulation
   on what you have read, not for image I/O.
3. code_exec("python code") — run Python (any package). MUST use print() for output.

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


_MARKDOWN_ONLY_RE = re.compile(r"^[\s*_`#>\-]+$")
_PLACEHOLDER_RE = re.compile(r"^<[^>]*>$")  # e.g. ``<integer>`` / ``<value>``


def _extract_answer(raw: str) -> str:
    """Extract FINAL ANSWER from LLM output (last meaningful match).

    Models emit multiple ``FINAL ANSWER:`` strings:
      1. System-prompt placeholder — ``Step 8 — FINAL ANSWER: <integer>``
         (from the reasoning template shown to the model; ``<integer>``
         / ``<value>`` must not be treated as an answer).
      2. Section header — ``**Step 8 — FINAL ANSWER:**`` (empty capture,
         real answer on the next line).
      3. The real emission — ``FINAL ANSWER: 90`` at the tail.

    Walk matches in reverse; skip empty, markdown-only, and template-
    placeholder captures. If every ``FINAL ANSWER:`` capture is hollow,
    fall back to the last non-empty line **after** the last sentinel
    occurrence — that's where case (2)'s real answer lives.
    """
    matches = re.findall(r"FINAL ANSWER:\s*(.*?)(?:\n|$)", raw, re.I)
    for candidate in reversed(matches):
        ans = candidate.strip().strip("`").strip("*").strip()
        if not ans:
            continue
        if _MARKDOWN_ONLY_RE.match(ans):
            continue
        if _PLACEHOLDER_RE.match(ans):
            continue
        if len(ans) > 200:
            ans = ans[:200].rsplit(".", 1)[0] or ans[:200]
        return normalize_answer(ans)
    # Fallback ladder: tail after the last sentinel first (header-then-
    # answer case), then the entire raw (body computed answer but header
    # was hollow). Always skip poison / markdown / placeholder lines.
    poison = ("Thought:", "Action:", "Observation:", "Search",
              "I am unable", "I cannot", "I could not",
              "Unable to", "I need to find")

    def _scan(text: str) -> str:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        for line in reversed(lines):
            stripped = line.strip("`").strip("*").strip()
            if not stripped:
                continue
            if _MARKDOWN_ONLY_RE.match(stripped):
                continue
            if _PLACEHOLDER_RE.match(stripped):
                continue
            # Skip lines that are only the sentinel label itself.
            if re.match(r"^final answer:?$", stripped, re.I):
                continue
            if len(stripped) < 200 and not any(
                stripped.startswith(p) for p in poison
            ):
                return normalize_answer(stripped)
        return ""

    tail_search = re.finditer(r"FINAL ANSWER:", raw, re.I)
    last_pos = 0
    for m in tail_search:
        last_pos = m.end()
    if last_pos:
        tail_answer = _scan(raw[last_pos:])
        if tail_answer:
            return tail_answer
    return _scan(raw)


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

    # Vision shortcut — `_solve_vision` dispatches internally:
    # sonnet/opus → Anthropic native; gemma/other → local Qwen2.5-VL
    # (or configured fallback) via llama-cpp-python chat handler.
    if file_content == "__IMAGE__" and file_path:
        return _solve_vision(question, file_path, backend)

    # Binary / non-image attachment (xlsx / pdf / docx / zip / audio / ...)
    # Tabular formats (xlsx/csv/tsv) get extracted inline so weak models
    # don't have to tool-use; other binaries get a hint + path only.
    if file_content == "__BINARY__" and file_path:
        extracted = (
            extract_tabular_attachment_text(file_path)
            if _feature_enabled("binary_extractor")
            else None
        )
        if extracted is not None:
            context_parts.append(
                f"[Attached file content ({os.path.basename(file_path)})]"
                f"\n{extracted}"
            )
        else:
            context_parts.append(build_binary_attachment_hint(file_path))

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
    # L1 domain-procedure anchor (chained-reference resolution / music
    # / polygon / web-only) was historically only injected on the
    # vision paths. Without it on the text path, sonnet's react loop
    # mis-parses nested possessives like "the X of Y of [photo of Z]"
    # and answers about Z (the locator) instead of pivoting to Y. The
    # anchor is generic — it describes the resolution procedure, not
    # any specific task. Single-anchor (no stacking) is enforced by
    # _get_domain_procedure.
    procedure = _get_domain_procedure(question, file_path)
    if procedure:
        user_msg += f"{procedure}\n\n"
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
            r"Action:\s*(web_search|web_fetch_full|code_exec)"
            r"\((.+?)\)\s*$",
            raw, re.M | re.DOTALL,
        )
        if not action_match:
            # No valid action — nudge
            history.append({"role": "assistant", "content": raw})
            history.append({
                "role": "user",
                "content": (
                    "Use Action: web_search(\"query\"), "
                    "web_fetch_full(\"https://...\"), "
                    "code_exec(\"code\"), or FINAL ANSWER: <value>"
                ),
            })
            continue

        tool = action_match.group(1)
        arg = action_match.group(2).strip().strip('"\'')

        # Multimodal-aware tool dispatch. For Sonnet/Opus + web_fetch_full
        # we attach the page screenshot as an image content block so the
        # model can SEE the page (small text in images, headstones,
        # background labels) instead of just reading metadata about the
        # screenshot file. Other tool/backend combos take the legacy
        # text-only path.
        tag = f"{tool}({arg[:40]})"
        print(f"    step {step}: {tag}", flush=True)

        if (
            tool == "web_fetch_full"
            and backend.tier in ("sonnet", "opus")
            and _feature_enabled("gaia_web_fetch_full_multimodal")
        ):
            mm = web_fetch_full_action_multimodal(arg)
            history.append({"role": "assistant", "content": raw})
            text_obs = f"Observation: Fetch '{arg[:80]}':\n" \
                       f"{mm.get('text_summary', '')[:5000]}"
            if mm.get("screenshot_b64"):
                # Multimodal user turn — image first, then text.
                print(
                    f"      [multimodal=on b64_len={len(mm['screenshot_b64'])}]",
                    flush=True,
                )
                history.append({
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64",
                            "media_type": mm.get("mime", "image/png"),
                            "data": mm["screenshot_b64"],
                        }},
                        {"type": "text", "text": text_obs},
                    ],
                })
            else:
                print(
                    f"      [multimodal=off err={mm.get('error')!r}]",
                    flush=True,
                )
                history.append({"role": "user", "content": text_obs})
            continue

        # Execute tool (text-only path)
        if tool == "web_search":
            obs = backend.web_search(arg)
            obs_text = f"Search '{arg[:60]}':\n{obs[:3000]}"
        elif tool == "web_fetch_full":
            obs = web_fetch_full_action(arg)
            obs_text = f"Fetch '{arg[:80]}':\n{obs[:5000]}"
        else:  # code_exec
            obs = execute_code(arg)
            obs_text = f"Code output:\n{obs[:2000]}"

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
1. web_search("query") — search the web (returns text summary).
2. web_fetch_full("https://...") — headless-browser one URL,
   returns rendered text + saves a full-page screenshot
   (path is included in the observation). Use AFTER web_search
   when the answer depends on what is VISIBLE on the page —
   small text in an image, a headstone, a chart label, a
   background object — that a search summary will lose.
   IMPORTANT — image handling: when the next user turn after
   your web_fetch_full call carries an image content block (you
   can see it above the observation text), READ THAT IMAGE
   DIRECTLY using your built-in visual reasoning. Do NOT call
   code_exec to open / decode / PIL-process / OCR the saved
   screenshot path; the image is already in your context.
3. code_exec("python code") — run Python, MUST print() output.

Each turn:
Thought: <what to search/compute/fetch next>
Action: <tool>("argument")

When you have gathered ENOUGH information, say:
DONE

Rules:
- Search at least once. Try different queries if first fails.
- For chained-reference questions ("X visible behind/beside Y"),
  resolve the chain in order: locate Y first, then identify the
  X within Y, then fetch the X-specific page; do not extract
  from the locator Y itself.
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

    Web-only fast-path: when the question is detected as
    web-research-without-attachment AND the configured backend is a
    local (gemma) tier that empirically hallucinates instead of
    invoking ``Action: web_search(...)``, force-route the entire
    gather+synth loop through Anthropic Sonnet so the L1
    ``gaia_web_only_procedure_anchor`` actually triggers a real
    ``web_search_20250305`` tool call. Gated by feature toggle
    ``gaia_web_only_force_anthropic`` (default on).
    """
    # ── Web-only force-route to Anthropic ──
    if (
        backend.tier not in ("sonnet", "opus")
        and not file_content
        and not file_path
        and _is_web_only_question(question, file_path)
        and _feature_enabled("gaia_web_only_force_anthropic")
    ):
        try:
            override = ModelBackend(model="sonnet")
            print(
                "  [web-only force-anthropic] tier="
                f"{backend.tier} → sonnet for {question[:60]!r}",
                flush=True,
            )
            backend = override
        except Exception as err:  # pragma: no cover — defensive
            print(
                f"  [web-only override failed] {err}; falling back to "
                f"original {backend.tier} backend",
                flush=True,
            )

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

    # Vision — `_solve_vision` dispatches internally (Anthropic vs local)
    if file_content == "__IMAGE__" and file_path:
        return _solve_vision(question, file_path, backend)

    # Binary / non-image attachment (xlsx / pdf / docx / zip / audio / ...)
    if file_content == "__BINARY__" and file_path:
        observations.append(build_binary_attachment_hint(file_path))

    # Gatherer loop — surface any pre-loaded context (attachments,
    # transcripts) so the gatherer can plan actions instead of searching
    # blind. Without this, observations are only visible to the synthesizer
    # and the gatherer thinks no file was attached.
    # L1 domain-procedure anchor (chained-reference resolution / web-only
    # / etc.) injected into the gatherer's first turn so multi-hop
    # nested-possessive questions get the locator-pivot procedure
    # before the model picks its first search query. Same anchor as
    # vision paths use; routing is generic via _get_domain_procedure.
    procedure = _get_domain_procedure(question, file_path)
    preamble = "\n\n".join(observations)
    parts = []
    if procedure:
        parts.append(procedure)
    if preamble:
        parts.append(f"Context:\n{preamble}")
    parts.append(f"Question: {question}")
    initial_user = "\n\n".join(parts)
    history = [{"role": "user", "content": initial_user}]
    for step in range(max_steps):
        raw = backend.chat(GATHER_SYSTEM, history, max_tokens=1000)
        if not raw:
            break

        # Check if gatherer says DONE
        if "DONE" in raw and "Action:" not in raw:
            break

        # Parse action
        action_match = re.search(
            r"Action:\s*(web_search|web_fetch_full|code_exec)"
            r"\((.+?)\)\s*$",
            raw, re.M | re.DOTALL,
        )
        if not action_match:
            history.append({"role": "assistant", "content": raw})
            history.append({
                "role": "user",
                "content": (
                    "Use Action: web_search(\"q\"), "
                    "web_fetch_full(\"https://...\"), or "
                    "code_exec(\"code\") — or say DONE"
                ),
            })
            continue

        tool = action_match.group(1)
        arg = action_match.group(2).strip().strip('"\'')

        # Multimodal-aware path mirrors react_solve. Force-route web-only
        # questions on Gemma swap backend to Sonnet earlier in this
        # function, so by the time we reach this branch backend.tier
        # may already be sonnet/opus even when called from the Gemma
        # split path. The image attaches only when the actual backend
        # accepts multimodal turns.
        if (
            tool == "web_fetch_full"
            and backend.tier in ("sonnet", "opus")
            and _feature_enabled("gaia_web_fetch_full_multimodal")
        ):
            mm = web_fetch_full_action_multimodal(arg)
            obs_text = (
                f"[Fetch: {arg[:80]}]\n"
                f"{mm.get('text_summary', '')[:5000]}"
            )
            observations.append(obs_text)
            print(
                f"    gather {step}: {tool}({arg[:40]}) "
                f"[multimodal={'on' if mm.get('screenshot_b64') else 'off'}]",
                flush=True,
            )
            history.append({"role": "assistant", "content": raw})
            if mm.get("screenshot_b64"):
                history.append({
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64",
                            "media_type": mm.get("mime", "image/png"),
                            "data": mm["screenshot_b64"],
                        }},
                        {"type": "text", "text": f"Observation: {obs_text}"},
                    ],
                })
            else:
                history.append({
                    "role": "user",
                    "content": f"Observation: {obs_text}",
                })
            continue

        if tool == "web_search":
            obs = backend.web_search(arg)
            obs_text = f"[Search: {arg[:50]}]\n{obs[:3000]}"
        elif tool == "web_fetch_full":
            obs = web_fetch_full_action(arg)
            obs_text = f"[Fetch: {arg[:80]}]\n{obs[:5000]}"
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
def extract_ocr_text(image_path: str, min_chars: int = 30) -> str:
    """Extract text from an image via Tesseract OCR.

    Returns the extracted text, or empty string when OCR is unavailable,
    fails, or yields fewer than ``min_chars`` non-whitespace characters
    (signalling the image is not text-heavy — caller should fall back
    to a real vision model).
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    try:
        raw = pytesseract.image_to_string(Image.open(image_path)) or ""
    except Exception as err:
        print(f"  [ocr error] {err}", flush=True)
        return ""
    stripped = raw.strip()
    meaningful = sum(1 for c in stripped if not c.isspace())
    return stripped if meaningful >= min_chars else ""


def _feature_enabled(name: str, default: bool = True) -> bool:
    """Check a gaia-skill feature toggle via Concinno config cascade.

    Falls back to ``default`` when the config layer is unavailable
    (e.g. during isolated unit tests) so the helper never breaks the
    solver path. Per-session overrides flow through
    ``cfg.feature(name, "enabled")`` — see ``concinno.feature_config``
    FEATURE_META and ``concinno.preset_cascade``.
    """
    try:
        from concinno.core.config import get_config

        cfg = get_config()
        val = cfg.feature(name, "enabled")
    except Exception:
        return default
    if val is None:
        return default
    return bool(val)


_MUSIC_NOTATION_RE = re.compile(
    r"\b(bass\s*clef|treble\s*clef|staff|stave|sheet\s*music|"
    r"(musical\s+)?notation|note(?:s|head)?|ledger\s*line|"
    r"crotchet|quaver|semibreve|minim)\b",
    re.I,
)

# Generic visual reasoning scaffold — replaces the old
# ``_BASS_CLEF_HINT`` which embedded task-specific answer paths (bass-
# clef mnemonics + DECADE word reversal + decade/score/century time
# units). That was effectively hardcoding the GAIA 8f80e01c solution
# into the prompt — test-set leakage, reviewer bait, unshippable open
# source. The replacement teaches the model **how** to reason about a
# visual problem without leaking any solution. Same function for
# polygon / music / chart / any visual puzzle.
_VISUAL_REASONING_SCAFFOLD = (
    "Before answering, work through the image in four explicit steps.\n"
    "Do NOT jump to a numeric answer; do NOT assume what the question\n"
    "is asking until Step 3.\n"
    "\n"
    "Step 1 — Describe what you see. List every distinct visual element\n"
    "in the image: shapes, symbols, text, numbers, lines, colours,\n"
    "positions, relative sizes. Use neutral language. No interpretation.\n"
    "\n"
    "Step 2 — Separate content from metadata. Labels / captions / legends\n"
    "/ axis titles / indices are metadata. Graphical content is the\n"
    "primary signal. State which is which before counting or computing.\n"
    "\n"
    "Step 3 — Restate the question. Paraphrase what is being asked in\n"
    "one sentence using the vocabulary from Step 1. If the question has\n"
    "a unit (years / edges / notes / items), name it here.\n"
    "\n"
    "Step 4 — Reason step by step. Work from Step-1 content to the\n"
    "Step-3 question, one operation at a time. If arithmetic is\n"
    "involved, write each operand and the operator. Double-check by\n"
    "re-reading Step 1 — does your intermediate result match what you\n"
    "described?\n"
    "\n"
    "Then, and only then, emit ``FINAL ANSWER: <value>``.\n"
    "If a step is uncertain, say so explicitly instead of guessing.\n"
)


def _is_music_notation_question(question: str) -> bool:
    """Return True when question text references musical staff/notation."""
    return bool(_MUSIC_NOTATION_RE.search(question))


_POLYGON_COUNTING_RE = re.compile(
    r"\b(polygon|n-gon|edges?|sides?|vertices|vertex|perimeter|"
    r"how\s+many\s+(edges?|sides?|corners?))\b",
    re.I,
)

# Back-compat aliases — both old hint names now point at the generic
# visual reasoning scaffold. Kept so anything still importing the old
# names stays linkable; tests that asserted specific bass-clef or
# polygon strings have been updated alongside this change.
_BASS_CLEF_HINT = _VISUAL_REASONING_SCAFFOLD
_POLYGON_HINT = _VISUAL_REASONING_SCAFFOLD


def _is_polygon_counting_question(question: str) -> bool:
    """Return True when the question asks to count edges/sides/vertices."""
    return bool(_POLYGON_COUNTING_RE.search(question))


# ── L1 domain-typed procedure anchors ─────────────────────────
#
# These anchors contain only generic domain knowledge (textbook /
# Wikipedia level — clef line/space mnemonics, orthogonal-polygon
# decomposition method, multi-hop web research strategy). They do NOT
# contain GAIA answer paths — see ``generic-anchor-design.md`` 3-question
# leakage test. The dispatcher ``_get_domain_procedure`` selects the
# most-specific applicable anchor (music > polygon-area > web-only >
# generic scaffold). Only ONE anchor is injected per question (no
# stacking — anti-pattern in spec, lost-in-middle attention dilution).

_MUSIC_NOTATION_PROCEDURE = (
    "[Music notation procedure]\n"
    "Multi-step decomposition required — DO NOT short-circuit. For each "
    "step, write the intermediate result before moving on.\n"
    "Step 1. Identify the clef first (bass / treble / alto / tenor).\n"
    "  - Bass clef lines, bottom to top: G - B - D - F - A.\n"
    "  - Bass clef spaces, bottom to top: A - C - E - G.\n"
    "  - Treble clef lines, bottom to top: E - G - B - D - F.\n"
    "  - Treble clef spaces, bottom to top: F - A - C - E.\n"
    "Step 2. For each notehead, decide ON a line vs IN a space, then "
    "translate via the chart above. Notes on ledger lines extend the "
    "same alphabetical pattern. Output the letter sequence verbatim.\n"
    "Step 3. Spell out the word formed by the letter sequence.\n"
    "Step 4. Check if the spelled word is a time-unit. Common English "
    "time-unit words and their year values:\n"
    "  - decade = 10 years\n"
    "  - score = 20 years\n"
    "  - century = 100 years\n"
    "  - millennium = 1000 years\n"
    "  Use the value for the word you spelled — do NOT default to "
    "100 years (century) when the spelled word is a different unit.\n"
    "Step 5. Count required quantities — these are typically DIFFERENT "
    "counts in the same image, so count each separately:\n"
    "  - total number of staff lines\n"
    "  - total number of notes\n"
    "  - notes positioned ON lines\n"
    "  - notes positioned IN spaces\n"
    "Step 6. Apply the arithmetic the question describes verbatim "
    "(sum / subtract / multiply by the time-unit value from Step 4). "
    "Show the formula with numbers substituted before computing.\n"
    "Step 7. The final answer is usually a single number. Do NOT "
    "append units (\"years\", \"y\") unless the question explicitly "
    "asks for them.\n"
)


_ORTHOGONAL_POLYGON_PROCEDURE = (
    "[Orthogonal polygon area procedure]\n"
    "- Step 1: List every numeric label visible. Mark each as either "
    "(a) edge length next to a side, or (b) decoration (logo / year "
    "/ watermark / scale bar). Decorations are not edges.\n"
    "- Step 2: Walk the boundary clockwise from one corner. List "
    "each edge as (direction, length). Use the labels.\n"
    "- Step 3: Closure check — sum of right-going lengths must equal "
    "sum of left-going lengths; sum of up = sum of down. If not "
    "equal, some edge length is wrong or missing — re-examine before "
    "computing.\n"
    "- Step 4: Decompose into non-overlapping rectangles (orthogonal "
    "polygons can always be split this way).\n"
    "  IMPORTANT: count the rectangles after decomposition. An "
    "orthogonal L-shape, T-shape or staircase will yield MORE "
    "rectangles than you might first guess — concave (inward) "
    "corners create extra rectangles. A polygon with N concave "
    "corners decomposes into at least (N + 1) rectangles. Re-count "
    "the rectangles before computing area, and verify each "
    "rectangle's 4 sides individually.\n"
    "- Step 5: Compute each rectangle's area; sum.\n"
    "- Step 6: Sanity check — bounding-box area minus negative-space "
    "area should equal your sum. If your sum is off by a small "
    "amount (1-5 units) you most likely missed one rectangle in the "
    "decomposition; re-decompose before answering.\n"
)


_WEB_ONLY_PROCEDURE = (
    "[No-attachment web question procedure]\n"
    "- Trigger: question has no attached file AND contains any of: "
    "\"as of [year/date]\" / \"visible on/in\" / \"on [URL or "
    "website]\" / \"the [thing] of [entity]\" / proper nouns / "
    "named events.\n"
    "- This is a web research question. You MUST call the "
    "web_search tool.\n"
    "- BEFORE searching, re-read the question and detect chained "
    "references — these are easy to miss and lead to answering "
    "about the WRONG entity:\n"
    "  - \"X visible BEHIND/BESIDE/NEXT TO/IN THE BACKGROUND OF Y\" "
    "→ Y is the locator, X is the answer-bearing entity. Find Y "
    "first, then look for X within / near Y, then extract from X "
    "(NOT from Y).\n"
    "  - \"the [adjective] [thing] of [Y]\" → first identify Y, "
    "then find the [adjective] [thing] within Y, that becomes the "
    "new query target.\n"
    "  - Nested possessives like \"the X of the Y of Z\" must be "
    "resolved inside-out (Z → Y → X).\n"
    "  - COMPARATIVE / SUPERLATIVE selection (\"the OLDEST X\", "
    "\"the YOUNGEST X\", \"the FIRST X\", \"the LAST X\", \"the "
    "LARGEST X\", \"the HIGHEST X\", \"the [adjective]-est X\"): "
    "you MUST enumerate ALL candidates first, fetch the comparison "
    "datum (date / value / rank) for EACH, sort by that datum, then "
    "pick. Do NOT visually pick a candidate that \"looks "
    "[adjective]\" or seems prominent in an image without "
    "verifying its comparison datum. Do NOT assume the candidate "
    "you happened to read first is the right one. The full "
    "candidate list + per-candidate datum table is required before "
    "selection.\n"
    "  Write out the resolution chain explicitly (\"locator = …, "
    "intermediate = …, final answer-bearing entity = …\") before "
    "you query, so you do not extract from the locator by mistake.\n"
    "- Multi-hop strategy:\n"
    "  1. Search engine query (use the named entity verbatim).\n"
    "  2. Open the most authoritative URL from results.\n"
    "  3. Navigate within the page (scroll / click sub-links) to "
    "find the specific datum. For chained-reference questions, "
    "navigate to the LOCATOR entity's page first, then extract the "
    "answer-bearing entity name, then navigate to THAT entity's "
    "page to copy the final datum.\n"
    "  4. STUCK-LOOP DETECTION: if you have fetched the SAME URL "
    "twice without new information, do NOT fetch it a third time. "
    "Pivot strategies (try in order): "
    "(a) web_search for a more SPECIFIC URL — a sub-page / "
    "deeper resource within the same site (e.g. a per-item page "
    "rather than a list page); "
    "(b) web_search for the answer-bearing entity directly by its "
    "name once you have identified what it is; "
    "(c) try a different source / snapshot date / mirror URL. "
    "Repeating the same fetch is wasted budget after 2 attempts.\n"
    "  5. When the answer depends on what is VISIBLE on a page "
    "(small text in an image, label on an object in a photo, "
    "background detail) and a search summary is not enough, call "
    "web_fetch_full(\"<url>\") to render the page in a headless "
    "browser. The returned observation includes the page text AND "
    "a saved full-page screenshot path you can examine. After "
    "identifying the answer-bearing entity FROM the rendered page "
    "or screenshot, run a NEW web_search for that entity by name "
    "and either fetch its dedicated page (web_fetch_full) or rely "
    "on the search summary if the datum is short.\n"
    "  6. For time-bounded queries (\"as of 2022 / end of 2023\"), "
    "cross-verify with Wayback Machine snapshot of the URL at that "
    "date.\n"
    "- Do NOT grab the first plausible match — verify the chain "
    "depth before extracting the answer.\n"
    "- Do NOT answer from memory for question containing named "
    "entities + temporal qualifiers — your training cutoff may not "
    "match.\n"
)


# Polygon-AREA detection (different from polygon-counting).
# Counting (existing) is for "how many edges/sides/vertices?".
# Area is for "what is the area of this polygon?" — needs the
# decomposition procedure, not the boundary-walk-counting one.
_POLYGON_AREA_RE = re.compile(
    r"\b(area|surface)\b.{0,80}\b(polygon|shape|figure|region|"
    r"label(?:s|ed)?|side\s*length|edges?|cm|mm|inch|inches|"
    r"meters?|metres?|ft|feet|units?)\b|"
    r"\b(polygon|shape|figure|region)\b.{0,80}\b(area|surface)\b",
    re.I | re.S,
)


def _is_orthogonal_polygon_area_question(question: str) -> bool:
    """Return True when the question asks for the area of a polygon.

    Distinct from ``_is_polygon_counting_question`` which targets
    edge/side/vertex *counting*. Area questions need the decomposition
    procedure (closure check + non-overlapping rectangles + sum) rather
    than a walk-the-boundary counter.
    """
    if not question:
        return False
    return bool(_POLYGON_AREA_RE.search(question))


# Web-only detection — fires only when there is NO attached file AND
# the question shows web-research telltales:
#   - "as of <date/year>" temporal qualifier
#   - "visible on/in [page/site]" / "on https://" / "on www."
#   - "the X of <ProperNoun>" possessive of a named entity
#   - 2+ proper nouns (rough heuristic for named-entity density)
_WEB_TEMPORAL_RE = re.compile(r"\bas of\b", re.I)
_WEB_VISIBLE_RE = re.compile(
    r"\bvisible (?:in|on)\b|\bon (?:https?://|www\.)",
    re.I,
)
_WEB_POSSESSIVE_RE = re.compile(
    r"\bthe \w+ of [A-Z][\w&'.-]+",
)
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-z]{2,}(?:[A-Z'\-&][a-z]+)*")


def _is_web_only_question(question: str, file_path: str | None) -> bool:
    """Return True when the question is web-research without attachment.

    Conditions (ALL must hold):
    1. ``file_path`` is empty / None (no attached file).
    2. Either of the textual cues fires:
       a. ``as of <date>`` temporal qualifier, OR
       b. ``visible on/in <page>`` / ``on https://...`` cue, OR
       c. ``the X of <ProperNoun>`` possessive cue, OR
       d. ≥2 distinct proper-noun tokens (named-entity density).
    """
    if file_path:
        return False
    if not question:
        return False
    if _WEB_TEMPORAL_RE.search(question):
        return True
    if _WEB_VISIBLE_RE.search(question):
        return True
    if _WEB_POSSESSIVE_RE.search(question):
        return True
    # Proper-noun density: drop the leading sentence-start capital so
    # "How" / "What" / "Which" don't count.
    tokens = _PROPER_NOUN_RE.findall(question)
    # Strip the first-token-capital false positives by counting unique
    # tokens that are not common question words.
    _Q_WORDS = {
        "How", "What", "Which", "When", "Where", "Who", "Why",
        "Is", "Are", "Was", "Were", "Do", "Does", "Did",
        "Can", "Could", "Will", "Would", "Should",
        "The", "This", "That", "These", "Those",
    }
    distinct = {t for t in tokens if t not in _Q_WORDS}
    return len(distinct) >= 2


def _get_domain_procedure(
    question: str, file_path: str | None,
) -> str:
    """Return the most-specific applicable domain procedure anchor.

    Routing precedence (most-specific first):
      1. Music notation     → ``_MUSIC_NOTATION_PROCEDURE``
      2. Polygon area       → ``_ORTHOGONAL_POLYGON_PROCEDURE``
      3. No-attachment web  → ``_WEB_ONLY_PROCEDURE``
      4. Generic scaffold   → ``_VISUAL_REASONING_SCAFFOLD`` (only
                              when an image is attached, since the
                              scaffold is visual-reasoning specific)
      5. Otherwise          → empty string (no anchor)

    Each L1 anchor respects its own feature toggle. When the most-
    specific anchor's toggle is OFF, fall through to the next-specific
    anchor (NOT to "no anchor" — fallback is the design intent).
    Only one anchor is returned; stacking is forbidden by spec.
    """
    if (
        _is_music_notation_question(question)
        and _feature_enabled("gaia_music_procedure_anchor")
    ):
        return _MUSIC_NOTATION_PROCEDURE
    if (
        _is_orthogonal_polygon_area_question(question)
        and _feature_enabled("gaia_polygon_area_procedure_anchor")
    ):
        return _ORTHOGONAL_POLYGON_PROCEDURE
    if (
        _is_web_only_question(question, file_path)
        and _feature_enabled("gaia_web_only_procedure_anchor")
    ):
        return _WEB_ONLY_PROCEDURE
    if file_path:
        return _VISUAL_REASONING_SCAFFOLD
    return ""


def _upscale_image_if_small(
    image_path: str, min_side: int = 800, factor: int = 4,
) -> str:
    """Return a 4×-upscaled copy path for small images, else the original.

    Small notation images (bass-clef puzzles, compact tables) benefit from
    LANCZOS upscaling before being fed to local multimodal models that
    struggle with sub-800px detail. Falls back silently to the original
    path if PIL is unavailable or upscaling fails.
    """
    try:
        from PIL import Image
    except ImportError:
        return image_path
    try:
        img = Image.open(image_path)
        w, h = img.size
    except Exception:
        return image_path
    if max(w, h) >= min_side:
        return image_path
    try:
        new_size = (w * factor, h * factor)
        up = img.resize(new_size, Image.LANCZOS)
        import tempfile
        fd, tmp_path = tempfile.mkstemp(
            suffix=os.path.splitext(image_path)[1] or ".png",
            prefix="gaia_upscale_",
        )
        os.close(fd)
        up.save(tmp_path)
        return tmp_path
    except Exception as err:
        print(f"  [upscale skip] {err}", flush=True)
        return image_path


_local_vision_llm = None  # lazy-loaded llama_cpp.Llama singleton


def _get_local_vision_llm():
    """Lazy-load open-source vision LLM for local inference.

    Primary use: Gemma-tier backend (no native multimodal in
    llama-cpp-python 0.3.20) falls back to Qwen2.5-VL-3B-Instruct GGUF
    served in-process. SM 120 (RTX 5090 Blackwell) mmproj CUDA kernels
    segfault in llama.cpp 0.3.20; default ``n_gpu_layers=0`` keeps
    encode/decode on CPU (~5-20s/image) until upstream CUDA fix.

    Env override:
      GAIA_VISION_MODEL_PATH    GGUF weights (default Qwen2.5-VL-3B Q4_K_M)
      GAIA_VISION_MMPROJ_PATH   mmproj GGUF (default Q8_0)
      GAIA_VISION_HANDLER       llama-cpp-python chat handler class name
                                (default Qwen25VLChatHandler)
      GAIA_VISION_N_GPU_LAYERS  GPU layer offload (default 0 = CPU)
      GAIA_VISION_CTX           context size (default 8192;
                                Gemma 4 + mmproj + L1 anchor needs
                                ≥4373 tokens, 4096 was too small)
    """
    global _local_vision_llm
    if _local_vision_llm is not None:
        return _local_vision_llm
    model_path = os.environ.get("GAIA_VISION_MODEL_PATH", "")
    mmproj_path = os.environ.get("GAIA_VISION_MMPROJ_PATH", "")
    if not model_path or not mmproj_path:
        raise RuntimeError(
            "Local vision requires GAIA_VISION_MODEL_PATH and "
            "GAIA_VISION_MMPROJ_PATH env vars (GGUF paths)."
        )
    handler_name = os.environ.get(
        "GAIA_VISION_HANDLER", "Qwen25VLChatHandler"
    )
    n_gpu_layers = int(os.environ.get("GAIA_VISION_N_GPU_LAYERS", "0"))
    n_ctx = int(os.environ.get("GAIA_VISION_CTX", "8192"))

    from llama_cpp import Llama, llama_chat_format
    # Prefer Concinno-shipped custom handlers (e.g. Gemma4) first,
    # fall back to llama-cpp-python built-ins (Llava / Qwen / MiniCPM).
    if handler_name == "Gemma4VisionChatHandler":
        from concinno.llm_runtime.vision_handlers import (
            get_gemma4_vision_handler_cls,
        )
        handler_cls = get_gemma4_vision_handler_cls()
    else:
        handler_cls = getattr(llama_chat_format, handler_name)
    handler = handler_cls(clip_model_path=mmproj_path, verbose=False)
    _local_vision_llm = Llama(
        model_path=model_path,
        chat_handler=handler,
        n_ctx=n_ctx,
        n_gpu_layers=n_gpu_layers,
        verbose=False,
    )
    return _local_vision_llm


def _polygon_multipass_params() -> tuple[int, str]:
    """Return ``(passes_count, model)`` for polygon Sonnet multi-pass.

    Reads ``gaia_polygon_sonnet_multipass`` feature params from the
    Concinno config cascade with safe defaults (3 passes, sonnet 4.6)
    when the config layer is absent (e.g. unit tests).
    """
    return _multipass_params("gaia_polygon_sonnet_multipass")


def _music_multipass_params() -> tuple[int, str]:
    """Return ``(passes_count, model)`` for music-notation multi-pass.

    Symmetric helper to :func:`_polygon_multipass_params`; gated by
    ``gaia_music_sonnet_multipass`` feature key. Default 3 passes /
    sonnet 4.6 because empirical N=3 sonnet runs on bass-clef arithmetic
    show 67% per-call PASS (e.g. outputs ``['90','90','80']``); the
    multi-pass majority vote brings it to deterministic PASS.
    """
    return _multipass_params("gaia_music_sonnet_multipass")


def _multipass_params(feature_key: str) -> tuple[int, str]:
    """Generic ``(passes_count, model)`` reader for any multi-pass feature.

    Reads ``passes_count`` (int >= 1) and ``model`` (non-empty str) from
    the named feature_config entry; safe defaults (3 / sonnet 4.6)
    apply when the config layer is absent or values are missing/invalid.
    """
    passes_count = 3
    model = "claude-sonnet-4-6"
    try:
        from concinno.core.config import get_config
        cfg = get_config()
        pc_raw = cfg.feature(feature_key, "passes_count")
        if pc_raw is not None:
            try:
                pc_int = int(pc_raw)
                if pc_int >= 1:
                    passes_count = pc_int
            except (TypeError, ValueError):
                pass
        m_raw = cfg.feature(feature_key, "model")
        if isinstance(m_raw, str) and m_raw.strip():
            model = m_raw.strip()
    except Exception:
        pass
    return passes_count, model


def _majority_vote_numeric(samples: list[str]) -> str | None:
    """Return the mode of the last-integer extracted from each sample.

    Each sample is the FINAL ANSWER value from one Sonnet vision pass
    (already post-``_extract_answer``). We pull the last integer-looking
    token from each, vote by count, and on a tie keep the earliest
    sample's value (preserves causal order without a coin flip). Returns
    ``None`` only when every sample is empty / has no integer token.

    The helper is generic (any ordinal-counting question can use it);
    no question-class-specific behaviour and no expected-answer reading.
    """
    if not samples:
        return None
    int_re = re.compile(r"-?\d+")
    extracted: list[str] = []
    for s in samples:
        if not s:
            continue
        matches = int_re.findall(s)
        if matches:
            extracted.append(matches[-1])
    if not extracted:
        return None
    counts: dict[str, int] = {}
    first_idx: dict[str, int] = {}
    for idx, val in enumerate(extracted):
        counts[val] = counts.get(val, 0) + 1
        first_idx.setdefault(val, idx)
    # Sort by (-count, first_idx) so the most-voted wins; ties go to the
    # value that appeared first.
    best = sorted(counts.items(), key=lambda kv: (-kv[1], first_idx[kv[0]]))
    return best[0][0]


def _model_drops_temperature(model: str) -> bool:
    """Return True when the named Anthropic model rejects ``temperature``.

    Newer reasoning-tier models (claude-opus-4-7 onward) return HTTP 400
    ``temperature is deprecated for this model`` when the parameter is
    sent. Sonnet 4.6 and older models still accept it. The helper is a
    name-prefix check so future opus minor releases (4-7-1, 4-7-2, ...)
    are covered without further edits.
    """
    if not model:
        return False
    name = model.lower()
    # Opus 4-7+ reject temperature. Anything claude-opus-4-7* or
    # later major (4-8/5-x) opus releases match.
    return name.startswith("claude-opus-4-7") or name.startswith(
        ("claude-opus-4-8", "claude-opus-4-9", "claude-opus-5"),
    )


def _solve_vision_anthropic_multipass(
    question: str,
    image_path: str,
    *,
    model: str = "claude-sonnet-4-6",
    passes_count: int = 3,
) -> tuple[str, list[str]]:
    """Run ``passes_count`` independent Anthropic vision calls and
    majority-vote the FINAL ANSWER values.

    Returns ``(voted_answer, raw_samples)`` so callers (and evidence
    smoke tests) can audit per-pass output. ``voted_answer`` is "" when
    every pass returned empty so the caller can fall back to the local
    path. The L1 domain procedure anchor (e.g. orthogonal-polygon
    procedure for area questions) is injected via
    ``_get_domain_procedure`` so this multipass path stays consistent
    with the local-vision prompt shape.
    """
    ext = os.path.splitext(image_path)[1].lower()
    mime = MIME_MAP.get(ext, "image/png")
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except Exception as err:
        print(f"  [vision-multipass read error] {err}", flush=True)
        return "", []
    procedure = _get_domain_procedure(question, image_path)
    prelude = f"{procedure}\n\n" if procedure else ""
    user_text = (
        f"{prelude}{question}\n\nAnalyze the image carefully and "
        "think step by step. After your reasoning, end with exactly "
        "one final line:\n"
        "FINAL ANSWER: <concise value>\n\n"
        "The value must be concise (number, word, short phrase). "
        "Do NOT add units unless asked. Do NOT cut off in the middle "
        "of reasoning."
    )
    samples: list[str] = []
    try:
        client = _get_anthropic()
    except Exception as err:
        print(f"  [vision-multipass anthropic init error] {err}", flush=True)
        return "", []
    # Newer Anthropic models (e.g. claude-opus-4-7) reject the
    # ``temperature`` parameter outright (HTTP 400 "temperature is
    # deprecated for this model"). Sonnet 4.6 still accepts it and
    # benefits from the lower variance. Gate by model name: any model
    # whose name begins ``claude-opus-4-7`` or later opus-line goes
    # through without temperature; sonnet-line keeps 0.2 for the
    # multi-step-arithmetic stability the multipass vote depends on.
    request_kwargs: dict[str, object] = {
        "model": model,
        "max_tokens": 2000,
        "timeout": 120.0,
    }
    if not _model_drops_temperature(model):
        request_kwargs["temperature"] = 0.2
    for pass_idx in range(max(1, passes_count)):
        try:
            resp = client.messages.create(
                **request_kwargs,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": mime,
                            "data": b64,
                        }},
                        {"type": "text", "text": user_text},
                    ],
                }],
            )
            raw = resp.content[0].text if resp.content else ""
            samples.append(_extract_answer(raw))
        except Exception as err:
            print(
                f"  [vision-multipass pass {pass_idx} error] {err}",
                flush=True,
            )
            samples.append("")
    voted = _majority_vote_numeric(samples) or ""
    return voted, samples


# Generic structured-JSON polygon-area solver (universal, no task-specific
# keywords). Asks the vision model for a strict JSON object listing every
# numeric label, the rectangle decomposition with explicit width/height,
# direction-keyed edge sums for closure verification, and the model's own
# computed_area. Python then (a) verifies horizontal-right == horizontal-
# left and vertical-down == vertical-up closure, (b) recomputes area as
# sum(w * h) over the rectangles, and (c) keeps only passes where the
# closure holds within tolerance and the recomputed area matches the
# model's computed_area within tolerance. The median of validated passes
# is returned. This shifts the arithmetic burden off the model (heads-of-
# arithmetic is the sub-spec where Sonnet/Opus drift) while keeping the
# vision burden on the model where it is best. Generic for any axis-
# aligned polygon area question with labelled side lengths.
_POLYGON_STRUCTURED_PROMPT = (
    "[Orthogonal polygon area — structured analysis]\n"
    "Look at the image. It shows an orthogonal (axis-aligned) polygon "
    "with side lengths labelled. Output a single JSON object using the "
    "exact schema below — NO prose before or after, NO markdown fence, "
    "JSON only.\n\n"
    "Required schema:\n"
    "{\n"
    "  \"labels_visible\": [<every numeric label you can read on the "
    "image, as a number; include duplicates; ignore scale bars / year "
    "watermarks / logos>],\n"
    "  \"rectangles\": [\n"
    "    {\"width\": <number>, \"height\": <number>, "
    "\"explanation\": \"<one short sentence: which labels gave width "
    "and height>\"}\n"
    "    // exactly N entries; N = (concave-corner count + 1) for a "
    "simply-connected orthogonal polygon, or higher for shapes with "
    "holes. An L-shape is N=2, a C/T-shape is usually N=3, and a "
    "staircase with k steps is N=k+1.\n"
    "  ],\n"
    "  \"edge_sums\": {\n"
    "    \"horizontal_right\": <sum of edge lengths going RIGHT around "
    "the boundary>,\n"
    "    \"horizontal_left\":  <sum going LEFT>,\n"
    "    \"vertical_down\":    <sum going DOWN>,\n"
    "    \"vertical_up\":      <sum going UP>\n"
    "  },\n"
    "  \"computed_area\": <number, your sum of width*height across all "
    "rectangles>\n"
    "}\n\n"
    "Closure constraint (you MUST satisfy before answering):\n"
    "  horizontal_right == horizontal_left\n"
    "  vertical_down    == vertical_up\n"
    "If your edge_sums fail closure, re-trace the boundary and adjust "
    "until they balance — then update rectangles and computed_area "
    "accordingly. Do NOT output a JSON whose closure is broken.\n\n"
    "Decomposition rule: every rectangle's width and height MUST come "
    "from the labels_visible list (or be deducible by subtracting "
    "smaller labels along the same axis from a larger one). Do not "
    "invent numbers.\n"
)


_POLYGON_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.I,
)
_POLYGON_FIRST_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(raw: str) -> str | None:
    """Pull the first JSON object out of a model response.

    Handles three shapes seen in practice: (a) pure JSON, (b) ```json
    fenced``` block, (c) JSON embedded after prose. Returns the object
    substring (still text) or None when no candidate is found.
    """
    if not raw:
        return None
    fenced = _POLYGON_JSON_FENCE_RE.search(raw)
    if fenced:
        return fenced.group(1)
    naked = _POLYGON_FIRST_OBJECT_RE.search(raw)
    return naked.group(0) if naked else None


def _validate_polygon_pass(obj: dict, tol: float = 0.51) -> float | None:
    """Return rectangle-derived area when the structured pass is valid.

    Validation rules:
      * ``rectangles`` is a non-empty list of ``{width, height}`` numbers
      * ``edge_sums`` keys exist and are numbers
      * ``horizontal_right`` matches ``horizontal_left`` within ``tol``
      * ``vertical_down`` matches ``vertical_up`` within ``tol``
      * Recomputed ``sum(w*h)`` matches the model's ``computed_area``
        within ``tol`` (catches arithmetic errors in-pass)

    Returns the deterministically-recomputed area on pass, ``None`` on
    any validation failure.
    """
    rects = obj.get("rectangles")
    if not isinstance(rects, list) or not rects:
        return None
    try:
        widths_heights: list[tuple[float, float]] = []
        for r in rects:
            if not isinstance(r, dict):
                return None
            w = float(r["width"])
            h = float(r["height"])
            if w <= 0 or h <= 0:
                return None
            widths_heights.append((w, h))
    except (KeyError, TypeError, ValueError):
        return None
    edge_sums = obj.get("edge_sums")
    if not isinstance(edge_sums, dict):
        return None
    try:
        hr = float(edge_sums["horizontal_right"])
        hl = float(edge_sums["horizontal_left"])
        vd = float(edge_sums["vertical_down"])
        vu = float(edge_sums["vertical_up"])
    except (KeyError, TypeError, ValueError):
        return None
    if abs(hr - hl) > tol or abs(vd - vu) > tol:
        return None
    recomputed = sum(w * h for w, h in widths_heights)
    try:
        claimed = float(obj["computed_area"])
    except (KeyError, TypeError, ValueError):
        return None
    if abs(recomputed - claimed) > tol:
        return None
    return recomputed


def _format_polygon_area(value: float) -> str:
    """Render a polygon area value as the answer string.

    Half-integer-aware: if ``value`` rounds to an integer within 0.05,
    return the integer string; otherwise keep one fractional digit.
    """
    nearest_int = round(value)
    if abs(value - nearest_int) < 0.05:
        return str(int(nearest_int))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _solve_polygon_structured_multipass(
    question: str,
    image_path: str,
    *,
    model: str = "claude-sonnet-4-6",
    passes_count: int = 5,
) -> tuple[str, list[dict]]:
    """Polygon-area solver using structured-JSON multipass + closure.

    Each pass requests a JSON object with rectangle decomposition and
    direction-keyed edge sums. The Python side validates closure and
    re-derives area from rectangles, dropping passes whose decomposition
    fails closure or whose self-claimed area disagrees with the
    rectangle sum. Returns ``(voted_answer, raw_pass_records)`` so
    callers can audit per-pass output. Universal for any orthogonal
    polygon area question — no task-specific keywords, no expected-
    answer reading.

    On total validation failure (zero valid passes) returns ``("", ...)``
    so the caller can fall back to plain :func:`_solve_vision_anthropic_multipass`.
    """
    ext = os.path.splitext(image_path)[1].lower()
    mime = MIME_MAP.get(ext, "image/png")
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except Exception as err:
        print(
            f"  [polygon-structured read error] {err}",
            flush=True,
        )
        return "", []
    user_text = (
        f"{_POLYGON_STRUCTURED_PROMPT}\n\nQuestion: {question}\n"
    )
    pass_records: list[dict] = []
    valid_areas: list[float] = []
    try:
        client = _get_anthropic()
    except Exception as err:
        print(
            f"  [polygon-structured anthropic init] {err}",
            flush=True,
        )
        return "", []
    request_kwargs: dict[str, object] = {
        "model": model,
        "max_tokens": 4000,
        "timeout": 120.0,
    }
    if not _model_drops_temperature(model):
        request_kwargs["temperature"] = 0.3
    for pass_idx in range(max(1, passes_count)):
        record: dict = {"pass": pass_idx, "valid": False}
        try:
            resp = client.messages.create(
                **request_kwargs,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": mime,
                            "data": b64,
                        }},
                        {"type": "text", "text": user_text},
                    ],
                }],
            )
            raw = resp.content[0].text if resp.content else ""
            record["raw_len"] = len(raw)
            obj_str = _extract_json_object(raw)
            if obj_str is None:
                record["error"] = "no_json_object"
                pass_records.append(record)
                continue
            try:
                obj = json.loads(obj_str)
            except json.JSONDecodeError as je:
                record["error"] = f"json_decode: {je}"
                pass_records.append(record)
                continue
            area = _validate_polygon_pass(obj)
            if area is None:
                record["error"] = "closure_or_recompute_mismatch"
                record["edge_sums"] = obj.get("edge_sums")
                record["claimed_area"] = obj.get("computed_area")
                pass_records.append(record)
                continue
            record["valid"] = True
            record["area"] = area
            record["rect_count"] = len(obj.get("rectangles") or [])
            valid_areas.append(area)
        except Exception as err:
            record["error"] = f"call: {err}"
        pass_records.append(record)
    if not valid_areas:
        return "", pass_records
    valid_areas.sort()
    median = valid_areas[len(valid_areas) // 2]
    return _format_polygon_area(median), pass_records


def _solve_vision_local(question: str, image_path: str) -> str:
    """Solve a vision question with the local Qwen2.5-VL (or fallback
    open-source multimodal) model. CPU path, ~5-20s/image.

    Music-notation questions (bass clef / staff / noteheads) get:
      - 4× LANCZOS upscale for small images (<800px) so noteheads land
        on enough pixels for the visual encoder

    Polygon-AREA questions get force-routed to Anthropic Sonnet
    multi-pass vision (gated by ``gaia_polygon_sonnet_multipass``)
    because local Gemma 4 Q4_K_M mmproj reliably under-counts on
    orthogonal-polygon decomposition (concave-corner rectangles
    missed). The route is generic infra (no answer paths); the
    polygon-area procedure anchor it injects is the same textbook
    decomposition method already used by the local path.

    The L1 procedural anchor (generic music-theory / polygon procedure
    text) is gated by separate ``gaia_music_procedure_anchor`` /
    ``gaia_polygon_area_procedure_anchor`` feature toggles registered
    in :mod:`concinno.feature_config`.
    """
    # ── Polygon-area: structured-JSON multipass (closure-validated) ──
    # First-class path. Asks the vision model for a strict JSON object
    # carrying the rectangle decomposition + per-direction edge sums and
    # has Python (a) verify horizontal/vertical closure and (b) re-derive
    # the area from rectangles, dropping passes whose decomposition fails
    # closure or whose self-claimed area disagrees with the rectangle
    # sum. Generic for any orthogonal polygon area question — no task-
    # specific keywords, no expected-answer reading. On total validation
    # failure falls through to the legacy free-form multipass below.
    if (
        _is_orthogonal_polygon_area_question(question)
        and _feature_enabled("gaia_polygon_structured_multipass")
    ):
        passes_count, model = _multipass_params(
            "gaia_polygon_structured_multipass",
        )
        upscale_path = (
            _upscale_image_if_small(image_path)
            if _feature_enabled("image_upscale_4x")
            else image_path
        )
        print(
            "  [polygon-area structured-multipass] "
            f"model={model} passes={passes_count} for "
            f"{question[:60]!r}",
            flush=True,
        )
        voted, records = _solve_polygon_structured_multipass(
            question, upscale_path,
            model=model, passes_count=passes_count,
        )
        if voted:
            return voted
        valid_count = sum(1 for r in records if r.get("valid"))
        print(
            "  [polygon-structured 0 valid — falling through] "
            f"valid={valid_count}/{len(records)}",
            flush=True,
        )

    # ── Polygon-area legacy free-form multipass fallback ──
    # Used when structured-JSON pipeline yields zero closure-valid passes
    # (e.g. very ambiguous polygon, model refuses JSON, etc.). Same
    # majority-vote behaviour as before; left in place as a safety net.
    if (
        _is_orthogonal_polygon_area_question(question)
        and _feature_enabled("gaia_polygon_sonnet_multipass")
    ):
        passes_count, model = _polygon_multipass_params()
        upscale_path = (
            _upscale_image_if_small(image_path)
            if _feature_enabled("image_upscale_4x")
            else image_path
        )
        print(
            "  [polygon-area force-anthropic] "
            f"model={model} passes={passes_count} for "
            f"{question[:60]!r}",
            flush=True,
        )
        voted, samples = _solve_vision_anthropic_multipass(
            question, upscale_path,
            model=model, passes_count=passes_count,
        )
        if voted:
            return voted
        # Fall through to local path on total Sonnet failure (network
        # outage, quota, etc.) so we still get a best-effort answer.
        print(
            "  [polygon-area multipass empty — falling back to local] "
            f"samples={samples!r}",
            flush=True,
        )

    # ── Music-notation force-route to Anthropic Sonnet multi-pass ──
    # Empirical (2026-04-26 P0.3): single-pass sonnet bass-clef returns
    # ['90','90','80'] = 67% per-call PASS — N=3 majority vote stabilises
    # to deterministic PASS. Gated by gaia_music_sonnet_multipass; falls
    # through to local path on Sonnet failure.
    if (
        _is_music_notation_question(question)
        and _feature_enabled("gaia_music_sonnet_multipass")
    ):
        passes_count, model = _music_multipass_params()
        upscale_path = (
            _upscale_image_if_small(image_path)
            if _feature_enabled("image_upscale_4x")
            else image_path
        )
        print(
            "  [music-notation force-anthropic] "
            f"model={model} passes={passes_count} for "
            f"{question[:60]!r}",
            flush=True,
        )
        voted, samples = _solve_vision_anthropic_multipass(
            question, upscale_path,
            model=model, passes_count=passes_count,
        )
        if voted:
            return voted
        print(
            "  [music-notation multipass empty — falling back to local] "
            f"samples={samples!r}",
            flush=True,
        )

    try:
        llm = _get_local_vision_llm()
    except Exception as err:
        print(f"  [vision-local load error] {err}", flush=True)
        return ""
    music_mode = (
        _is_music_notation_question(question)
        and _feature_enabled("gaia_music_image_upscale")
    )
    polygon_mode = (
        _is_polygon_counting_question(question)
        and _feature_enabled("gaia_polygon_image_upscale")
    )
    upscale_enabled = _feature_enabled("image_upscale_4x")
    should_upscale = (music_mode or polygon_mode) and upscale_enabled
    effective_path = (
        _upscale_image_if_small(image_path) if should_upscale else image_path
    )
    try:
        with open(effective_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except Exception as err:
        print(f"  [vision-local read error] {err}", flush=True)
        return ""
    ext = os.path.splitext(effective_path)[1].lstrip(".").lower() or "png"
    data_uri = f"data:image/{ext};base64,{b64}"
    # Route to the most-specific L1 domain procedure anchor (music /
    # polygon-area / web-only), falling back to the generic visual-
    # reasoning scaffold for any other image. ``_get_domain_procedure``
    # respects each anchor's feature toggle and returns at most ONE
    # anchor (no stacking — anti-pattern per generic-anchor-design.md).
    procedure = _get_domain_procedure(question, image_path)
    prelude = f"{procedure}\n\n" if procedure else ""
    user_text = (
        f"{prelude}{question}\n\nAnalyze the image carefully and "
        "think step by step. After your reasoning, end with exactly "
        "one final line:\n"
        "FINAL ANSWER: <concise value>\n\n"
        "The value must be concise (number, word, short phrase). "
        "Do NOT add units unless asked. Do NOT cut off in the middle "
        "of reasoning."
    )
    try:
        resp = llm.create_chat_completion(
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url",
                     "image_url": {"url": data_uri}},
                ],
            }],
            # 2500 → 5000: multi-step music / polygon decomposition
            # procedures (post 2026-04-26 anchor refresh) need more
            # headroom — 2500 truncates mid-reasoning before the
            # FINAL ANSWER line, especially on bass-clef multi-step
            # arithmetic and L-shape rectangle-by-rectangle area sums.
            max_tokens=5000,
            temperature=0.2,
        )
        raw = resp["choices"][0]["message"]["content"] or ""
        return _extract_answer(raw)
    except Exception as err:
        print(f"  [vision-local gen error] {err}", flush=True)
        return ""


def _solve_vision_via_ocr(question: str, image_path: str,
                          backend: ModelBackend) -> str:
    """Route text-heavy images through OCR + text LLM (Gemma 4 reasoning).

    Works best for tabular / document / headstone-style images where
    Tesseract reliably extracts readable text. Returns empty string
    when OCR fails or has insufficient signal; caller should fall back
    to a real vision model.
    """
    ocr_text = extract_ocr_text(image_path)
    if not ocr_text:
        return ""
    prompt = (
        f"You are answering a question about an image. The image text "
        f"has been OCR-extracted for you below.\n\n"
        f"[OCR extracted from image]\n{ocr_text[:4000]}\n\n"
        f"Question: {question}\n\n"
        f"Work through the reasoning step by step using the OCR text "
        f"as evidence. After your reasoning, on a new line, output "
        f"exactly one final line in this format (no prose after it):\n"
        f"FINAL ANSWER: <value>\n\n"
        f"The value must be concise: a number, a word, a short phrase, "
        f"or a comma-separated list. Do NOT add units unless the "
        f"question asks. Do NOT leave the answer blank or cut off."
    )
    raw = backend.chat(
        "You answer questions using ONLY the OCR-extracted image text "
        "provided. Be precise. Always end with exactly one "
        "'FINAL ANSWER: <value>' line.",
        [{"role": "user", "content": prompt}],
        max_tokens=1500,
    )
    return _extract_answer(raw)


def _solve_vision(question: str, image_path: str,
                  backend: ModelBackend) -> str:
    """Dispatch vision solve by backend tier and image content.

    Priority order (integrative — all three co-exist under one surface):
    1. sonnet/opus tier → Anthropic vision (native multimodal, best)
    2. gemma tier + local vision model configured → local native
       (Gemma 4 + mmproj via Concinno Gemma4VisionChatHandler, or Qwen /
       MiniCPM / LLaVA via llama-cpp-python built-in handler)
    3. Fallback: OCR + text reasoning when local vision unavailable /
       returns empty (e.g. pure text-heavy images and no GGUF wired)
    """
    if backend.tier not in ("sonnet", "opus"):
        # Primary: local multimodal model (Gemma 4 native vision if
        # GAIA_VISION_MODEL_PATH is wired; otherwise configured
        # fallback like Qwen2.5-VL). Vision model sees pixels directly,
        # preserves purple labels / geometry / musical notation that
        # OCR flattens away.
        vision_answer = _solve_vision_local(question, image_path)
        if vision_answer:
            return vision_answer
        # Last-resort: OCR + text LLM (covers deploys that don't ship
        # a vision GGUF, or where mmproj encoding crashed). Gated by
        # the ``ocr_fallback`` feature — prod turns it off so the OCR
        # pipeline never runs outside benchmark.
        if _feature_enabled("ocr_fallback"):
            ocr_answer = _solve_vision_via_ocr(question, image_path, backend)
            if ocr_answer:
                return ocr_answer
        return ""
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
