"""
Verify our scores against MTEB official framework.
Run ArguAna + FiQA with BGE-base / GTE-base via MTEB.
Compare with our pytrec_eval scores.

Usage: python run_mteb_verify.py
"""
import json

import mteb
from sentence_transformers import SentenceTransformer

# Models + datasets to verify
TESTS = [
    {
        "model": "BAAI/bge-base-en-v1.5",
        "datasets": ["ArguAna", "SciFact"],
    },
    {
        "model": "thenlper/gte-base",
        "datasets": ["FiQA", "SciFact"],
    },
]


def main():
    all_results = {}

    for test in TESTS:
        model_name = test["model"]
        print(f"\nLoading {model_name}...")
        model = SentenceTransformer(model_name)

        for ds_name in test["datasets"]:
            print(f"\n{'=' * 50}")
            print(f"{ds_name} + {model_name}")
            print(f"{'=' * 50}")

            tasks = mteb.get_tasks(
                tasks=[ds_name],
                task_types=["Retrieval"],
            )

            evaluation = mteb.MTEB(tasks=tasks)
            results = evaluation.run(
                model, output_folder="./mteb_results",
                eval_splits=["test"],
            )

            for task_result in results:
                scores = task_result.scores
                if "test" in scores:
                    for score_set in scores["test"]:
                        ndcg10 = score_set.get(
                            "ndcg_at_10", "?"
                        )
                        print(
                            f"  MTEB nDCG@10 = {ndcg10}"
                        )
                        key = (
                            f"{ds_name}__{model_name}"
                            .replace("/", "_")
                        )
                        all_results[key] = {
                            "model": model_name,
                            "dataset": ds_name,
                            "ndcg_at_10": ndcg10,
                        }

        del model

    # Summary
    sep = "=" * 50
    print(f"\n{sep}")
    print("MTEB VERIFICATION RESULTS")
    print(sep)

    # Our scores for comparison
    our_scores = {
        "ArguAna__BGE-base": 0.4573,
        "FiQA__GTE-base": 0.4082,
        "SciFact__BGE-base": 0.7573,
        "SciFact__GTE-base": "?",
    }

    for key, data in all_results.items():
        ds = data["dataset"]
        model_short = data["model"].split("/")[-1]
        ours_key = f"{ds}__{model_short}"
        ours = our_scores.get(ours_key, "?")
        print(
            f"  {ds:<12} {model_short:<20} "
            f"MTEB={data['ndcg_at_10']:.4f}  "
            f"Ours={ours}"
        )

    with open("mteb_verify_results.json", "w") as f:
        json.dump(all_results, f, indent=2)
    print("\nSaved to mteb_verify_results.json")


if __name__ == "__main__":
    main()
