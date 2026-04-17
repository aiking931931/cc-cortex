"""Tests for verify_before_write — Verify external references before writing."""

from concinno.core.state_store import StateStore
from concinno.guards.base import GuardAction, GuardContext
from concinno.verify_before_write import (
    VerifyBeforeWriteGuard,
    _extract_api_endpoints,
    _extract_packages,
)


def _ctx(
    tmp_path,
    tool_name="Edit",
    tool_input=None,
    *,
    hook_event="PostToolUse",
    tool_result="",
    session_id="test-session",
):
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input or {"file_path": "test.py", "new_string": "content"},
        session_id=session_id,
        cache_dir=str(tmp_path),
        hook_event=hook_event,
        tool_result=tool_result,
        workspace="",
    )


# ── _extract_packages ─────────────────────────────────────────────


class TestExtractPackages:
    def test_python_from_import(self):
        pkgs = _extract_packages("from flask import Flask")
        assert "flask" in pkgs

    def test_python_import(self):
        pkgs = _extract_packages("import requests")
        assert "requests" in pkgs

    def test_python_dotted_import(self):
        pkgs = _extract_packages("from sqlalchemy.orm import Session")
        assert "sqlalchemy" in pkgs

    def test_js_import(self):
        pkgs = _extract_packages("import axios from 'axios'")
        assert "axios" in pkgs

    def test_js_require(self):
        pkgs = _extract_packages("const express = require('express')")
        assert "express" in pkgs

    def test_scoped_npm_package(self):
        pkgs = _extract_packages("import { foo } from '@anthropic-ai/sdk'")
        assert "@anthropic-ai/sdk" in pkgs

    def test_relative_import_skipped(self):
        pkgs = _extract_packages("import { foo } from './utils'")
        assert not pkgs

    def test_stdlib_skipped(self):
        pkgs = _extract_packages("import os\nimport sys\nimport re")
        assert not pkgs

    def test_version_pin(self):
        pkgs = _extract_packages('"axios": "^1.6.0"')
        assert "axios" in pkgs

    def test_future_import_skipped(self):
        pkgs = _extract_packages("from __future__ import annotations")
        assert not pkgs


# ── _extract_api_endpoints ────────────────────────────────────────


class TestExtractApiEndpoints:
    def test_api_path(self):
        eps = _extract_api_endpoints("fetch('/api/v2/users')")
        assert any("/api/v2/users" in e for e in eps)

    def test_full_url_with_version(self):
        eps = _extract_api_endpoints("url = 'https://api.example.com/v3/data'")
        assert len(eps) == 1

    def test_localhost_skipped(self):
        eps = _extract_api_endpoints("url = 'https://localhost/v2/test'")
        assert not eps

    def test_no_match(self):
        eps = _extract_api_endpoints("just some normal text")
        assert not eps


# ── VerifyBeforeWriteGuard.check() ────────────────────────────────


class TestVerifyBeforeWriteCheck:
    def test_check_always_returns_none(self, tmp_path):
        guard = VerifyBeforeWriteGuard()
        ctx = _ctx(tmp_path, hook_event="PreToolUse")
        assert guard.check(ctx) is None


# ── VerifyBeforeWriteGuard.on_post_tool() ─────────────────────────


class TestVerifyBeforeWriteOnPostTool:
    def test_tracks_read_as_verification(self, tmp_path):
        guard = VerifyBeforeWriteGuard()
        ctx = _ctx(
            tmp_path,
            tool_name="Read",
            tool_input={"file_path": "node_modules/axios/index.js"},
            tool_result="module.exports = ...",
        )
        guard.on_post_tool(ctx)
        store = StateStore(str(tmp_path))
        state = store.read("verify_write", "test-session", default={})
        assert "axios" in state.get("verified_packages", [])

    def test_tracks_grep_pattern_as_verification(self, tmp_path):
        guard = VerifyBeforeWriteGuard()
        ctx = _ctx(
            tmp_path,
            tool_name="Grep",
            tool_input={"pattern": "flask", "path": "src/"},
            tool_result="found matches",
        )
        guard.on_post_tool(ctx)
        store = StateStore(str(tmp_path))
        state = store.read("verify_write", "test-session", default={})
        assert "flask" in state.get("verified_packages", [])

    def test_flags_unverified_import(self, tmp_path):
        guard = VerifyBeforeWriteGuard()
        content = "from pandas import DataFrame\ndf = DataFrame(data)"
        ctx = _ctx(
            tmp_path,
            tool_name="Edit",
            tool_input={"file_path": "analysis.py", "new_string": content},
        )
        result = guard.on_post_tool(ctx)
        assert result is not None
        assert result.action == GuardAction.ALLOW
        assert "pandas" in result.context

    def test_no_flag_after_verification(self, tmp_path):
        guard = VerifyBeforeWriteGuard()
        # First: read pandas source
        read_ctx = _ctx(
            tmp_path,
            tool_name="Read",
            tool_input={"file_path": "site-packages/pandas/core.py"},
            tool_result="pandas source",
        )
        guard.on_post_tool(read_ctx)

        # Then: write pandas import — should be clean
        content = "from pandas import DataFrame\ndf = DataFrame(data)"
        write_ctx = _ctx(
            tmp_path,
            tool_name="Edit",
            tool_input={"file_path": "analysis.py", "new_string": content},
        )
        result = guard.on_post_tool(write_ctx)
        assert result is None

    def test_skips_short_content(self, tmp_path):
        guard = VerifyBeforeWriteGuard()
        ctx = _ctx(
            tmp_path,
            tool_name="Edit",
            tool_input={"file_path": "test.py", "new_string": "x = 1"},
        )
        result = guard.on_post_tool(ctx)
        assert result is None

    def test_skips_non_write_tools(self, tmp_path):
        guard = VerifyBeforeWriteGuard()
        ctx = _ctx(
            tmp_path,
            tool_name="Bash",
            tool_input={"command": "ls"},
        )
        result = guard.on_post_tool(ctx)
        assert result is None

    def test_no_cache_dir_returns_none(self):
        guard = VerifyBeforeWriteGuard()
        ctx = GuardContext(
            tool_name="Edit",
            tool_input={"file_path": "test.py", "new_string": "import pandas"},
            session_id="s",
            cache_dir="",
            hook_event="PostToolUse",
        )
        assert guard.on_post_tool(ctx) is None

    def test_flags_api_endpoint(self, tmp_path):
        guard = VerifyBeforeWriteGuard()
        content = "const url = 'https://api.stripe.com/v2/charges'; fetch(url);"
        ctx = _ctx(
            tmp_path,
            tool_name="Write",
            tool_input={"file_path": "client.js", "content": content},
        )
        result = guard.on_post_tool(ctx)
        assert result is not None
        assert result.action == GuardAction.ALLOW
        assert "API" in result.context or "端點" in result.context

    def test_no_external_refs_no_flag(self, tmp_path):
        guard = VerifyBeforeWriteGuard()
        content = "import os\nimport sys\nprint('hello world, this is a test')"
        ctx = _ctx(
            tmp_path,
            tool_name="Edit",
            tool_input={"file_path": "main.py", "new_string": content},
        )
        result = guard.on_post_tool(ctx)
        assert result is None

    def test_records_flag_count(self, tmp_path):
        guard = VerifyBeforeWriteGuard()
        content = "from numpy import array\narr = array([1,2,3])"
        for _ in range(2):
            ctx = _ctx(
                tmp_path,
                tool_name="Edit",
                tool_input={"file_path": "math.py", "new_string": content},
            )
            guard.on_post_tool(ctx)

        store = StateStore(str(tmp_path))
        state = store.read("verify_write", "test-session", default={})
        assert state.get("flag_count", 0) == 2
