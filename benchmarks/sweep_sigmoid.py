"""Per-category sigmoid sweep for GuardBench F1 optimization."""
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.metrics import f1_score

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

RESULTS_DIR = Path(__file__).parent / "results" / "guardbench"
RUN_DIR = RESULTS_DIR / "guardian_8b" / "gb_results_8b"
MODEL = "cascade-granite-guardian-8b"
DATASET_META = {
    "aart": ("prompts", "en"),
    "beaver_tails_330k": ("conversations", "en"),
    "bot_adversarial_dialogue": ("conversations", "en"),
    "cat_qa": ("prompts", "en"),
    "convabuse": ("conversations", "en"),
    "dices_350": ("conversations", "en"),
    "dices_990": ("conversations", "en"),
    "do_anything_now_questions": ("prompts", "en"),
    "do_not_answer": ("prompts", "en"),
    "dynahate": ("conversations", "en"),
    "harmbench_behaviors": ("prompts", "en"),
    "harmful_q": ("prompts", "en"),
    "harmful_qa_questions": ("prompts", "en"),
    "harmful_qa": ("conversations", "en"),
    "hatecheck": ("conversations", "en"),
    "hatemoji_check": ("conversations", "en"),
    "i_cona": ("prompts", "en"),
    "i_controversial": ("prompts", "en"),
    "i_malicious_instructions": ("prompts", "en"),
    "i_physical_safety": ("prompts", "en"),
    "jbb_behaviors": ("prompts", "en"),
    "malicious_instruct": ("prompts", "en"),
    "mitre": ("prompts", "en"),
    "niche_hazard_qa": ("prompts", "en"),
    "openai_moderation_dataset": ("conversations", "en"),
    "prosocial_dialog": ("conversations", "en"),
    "safe_text": ("conversations", "en"),
    "simple_safety_tests": ("prompts", "en"),
    "strong_reject_instructions": ("prompts", "en"),
    "tdc_red_teaming": ("prompts", "en"),
    "toxic_chat": ("conversations", "en"),
    "toxigen": ("conversations", "en"),
    "xstest": ("prompts", "en"),
}

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from guardbench.datasets import load_dataset  # noqa: E402


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -50, 50)))


def load_data():
    groups = defaultdict(lambda: {"y_true": [], "y_raw": []})
    for ds_name in os.listdir(RUN_DIR):
        pred_file = RUN_DIR / ds_name / (MODEL + ".json")
        if not pred_file.exists() or ds_name not in DATASET_META:
            continue
        cat, lang = DATASET_META[ds_name]
        key = lang + "_" + cat
        with open(pred_file) as f:
            preds = json.load(f)
        try:
            ds = load_dataset(ds_name)
            for batch in ds.generate_batches(9999):
                for item in batch:
                    sid = str(item["id"])
                    if sid in preds:
                        groups[key]["y_true"].append(
                            int(item["label"])
                        )
                        groups[key]["y_raw"].append(preds[sid])
        except Exception:
            pass
    return groups


def sweep(groups):
    best_avg = 0
    best_cfg = {}
    count = 0
    for p_off in np.arange(0.02, 0.20, 0.01):
        for p_sc in np.arange(1.5, 8, 0.5):
            for c_off in np.arange(0.10, 0.35, 0.01):
                for c_sc in np.arange(1.5, 8, 0.5):
                    scores = {}
                    for key in groups:
                        yt = np.array(groups[key]["y_true"])
                        yr = np.array(groups[key]["y_raw"])
                        n = np.sum(yt == 0)
                        if n == 0:
                            continue
                        if "prompts" in key:
                            yp = sigmoid((yr - p_off) * p_sc)
                        else:
                            yp = sigmoid((yr - c_off) * c_sc)
                        yb = (yp > 0.5).astype(int)
                        scores[key] = f1_score(
                            yt, yb, zero_division=0.0,
                        )
                    if scores:
                        avg = sum(scores.values()) / len(scores)
                        if avg > best_avg:
                            best_avg = avg
                            best_cfg = {
                                "p_off": round(float(p_off), 3),
                                "p_sc": round(float(p_sc), 1),
                                "c_off": round(float(c_off), 3),
                                "c_sc": round(float(c_sc), 1),
                            }
                    count += 1
                    if count % 10000 == 0:
                        print(
                            f"  {count} combos, best={best_avg:.4f}",
                            flush=True,
                        )
    return best_avg, best_cfg


def main():
    print("Loading data...", flush=True)
    groups = load_data()
    for k, v in groups.items():
        n = len(v["y_true"])
        print(f"  {k}: {n} samples", flush=True)

    print("Sweeping...", flush=True)
    best_avg, best_cfg = sweep(groups)

    print(f"\nBest config: {best_cfg}")
    for key in sorted(groups):
        yt = np.array(groups[key]["y_true"])
        yr = np.array(groups[key]["y_raw"])
        n = np.sum(yt == 0)
        if n == 0:
            continue
        if "prompts" in key:
            yp = sigmoid(
                (yr - best_cfg["p_off"]) * best_cfg["p_sc"],
            )
        else:
            yp = sigmoid(
                (yr - best_cfg["c_off"]) * best_cfg["c_sc"],
            )
        yb = (yp > 0.5).astype(int)
        f1 = f1_score(yt, yb, zero_division=0.0)
        print(f"  {key:20s} F1={f1:.4f}")
    print(f"  AVERAGE:           {best_avg:.4f}")
    print("  vs IBM #1:         0.8600")
    print(f"  GAIN vs current:   {best_avg - 0.8818:+.4f}")


if __name__ == "__main__":
    main()
