"""Tests for cc_cortex.cbua_ux — CBUA UX display codes."""

from __future__ import annotations

from cc_cortex.cbua_ux import (
    CbuaCode,
    cbua_clean_write,
    cbua_format,
    cbua_label,
    cbua_redteam_verdict,
    cbua_three_layer,
    cbua_wiredo,
)

# ── CbuaCode Enum ─────────────────────────────────────────


class TestCbuaCode:
    def test_all_stages_present(self):
        values = [c.value for c in CbuaCode]
        # C stage
        assert "C0" in values
        assert "C3" in values
        # B stage
        assert "B0" in values
        assert "B1_根因" in values
        assert "B1_甜蜜點" in values
        assert "B1_策略" in values
        # U stage
        assert "U0_藍隊" in values
        assert "U1_紅隊" in values
        assert "U_判決" in values
        # A stage
        assert "A1" in values
        assert "A3.W" in values
        assert "A3.D" in values
        assert "A4_FTRL" in values
        # Laws
        assert "⛔L1_守恆" in values
        assert "⛔L6_誠實" in values

    def test_enum_count(self):
        # Should have a substantial number of codes
        assert len(CbuaCode) >= 50

    def test_wiredo_complete(self):
        wiredo = {"A3.W", "A3.I", "A3.R", "A3.E", "A3.D", "A3.O"}
        values = {c.value for c in CbuaCode}
        assert wiredo.issubset(values)

    def test_three_layer_complete(self):
        three = {"B1_根因", "B1_甜蜜點", "B1_策略"}
        values = {c.value for c in CbuaCode}
        assert three.issubset(values)

    def test_knowledge_depth_complete(self):
        depth = {"C2.索引", "C2.摘要", "C2.全文"}
        values = {c.value for c in CbuaCode}
        assert depth.issubset(values)

    def test_convention_sop_complete(self):
        conv = {"A3.命名", "A3.放置", "A3.複用"}
        values = {c.value for c in CbuaCode}
        assert conv.issubset(values)

    def test_clean_write_complete(self):
        cw = {"A4.清", "A4.寫"}
        values = {c.value for c in CbuaCode}
        assert cw.issubset(values)


# ── cbua_label ────────────────────────────────────────────


class TestCbuaLabel:
    def test_zh_tw(self):
        label = cbua_label(CbuaCode.B1_ROOT, locale="zh-TW")
        assert label == "B1 根因"

    def test_en(self):
        label = cbua_label(CbuaCode.B1_ROOT, locale="en")
        assert label == "B1 Root Cause"

    def test_wiredo_zh(self):
        assert cbua_label(CbuaCode.A3_W, "zh-TW") == "A3.W 接線"
        assert cbua_label(CbuaCode.A3_D, "zh-TW") == "A3.D 驗證"

    def test_wiredo_en(self):
        assert cbua_label(CbuaCode.A3_W, "en") == "A3.W Wired"
        assert cbua_label(CbuaCode.A3_D, "en") == "A3.D Defended"

    def test_law_label(self):
        assert "守恆" in cbua_label(CbuaCode.L1_CONSERVE, "zh-TW")
        assert "Conservation" in cbua_label(CbuaCode.L1_CONSERVE, "en")

    def test_fallback_to_value(self):
        # Unknown locale falls back to English
        label = cbua_label(CbuaCode.C0, locale="ja")
        assert label == "C0 Perceive"

    def test_all_codes_have_labels(self):
        for code in CbuaCode:
            # Both locales must have a label
            en = cbua_label(code, "en")
            zh = cbua_label(code, "zh-TW")
            assert en, f"Missing en label for {code}"
            assert zh, f"Missing zh-TW label for {code}"


# ── cbua_format ───────────────────────────────────────────


class TestCbuaFormat:
    def test_zh_format(self):
        result = cbua_format(CbuaCode.B1_ROOT, "PATH 衝突", locale="zh-TW")
        assert result == "B1 根因：PATH 衝突"

    def test_en_format(self):
        result = cbua_format(CbuaCode.B1_ROOT, "PATH conflict", locale="en")
        assert result == "B1 Root Cause: PATH conflict"

    def test_wiredo_format(self):
        result = cbua_format(CbuaCode.A3_D, "50/50 green ✅", locale="zh-TW")
        assert result == "A3.D 驗證：50/50 green ✅"

    def test_law_format(self):
        result = cbua_format(CbuaCode.L5_ENTROPY, "寫10清5", locale="zh-TW")
        assert "⛔L5 反熵" in result
        assert "寫10清5" in result

    def test_redteam_format(self):
        result = cbua_format(CbuaCode.U1_RED, "#1 學術 3F", locale="zh-TW")
        assert "U1 紅隊" in result
        assert "#1 學術 3F" in result

    def test_blue_team_format(self):
        result = cbua_format(CbuaCode.U0_BLUE, "自爆5弱點", locale="zh-TW")
        assert "U0 藍隊" in result

    def test_verdict_format(self):
        result = cbua_format(CbuaCode.U_VERDICT, "打中3", locale="zh-TW")
        assert "U 判決" in result

    def test_alpha_t_format(self):
        result = cbua_format(CbuaCode.C0_ALPHA, "0.45 → Complicated", locale="zh-TW")
        assert "C0 α_t" in result

    def test_knowledge_depth_format(self):
        result = cbua_format(CbuaCode.C2_INDEX, "42 條標題命中 3", locale="zh-TW")
        assert "C2.索引" in result

    def test_convention_format(self):
        result = cbua_format(CbuaCode.A3_NAME, "PAT-{seq} ✅", locale="zh-TW")
        assert "A3.命名" in result

    def test_clean_write_codes(self):
        c = cbua_format(CbuaCode.A4_CLEAN, "刪 8 行", locale="zh-TW")
        w = cbua_format(CbuaCode.A4_WRITE, "加 5 行", locale="zh-TW")
        assert "A4.清" in c
        assert "A4.寫" in w


# ── Helper Functions ──────────────────────────────────────


class TestCbuaWiredo:
    def test_full_wiredo(self):
        results = {
            "W": (True, "import ok"),
            "I": (True, "follows convention"),
            "R": (True, "responsive"),
            "E": (True, "configurable"),
            "D": (True, "50/50 green"),
            "O": (True, "has logging"),
        }
        output = cbua_wiredo(results, locale="zh-TW")
        assert "A3.W 接線" in output
        assert "A3.D 驗證" in output
        assert "✅" in output
        assert output.count("\n") == 5  # 6 lines

    def test_partial_wiredo(self):
        results = {
            "W": (True, "ok"),
            "D": (False, "no test"),
        }
        output = cbua_wiredo(results, locale="zh-TW")
        assert "✅" in output
        assert "❌" in output
        assert "A3.I" not in output  # Not included

    def test_empty_wiredo(self):
        assert cbua_wiredo({}, locale="zh-TW") == ""

    def test_wiredo_en(self):
        results = {"D": (True, "tests pass")}
        output = cbua_wiredo(results, locale="en")
        assert "A3.D Defended" in output


class TestCbuaThreeLayer:
    def test_three_layer(self):
        output = cbua_three_layer(
            root_cause="PATH 衝突",
            sweet_spot="python -m pip",
            strategy="用 -m 旗標",
            locale="zh-TW",
        )
        lines = output.split("\n")
        assert len(lines) == 3
        assert "B1 根因" in lines[0]
        assert "B1 甜蜜點" in lines[1]
        assert "B1 策略" in lines[2]

    def test_three_layer_en(self):
        output = cbua_three_layer("X", "Y", "Z", locale="en")
        assert "Root Cause" in output
        assert "Sweet Spot" in output
        assert "Strategy" in output


class TestCbuaRedteamVerdict:
    def test_verdict(self):
        output = cbua_redteam_verdict(3, 2, "採納修正", locale="zh-TW")
        assert "U 判決" in output
        assert "打中3" in output
        assert "打偏2" in output

    def test_verdict_en(self):
        output = cbua_redteam_verdict(1, 0, "all adopted", locale="en")
        assert "Verdict" in output


class TestCbuaCleanWrite:
    def test_net_negative(self):
        output = cbua_clean_write(8, 5, locale="zh-TW")
        assert "A4.清" in output
        assert "A4.寫" in output
        assert "淨-3" in output

    def test_net_positive(self):
        output = cbua_clean_write(3, 10, locale="zh-TW")
        assert "淨+7" in output

    def test_net_zero(self):
        output = cbua_clean_write(5, 5, locale="zh-TW")
        assert "淨0" in output  # net=0, sign="" (0 is not > 0)
