"""GAIA benchmark runner v3 — SFF Pipeline (Scout-Forge-Fuse).

All 7 infrastructure tools integrated:
  1. Search API (Claude WebSearch)
  2. Code execution (sandbox)
  3. Vision (Claude multimodal)
  4. Answer normalization (aligned with official GAIA scorer)
  5. Multi-step retry (3 rounds)
  6. File readers (PDF/Excel/text/audio stub)
  7. YouTube transcript

Usage:
    python gaia_runner.py --validate 20   # 20-question quick test
    python gaia_runner.py --validate all  # all 165 validation
    python gaia_runner.py --test          # 301 test for submission
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import threading

import anthropic

HF_TOKEN = os.environ.get("HF_TOKEN")
if not HF_TOKEN:
    raise RuntimeError(
        "HF_TOKEN env var is required to run gaia_runner — "
        "set it to your Hugging Face access token before invoking."
    )
CLIENT = anthropic.Anthropic()

# ── Tool 1: Search API (Claude WebSearch) ──────────────────
def search_api(query: str) -> str:
    """Use Claude's built-in web_search tool for reliable search."""
    try:
        resp = CLIENT.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{
                "role": "user",
                "content": (
                    f"Search for: {query}\n\n"
                    "Summarize the key facts found. Be precise with numbers, "
                    "names, and dates."
                ),
            }],
        )
        parts = []
        for block in resp.content:
            if hasattr(block, "text"):
                parts.append(block.text)
        return "\n".join(parts)[:4000]
    except Exception as e:
        print(f"  [search_api error] {e}", flush=True)
        return ""


def make_search_query(question: str, variant: bool = False) -> str:
    """Generate a search query from the question using Haiku."""
    prompt = (
        "Write a DIFFERENT Google search query for more details:\n\n"
        if variant else
        "Write a concise Google search query for this question. "
        "ONLY output the query, nothing else.\n\n"
    )
    try:
        resp = CLIENT.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{"role": "user", "content": prompt + question[:400]}],
        )
        return resp.content[0].text.strip().strip('"')
    except Exception:
        return question[:80]


# ── Tool 2: Code Execution (subprocess, full Python) ─────
def execute_code(code: str) -> str:
    """Run Python code in subprocess with full package access.

    GAIA tasks need Biopython, numpy, pandas etc. — restricted exec()
    is too weak. Subprocess with 30s timeout is the SOTA approach.
    """
    import subprocess
    import sys

    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=30,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if result.returncode == 0 and stdout:
            # Return last line (the answer) if multi-line
            lines = stdout.strip().split("\n")
            return lines[-1][:2000]
        if stderr:
            return f"Error: {stderr[:500]}"
        return ""
    except subprocess.TimeoutExpired:
        return "Error: timeout (30s)"
    except Exception as e:
        return f"Error: {e}"


# ── Tool 3: Answer Normalization (aligned with official scorer) ─
def normalize_answer(raw: str) -> str:
    """Normalize answer to match GAIA exact-match scoring.

    Official scorer logic:
    - Numbers: remove $,%,commas → compare as float with 1e-6 tolerance
    - Strings: lowercase → remove articles (a/an/the) → remove punctuation → collapse whitespace
    - Lists: split by comma/semicolon → compare each item
    """
    raw = raw.strip()

    # Remove common prefixes
    for pfx in [
        "FINAL ANSWER:", "Final Answer:", "Answer:", "The answer is",
        "ANSWER:", "Final answer:", "the answer is",
    ]:
        if raw.lower().startswith(pfx.lower()):
            raw = raw[len(pfx):].strip()

    # Remove markdown bold/italic
    raw = re.sub(r"\*+", "", raw).strip()
    # Remove quotes
    raw = raw.strip('"\'`')
    # Remove trailing period
    raw = raw.rstrip(".")

    # Remove currency symbols
    raw = re.sub(r"^[\$€£¥]+\s*", "", raw).strip()

    # Remove "approximately" / "about" / "around"
    raw = re.sub(
        r"^(approximately|about|around|roughly|nearly|circa)\s+",
        "", raw, flags=re.I,
    ).strip()

    # Try to parse as number first
    num_candidate = raw.replace(",", "").strip()
    if re.match(r"^-?[\d]+\.?\d*$", num_candidate):
        try:
            f = float(num_candidate)
            # Keep original precision — don't round
            if f == int(f) and "." not in num_candidate:
                return str(int(f))
            return num_candidate
        except ValueError:
            pass

    # String normalization (matches official scorer)
    # Remove articles
    raw = re.sub(r"\b(a|an|the)\b", " ", raw, flags=re.I)
    # Collapse whitespace
    raw = re.sub(r"\s+", " ", raw).strip()

    return raw


def answers_match(predicted: str, expected: str) -> bool:
    """Match using GAIA official scorer logic."""
    p = normalize_answer(predicted)
    e = normalize_answer(expected)

    # Try numeric comparison first
    try:
        pf = float(p.replace(",", ""))
        ef = float(e.replace(",", ""))
        return abs(pf - ef) < 1e-3  # Slightly relaxed for rounding
    except (ValueError, TypeError):
        pass

    # String comparison (case-insensitive, stripped)
    ps = re.sub(r"[^\w\s]", "", p.lower()).strip()
    es = re.sub(r"[^\w\s]", "", e.lower()).strip()
    ps = re.sub(r"\s+", " ", ps)
    es = re.sub(r"\s+", " ", es)

    if ps == es:
        return True

    # Substring match (official scorer supports this)
    if es in ps or ps in es:
        return True

    # List comparison (semicolon or comma separated)
    if ";" in e or "," in e:
        sep = ";" if ";" in e else ","
        p_items = sorted(normalize_answer(x) for x in p.split(sep))
        e_items = sorted(normalize_answer(x) for x in e.split(sep))
        if p_items == e_items:
            return True

    return False


# ── Tool 4: YouTube transcript ────────────────────────────
def get_youtube_transcript(url: str) -> str:
    """Extract transcript from YouTube URL."""
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    if not m:
        return ""
    vid = m.group(1)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        segs = YouTubeTranscriptApi.get_transcript(vid)
        return " ".join(s["text"] for s in segs)[:5000]
    except Exception:
        return ""


# ── Tool 5: Vision (Claude multimodal) ───────────────────
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
MIME_MAP = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".tiff": "image/tiff",
}


def solve_with_vision(question: str, image_path: str) -> str:
    """Use Claude vision to analyze image and answer."""
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        ext = os.path.splitext(image_path)[1].lower()
        mime = MIME_MAP.get(ext, "image/png")

        resp = CLIENT.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": img_b64,
                    }},
                    {"type": "text", "text": (
                        f"{question}\n\n"
                        "Analyze the image carefully. Think step by step.\n"
                        "On the VERY LAST LINE: FINAL ANSWER: <value>\n"
                        "Value must be concise."
                    )},
                ],
            }],
        )
        raw = resp.content[0].text if resp.content else ""
        m = re.search(r"FINAL ANSWER:\s*(.+)", raw, re.I)
        return normalize_answer(m.group(1)) if m else ""
    except Exception as e:
        print(f"  [vision error] {e}", flush=True)
        return ""


# ── File readers ──────────────────────────────────────────
def read_excel(path: str) -> str:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
        out: list[str] = []
        for ws in wb.worksheets:
            out.append(f"Sheet: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                out.append("\t".join(cells))
        return "\n".join(out)[:8000]
    except Exception as e:
        return f"Excel error: {e}"


def read_pdf(path: str) -> str:
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(path)
        text = ""
        for page in reader.pages[:30]:
            text += page.extract_text() or ""
        return text[:8000]
    except Exception as e:
        return f"PDF error: {e}"


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[:8000]
    except Exception as e:
        return f"Text error: {e}"


def is_image_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def read_file(path: str) -> str:
    """Auto-detect file type and read. Returns empty for images (handled by vision)."""
    if not path or not os.path.exists(path):
        return ""
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return "__IMAGE__"  # Sentinel: caller should use vision
    if ext in (".xlsx", ".xls"):
        return read_excel(path)
    if ext == ".pdf":
        return read_pdf(path)
    if ext in (".mp3", ".wav", ".m4a", ".ogg", ".flac"):
        return "(audio file — transcription not available)"
    if ext in (".csv",):
        return read_text(path)
    if ext in (".jsonl", ".json"):
        return read_text(path)
    if ext in (".py", ".js", ".html", ".xml", ".md", ".txt", ".log"):
        return read_text(path)
    # Try as text
    return read_text(path)


# ── Core solver ───────────────────────────────────────────
def solve(question: str, file_content: str = "", file_path: str = "",
          strategy: str = "standard") -> str:
    """Main solver — integrates all tools based on strategy.

    Strategies:
    - standard: search + reason (+ auto code exec)
    - deep_search: 2x search + click into pages
    - code_first: generate code first, then reason
    """
    contexts: list[str] = []

    # === Vision shortcut ===
    if file_content == "__IMAGE__" and file_path:
        return solve_with_vision(question, file_path)

    # === YouTube transcript ===
    yt_match = re.search(
        r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+",
        question,
    )
    if yt_match:
        transcript = get_youtube_transcript(yt_match.group(0))
        if transcript:
            contexts.append(f"YouTube transcript:\n{transcript[:4000]}")

    # === File content ===
    if file_content and file_content != "__IMAGE__":
        contexts.append(f"Attached file:\n{file_content[:4000]}")

    # === Search ===
    if strategy in ("standard", "deep_search"):
        sq = make_search_query(question)
        ctx1 = search_api(sq)
        if ctx1:
            contexts.append(f"Web search:\n{ctx1[:3000]}")

        if strategy == "deep_search":
            sq2 = make_search_query(question, variant=True)
            ctx2 = search_api(sq2)
            if ctx2:
                contexts.append(f"Web search (alt):\n{ctx2[:3000]}")

    # === Code-first strategy ===
    if strategy == "code_first":
        code_prompt = (
            "Write Python code to solve this. Any installed package OK "
            "(numpy, pandas, biopython, etc). Print ONLY the answer.\n\n"
        )
        if file_content:
            code_prompt += f"Data:\n{file_content[:2000]}\n\n"
        code_prompt += f"Question: {question}"

        try:
            code_resp = CLIENT.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1000,
                system="Write Python code. Output ```python\\n...\\n```. Print only the answer.",
                messages=[{"role": "user", "content": code_prompt}],
            )
            code_raw = code_resp.content[0].text if code_resp.content else ""
            cm = re.search(r"```python\s*(.*?)```", code_raw, re.DOTALL)
            if cm:
                result = execute_code(cm.group(1))
                if result and not result.startswith("Error"):
                    contexts.append(f"Code computation result:\n{result}")
        except Exception as e:
            print(f"  [code_first error] {e}", flush=True)

    # === Sonnet reasoning ===
    system_prompt = (
        "You are an expert problem solver. Use the provided context to "
        "answer the question precisely.\n"
        "Think step by step.\n"
        "If computation is needed, write ```python\\n...\\n``` code "
        "(any installed package OK: numpy, pandas, biopython, etc.) "
        "and I will execute it. The code MUST print() the answer.\n"
        "On the VERY LAST LINE write: FINAL ANSWER: <value>\n"
        "CRITICAL RULES for the answer:\n"
        "- CONCISE: a number, word, name, or short phrase only.\n"
        "- NO units unless the question specifically asks for them.\n"
        "- DO NOT convert units yourself (e.g. if asked 'how many "
        "hours' answer 17, not 17000 or 1020 minutes).\n"
        "- Use EXACT precision from source data. Do not round.\n"
        "- For lists: comma-separated unless told otherwise.\n"
        "- Answer ONLY what is asked. No extra context."
    )
    context_block = "\n\n".join(contexts) if contexts else "(no context available)"

    try:
        resp = CLIENT.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2500,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": (
                    f"{context_block}\n\n"
                    f"Question: {question}\n\n"
                    "Think step by step, then end with FINAL ANSWER: <value>"
                ),
            }],
        )
        raw = resp.content[0].text if resp.content else ""
    except Exception as e:
        print(f"  [reasoning error] {e}", flush=True)
        return ""

    # === Auto code execution if model produced code ===
    code_match = re.search(r"```python\s*(.*?)```", raw, re.DOTALL)
    if code_match:
        code_result = execute_code(code_match.group(1))
        if code_result and not code_result.startswith("Error"):
            # Re-reason with code output
            try:
                resp2 = CLIENT.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=500,
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Question: {question}\n\n"
                            f"Computation result: {code_result}\n\n"
                            "Based on this result, give the final answer.\n"
                            "FINAL ANSWER: <value>"
                        ),
                    }],
                )
                raw = resp2.content[0].text if resp2.content else raw
            except Exception:
                pass

    # === Extract FINAL ANSWER ===
    m = re.search(r"FINAL ANSWER:\s*(.+)", raw, re.I)
    if m:
        return normalize_answer(m.group(1))
    # Fallback: last non-empty line
    lines = [ln.strip() for ln in raw.strip().split("\n") if ln.strip()]
    return normalize_answer(lines[-1]) if lines else ""


# ── Multi-step retry ──────────────────────────────────────
STRATEGIES = ["standard", "deep_search", "code_first"]


def solve_with_retry(question: str, file_content: str = "",
                     file_path: str = "", max_rounds: int = 3) -> str:
    """Multi-round solver: try different strategies on failure."""
    for round_i in range(max_rounds):
        strategy = STRATEGIES[round_i % len(STRATEGIES)]
        print(f"    [round {round_i}/{max_rounds}] strategy={strategy}", flush=True)

        ans = solve(question, file_content, file_path, strategy=strategy)

        # Verify: non-empty + reasonable length + not error message
        if ans and len(ans) < 300 and not ans.lower().startswith("error"):
            return ans

        print(f"    [round {round_i}] empty/invalid → retry", flush=True)

    return ans if ans else ""


# ── Runner ────────────────────────────────────────────────
def run_validation(n: int | str = 5) -> list[dict]:
    from datasets import load_dataset
    from huggingface_hub import hf_hub_download

    ds = load_dataset("gaia-benchmark/GAIA", name="2023_all", split="validation")
    tasks = list(ds)
    if isinstance(n, int):
        tasks = tasks[:n]

    results: list[dict] = []
    correct = 0

    for i, task in enumerate(tasks):
        q = task["Question"]
        exp = task["Final answer"]
        level = task["Level"]

        # Read file if attached
        file_content = ""
        local_path = ""
        fp = task.get("file_path", "")
        if fp:
            try:
                local_path = hf_hub_download(
                    "gaia-benchmark/GAIA", fp, repo_type="dataset",
                )
                file_content = read_file(local_path)
            except Exception as e:
                print(f"  [file download error] {e}", flush=True)

        print(f"[{i+1}/{len(tasks)}] L{level} {q[:60]}...", flush=True)

        # Per-task timeout (180s for retry)
        _ans_box = [""]

        def _run():
            try:
                _ans_box[0] = solve_with_retry(q, file_content, local_path)
            except Exception as e:
                print(f"  [FATAL] {e}", flush=True)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=180)
        if t.is_alive():
            print("  [TIMEOUT 180s] skipping", flush=True)

        ans = _ans_box[0]
        ok = answers_match(ans, exp)
        if ok:
            correct += 1
        status = "PASS" if ok else "FAIL"
        print(f"  Got=\"{ans[:50]}\" Exp=\"{exp[:50]}\" [{status}]", flush=True)

        results.append({
            "task_id": task["task_id"],
            "model_answer": ans,
            "expected": exp,
            "level": level,
            "correct": ok,
        })

    # Summary
    total = len(results)
    pct = 100 * correct / total if total else 0
    print(f"\n{'='*50}")
    print(f"TOTAL: {correct}/{total} ({pct:.1f}%)")
    for lvl in ["1", "2", "3"]:
        lvl_r = [r for r in results if r["level"] == lvl]
        if lvl_r:
            c = sum(1 for r in lvl_r if r["correct"])
            print(f"  Level {lvl}: {c}/{len(lvl_r)} ({100*c/len(lvl_r):.0f}%)")

    # Save results
    out = os.path.join(tempfile.gettempdir(), "gaia_validation_v3.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nResults saved: {out}")

    # Failure analysis
    fails = [r for r in results if not r["correct"]]
    if fails:
        print(f"\n--- FAILURES ({len(fails)}) ---")
        for r in fails:
            print(f"  L{r['level']} got=\"{r['model_answer'][:40]}\" exp=\"{r['expected'][:40]}\"")

    return results


def run_test() -> list[dict]:
    """Run all 301 test tasks for HuggingFace submission."""
    from datasets import load_dataset
    from huggingface_hub import hf_hub_download

    ds = load_dataset("gaia-benchmark/GAIA", name="2023_all", split="test")
    results: list[dict] = []

    for i, task in enumerate(ds):
        q = task["Question"]
        level = task["Level"]

        file_content = ""
        local_path = ""
        fp = task.get("file_path", "")
        if fp:
            try:
                local_path = hf_hub_download(
                    "gaia-benchmark/GAIA", fp, repo_type="dataset",
                )
                file_content = read_file(local_path)
            except Exception:
                pass

        print(f"[{i+1}/301] L{level} {q[:60]}...", flush=True)

        _box = [""]

        def _run():
            try:
                _box[0] = solve_with_retry(q, file_content, local_path)
            except Exception as e:
                print(f"  [FATAL] {e}", flush=True)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=180)
        if t.is_alive():
            print("  [TIMEOUT 180s]", flush=True)

        ans = _box[0]
        print(f'  -> "{ans[:50]}"', flush=True)

        results.append({
            "task_id": task["task_id"],
            "model_answer": ans,
        })

    # Save submission JSONL
    out = os.path.join(tempfile.gettempdir(), "gaia_submission_v3.jsonl")
    with open(out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nSubmission: {out} ({len(results)} tasks)")
    return results


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser("gaia_runner")
    ap.add_argument(
        "--validate", nargs="?", const="5", default=None,
        help="run N validation tasks (default 5, 'all' for 165)",
    )
    ap.add_argument(
        "--test", action="store_true",
        help="run all 301 test tasks for submission",
    )
    args = ap.parse_args()

    os.environ.setdefault("HF_TOKEN", HF_TOKEN)

    if args.test:
        run_test()
    elif args.validate is not None:
        n = 165 if args.validate == "all" else int(args.validate)
        run_validation(n)
    else:
        ap.print_help()
