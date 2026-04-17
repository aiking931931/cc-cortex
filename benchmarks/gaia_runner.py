"""GAIA Benchmark Runner for STAR RAG Engine.

Team: AI King
System: CCC STAR v3.5

Runs GAIA validation split (165 questions) through STAR engine,
records accuracy + cost + latency per question.

Usage:
    # Validation (local scoring, no upload)
    python benchmarks/gaia_runner.py --split validation

    # With specific profile
    python benchmarks/gaia_runner.py --split validation --profile balanced

    # Ablation: Claude raw (no RAG)
    python benchmarks/gaia_runner.py --split validation --no-rag
"""

from __future__ import annotations

import argparse
import json
import os
import re
import string
import time

import anthropic

# ── GAIA Scorer (from official scorer.py) ─────────────────


def normalize_number_str(number_str: str) -> float | None:
    """Normalize number string by removing $, %, commas."""
    for char in ["$", "%", ","]:
        number_str = number_str.replace(char, "")
    try:
        return float(number_str)
    except ValueError:
        return None


def normalize_str(input_str: str) -> str:
    """Lower, strip, remove punctuation and articles."""
    # Remove articles
    no_articles = re.sub(r"\b(a|an|the)\b", " ", input_str.lower())
    # Remove punctuation
    no_punct = no_articles.translate(str.maketrans("", "", string.punctuation))
    # Normalize whitespace
    return " ".join(no_punct.split())


def question_scorer(model_answer: str, ground_truth: str) -> bool:
    """Official GAIA scoring logic."""
    if not model_answer:
        return False

    # Try number comparison
    gt_num = normalize_number_str(ground_truth)
    ma_num = normalize_number_str(model_answer)
    if gt_num is not None and ma_num is not None:
        return abs(gt_num - ma_num) < 1e-6

    # Try list comparison (comma or semicolon separated)
    if any(sep in ground_truth for sep in [",", ";"]):
        sep = "," if "," in ground_truth else ";"
        gt_parts = [s.strip() for s in ground_truth.split(sep)]
        ma_parts = [s.strip() for s in model_answer.split(sep)]

        if len(gt_parts) != len(ma_parts):
            return False

        for gt_p, ma_p in zip(gt_parts, ma_parts):
            gt_n = normalize_number_str(gt_p)
            ma_n = normalize_number_str(ma_p)
            if gt_n is not None and ma_n is not None:
                if abs(gt_n - ma_n) >= 1e-6:
                    return False
            elif normalize_str(gt_p) != normalize_str(ma_p):
                return False
        return True

    # String comparison
    return normalize_str(model_answer) == normalize_str(ground_truth)


# ── Profile routing ───────────────────────────────────────


def classify_question(question: str) -> str:
    """Route question to best profile.

    Returns: 'precision' | 'recall' | 'balanced'
    """
    q_lower = question.lower()

    # Calculation/code → precision (skip RAG mostly, direct Claude)
    if any(kw in q_lower for kw in [
        "calculate", "compute", "how many", "what is the sum",
        "what is the product", "convert", "formula",
    ]):
        return "precision"

    # Multi-step / complex reasoning → balanced + confluence
    if any(kw in q_lower for kw in [
        "step by step", "explain why", "compare", "relationship between",
        "what would happen if", "analyze",
    ]):
        return "balanced"

    # Search-heavy → recall
    if any(kw in q_lower for kw in [
        "find all", "list all", "search for", "who are the",
        "what are all the", "every",
    ]):
        return "recall"

    # Default: precision (most GAIA questions are factual)
    return "precision"


# ── Claude API caller ─────────────────────────────────────


def ask_claude(
    client: anthropic.Anthropic,
    question: str,
    context: str = "",
    model: str = "claude-haiku-4-5-20251001",
) -> tuple[str, int, int]:
    """Ask Claude a GAIA question. Returns (answer, input_tokens, output_tokens)."""
    system = (
        "You are answering GAIA benchmark questions. "
        "Give ONLY the final answer — no explanation, no reasoning, no preamble. "
        "If the answer is a number, give just the number. "
        "If the answer is a name, give just the name. "
        "If the answer is a list, separate items with commas. "
        "Be precise and concise."
    )

    messages = []
    if context:
        messages.append({
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {question}\n\nAnswer (short, precise):",
        })
    else:
        messages.append({
            "role": "user",
            "content": f"Question: {question}\n\nAnswer (short, precise):",
        })

    resp = client.messages.create(
        model=model,
        max_tokens=256,
        system=system,
        messages=messages,
    )

    answer = resp.content[0].text.strip()
    return answer, resp.usage.input_tokens, resp.usage.output_tokens


# ── Main runner ───────────────────────────────────────────


def run_gaia(
    split: str = "validation",
    profile: str | None = None,
    no_rag: bool = False,
    model: str = "claude-haiku-4-5-20251001",
    output_dir: str = "",
    max_questions: int = 0,
):
    """Run GAIA benchmark."""
    # Load dataset
    from datasets import load_dataset
    from huggingface_hub import snapshot_download

    print("Downloading GAIA dataset...")
    data_dir = snapshot_download(repo_id="gaia-benchmark/GAIA", repo_type="dataset")
    print(f"Dataset at: {data_dir}")

    dataset = load_dataset(data_dir, "2023_all", split=split)
    print(f"Loaded {len(dataset)} questions from {split} split")

    if max_questions:
        dataset = dataset.select(range(min(max_questions, len(dataset))))
        print(f"  (limited to {len(dataset)} questions)")

    # Init Claude client
    client = anthropic.Anthropic()

    # Init STAR engine (unless no_rag)
    star_engine = None
    if not no_rag:
        try:
            from concinno.star import RetrievalProfile, create_star_engine

            profile_enum = None
            if profile:
                profile_enum = RetrievalProfile(profile)
            else:
                profile_enum = RetrievalProfile.PRECISION  # default

            star_engine = create_star_engine(
                project_dir=os.getcwd(),
                profile=profile_enum,
            )
            print(f"STAR engine loaded (profile={profile_enum.value})")
        except Exception as e:
            print(f"STAR engine not available: {e}")
            print("Running without RAG (Claude raw)")

    # Output directory
    if not output_dir:
        output_dir = os.path.join(
            os.path.dirname(__file__), "results"
        )
    os.makedirs(output_dir, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M")
    profile_tag = profile or ("raw" if no_rag else "precision")
    results_path = os.path.join(output_dir, f"gaia_{split}_{profile_tag}_{timestamp}.jsonl")
    summary_path = os.path.join(output_dir, f"gaia_{split}_{profile_tag}_{timestamp}_summary.md")

    # Run questions
    results = []
    correct_count = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_latency = 0

    by_level = {
        1: {"correct": 0, "total": 0},
        2: {"correct": 0, "total": 0},
        3: {"correct": 0, "total": 0},
    }

    for i, row in enumerate(dataset):
        task_id = row["task_id"]
        question = row["Question"]
        ground_truth = row.get("Final answer", "")
        level = row["Level"]
        has_file = bool(row.get("file_name"))

        # Get RAG context
        context = ""
        retrieval_sources = 0
        if star_engine and not has_file:  # Skip RAG for file-based questions
            try:
                result = star_engine.retrieve(question)
                if result and result.injection:
                    context = result.injection[:2000]  # Cap context
                    retrieval_sources = len(result.sources) if result.sources else 0
            except Exception:
                pass

        # Ask Claude
        start_time = time.time()
        try:
            answer, in_tok, out_tok = ask_claude(client, question, context, model)
        except Exception as e:
            answer = ""
            in_tok, out_tok = 0, 0
            print(f"  ERROR on {task_id}: {e}")
        latency = (time.time() - start_time) * 1000  # ms

        # Score
        is_correct = question_scorer(answer, ground_truth) if ground_truth else None

        if is_correct:
            correct_count += 1
        by_level[level]["total"] += 1
        if is_correct:
            by_level[level]["correct"] += 1

        total_input_tokens += in_tok
        total_output_tokens += out_tok
        total_latency += latency

        # Calculate cost (Haiku pricing)
        cost = (in_tok / 1_000_000 * 0.25) + (out_tok / 1_000_000 * 1.25)

        result_row = {
            "task_id": task_id,
            "level": level,
            "has_file": has_file,
            "profile": profile_tag,
            "model_answer": answer,
            "ground_truth": ground_truth,
            "correct": is_correct,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "retrieval_sources": retrieval_sources,
            "latency_ms": round(latency, 1),
            "cost_usd": round(cost, 6),
        }
        results.append(result_row)

        if is_correct:
            status = "✅"
        elif is_correct is False:
            status = "❌"
        else:
            status = "❓"
        short = answer[:50]
        print(
            f"  [{i+1}/{len(dataset)}] L{level} {status}"
            f" ({latency:.0f}ms, {in_tok}+{out_tok}t) {short}"
        )

    # Write results JSONL
    with open(results_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Calculate summary
    total = len(results)
    accuracy = correct_count / total * 100 if total else 0
    total_cost = sum(r["cost_usd"] for r in results)
    avg_latency = total_latency / total if total else 0
    cp_value = accuracy / total_cost if total_cost > 0 else 0

    # Write summary
    def _pct(lv: int) -> str:
        c = by_level[lv]["correct"]
        t = by_level[lv]["total"]
        return f"{c / max(t, 1) * 100:.1f}%"

    lines = [
        f"# GAIA {split} — {profile_tag}"
        f" | {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Team: AI King | System: CCC STAR v3.5",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| **Accuracy** | **{accuracy:.1f}%**"
        f" ({correct_count}/{total}) |",
        f"| Total Cost | ${total_cost:.4f} USD"
        f" ({total_cost * 32:.1f} TWD) |",
        f"| Avg Latency | {avg_latency:.0f} ms |",
        f"| CP Value | {cp_value:.0f} (accuracy/cost) |",
        f"| Model | {model} |",
        f"| Profile | {profile_tag} |",
        f"| Input Tokens | {total_input_tokens:,} |",
        f"| Output Tokens | {total_output_tokens:,} |",
        "",
        "## By Level",
        "",
        "| Level | Correct | Total | Accuracy |",
        "|-------|---------|-------|----------|",
        f"| L1 | {by_level[1]['correct']}"
        f" | {by_level[1]['total']} | {_pct(1)} |",
        f"| L2 | {by_level[2]['correct']}"
        f" | {by_level[2]['total']} | {_pct(2)} |",
        f"| L3 | {by_level[3]['correct']}"
        f" | {by_level[3]['total']} | {_pct(3)} |",
        "",
        "## Results",
        "",
        results_path,
    ]
    summary = "\n".join(lines) + "\n"

    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary)

    print(f"\n{'='*60}")
    print(f"GAIA {split} — {profile_tag}")
    print(f"{'='*60}")
    print(f"Accuracy: {accuracy:.1f}% ({correct_count}/{total})")
    print(f"  L1: {by_level[1]['correct']}/{by_level[1]['total']}")
    print(f"  L2: {by_level[2]['correct']}/{by_level[2]['total']}")
    print(f"  L3: {by_level[3]['correct']}/{by_level[3]['total']}")
    print(f"Cost: ${total_cost:.4f} USD ({total_cost * 32:.1f} TWD)")
    print(f"Avg Latency: {avg_latency:.0f} ms")
    print(f"CP Value: {cp_value:.0f}")
    print(f"\nResults: {results_path}")
    print(f"Summary: {summary_path}")

    return accuracy, total_cost


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GAIA Benchmark Runner")
    parser.add_argument("--split", default="validation", choices=["validation", "test"])
    parser.add_argument("--profile", default=None, choices=["precision", "recall", "balanced"])
    parser.add_argument("--no-rag", action="store_true", help="Claude raw (no STAR engine)")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--max", type=int, default=0, help="Max questions (0=all)")
    args = parser.parse_args()

    run_gaia(
        split=args.split,
        profile=args.profile,
        no_rag=args.no_rag,
        model=args.model,
        output_dir=args.output_dir,
        max_questions=args.max,
    )
