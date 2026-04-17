"""Tests for concinno.token_zone — Three-zone token management."""

from __future__ import annotations

import json
import time

from concinno.token_zone import (
    HANDOFF_TOOLS,
    MODEL_PROFILES,
    Zone,
    detect_model,
    detect_zone,
    detect_zone_abs,
    format_ux,
    read_zone_file,
    should_gate_tool,
    write_zone_file,
    zone_injection,
)

# ── Legacy Zone Detection (percentage-based) ──────────────


class TestDetectZone:
    def test_green(self):
        assert detect_zone(0) == Zone.GREEN
        assert detect_zone(59.9) == Zone.GREEN

    def test_yellow(self):
        assert detect_zone(60) == Zone.YELLOW
        assert detect_zone(84.9) == Zone.YELLOW

    def test_red(self):
        assert detect_zone(85) == Zone.RED
        assert detect_zone(100) == Zone.RED


# ── Absolute Zone Detection ───────────────────────────────


class TestDetectZoneAbs:
    def test_green_under_c1(self):
        profile = MODEL_PROFILES["opus"]
        assert detect_zone_abs(100_000, profile) == Zone.GREEN
        assert detect_zone_abs(199_999, profile) == Zone.GREEN

    def test_yellow_c1_c2(self):
        profile = MODEL_PROFILES["opus"]
        # Opus: C1=200K, C3=600K
        assert detect_zone_abs(200_000, profile) == Zone.YELLOW
        assert detect_zone_abs(400_000, profile) == Zone.YELLOW
        assert detect_zone_abs(599_999, profile) == Zone.YELLOW

    def test_orange_c3_c4(self):
        profile = MODEL_PROFILES["opus"]
        # Opus: C3=600K, C5=900K
        assert detect_zone_abs(600_000, profile) == Zone.ORANGE
        assert detect_zone_abs(800_000, profile) == Zone.ORANGE
        assert detect_zone_abs(899_999, profile) == Zone.ORANGE

    def test_red_at_c5(self):
        profile = MODEL_PROFILES["opus"]
        assert detect_zone_abs(900_000, profile) == Zone.RED
        assert detect_zone_abs(999_000, profile) == Zone.RED

    def test_haiku_thresholds(self):
        # Haiku: C1=80K, C3=150K, C5=185K
        profile = MODEL_PROFILES["haiku"]
        assert detect_zone_abs(50_000, profile) == Zone.GREEN
        assert detect_zone_abs(80_000, profile) == Zone.YELLOW
        assert detect_zone_abs(150_000, profile) == Zone.ORANGE
        assert detect_zone_abs(185_000, profile) == Zone.RED

    def test_sonnet_matches_opus(self):
        opus = MODEL_PROFILES["opus"]
        sonnet = MODEL_PROFILES["sonnet"]
        for t in (100_000, 300_000, 700_000, 950_000):
            assert detect_zone_abs(t, opus) == detect_zone_abs(t, sonnet)


# ── UX Format ─────────────────────────────────────────────


class TestFormatUx:
    def test_green_format(self):
        profile = MODEL_PROFILES["opus"]
        result = format_ux(87_000, profile)
        assert "[Opus]" in result
        assert "🟢" in result
        assert "87/800K" in result

    def test_yellow_format(self):
        profile = MODEL_PROFILES["sonnet"]
        result = format_ux(300_000, profile)
        assert "[Sonnet]" in result
        assert "🟡" in result

    def test_orange_format(self):
        profile = MODEL_PROFILES["sonnet"]
        result = format_ux(850_000, profile)
        assert "[Sonnet]" in result
        assert "🟠" in result
        assert "子代理" in result

    def test_red_with_handoff(self):
        profile = MODEL_PROFILES["opus"]
        result = format_ux(960_000, profile, compact_count=3)
        assert "[Opus]" in result
        assert "🔴" in result
        assert "960/800K" in result
        assert "[C3]" in result
        assert "交接" in result

    def test_compact_count_shown(self):
        profile = MODEL_PROFILES["opus"]
        result = format_ux(300_000, profile, compact_count=1)
        assert "[C1]" in result

    def test_no_compact_count_when_zero(self):
        profile = MODEL_PROFILES["opus"]
        result = format_ux(100_000, profile, compact_count=0)
        assert "[C" not in result

    def test_haiku_format(self):
        profile = MODEL_PROFILES["haiku"]
        result = format_ux(52_000, profile)
        assert "[Haiku]" in result
        assert "52/200K" in result


# ── Model Detection ───────────────────────────────────────


class TestDetectModel:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MODEL", "sonnet")
        result = detect_model()
        assert result["display"] == "Sonnet"

    def test_default_is_opus(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/nonexistent")
        result = detect_model()
        assert result["display"] == "Opus"

    def test_haiku_detection(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
        result = detect_model()
        assert result["display"] == "Haiku"
        assert result["context_limit"] == 200_000

    def test_mythos_detection(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-mythos-1")
        result = detect_model()
        assert result["display"] == "Mythos"
        assert result["context_limit"] == 2_000_000
        assert result["quality_zone"] == 400_000
        assert result["force_handoff"] == 1_600_000

    def test_capybara_alias(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MODEL", "capybara-preview")
        result = detect_model()
        assert result["display"] == "Mythos"

    def test_mythos_in_profiles(self):
        assert "mythos" in MODEL_PROFILES
        p = MODEL_PROFILES["mythos"]
        assert p["quality_zone"] == 400_000


# ── Zone Injection ─────────────────────────────────────────


class TestZoneInjection:
    def test_green_returns_none(self):
        assert zone_injection(30) is None

    def test_yellow_has_content(self):
        text = zone_injection(65)
        assert text is not None
        assert "65%" in text

    def test_red_has_content(self):
        text = zone_injection(90)
        assert text is not None

    def test_absolute_tokens_injection(self):
        # Opus 1M: quality_zone=800K, so 850K triggers yellow injection
        text = zone_injection(0, input_tokens=850_000)
        assert text is not None
        assert "800K" in text

    def test_absolute_green_none(self):
        # 100K is green under Opus 1M (C1=200K)
        text = zone_injection(0, input_tokens=100_000)
        assert text is None

    def test_persona_mode_zero_injection(self):
        for pct in (30, 65, 85, 99):
            result = zone_injection(pct, persona_mode=True)
            assert result is None, (
                f"persona_mode should return None at {pct}%"
            )


# ── Tool Gating ────────────────────────────────────────────


class TestShouldGateTool:
    def test_green_passes_all(self):
        assert should_gate_tool("Agent", Zone.GREEN) is None
        assert should_gate_tool("Bash", Zone.GREEN) is None

    def test_yellow_allows_agent(self):
        # Agent is encouraged in yellow zone (delegate to subagents)
        assert should_gate_tool("Agent", Zone.YELLOW) is None

    def test_yellow_passes_other_tools(self):
        assert should_gate_tool("Read", Zone.YELLOW) is None
        assert should_gate_tool("Bash", Zone.YELLOW) is None

    def test_red_blocks_non_handoff(self):
        assert should_gate_tool("Bash", Zone.RED) is not None
        # Agent is allowed in red zone (main agent delegates via Agent)
        assert should_gate_tool("Agent", Zone.RED) is None

    def test_red_passes_handoff_tools(self):
        for tool in HANDOFF_TOOLS:
            assert should_gate_tool(tool, Zone.RED) is None

    def test_persona_mode_skips_all_gates(self):
        assert should_gate_tool("Agent", Zone.YELLOW, persona_mode=True) is None
        assert should_gate_tool("Bash", Zone.RED, persona_mode=True) is None

    def test_full_mode_skips_all_gates(self):
        assert should_gate_tool("Agent", Zone.YELLOW, handoff_mode="full") is None
        assert should_gate_tool("Bash", Zone.RED, handoff_mode="full") is None
        assert should_gate_tool("Agent", Zone.RED, handoff_mode="full") is None


# ── Zone File I/O ──────────────────────────────────────────


class TestZoneFileIO:
    def test_write_and_read(self, tmp_path, monkeypatch):
        zone_file = str(tmp_path / ".token_zone.json")
        monkeypatch.setattr("concinno.token_zone.ZONE_FILE", zone_file)

        write_zone_file(72.5, 145_000)
        data = read_zone_file(max_age_s=10)

        assert data is not None
        assert data["zone"] == "green"  # 145K < 200K quality zone
        assert data["pct"] == 72.5
        assert data["input_tokens"] == 145_000
        assert data["model_display"] is not None

    def test_write_yellow_zone(self, tmp_path, monkeypatch):
        zone_file = str(tmp_path / ".token_zone.json")
        monkeypatch.setattr("concinno.token_zone.ZONE_FILE", zone_file)

        # Opus 1M: 850K > C3(600K) → orange
        write_zone_file(50.0, 850_000)
        data = read_zone_file(max_age_s=10)

        assert data is not None
        assert data["zone"] == "orange"

    def test_compact_count_persisted(self, tmp_path, monkeypatch):
        zone_file = str(tmp_path / ".token_zone.json")
        monkeypatch.setattr("concinno.token_zone.ZONE_FILE", zone_file)

        write_zone_file(50.0, 100_000, compact_count=2)
        data = read_zone_file(max_age_s=10)
        assert data["compact_count"] == 2

    def test_stale_file_returns_none(self, tmp_path, monkeypatch):
        zone_file = str(tmp_path / ".token_zone.json")
        monkeypatch.setattr("concinno.token_zone.ZONE_FILE", zone_file)

        with open(zone_file, "w") as f:
            json.dump({"zone": "red", "pct": 90, "ts": int(time.time()) - 300}, f)

        data = read_zone_file(max_age_s=120)
        assert data is None

    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "concinno.token_zone.ZONE_FILE",
            str(tmp_path / "nonexistent.json"),
        )
        assert read_zone_file() is None


# ── Integration ────────────────────────────────────────────


class TestIntegration:
    def test_full_flow(self, tmp_path, monkeypatch):
        """Status line → zone file → hook read → gate check."""
        zone_file = str(tmp_path / ".token_zone.json")
        monkeypatch.setattr("concinno.token_zone.ZONE_FILE", zone_file)

        # Opus 1M: 850K = orange (past C3 600K)
        write_zone_file(50.0, 850_000)

        # Hook reads zone
        data = read_zone_file()
        assert data is not None
        assert data["zone"] == "orange"

        # Gate checks
        zone = Zone(data["zone_level"])
        assert should_gate_tool("Agent", zone) is None  # allowed (delegate)
        assert should_gate_tool("Write", zone) is None  # allowed

        # Injection text
        text = zone_injection(data["pct"], input_tokens=850_000)
        assert text is not None
