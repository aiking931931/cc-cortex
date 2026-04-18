"""concinno.tasks.weekly_evolve — Weekly evolution digest task.

@module weekly_evolve
@responsibility Register and run a weekly Claude Code session that researches
    recent AI agent / LLM / Claude Code developments, produces a digest, and
    optionally flags architecture-impacting changes.
@dependencies concinno.scheduler (TaskConfig, launch_task, install_schedule)
@exports TASK_CONFIG, render_prompt, cmd_evolve
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_TZ = timezone(timedelta(hours=8))

# ── Task configuration ──────────────────────────

TASK_CONFIG = {
    "name": "weekly_evolve",
    "prompt_file": "weekly-evolve-prompt.txt",
    "model": "claude-sonnet-4-6",
    "log_name": "weekly-evolve.log",
    "allowed_tools": "Read,Edit,Write,Glob,Grep,WebSearch,Bash",
    "max_budget_usd": "2.00",
    "timeout_sec": 900,
    "min_interval_hours": 168,  # 7 days = 10080 min
}

_PROMPT_TEMPLATE = """\
你是 AI King 的進化引擎。每週自動執行：
1. WebSearch 最近 7 天 AI agent / LLM / Claude Code 相關新聞 + 論文
2. 讀 Skill 社群（clawsights.com / github.com/anthropics/claude-code 新 issues/PRs）
3. 整理 ≤20 條摘要寫入 _AI_BRAIN/01_Memory/evolution/weekly_digest_{date}.md
4. 若有重大更新影響 CCC/Aegis 架構 → 標 ⚠️ 需要用戶確認
5. 完成後 git add -A && git commit -m "auto: weekly evolve digest {date}"

日期：{date}
"""


def render_prompt(date: str | None = None) -> str:
    """Render the weekly evolve prompt with the current date."""
    if date is None:
        date = datetime.now(_TZ).strftime("%Y-%m-%d")
    return _PROMPT_TEMPLATE.format(date=date)
