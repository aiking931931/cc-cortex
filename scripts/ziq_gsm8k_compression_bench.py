"""ZIQ 壓縮 vs Reasoning Benchmark.

Hypothesis: 壓縮 (KV cache quantization) 後 GSM8K accuracy 會崩嗎？

Pod: ziq-bench3 (L40S).
Model: Qwen2.5-1.5B-Instruct.
Dataset: GSM8K test (1319 問題).

壓縮策略 (KV cache quantization via HQQ):
  baseline: fp16 KV (16-bit, 0% compression)
  q8:  8-bit KV (~50% compression)
  q4:  4-bit KV (~75% compression, 稱 "80%")
  q2:  2-bit KV (~87.5% compression, 稱 "90%")
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, "/workspace/pylibs")
os.environ.setdefault("HF_HOME", "/workspace/hf_cache")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

import torch  # noqa: E402
from datasets import load_dataset  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402
from transformers.cache_utils import QuantizedCache  # noqa: E402

MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct"
ANS_RE = re.compile(r"####\s*(-?[\d,]+(?:\.\d+)?)")
BOXED_RE = re.compile(r"\\boxed\{\s*\$?(-?[\d,]+(?:\.\d+)?)\s*\}")
ANSWER_IS_RE = re.compile(r"(?:answer is|答案是|=)\s*\$?(-?[\d,]+(?:\.\d+)?)", re.I)
NUM_RE = re.compile(r"(-?[\d,]+(?:\.\d+)?)")

PROMPT_TMPL = (
    "You are a math expert. Solve the problem step by step. "
    "At the end, give the final numeric answer on a line starting with '#### '.\n\n"
    "Question: {q}\n\nSolution:"
)


def extract_gold(ans: str) -> str | None:
    m = ANS_RE.search(ans)
    if not m:
        return None
    return m.group(1).replace(",", "").rstrip(".")


def extract_pred(text: str) -> str | None:
    m = ANS_RE.search(text)
    if m:
        return m.group(1).replace(",", "").rstrip(".")
    m = BOXED_RE.search(text)
    if m:
        return m.group(1).replace(",", "").rstrip(".")
    m = ANSWER_IS_RE.search(text)
    if m:
        return m.group(1).replace(",", "").rstrip(".")
    nums = NUM_RE.findall(text)
    if nums:
        return nums[-1].replace(",", "").rstrip(".")
    return None


def eq_num(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) < 1e-4
    except ValueError:
        return a == b


def make_cache(mode: str, model_config) -> QuantizedCache | None:
    if mode == "baseline":
        return None
    nbits_map = {"q8": 8, "q4": 4, "q2": 2}
    if mode not in nbits_map:
        raise ValueError(mode)
    return QuantizedCache(
        backend="hqq",
        config=model_config,
        nbits=nbits_map[mode],
        axis_key=0,
        axis_value=0,
        q_group_size=64,
        residual_length=128,
    )


def build_prompts(tok, questions: list[str]) -> list[str]:
    out = []
    for q in questions:
        msgs = [{"role": "user", "content": PROMPT_TMPL.format(q=q)}]
        out.append(tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True))
    return out


def run_split(
    model,
    tok,
    data,
    mode: str,
    max_new: int = 384,
    batch_size: int = 8,
    verbose_every: int = 1,
) -> dict:
    model.eval()
    correct = 0
    total = 0
    t0 = time.time()
    n = len(data)
    tok.padding_side = "left"
    if tok.pad_token_id is None:
        tok.pad_token_id = tok.eos_token_id

    for bstart in range(0, n, batch_size):
        batch = data.select(range(bstart, min(bstart + batch_size, n)))
        qs = [row["question"] for row in batch]
        golds = [extract_gold(row["answer"]) for row in batch]
        prompts = build_prompts(tok, qs)
        inputs = tok(prompts, return_tensors="pt", padding=True, truncation=True,
                     max_length=1024).to(model.device)
        # QuantizedCache cannot be shared across batch items; rebuild per batch.
        cache = make_cache(mode, model.config)
        with torch.no_grad():
            out = model.generate(
                **inputs,
                max_new_tokens=max_new,
                do_sample=False,
                past_key_values=cache,
                use_cache=True,
                pad_token_id=tok.pad_token_id,
            )
        gen_tokens = out[:, inputs["input_ids"].shape[1]:]
        for i, gtok in enumerate(gen_tokens):
            gen = tok.decode(gtok, skip_special_tokens=True)
            pred = extract_pred(gen)
            correct += int(eq_num(pred, golds[i]))
            total += 1
        if verbose_every and (bstart // batch_size) % verbose_every == 0:
            dt = time.time() - t0
            rate = total / dt if dt else 0
            print(
                f"  [{mode}] {total}/{n} acc={correct / total:.3f} "
                f"({correct}/{total}) elapsed={dt:.0f}s rate={rate:.2f} q/s",
                flush=True,
            )

    dt = time.time() - t0
    return {
        "mode": mode,
        "correct": correct,
        "total": total,
        "accuracy": correct / total if total else 0.0,
        "elapsed_s": dt,
        "qps": total / dt if dt else 0,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = full test set")
    ap.add_argument("--modes", default="baseline,q8,q4,q2")
    ap.add_argument("--out", default="/workspace/ziq_gsm8k_results.json")
    ap.add_argument("--max_new", type=int, default=384)
    ap.add_argument("--batch_size", type=int, default=8)
    args = ap.parse_args()

    print(f"[load] {MODEL_ID}", flush=True)
    tok = AutoTokenizer.from_pretrained(MODEL_ID)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID, dtype=torch.float16, device_map="cuda"
    )
    params_b = sum(p.numel() for p in model.parameters()) / 1e9
    print(
        f"[load] params={params_b:.3f}B "
        f"mem={torch.cuda.memory_allocated() / 1e9:.2f}GB",
        flush=True,
    )

    print("[data] gsm8k main/test", flush=True)
    ds = load_dataset("gsm8k", "main", split="test")
    if args.limit > 0:
        ds = ds.select(range(min(args.limit, len(ds))))
    print(f"[data] N={len(ds)}", flush=True)

    results = []
    for mode in args.modes.split(","):
        mode = mode.strip()
        print(f"\n==== mode={mode} ====", flush=True)
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        mem_before = torch.cuda.memory_allocated() / 1e9
        try:
            r = run_split(
                model, tok, ds, mode,
                max_new=args.max_new, batch_size=args.batch_size,
            )
            mem_peak = torch.cuda.max_memory_allocated() / 1e9
            r["mem_before_gb"] = round(mem_before, 2)
            r["mem_peak_gb"] = round(mem_peak, 2)
            results.append(r)
            print(
                f"  DONE {mode}: acc={r['accuracy']:.4f} "
                f"({r['correct']}/{r['total']}) "
                f"time={r['elapsed_s']:.0f}s "
                f"qps={r['qps']:.2f} "
                f"mem_peak={mem_peak:.2f}GB",
                flush=True,
            )
        except Exception as e:
            print(f"  FAIL {mode}: {type(e).__name__}: {e}", flush=True)
            results.append({"mode": mode, "error": f"{type(e).__name__}: {e}"})

        with open(args.out, "w") as f:
            json.dump({"model": MODEL_ID, "n": len(ds), "results": results}, f, indent=2)

    print("\n==== SUMMARY ====", flush=True)
    header = f"{'mode':<10} {'acc':>8} {'correct':>12} {'time(s)':>10} {'qps':>8} {'mem_peak':>10}"
    print(header)
    for r in results:
        if "error" in r:
            print(f"{r['mode']:<10} ERROR: {r['error']}")
            continue
        cstr = f"{r['correct']}/{r['total']}"
        print(
            f"{r['mode']:<10} {r['accuracy']:>8.4f} "
            f"{cstr:>12} "
            f"{r['elapsed_s']:>10.0f} {r['qps']:>8.2f} "
            f"{r['mem_peak_gb']:>8.2f}GB"
        )


if __name__ == "__main__":
    main()
