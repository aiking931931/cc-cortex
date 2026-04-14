"""
Step 1: Encode all Batch B corpora (GPU machine)
Usage: python step1_encode_gpu.py [--cache-dir ./cache]
Output: .npz files in cache-dir, plus doc_ids JSON
Transfer cache-dir to CPU machine for Step 2.
"""

import argparse
import json
import os
import time

import numpy as np
import torch
from beir import util
from beir.datasets.data_loader import GenericDataLoader
from sentence_transformers import SentenceTransformer

MODEL_ID = "intfloat/e5-base-unsupervised"
PREFIX_D = "passage: "
BEIR_BASE = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets"
CQA_FORUMS = [
    "android", "english", "gaming", "gis", "mathematica",
    "physics", "programmers", "stats", "tex",
    "unix", "webmasters", "wordpress",
]


def encode_corpus(model, corpus, ds_name, cache_dir, device):
    cache_path = os.path.join(cache_dir, f"{ds_name}_embs.npz")
    ckpt_path = os.path.join(cache_dir, f"{ds_name}_embs.ckpt.npz")
    ids_path = os.path.join(cache_dir, f"{ds_name}_doc_ids.json")

    doc_id_list = list(corpus.keys())

    if os.path.exists(cache_path) and os.path.exists(ids_path):
        data = np.load(cache_path)
        print(f"  Cache hit: {data['embs'].shape}")
        return

    texts = [
        f"{PREFIX_D}{corpus[did].get('title', '')} {corpus[did].get('text', '')}".strip()
        for did in doc_id_list
    ]

    start_idx = 0
    all_embs = []
    if os.path.exists(ckpt_path):
        ckpt = np.load(ckpt_path)
        start_idx = int(ckpt["done"])
        all_embs = [ckpt["embs"]]
        print(f"  Resuming from {start_idx}/{len(texts)}")

    batch_size = 512 if "cuda" in device else 64
    t0 = time.time()
    for i in range(start_idx, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        embs = model.encode(
            batch, normalize_embeddings=True, show_progress_bar=False, batch_size=256
        )
        all_embs.append(embs)
        done = min(i + batch_size, len(texts))
        if done % 5000 < batch_size or done == len(texts):
            partial = np.vstack(all_embs)
            np.savez_compressed(ckpt_path, embs=partial, done=done)
            elapsed = time.time() - t0
            speed = (done - start_idx) / elapsed if elapsed > 0 else 0
            eta = (len(texts) - done) / speed if speed > 0 else 0
            print(f"  {done}/{len(texts)} ({speed:.0f} d/s, ETA {eta:.0f}s)")

    passage_embs = np.vstack(all_embs)
    np.savez_compressed(cache_path, embs=passage_embs)
    with open(ids_path, "w") as f:
        json.dump(doc_id_list, f)
    if os.path.exists(ckpt_path):
        os.remove(ckpt_path)
    total = time.time() - t0
    print(f"  Done: {passage_embs.shape} in {total:.0f}s ({total / 60:.1f}min)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default="./cache")
    parser.add_argument("--data-dir", default="./datasets")
    args = parser.parse_args()

    os.makedirs(args.cache_dir, exist_ok=True)
    os.makedirs(args.data_dir, exist_ok=True)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")
    if device == "cuda":
        print(f"GPU: {torch.cuda.get_device_name(0)}")

    print(f"\nLoading {MODEL_ID}...")
    model = SentenceTransformer(MODEL_ID, device=device)

    datasets = {
        "trec-covid": f"{BEIR_BASE}/trec-covid.zip",
        "webis-touche2020": f"{BEIR_BASE}/webis-touche2020.zip",
        "quora": f"{BEIR_BASE}/quora.zip",
    }

    for ds_name, url in datasets.items():
        print(f"\n{'=' * 50}\n{ds_name.upper()}\n{'=' * 50}")
        data_path = os.path.join(args.data_dir, ds_name)
        if not os.path.isdir(data_path):
            data_path = util.download_and_unzip(url, args.data_dir)
        corpus, _, _ = GenericDataLoader(data_path).load(split="test")
        print(f"  {len(corpus)} docs")
        encode_corpus(model, corpus, ds_name, args.cache_dir, device)

    cqa_path = os.path.join(args.data_dir, "cqadupstack")
    if not os.path.isdir(cqa_path):
        print("\nDownloading cqadupstack (4.98 GB)...")
        cqa_path = util.download_and_unzip(f"{BEIR_BASE}/cqadupstack.zip", args.data_dir)

    for forum in CQA_FORUMS:
        print(f"\n{'=' * 50}\nCQA/{forum.upper()}\n{'=' * 50}")
        forum_path = os.path.join(cqa_path, forum)
        corpus, _, _ = GenericDataLoader(forum_path).load(split="test")
        print(f"  {len(corpus)} docs")
        encode_corpus(model, corpus, f"cqa_{forum}", args.cache_dir, device)

    files = [f for f in os.listdir(args.cache_dir) if f.endswith("_embs.npz")]
    total_size = sum(os.path.getsize(os.path.join(args.cache_dir, f)) for f in files)
    print(f"\n{'=' * 50}")
    print(f"DONE: {len(files)} cache files, {total_size / 1e9:.2f} GB")
    print(f"Transfer {args.cache_dir}/ to CPU machine for step2_evaluate_cpu.py")


if __name__ == "__main__":
    main()
