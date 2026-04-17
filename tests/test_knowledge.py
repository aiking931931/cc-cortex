"""Tests for concinno.knowledge — correction detection, extraction, and learning lifecycle."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from concinno.knowledge import (
    check_conflicts,
    check_staleness,
    detect_skill_candidates,
    extract_corrections,
    get_pending_promotions,
    is_correction,
    log_corrections,
    mark_promoted,
    suggest_rule_promotions,
    update_learnings,
)

# ── is_correction ────────────────────────────────────────


class TestIsCorrection:
    # -- Chinese L1 (high confidence) --
    @pytest.mark.parametrize("text", [
        "不對，要改這裡",
        "你搞錯了",
        "錯了，應該是另一個",
        "改成 UTF-8",
        "不是這樣做的",
        "別用那個套件",
    ])
    def test_cn_l1_patterns(self, text):
        assert is_correction(text) is True

    @pytest.mark.parametrize("text", [
        "不對，要改這裡",
        "錯了，重來",
    ])
    def test_cn_l1_confidence_is_1(self, text):
        matched, conf = is_correction(text, return_confidence=True)
        assert matched is True
        assert conf == 1.0

    # -- Chinese L2 (medium confidence) --
    @pytest.mark.parametrize("text", [
        "把那個名稱改掉",
        "漏了一個欄位",
        "不需要這個功能",
        "加上 type hints",
        "太複雜了吧",
        "怎麼又壞了",
        "記住這個規則",
    ])
    def test_cn_l2_patterns(self, text):
        assert is_correction(text) is True

    def test_cn_l2_confidence_is_06(self):
        matched, conf = is_correction("漏了一個欄位", return_confidence=True)
        assert matched is True
        assert conf == 0.6

    # -- English L1 --
    @pytest.mark.parametrize("text", [
        "That's wrong, fix it",
        "No, not that one",
        "Actually, use the other API",
        "Use X instead",
        "Should be lowercase",
        "Don't use eval",
        "That's not correct",
        "Please change the order",
    ])
    def test_en_l1_patterns(self, text):
        assert is_correction(text) is True

    def test_en_l1_confidence(self):
        matched, conf = is_correction("That's wrong", return_confidence=True)
        assert matched is True
        assert conf == 1.0

    # -- English L2 --
    @pytest.mark.parametrize("text", [
        "Change the name to foo",
        "Remove that import",
        "Missing a semicolon",
        "Why did you do that",
        "Still broken after fix",
        "Undo the last change",
        "Remember to use async",
    ])
    def test_en_l2_patterns(self, text):
        assert is_correction(text) is True

    def test_en_l2_confidence(self):
        matched, conf = is_correction("Remove that import", return_confidence=True)
        assert matched is True
        assert conf == 0.6

    # -- Japanese --
    @pytest.mark.parametrize("text", [
        "違うよそれは",
        "間違ってるよ",
        "ダメだそれは",
        "変えてください",
        "修正してほしい",
    ])
    def test_ja_patterns(self, text):
        assert is_correction(text) is True

    # -- Korean --
    @pytest.mark.parametrize("text", [
        "아니 그거 아니야",
        "틀렸어 다시 해",
        "잘못된 코드야",
        "고쳐줘 빨리",
        "수정해 주세요",
    ])
    def test_ko_patterns(self, text):
        assert is_correction(text) is True

    # -- Spanish --
    @pytest.mark.parametrize("text", [
        "no es correcto",
        "está mal hecho",
        "cambia el nombre",
        "incorrecto, arregla eso",
    ])
    def test_es_patterns(self, text):
        assert is_correction(text) is True

    # -- Length boundaries --
    def test_too_short_text(self):
        assert is_correction("no") is False  # len < 4

    def test_exactly_4_chars(self):
        # "錯了啊" is 3 CJK chars = 3 len; "錯了啊吧" = 4 len
        assert is_correction("錯了啊吧") is True

    def test_too_long_text(self):
        long_text = "錯了" + "x" * 500
        assert is_correction(long_text) is False

    def test_at_max_length(self):
        # 500 chars total with a pattern inside
        text = "錯了" + "x" * 498
        assert len(text) == 500
        assert is_correction(text) is True

    def test_over_max_length(self):
        text = "錯了" + "x" * 499
        assert len(text) == 501
        assert is_correction(text) is False

    # -- No match --
    def test_normal_message_no_match(self):
        assert is_correction("幫我寫一個 Python 腳本") is False

    def test_return_confidence_false(self):
        matched, conf = is_correction("今天天氣不錯", return_confidence=True)
        assert matched is False
        assert conf == 0.0

    # -- SKIP_PREFIXES do NOT apply in is_correction --
    def test_skip_prefix_not_applied(self):
        # is_correction only checks patterns, skip prefixes are for extract_corrections
        assert is_correction("交接 進化 錯了") is True


# ── extract_corrections ──────────────────────────────────


def _make_transcript(entries: list[dict]) -> str:
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)


class TestExtractCorrections:
    def test_valid_transcript(self, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        lines = [
            {"type": "assistant", "message": {"content": "I'll use eval()"}},
            {"type": "user", "message": {"content": "不要用 eval"}},
        ]
        transcript.write_text(_make_transcript(lines), encoding="utf-8")

        result = extract_corrections(str(transcript))
        assert len(result) == 1
        assert "eval" in result[0]["assistant_before"]
        assert "不要用" in result[0]["user_correction"]
        assert result[0]["confidence"] == 1.0

    def test_empty_file(self, tmp_path):
        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("", encoding="utf-8")

        result = extract_corrections(str(transcript))
        assert result == []

    def test_nonexistent_file(self):
        result = extract_corrections("/nonexistent/path.jsonl")
        assert result == []

    def test_too_large_file(self, tmp_path):
        transcript = tmp_path / "big.jsonl"
        # max_file_size=100 to make test fast
        transcript.write_text("x" * 200, encoding="utf-8")

        result = extract_corrections(str(transcript), max_file_size=100)
        assert result == []

    def test_max_corrections_limit(self, tmp_path):
        transcript = tmp_path / "many.jsonl"
        entries = []
        for i in range(20):
            entries.append({"type": "assistant", "message": {"content": f"Response {i}"}})
            entries.append({"type": "user", "message": {"content": f"錯了 fix {i}"}})
        transcript.write_text(_make_transcript(entries), encoding="utf-8")

        result = extract_corrections(str(transcript), max_corrections=3)
        assert len(result) == 3

    def test_skip_prefixes(self, tmp_path):
        transcript = tmp_path / "skip.jsonl"
        entries = [
            {"type": "assistant", "message": {"content": "Some response"}},
            {"type": "user", "message": {"content": "交接 進化 錯了"}},
            {"type": "assistant", "message": {"content": "Another response"}},
            {"type": "user", "message": {"content": "<system 錯了"}},
            {"type": "assistant", "message": {"content": "Third response"}},
            {"type": "user", "message": {"content": "<ide_action 錯了"}},
        ]
        transcript.write_text(_make_transcript(entries), encoding="utf-8")

        result = extract_corrections(str(transcript))
        assert len(result) == 0

    def test_no_preceding_assistant(self, tmp_path):
        """User message without a preceding assistant message should not be extracted."""
        transcript = tmp_path / "no_assist.jsonl"
        entries = [
            {"type": "user", "message": {"content": "錯了"}},
        ]
        transcript.write_text(_make_transcript(entries), encoding="utf-8")

        result = extract_corrections(str(transcript))
        assert result == []

    def test_content_as_list(self, tmp_path):
        """Content can be a list of content blocks."""
        transcript = tmp_path / "list_content.jsonl"
        ast_content = [{"type": "text", "text": "I used eval"}]
        entries = [
            {"type": "assistant", "message": {"content": ast_content}},
            {"type": "user", "message": {"content": "不要用 eval"}},
        ]
        transcript.write_text(_make_transcript(entries), encoding="utf-8")

        result = extract_corrections(str(transcript))
        assert len(result) == 1

    def test_with_timezone(self, tmp_path):
        transcript = tmp_path / "tz.jsonl"
        entries = [
            {"type": "assistant", "message": {"content": "Response"}},
            {"type": "user", "message": {"content": "錯了，重來一次"}},
        ]
        transcript.write_text(_make_transcript(entries), encoding="utf-8")

        tz = timezone(timedelta(hours=8))
        result = extract_corrections(str(transcript), tz=tz)
        assert len(result) == 1
        assert "+08:00" in result[0]["timestamp"]


# ── log_corrections ──────────────────────────────────────


class TestLogCorrections:
    def test_writes_jsonl(self, tmp_path):
        log_path = str(tmp_path / "corrections.jsonl")
        corrections = [
            {"user_correction": "錯了", "timestamp": "2026-01-01T00:00:00"},
            {"user_correction": "不對", "timestamp": "2026-01-01T00:01:00"},
        ]

        log_corrections(corrections, session_id="abcdef1234567890", log_path=log_path)

        with open(log_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        assert len(lines) == 2
        assert lines[0]["session_id"] == "abcdef12"  # truncated to 8
        assert lines[1]["user_correction"] == "不對"

    def test_creates_parent_dir(self, tmp_path):
        log_path = str(tmp_path / "deep" / "nested" / "corrections.jsonl")

        log_corrections(
            [{"user_correction": "test", "timestamp": "2026-01-01"}],
            session_id="sess1234",
            log_path=log_path,
        )

        assert os.path.isfile(log_path)

    def test_appends_to_existing(self, tmp_path):
        log_path = str(tmp_path / "corrections.jsonl")
        log_corrections(
            [{"user_correction": "first", "timestamp": "t1"}],
            session_id="aaaa1111",
            log_path=log_path,
        )
        log_corrections(
            [{"user_correction": "second", "timestamp": "t2"}],
            session_id="bbbb2222",
            log_path=log_path,
        )

        with open(log_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        assert len(lines) == 2


# ── update_learnings ─────────────────────────────────────


class TestUpdateLearnings:
    def _make_correction(self, text="錯了要改", ts="2026-01-01T00:00:00"):
        return {
            "user_correction": text,
            "assistant_before": "I did something wrong",
            "confidence": 1.0,
            "timestamp": ts,
        }

    def test_new_learning(self, tmp_path):
        lpath = str(tmp_path / "learnings.json")
        corrections = [self._make_correction()]

        update_learnings(corrections, lpath)

        with open(lpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data["learnings"]
        assert len(items) == 1
        assert items[0]["count"] == 1
        assert items[0]["promoted"] is False

    def test_dedup_increment(self, tmp_path):
        lpath = str(tmp_path / "learnings.json")
        c1 = self._make_correction(ts="2026-01-01T00:00:00")
        c2 = self._make_correction(ts="2026-01-02T00:00:00")

        update_learnings([c1], lpath)
        update_learnings([c2], lpath)

        with open(lpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data["learnings"]
        assert len(items) == 1
        assert items[0]["count"] == 2
        assert items[0]["last_seen"] == "2026-01-02T00:00:00"

    def test_max_stored_trim(self, tmp_path):
        lpath = str(tmp_path / "learnings.json")
        corrections = [
            self._make_correction(
                text=f"unique correction number {i}",
                ts=f"2026-01-{i+1:02d}T00:00:00",
            )
            for i in range(10)
        ]

        update_learnings(corrections, lpath, max_stored=5)

        with open(lpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["learnings"]) == 5

    def test_different_corrections_not_deduped(self, tmp_path):
        lpath = str(tmp_path / "learnings.json")
        c1 = self._make_correction(text="改用 async")
        c2 = self._make_correction(text="不要用 eval")

        update_learnings([c1, c2], lpath)

        with open(lpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["learnings"]) == 2


# ── get_pending_promotions ───────────────────────────────


class TestGetPendingPromotions:
    def _write_learnings(self, tmp_path, items):
        lpath = str(tmp_path / "learnings.json")
        data = {"version": "1.0", "learnings": items}
        with open(lpath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return lpath

    def test_threshold_filtering(self, tmp_path):
        items = [
            {"id": "a1", "count": 5, "promoted": False, "correction_text": "fix A"},
            {"id": "a2", "count": 2, "promoted": False, "correction_text": "fix B"},
            {"id": "a3", "count": 3, "promoted": False, "correction_text": "fix C"},
        ]
        lpath = self._write_learnings(tmp_path, items)

        result = get_pending_promotions(lpath, threshold=3)
        ids = [r["id"] for r in result]
        assert "a1" in ids
        assert "a3" in ids
        assert "a2" not in ids

    def test_promoted_exclusion(self, tmp_path):
        items = [
            {"id": "a1", "count": 10, "promoted": True, "correction_text": "fix A"},
            {"id": "a2", "count": 5, "promoted": False, "correction_text": "fix B"},
        ]
        lpath = self._write_learnings(tmp_path, items)

        result = get_pending_promotions(lpath, threshold=3)
        assert len(result) == 1
        assert result[0]["id"] == "a2"

    def test_empty_file(self, tmp_path):
        lpath = str(tmp_path / "learnings.json")
        result = get_pending_promotions(lpath, threshold=3)
        assert result == []


# ── suggest_rule_promotions ──────────────────────────────


class TestSuggestRulePromotions:
    def _write_learnings(self, tmp_path, items):
        lpath = str(tmp_path / "learnings.json")
        data = {"version": "1.0", "learnings": items}
        with open(lpath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return lpath

    def test_generates_suggestions_with_slug(self, tmp_path):
        items = [{
            "id": "abc1", "count": 5, "promoted": False,
            "correction_text": "Always use async await",
            "confidence": 0.8, "context": "some context",
        }]
        lpath = self._write_learnings(tmp_path, items)

        result = suggest_rule_promotions(lpath, threshold=3)
        assert len(result) == 1
        s = result[0]
        assert s["learning_id"] == "abc1"
        assert s["target_file"].startswith("learned-")
        assert s["target_file"].endswith(".md")
        assert s["count"] == 5

    def test_sorted_by_count_desc(self, tmp_path):
        items = [
            {"id": "lo", "count": 3, "promoted": False,
             "correction_text": "Low freq", "confidence": 1.0},
            {"id": "hi", "count": 10, "promoted": False,
             "correction_text": "High freq", "confidence": 1.0},
        ]
        lpath = self._write_learnings(tmp_path, items)

        result = suggest_rule_promotions(lpath, threshold=3)
        assert result[0]["learning_id"] == "hi"
        assert result[1]["learning_id"] == "lo"

    def test_no_pending_returns_empty(self, tmp_path):
        items = [
            {"id": "a", "count": 1, "promoted": False, "correction_text": "x"},
        ]
        lpath = self._write_learnings(tmp_path, items)

        result = suggest_rule_promotions(lpath, threshold=3)
        assert result == []


# ── mark_promoted ────────────────────────────────────────


class TestMarkPromoted:
    def _write_learnings(self, tmp_path, items):
        lpath = str(tmp_path / "learnings.json")
        data = {"version": "1.0", "learnings": items}
        with open(lpath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return lpath

    def test_marks_and_returns_true(self, tmp_path):
        items = [
            {"id": "abc1", "count": 5, "promoted": False, "correction_text": "fix"},
        ]
        lpath = self._write_learnings(tmp_path, items)

        assert mark_promoted(lpath, "abc1") is True

        with open(lpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["learnings"][0]["promoted"] is True
        assert "promoted_at" in data["learnings"][0]

    def test_missing_id_returns_false(self, tmp_path):
        items = [
            {"id": "abc1", "count": 5, "promoted": False, "correction_text": "fix"},
        ]
        lpath = self._write_learnings(tmp_path, items)

        assert mark_promoted(lpath, "nonexistent") is False

    def test_empty_learnings_returns_false(self, tmp_path):
        lpath = str(tmp_path / "learnings.json")
        assert mark_promoted(lpath, "anything") is False


# ── check_staleness ──────────────────────────────────────


class TestCheckStaleness:
    def _write_learnings(self, tmp_path, items):
        lpath = str(tmp_path / "learnings.json")
        data = {"version": "1.0", "learnings": items}
        with open(lpath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return lpath

    def test_marks_stale_items(self, tmp_path):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        old_date = (now - timedelta(days=100)).isoformat()
        items = [
            {"id": "old1", "last_seen": old_date, "correction_text": "old fix"},
        ]
        lpath = self._write_learnings(tmp_path, items)

        result = check_staleness(lpath, stale_days=90, now=now)
        assert len(result) == 1
        assert result[0]["id"] == "old1"
        assert result[0]["stale"] is True

    def test_respects_stale_days(self, tmp_path):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        recent_date = (now - timedelta(days=30)).isoformat()
        items = [
            {"id": "recent", "last_seen": recent_date, "correction_text": "recent fix"},
        ]
        lpath = self._write_learnings(tmp_path, items)

        result = check_staleness(lpath, stale_days=90, now=now)
        assert len(result) == 0

    def test_already_stale_skipped(self, tmp_path):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        old_date = (now - timedelta(days=200)).isoformat()
        items = [
            {"id": "already", "last_seen": old_date, "stale": True, "correction_text": "old"},
        ]
        lpath = self._write_learnings(tmp_path, items)

        result = check_staleness(lpath, stale_days=90, now=now)
        assert len(result) == 0

    def test_empty_learnings(self, tmp_path):
        lpath = self._write_learnings(tmp_path, [])
        result = check_staleness(lpath, stale_days=90)
        assert result == []


# ── check_conflicts ──────────────────────────────────────


class TestCheckConflicts:
    def _write_learnings(self, tmp_path, items):
        lpath = str(tmp_path / "learnings.json")
        data = {"version": "1.0", "learnings": items}
        with open(lpath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return lpath

    def test_detects_conflicts(self, tmp_path):
        same_ctx = "When deploying the application"
        items = [
            {"id": "a", "context": same_ctx, "correction_text": "Use docker compose"},
            {"id": "b", "context": same_ctx, "correction_text": "Use kubernetes instead"},
        ]
        lpath = self._write_learnings(tmp_path, items)

        result = check_conflicts(lpath, similarity_chars=30)
        assert len(result) == 1
        assert result[0][0]["id"] == "a"
        assert result[0][1]["id"] == "b"

    def test_no_conflicts_different_contexts(self, tmp_path):
        items = [
            {"id": "a", "context": "Context about deployment", "correction_text": "Use docker"},
            {"id": "b", "context": "Context about testing", "correction_text": "Use pytest"},
        ]
        lpath = self._write_learnings(tmp_path, items)

        result = check_conflicts(lpath, similarity_chars=30)
        assert len(result) == 0

    def test_no_conflicts_same_correction(self, tmp_path):
        same_ctx = "When configuring the database"
        items = [
            {"id": "a", "context": same_ctx, "correction_text": "Use PostgreSQL"},
            {"id": "b", "context": same_ctx, "correction_text": "Use PostgreSQL"},
        ]
        lpath = self._write_learnings(tmp_path, items)

        result = check_conflicts(lpath, similarity_chars=30)
        assert len(result) == 0

    def test_single_item_no_conflict(self, tmp_path):
        items = [
            {"id": "a", "context": "ctx", "correction_text": "fix"},
        ]
        lpath = self._write_learnings(tmp_path, items)

        result = check_conflicts(lpath, similarity_chars=30)
        assert len(result) == 0

    def test_empty_context_skipped(self, tmp_path):
        items = [
            {"id": "a", "context": "", "correction_text": "fix A"},
            {"id": "b", "context": "", "correction_text": "fix B"},
        ]
        lpath = self._write_learnings(tmp_path, items)

        result = check_conflicts(lpath, similarity_chars=30)
        assert len(result) == 0


# ── detect_skill_candidates ─────────────────────────────


class TestDetectSkillCandidates:
    def _write_learnings(self, tmp_path, items):
        lpath = str(tmp_path / "learnings.json")
        data = {"version": "1.0", "learnings": items}
        with open(lpath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return lpath

    def _write_drive_state(self, tmp_path, state=None):
        dpath = str(tmp_path / "drive-state.json")
        with open(dpath, "w", encoding="utf-8") as f:
            json.dump(state or {}, f, ensure_ascii=False)
        return dpath

    def test_detects_high_count_pattern(self, tmp_path):
        items = [
            {"id": "a1", "count": 2, "pattern_key": "toast-notification",
             "correction_text": "彈窗消失太快", "promoted": False},
            {"id": "a2", "count": 2, "pattern_key": "toast-notification",
             "correction_text": "通知沒彈出來", "promoted": False},
        ]
        lpath = self._write_learnings(tmp_path, items)
        dpath = self._write_drive_state(tmp_path)

        result = detect_skill_candidates(lpath, dpath, pattern_threshold=3)
        assert len(result) == 1
        assert result[0]["pattern_key"] == "toast-notification"
        assert result[0]["total_count"] == 4
        assert result[0]["status"] == "detected"

        with open(dpath, "r", encoding="utf-8") as f:
            ds = json.load(f)
        assert len(ds["learning_tracker"]["skill_candidates"]) == 1

    def test_below_threshold_not_added(self, tmp_path):
        items = [
            {"id": "a1", "count": 1, "pattern_key": "book-content",
             "correction_text": "章節順序錯", "promoted": False},
        ]
        lpath = self._write_learnings(tmp_path, items)
        dpath = self._write_drive_state(tmp_path)

        result = detect_skill_candidates(lpath, dpath, pattern_threshold=3)
        assert len(result) == 0

    def test_uncategorized_skipped(self, tmp_path):
        items = [
            {"id": "a1", "count": 10, "pattern_key": "uncategorized",
             "correction_text": "some random fix", "promoted": False},
        ]
        lpath = self._write_learnings(tmp_path, items)
        dpath = self._write_drive_state(tmp_path)

        result = detect_skill_candidates(lpath, dpath, pattern_threshold=3)
        assert len(result) == 0

    def test_existing_candidate_not_duplicated(self, tmp_path):
        items = [
            {"id": "a1", "count": 5, "pattern_key": "cc-ccc-boundary",
             "correction_text": "邊界違規", "promoted": False},
        ]
        lpath = self._write_learnings(tmp_path, items)
        dpath = self._write_drive_state(tmp_path, {
            "learning_tracker": {
                "skill_candidates": [
                    {"pattern_key": "cc-ccc-boundary", "status": "detected"}
                ]
            }
        })

        result = detect_skill_candidates(lpath, dpath, pattern_threshold=3)
        assert len(result) == 0

        with open(dpath, "r", encoding="utf-8") as f:
            ds = json.load(f)
        assert len(ds["learning_tracker"]["skill_candidates"]) == 1

    def test_multiple_patterns_detected(self, tmp_path):
        items = [
            {"id": "a1", "count": 3, "pattern_key": "token-efficiency",
             "correction_text": "token 估算太高", "promoted": False},
            {"id": "a2", "count": 4, "pattern_key": "process-guard",
             "correction_text": "殭屍沒殺乾淨", "promoted": False},
            {"id": "a3", "count": 1, "pattern_key": "ux-branding",
             "correction_text": "命名不對", "promoted": False},
        ]
        lpath = self._write_learnings(tmp_path, items)
        dpath = self._write_drive_state(tmp_path)

        result = detect_skill_candidates(lpath, dpath, pattern_threshold=3)
        patterns = {r["pattern_key"] for r in result}
        assert "token-efficiency" in patterns
        assert "process-guard" in patterns
        assert "ux-branding" not in patterns

    def test_empty_learnings(self, tmp_path):
        lpath = self._write_learnings(tmp_path, [])
        dpath = self._write_drive_state(tmp_path)

        result = detect_skill_candidates(lpath, dpath, pattern_threshold=3)
        assert result == []

    def test_examples_limited_to_3(self, tmp_path):
        items = [
            {"id": f"a{i}", "count": 1, "pattern_key": "image-generation",
             "correction_text": f"圖片問題 {i}", "promoted": False}
            for i in range(5)
        ]
        lpath = self._write_learnings(tmp_path, items)
        dpath = self._write_drive_state(tmp_path)

        result = detect_skill_candidates(lpath, dpath, pattern_threshold=3)
        assert len(result) == 1
        assert len(result[0]["examples"]) == 3
