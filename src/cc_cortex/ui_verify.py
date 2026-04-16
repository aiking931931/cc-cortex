"""cc_cortex.ui_verify — UI verification gate after deploy.

@module ui_verify
@responsibility Lock non-verify tools until UI screenshot confirms deploy
@dependencies cc_cortex.core.path_utils, cc_cortex.core.state_store,
    cc_cortex.guards.base
@exports UIVerifyGuard

9B Poka-Yoke pipeline (4 sub-tasks in one Guard):
  9B-1 UIChangeTracker:  PostToolUse — track UI file edits
  9B-2 DeployGate:       PostToolUse — deploy + ui_files → lock (verify_pending)
  9B-3 VerifyLock:       PreToolUse  — enforce state-appropriate tool access
  9B-4 VerifyOutcome:    PostToolUse — screenshot result → fix_mode or verified

Three-state verification cycle:
  verify_pending → (screenshot fails) → fix_mode → (re-deploy) → verify_pending
  verify_pending → (screenshot passes) → verified (unlocked)
  fix_mode allows Edit/Write on UI files + deploy commands for fix→retry cycle.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

from cc_cortex.core.path_utils import extract_file_path
from cc_cortex.core.state_store import StateStore
from cc_cortex.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

_NS = "ui_verify"

_UI_EXTENSIONS = frozenset({
    ".tsx", ".jsx", ".css", ".scss", ".less",
    ".html", ".vue", ".svelte",
})

_DEPLOY_PATTERNS = (
    "deploy.py", "deploy.sh", "deploy.ts",
    "npm run deploy", "vercel deploy", "vercel --prod",
    "netlify deploy",
)

_VERIFY_CMD_PATTERNS = (
    "screenshot", "playwright", "puppeteer",
    "curl localhost", "curl 127.0.0.1", "devtools",
)

# Max full fix→deploy→verify cycles before deadlock breaker releases
_MAX_CYCLES = 5


def _is_ui_file(path: str) -> bool:
    if not path:
        return False
    _, ext = os.path.splitext(path.lower())
    return ext in _UI_EXTENSIONS


def _is_deploy_command(tool_input: dict) -> bool:
    cmd = tool_input.get("command", "")
    return any(p in cmd for p in _DEPLOY_PATTERNS)


def _is_verify_action(tool_name: str, tool_input: dict) -> bool:
    if tool_name == "Bash":
        cmd = tool_input.get("command", "").lower()
        return any(p in cmd for p in _VERIFY_CMD_PATTERNS)
    if tool_name == "Read":
        path = tool_input.get("file_path", "").lower()
        return "screenshot" in path or "verify" in path
    return False


def _is_fix_action(tool_name: str, tool_input: dict) -> bool:
    """Fix-mode whitelist: Edit/Write UI files, deploy, or read-only tools."""
    if tool_name in ("Edit", "Write"):
        return _is_ui_file(extract_file_path(tool_input))
    if tool_name == "Bash":
        return _is_deploy_command(tool_input)
    return tool_name in ("Read", "Grep", "Glob", "Agent")


def _parse_screenshot_result(tool_result: str) -> tuple[int, int]:
    """Parse '📊 Total: X/Y passed' or JSON {passed, total}. Returns (passed, total)."""
    if not tool_result:
        return 0, 0
    m = re.search(r"Total:\s*(\d+)/(\d+)\s*passed", tool_result)
    if m:
        return int(m.group(1)), int(m.group(2))
    try:
        data = json.loads(tool_result)
        return data.get("passed", 0), data.get("total", 0)
    except (json.JSONDecodeError, TypeError):
        return 0, 0


def _has_failures(tool_result: str) -> bool:
    passed, total = _parse_screenshot_result(tool_result)
    if total > 0:
        return passed < total
    if not tool_result:
        return False
    lower = tool_result.lower()
    return any(s in lower for s in ("❌", "error", "failed", "timeout", "enoent"))


def _file_list_str(ui_files: list[str], limit: int = 5) -> str:
    lines = [f"  - {f}" for f in ui_files[:limit]]
    if len(ui_files) > limit:
        lines.append(f"  ...（共 {len(ui_files)} 個）")
    return "\n".join(lines)


# ── State-specific deny builders ──

def _get_screenshot_command() -> str:
    """Pick the best screenshot method for current environment.

    Priority:
    1. Playwright headless (non-intrusive, works in CI)
    2. System screenshot tool (windows Skill / mss / platform native)
    """
    playwright_script = os.path.join(
        os.environ.get("CLAUDE_PROJECT_DIR", ""),
        "scripts", "tools", "psyche-screenshot.js",
    )
    if os.path.isfile(playwright_script):
        return f"node {playwright_script}"
    return (
        "Take a screenshot of the deployed UI using the best "
        "available method: Playwright script or system screenshot tool."
    )


def _deny_verify_pending(ui_files: list[str]) -> GuardResult:
    cmd = _get_screenshot_command()
    return GuardResult.deny(
        "deploy 完成但 UI 未驗證 — 請先截圖確認",
        context=(
            "🔒 UI 驗證鎖（verify_pending）\n\n"
            f"已變更的 UI 檔案：\n{_file_list_str(ui_files)}\n\n"
            "下一步（自動選擇截圖方式）：\n"
            f"  {cmd}\n"
            "截圖通過 → 解鎖 | 截圖失敗 → 進入修復模式\n\n"
            "Playwright 優先（不影響用戶），"
            "系統截圖工具為 fallback（截桌面視窗）。"
        ),
    )


def _deny_fix_mode(fails: list[str]) -> GuardResult:
    info = "\n".join(f"  - {f}" for f in fails[:5]) if fails else "  （無詳細資訊）"
    return GuardResult.deny(
        "截圖驗證失敗 — 請修復 UI 問題後重新部署+驗證",
        context=(
            "🔧 UI 修復模式（fix_mode）\n\n"
            f"截圖失敗項：\n{info}\n\n"
            "允許：Edit/Write UI 檔 | deploy | Read/Grep | screenshot\n"
            "流程：修復 → 部署 → 截圖 → 通過才解鎖"
        ),
    )


class UIVerifyGuard(BaseGuard):
    """UI verification gate — three-state cycle after deploy.

    States: None → verify_pending → (pass: unlock | fail: fix_mode → re-deploy → ...)
    """

    name = "ui_verify"
    category = GuardCategory.QUALITY
    step_back_reason = "UI not verified after deploy"

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """9B-3 VerifyLock: enforce state-appropriate tool access."""
        if not ctx.cache_dir:
            return None
        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, "state", default={})
        phase = state.get("phase")
        if not phase:
            return None

        # Deadlock breaker
        if state.get("cycle_count", 0) >= _MAX_CYCLES:
            state["phase"] = None
            state["released_reason"] = "deadlock_breaker"
            state["released_at"] = time.time()
            store.write(_NS, "state", state)
            return GuardResult.allow(context="⚠ UI 驗證鎖自動釋放（已嘗試 5 輪）。請手動確認。")

        if _is_verify_action(ctx.tool_name, ctx.tool_input):
            return None

        if phase == "verify_pending":
            return _deny_verify_pending(state.get("ui_changed_files", []))

        if phase == "fix_mode":
            if _is_fix_action(ctx.tool_name, ctx.tool_input):
                return None
            return _deny_fix_mode(state.get("last_screenshot_fails", []))

        return None

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """9B-1 Track + 9B-2 Deploy gate + 9B-4 Verify outcome."""
        if not ctx.cache_dir:
            return None
        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, "state", default={})

        r = self._track_ui_edit(ctx, state, store)
        if r:
            return r
        r = self._handle_deploy(ctx, state, store)
        if r:
            return r
        return self._handle_verify_result(ctx, state, store)

    # ── PostToolUse sub-handlers ──

    def _track_ui_edit(
        self, ctx: GuardContext, state: dict, store: StateStore,
    ) -> Optional[GuardResult]:
        """9B-1: Track UI file changes + auto-trigger visual verification hint."""
        if ctx.tool_name not in ("Edit", "Write"):
            return None
        path = extract_file_path(ctx.tool_input)
        if not _is_ui_file(path):
            return None
        ui_files = state.get("ui_changed_files", [])
        if path not in ui_files:
            ui_files.append(path)
            state["ui_changed_files"] = ui_files

        # CBUA v2: auto-trigger visual verification hint after ≥3 UI edits
        ui_edit_count = state.get("ui_edit_count", 0) + 1
        state["ui_edit_count"] = ui_edit_count
        store.write(_NS, "state", state)  # single write for both mutations
        if ui_edit_count == 3 and not state.get("phase"):
            cmd = _get_screenshot_command()
            return GuardResult.allow(
                context=(
                    f"💡 已修改 {len(ui_files)} 個 UI 檔案。"
                    f"建議截圖驗證：{cmd}"
                ),
            )
        return None

    def _handle_deploy(
        self, ctx: GuardContext, state: dict, store: StateStore,
    ) -> Optional[GuardResult]:
        """9B-2: Deploy detected → verify_pending."""
        if ctx.tool_name != "Bash" or not _is_deploy_command(ctx.tool_input):
            return None
        if not state.get("ui_changed_files"):
            return None

        was_fix = state.get("phase") == "fix_mode"
        state["phase"] = "verify_pending"
        state["locked_at"] = time.time()
        state["cycle_count"] = (state.get("cycle_count", 0) + 1) if was_fix else 0
        store.write(_NS, "state", state)

        cycle = state["cycle_count"]
        tag = f"（修復循環第 {cycle} 輪）" if cycle > 0 else ""
        cmd = _get_screenshot_command()
        return GuardResult.allow(
            context=(
                f"🔒 UI 驗證鎖已啟動{tag} — "
                f"立即執行截圖驗證：{cmd}"
            ),
        )

    def _handle_verify_result(
        self, ctx: GuardContext, state: dict, store: StateStore,
    ) -> Optional[GuardResult]:
        """9B-4: Screenshot result → fix_mode or verified."""
        if state.get("phase") != "verify_pending":
            return None
        if ctx.tool_name != "Bash" or not _is_verify_action(ctx.tool_name, ctx.tool_input):
            return None

        result = ctx.tool_result or ""
        if _has_failures(result):
            fail_lines = [ln.strip() for ln in result.split("\n") if "❌" in ln][:10]
            state["phase"] = "fix_mode"
            state["last_screenshot_fails"] = fail_lines
            state["fix_entered_at"] = time.time()
            store.write(_NS, "state", state)
            return GuardResult.allow(
                context=(
                    f"❌ 截圖驗證失敗（{len(fail_lines)} 項）— 進入修復模式。"
                    "修復→部署→截圖→通過才解鎖。"
                ),
            )

        # All passed → unlock
        state["phase"] = None
        state["ui_changed_files"] = []
        state["released_reason"] = "verified"
        state["released_at"] = time.time()
        store.write(_NS, "state", state)
        return GuardResult.allow(context="✅ 截圖驗證全部通過 — UI 驗證鎖已釋放。")
