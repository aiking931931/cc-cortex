"""Tests for concinno.insight_engine — Proactive Insight Engine."""

from __future__ import annotations

from concinno.insight_engine import (
    InsightRule,
    _builtin_rules,
    _find_best_match,
    _match_rule,
    _resolve_assertion,
    check_insight,
    load_insight_rules,
)

# ── InsightRule ───────────────────────────────────────────


class TestInsightRule:
    def test_frozen(self):
        rule = InsightRule(rule_id="test", keywords=("a",))
        try:
            rule.rule_id = "changed"  # type: ignore[misc]
            assert False, "should be frozen"
        except AttributeError:
            pass

    def test_defaults(self):
        rule = InsightRule(rule_id="r", keywords=("k",))
        assert rule.context == ()
        assert rule.assertion_key == ""
        assert rule.assertion_fallback == ""
        assert rule.confidence == 0.8


# ── _builtin_rules ────────────────────────────────────────


class TestBuiltinRules:
    def test_returns_list(self):
        rules = _builtin_rules()
        assert isinstance(rules, list)
        assert len(rules) >= 5

    def test_all_have_ids(self):
        for rule in _builtin_rules():
            assert rule.rule_id
            assert rule.keywords

    def test_unique_ids(self):
        ids = [r.rule_id for r in _builtin_rules()]
        assert len(ids) == len(set(ids))

    def test_confidence_range(self):
        for rule in _builtin_rules():
            assert 0.0 <= rule.confidence <= 1.0


# ── _match_rule ───────────────────────────────────────────


class TestMatchRule:
    def test_keyword_match(self):
        rule = InsightRule(
            rule_id="t", keywords=("API", "token"),
        )
        assert _match_rule("how much does the api cost", rule)

    def test_keyword_no_match(self):
        rule = InsightRule(
            rule_id="t", keywords=("API", "token"),
        )
        assert not _match_rule("how is the weather", rule)

    def test_context_narrows(self):
        rule = InsightRule(
            rule_id="t",
            keywords=("API",),
            context=("Claude", "subscription"),
        )
        assert _match_rule("claude api key", rule)
        assert not _match_rule("openai api key", rule)

    def test_case_insensitive(self):
        rule = InsightRule(
            rule_id="t", keywords=("API",), context=("claude",),
        )
        # _match_rule expects pre-lowered prompt (caller responsibility)
        assert _match_rule("claude api", rule)

    def test_empty_context_always_passes(self):
        rule = InsightRule(rule_id="t", keywords=("hook",))
        assert _match_rule("what is a hook", rule)


# ── _find_best_match ──────────────────────────────────────


class TestFindBestMatch:
    def test_picks_highest_confidence(self):
        r1 = InsightRule(
            rule_id="low", keywords=("test",), confidence=0.5,
        )
        r2 = InsightRule(
            rule_id="high", keywords=("test",), confidence=0.9,
        )
        result = _find_best_match("test prompt", [r1, r2], set())
        assert result is not None
        assert result.rule_id == "high"

    def test_skips_fired(self):
        r1 = InsightRule(
            rule_id="fired", keywords=("test",), confidence=0.9,
        )
        r2 = InsightRule(
            rule_id="unfired", keywords=("test",), confidence=0.5,
        )
        result = _find_best_match("test prompt", [r1, r2], {"fired"})
        assert result is not None
        assert result.rule_id == "unfired"

    def test_all_fired_returns_none(self):
        r1 = InsightRule(rule_id="a", keywords=("test",))
        result = _find_best_match("test prompt", [r1], {"a"})
        assert result is None

    def test_no_match_returns_none(self):
        r1 = InsightRule(rule_id="a", keywords=("xyz",))
        result = _find_best_match("test prompt", [r1], set())
        assert result is None


# ── _resolve_assertion ────────────────────────────────────


class TestResolveAssertion:
    def test_fallback_when_no_key(self):
        rule = InsightRule(
            rule_id="t",
            keywords=("k",),
            assertion_fallback="fallback text",
        )
        assert _resolve_assertion(rule) == "fallback text"

    def test_fallback_when_key_not_found(self):
        rule = InsightRule(
            rule_id="t",
            keywords=("k",),
            assertion_key="nonexistent.key",
            assertion_fallback="fallback text",
        )
        assert _resolve_assertion(rule) == "fallback text"

    def test_i18n_key_resolved(self):
        rule = InsightRule(
            rule_id="t",
            keywords=("k",),
            assertion_key="insight.cli_free_on_max",
            assertion_fallback="should not use this",
        )
        text = _resolve_assertion(rule)
        # Should resolve to i18n text, not fallback
        assert "CLI" in text or "cli" in text.lower()


# ── check_insight ─────────────────────────────────────────


class TestCheckInsight:
    def test_short_prompt_returns_none(self):
        assert check_insight("hi") is None

    def test_slash_command_returns_none(self):
        assert check_insight("/mode engineering 很長的提示") is None

    def test_no_match_returns_none(self):
        assert check_insight("今天天氣怎麼樣呢朋友們") is None

    def test_matching_prompt_returns_assertion(self):
        result = check_insight(
            "Claude API token cost 付費 subscription"
        )
        assert result is not None
        assert result.startswith("💡")

    def test_cli_non_interactive_match(self):
        result = check_insight(
            "我想用 CLI 非互動模式跑 claude 自動化腳本"
        )
        assert result is not None
        assert "💡" in result

    def test_empty_returns_none(self):
        assert check_insight("") is None
        assert check_insight(None) is None  # type: ignore[arg-type]

    def test_dedup_with_cache(self, tmp_path):
        cache_dir = str(tmp_path)
        sid = "test1234-abcd"
        prompt = "Claude API token cost 付費 subscription"

        r1 = check_insight(prompt, cache_dir=cache_dir, session_id=sid)
        assert r1 is not None

        # Same prompt, same session → should be deduped
        r2 = check_insight(prompt, cache_dir=cache_dir, session_id=sid)
        assert r2 is None

    def test_different_sessions_not_deduped(self, tmp_path):
        cache_dir = str(tmp_path)
        prompt = "Claude API token cost 付費 subscription"

        r1 = check_insight(
            prompt, cache_dir=cache_dir, session_id="sess1111",
        )
        r2 = check_insight(
            prompt, cache_dir=cache_dir, session_id="sess2222",
        )
        assert r1 is not None
        assert r2 is not None


# ── load_insight_rules ────────────────────────────────────


class TestLoadInsightRules:
    def test_includes_builtin(self):
        rules = load_insight_rules()
        ids = {r.rule_id for r in rules}
        assert "cli_free_on_max" in ids
        assert "hook_types" in ids

    def test_returns_insight_rules(self):
        for rule in load_insight_rules():
            assert isinstance(rule, InsightRule)
