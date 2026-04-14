"""Fault-tolerant GuardBench runner — Granite Guardian 5B on GPU."""
import json
import sys
import traceback

sys.path.insert(0, "/workspace")

from granite_guardian_adapter import moderate  # noqa: E402

ALL_DATASETS = [
    "aart", "beaver_tails_330k", "bot_adversarial_dialogue",
    "cat_qa", "convabuse", "dices_350", "dices_990",
    "do_anything_now_questions", "do_not_answer", "dynahate",
    "harm_eval", "harmbench_behaviors", "harmful_q",
    "harmful_qa_questions", "harmful_qa", "hatecheck",
    "hatemoji_check", "hex_phi", "i_cona", "i_controversial",
    "i_malicious_instructions", "i_physical_safety",
    "jbb_behaviors", "malicious_instruct", "mitre",
    "niche_hazard_qa", "openai_moderation_dataset",
    "prosocial_dialog", "safe_text", "simple_safety_tests",
    "strong_reject_instructions", "tdc_red_teaming",
    "tech_hazard_qa", "toxic_chat", "toxigen", "xstest",
]

# Fresh run — old results used wrong template
DONE_DIR = "/workspace/gb_results_8b_v2"
DATASETS = ALL_DATASETS
print(f"Full run: {len(DATASETS)} datasets (official template)")

import guardbench  # noqa: E402

results = {}
failed = []
for i, ds in enumerate(DATASETS):
    print(f"\n{'=' * 60}")
    print(f"[{i + 1}/{len(DATASETS)}] {ds}")
    try:
        guardbench.benchmark(
            moderate=moderate,
            model_name="cascade-granite-guardian-8b",
            out_dir="/workspace/gb_results_8b_v2",
            batch_size=16,
            datasets=[ds],
            metrics=["f1", "recall", "precision", "auprc"],
        )
        results[ds] = "OK"
        print("  -> OK")
    except Exception as e:
        failed.append(ds)
        results[ds] = str(e)[:200]
        print(f"  -> FAILED: {str(e)[:200]}")
        traceback.print_exc()

print(f"\n{'=' * 60}")
print(
    f"DONE: {len(DATASETS) - len(failed)}/{len(DATASETS)} "
    "succeeded"
)
print(f"Failed: {failed}")
with open("/workspace/gb_summary_5b.json", "w") as f:
    json.dump(
        {"results": results, "failed": failed}, f, indent=2,
    )
