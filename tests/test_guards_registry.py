"""Tests for guards.registry — Default pipeline factory and guard ordering."""

from __future__ import annotations

import pytest

from cc_cortex.guards.base import BaseGuard, GuardCategory
from cc_cortex.guards.pipeline import GuardPipeline
from cc_cortex.guards.registry import create_default_pipeline

# ── Factory ──────────────────────────────────────────────────


class TestCreateDefaultPipeline:
    def test_returns_guard_pipeline(self):
        pipe = create_default_pipeline()
        assert isinstance(pipe, GuardPipeline)

    def test_has_guards(self):
        pipe = create_default_pipeline()
        assert pipe.guard_count >= 15  # at least 15 guards

    def test_step_back_dir_forwarded(self):
        pipe = create_default_pipeline(step_back_state_dir="/tmp/sb")
        assert pipe._step_back_dir == "/tmp/sb"

    def test_no_step_back_by_default(self):
        pipe = create_default_pipeline()
        assert pipe._step_back_dir == ""


# ── Three-Layer Architecture ─────────────────────────────────


class TestThreeLayerArchitecture:
    @pytest.fixture
    def pipe(self):
        return create_default_pipeline()

    def test_has_security_layer(self, pipe):
        guards = pipe.list_guards()
        assert "security" in guards
        assert len(guards["security"]) >= 5

    def test_has_quality_layer(self, pipe):
        guards = pipe.list_guards()
        assert "quality" in guards
        assert len(guards["quality"]) >= 10

    def test_has_cognitive_layer(self, pipe):
        guards = pipe.list_guards()
        assert "cognitive" in guards
        assert len(guards["cognitive"]) >= 1

    def test_execution_order(self, pipe):
        """Security runs before Quality runs before Cognitive."""
        guards = pipe.list_guards()
        keys = list(guards.keys())
        assert keys.index("security") < keys.index("quality")
        assert keys.index("quality") < keys.index("cognitive")


# ── Known Guards Present ─────────────────────────────────────


class TestKnownGuards:
    @pytest.fixture
    def all_names(self):
        pipe = create_default_pipeline()
        names = []
        for cat_names in pipe.list_guards().values():
            names.extend(cat_names)
        return names

    # Security guards
    def test_secret_scan_registered(self, all_names):
        assert "secret_scan" in all_names

    def test_git_safety_registered(self, all_names):
        assert "git_safety" in all_names

    def test_dep_audit_registered(self, all_names):
        assert "dep_audit" in all_names

    def test_exfil_guard_registered(self, all_names):
        assert "exfil_guard" in all_names

    def test_identity_guard_registered(self, all_names):
        assert "identity_guard" in all_names

    def test_destruction_guard_registered(self, all_names):
        assert "destruction_guard" in all_names

    # Quality guards
    def test_window_guard_registered(self, all_names):
        assert "window_guard" in all_names

    def test_token_guard_registered(self, all_names):
        assert "token_guard" in all_names

    def test_agent_gate_registered(self, all_names):
        assert "agent_gate" in all_names

    def test_read_first_registered(self, all_names):
        assert "read_first" in all_names

    def test_sentinel_registered(self, all_names):
        assert "sentinel" in all_names

    def test_boundary_guard_registered(self, all_names):
        assert "boundary_guard" in all_names

    # Cognitive
    def test_cognitive_guard_registered(self, all_names):
        assert "cognitive" in all_names

    # PostTool guards
    def test_code_guard_registered(self, all_names):
        assert "code_guard" in all_names

    def test_lint_guard_registered(self, all_names):
        assert "lint_guard" in all_names

    def test_delivery_guard_registered(self, all_names):
        assert "delivery_guard" in all_names


# ── Guard Integrity ──────────────────────────────────────────


class TestGuardIntegrity:
    @pytest.fixture
    def pipe(self):
        return create_default_pipeline()

    def test_all_guards_are_baseguard(self, pipe):
        for guard in pipe._guards:
            assert isinstance(guard, BaseGuard), f"{guard} is not BaseGuard"

    def test_all_guards_have_name(self, pipe):
        for guard in pipe._guards:
            assert guard.name, f"{guard.__class__.__name__} has no name"

    def test_all_guards_have_category(self, pipe):
        for guard in pipe._guards:
            assert isinstance(guard.category, GuardCategory)

    def test_no_duplicate_names(self, pipe):
        names = [g.name for g in pipe._guards]
        dupes = [n for n in names if names.count(n) > 1]
        assert len(names) == len(set(names)), f"Duplicates: {dupes}"

    def test_security_guards_no_step_back(self, pipe):
        """Security guards should NOT have step_back_reason — they hard-deny."""
        for guard in pipe._guards:
            if guard.category == GuardCategory.SECURITY:
                assert guard.step_back_reason == "", (
                    f"Security guard {guard.name} has step_back_reason="
                    f"{guard.step_back_reason!r}"
                )

    def test_repr_shows_all_layers(self, pipe):
        r = repr(pipe)
        assert "security=" in r
        assert "quality=" in r
        assert "cognitive=" in r
