"""GAIA ZIQ v2: buff-stack (web search base + strategy buffs).

v1 failed: routing away from web_search lost context (-15pp).
v2 fix: every task gets web_search FIRST, then stack buffs on top.

posterior = SPS(structure) * FTRL(outcome) per buff, not per strategy.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading

_SKILLS = os.path.expanduser(r"~/.claude/skills")
sys.path.insert(0, os.path.join(_SKILLS, "browser"))

import anthropic  # noqa: E402  (must follow sys.path injection above)
import browser as b_mod  # noqa: E402

CLIENT = anthropic.Anthropic()
FTRL_PATH = os.path.expanduser("~/.gaia_ftrl_v2.json")
BUFFS = ["file_read", "youtube", "code_exec", "deep_search", "llm_confidence"]


def _norm(s):
    t = sum(s.values())
    return {k: v / t for k, v in s.items()} if t else {k: 1 / len(s) for k in s}


def sps_buffs(q_raw, file_name):
    """Which buffs to activate (0-1 score each)."""
    q = q_raw.lower()
    scores = {k: 0.0 for k in BUFFS}
    if file_name:
        scores["file_read"] = 0.9
    if re.search(r"youtube\.com|youtu\.be", q):
        scores["youtube"] = 0.95
    calc_kw = [
        "calculate", "how many", "sum", "average", "ratio",
        "convert", "m^3", "percentage", "multiply",
    ]
    if any(k in q for k in calc_kw):
        scores["code_exec"] = 0.7
    if len(q_raw) > 300:
        scores["deep_search"] = 0.6
    if len(q_raw) < 120 and not file_name:
        scores["llm_confidence"] = 0.5
    if any(k in q for k in ["riddle", "puzzle", "logic", "game"]):
        scores["llm_confidence"] = 0.8
    return scores


class FTRLv2:
    def __init__(self, lr=0.3):
        self.z = {k: 0.0 for k in BUFFS}
        self.n = {k: 0.0 for k in BUFFS}
        self.lr = lr

    def gate(self, buff):
        if self.n[buff] == 0:
            return 0.5
        sign = -1 if self.z[buff] < 0 else 1
        return max(
            0.01,
            min(0.99, 0.5 - sign * self.z[buff] / (self.lr + math.sqrt(self.n[buff]))),
        )

    def update(self, buff, reward):
        g = -(reward - 0.5)
        self.z[buff] += g
        self.n[buff] += g * g

    def save(self):
        with open(FTRL_PATH, "w") as f:
            json.dump({"z": self.z, "n": self.n}, f)

    def load(self):
        if os.path.exists(FTRL_PATH):
            with open(FTRL_PATH) as f:
                d = json.load(f)
            self.z = d.get("z", self.z)
            self.n = d.get("n", self.n)


def active_buffs(sps, ftrl):
    """Activate buffs where SPS * FTRL_gate > 0.3."""
    out = []
    for buff in BUFFS:
        score = sps.get(buff, 0.0) * ftrl.gate(buff)
        if score > 0.3:
            out.append(buff)
    return out


def _haiku(prompt):
    return CLIENT.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=40,
        messages=[{"role": "user", "content": prompt}],
    ).content[0].text.strip().strip('"')


def _sonnet_final(question, context_parts):
    from gaia_runner import normalize_answer
    ctx = "\n\n".join(f"[{k}]\n{v}" for k, v in context_parts.items() if v)
    if not ctx:
        ctx = "(no context)"
    try:
        r = CLIENT.messages.create(
            model="claude-sonnet-4-6", max_tokens=1500,
            system="Solve step by step. Last line: FINAL ANSWER: <value>. Concise.",
            messages=[
                {
                    "role": "user",
                    "content": f"{ctx[:6000]}\n\nQ: {question}\n\nFINAL ANSWER:",
                },
            ],
        )
        raw = r.content[0].text if r.content else ""
    except Exception:
        return ""
    m = re.search(r"FINAL ANSWER:\s*(.+)", raw, re.I)
    if m:
        return normalize_answer(m.group(1))
    lines = [line.strip() for line in raw.strip().split("\n") if line.strip()]
    return normalize_answer(lines[-1]) if lines else ""


def solve(question, file_content="", file_name=""):
    """Buff-stack solver: always web_search + conditional buffs."""
    from gaia_runner import get_youtube_transcript, web_search
    sps = sps_buffs(question, file_name)
    # Always start with web search (base)
    ctx = {}
    sq = _haiku(f"Google search query: {question[:300]}")
    ctx["web"] = web_search(sq)

    # Stack buffs
    buffs = active_buffs(sps, FTRLv2())  # cold start = all above threshold
    if "deep_search" in buffs:
        sq2 = _haiku(f"Different search angle: {question[:300]}")
        ctx["deep"] = web_search(sq2, click_first=True)
    if "file_read" in buffs and file_content:
        ctx["file"] = file_content[:3000]
    if "youtube" in buffs:
        m = re.search(
            r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+", question,
        )
        if m:
            ctx["youtube"] = get_youtube_transcript(m.group(0))[:3000]
    if "code_exec" in buffs:
        code_r = CLIENT.messages.create(
            model="claude-sonnet-4-6", max_tokens=500,
            system="Write Python. Print ONLY answer. Stdlib. ```python...```",
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Context:\n{ctx.get('web', '')[:1500]}\n"
                        f"{file_content[:1000]}\n\nQ:{question}"
                    ),
                },
            ],
        ).content[0].text
        cm = re.search(r"```python\s*(.*?)```", code_r, re.DOTALL)
        if cm:
            try:
                r = subprocess.run(
                    [sys.executable, "-c", cm.group(1)],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0 and r.stdout.strip():
                    ctx["code_result"] = r.stdout.strip().split("\n")[-1]
            except Exception:
                pass
    if "llm_confidence" in buffs:
        # Try direct answer first, use if confident
        direct = _sonnet_final(question, {})
        if direct and len(direct) < 50:
            ctx["llm_direct"] = f"Direct answer (high confidence): {direct}"

    return _sonnet_final(question, ctx)


def train(n="all"):
    if not os.environ.get("HF_TOKEN"):
        raise RuntimeError(
            "HF_TOKEN env var is required to run gaia_ziq.train — "
            "set it to your Hugging Face access token before invoking.",
        )
    from datasets import load_dataset
    from gaia_runner import read_file
    from huggingface_hub import hf_hub_download
    ds = load_dataset(
        "gaia-benchmark/GAIA", name="2023_all", split="validation",
    )
    tasks = list(ds) if n == "all" else list(ds)[: int(n)]
    router = FTRLv2()
    router.load()
    b_mod.launch(headless=True)
    ok = tot = 0
    for i, task in enumerate(tasks):
        q = task["Question"]
        exp = task["Final answer"]
        fn = task.get("file_name", "")
        fc = ""
        fp = task.get("file_path", "")
        if fp:
            try:
                fc = read_file(
                    hf_hub_download(
                        "gaia-benchmark/GAIA", fp, repo_type="dataset",
                    ),
                )
            except Exception:
                pass
        sps = sps_buffs(q, fn)
        buffs_used = active_buffs(sps, router)
        box = [""]

        def _run(_q=q, _fc=fc, _fn=fn, _box=box):
            try:
                _box[0] = solve(_q, _fc, _fn)
            except Exception as e:
                print(f"  [ERR] {e}", flush=True)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=120)
        if t.is_alive():
            print("  [TIMEOUT]", flush=True)
            try:
                b_mod.close()
                b_mod.launch(headless=True)
            except Exception:
                pass
        ans = box[0]
        match = ans.strip().lower() == exp.strip().lower()
        for bf in buffs_used:
            router.update(bf, 1.0 if match else 0.0)
        tot += 1
        ok += int(match)
        tag = "PASS" if match else "FAIL"
        bstr = "+".join(buffs_used) if buffs_used else "base"
        print(
            f"[{i}/{len(tasks)}] L{task['Level']} {bstr:25} {tag} "
            f'got="{ans[:25]}" exp="{exp[:25]}"',
            flush=True,
        )
        if (i + 1) % 20 == 0:
            router.save()
            print(
                f"  --- {ok}/{tot} ({100 * ok // max(tot, 1)}%) ---",
                flush=True,
            )
    b_mod.close()
    router.save()
    print(f"\n=== {ok}/{tot} ({100 * ok // max(tot, 1)}%) ===")
    for bf in BUFFS:
        print(f"  {bf}: gate={router.gate(bf):.2f}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", nargs="?", const="20")
    a = ap.parse_args()
    if a.train:
        train(a.train if a.train != "all" else "all")
