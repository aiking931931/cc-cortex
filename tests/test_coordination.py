"""Tests for concinno.coordination — strategy pattern abstraction."""

import pytest

from concinno.coordination import (
    CoordinationStrategy,
    LockResult,
    SessionInfo,
    get_strategy,
)
from concinno.coordination.agent_teams import AgentTeamsStrategy
from concinno.coordination.base import CoordinationStrategy as BaseStrategy
from concinno.coordination.file_lock import FileLockStrategy

# ── get_strategy selector ─────────────────────────────────────────


class TestGetStrategy:
    def test_default_is_file_lock(self):
        """Default strategy is FileLockStrategy."""
        s = get_strategy()
        assert isinstance(s, FileLockStrategy)

    def test_file_lock_by_name(self):
        """Explicit 'file_lock' returns FileLockStrategy."""
        s = get_strategy("file_lock")
        assert isinstance(s, FileLockStrategy)

    def test_agent_teams_by_name(self):
        """Explicit 'agent_teams' returns AgentTeamsStrategy."""
        s = get_strategy("agent_teams")
        assert isinstance(s, AgentTeamsStrategy)

    def test_unknown_raises(self):
        """Unknown name raises ValueError."""
        with pytest.raises(ValueError, match="Unknown coordination strategy"):
            get_strategy("nonexistent")


# ── Base class contract ───────────────────────────────────────────


class TestBaseClassContract:
    def test_cannot_instantiate_base(self):
        """CoordinationStrategy is abstract — cannot be instantiated."""
        with pytest.raises(TypeError):
            BaseStrategy()  # type: ignore[abstract]

    def test_subclass_must_implement_all(self):
        """A subclass missing methods cannot be instantiated."""

        class Incomplete(CoordinationStrategy):
            def register_session(self, info):
                return True
            # Missing all other methods

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_session_info_defaults(self):
        """SessionInfo has sensible defaults for active_files."""
        info = SessionInfo(session_id="abc", pid=123, started_at="now")
        assert info.active_files == []

    def test_lock_result_defaults(self):
        """LockResult defaults for holder and message."""
        r = LockResult(success=True)
        assert r.holder is None
        assert r.message == ""


# ── AgentTeamsStrategy placeholder ────────────────────────────────


class TestAgentTeamsPlaceholder:
    def setup_method(self):
        self.strategy = AgentTeamsStrategy()
        self.info = SessionInfo(
            session_id="test-123", pid=999, started_at="2026-01-01T00:00:00"
        )

    def test_register_raises(self):
        with pytest.raises(NotImplementedError, match="Agent Teams"):
            self.strategy.register_session(self.info)

    def test_unregister_raises(self):
        with pytest.raises(NotImplementedError, match="Agent Teams"):
            self.strategy.unregister_session("test-123")

    def test_acquire_raises(self):
        with pytest.raises(NotImplementedError, match="Agent Teams"):
            self.strategy.acquire_file_lock("test-123", "src/a.py")

    def test_release_raises(self):
        with pytest.raises(NotImplementedError, match="Agent Teams"):
            self.strategy.release_file_lock("test-123", "src/a.py")

    def test_get_active_raises(self):
        with pytest.raises(NotImplementedError, match="Agent Teams"):
            self.strategy.get_active_sessions()

    def test_cleanup_raises(self):
        with pytest.raises(NotImplementedError, match="Agent Teams"):
            self.strategy.cleanup_zombies()

    def test_check_conflict_raises(self):
        with pytest.raises(NotImplementedError, match="Agent Teams"):
            self.strategy.check_conflict("test-123", "src/a.py")

    def test_acquire_project_raises(self):
        with pytest.raises(NotImplementedError, match="Agent Teams"):
            self.strategy.acquire_project("test-123", "psyche")

    def test_release_project_raises(self):
        with pytest.raises(NotImplementedError, match="Agent Teams"):
            self.strategy.release_project("test-123", "psyche")

    def test_check_project_conflict_raises(self):
        with pytest.raises(NotImplementedError, match="Agent Teams"):
            self.strategy.check_project_conflict("test-123", "psyche")

    def test_rename_session_raises(self):
        with pytest.raises(NotImplementedError, match="Agent Teams"):
            self.strategy.rename_session("test-123", "psyche", "doing work")


# ── FileLockStrategy integration ──────────────────────────────────


class TestFileLockStrategy:
    def setup_method(self):
        """Each test gets a fresh strategy with isolated lock file."""
        import os
        import tempfile

        self.tmpdir = tempfile.mkdtemp()
        self.lock_path = os.path.join(self.tmpdir, "lock.json")
        self.strategy = FileLockStrategy(
            lock_path=self.lock_path, marker_dir=self.tmpdir
        )

    def _make_info(self, sid="sess-abcdef01", pid=1234, files=None):
        return SessionInfo(
            session_id=sid,
            pid=pid,
            started_at="2026-01-01T00:00:00+00:00",
            active_files=files or [],
        )

    def test_register_and_list(self):
        """Register a session, then list it."""
        info = self._make_info()
        assert self.strategy.register_session(info) is True
        sessions = self.strategy.get_active_sessions()
        assert len(sessions) == 1
        assert sessions[0].session_id == "sess-abcdef01"

    def test_unregister(self):
        """Unregister removes the session."""
        info = self._make_info()
        self.strategy.register_session(info)
        assert self.strategy.unregister_session("sess-abcdef01") is True
        assert self.strategy.get_active_sessions() == []

    def test_unregister_nonexistent(self):
        """Unregistering unknown session returns False."""
        assert self.strategy.unregister_session("nope") is False

    def test_acquire_and_release(self):
        """Acquire a file lock, then release it."""
        info = self._make_info()
        self.strategy.register_session(info)

        result = self.strategy.acquire_file_lock("sess-abcdef01", "src/a.py")
        assert result.success is True

        released = self.strategy.release_file_lock("sess-abcdef01", "src/a.py")
        assert released is True

    def test_acquire_unregistered(self):
        """Acquiring lock for unregistered session fails."""
        result = self.strategy.acquire_file_lock("ghost", "src/a.py")
        assert result.success is False
        assert "not registered" in result.message.lower()

    def test_conflict_detection(self):
        """Two sessions claiming the same file -> conflict."""
        info_a = self._make_info(sid="sess-aaaaaaaa", pid=100)
        info_b = self._make_info(sid="sess-bbbbbbbb", pid=200)
        self.strategy.register_session(info_a)
        self.strategy.register_session(info_b)

        # A claims file
        self.strategy.acquire_file_lock("sess-aaaaaaaa", "src/shared.py")

        # B tries to claim same file
        result = self.strategy.acquire_file_lock("sess-bbbbbbbb", "src/shared.py")
        assert result.success is False
        assert result.holder is not None

    def test_check_conflict_returns_holder(self):
        """check_conflict returns the holder's key."""
        info_a = self._make_info(sid="sess-aaaaaaaa", pid=100)
        info_b = self._make_info(sid="sess-bbbbbbbb", pid=200)
        self.strategy.register_session(info_a)
        self.strategy.register_session(info_b)
        self.strategy.acquire_file_lock("sess-aaaaaaaa", "src/x.py")

        conflict = self.strategy.check_conflict("sess-bbbbbbbb", "src/x.py")
        assert conflict is not None
        assert "sess-aaa" in conflict

    def test_no_conflict_different_files(self):
        """Different files -> no conflict."""
        info_a = self._make_info(sid="sess-aaaaaaaa", pid=100)
        info_b = self._make_info(sid="sess-bbbbbbbb", pid=200)
        self.strategy.register_session(info_a)
        self.strategy.register_session(info_b)
        self.strategy.acquire_file_lock("sess-aaaaaaaa", "src/a.py")

        conflict = self.strategy.check_conflict("sess-bbbbbbbb", "src/b.py")
        assert conflict is None

    def test_release_nonexistent_file(self):
        """Releasing a file not held returns False."""
        info = self._make_info()
        self.strategy.register_session(info)
        assert self.strategy.release_file_lock("sess-abcdef01", "nope.py") is False

    def test_cleanup_zombies(self):
        """Old sessions are cleaned up."""
        info = self._make_info()
        self.strategy.register_session(info)
        # Zombie cleanup with 0 timeout should remove everything
        removed = self.strategy.cleanup_zombies(timeout_seconds=0)
        assert len(removed) == 1
        assert self.strategy.get_active_sessions() == []

    def test_register_with_initial_files(self):
        """Files passed in SessionInfo are tracked on registration."""
        info = self._make_info(files=["src/a.py", "src/b.py"])
        self.strategy.register_session(info)

        sessions = self.strategy.get_active_sessions()
        assert "src/a.py" in sessions[0].active_files
        assert "src/b.py" in sessions[0].active_files


# ── Project-level lock (the "different processes grabbing the same
# project" guarantee — stronger than per-file locking alone) ──────


class TestProjectLock:
    def setup_method(self):
        import os
        import tempfile

        self.tmpdir = tempfile.mkdtemp()
        self.lock_path = os.path.join(self.tmpdir, "lock.json")
        self.strategy = FileLockStrategy(
            lock_path=self.lock_path,
            marker_dir=self.tmpdir,
            os_lock_timeout=2.0,
        )

    def _register(self, sid, pid=1000):
        info = SessionInfo(
            session_id=sid,
            pid=pid,
            started_at="2026-01-01T00:00:00+00:00",
        )
        self.strategy.register_session(info)

    def test_acquire_project_requires_registration(self):
        """Unregistered session cannot claim a project."""
        r = self.strategy.acquire_project("ghost-01234567", "psyche")
        assert r.success is False
        assert "not registered" in r.message.lower()

    def test_acquire_project_empty_name_rejected(self):
        """Empty project name is rejected."""
        self._register("sess-aaaaaaaa")
        r = self.strategy.acquire_project("sess-aaaaaaaa", "")
        assert r.success is False
        assert "empty" in r.message.lower()

    def test_first_claim_wins(self):
        """First session to claim a project wins."""
        self._register("sess-aaaaaaaa")
        self._register("sess-bbbbbbbb")

        r1 = self.strategy.acquire_project("sess-aaaaaaaa", "psyche")
        r2 = self.strategy.acquire_project("sess-bbbbbbbb", "psyche")

        assert r1.success is True
        assert r2.success is False
        assert r2.holder == "cc_sess-aaa" or "sess-aaa" in (r2.holder or "")

    def test_project_name_normalization(self):
        """psyche / PSYCHE / '  psyche  ' all collide on the same lock."""
        self._register("sess-aaaaaaaa")
        self._register("sess-bbbbbbbb")

        assert self.strategy.acquire_project(
            "sess-aaaaaaaa", "psyche"
        ).success is True

        # Different casing / whitespace — must still conflict
        r = self.strategy.acquire_project("sess-bbbbbbbb", "  PSYCHE  ")
        assert r.success is False

    def test_idempotent_claim_by_same_session(self):
        """Same session can re-acquire its own project claim."""
        self._register("sess-aaaaaaaa")
        self.strategy.acquire_project("sess-aaaaaaaa", "psyche")
        r = self.strategy.acquire_project("sess-aaaaaaaa", "psyche")
        assert r.success is True

    def test_release_frees_project(self):
        """Releasing a project allows another session to take it."""
        self._register("sess-aaaaaaaa")
        self._register("sess-bbbbbbbb")

        self.strategy.acquire_project("sess-aaaaaaaa", "psyche")
        assert self.strategy.release_project("sess-aaaaaaaa", "psyche") is True

        r = self.strategy.acquire_project("sess-bbbbbbbb", "psyche")
        assert r.success is True

    def test_release_rejects_non_holder(self):
        """A session cannot release a project it doesn't hold."""
        self._register("sess-aaaaaaaa")
        self._register("sess-bbbbbbbb")
        self.strategy.acquire_project("sess-aaaaaaaa", "psyche")

        assert self.strategy.release_project("sess-bbbbbbbb", "psyche") is False

    def test_unregister_releases_project(self):
        """Unregistering a session releases its project claims."""
        self._register("sess-aaaaaaaa")
        self._register("sess-bbbbbbbb")

        self.strategy.acquire_project("sess-aaaaaaaa", "psyche")
        self.strategy.unregister_session("sess-aaaaaaaa")

        # Now B can take it
        r = self.strategy.acquire_project("sess-bbbbbbbb", "psyche")
        assert r.success is True

    def test_stale_holder_is_replaced(self):
        """If the holder session has vanished, a new session can take over."""
        self._register("sess-aaaaaaaa")
        self.strategy.acquire_project("sess-aaaaaaaa", "psyche")

        # Simulate the holder dying without proper release — edit the JSON directly
        import json
        with open(self.lock_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        del data["sessions"]["cc_sess-aaa"]
        with open(self.lock_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        self._register("sess-bbbbbbbb")
        r = self.strategy.acquire_project("sess-bbbbbbbb", "psyche")
        assert r.success is True

    def test_check_project_conflict_returns_holder(self):
        """check_project_conflict returns current holder key."""
        self._register("sess-aaaaaaaa")
        self._register("sess-bbbbbbbb")
        self.strategy.acquire_project("sess-aaaaaaaa", "psyche")

        holder = self.strategy.check_project_conflict("sess-bbbbbbbb", "psyche")
        assert holder is not None
        assert "sess-aaa" in holder

    def test_check_project_conflict_none_for_self(self):
        """Holder itself sees no conflict."""
        self._register("sess-aaaaaaaa")
        self.strategy.acquire_project("sess-aaaaaaaa", "psyche")
        assert (
            self.strategy.check_project_conflict("sess-aaaaaaaa", "psyche")
            is None
        )

    def test_register_with_project_auto_claims(self):
        """SessionInfo(project=...) auto-claims on registration."""
        info = SessionInfo(
            session_id="sess-aaaaaaaa",
            pid=100,
            started_at="2026-01-01T00:00:00+00:00",
            project="psyche",
        )
        assert self.strategy.register_session(info) is True

        sessions = self.strategy.get_active_sessions()
        assert sessions[0].project == "psyche"

        # A different session trying to claim same project fails
        self._register("sess-bbbbbbbb")
        r = self.strategy.acquire_project("sess-bbbbbbbb", "psyche")
        assert r.success is False


# ── Session rename: project-scoped key + marker + toast ──────────


class TestRenameSession:
    def setup_method(self):
        import os
        import tempfile

        self.tmpdir = tempfile.mkdtemp()
        self.marker_dir = os.path.join(self.tmpdir, "markers")
        os.makedirs(self.marker_dir, exist_ok=True)
        self.lock_path = os.path.join(self.tmpdir, "lock.json")
        self.strategy = FileLockStrategy(
            lock_path=self.lock_path,
            marker_dir=self.marker_dir,
            os_lock_timeout=2.0,
        )

    def _register(self, sid="sess-aaaaaaaa", pid=1234):
        info = SessionInfo(
            session_id=sid,
            pid=pid,
            started_at="2026-01-01T00:00:00+00:00",
        )
        self.strategy.register_session(info)
        return info

    def test_rename_unregistered_fails(self):
        r = self.strategy.rename_session("ghost-01234567", "psyche", "work")
        assert r.success is False
        assert "not registered" in r.message.lower()

    def test_rename_format(self):
        """New key must match ``<ABBR>_<4hex>_<HHMM>``."""
        import re

        self._register("sess-aaaaaaaa")
        r = self.strategy.rename_session("sess-aaaaaaaa", "psyche", "task A")

        assert r.success is True
        assert re.fullmatch(r"PSY_[0-9a-f]{4}_\d{4}", r.new_key), r.new_key
        assert r.new_key == r.title

    def test_rename_unknown_project_falls_back_to_uppercase(self):
        import re

        self._register("sess-aaaaaaaa")
        r = self.strategy.rename_session("sess-aaaaaaaa", "mercury", "x")
        assert re.fullmatch(r"MERC_[0-9a-f]{4}_\d{4}", r.new_key), r.new_key

    def test_rename_abbr_override(self):
        import re

        self._register("sess-aaaaaaaa")
        r = self.strategy.rename_session(
            "sess-aaaaaaaa",
            "psyche",
            "work",
            abbr_overrides={"psyche": "PSYX"},
        )
        assert re.fullmatch(r"PSYX_[0-9a-f]{4}_\d{4}", r.new_key), r.new_key

    def test_rename_updates_task_and_project(self):
        """After rename, task and project fields are set on the entry."""
        self._register("sess-aaaaaaaa")
        r = self.strategy.rename_session(
            "sess-aaaaaaaa", "psyche", "fix the thing"
        )
        assert r.success is True

        sessions = self.strategy.get_active_sessions()
        target = next(s for s in sessions if s.session_id == "sess-aaaaaaaa")
        assert target.project == "psyche"

    def test_rename_claims_project_lock(self):
        """Rename also claims the project — competitors see conflict."""
        self._register("sess-aaaaaaaa")
        self._register("sess-bbbbbbbb")

        r = self.strategy.rename_session("sess-aaaaaaaa", "psyche", "work")
        assert r.success is True

        conflict = self.strategy.check_project_conflict(
            "sess-bbbbbbbb", "psyche"
        )
        assert conflict == r.new_key

    def test_rename_writes_marker(self):
        """Marker file lands in marker_dir with the new title."""
        import os

        self._register("sess-aaaaaaaa")
        r = self.strategy.rename_session("sess-aaaaaaaa", "psyche", "work")

        marker = os.path.join(self.marker_dir, "sess-aaaaaaaa.session_name")
        assert os.path.isfile(marker)
        with open(marker, encoding="utf-8") as f:
            assert f.read().strip() == r.new_key

    def test_rename_migrates_handoff_locks(self):
        """Any handoff_locks referencing the old key follow to the new key."""
        import json

        self._register("sess-aaaaaaaa")

        # Inject a handoff lock that references the old session key
        with open(self.lock_path, encoding="utf-8") as f:
            data = json.load(f)
        data["handoff_locks"] = {
            "h1": {"session": "cc_sess-aaa", "path": "handoff.md"}
        }
        with open(self.lock_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        r = self.strategy.rename_session("sess-aaaaaaaa", "psyche", "work")
        assert r.success is True

        with open(self.lock_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["handoff_locks"]["h1"]["session"] == r.new_key

    def test_rename_notify_does_not_crash(self):
        """notify=True must never raise even if the toast backend fails."""
        self._register("sess-aaaaaaaa")
        r = self.strategy.rename_session(
            "sess-aaaaaaaa", "psyche", "work", notify=True
        )
        assert r.success is True

    def test_project_abbr_helper(self):
        """Abbreviation map: known projects + override + fallback."""
        from concinno.coordination.base import project_abbr

        assert project_abbr("psyche") == "PSY"
        assert project_abbr("evolution") == "EVO"
        assert project_abbr("evolution", {"evolution": "EVX"}) == "EVX"
        assert project_abbr("something-new") == "SOME"
        assert project_abbr("") == "GEN"


# ── OS-level inter-process lock (TOCTOU safety) ──────────────────


class TestOSFileLock:
    def setup_method(self):
        import os
        import tempfile

        self.tmpdir = tempfile.mkdtemp()
        self.lock_path = os.path.join(self.tmpdir, "state.json.lockfile")

    def test_basic_acquire_release(self):
        """A single process can acquire and release the lock."""
        from concinno.coordination._os_lock import OSFileLock

        with OSFileLock(self.lock_path, timeout=1.0):
            pass  # Acquired and released cleanly
        # Should be re-acquirable
        with OSFileLock(self.lock_path, timeout=1.0):
            pass

    def test_timeout_when_held(self):
        """A second acquisition times out while the first still holds it."""
        from concinno.coordination._os_lock import (
            LockAcquireTimeout,
            OSFileLock,
        )

        lock_a = OSFileLock(self.lock_path, timeout=0.2)
        lock_b = OSFileLock(self.lock_path, timeout=0.2)
        lock_a.__enter__()
        try:
            with pytest.raises(LockAcquireTimeout):
                lock_b.__enter__()
        finally:
            lock_a.__exit__(None, None, None)

    def test_reacquire_after_release(self):
        """After release, another acquire succeeds."""
        from concinno.coordination._os_lock import OSFileLock

        lock_a = OSFileLock(self.lock_path, timeout=1.0)
        lock_a.__enter__()
        lock_a.__exit__(None, None, None)

        lock_b = OSFileLock(self.lock_path, timeout=1.0)
        lock_b.__enter__()
        lock_b.__exit__(None, None, None)

    def test_concurrent_file_lock_strategy_no_lost_claim(self):
        """Two threads claiming different files both succeed, both claims
        persist — OS lock prevents read-modify-write races from clobbering."""
        import os
        import threading

        lock_path = os.path.join(self.tmpdir, "concurrent.json")
        strat = FileLockStrategy(
            lock_path=lock_path, marker_dir=self.tmpdir, os_lock_timeout=5.0
        )

        # Pre-register two sessions
        for sid in ("sess-aaaaaaaa", "sess-bbbbbbbb"):
            strat.register_session(
                SessionInfo(
                    session_id=sid,
                    pid=0,
                    started_at="2026-01-01T00:00:00+00:00",
                )
            )

        results = {}

        def claim(sid, path):
            results[sid] = strat.acquire_file_lock(sid, path)

        t1 = threading.Thread(
            target=claim, args=("sess-aaaaaaaa", "src/a.py")
        )
        t2 = threading.Thread(
            target=claim, args=("sess-bbbbbbbb", "src/b.py")
        )
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results["sess-aaaaaaaa"].success is True
        assert results["sess-bbbbbbbb"].success is True

        # Both claims must be visible (no TOCTOU loss)
        sessions = {s.session_id: s for s in strat.get_active_sessions()}
        assert "src/a.py" in sessions["sess-aaaaaaaa"].active_files
        assert "src/b.py" in sessions["sess-bbbbbbbb"].active_files
