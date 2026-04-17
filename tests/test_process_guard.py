"""Tests for concinno.process_guard."""

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

from concinno.process_guard import (
    ClaudeProcess,
    Tier,
    _classify_processes,
    _cleanup_instance_lock,
    _emergency_memory_relief,
    _find_ancestor,
    _find_claude_processes,
    _find_orphan_children,
    _find_subagent_pids,
    _get_child_tree,
    _get_system_memory_percent,
    _is_scheduled_task,
    _read_instance_lock,
    run_guard,
)

# ── Fixtures ──


def _make_proc(pid, ppid=1, name="claude.exe", cmdline="claude", mem_kb=100000, start_time=None):
    return {
        "pid": pid,
        "ppid": ppid,
        "name": name,
        "cmdline": cmdline,
        "mem_kb": mem_kb,
        "start_time": start_time,
    }


def _make_lock(sessions: dict) -> str:
    """Create a temp instance_lock.json and return path."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"sessions": sessions, "last_updated": "2026-01-01T00:00:00"}, f)
    return path


# ── Unit Tests ──


class TestFindClaudeProcesses:
    def test_finds_claude_exe(self):
        procs = [_make_proc(100, name="claude.exe", cmdline="claude")]
        result = _find_claude_processes(procs)
        assert len(result) == 1
        assert result[0].pid == 100
        assert result[0].name == "claude"

    def test_finds_node_with_claude(self):
        procs = [_make_proc(200, name="node.exe", cmdline="node /path/to/claude/cli.js")]
        result = _find_claude_processes(procs)
        assert len(result) == 1
        assert result[0].name == "node(claude)"

    def test_skips_process_guard_node(self):
        procs = [_make_proc(300, name="node.exe", cmdline="node process-guard.js")]
        result = _find_claude_processes(procs)
        assert len(result) == 0

    def test_finds_code_cli(self):
        procs = [_make_proc(400, name="Code.exe", cmdline="Code.exe cli.js --output-format json")]
        result = _find_claude_processes(procs)
        assert len(result) == 1
        assert result[0].name == "code(claude-cli)"

    def test_ignores_unrelated(self):
        procs = [
            _make_proc(500, name="firefox.exe", cmdline="firefox"),
            _make_proc(501, name="node.exe", cmdline="node webpack.js"),
        ]
        result = _find_claude_processes(procs)
        assert len(result) == 0

    def test_deduplicates(self):
        procs = [
            _make_proc(100, name="claude.exe"),
            _make_proc(100, name="claude.exe"),  # dup
        ]
        result = _find_claude_processes(procs)
        assert len(result) == 1


class TestFindAncestor:
    def test_finds_direct_parent(self):
        proc_map = {
            100: {"pid": 100, "ppid": 50},
            50: {"pid": 50, "ppid": 1},
        }
        assert _find_ancestor(100, {50}, proc_map) == 50

    def test_finds_grandparent(self):
        proc_map = {
            100: {"pid": 100, "ppid": 80},
            80: {"pid": 80, "ppid": 50},
            50: {"pid": 50, "ppid": 1},
        }
        assert _find_ancestor(100, {50}, proc_map) == 50

    def test_returns_zero_when_not_found(self):
        proc_map = {100: {"pid": 100, "ppid": 80}}
        assert _find_ancestor(100, {50}, proc_map) == 0


class TestIsScheduledTask:
    def test_detects_scheduled_launcher(self):
        proc_map = {
            100: {"pid": 100, "ppid": 50, "cmdline": "claude --print"},
            50: {"pid": 50, "ppid": 1, "cmdline": "powershell scheduled_launcher.ps1"},
        }
        assert _is_scheduled_task(100, proc_map) is True

    def test_returns_false_for_normal(self):
        proc_map = {
            100: {"pid": 100, "ppid": 50, "cmdline": "claude"},
            50: {"pid": 50, "ppid": 1, "cmdline": "Code.exe"},
        }
        assert _is_scheduled_task(100, proc_map) is False


class TestReadInstanceLock:
    def test_reads_valid(self):
        path = _make_lock({"s1": {"last_active": "2026-01-01T00:00:00+08:00"}})
        try:
            lock = _read_instance_lock(path)
            assert "sessions" in lock
            assert "s1" in lock["sessions"]
        finally:
            os.unlink(path)

    def test_returns_empty_on_missing(self):
        assert _read_instance_lock("/nonexistent/path.json") == {}

    def test_returns_empty_on_invalid(self):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            f.write("not json")
        try:
            assert _read_instance_lock(path) == {}
        finally:
            os.unlink(path)


class TestClassifyProcesses:
    def test_orphan_no_ide(self):
        all_procs = [
            _make_proc(100, ppid=999, name="claude.exe"),
        ]
        claude = [ClaudeProcess(pid=100, name="claude", start_time=1000)]
        lock_path = _make_lock({})
        try:
            result = _classify_processes(claude, all_procs, lock_path)
            assert result[0].tier == Tier.ORPHAN
        finally:
            os.unlink(lock_path)

    def test_alive_with_active_session(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        all_procs = [
            _make_proc(100, ppid=50, name="claude.exe"),
            _make_proc(50, ppid=1, name="Code.exe"),
        ]
        lock_path = _make_lock({
            "s1": {"last_active": now_iso, "vscode_pid": 50}
        })
        claude = [ClaudeProcess(pid=100, name="claude", start_time=1000)]
        try:
            result = _classify_processes(claude, all_procs, lock_path)
            assert result[0].tier == Tier.ALIVE
        finally:
            os.unlink(lock_path)


class TestFindOrphanChildren:
    def test_finds_orphan_bash(self):
        procs = [
            _make_proc(100, ppid=999, name="bash.exe", cmdline="bash claude hook"),
        ]
        # ppid 999 not in proc_map
        result = _find_orphan_children(procs)
        assert 100 in result

    def test_finds_orphan_mcp_server(self):
        procs = [
            _make_proc(200, ppid=888, name="python.exe",
                       cmdline='python.exe -u "word_mcp_server.py"'),
        ]
        # ppid 888 not in proc_map → orphan
        result = _find_orphan_children(procs)
        assert 200 in result

    @patch("concinno.process_guard.classifier._pid_alive", return_value=True)
    def test_skips_alive_mcp_server(self, mock_alive):
        procs = [
            _make_proc(200, ppid=50, name="python.exe",
                       cmdline='python.exe -u "word_mcp_server.py"'),
            _make_proc(50, ppid=1, name="claude.exe"),
        ]
        result = _find_orphan_children(procs)
        assert 200 not in result

    @patch("concinno.process_guard.classifier._pid_alive", return_value=True)
    def test_skips_alive_parent(self, mock_alive):
        procs = [
            _make_proc(100, ppid=50, name="bash.exe", cmdline="bash claude"),
            _make_proc(50, ppid=1, name="claude.exe"),
        ]
        result = _find_orphan_children(procs)
        assert 100 not in result


class TestCleanupInstanceLock:
    def test_removes_dead_ide(self):
        lock_path = _make_lock({
            "s1": {
                "vscode_pid": 99999,
                "last_active": "2020-01-01T00:00:00+00:00",
                "started": "2020-01-01",
            }
        })
        try:
            removed = _cleanup_instance_lock(lock_path, {"s1"}, set())
            assert removed == 1
            with open(lock_path) as f:
                data = json.load(f)
            assert "s1" not in data["sessions"]
        finally:
            os.unlink(lock_path)


class TestRunGuard:
    @patch("concinno.process_guard.guard._get_all_processes", return_value=[])
    def test_no_processes(self, mock_procs):
        result = run_guard(dry_run=True)
        assert result.scanned == 0
        assert result.killed == 0

    @patch("concinno.process_guard.guard._get_all_processes")
    @patch("concinno.process_guard.guard._kill_process", return_value=True)
    def test_dry_run_no_kills(self, mock_kill, mock_procs):
        mock_procs.return_value = [
            _make_proc(100, ppid=999, name="claude.exe"),
        ]
        lock_path = _make_lock({})
        try:
            result = run_guard(lock_path=lock_path, dry_run=True)
            assert result.scanned == 1
            assert result.killed == 0
            mock_kill.assert_not_called()
        finally:
            os.unlink(lock_path)


class TestGetSystemMemoryPercent:
    def test_returns_float(self):
        result = _get_system_memory_percent()
        assert isinstance(result, float)
        assert 0 <= result <= 100


class TestFindSubagentPids:
    def test_finds_child_claude(self):
        procs = [
            _make_proc(100, ppid=1, name="claude.exe"),   # mother
            _make_proc(200, ppid=100, name="bash.exe"),
            _make_proc(300, ppid=200, name="claude.exe"),  # subagent
        ]
        result = _find_subagent_pids(100, procs)
        assert 300 in result
        assert 100 not in result

    def test_ignores_unrelated_claude(self):
        procs = [
            _make_proc(100, ppid=1, name="claude.exe"),
            _make_proc(400, ppid=1, name="claude.exe"),  # different parent tree
        ]
        result = _find_subagent_pids(100, procs)
        assert 400 not in result


class TestGetChildTree:
    def test_gets_full_tree(self):
        procs = [
            _make_proc(100, ppid=1, name="claude.exe"),
            _make_proc(200, ppid=100, name="bash.exe"),
            _make_proc(300, ppid=200, name="node.exe"),
            _make_proc(400, ppid=300, name="python.exe"),
        ]
        result = _get_child_tree(100, procs)
        assert set(result) == {200, 300, 400}

    def test_empty_for_leaf(self):
        procs = [_make_proc(100, ppid=1)]
        assert _get_child_tree(100, procs) == []


class TestEmergencyMemoryRelief:
    @patch("concinno.process_guard.classifier._get_system_memory_percent", return_value=50.0)
    def test_no_action_below_threshold(self, mock_mem):
        actions, killed, freed = _emergency_memory_relief([], [], "/fake", dry_run=True)
        assert killed == 0
        assert len(actions) == 0

    @patch(
        "concinno.process_guard.classifier._get_system_memory_percent",
        side_effect=[96.0, 80.0],
    )
    @patch("concinno.process_guard.guard._kill_process", return_value=True)
    @patch("concinno.process_guard.classifier._pid_alive", return_value=False)
    def test_kills_orphans_first(self, mock_alive, mock_kill, mock_mem):
        procs = [
            # Orphan MCP server: parent 999 not in proc list
            _make_proc(500, ppid=999, name="python.exe",
                       cmdline='python.exe -u "word_mcp_server.py"', mem_kb=75000),
        ]
        actions, killed, freed = _emergency_memory_relief([], procs, "/fake")
        assert killed >= 1
        assert any("EMERGENCY KILL" in a for a in actions)

    @patch(
        "concinno.process_guard.classifier._get_system_memory_percent",
        side_effect=[96.0, 96.0, 96.0, 80.0],
    )
    @patch("concinno.process_guard.classifier._kill_process", return_value=True)
    @patch("concinno.process_guard.classifier._pid_alive", return_value=True)
    def test_preserves_mother_claude(self, mock_alive, mock_kill, mock_mem):
        """Wave 3 kills children but NOT the mother claude.exe."""
        procs = [
            _make_proc(100, ppid=1, name="claude.exe"),   # mother
            _make_proc(200, ppid=100, name="bash.exe", cmdline="bash"),
            _make_proc(300, ppid=200, name="node.exe", cmdline="node"),
        ]
        lock_path = _make_lock({
            "s1": {
                "cli_pid": 100,
                "vscode_pid": 1,
                "last_active": "2020-01-01T00:00:00+00:00",
                "started": "2020-01-01",
            }
        })
        try:
            actions, killed, freed = _emergency_memory_relief([], procs, lock_path)
            # Mother (PID 100) should NOT be killed
            killed_pids = [
                call.args[0] for call in mock_kill.call_args_list
            ]
            assert 100 not in killed_pids
            assert 200 in killed_pids or 300 in killed_pids
        finally:
            os.unlink(lock_path)
