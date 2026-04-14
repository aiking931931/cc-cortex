"""Tests for cc_cortex.memory_palace — 空間結構化記憶系統。"""

from __future__ import annotations

import pytest

from cc_cortex.core.state_store import StateStore
from cc_cortex.memory_palace import (
    Memory,
    MemoryPalace,
    Room,
    Tunnel,
    Wing,
    _keyword_score,
    _tokenize,
)


@pytest.fixture()
def store(tmp_path):
    """建立臨時 StateStore。"""
    return StateStore(str(tmp_path))


@pytest.fixture()
def palace(store):
    """建立空 MemoryPalace。"""
    return MemoryPalace(store)


# ── 資料結構序列化 ──────────────────────────────────────


class TestDataclassSerialization:
    def test_memory_roundtrip(self):
        m = Memory(
            id="abc123",
            content="ZIQ 壓縮率 3.2x",
            source_type="observation",
            importance=0.8,
            created_at=1000.0,
            tags=["ziq", "compression"],
        )
        d = m.to_dict()
        m2 = Memory.from_dict(d)
        assert m2.id == m.id
        assert m2.content == m.content
        assert m2.tags == ["ziq", "compression"]

    def test_room_roundtrip(self):
        r = Room(name="benchmark")
        r.memories.append(
            Memory(
                id="x1",
                content="NDCG@10 = 0.45",
                source_type="derived",
                importance=0.6,
                created_at=2000.0,
            )
        )
        d = r.to_dict()
        r2 = Room.from_dict(d)
        assert r2.name == "benchmark"
        assert len(r2.memories) == 1

    def test_wing_roundtrip(self):
        w = Wing(name="king")
        w.rooms["deploy"] = Room(name="deploy")
        d = w.to_dict()
        w2 = Wing.from_dict(d)
        assert w2.name == "king"
        assert "deploy" in w2.rooms

    def test_tunnel_roundtrip(self):
        t = Tunnel(
            id="t1",
            wing_a="king",
            room_a="deploy",
            wing_b="psyche",
            room_b="infra",
            created_at=3000.0,
        )
        d = t.to_dict()
        t2 = Tunnel.from_dict(d)
        assert t2.wing_a == "king"
        assert t2.room_b == "infra"


# ── 關鍵字搜索 ─────────────────────────────────────────


class TestKeywordSearch:
    def test_tokenize_english(self):
        tokens = _tokenize("The quick brown fox")
        assert "quick" in tokens
        assert "the" not in tokens  # 停用詞

    def test_tokenize_chinese(self):
        tokens = _tokenize("壓縮率很高的模型")
        assert "壓縮率很高" in tokens or len(tokens) > 0

    def test_keyword_score_exact_match(self):
        score = _keyword_score(["compression", "ziq"], "ZIQ compression algorithm")
        assert score > 0.5

    def test_keyword_score_no_match(self):
        score = _keyword_score(["quantum", "physics"], "ZIQ compression algorithm")
        assert score == 0.0

    def test_keyword_score_empty_query(self):
        score = _keyword_score([], "anything")
        assert score == 0.0


# ── add_memory ──────────────────────────────────────────


class TestAddMemory:
    def test_basic_add(self, palace):
        mem = palace.add_memory("king", "deploy", "部署到 VPS 成功")
        assert mem.content == "部署到 VPS 成功"
        assert mem.source_type == "observation"
        assert 0.0 <= mem.importance <= 1.0

    def test_add_creates_wing_and_room(self, palace):
        palace.add_memory("new_wing", "new_room", "test")
        assert "new_wing" in palace.list_wings()
        assert "new_room" in palace.list_rooms("new_wing")

    def test_importance_clamped(self, palace):
        m1 = palace.add_memory("w", "r", "too high", importance=2.0)
        m2 = palace.add_memory("w", "r", "too low", importance=-1.0)
        assert m1.importance == 1.0
        assert m2.importance == 0.0

    def test_add_with_tags(self, palace):
        mem = palace.add_memory("w", "r", "tagged", tags=["a", "b"])
        assert mem.tags == ["a", "b"]

    def test_memory_count_increments(self, palace):
        assert palace.get_memory_count() == 0
        palace.add_memory("w", "r", "first")
        palace.add_memory("w", "r", "second")
        assert palace.get_memory_count() == 2


# ── search ──────────────────────────────────────────────


class TestSearch:
    def test_search_finds_relevant(self, palace):
        palace.add_memory("king", "compression", "ZIQ 壓縮率 3.2x", importance=0.9)
        palace.add_memory("king", "deploy", "VPS 部署成功", importance=0.5)
        results = palace.search("ZIQ compression")
        assert len(results) >= 1
        assert results[0]["wing"] == "king"

    def test_search_wing_filter(self, palace):
        palace.add_memory("king", "deploy", "deploy ok")
        palace.add_memory("psyche", "deploy", "deploy ok")
        results = palace.search("deploy", wing="king")
        assert all(r["wing"] == "king" for r in results)

    def test_search_room_filter(self, palace):
        palace.add_memory("king", "deploy", "deploy successful")
        palace.add_memory("king", "benchmark", "deploy metrics")
        results = palace.search("deploy", wing="king", room="deploy")
        assert all(r["room"] == "deploy" for r in results)

    def test_search_top_k(self, palace):
        for i in range(10):
            palace.add_memory("w", "r", f"item {i} benchmark")
        results = palace.search("benchmark", top_k=3)
        assert len(results) == 3

    def test_search_empty_query(self, palace):
        palace.add_memory("w", "r", "something")
        results = palace.search("")
        assert results == []

    def test_search_nonexistent_wing(self, palace):
        results = palace.search("anything", wing="nonexistent")
        assert results == []

    def test_search_importance_weighting(self, palace):
        palace.add_memory("w", "r", "benchmark result high", importance=1.0)
        palace.add_memory("w", "r", "benchmark result low", importance=0.1)
        results = palace.search("benchmark result")
        assert len(results) == 2
        # 高重要度排前面
        assert results[0]["memory"].importance >= results[1]["memory"].importance


# ── get_summary ─────────────────────────────────────────


class TestGetSummary:
    def test_l0_empty(self, palace):
        s = palace.get_summary(depth="L0")
        assert "為空" in s

    def test_l0_with_data(self, palace):
        palace.add_memory("king", "deploy", "test")
        palace.add_memory("king", "benchmark", "test2")
        s = palace.get_summary(depth="L0")
        assert "king" in s
        assert "2 rooms" in s

    def test_l1_shows_important(self, palace):
        palace.add_memory("king", "deploy", "重要部署", importance=0.9)
        palace.add_memory("king", "deploy", "不重要", importance=0.3)
        s = palace.get_summary(depth="L1")
        assert "重要部署" in s
        assert "不重要" not in s

    def test_l2_full_content(self, palace):
        palace.add_memory("king", "deploy", "all content here", importance=0.1)
        s = palace.get_summary(wing="king", depth="L2")
        assert "all content here" in s

    def test_l2_nonexistent_wing(self, palace):
        s = palace.get_summary(wing="nope", depth="L2")
        assert "不存在" in s


# ── create_tunnel ───────────────────────────────────────


class TestCreateTunnel:
    def test_basic_tunnel(self, palace):
        palace.add_memory("king", "deploy", "test")
        palace.add_memory("psyche", "infra", "test")
        t = palace.create_tunnel("king", "deploy", "psyche", "infra")
        assert t.wing_a == "king"
        assert t.wing_b == "psyche"

    def test_duplicate_tunnel_returns_existing(self, palace):
        palace.add_memory("king", "deploy", "test")
        palace.add_memory("psyche", "infra", "test")
        t1 = palace.create_tunnel("king", "deploy", "psyche", "infra")
        t2 = palace.create_tunnel("king", "deploy", "psyche", "infra")
        assert t1.id == t2.id

    def test_reverse_duplicate_detected(self, palace):
        palace.add_memory("king", "deploy", "test")
        palace.add_memory("psyche", "infra", "test")
        t1 = palace.create_tunnel("king", "deploy", "psyche", "infra")
        t2 = palace.create_tunnel("psyche", "infra", "king", "deploy")
        assert t1.id == t2.id

    def test_tunnel_nonexistent_wing_raises(self, palace):
        palace.add_memory("king", "deploy", "test")
        with pytest.raises(ValueError, match="不存在"):
            palace.create_tunnel("king", "deploy", "nonexistent", "room")

    def test_tunnel_nonexistent_room_raises(self, palace):
        palace.add_memory("king", "deploy", "test")
        palace.add_memory("psyche", "infra", "test")
        with pytest.raises(ValueError, match="不存在"):
            palace.create_tunnel("king", "deploy", "psyche", "wrong_room")

    def test_get_tunnels_for(self, palace):
        palace.add_memory("king", "deploy", "test")
        palace.add_memory("psyche", "infra", "test")
        palace.create_tunnel("king", "deploy", "psyche", "infra")
        assert len(palace.get_tunnels_for("king")) == 1
        assert len(palace.get_tunnels_for("psyche", "infra")) == 1
        assert len(palace.get_tunnels_for("other")) == 0


# ── 持久化 ──────────────────────────────────────────────


class TestPersistence:
    def test_reload_preserves_data(self, store):
        p1 = MemoryPalace(store)
        p1.add_memory("king", "deploy", "persist me", importance=0.9)
        p1.add_memory("king", "benchmark", "also persist")
        p1.add_memory("psyche", "infra", "cross wing")
        p1.create_tunnel("king", "deploy", "psyche", "infra")

        # 建立新 instance（模擬重啟）
        p2 = MemoryPalace(store)
        assert p2.get_memory_count() == 3
        assert len(p2.list_tunnels()) == 1
        assert "king" in p2.list_wings()
        assert "psyche" in p2.list_wings()

    def test_delete_memory(self, palace):
        mem = palace.add_memory("w", "r", "to delete")
        assert palace.delete_memory("w", "r", mem.id) is True
        assert palace.get_memory_count() == 0

    def test_delete_nonexistent(self, palace):
        assert palace.delete_memory("w", "r", "nope") is False


# ── stats ───────────────────────────────────────────────


class TestStats:
    def test_empty_stats(self, palace):
        s = palace.stats
        assert s == {"wings": 0, "rooms": 0, "memories": 0, "tunnels": 0}

    def test_stats_after_operations(self, palace):
        palace.add_memory("king", "deploy", "test")
        palace.add_memory("king", "benchmark", "test2")
        palace.add_memory("psyche", "infra", "test3")
        palace.create_tunnel("king", "deploy", "psyche", "infra")
        s = palace.stats
        assert s["wings"] == 2
        assert s["rooms"] == 3
        assert s["memories"] == 3
        assert s["tunnels"] == 1
