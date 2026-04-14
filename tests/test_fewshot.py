"""Tests for cc_cortex.fewshot — generic solved-case store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cc_cortex.fewshot import (
    DEFAULT_STOP_WORDS,
    FewshotBank,
    FewshotCase,
    load_bank,
    retrieve_fewshot,
)


def _case(cid: str, desc: str, resp: str = "r", tags: tuple[str, ...] = ()) -> FewshotCase:
    return FewshotCase(id=cid, description=desc, response=resp, tags=tags)


def test_empty_bank_retrieve_returns_empty() -> None:
    bank = FewshotBank()
    assert bank.retrieve("anything", k=5) == []
    assert len(bank) == 0


def test_single_case_retrieve_returns_it() -> None:
    bank = FewshotBank([_case("c1", "buffer overflow in parser")])
    got = bank.retrieve("parser buffer overflow", k=2)
    assert len(got) == 1
    assert got[0].id == "c1"
    assert got[0].score > 0.0


def test_k_larger_than_bank_returns_all() -> None:
    bank = FewshotBank(
        [
            _case("a", "alpha beta gamma"),
            _case("b", "alpha delta epsilon"),
        ]
    )
    got = bank.retrieve("alpha", k=10)
    assert len(got) == 2


def test_disjoint_query_zero_score() -> None:
    bank = FewshotBank([_case("c1", "buffer overflow parser")])
    got = bank.retrieve("completely unrelated topic matter", k=1)
    # Jaccard > 0 required by default min_score=0.0, so 0.0 score is kept.
    assert len(got) == 1
    assert got[0].score == 0.0


def test_jaccard_ordering_matches_overlap() -> None:
    bank = FewshotBank(
        [
            _case("low", "alpha xyzzy foobar quux"),
            _case("high", "alpha beta gamma delta"),
            _case("mid", "alpha beta xyzzy foobar"),
        ]
    )
    got = bank.retrieve("alpha beta gamma", k=3)
    ids = [c.id for c in got]
    assert ids[0] == "high"
    assert got[0].score > got[1].score >= got[2].score


def test_stop_words_filtered() -> None:
    bank = FewshotBank([_case("c1", "the quick brown fox")])
    # Query uses only stop words + a content word not in the case.
    got = bank.retrieve("the and or but", k=1)
    # Query tokens empty after stop-word filter → union contains only case
    # tokens (quick, brown, fox), inter=0 → score 0.
    assert len(got) == 1
    assert got[0].score == 0.0


def test_required_tags_filter() -> None:
    bank = FewshotBank(
        [
            _case("a", "alpha beta gamma", tags=("python", "security")),
            _case("b", "alpha beta gamma", tags=("python",)),
            _case("c", "alpha beta gamma", tags=("security",)),
        ]
    )
    got = bank.retrieve("alpha beta", k=5, required_tags=("python", "security"))
    assert [c.id for c in got] == ["a"]


def test_min_score_filter() -> None:
    bank = FewshotBank(
        [
            _case("high", "alpha beta gamma delta"),
            _case("low", "alpha xyzzy foobar quux"),
        ]
    )
    got = bank.retrieve("alpha beta gamma", k=5, min_score=0.5)
    # Only "high" should reach 0.5 Jaccard.
    assert [c.id for c in got] == ["high"]
    assert got[0].score >= 0.5


def test_cjk_custom_tokenizer() -> None:
    def cjk_tok(text: str) -> set[str]:
        return {ch for ch in text if "\u4e00" <= ch <= "\u9fff"}

    bank = FewshotBank(
        [
            _case("c1", "緩衝區溢位漏洞分析"),
            _case("c2", "完全不同的內容主題"),
        ],
        tokenizer=cjk_tok,
    )
    got = bank.retrieve("緩衝區溢位", k=1)
    assert len(got) == 1
    assert got[0].id == "c1"
    assert got[0].score > 0.0


def test_cybergym_legacy_format_task_id_synthesized(tmp_path: Path) -> None:
    legacy = [
        {
            "task_id": "legacy-001",
            "description": "old buffer overflow example",
            "response": "patch",
            "has_code": True,
        }
    ]
    p = tmp_path / "legacy.json"
    p.write_text(json.dumps(legacy), encoding="utf-8")
    bank = FewshotBank.load(p)
    assert len(bank) == 1
    cases = list(bank)
    assert cases[0].id == "legacy-001"
    # Unknown key "has_code" should land in metadata.
    assert cases[0].metadata.get("has_code") is True


def test_save_load_roundtrip(tmp_path: Path) -> None:
    bank = FewshotBank(
        [
            _case("a", "alpha beta gamma", resp="resp-a", tags=("x",)),
            _case("b", "delta epsilon", resp="resp-b"),
        ]
    )
    p = tmp_path / "bank.json"
    bank.save(p)
    loaded = FewshotBank.load(p)
    assert len(loaded) == 2
    by_id = {c.id: c for c in loaded}
    assert by_id["a"].description == "alpha beta gamma"
    assert by_id["a"].response == "resp-a"
    assert by_id["a"].tags == ("x",)
    assert by_id["b"].tags == ()


def test_add_duplicate_id_raises() -> None:
    bank = FewshotBank([_case("dup", "first")])
    with pytest.raises(ValueError, match="duplicate id: dup"):
        bank.add(_case("dup", "second"))


def test_load_malformed_json_raises_valueerror(tmp_path: Path) -> None:
    p = tmp_path / "broken.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed bank file"):
        FewshotBank.load(p)


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FewshotBank.load(tmp_path / "does_not_exist.json")


def test_len_and_iter() -> None:
    cases = [_case("a", "x"), _case("b", "y"), _case("c", "z")]
    bank = FewshotBank(cases)
    assert len(bank) == 3
    ids = [c.id for c in bank]
    assert ids == ["a", "b", "c"]


def test_empty_query_returns_first_k_no_score() -> None:
    bank = FewshotBank(
        [
            _case("a", "alpha"),
            _case("b", "beta"),
            _case("c", "gamma"),
        ]
    )
    got = bank.retrieve("", k=2)
    assert [c.id for c in got] == ["a", "b"]
    # No scoring done → default score 0.0 untouched.
    assert all(c.score == 0.0 for c in got)


def test_score_attached_to_returned_cases() -> None:
    bank = FewshotBank([_case("c1", "alpha beta gamma")])
    got = bank.retrieve("alpha beta gamma", k=1)
    assert got[0].score == pytest.approx(1.0)


def test_add_after_load_persists_on_resave(tmp_path: Path) -> None:
    p = tmp_path / "bank.json"
    FewshotBank([_case("a", "alpha")]).save(p)
    bank = FewshotBank.load(p)
    bank.add(_case("b", "beta"))
    bank.save(p)
    reloaded = FewshotBank.load(p)
    assert {c.id for c in reloaded} == {"a", "b"}


def test_retrieve_filters_then_sorts_then_caps_k() -> None:
    bank = FewshotBank(
        [
            _case("a", "alpha beta gamma", tags=("keep",)),
            _case("b", "alpha beta gamma delta epsilon", tags=("drop",)),
            _case("c", "alpha beta", tags=("keep",)),
            _case("d", "alpha", tags=("keep",)),
        ]
    )
    got = bank.retrieve("alpha beta gamma", k=2, required_tags=("keep",))
    ids = [c.id for c in got]
    # "b" is dropped by tag filter despite being a strong match.
    assert "b" not in ids
    assert ids[0] == "a"  # highest overlap among kept
    assert len(got) == 2


def test_default_stop_words_includes_common_english() -> None:
    for w in ("the", "and", "or", "but", "is", "of", "to", "a", "an"):
        assert w in DEFAULT_STOP_WORDS


def test_functional_wrappers(tmp_path: Path) -> None:
    p = tmp_path / "bank.json"
    FewshotBank([_case("a", "alpha beta")]).save(p)
    bank = load_bank(p)
    got = retrieve_fewshot("alpha beta", bank, k=1)
    assert len(got) == 1 and got[0].id == "a"


def test_duplicate_in_constructor_raises() -> None:
    with pytest.raises(ValueError, match="duplicate id"):
        FewshotBank([_case("x", "a"), _case("x", "b")])
