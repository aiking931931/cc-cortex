"""Tests for hallucination_guard — Detect unsourced assertions in written content."""

from cc_cortex.core.state_store import StateStore
from cc_cortex.guards.base import GuardAction, GuardContext
from cc_cortex.hallucination_guard import (
    HallucinationGuard,
    _is_code_file,
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
        tool_input=tool_input or {"file_path": "test.md", "new_string": "content"},
        session_id=session_id,
        cache_dir=str(tmp_path),
        hook_event=hook_event,
        tool_result=tool_result,
        workspace="",
    )


# ── _is_code_file ──────────────────────────────────────────────────────────────


class TestIsCodeFile:
    def test_py(self):
        assert _is_code_file("src/main.py")

    def test_js(self):
        assert _is_code_file("app.js")

    def test_json(self):
        assert _is_code_file("package.json")

    def test_ts(self):
        assert _is_code_file("index.ts")

    def test_tsx(self):
        assert _is_code_file("App.tsx")

    def test_yaml(self):
        assert _is_code_file("config.yaml")

    def test_md_is_not_code(self):
        assert not _is_code_file("README.md")

    def test_txt_is_not_code(self):
        assert not _is_code_file("notes.txt")

    def test_empty_is_not_code(self):
        assert not _is_code_file("")


# ── HallucinationGuard.check() ─────────────────────────────────────────────────


class TestHallucinationGuardCheck:
    def test_check_always_returns_none(self, tmp_path):
        guard = HallucinationGuard()
        ctx = _ctx(tmp_path, hook_event="PreToolUse")
        assert guard.check(ctx) is None

    def test_check_returns_none_regardless_of_tool(self, tmp_path):
        guard = HallucinationGuard()
        ctx = _ctx(
            tmp_path,
            tool_name="Bash",
            tool_input={"command": "ls"},
            hook_event="PreToolUse",
        )
        assert guard.check(ctx) is None


# ── HallucinationGuard.on_post_tool() ─────────────────────────────────────────


class TestHallucinationGuardOnPostTool:
    def test_tracks_evidence_on_read(self, tmp_path):
        guard = HallucinationGuard()
        ctx = _ctx(
            tmp_path,
            tool_name="Read",
            tool_input={"file_path": "source.md"},
            tool_result="some research content here",
        )
        guard.on_post_tool(ctx)
        store = StateStore(str(tmp_path))
        state = store.read("hallucination_guard", "test-session", default={})
        assert state.get("evidence_count", 0) == 1

    def test_tracks_evidence_accumulates(self, tmp_path):
        guard = HallucinationGuard()
        for _ in range(3):
            ctx = _ctx(
                tmp_path,
                tool_name="Read",
                tool_input={"file_path": "source.md"},
                tool_result="content",
            )
            guard.on_post_tool(ctx)
        store = StateStore(str(tmp_path))
        state = store.read("hallucination_guard", "test-session", default={})
        assert state.get("evidence_count", 0) == 3

    def test_skips_code_files(self, tmp_path):
        guard = HallucinationGuard()
        ctx = _ctx(
            tmp_path,
            tool_name="Edit",
            tool_input={"file_path": "main.py", "new_string": "提升了 30% 的效能"},
        )
        result = guard.on_post_tool(ctx)
        assert result is None

    def test_flags_unsourced_claim_in_md_low_evidence(self, tmp_path):
        guard = HallucinationGuard()
        # No evidence reads yet (evidence_count = 0)
        content = "這項改進提升了 30% 的效能，效果顯著，大幅超越原始基準水準。（共超過三十字）"
        ctx = _ctx(
            tmp_path,
            tool_name="Edit",
            tool_input={"file_path": "report.md", "new_string": content},
        )
        result = guard.on_post_tool(ctx)
        assert result is not None
        assert result.action == GuardAction.ALLOW
        assert "幻覺" in result.context or "未驗證" in result.context

    def test_allows_claims_when_evidence_count_gte_3(self, tmp_path):
        guard = HallucinationGuard()
        store = StateStore(str(tmp_path))
        store.write("hallucination_guard", "test-session", {"evidence_count": 3})
        content = "研究顯示提升了 30% 的效能，效果極為顯著。"
        ctx = _ctx(
            tmp_path,
            tool_name="Edit",
            tool_input={"file_path": "report.md", "new_string": content},
        )
        result = guard.on_post_tool(ctx)
        assert result is None

    def test_no_claims_no_flag(self, tmp_path):
        guard = HallucinationGuard()
        ctx = _ctx(
            tmp_path,
            tool_name="Edit",
            tool_input={"file_path": "notes.md", "new_string": "這是一段普通的說明文字。"},
        )
        result = guard.on_post_tool(ctx)
        assert result is None

    def test_no_cache_dir_returns_none(self):
        guard = HallucinationGuard()
        ctx = GuardContext(
            tool_name="Edit",
            tool_input={"file_path": "report.md", "new_string": "提升了 30%"},
            session_id="s",
            cache_dir="",
            hook_event="PostToolUse",
        )
        assert guard.on_post_tool(ctx) is None


class TestBashEvidenceCredit:
    """Bash output that looks like a real fetch counts as evidence.

    Without this, sessions that verified every claim via curl/gh api/
    git show were stuck at evidence_count=0 and triggered the warning
    on every Write/Edit.
    """

    def _read_count(self, tmp_path, sid="bash-sess"):
        store = StateStore(str(tmp_path))
        return store.read("hallucination_guard", sid, default={}).get(
            "evidence_count", 0,
        )

    def test_http_200_status_line_credits(self, tmp_path):
        guard = HallucinationGuard()
        ctx = _ctx(
            tmp_path,
            tool_name="Bash",
            tool_input={"command": "curl -i https://example.com"},
            tool_result="HTTP/1.1 200 OK\nContent-Type: text/html",
            session_id="bash-sess",
        )
        guard.on_post_tool(ctx)
        assert self._read_count(tmp_path) == 1

    def test_http2_200_credits(self, tmp_path):
        guard = HallucinationGuard()
        ctx = _ctx(
            tmp_path,
            tool_name="Bash",
            tool_input={"command": "curl --http2 https://api.example.com"},
            tool_result="HTTP/2 200\ncontent-length: 42",
            session_id="bash-sess",
        )
        guard.on_post_tool(ctx)
        assert self._read_count(tmp_path) == 1

    def test_json_status_field_credits(self, tmp_path):
        guard = HallucinationGuard()
        ctx = _ctx(
            tmp_path,
            tool_name="Bash",
            tool_input={"command": "gh api repos/x/y"},
            tool_result='{"status_code": 200, "data": {"id": 1}}',
            session_id="bash-sess",
        )
        guard.on_post_tool(ctx)
        assert self._read_count(tmp_path) == 1

    def test_raw_json_body_credits(self, tmp_path):
        guard = HallucinationGuard()
        ctx = _ctx(
            tmp_path,
            tool_name="Bash",
            tool_input={"command": "curl https://api.example.com/users/1"},
            tool_result='{"id": 1, "name": "Alice"}',
            session_id="bash-sess",
        )
        guard.on_post_tool(ctx)
        assert self._read_count(tmp_path) == 1

    def test_plain_bash_does_not_credit(self, tmp_path):
        guard = HallucinationGuard()
        ctx = _ctx(
            tmp_path,
            tool_name="Bash",
            tool_input={"command": "echo hello"},
            tool_result="hello\n",
            session_id="bash-sess",
        )
        guard.on_post_tool(ctx)
        assert self._read_count(tmp_path) == 0

    def test_empty_result_does_not_crash(self, tmp_path):
        guard = HallucinationGuard()
        ctx = _ctx(
            tmp_path,
            tool_name="Bash",
            tool_input={"command": "true"},
            tool_result="",
            session_id="bash-sess",
        )
        # Should not raise, should not credit.
        guard.on_post_tool(ctx)
        assert self._read_count(tmp_path) == 0
