"""Aegis agent evaluation runner — 跑 benchmark tasks.

接 agent loop (browser/windows Skill) 到 benchmark evaluation。
不需要 Docker — 用 live web 或 data: URL 跑。

Usage:
    python eval_runner.py --task "Go to google.com and search 'test'"
    python eval_runner.py --suite demo  # 跑內建 demo suite
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

_SKILLS = os.path.expanduser(r"~/.claude/skills")
sys.path.insert(0, os.path.join(_SKILLS, "browser"))
sys.path.insert(0, os.path.join(_SKILLS, "windows"))

import browser as b  # noqa: E402


def run_web_task(
    instruction: str,
    start_url: str = "about:blank",
    *,
    max_steps: int = 10,
    timeout_s: float = 60,
) -> dict:
    """Execute a web task using agent loop.

    Returns: {
        "instruction": str,
        "success": bool,
        "steps": int,
        "final_url": str,
        "final_title": str,
        "snapshots": [str, ...],
        "screenshot_path": str,
        "elapsed_s": float,
        "error": str | None,
    }
    """
    t0 = time.time()
    result: dict = {
        "instruction": instruction,
        "success": False,
        "steps": 0,
        "snapshots": [],
        "error": None,
    }

    try:
        b.launch(headless=True)  # no-op if already running
        b.goto(start_url, timeout=15000)
        time.sleep(1)
        b.wait_for_load(timeout=10000)

        # LLM planner (Sonnet + vision) if API key, else heuristic
        snap_init = b.snapshot()
        has_api = bool(os.environ.get("ANTHROPIC_API_KEY"))
        shot_b64 = None
        if has_api:
            try:
                import base64
                raw = b.screenshot()
                shot_b64 = base64.b64encode(raw).decode()
            except Exception:
                pass
        if has_api:
            try:
                goals = _llm_plan(
                    instruction, snap_init, shot_b64,
                )
                result["planner"] = "llm-sonnet"
            except Exception:
                goals = _parse_goals(instruction)
                result["planner"] = "heuristic-fallback"
        else:
            goals = _parse_goals(instruction)
            result["planner"] = "heuristic"
        # Also keep heuristic as retry backup
        heuristic_goals = _parse_goals(instruction)

        for i, goal in enumerate(goals):
            if time.time() - t0 > timeout_s:
                result["error"] = f"timeout after {timeout_s}s"
                break
            if i >= max_steps:
                result["error"] = f"max_steps {max_steps} reached"
                break

            snap = b.snapshot()
            result["snapshots"].append(snap[:500])

            try:
                _execute_goal(goal)
            except Exception:
                # LLM selector failed → retry with heuristic
                if (
                    heuristic_goals
                    and i < len(heuristic_goals)
                ):
                    _execute_goal(heuristic_goals[i])
                    result["planner"] += "+heuristic-retry"
                else:
                    raise
            result["steps"] += 1

            time.sleep(1)
            try:
                b.wait_for_load(timeout=10000)
            except Exception:
                time.sleep(2)

        # Final capture
        try:
            result["final_url"] = b.url()
        except Exception:
            result["final_url"] = "unknown"
        try:
            result["final_title"] = b.title()
        except Exception:
            result["final_title"] = "unknown"
        out = os.path.join(
            tempfile.gettempdir(),
            f"eval_{int(time.time())}.png",
        )
        try:
            b.screenshot(path=out, full_page=False)
            result["screenshot_path"] = out
        except Exception:
            result["screenshot_path"] = ""
        result["success"] = True
        result["elapsed_s"] = round(time.time() - t0, 1)

    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
        result["elapsed_s"] = round(time.time() - t0, 1)

    return result


def _llm_plan(
    instruction: str,
    snapshot: str,
    screenshot_b64: str | None = None,
) -> list[dict]:
    """Use Claude Sonnet to plan actions (ARIA + optional screenshot)."""
    import anthropic

    client = anthropic.Anthropic()
    text_part = (
        "You are a browser automation agent. "
        "Given the page structure and a task, "
        "return a JSON array of actions.\n\n"
        "Actions:\n"
        '- {"action":"goto","url":"..."}\n'
        '- {"action":"fill","selector":"CSS","value":"text"}\n'
        '- {"action":"click","selector":"CSS or text=X"}\n'
        '- {"action":"press","key":"Enter"}\n\n'
        "Selector rules:\n"
        "- Use ARIA roles: [role=search] input, "
        "button[aria-label=...]\n"
        "- Fallback: input[type=text], textarea, "
        "input:visible\n"
        "- For search boxes try: "
        "input[name=q], textarea[name=q], "
        "[role=search] input, "
        "input[aria-label*=earch]\n"
        "- Return ONLY JSON array\n\n"
        f"ARIA:\n```\n{snapshot[:2000]}\n```\n\n"
        f"Task: {instruction}"
    )
    content: list[dict] = []
    if screenshot_b64:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": screenshot_b64,
            },
        })
    content.append({"type": "text", "text": text_part})

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": content}],
    )
    text = resp.content[0].text.strip()
    # Parse JSON from response
    import re
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    return json.loads(text)


def _parse_goals(instruction: str) -> list[dict]:
    """Heuristic fallback (no API key needed)."""
    goals: list[dict] = []
    lower = instruction.lower()

    for word in instruction.split():
        if word.startswith("http") or "." in word:
            url = word if "://" in word else f"https://{word}"
            goals.append({"action": "goto", "url": url})
            break

    if "search" in lower or "find" in lower:
        for kw in ["search for ", "search '", 'search "',
                    "find ", "search "]:
            idx = lower.find(kw)
            if idx >= 0:
                query = instruction[idx + len(kw):].strip("'\"")
                goals.append({
                    "action": "fill",
                    "selector": (
                        "textarea[name=q], input[name=q], "
                        "input[type=search], "
                        "input[name=search], "
                        "input[aria-label*=earch], "
                        "input[placeholder*=earch]"
                    ),
                    "value": query,
                })
                goals.append({"action": "press", "key": "Enter"})
                break

    if "click" in lower:
        import re
        m = re.search(
            r"click (?:on )?['\"]?(.+?)['\"]?$", lower,
        )
        if m:
            goals.append({
                "action": "click",
                "selector": f"text={m.group(1)}",
            })

    if not goals:
        goals.append({"action": "goto", "url": instruction})

    return goals


def _execute_goal(goal: dict) -> None:
    action = goal["action"]
    if action == "goto":
        b.goto(goal["url"], timeout=15000)
    elif action == "fill":
        for sel in goal["selector"].split(", "):
            try:
                b.fill(sel.strip(), goal["value"], timeout=2000)
                return
            except Exception:
                continue
        # Fallback: press "/" (universal search shortcut) then retry
        try:
            b.press("/")
            time.sleep(0.5)
            for sel in goal["selector"].split(", "):
                try:
                    b.fill(sel.strip(), goal["value"], timeout=2000)
                    return
                except Exception:
                    continue
        except Exception:
            pass
        # Last resort: find any visible input
        try:
            b.fill("input:visible", goal["value"], timeout=2000)
            return
        except Exception:
            pass
        raise RuntimeError(
            f"no matching input for {goal['selector']}"
        )
    elif action == "press":
        b.press(goal.get("key", "Enter"))
    elif action == "click":
        b.click(goal["selector"], timeout=5000)


# -- Demo suite --
DEMO_TASKS = [
    {
        "id": "demo-1",
        "instruction": "Go to google.com and search for 'Aegis AI agent'",
        "start_url": "https://www.google.com",
    },
    {
        "id": "demo-2",
        "instruction": "Go to github.com and search for 'claude code'",
        "start_url": "https://github.com",
    },
    {
        "id": "demo-3",
        "instruction": "Go to en.wikipedia.org and search for 'artificial intelligence'",
        "start_url": "https://en.wikipedia.org",
    },
]


def run_suite(tasks: list[dict] | None = None) -> list[dict]:
    tasks = tasks or DEMO_TASKS
    b.launch(headless=True)
    results = []
    for task in tasks:
        print(f"\n--- {task['id']}: {task['instruction'][:60]} ---")
        r = run_web_task(
            task["instruction"],
            start_url=task.get("start_url", "about:blank"),
        )
        t = r.get("final_title", r.get("error", ""))[:40]
        print(
            f"  success={r['success']} steps={r['steps']} "
            f"elapsed={r['elapsed_s']}s "
            f"title={t!r}"
        )
        r["task_id"] = task["id"]
        results.append(r)
    return results


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser("eval_runner")
    ap.add_argument("--task", help="single task instruction")
    ap.add_argument(
        "--suite", choices=["demo"],
        help="run built-in suite",
    )
    args = ap.parse_args()

    if args.task:
        r = run_web_task(args.task)
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    elif args.suite == "demo":
        results = run_suite()
        print(f"\n=== {len(results)} tasks ===")
        passed = sum(1 for r in results if r["success"])
        print(f"passed: {passed}/{len(results)}")
        out = os.path.join(tempfile.gettempdir(), "eval_results.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print(f"results: {out}")
    else:
        ap.print_help()
