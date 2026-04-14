"""Tests for cc_cortex.prompt_guard — clarity gate + multi-question detection."""

from __future__ import annotations

import json
import os
import tempfile

from cc_cortex.prompt_guard import (
    MULTI_Q_MARKERS,
    _get_irreversible,
    _get_known_commands,
    _get_multi_topic,
    _get_vague,
    clarity_score,
    count_questions,
    is_irreversible,
    multi_question_injection,
    run_clarity_gate,
)

# ── count_questions ────────────────────────────────────


class TestCountQuestions:
    def test_no_questions(self):
        assert count_questions("修改 main.py 的函數") == 0

    def test_single_question_mark_zh(self):
        assert count_questions("這是什麼？") == 1

    def test_single_question_mark_en(self):
        assert count_questions("what is this?") == 1

    def test_multiple_question_marks(self):
        assert count_questions("為什麼？怎麼修？") == 2

    def test_topic_markers_zh(self):
        assert count_questions("修改 A 還有修改 B") >= 1

    def test_topic_markers_en(self):
        assert count_questions("fix A and also fix B") >= 1

    def test_multiple_markers(self):
        result = count_questions("先修 A？另外 B 怎麼辦？最後 C")
        assert result >= 3

    def test_multiline_blocks(self):
        prompt = (
            "這是第一個很長的段落要處理\n"
            "這是第二個很長的段落要處理\n"
            "這是第三個很長的段落要處理"
        )
        assert count_questions(prompt) >= 2

    def test_short_lines_ignored(self):
        prompt = "a\nb\nc"
        assert count_questions(prompt) == 0

    def test_empty(self):
        assert count_questions("") == 0

    def test_all_markers_combined(self):
        prompt = "問題一？還有問題二？另外問題三"
        assert count_questions(prompt) >= 3


# ── multi_question_injection ───────────────────────────


class TestMultiQuestionInjection:
    def test_returns_none_for_single_question(self):
        assert multi_question_injection("修改 main.py 的函數名稱") is None

    def test_returns_injection_for_multi(self):
        result = multi_question_injection("先修 A 的 bug？另外 B 也要改？")
        assert result is not None
        assert "questions detected" in result.lower() or "問題偵測" in result

    def test_skips_slash_commands(self):
        assert multi_question_injection("/mode engineering？還有？") is None

    def test_skips_short_prompts(self):
        assert multi_question_injection("改？修？") is None

    def test_includes_count(self):
        result = multi_question_injection("問題一？問題二？問題三？這個很長的提示確保超過二十字")
        assert result is not None
        assert "3" in result or "4" in result

    def test_checklist_format(self):
        result = multi_question_injection("先做 A 的事情要怎麼做？還有 B 也要處理怎麼辦？")
        assert result is not None
        assert "✅" in result
        assert "❌" in result


# ── clarity_score ──────────────────────────────────────


class TestClarityScore:
    def test_clear_prompt_high_score(self):
        score = clarity_score("修改 src/main.py 第 42 行的函數")
        assert score >= 0.7

    def test_very_short_low_score(self):
        score = clarity_score("改")
        assert score < 0.7

    def test_vague_references_lower_score(self):
        score_clear = clarity_score("修改 src/config.py 的設定值")
        score_vague = clarity_score("改那個之前的東西")
        assert score_vague < score_clear

    def test_known_command_bonus(self):
        score = clarity_score("交接 evolution")
        assert score >= 0.8

    def test_slash_command_bonus(self):
        score = clarity_score("/mode engineering")
        assert score >= 0.8

    def test_file_path_bonus(self):
        score = clarity_score("看一下 src/utils.ts 的問題")
        assert score >= 0.7

    def test_score_clamped_0_1(self):
        score = clarity_score("x")
        assert 0.0 <= score <= 1.0
        score2 = clarity_score(
            "/mode engineering 修改 src/main.py 很明確的指令"
        )
        assert 0.0 <= score2 <= 1.0

    def test_no_tech_identifiers_deduction(self):
        score = clarity_score("把那個改一下好了拜託")
        assert score < 0.7

    def test_with_preference_path(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({
                    "asked_too_much_patterns": ["部署"],
                    "should_have_asked_patterns": ["刪除全部"],
                }, f)
            # "asked too much" → raises score
            score_deploy = clarity_score("部署到正式環境", prefs_path=path)
            score_no_pref = clarity_score("部署到正式環境")
            assert score_deploy >= score_no_pref
        finally:
            os.unlink(path)

    def test_preference_should_ask(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({
                    "should_have_asked_patterns": ["全部"],
                }, f)
            score_with = clarity_score("改全部的檔案", prefs_path=path)
            score_without = clarity_score("改全部的檔案")
            assert score_with <= score_without
        finally:
            os.unlink(path)

    def test_preference_missing_file(self):
        score = clarity_score("test", prefs_path="/nonexistent.json")
        assert 0.0 <= score <= 1.0

    def test_preference_corrupt_file(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                f.write("not json")
            score = clarity_score("test", prefs_path=path)
            assert 0.0 <= score <= 1.0
        finally:
            os.unlink(path)


# ── is_irreversible ────────────────────────────────────


class TestIsIrreversible:
    def test_delete_zh(self):
        assert is_irreversible("刪除這個檔案")

    def test_deploy(self):
        assert is_irreversible("deploy to production")

    def test_rm_space(self):
        assert is_irreversible("rm -rf /tmp/test")

    def test_push(self):
        assert is_irreversible("push to main")

    def test_safe_prompt(self):
        assert not is_irreversible("讀取 src/main.py")

    def test_custom_keywords(self):
        assert is_irreversible("nuke it", keywords=("nuke",))
        assert not is_irreversible("nuke it")

    def test_case_insensitive(self):
        assert is_irreversible("DEPLOY now")
        assert is_irreversible("Reset --hard")


# ── run_clarity_gate ───────────────────────────────────


class TestRunClarityGate:
    def test_allows_clear_prompt(self):
        result = run_clarity_gate("修改 src/main.py 第 10 行")
        assert result is None

    def test_allows_slash_command(self):
        result = run_clarity_gate("/mode engineering")
        assert result is None

    def test_allows_long_prompt(self):
        result = run_clarity_gate("刪除 " + "x" * 600)
        assert result is None

    def test_blocks_ambiguous_irreversible(self):
        result = run_clarity_gate("刪除那個")
        assert result is not None
        assert result["hookSpecificOutput"]["decision"] == "block"
        reason = result["hookSpecificOutput"]["reason"].lower()
        assert "ambiguity" in reason or "歧義" in reason

    def test_allows_ambiguous_but_safe(self):
        # Ambiguous but not irreversible → allow
        result = run_clarity_gate("改那個")
        assert result is None

    def test_allows_irreversible_but_clear(self):
        result = run_clarity_gate("刪除 src/tmp/old_backup.py")
        assert result is None

    def test_with_prefs(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"asked_too_much_patterns": ["刪除"]}, f)
            # "asked too much" raises score → less likely to block
            result = run_clarity_gate("刪除那個", prefs_path=path)
            # May or may not block depending on final score
            assert result is None or "decision" in result.get(
                "hookSpecificOutput", {}
            )
        finally:
            os.unlink(path)

    def test_block_includes_hint(self):
        result = run_clarity_gate("刪除那個")
        assert result is not None
        reason = result["hookSpecificOutput"]["reason"].lower()
        assert "path" in reason or "路徑" in reason


# ── Constants ──────────────────────────────────────────


class TestConstants:
    def test_irreversible_has_entries(self):
        assert len(_get_irreversible()) >= 5

    def test_vague_has_entries(self):
        assert len(_get_vague()) >= 3

    def test_known_commands_has_entries(self):
        assert len(_get_known_commands()) >= 5

    def test_multi_q_markers(self):
        assert "？" in MULTI_Q_MARKERS
        assert "?" in MULTI_Q_MARKERS

    def test_multi_topic_markers_bilingual(self):
        topics = _get_multi_topic()
        has_zh = any(m for m in topics if ord(m[0]) > 127)
        has_en = any(m for m in topics if ord(m[0]) < 127)
        assert has_zh
        assert has_en
