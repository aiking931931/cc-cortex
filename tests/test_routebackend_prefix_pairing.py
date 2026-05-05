"""Tests for concinno.routebackend_prefix_pairing module."""

from __future__ import annotations

from pathlib import Path

from concinno.guards.base import GuardContext
from concinno.routebackend_prefix_pairing import (
    RoutebackendPrefixPairingGuard,
    _extract_model_aliases,
    _extract_routed_prefixes,
    _model_prefix,
)

# ── helpers ──────────────────────────────────────────────


def _ctx(
    *,
    tool_name: str = "Edit",
    file_path: str = "",
    workspace: str = "",
    hook_event: str = "PostToolUse",
) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        tool_input={"file_path": file_path},
        session_id="test-session",
        cache_dir="",
        hook_event=hook_event,
        workspace=workspace,
    )


def _make_psyche_layout(
    tmp_path: Path,
    *,
    cognition_text: str,
    anthropic_text: str,
    cognition_filename: str = "mode-extract.ts",
) -> tuple[Path, Path]:
    psyche = tmp_path / "psyche-engine"
    src = psyche / "src"
    cognition_dir = src / "cognition"
    cognition_dir.mkdir(parents=True)
    cognition_file = cognition_dir / cognition_filename
    cognition_file.write_text(cognition_text, encoding="utf-8")
    anthropic_file = src / "anthropic.ts"
    anthropic_file.write_text(anthropic_text, encoding="utf-8")
    return cognition_file, anthropic_file


# ── unit helpers ─────────────────────────────────────────


def test_model_prefix_basic() -> None:
    assert _model_prefix("qwen3-3b-nsfw") == "qwen3"
    assert _model_prefix("gemma4-nsfw") == "gemma4"
    assert _model_prefix("claude-3-7-sonnet") == "claude"
    assert _model_prefix("plain") == "plain"
    assert _model_prefix("") == ""


def test_extract_model_aliases() -> None:
    text = """
        const opts = { model: 'qwen3-3b-nsfw', temperature: 0.2 };
        const other = { model: "gemma4-nsfw" };
        // model: 'commented-out'  ← regex still picks this up; OK as
        // operators rarely leave commented aliases referencing fresh
        // backends, and false-positives are warn-only.
    """
    aliases = _extract_model_aliases(text)
    assert "qwen3-3b-nsfw" in aliases
    assert "gemma4-nsfw" in aliases


def test_extract_routed_prefixes() -> None:
    text = """
        export function isSancioRouted(model: string): boolean {
            return model.startsWith('gemma4')
                || model.startsWith("qwen3")
                || model.startsWith('phi-mini');
        }
    """
    routed = _extract_routed_prefixes(text)
    assert "gemma4" in routed
    assert "qwen3" in routed
    assert "phi-mini" in routed


# ── guard behaviour ──────────────────────────────────────


def test_guard_silent_when_all_aliases_routed(tmp_path: Path) -> None:
    cognition_file, _ = _make_psyche_layout(
        tmp_path,
        cognition_text="""
            const stage1 = { model: 'qwen3-3b-nsfw' };
            const stage2 = { model: 'gemma4-nsfw' };
        """,
        anthropic_text="""
            export function isSancioRouted(m: string) {
                return m.startsWith('qwen3') || m.startsWith('gemma4');
            }
        """,
    )
    guard = RoutebackendPrefixPairingGuard()
    result = guard.on_post_tool(
        _ctx(file_path=str(cognition_file), workspace=str(tmp_path)),
    )
    assert result is None


def test_guard_warns_when_alias_missing_from_router(tmp_path: Path) -> None:
    cognition_file, _ = _make_psyche_layout(
        tmp_path,
        cognition_text="""
            const stage1 = { model: 'qwen3-3b-nsfw' };
            const stage2 = { model: 'gemma4-nsfw' };
        """,
        anthropic_text="""
            export function isSancioRouted(m: string) {
                return m.startsWith('gemma4');
            }
        """,
    )
    guard = RoutebackendPrefixPairingGuard()
    result = guard.on_post_tool(
        _ctx(file_path=str(cognition_file), workspace=str(tmp_path)),
    )
    assert result is not None
    assert result.advisory is True
    assert "qwen3-3b-nsfw" in result.context
    assert "qwen3" in result.context
    # gemma4 IS paired so it should NOT be in the missing list
    assert "alias='gemma4-nsfw'" not in result.context


def test_guard_ignores_claude_prefixes(tmp_path: Path) -> None:
    cognition_file, _ = _make_psyche_layout(
        tmp_path,
        cognition_text="""
            const fallback = { model: 'claude-3-7-sonnet' };
            const stage1 = { model: 'gemma4-nsfw' };
        """,
        anthropic_text="""
            export function isSancioRouted(m: string) {
                return m.startsWith('gemma4');
            }
        """,
    )
    guard = RoutebackendPrefixPairingGuard()
    result = guard.on_post_tool(
        _ctx(file_path=str(cognition_file), workspace=str(tmp_path)),
    )
    # claude-* is ignored, gemma4 is paired → silent
    assert result is None


def test_guard_skips_non_cognition_paths(tmp_path: Path) -> None:
    other = tmp_path / "psyche-engine" / "src" / "anthropic.ts"
    other.parent.mkdir(parents=True)
    other.write_text(
        "export function isSancioRouted(m: string) {"
        " return m.startsWith('gemma4'); }",
        encoding="utf-8",
    )
    guard = RoutebackendPrefixPairingGuard()
    result = guard.on_post_tool(
        _ctx(file_path=str(other), workspace=str(tmp_path)),
    )
    assert result is None


def test_guard_skips_read_tool(tmp_path: Path) -> None:
    cognition_file, _ = _make_psyche_layout(
        tmp_path,
        cognition_text="const x = { model: 'qwen3-3b-nsfw' };",
        anthropic_text="// no router yet",
    )
    guard = RoutebackendPrefixPairingGuard()
    result = guard.on_post_tool(
        _ctx(
            tool_name="Read",
            file_path=str(cognition_file),
            workspace=str(tmp_path),
        ),
    )
    assert result is None


def test_guard_handles_dist_js_paths(tmp_path: Path) -> None:
    psyche = tmp_path / "psyche-engine"
    dist = psyche / "dist" / "cognition"
    dist.mkdir(parents=True)
    js = dist / "mode-extract.js"
    js.write_text(
        "const stage = { model: 'qwen3-3b-nsfw' };",
        encoding="utf-8",
    )
    src = psyche / "src"
    src.mkdir()
    (src / "anthropic.ts").write_text(
        "export function isSancioRouted(m) { return m.startsWith('gemma4'); }",
        encoding="utf-8",
    )
    guard = RoutebackendPrefixPairingGuard()
    result = guard.on_post_tool(
        _ctx(file_path=str(js), workspace=str(tmp_path)),
    )
    assert result is not None
    assert "qwen3-3b-nsfw" in result.context


def test_check_returns_none(tmp_path: Path) -> None:
    cognition_file, _ = _make_psyche_layout(
        tmp_path,
        cognition_text="const x = { model: 'qwen3-3b-nsfw' };",
        anthropic_text="// empty",
    )
    guard = RoutebackendPrefixPairingGuard()
    # PreToolUse path
    result = guard.check(
        _ctx(
            file_path=str(cognition_file),
            workspace=str(tmp_path),
            hook_event="PreToolUse",
        ),
    )
    assert result is None
