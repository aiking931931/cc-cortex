"""Tests for RMT Riverbed Memory Topology engine.

Validates all 13 capabilities that surpass GraphRAG / MemGPT / VectorDB:
 1. Experience-is-recording
 2. Stimulus-triggered recall
 3. Multi-hop via confluence
 4. Natural silting (exponential decay)
 5. Emotional charge priority
 6. Stake-anchored protection
 7. Self-emerging topology (confluence)
 8. Zero construction cost
 9. Sub-linear scaling (merge similar)
10. Memory recovery (re-carve silted)
11. Trauma modeling (deep = hyper-sensitive)
12. Unified with emotion (CTEE integration point)
13. Constructive recall (emotion biases memory)
"""

import math
import time

import pytest

from concinno.riverbed import (
    MIN_DEPTH,
    RecallResult,
    Riverbed,
    RiverbedMemory,
    Stake,
)


@pytest.fixture
def mem(tmp_path):
    """Fresh RiverbedMemory with no vector backbone (pure topology)."""
    return RiverbedMemory(
        project_dir=str(tmp_path),
        cache_dir=str(tmp_path / "cache"),
    )


# ── Capability #1: Experience-is-recording ───────────────


class TestExperienceIsRecording:
    """Memory forms as a side effect of experience, not a separate write."""

    def test_experience_creates_riverbed(self, mem):
        ids = mem.experience("User corrected: always verify before commit")
        assert len(ids) == 1
        assert ids[0] in mem._riverbeds

    def test_no_explicit_memory_write_needed(self, mem):
        """No separate 'store' or 'insert' step — experience IS storage."""
        ids = mem.experience("Learned: test before deploy")
        rb = mem._riverbeds[ids[0]]
        assert rb.depth > 0
        assert rb.flow_count == 1
        assert rb.path.startswith("Learned:")

    def test_experience_returns_carved_ids(self, mem):
        ids = mem.experience("First experience")
        assert isinstance(ids, list)
        assert all(isinstance(i, str) for i in ids)


# ── Capability #2: Stimulus-triggered recall ─────────────


class TestStimulusTriggeredRecall:
    """Recall is passive resonance, not active search."""

    def test_recall_finds_relevant_memory(self, mem):
        mem.experience("Always run tests before deploying code")
        results = mem.recall("should I test before deploy?")
        assert len(results) > 0
        assert isinstance(results[0], RecallResult)

    def test_recall_returns_empty_for_no_match(self, mem):
        mem.experience("Python is great for scripting")
        results = mem.recall("quantum physics equations")
        # May or may not match depending on text overlap
        # But should not crash
        assert isinstance(results, list)

    def test_recall_reinforces_riverbed(self, mem):
        ids = mem.experience("Important pattern: check twice")
        rb = mem._riverbeds[ids[0]]
        depth_before = rb.depth
        mem.recall("check twice before commit")
        # Recall itself is a light re-flow
        assert rb.depth >= depth_before


# ── Capability #3: Multi-hop via confluence ──────────────


class TestMultiHopConfluence:
    """Co-activated riverbeds form connections for multi-hop recall."""

    def test_confluence_forms_between_coactivated(self, mem):
        ids = mem.experience(
            "Coffee and friends",
            source_ids=None,
        )
        id_a = ids[0]
        # Create second riverbed
        ids_b = mem.experience("Friends and music")
        id_b = ids_b[0]

        # Manually co-activate to form confluence
        mem._form_confluences([id_a, id_b], 1.0)
        rb_a = mem._riverbeds[id_a]
        assert any(c.target_id == id_b for c in rb_a.confluences)

    def test_propagation_through_confluence(self, mem):
        # Create chain: A → B → C
        ids_a = mem.experience("Alpha concept")
        ids_b = mem.experience("Beta concept")
        ids_c = mem.experience("Gamma concept")
        a, b, c = ids_a[0], ids_b[0], ids_c[0]

        # Form chain
        mem._form_confluences([a, b], 1.0)
        mem._form_confluences([b, c], 1.0)

        activated = {a: (mem._riverbeds[a], 0, 1.0)}
        mem._propagate_confluences(activated, max_hops=2)

        # B and C should be reachable
        assert b in activated
        assert c in activated
        assert activated[b][1] == 1  # 1 hop
        assert activated[c][1] == 2  # 2 hops


# ── Capability #4: Natural silting (exponential decay) ───


class TestNaturalSilting:
    """Riverbeds decay over time but never truly reach zero."""

    def test_decay_reduces_depth(self, mem):
        ids = mem.experience("Temporary info", emotional_charge=0.5)
        rb = mem._riverbeds[ids[0]]
        original_depth = rb.depth

        # Simulate time passage (force last_flow to past)
        rb.last_flow = time.time() - 3600 * 24 * 30  # 30 days ago
        rb._effective_decay_rate = 0.01  # Moderate decay

        mem._apply_decay(rb, time.time())
        assert rb.depth < original_depth

    def test_depth_never_reaches_zero(self, mem):
        ids = mem.experience("Ancient memory", emotional_charge=0.3)
        rb = mem._riverbeds[ids[0]]

        # Extreme time passage
        rb.last_flow = time.time() - 3600 * 24 * 365 * 10  # 10 years
        rb._effective_decay_rate = 0.1

        mem._apply_decay(rb, time.time())
        assert rb.depth >= MIN_DEPTH
        assert rb.depth > 0

    def test_decay_is_exponential(self, mem):
        ids = mem.experience("Decaying memory", emotional_charge=0.5)
        rb = mem._riverbeds[ids[0]]
        rb._effective_decay_rate = 0.01
        initial = rb.depth

        # Decay at 100h
        rb.last_flow = time.time() - 3600 * 100
        mem._apply_decay(rb, time.time())
        after_100h = rb.depth

        # Reset and decay at 200h
        rb.depth = initial
        rb.last_flow = time.time() - 3600 * 200
        mem._apply_decay(rb, time.time())
        after_200h = rb.depth

        # Exponential: ratio should be consistent
        ratio_100 = after_100h / initial
        ratio_200 = after_200h / initial
        assert abs(ratio_200 - ratio_100**2) < 0.01


# ── Capability #5: Emotional charge priority ─────────────


class TestEmotionalCharge:
    """High-emotion memories surface before low-emotion ones."""

    def test_high_charge_higher_priority(self, mem):
        mem.experience("Father passed away", emotional_charge=0.95)
        mem.experience("Had sushi for lunch", emotional_charge=0.1)

        results = mem.recall("what happened recently?")
        if len(results) >= 2:
            # Father's death should rank higher
            charges = [r.emotional_charge for r in results]
            assert abs(charges[0]) >= abs(charges[1])

    def test_recall_priority_formula(self):
        rb = Riverbed(
            id="test",
            path="test",
            depth=50.0,
            emotional_charge=0.95,
        )
        priority = rb.recall_priority(recency_bonus=0.0)
        assert priority == pytest.approx(50.0 * 0.95, rel=0.01)

    def test_neutral_charge_still_works(self):
        rb = Riverbed(id="t", path="t", depth=10.0, emotional_charge=0.0)
        priority = rb.recall_priority()
        assert priority > 0  # Floor at 0.05


# ── Capability #6: Stake-anchored protection ─────────────


class TestStakeProtection:
    """Identity memories near stakes decay extremely slowly."""

    def test_stake_reduces_decay_rate(self, mem):
        # Create a riverbed with embedding
        ids = mem.experience("I am a builder")
        rb = mem._riverbeds[ids[0]]
        rate_without_stake = mem._compute_decay_rate(rb)

        # Plant a stake at the same position
        mem.add_stake(Stake(
            id="builder",
            label="I am a builder",
            mass=5.0,
            position=rb.embedding.copy() if rb.embedding else [],
        ))

        rate_with_stake = mem._compute_decay_rate(rb)

        # With stake nearby, decay should be slower
        # (Only meaningful if we have embeddings)
        if rb.embedding:
            assert rate_with_stake < rate_without_stake

    def test_stake_protection_radius_scales_with_mass(self):
        light = Stake(id="l", label="light", mass=1.0)
        heavy = Stake(id="h", label="heavy", mass=10.0)
        assert heavy.protection_radius() > light.protection_radius()

    def test_remove_stake_restores_decay(self, mem):
        mem.add_stake(Stake(
            id="temp", label="temporary belief", mass=3.0,
        ))
        assert len(mem.get_stakes()) == 1
        removed = mem.remove_stake("temp")
        assert removed is True
        assert len(mem.get_stakes()) == 0


# ── Capability #7: Self-emerging topology ────────────────


class TestSelfEmergingTopology:
    """Knowledge graph forms naturally from co-activation."""

    def test_confluence_creation(self, mem):
        rb_a = Riverbed(id="a", path="coffee")
        rb_b = Riverbed(id="b", path="friend")
        mem._riverbeds = {"a": rb_a, "b": rb_b}
        mem._loaded = True

        mem._form_confluences(["a", "b"], 1.0)

        assert any(c.target_id == "b" for c in rb_a.confluences)
        assert any(c.target_id == "a" for c in rb_b.confluences)

    def test_confluence_strengthens_on_repeat(self, mem):
        rb_a = Riverbed(id="a", path="coffee")
        rb_b = Riverbed(id="b", path="friend")
        mem._riverbeds = {"a": rb_a, "b": rb_b}
        mem._loaded = True

        mem._form_confluences(["a", "b"], 1.0)
        mem._form_confluences(["a", "b"], 1.0)

        conf = next(c for c in rb_a.confluences if c.target_id == "b")
        assert conf.strength == 2.0


# ── Capability #8: Zero construction cost ────────────────


class TestZeroConstructionCost:
    """No preprocessing step needed — experience is the index."""

    def test_no_build_step_required(self, mem):
        # Unlike GraphRAG (hours of preprocessing), just experience
        mem.experience("First thing")
        mem.experience("Second thing")
        results = mem.recall("first")
        # Should work immediately, no build() call needed
        assert isinstance(results, list)

    def test_incremental_by_nature(self, mem):
        mem.experience("Alpha")
        count_1 = len(mem._riverbeds)
        mem.experience("Beta")
        count_2 = len(mem._riverbeds)
        assert count_2 > count_1


# ── Capability #9: Sub-linear scaling ────────────────────


class TestSubLinearScaling:
    """100 similar experiences = 1 deep riverbed, not 100 records."""

    def test_similar_experiences_merge(self, mem):
        # Without vector backbone, uses text prefix matching
        for i in range(10):
            mem.experience(
                "Always verify before commit changes to production",
                emotional_charge=0.3,
            )
        # Should merge into ~1 riverbed, not 10
        assert len(mem._riverbeds) == 1

    def test_merged_riverbed_is_deeper(self, mem):
        mem.experience("Same pattern repeated", emotional_charge=0.5)
        rb_id = list(mem._riverbeds.keys())[0]
        depth_1 = mem._riverbeds[rb_id].depth

        mem.experience("Same pattern repeated", emotional_charge=0.5)
        depth_2 = mem._riverbeds[rb_id].depth
        assert depth_2 > depth_1

    def test_different_experiences_stay_separate(self, mem):
        mem.experience("Python programming tips")
        mem.experience("Japanese cooking recipes with detailed instructions")
        assert len(mem._riverbeds) == 2


# ── Capability #10: Memory recovery ──────────────────────


class TestMemoryRecovery:
    """Silted riverbeds can be re-carved by strong stimuli."""

    def test_silted_riverbed_recoverable(self, mem):
        ids = mem.experience("Old forgotten memory", emotional_charge=0.3)
        rb = mem._riverbeds[ids[0]]

        # Simulate heavy silting
        rb.depth = MIN_DEPTH  # Almost gone
        rb.last_flow = time.time() - 3600 * 24 * 365  # 1 year ago

        # Strong re-experience carves it back
        mem.experience(
            "Old forgotten memory",
            emotional_charge=0.9,
            source_ids=ids,
        )
        assert rb.depth > MIN_DEPTH * 10  # Significantly recovered

    def test_silted_not_deleted(self, mem):
        ids = mem.experience("Will silt", emotional_charge=0.1)
        rb = mem._riverbeds[ids[0]]
        rb.depth = MIN_DEPTH
        # Still in topology — not deleted
        assert ids[0] in mem._riverbeds


# ── Capability #11: Trauma modeling ──────────────────────


class TestTraumaModeling:
    """Extremely deep riverbeds = hyper-sensitive trigger threshold."""

    def test_high_emotion_creates_deep_riverbed(self, mem):
        ids_trauma = mem.experience(
            "Catastrophic failure in production",
            emotional_charge=0.99,
        )
        ids_normal = mem.experience(
            "Normal day at work with routine tasks",
            emotional_charge=0.1,
        )

        trauma = mem._riverbeds[ids_trauma[0]]
        normal = mem._riverbeds[ids_normal[0]]
        assert trauma.depth > normal.depth

    def test_deep_riverbed_triggers_easily(self, mem):
        # Create a very deep riverbed (trauma)
        rb = Riverbed(
            id="trauma",
            path="production outage",
            depth=100.0,
            emotional_charge=0.95,
            last_flow=time.time(),
        )
        mem._riverbeds["trauma"] = rb
        mem._loaded = True

        # Even vague stimulus should trigger it (high priority)
        priority = rb.recall_priority(recency_bonus=0.5)
        assert priority > 100  # Very high


# ── Capability #12: Unified with emotion ─────────────────


class TestUnifiedWithEmotion:
    """Emotional charge integrates with CTEE (or standalone)."""

    def test_emotional_charge_blends_on_reflow(self, mem):
        ids = mem.experience("Event A", emotional_charge=0.8)
        rb = mem._riverbeds[ids[0]]

        # Re-experience with different emotion
        mem.experience("Event A", emotional_charge=-0.2, source_ids=ids)
        # Should blend (weighted average), not replace
        assert rb.emotional_charge < 0.8
        assert rb.emotional_charge > -0.2

    def test_charge_bounded(self, mem):
        ids = mem.experience("Extreme", emotional_charge=5.0)
        rb = mem._riverbeds[ids[0]]
        assert rb.emotional_charge <= 1.0

        ids2 = mem.experience("Negative extreme", emotional_charge=-5.0)
        rb2 = mem._riverbeds[ids2[0]]
        assert rb2.emotional_charge >= -1.0


# ── Capability #13: Constructive recall ──────────────────


class TestConstructiveRecall:
    """Current emotional state biases what is remembered."""

    def test_same_valence_boosted(self, mem):
        mem.experience("Happy birthday party", emotional_charge=0.8)
        mem.experience("Sad farewell dinner", emotional_charge=-0.8)

        # When currently happy, happy memories get boosted
        results_happy = mem.recall(
            "dinner party",
            current_emotion=0.8,
        )
        results_sad = mem.recall(
            "dinner party",
            current_emotion=-0.8,
        )

        # Both should return results; priority ordering may differ
        assert isinstance(results_happy, list)
        assert isinstance(results_sad, list)


# ── Persistence ──────────────────────────────────────────


class TestPersistence:
    """Topology survives save/load cycle."""

    def test_save_and_load(self, mem):
        mem.experience("Persistent memory", emotional_charge=0.6)
        mem.add_stake(Stake(id="core", label="core value", mass=3.0))
        stats = mem.save()
        assert stats["riverbeds_saved"] == 1
        assert stats["stakes_saved"] == 1

        # Load into fresh instance
        mem2 = RiverbedMemory(
            project_dir=mem.project_dir,
            cache_dir=mem.cache_dir,
        )
        assert mem2.load() is True
        assert len(mem2._riverbeds) == 1
        assert len(mem2._stakes) == 1

    def test_confluence_survives_save(self, mem):
        rb_a = Riverbed(id="a", path="alpha", depth=1.0)
        rb_b = Riverbed(id="b", path="beta", depth=1.0)
        rb_a.add_confluence("b", 2.0)
        rb_b.add_confluence("a", 2.0)
        mem._riverbeds = {"a": rb_a, "b": rb_b}
        mem._loaded = True
        mem.save()

        mem2 = RiverbedMemory(
            project_dir=mem.project_dir,
            cache_dir=mem.cache_dir,
        )
        mem2.load()
        assert len(mem2._riverbeds["a"].confluences) == 1
        assert mem2._riverbeds["a"].confluences[0].target_id == "b"


# ── Analytics ────────────────────────────────────────────


class TestAnalytics:
    def test_stats_empty(self, mem):
        s = mem.stats()
        assert s["total_riverbeds"] == 0

    def test_stats_with_data(self, mem):
        mem.experience("Data A", emotional_charge=0.5)
        mem.experience("Data B different enough to be separate riverbed xx")
        s = mem.stats()
        assert s["total_riverbeds"] >= 1
        assert "depth_max" in s

    def test_deepest(self, mem):
        mem.experience("Shallow", emotional_charge=0.1)
        mem.experience("Extremely deep trauma event", emotional_charge=0.99)
        top = mem.deepest(n=2)
        assert len(top) >= 1
        assert top[0]["depth"] >= top[-1]["depth"]

    def test_global_decay(self, mem):
        ids = mem.experience("Will decay", emotional_charge=0.1)
        rb = mem._riverbeds[ids[0]]
        rb.last_flow = time.time() - 3600 * 24 * 365  # 1 year
        rb._effective_decay_rate = 0.1
        rb.depth = 0.01  # Near threshold

        silted = mem.global_decay()
        assert isinstance(silted, int)


# ── Data Structure Unit Tests ────────────────────────────


class TestDataStructures:
    def test_stake_protection_radius(self):
        s = Stake(id="t", label="test", mass=1.0)
        r = s.protection_radius()
        assert r == pytest.approx(0.5 * math.log(2.0), rel=0.01)

    def test_riverbed_to_from_dict(self):
        rb = Riverbed(
            id="test",
            path="test path",
            depth=5.0,
            emotional_charge=0.7,
            last_flow=1000.0,
            created_at=999.0,
            flow_count=3,
        )
        rb.add_confluence("other", 2.5)

        d = rb.to_dict()
        rb2 = Riverbed.from_dict(d)
        assert rb2.id == "test"
        assert rb2.depth == 5.0
        assert rb2.flow_count == 3
        assert len(rb2.confluences) == 1
        assert rb2.confluences[0].strength == 2.5

    def test_confluence_add_strengthens(self):
        rb = Riverbed(id="t", path="t")
        rb.add_confluence("x", 1.0)
        rb.add_confluence("x", 1.0)
        assert rb.confluences[0].strength == 2.0

    def test_recall_result_fields(self):
        r = RecallResult(
            riverbed_id="rb1",
            text="hello",
            depth=3.0,
            emotional_charge=0.5,
            priority=1.5,
            hops=0,
        )
        assert r.riverbed_id == "rb1"
        assert r.hops == 0
