"""Tests for cc_cortex.plugin_loader — plugin discovery, loading, validation."""

import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cc_cortex.guards.base import BaseGuard, GuardCategory, GuardContext
from cc_cortex.plugin_loader import (
    PluginError,
    PluginMeta,
    discover_plugins,
    load_guard_file,
    validate_guard,
)

# ── Fixtures ─────────────────────────────────────────────


class _ValidGuard(BaseGuard):
    name = "test_valid"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext):
        return None


class _NoNameGuard(BaseGuard):
    name = ""
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext):
        return None


def _write_plugin(tmp_path, filename, content):
    p = tmp_path / filename
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return str(p)


# ── validate_guard ───────────────────────────────────────


class TestValidateGuard:
    def test_valid_guard(self):
        errors = validate_guard(_ValidGuard())
        assert errors == []

    def test_not_baseguard(self):
        errors = validate_guard("not a guard")
        assert len(errors) == 1
        assert "Not a BaseGuard" in errors[0]

    def test_empty_name(self):
        errors = validate_guard(_NoNameGuard())
        assert any("empty" in e for e in errors)

    def test_duplicate_name(self):
        errors = validate_guard(_ValidGuard(), existing_names={"test_valid"})
        assert any("Duplicate" in e for e in errors)

    def test_no_duplicate_with_different_name(self):
        errors = validate_guard(_ValidGuard(), existing_names={"other_guard"})
        assert errors == []


# ── PluginMeta ───────────────────────────────────────────


class TestPluginMeta:
    def test_valid_when_no_errors(self):
        m = PluginMeta(guard=_ValidGuard(), source="test", path="")
        assert m.valid is True

    def test_invalid_when_errors(self):
        m = PluginMeta(guard=None, source="test", errors=["bad"])
        assert m.valid is False


# ── load_guard_file ──────────────────────────────────────


class TestLoadGuardFile:
    def test_load_valid_plugin(self, tmp_path):
        path = _write_plugin(tmp_path, "my_guard.py", """\
            from cc_cortex.guards.base import BaseGuard, GuardCategory

            class MyGuard(BaseGuard):
                name = "my_plugin_guard"
                category = GuardCategory.QUALITY
                def check(self, ctx):
                    return None
        """)
        metas = load_guard_file(path)
        assert len(metas) == 1
        assert metas[0].valid
        assert metas[0].guard.name == "my_plugin_guard"
        assert metas[0].source == "file"
        assert metas[0].load_time_ms >= 0

    def test_load_multiple_guards(self, tmp_path):
        path = _write_plugin(tmp_path, "multi.py", """\
            from cc_cortex.guards.base import BaseGuard, GuardCategory

            class GuardA(BaseGuard):
                name = "guard_a"
                category = GuardCategory.SECURITY
                def check(self, ctx):
                    return None

            class GuardB(BaseGuard):
                name = "guard_b"
                category = GuardCategory.COGNITIVE
                def check(self, ctx):
                    return None
        """)
        metas = load_guard_file(path)
        assert len(metas) == 2
        names = {m.guard.name for m in metas}
        assert names == {"guard_a", "guard_b"}

    def test_file_not_found(self):
        with pytest.raises(PluginError, match="not found"):
            load_guard_file("/nonexistent/guard.py")

    def test_not_py_file(self, tmp_path):
        path = tmp_path / "guard.txt"
        path.write_text("hello")
        with pytest.raises(PluginError, match=".py"):
            load_guard_file(str(path))

    def test_no_guards_in_file(self, tmp_path):
        path = _write_plugin(tmp_path, "empty.py", """\
            x = 42
        """)
        with pytest.raises(PluginError, match="No BaseGuard"):
            load_guard_file(path)

    def test_syntax_error_in_plugin(self, tmp_path):
        path = _write_plugin(tmp_path, "broken.py", """\
            def this is broken(
        """)
        with pytest.raises(PluginError, match="Failed to load"):
            load_guard_file(path)

    def test_instantiation_error(self, tmp_path):
        path = _write_plugin(tmp_path, "bad_init.py", """\
            from cc_cortex.guards.base import BaseGuard, GuardCategory

            class BadGuard(BaseGuard):
                name = "bad_guard"
                category = GuardCategory.QUALITY
                def __init__(self):
                    raise RuntimeError("init failed")
                def check(self, ctx):
                    return None
        """)
        metas = load_guard_file(path)
        assert len(metas) == 1
        assert not metas[0].valid
        assert "init failed" in metas[0].errors[0]

    def test_guard_with_empty_name(self, tmp_path):
        path = _write_plugin(tmp_path, "noname.py", """\
            from cc_cortex.guards.base import BaseGuard, GuardCategory

            class NoNameGuard(BaseGuard):
                name = ""
                category = GuardCategory.QUALITY
                def check(self, ctx):
                    return None
        """)
        metas = load_guard_file(path)
        assert len(metas) == 1
        assert not metas[0].valid
        assert any("empty" in e for e in metas[0].errors)

    def test_abstract_class_skipped(self, tmp_path):
        """Abstract classes without check() implementation are skipped."""
        path = _write_plugin(tmp_path, "abstract.py", """\
            from abc import abstractmethod
            from cc_cortex.guards.base import BaseGuard, GuardCategory

            class AbstractGuard(BaseGuard):
                name = "abstract_guard"
                category = GuardCategory.QUALITY

            class ConcreteGuard(BaseGuard):
                name = "concrete_guard"
                category = GuardCategory.QUALITY
                def check(self, ctx):
                    return None
        """)
        metas = load_guard_file(path)
        # Only ConcreteGuard should be loaded (AbstractGuard has abstract check)
        assert len(metas) == 1
        assert metas[0].guard.name == "concrete_guard"


# ── discover_plugins ─────────────────────────────────────


class TestDiscoverPlugins:
    def test_no_plugins(self):
        metas = discover_plugins(use_entrypoints=False, paths=[])
        assert metas == []

    def test_file_path_discovery(self, tmp_path):
        path = _write_plugin(tmp_path, "disc.py", """\
            from cc_cortex.guards.base import BaseGuard, GuardCategory

            class DiscGuard(BaseGuard):
                name = "disc_guard"
                category = GuardCategory.QUALITY
                def check(self, ctx):
                    return None
        """)
        metas = discover_plugins(
            use_entrypoints=False, paths=[path],
        )
        assert len(metas) == 1
        assert metas[0].valid

    def test_duplicate_detection_across_files(self, tmp_path):
        path1 = _write_plugin(tmp_path, "g1.py", """\
            from cc_cortex.guards.base import BaseGuard, GuardCategory
            class G(BaseGuard):
                name = "same_name"
                category = GuardCategory.QUALITY
                def check(self, ctx): return None
        """)
        path2 = _write_plugin(tmp_path, "g2.py", """\
            from cc_cortex.guards.base import BaseGuard, GuardCategory
            class G(BaseGuard):
                name = "same_name"
                category = GuardCategory.QUALITY
                def check(self, ctx): return None
        """)
        metas = discover_plugins(
            use_entrypoints=False, paths=[path1, path2],
        )
        assert len(metas) == 2
        # First is valid, second has duplicate error
        assert metas[0].valid
        assert not metas[1].valid
        assert any("Duplicate" in e for e in metas[1].errors)

    def test_duplicate_with_existing_names(self, tmp_path):
        path = _write_plugin(tmp_path, "dup.py", """\
            from cc_cortex.guards.base import BaseGuard, GuardCategory
            class G(BaseGuard):
                name = "builtin_guard"
                category = GuardCategory.QUALITY
                def check(self, ctx): return None
        """)
        metas = discover_plugins(
            use_entrypoints=False,
            paths=[path],
            existing_names={"builtin_guard"},
        )
        assert len(metas) == 1
        assert not metas[0].valid

    def test_bad_path_handled(self):
        metas = discover_plugins(
            use_entrypoints=False,
            paths=["/nonexistent/bad.py"],
        )
        assert len(metas) == 1
        assert not metas[0].valid
        assert "not found" in metas[0].errors[0]

    def test_entrypoints_off(self, tmp_path):
        """With use_entrypoints=False, only file paths are scanned."""
        metas = discover_plugins(use_entrypoints=False, paths=[])
        assert metas == []


# ── create_extended_pipeline ─────────────────────────────


class TestCreateExtendedPipeline:
    def test_no_plugins(self):
        from cc_cortex.guards.registry import create_extended_pipeline

        pipe, metas = create_extended_pipeline(
            use_entrypoints=False, plugin_paths=[],
        )
        assert len(pipe._guards) > 0  # Built-in guards loaded
        assert metas == []

    def test_with_plugin_file(self, tmp_path):
        from cc_cortex.guards.registry import create_extended_pipeline

        path = _write_plugin(tmp_path, "ext.py", """\
            from cc_cortex.guards.base import BaseGuard, GuardCategory
            class ExtGuard(BaseGuard):
                name = "ext_test_guard"
                category = GuardCategory.COGNITIVE
                def check(self, ctx): return None
        """)
        pipe, metas = create_extended_pipeline(
            use_entrypoints=False, plugin_paths=[path],
        )
        names = {g.name for g in pipe._guards}
        assert "ext_test_guard" in names
        assert len(metas) == 1
        assert metas[0].valid

    def test_invalid_plugin_not_registered(self, tmp_path):
        from cc_cortex.guards.registry import (
            create_default_pipeline,
            create_extended_pipeline,
        )

        path = _write_plugin(tmp_path, "bad.py", """\
            from cc_cortex.guards.base import BaseGuard, GuardCategory
            class BadGuard(BaseGuard):
                name = ""
                category = GuardCategory.QUALITY
                def check(self, ctx): return None
        """)
        default_pipe = create_default_pipeline()
        ext_pipe, metas = create_extended_pipeline(
            use_entrypoints=False, plugin_paths=[path],
        )
        # Invalid guard should not be added
        assert len(ext_pipe._guards) == len(default_pipe._guards)
        assert not metas[0].valid
