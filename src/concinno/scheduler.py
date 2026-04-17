"""concinno.scheduler — Cross-platform scheduled task launcher for Claude Code.

@module scheduler
@responsibility Cross-platform scheduled task management: dedup, process collision
    detection, watchdog timeout, platform-native scheduling (Task Scheduler/launchd/cron),
    notifications, and log rotation.
@dependencies none (stdlib only)
@exports TaskConfig, LaunchResult, launch_task, install_schedule, uninstall_schedule, main

Ported from scheduled_launcher.ps1. Zero external dependencies (stdlib only).

Features:
  - Dedup: skip if ran too recently (configurable per-task intervals)
  - Process collision: skip if active Claude sessions exist
  - Watchdog: timeout + auto-kill
  - Platform detection: Windows (Task Scheduler), macOS (launchd), Linux (cron)
  - Notifications: Windows toast, macOS osascript, Linux notify-send
  - Log rotation: keep last 200 lines

Usage:
  python -m concinno.scheduler self-reflection
  python -m concinno.scheduler scavenger --config /path/to/schedule_config.json
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("concinno.scheduler")

SYSTEM = platform.system()

# ──────────────────────────────────────────────
# Task Definitions (defaults, overridden by config)
# ──────────────────────────────────────────────

DEFAULT_TASKS: dict[str, dict] = {
    "self-reflection": {
        "prompt_file": "self-reflection-prompt.txt",
        "model": "claude-sonnet-4-6",
        "log_name": "self-reflection.log",
        "allowed_tools": "Read,Edit,Write,Glob,Grep",
        "max_budget_usd": "0.50",
        "timeout_sec": 600,
        "min_interval_hours": 20,
    },
    "scavenger": {
        "prompt_file": "scavenger-prompt.txt",
        "model": "claude-sonnet-4-6",
        "log_name": "scavenger.log",
        "allowed_tools": "Read,Edit,Write,Glob,Grep,Bash",
        "max_budget_usd": "1.00",
        "timeout_sec": 900,
        "min_interval_hours": 68,
    },
    "weekly-research": {
        "prompt_file": "weekly-research-prompt.txt",
        "model": "claude-sonnet-4-6",
        "log_name": "weekly-research.log",
        "allowed_tools": "Read,Edit,Write,Glob,Grep,WebSearch",
        "max_budget_usd": "1.50",
        "timeout_sec": 600,
        "min_interval_hours": 160,
    },
    "weekly_evolve": {
        "prompt_file": "weekly-evolve-prompt.txt",
        "model": "claude-sonnet-4-6",
        "log_name": "weekly-evolve.log",
        "allowed_tools": "Read,Edit,Write,Glob,Grep,WebSearch,Bash",
        "max_budget_usd": "2.00",
        "timeout_sec": 900,
        "min_interval_hours": 168,
    },
    "cleanup": {
        "prompt_file": "cleanup-prompt.txt",
        "model": "claude-sonnet-4-6",
        "log_name": "cleanup.log",
        "allowed_tools": "Read,Glob,Grep,Bash",
        "max_budget_usd": "0.30",
        "timeout_sec": 300,
        "min_interval_hours": 24,
    },
}


@dataclass
class TaskConfig:
    name: str
    prompt_file: str
    model: str
    log_name: str
    allowed_tools: str
    max_budget_usd: str
    timeout_sec: int
    min_interval_hours: float
    enabled: bool = True
    report_enabled: bool = True


@dataclass
class LaunchResult:
    task_name: str
    success: bool = False
    exit_code: int = -1
    duration_sec: float = 0
    skipped: bool = False
    skip_reason: str = ""
    output_preview: str = ""
    error: str = ""


# ──────────────────────────────────────────────
# Config loading
# ──────────────────────────────────────────────


def load_task_config(
    task_name: str,
    config_path: Optional[str] = None,
    hooks_dir: Optional[str] = None,
) -> Optional[TaskConfig]:
    """Load task configuration from defaults + schedule_config.json overrides."""
    if task_name not in DEFAULT_TASKS:
        return None

    defaults = DEFAULT_TASKS[task_name].copy()
    report_enabled = True

    # Override from config file
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
            task_cfg = cfg.get("tasks", {}).get(task_name, {})
            if task_cfg.get("enabled") is False:
                tc = TaskConfig(name=task_name, **defaults)
                tc.enabled = False
                return tc
            for key in ("model", "max_budget_usd", "timeout_sec"):
                if key in task_cfg and task_cfg[key]:
                    defaults[key] = task_cfg[key]
            report_enabled = task_cfg.get("report_enabled", True)
        except Exception:
            pass

    tc = TaskConfig(name=task_name, **defaults)
    tc.report_enabled = report_enabled
    return tc


def _check_dedup(
    task_name: str,
    min_interval_hours: float,
    config_path: Optional[str],
) -> Optional[str]:
    """Check if task ran too recently. Returns skip reason or None."""
    if not config_path or not os.path.exists(config_path):
        return None

    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        last_run = cfg.get("tasks", {}).get(task_name, {}).get("last_run_timestamp")
        if last_run:
            last_dt = datetime.fromisoformat(last_run)
            hours_since = (datetime.now() - last_dt).total_seconds() / 3600
            if hours_since < min_interval_hours:
                return f"ran {hours_since:.1f}h ago (min interval: {min_interval_hours}h)"
    except Exception:
        pass
    return None


def _check_active_sessions() -> Optional[str]:
    """Check if any active Claude sessions exist. Returns skip reason or None."""
    try:
        if SYSTEM == "Windows":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq claude.exe", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if "claude.exe" in out.stdout:
                return "active Claude session detected"
        else:
            out = subprocess.run(
                ["pgrep", "-x", "claude"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                return "active Claude session detected"
    except Exception:
        pass
    return None


def _update_last_run(task_name: str, config_path: str) -> None:
    """Update last_run_timestamp in schedule_config.json."""
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        if "tasks" not in cfg:
            cfg["tasks"] = {}
        if task_name not in cfg["tasks"]:
            cfg["tasks"][task_name] = {}
        cfg["tasks"][task_name]["last_run_timestamp"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning("Failed to update last_run_timestamp: %s", e)


# ──────────────────────────────────────────────
# Claude CLI execution
# ──────────────────────────────────────────────


def _find_claude_cli() -> Optional[str]:
    """Find the claude CLI binary."""
    claude = shutil.which("claude")
    if claude:
        return claude

    # Windows: check npm global bin
    if SYSTEM == "Windows":
        npm_bin = os.path.join(os.environ.get("APPDATA", ""), "npm")
        for ext in (".cmd", ".ps1", ""):
            candidate = os.path.join(npm_bin, f"claude{ext}")
            if os.path.exists(candidate):
                return candidate

    return None


def _find_git_bash() -> Optional[str]:
    """Find git bash for Windows (needed by Claude CLI).

    Search order:
    1. CLAUDE_CODE_GIT_BASH_PATH env var (explicit override)
    2. Derive from `where git` (works regardless of install location)
    3. Common hardcoded paths as fallback
    """
    if SYSTEM != "Windows":
        return None

    # 1. Explicit env var
    env_path = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    # 2. Derive from `where git` — handles any install location
    try:
        result = subprocess.run(
            ["where", "git"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                git_exe = line.strip()
                # git.exe is typically at <root>/cmd/git.exe or <root>/mingw64/bin/git.exe
                # bash.exe is at <root>/bin/bash.exe
                git_dir = os.path.dirname(os.path.dirname(git_exe))
                bash_candidate = os.path.join(git_dir, "bin", "bash.exe")
                if os.path.isfile(bash_candidate):
                    return bash_candidate
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # 3. Hardcoded fallback
    fallback_paths = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    for p in fallback_paths:
        if os.path.isfile(p):
            return p
    return None


def launch_task(
    task: TaskConfig,
    work_dir: str,
    hooks_dir: str,
    config_path: Optional[str] = None,
) -> LaunchResult:
    """Launch a Claude Code scheduled task.

    Args:
        task: Task configuration.
        work_dir: Working directory for Claude.
        hooks_dir: Directory containing prompt files.
        config_path: Path to schedule_config.json for dedup.

    Returns:
        LaunchResult with execution details.
    """
    result = LaunchResult(task_name=task.name)

    # Pre-checks
    if not task.enabled:
        result.skipped = True
        result.skip_reason = "disabled in config"
        return result

    skip = _check_dedup(task.name, task.min_interval_hours, config_path)
    if skip:
        result.skipped = True
        result.skip_reason = skip
        return result

    skip = _check_active_sessions()
    if skip:
        result.skipped = True
        result.skip_reason = skip
        return result

    # Read prompt
    prompt_path = os.path.join(hooks_dir, task.prompt_file)
    if not os.path.exists(prompt_path):
        result.error = f"Prompt not found: {prompt_path}"
        return result

    with open(prompt_path, encoding="utf-8") as f:
        prompt = f.read()

    # Find Claude CLI
    claude_cli = _find_claude_cli()
    if not claude_cli:
        result.error = "claude CLI not found in PATH"
        return result

    # Build command
    cli_args = [
        "--print",
        "--dangerously-skip-permissions",
        "--model",
        task.model,
        "--max-budget-usd",
        task.max_budget_usd,
    ]
    if task.allowed_tools:
        cli_args.extend(["--allowedTools", task.allowed_tools])

    # Platform-specific execution
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    git_bash = _find_git_bash()
    if git_bash:
        env["CLAUDE_CODE_GIT_BASH_PATH"] = git_bash

    # Use cmd.exe on Windows (claude resolves to claude.cmd)
    if SYSTEM == "Windows":
        cmd = ["cmd.exe", "/c", "claude"] + cli_args
    else:
        cmd = [claude_cli] + cli_args

    logger.info(
        "Starting %s (model=%s, budget=%s, timeout=%ds)",
        task.name,
        task.model,
        task.max_budget_usd,
        task.timeout_sec,
    )

    start_time = time.time()

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=work_dir,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if SYSTEM == "Windows" else 0,
        )

        try:
            stdout_bytes, stderr_bytes = proc.communicate(
                input=prompt.encode("utf-8"),
                timeout=task.timeout_sec,
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_bytes, stderr_bytes = proc.communicate()
            result.error = f"timeout after {task.timeout_sec}s"

        result.exit_code = proc.returncode
        result.duration_sec = time.time() - start_time

        stdout_str = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_str = stderr_bytes.decode("utf-8", errors="replace").strip()

        # Claude --print outputs to stdout, but on some platforms it may go to stderr
        output = stdout_str or stderr_str
        if output:
            result.output_preview = output[-3000:]
        if stderr_str and not stdout_str and not result.error:
            # stderr was used as output fallback, don't treat as error
            pass
        elif stderr_str and not result.error:
            result.error = stderr_str[:1000]

        result.success = result.exit_code == 0

        # Update timestamp on success
        if result.success and config_path:
            _update_last_run(task.name, config_path)

    except Exception as e:
        result.error = str(e)
        result.duration_sec = time.time() - start_time

    return result


# ──────────────────────────────────────────────
# Notifications (cross-platform)
# ──────────────────────────────────────────────


def _notify(title: str, body: str) -> None:
    """Send a desktop notification via concinno.core.notify (best-effort)."""
    try:
        from concinno.core.notify import show_toast

        show_toast(title, body, tag="concinno-scheduler", group="concinno")
    except Exception:
        pass


def _extract_report_from_log(log_path: str) -> str:
    """Extract the latest report content from the task log file."""
    if not os.path.exists(log_path):
        return ""
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        # Find OUTPUT section — log format: [timestamp][OUTPUT] content
        output_lines: list[str] = []
        capturing = False
        for line in reversed(lines):
            if "[INFO] ===" in line and "completed" in line:
                break
            if "[OUTPUT]" in line or capturing:
                capturing = True
                # Strip log prefix: [timestamp][LEVEL]
                content = line
                for tag in ("[OUTPUT]", "[INFO]"):
                    if tag in content:
                        content = content.split(tag, 1)[-1]
                output_lines.append(content.strip())
        output_lines.reverse()
        return "\n".join(output_lines).strip()
    except Exception:
        return ""


def _show_report(task_name: str, report_text: str, log_path: str) -> None:
    """Save report to file and open it in default viewer (popup window)."""
    report_dir = os.path.join(os.path.expanduser("~"), ".claude", "logs", "reports")
    os.makedirs(report_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    report_file = os.path.join(report_dir, f"{task_name}_{ts}.md")

    # Build report content
    header = f"# {task_name} 排程報告\n\n"
    header += f"**時間**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n---\n\n"
    content = report_text or _extract_report_from_log(log_path)

    if not content:
        content = f"任務 {task_name} 已完成，但無報告輸出。\n詳見日誌：{log_path}"

    with open(report_file, "w", encoding="utf-8") as f:
        f.write(header + content + "\n")

    # Open report file in default viewer
    try:
        if SYSTEM == "Windows":
            os.startfile(report_file)  # type: ignore[attr-defined]
        elif SYSTEM == "Darwin":
            subprocess.Popen(["open", report_file])
        else:
            subprocess.Popen(["xdg-open", report_file])
    except Exception:
        logger.warning("Failed to open report: %s", report_file)


# ──────────────────────────────────────────────
# Log rotation
# ──────────────────────────────────────────────


def _rotate_log(log_path: str, keep_lines: int = 200) -> None:
    """Keep only the last N lines of a log file."""
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > keep_lines * 2:
            with open(log_path, "w", encoding="utf-8") as f:
                f.writelines(lines[-keep_lines:])
    except Exception:
        pass


# ──────────────────────────────────────────────
# Platform schedule installers
# ──────────────────────────────────────────────


def install_schedule(
    task_name: str,
    interval_minutes: int = 15,
    work_dir: Optional[str] = None,
) -> str:
    """Install a recurring schedule for a task. Returns status message."""
    python = sys.executable
    module_cmd = f"{python} -m concinno.scheduler {task_name}"

    if SYSTEM == "Windows":
        return _install_windows_schedule(task_name, module_cmd, interval_minutes)
    elif SYSTEM == "Darwin":
        return _install_launchd(task_name, module_cmd, interval_minutes, work_dir)
    else:
        return _install_cron(task_name, module_cmd, interval_minutes, work_dir)


def _install_windows_schedule(task_name: str, cmd: str, interval_minutes: int) -> str:
    """Install Windows Task Scheduler task."""
    sched_name = f"CC-Cortex-{task_name}"
    try:
        subprocess.run(
            [
                "schtasks",
                "/create",
                "/tn",
                sched_name,
                "/tr",
                cmd,
                "/sc",
                "MINUTE",
                "/mo",
                str(interval_minutes),
                "/f",
            ],
            capture_output=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return f"Windows Task Scheduler: {sched_name} every {interval_minutes}min"
    except Exception as e:
        return f"Failed: {e}"


def _install_launchd(
    task_name: str, cmd: str, interval_minutes: int, work_dir: Optional[str]
) -> str:
    """Install macOS launchd plist."""
    label = f"com.concinno.{task_name}"
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"

    parts = cmd.split()
    program_args = "\n".join(f"    <string>{p}</string>" for p in parts)

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}
    </array>
    <key>StartInterval</key>
    <integer>{interval_minutes * 60}</integer>
    <key>RunAtLoad</key>
    <true/>
    {f"<key>WorkingDirectory</key><string>{work_dir}</string>" if work_dir else ""}
    <key>StandardOutPath</key>
    <string>{Path.home() / ".claude" / "logs" / f"{task_name}.log"}</string>
    <key>StandardErrorPath</key>
    <string>{Path.home() / ".claude" / "logs" / f"{task_name}-error.log"}</string>
</dict>
</plist>"""

    try:
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist)
        subprocess.run(["launchctl", "load", str(plist_path)], timeout=10)
        return f"launchd: {label} every {interval_minutes}min"
    except Exception as e:
        return f"Failed: {e}"


def _install_cron(task_name: str, cmd: str, interval_minutes: int, work_dir: Optional[str]) -> str:
    """Install Linux cron job."""
    cron_comment = f"# concinno-{task_name}"
    if work_dir:
        cron_line = f"*/{interval_minutes} * * * * cd {work_dir} && {cmd}"
    else:
        cron_line = f"*/{interval_minutes} * * * * {cmd}"

    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        lines = existing.stdout.strip().splitlines() if existing.returncode == 0 else []

        # Remove old entry
        lines = [ln for ln in lines if f"concinno-{task_name}" not in ln]

        # Add new
        lines.append(cron_comment)
        lines.append(cron_line)

        proc = subprocess.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return f"cron: every {interval_minutes}min"
        return f"Failed: {proc.stderr}"
    except Exception as e:
        return f"Failed: {e}"


def uninstall_schedule(task_name: str) -> str:
    """Remove a scheduled task. Returns status message."""
    if SYSTEM == "Windows":
        sched_name = f"CC-Cortex-{task_name}"
        try:
            subprocess.run(
                ["schtasks", "/delete", "/tn", sched_name, "/f"],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return f"Removed: {sched_name}"
        except Exception as e:
            return f"Failed: {e}"

    elif SYSTEM == "Darwin":
        label = f"com.concinno.{task_name}"
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        try:
            subprocess.run(["launchctl", "unload", str(plist_path)], timeout=10)
            plist_path.unlink(missing_ok=True)
            return f"Removed: {label}"
        except Exception as e:
            return f"Failed: {e}"

    else:
        try:
            existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
            if existing.returncode != 0:
                return "No crontab found"
            lines = [
                ln for ln in existing.stdout.splitlines() if f"concinno-{task_name}" not in ln
            ]
            subprocess.run(
                ["crontab", "-"],
                input="\n".join(lines) + "\n",
                capture_output=True,
                text=True,
                timeout=5,
            )
            return f"Removed cron: concinno-{task_name}"
        except Exception as e:
            return f"Failed: {e}"


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────


def main() -> None:
    """CLI: python -m concinno.scheduler <task_name> [options]"""
    import argparse

    parser = argparse.ArgumentParser(description="CC Cortex Scheduled Task Launcher")
    parser.add_argument("task", nargs="?", help="Task name to run")
    parser.add_argument("--config", help="Path to schedule_config.json")
    parser.add_argument("--hooks-dir", help="Directory containing prompt files")
    parser.add_argument("--work-dir", help="Working directory for Claude")
    parser.add_argument(
        "--install",
        metavar="TASK",
        help="Install recurring schedule for a task",
    )
    parser.add_argument(
        "--uninstall",
        metavar="TASK",
        help="Remove recurring schedule for a task",
    )
    parser.add_argument("--interval", type=int, default=15, help="Interval in minutes")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s][%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Schedule management
    if args.install:
        msg = install_schedule(args.install, args.interval, args.work_dir)
        print(msg)
        return
    if args.uninstall:
        msg = uninstall_schedule(args.uninstall)
        print(msg)
        return

    # Run task
    if not args.task:
        parser.print_help()
        print(f"\nAvailable tasks: {', '.join(DEFAULT_TASKS.keys())}")
        sys.exit(1)

    work_dir = args.work_dir or os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    hooks_dir = args.hooks_dir or os.path.join(os.path.expanduser("~"), ".claude", "hooks")
    config_path = args.config or os.path.join(hooks_dir, "schedule_config.json")

    # Setup log file
    log_dir = os.path.join(os.path.expanduser("~"), ".claude", "logs")
    os.makedirs(log_dir, exist_ok=True)

    task_cfg = load_task_config(args.task, config_path, hooks_dir)
    if not task_cfg:
        print(f"Unknown task: {args.task}")
        print(f"Available: {', '.join(DEFAULT_TASKS.keys())}")
        sys.exit(1)

    # Add file handler
    log_path = os.path.join(log_dir, task_cfg.log_name)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(fh)

    # Launch
    result = launch_task(task_cfg, work_dir, hooks_dir, config_path)

    if result.skipped:
        logger.info("SKIP %s: %s", result.task_name, result.skip_reason)
    elif result.error and not result.success:
        logger.error("FAIL %s: %s", result.task_name, result.error)
        _notify(f"[FAIL] {result.task_name}", result.error[:120])
    else:
        duration_min = result.duration_sec / 60
        logger.info(
            "DONE %s: exit=%d, duration=%.0fs",
            result.task_name,
            result.exit_code,
            result.duration_sec,
        )

        # Log full output
        if result.output_preview:
            for line in result.output_preview.splitlines():
                logger.info("[OUTPUT] %s", line)

        # Always notify on success
        summary = f"完成 ({duration_min:.1f}min)"
        if result.output_preview:
            first_lines = result.output_preview.split("\n")[:3]
            summary = " ".join(first_lines)[:120]
        _notify(f"[OK] {result.task_name}", summary)

        # Show full report popup if report_enabled
        if task_cfg.report_enabled:
            _show_report(result.task_name, result.output_preview, log_path)

    # Rotate log
    _rotate_log(log_path)


if __name__ == "__main__":
    main()
