"""Tests for ui_verify — UI verification gate (three-state cycle)."""

import pytest

from concinno.core.state_store import StateStore
from concinno.guards.base import GuardAction, GuardContext
from concinno.ui_verify import (
    UIVerifyGuard,
    _has_failures,
    _is_deploy_command,
    _is_fix_action,
    _is_ui_file,
    _is_verify_action,
    _parse_screenshot_result,
)

# ── Helpers ────────────────────────────────────────────────


class TestIsUiFile:
    def test_tsx(self):
        assert _is_ui_file("src/App.tsx")

    def test_css(self):
        assert _is_ui_file("styles/main.css")

    def test_html(self):
        assert _is_ui_file("index.html")

    def test_vue(self):
        assert _is_ui_file("components/Header.vue")

    def test_svelte(self):
        assert _is_ui_file("routes/+page.svelte")

    def test_python(self):
        assert not _is_ui_file("main.py")

    def test_empty(self):
        assert not _is_ui_file("")

    def test_scss(self):
        assert _is_ui_file("theme.scss")


class TestIsDeployCommand:
    def test_deploy_py(self):
        assert _is_deploy_command({"command": "python deploy.py"})

    def test_npm_deploy(self):
        assert _is_deploy_command({"command": "npm run deploy"})

    def test_vercel(self):
        assert _is_deploy_command({"command": "vercel --prod"})

    def test_regular_command(self):
        assert not _is_deploy_command({"command": "npm test"})

    def test_empty(self):
        assert not _is_deploy_command({"command": ""})


class TestIsVerifyAction:
    def test_screenshot_bash(self):
        assert _is_verify_action("Bash", {"command": "node screenshot.js"})

    def test_playwright(self):
        assert _is_verify_action("Bash", {"command": "npx playwright test"})

    def test_read_screenshot(self):
        assert _is_verify_action("Read", {
            "file_path": "screenshots/verify/home.png",
        })

    def test_regular_read(self):
        assert not _is_verify_action("Read", {"file_path": "src/main.py"})

    def test_edit_not_verify(self):
        assert not _is_verify_action("Edit", {"file_path": "test.tsx"})


class TestIsFixAction:
    def test_edit_ui_file(self):
        assert _is_fix_action("Edit", {"file_path": "src/App.tsx"})

    def test_edit_non_ui(self):
        assert not _is_fix_action("Edit", {"file_path": "src/main.py"})

    def test_deploy(self):
        assert _is_fix_action("Bash", {"command": "python deploy.py"})

    def test_read_allowed(self):
        assert _is_fix_action("Read", {"file_path": "anything"})

    def test_grep_allowed(self):
        assert _is_fix_action("Grep", {"pattern": "error"})

    def test_random_bash_denied(self):
        assert not _is_fix_action("Bash", {"command": "npm test"})


class TestParseScreenshotResult:
    def test_standard_output(self):
        assert _parse_screenshot_result("📊 Total: 12/14 passed") == (12, 14)

    def test_all_passed(self):
        assert _parse_screenshot_result("📊 Total: 14/14 passed") == (14, 14)

    def test_json_output(self):
        assert _parse_screenshot_result('{"passed": 10, "total": 12}') == (10, 12)

    def test_empty(self):
        assert _parse_screenshot_result("") == (0, 0)


class TestHasFailures:
    def test_partial_pass(self):
        assert _has_failures("📊 Total: 10/14 passed")

    def test_all_pass(self):
        assert not _has_failures("📊 Total: 14/14 passed")

    def test_error_text(self):
        assert _has_failures("Error: ENOENT file not found")

    def test_red_x_emoji(self):
        assert _has_failures("❌ mobile-home: timeout")

    def test_success(self):
        assert not _has_failures("Screenshot saved to screenshots/home.png")

    def test_empty(self):
        assert not _has_failures("")


# ── UIVerifyGuard integration ──────────────────────────────


class TestUIVerifyGuard:
    @pytest.fixture()
    def cache_dir(self, tmp_path):
        return str(tmp_path)

    def _ctx(self, cache_dir, tool_name, tool_input, *, tool_result=""):
        return GuardContext(
            tool_name=tool_name,
            tool_input=tool_input,
            session_id="test-session",
            cache_dir=cache_dir,
            hook_event="PreToolUse",
            tool_result=tool_result,
        )

    def _post_ctx(self, cache_dir, tool_name, tool_input, *, tool_result=""):
        return GuardContext(
            tool_name=tool_name,
            tool_input=tool_input,
            session_id="test-session",
            cache_dir=cache_dir,
            hook_event="PostToolUse",
            tool_result=tool_result,
        )

    def _setup_locked(self, guard, cache_dir):
        """Track UI edit + deploy → verify_pending state."""
        guard.on_post_tool(self._post_ctx(cache_dir, "Edit", {
            "file_path": "src/App.tsx",
        }))
        guard.on_post_tool(self._post_ctx(cache_dir, "Bash", {
            "command": "python deploy.py",
        }))

    # ── No lock ──

    def test_no_lock_allows(self, cache_dir):
        guard = UIVerifyGuard()
        ctx = self._ctx(cache_dir, "Edit", {"file_path": "src/main.py"})
        assert guard.check(ctx) is None

    # ── Tracking ──

    def test_track_ui_file(self, cache_dir):
        guard = UIVerifyGuard()
        guard.on_post_tool(self._post_ctx(cache_dir, "Edit", {
            "file_path": "src/App.tsx",
        }))
        store = StateStore(cache_dir)
        state = store.read("ui_verify", "state", default={})
        assert "src/App.tsx" in state.get("ui_changed_files", [])

    def test_no_track_non_ui(self, cache_dir):
        guard = UIVerifyGuard()
        guard.on_post_tool(self._post_ctx(cache_dir, "Edit", {
            "file_path": "src/main.py",
        }))
        store = StateStore(cache_dir)
        state = store.read("ui_verify", "state", default={})
        assert state.get("ui_changed_files", []) == []

    def test_duplicate_ui_file_not_added(self, cache_dir):
        guard = UIVerifyGuard()
        for _ in range(3):
            guard.on_post_tool(self._post_ctx(cache_dir, "Edit", {
                "file_path": "src/App.tsx",
            }))
        store = StateStore(cache_dir)
        state = store.read("ui_verify", "state", default={})
        assert len(state.get("ui_changed_files", [])) == 1

    # ── Deploy gate (9B-2) ──

    def test_deploy_without_ui_changes_no_lock(self, cache_dir):
        guard = UIVerifyGuard()
        guard.on_post_tool(self._post_ctx(cache_dir, "Bash", {
            "command": "python deploy.py",
        }))
        store = StateStore(cache_dir)
        state = store.read("ui_verify", "state", default={})
        assert state.get("phase") is None

    def test_deploy_with_ui_changes_locks(self, cache_dir):
        guard = UIVerifyGuard()
        self._setup_locked(guard, cache_dir)
        store = StateStore(cache_dir)
        state = store.read("ui_verify", "state", default={})
        assert state["phase"] == "verify_pending"

    # ── verify_pending state ──

    def test_verify_pending_denies_regular_tool(self, cache_dir):
        guard = UIVerifyGuard()
        self._setup_locked(guard, cache_dir)
        result = guard.check(self._ctx(cache_dir, "Edit", {
            "file_path": "src/other.py",
        }))
        assert result is not None
        assert result.action == GuardAction.DENY
        assert "截圖" in result.context

    def test_verify_pending_allows_screenshot(self, cache_dir):
        guard = UIVerifyGuard()
        self._setup_locked(guard, cache_dir)
        result = guard.check(self._ctx(cache_dir, "Bash", {
            "command": "node screenshot.js",
        }))
        assert result is None

    # ── Screenshot pass → unlock ──

    def test_screenshot_all_pass_unlocks(self, cache_dir):
        guard = UIVerifyGuard()
        self._setup_locked(guard, cache_dir)
        result = guard.on_post_tool(self._post_ctx(
            cache_dir, "Bash",
            {"command": "node screenshot.js"},
            tool_result="✅ desktop-home\n✅ mobile-home\n📊 Total: 2/2 passed",
        ))
        assert result is not None
        assert "通過" in result.context
        store = StateStore(cache_dir)
        state = store.read("ui_verify", "state", default={})
        assert state.get("phase") is None

    # ── Screenshot fail → fix_mode ──

    def test_screenshot_fail_enters_fix_mode(self, cache_dir):
        guard = UIVerifyGuard()
        self._setup_locked(guard, cache_dir)
        result = guard.on_post_tool(self._post_ctx(
            cache_dir, "Bash",
            {"command": "node screenshot.js"},
            tool_result="✅ desktop-home\n❌ mobile-home: timeout\n📊 Total: 1/2 passed",
        ))
        assert result is not None
        assert "修復模式" in result.context
        store = StateStore(cache_dir)
        state = store.read("ui_verify", "state", default={})
        assert state["phase"] == "fix_mode"

    # ── fix_mode behavior ──

    def test_fix_mode_allows_edit_ui(self, cache_dir):
        guard = UIVerifyGuard()
        self._setup_locked(guard, cache_dir)
        # Fail screenshot → fix_mode
        guard.on_post_tool(self._post_ctx(
            cache_dir, "Bash",
            {"command": "node screenshot.js"},
            tool_result="❌ desktop-home\n📊 Total: 0/1 passed",
        ))
        # Edit UI file → allowed
        result = guard.check(self._ctx(cache_dir, "Edit", {
            "file_path": "src/App.tsx",
        }))
        assert result is None

    def test_fix_mode_allows_deploy(self, cache_dir):
        guard = UIVerifyGuard()
        self._setup_locked(guard, cache_dir)
        guard.on_post_tool(self._post_ctx(
            cache_dir, "Bash",
            {"command": "node screenshot.js"},
            tool_result="❌ fail\n📊 Total: 0/1 passed",
        ))
        result = guard.check(self._ctx(cache_dir, "Bash", {
            "command": "python deploy.py",
        }))
        assert result is None

    def test_fix_mode_denies_unrelated(self, cache_dir):
        guard = UIVerifyGuard()
        self._setup_locked(guard, cache_dir)
        guard.on_post_tool(self._post_ctx(
            cache_dir, "Bash",
            {"command": "node screenshot.js"},
            tool_result="❌ fail\n📊 Total: 0/1 passed",
        ))
        result = guard.check(self._ctx(cache_dir, "Bash", {
            "command": "npm test",
        }))
        assert result is not None
        assert result.action == GuardAction.DENY
        assert "修復模式" in result.context

    # ── Full cycle: fix_mode → re-deploy → verify_pending ──

    def test_full_fix_cycle(self, cache_dir):
        guard = UIVerifyGuard()
        self._setup_locked(guard, cache_dir)
        # Fail → fix_mode
        guard.on_post_tool(self._post_ctx(
            cache_dir, "Bash",
            {"command": "node screenshot.js"},
            tool_result="❌ broken\n📊 Total: 0/1 passed",
        ))
        store = StateStore(cache_dir)
        assert store.read("ui_verify", "state", default={})["phase"] == "fix_mode"
        # Fix + re-deploy → verify_pending
        guard.on_post_tool(self._post_ctx(cache_dir, "Edit", {
            "file_path": "src/App.tsx",
        }))
        result = guard.on_post_tool(self._post_ctx(cache_dir, "Bash", {
            "command": "python deploy.py",
        }))
        assert result is not None
        state = store.read("ui_verify", "state", default={})
        assert state["phase"] == "verify_pending"
        assert state["cycle_count"] == 1
        # Re-screenshot passes → unlock
        result = guard.on_post_tool(self._post_ctx(
            cache_dir, "Bash",
            {"command": "node screenshot.js"},
            tool_result="✅ fixed\n📊 Total: 1/1 passed",
        ))
        state = store.read("ui_verify", "state", default={})
        assert state.get("phase") is None
        assert state["released_reason"] == "verified"

    # ── Deadlock breaker ──

    def test_deadlock_breaker_after_max_cycles(self, cache_dir):
        guard = UIVerifyGuard()
        store = StateStore(cache_dir)
        # Manually set high cycle count
        store.write("ui_verify", "state", {
            "phase": "verify_pending",
            "ui_changed_files": ["src/App.tsx"],
            "cycle_count": 5,
        })
        result = guard.check(self._ctx(cache_dir, "Edit", {
            "file_path": "src/other.py",
        }))
        assert result is not None
        assert result.action == GuardAction.ALLOW
        assert "自動釋放" in result.context

    # ── Edge cases ──

    def test_no_cache_dir(self):
        guard = UIVerifyGuard()
        ctx = GuardContext(
            tool_name="Edit",
            tool_input={"file_path": "test.tsx"},
            session_id="test",
            cache_dir="",
            hook_event="PreToolUse",
        )
        assert guard.check(ctx) is None
        assert guard.on_post_tool(ctx) is None

    def test_guard_metadata(self):
        guard = UIVerifyGuard()
        assert guard.name == "ui_verify"
        assert guard.step_back_reason == "UI not verified after deploy"
