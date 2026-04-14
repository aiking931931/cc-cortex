"""Run remaining BEIR SciFact configs locally (sequential, one model at a time).

Already have:
  BM25 precision = 0.6654
  Dense:bge-m3 = 0.6437
  Hybrid:BM25+bge-m3 = 0.6928
  Hybrid+ms-marco-L6 = 0.6949

Still need:
  1. Dense:e5-large-v2
  2. Dense:bge-large-en-v1.5
  3. Hybrid:BM25+e5-large
  4. Hybrid:BM25+bge-large
  5. Reranker: bge-reranker-v2-m3 on best hybrid
"""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import time

PYTHON = sys.executable
RUNNER = os.path.join(os.path.dirname(__file__), "beir_runner.py")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")

# Configs to run (sequential - each unloads before next)
CONFIGS = [
    # 1. Dense only: e5-large-v2
    {
        "name": "Dense:e5-large-v2",
        "args": ["--dense-only", "--dense-model", "intfloat/e5-large-v2", "--no-reranker"],
    },
    # 2. Dense only: bge-large-en-v1.5
    {
        "name": "Dense:bge-large-en-v1.5",
        "args": ["--dense-only", "--dense-model", "BAAI/bge-large-en-v1.5", "--no-reranker"],
    },
    # 3. Hybrid: BM25 + e5-large (no reranker)
    {
        "name": "Hybrid:BM25+e5-large",
        "args": ["--dense-model", "intfloat/e5-large-v2", "--no-reranker"],
    },
    # 4. Hybrid: BM25 + bge-large (no reranker)
    {
        "name": "Hybrid:BM25+bge-large",
        "args": ["--dense-model", "BAAI/bge-large-en-v1.5", "--no-reranker"],
    },
    # 5. Hybrid: BM25 + e5-large + bge-reranker-v2-m3
    {
        "name": "Hybrid:BM25+e5-large+bge-reranker",
        "args": ["--dense-model", "intfloat/e5-large-v2",
                 "--reranker-model", "BAAI/bge-reranker-v2-m3"],
    },
    # 6. Hybrid: BM25 + bge-m3 + bge-reranker-v2-m3
    {
        "name": "Hybrid:BM25+bge-m3+bge-reranker",
        "args": ["--dense-model", "BAAI/bge-m3",
                 "--reranker-model", "BAAI/bge-reranker-v2-m3"],
    },
]


def run_config(config: dict) -> dict | None:
    """Run a single benchmark config as subprocess (isolates GPU memory)."""
    name = config["name"]
    args = config["args"]

    print(f"\n{'=' * 60}")
    print(f"Running: {name}")
    print(f"{'=' * 60}")

    cmd = [PYTHON, RUNNER, "--dataset", "scifact", "--profile", "precision"] + args

    t0 = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800,  # 30 min max per config
            cwd=os.path.dirname(RUNNER),
        )
        elapsed = time.time() - t0

        print(result.stdout[-500:] if len(result.stdout) > 500 else result.stdout)
        if result.returncode != 0:
            print(f"STDERR: {result.stderr[-300:]}")
            print(f"FAILED ({elapsed:.0f}s)")
            return None

        print(f"Completed in {elapsed:.0f}s")
        return {"name": name, "elapsed": elapsed, "status": "ok"}

    except subprocess.TimeoutExpired:
        print("TIMEOUT after 1800s")
        return {"name": name, "elapsed": 1800, "status": "timeout"}


def main():
    print("BEIR SciFact - Remaining Configs (Local GPU, Sequential)")
    print(f"Configs to run: {len(CONFIGS)}")
    print()

    results = []
    for i, config in enumerate(CONFIGS):
        print(f"\n[{i + 1}/{len(CONFIGS)}]")
        r = run_config(config)
        results.append(r)
        # Force garbage collection between runs
        gc.collect()

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for r in results:
        if r:
            print(f"  {r['name']}: {r['status']} ({r['elapsed']:.0f}s)")
        else:
            print("  FAILED")

    print(f"\nResults saved in: {RESULTS_DIR}")
    print("Combine with existing results for full ablation table.")


if __name__ == "__main__":
    main()
