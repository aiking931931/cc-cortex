"""Tests for concinno.agent.fork_context.

Covers:
  - CacheSafeParams immutability, hashability, identical_to semantics
  - tool_defs_hash canonicalization and tool_names fallback
  - FileStateCache get/set/delete/len/contains + clone isolation
  - create_subagent_context isolation + metadata copy
  - SubagentContext.clone_for_child depth semantics
  - is_in_fork_child helper
  - ForkDepthExceeded exception type

Target: >=18 tests (actual: 22).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from concinno.agent.fork_context import (
    CacheSafeParams,
    FileStateCache,
    ForkDepthExceeded,
    SubagentContext,
    create_cache_safe_params,
    create_subagent_context,
    is_in_fork_child,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _make_params(**overrides: Any) -> CacheSafeParams:
    defaults: dict[str, Any] = dict(
        system_prompt="you are a helpful assistant",
        tool_defs=[
            {"name": "Read", "input_schema": {"type": "object"}},
            {"name": "Edit", "input_schema": {"type": "object"}},
        ],
        betas=("context-1m-2025-08-07",),
        effort="medium",
        parent_session_id="sess-abc",
        fork_depth=0,
    )
    defaults.update(overrides)
    return create_cache_safe_params(**defaults)


def _make_cache(entries: dict[str, Any] | None = None) -> FileStateCache[Any]:
    cache: FileStateCache[Any] = FileStateCache()
    for k, v in (entries or {}).items():
        cache.set(k, v)
    return cache


# --------------------------------------------------------------------------- #
# CacheSafeParams — immutability + identical_to
# --------------------------------------------------------------------------- #


def test_cache_safe_params_frozen_is_hashable() -> None:
    p = _make_params()
    # frozen dataclass → must be hashable
    _ = hash(p)
    s = {p}
    assert p in s
    # and mutation must be rejected
    with pytest.raises(FrozenInstanceError):
        p.system_prompt = "mutated"  # type: ignore[misc]


def test_identical_to_compares_fields_but_not_metadata() -> None:
    parent = _make_params(parent_session_id="sess-A", fork_depth=0)
    child = _make_params(parent_session_id="sess-B", fork_depth=3)
    # metadata (session id, depth) differs — identical_to should still pass
    assert parent.identical_to(child) is True
    assert child.identical_to(parent) is True


def test_identical_to_detects_system_prompt_change() -> None:
    a = _make_params(system_prompt="prompt one")
    b = _make_params(system_prompt="prompt two")
    assert a.identical_to(b) is False


def test_identical_to_detects_tool_defs_change() -> None:
    a = _make_params(tool_defs=[{"name": "Read"}])
    b = _make_params(tool_defs=[{"name": "Read"}, {"name": "Edit"}])
    assert a.identical_to(b) is False


def test_identical_to_detects_betas_change() -> None:
    a = _make_params(betas=("beta-a",))
    b = _make_params(betas=("beta-a", "beta-b"))
    assert a.identical_to(b) is False


# --------------------------------------------------------------------------- #
# create_cache_safe_params — canonicalization + tool_names
# --------------------------------------------------------------------------- #


def test_tool_defs_hash_canonical_across_key_order() -> None:
    # Same content, different key insertion order → same hash.
    a = create_cache_safe_params(
        system_prompt="sys",
        tool_defs=[
            {"name": "Read", "description": "reads files", "version": 1},
        ],
    )
    b = create_cache_safe_params(
        system_prompt="sys",
        tool_defs=[
            {"version": 1, "description": "reads files", "name": "Read"},
        ],
    )
    assert a.tool_defs_hash == b.tool_defs_hash


def test_tool_defs_hash_differs_when_tool_added() -> None:
    a = create_cache_safe_params(
        system_prompt="sys",
        tool_defs=[{"name": "Read"}],
    )
    b = create_cache_safe_params(
        system_prompt="sys",
        tool_defs=[{"name": "Read"}, {"name": "Edit"}],
    )
    assert a.tool_defs_hash != b.tool_defs_hash


def test_tool_names_fallback_for_missing_name() -> None:
    p = create_cache_safe_params(
        system_prompt="sys",
        tool_defs=[
            {"name": "Read"},
            {"description": "no name here"},
            {"name": ""},  # empty → fallback
        ],
    )
    assert p.tool_names == ("Read", "tool_1", "tool_2")


# --------------------------------------------------------------------------- #
# FileStateCache — basic API
# --------------------------------------------------------------------------- #


def test_file_state_cache_get_set_delete() -> None:
    cache: FileStateCache[str] = FileStateCache()
    assert cache.get("a") is None
    cache.set("a", "alpha")
    assert cache.get("a") == "alpha"
    cache.delete("a")
    assert cache.get("a") is None
    # delete of non-existent key must not raise
    cache.delete("ghost")


def test_file_state_cache_len_and_contains() -> None:
    cache: FileStateCache[int] = FileStateCache()
    assert len(cache) == 0
    cache.set("a", 1)
    cache.set("b", 2)
    assert len(cache) == 2
    assert "a" in cache
    assert "b" in cache
    assert "c" not in cache
    # iteration over keys
    assert sorted(iter(cache)) == ["a", "b"]
    assert sorted(cache.keys()) == ["a", "b"]


# --------------------------------------------------------------------------- #
# FileStateCache — clone isolation (the core invariant)
# --------------------------------------------------------------------------- #


def test_file_state_cache_clone_preserves_current_entries() -> None:
    parent = _make_cache({"/a": "A", "/b": "B"})
    fork = parent.clone()
    assert fork.get("/a") == "A"
    assert fork.get("/b") == "B"
    assert len(fork) == 2


def test_file_state_cache_clone_isolates_parent() -> None:
    parent = _make_cache({"/shared": "orig"})
    fork = parent.clone()

    # mutation in fork must NOT touch parent
    fork.set("/fork_only", "F")
    fork.set("/shared", "fork-new")
    fork.delete("/shared")
    assert parent.get("/shared") == "orig"
    assert parent.get("/fork_only") is None
    assert "/fork_only" not in parent

    # mutation in parent must NOT touch fork
    parent.set("/parent_only", "P")
    parent.set("/shared", "parent-new")
    assert fork.get("/parent_only") is None
    assert "/parent_only" not in fork
    # fork already deleted /shared above → still absent
    assert fork.get("/shared") is None


# --------------------------------------------------------------------------- #
# create_subagent_context
# --------------------------------------------------------------------------- #


def test_create_subagent_context_clones_file_state() -> None:
    parent_params = _make_params()
    parent_cache = _make_cache({"/x": "X"})
    ctx = create_subagent_context(
        parent_params=parent_params,
        parent_file_state=parent_cache,
        parent_transcript_ref="transcript-id-1",
    )
    assert ctx.file_state is not parent_cache
    assert ctx.file_state.get("/x") == "X"
    # mutate fork → parent untouched
    ctx.file_state.set("/y", "Y")
    assert "/y" not in parent_cache
    assert "/y" in ctx.file_state


def test_create_subagent_context_does_not_mutate_parent() -> None:
    parent_params = _make_params()
    parent_cache = _make_cache({"/x": "X"})
    before_keys = parent_cache.keys()
    before_len = len(parent_cache)
    ctx = create_subagent_context(
        parent_params=parent_params,
        parent_file_state=parent_cache,
        parent_transcript_ref="ref",
        metadata={"label": "fork-1"},
    )
    assert parent_cache.keys() == before_keys
    assert len(parent_cache) == before_len
    # params are shared by reference (they're frozen — safe)
    assert ctx.params is parent_params


def test_create_subagent_context_new_metadata_dict() -> None:
    parent_params = _make_params()
    parent_cache = _make_cache()
    caller_meta: dict[str, Any] = {"label": "fork-1", "depth_hint": 0}
    ctx = create_subagent_context(
        parent_params=parent_params,
        parent_file_state=parent_cache,
        parent_transcript_ref="ref",
        metadata=caller_meta,
    )
    assert ctx.metadata == caller_meta
    assert ctx.metadata is not caller_meta
    # mutating the caller's dict must NOT leak into ctx
    caller_meta["label"] = "mutated"
    caller_meta["new_key"] = "new"
    assert ctx.metadata["label"] == "fork-1"
    assert "new_key" not in ctx.metadata

    # None metadata → empty dict
    ctx2 = create_subagent_context(
        parent_params=parent_params,
        parent_file_state=parent_cache,
        parent_transcript_ref="ref",
        metadata=None,
    )
    assert ctx2.metadata == {}


# --------------------------------------------------------------------------- #
# SubagentContext.clone_for_child
# --------------------------------------------------------------------------- #


def test_clone_for_child_increments_depth() -> None:
    parent_params = _make_params(fork_depth=0)
    ctx = SubagentContext(
        params=parent_params,
        file_state=_make_cache(),
        parent_transcript_ref="ref",
    )
    child = ctx.clone_for_child()
    assert child.params.fork_depth == 1
    grandchild = child.clone_for_child()
    assert grandchild.params.fork_depth == 2
    # parent's depth untouched
    assert ctx.params.fork_depth == 0


def test_clone_for_child_preserves_cache_safe_fields() -> None:
    parent_params = _make_params(fork_depth=0)
    ctx = SubagentContext(
        params=parent_params,
        file_state=_make_cache({"/a": "A"}),
        parent_transcript_ref="ref",
        metadata={"k": "v"},
    )
    child = ctx.clone_for_child()
    # cache-affecting fields identical
    assert child.params.identical_to(parent_params)
    # file_state cloned (not shared)
    assert child.file_state is not ctx.file_state
    assert child.file_state.get("/a") == "A"
    # metadata shallow-copied
    assert child.metadata == {"k": "v"}
    assert child.metadata is not ctx.metadata
    child.metadata["k"] = "mutated"
    assert ctx.metadata["k"] == "v"


def test_clone_for_child_disable_depth_increment() -> None:
    parent_params = _make_params(fork_depth=5)
    ctx = SubagentContext(
        params=parent_params,
        file_state=_make_cache(),
        parent_transcript_ref="ref",
    )
    child = ctx.clone_for_child(increment_depth=False)
    assert child.params.fork_depth == 5


# --------------------------------------------------------------------------- #
# is_in_fork_child
# --------------------------------------------------------------------------- #


def test_is_in_fork_child_none_returns_false() -> None:
    assert is_in_fork_child(None) is False


def test_is_in_fork_child_depth_zero_returns_false() -> None:
    ctx = SubagentContext(
        params=_make_params(fork_depth=0),
        file_state=_make_cache(),
        parent_transcript_ref="ref",
    )
    assert is_in_fork_child(ctx) is False


def test_is_in_fork_child_depth_positive_returns_true() -> None:
    ctx = SubagentContext(
        params=_make_params(fork_depth=1),
        file_state=_make_cache(),
        parent_transcript_ref="ref",
    )
    assert is_in_fork_child(ctx) is True
    ctx2 = SubagentContext(
        params=_make_params(fork_depth=7),
        file_state=_make_cache(),
        parent_transcript_ref="ref",
    )
    assert is_in_fork_child(ctx2) is True


# --------------------------------------------------------------------------- #
# ForkDepthExceeded
# --------------------------------------------------------------------------- #


def test_fork_depth_exceeded_subclass_of_runtime_error() -> None:
    assert issubclass(ForkDepthExceeded, RuntimeError)
    err = ForkDepthExceeded("too deep: 4 > max 3")
    assert isinstance(err, RuntimeError)
    assert "too deep" in str(err)
    # must be catchable as both its own type and RuntimeError
    with pytest.raises(ForkDepthExceeded):
        raise err
    with pytest.raises(RuntimeError):
        raise ForkDepthExceeded("again")
