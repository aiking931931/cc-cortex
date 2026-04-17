"""Tests for ``concinno.agent.sentinel_parser``."""

from __future__ import annotations

from concinno.agent.sentinel_parser import extract_sentinel_answer


class TestExtractSentinelAnswer:
    def test_simple(self) -> None:
        assert extract_sentinel_answer("FINAL ANSWER: 42") == "42"

    def test_case_insensitive(self) -> None:
        assert extract_sentinel_answer("final answer: foo") == "foo"

    def test_multi_occurrence_takes_last(self) -> None:
        text = (
            "First I thought FINAL ANSWER: wrong\n"
            "Let me recompute.\n"
            "FINAL ANSWER: 42"
        )
        assert extract_sentinel_answer(text) == "42"

    def test_multi_occurrence_takes_first(self) -> None:
        text = "FINAL ANSWER: first\nFINAL ANSWER: second"
        assert (
            extract_sentinel_answer(text, take_last=False) == "first"
        )

    def test_rule_quote_ignored(self) -> None:
        text = (
            "precede with `FINAL ANSWER:` then value.\n"
            "Since answer is 34689.\n"
            "<channel|>FINAL ANSWER: 34689"
        )
        assert extract_sentinel_answer(text) == "34689"

    def test_no_sentinel_returns_none(self) -> None:
        assert extract_sentinel_answer("no sentinel here") is None

    def test_empty_text(self) -> None:
        assert extract_sentinel_answer("") is None

    def test_trailing_backtick_stripped(self) -> None:
        assert extract_sentinel_answer("FINAL ANSWER: `") is None
        assert extract_sentinel_answer("FINAL ANSWER: `42`") == "42"

    def test_max_len_truncate(self) -> None:
        long = "FINAL ANSWER: " + ("abc. " * 100)
        out = extract_sentinel_answer(long, max_len=50)
        assert out is not None
        assert len(out) <= 50

    def test_custom_sentinel(self) -> None:
        text = "Foo bar ANSWER => baz"
        assert (
            extract_sentinel_answer(text, sentinel="ANSWER =>")
            == "baz"
        )

    def test_whitespace_collapse(self) -> None:
        assert extract_sentinel_answer("FINAL ANSWER:    42  ") == "42"

    def test_empty_value_returns_none(self) -> None:
        assert extract_sentinel_answer("FINAL ANSWER:\n") is None
