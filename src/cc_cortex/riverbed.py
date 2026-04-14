"""cc_cortex.riverbed — Riverbed Memory Topology (RMT) Engine.

@module riverbed
@responsibility Memory as naturally carved riverbeds, not stored records.
    Experience = water flow. Memory = riverbed depth. Recall = stimulus-triggered
    resonance. Forgetting = natural silting. Identity = stake-anchored protection.
@dependencies cc_cortex.rag (optional, for vector projection backbone)
@exports RiverbedMemory, Riverbed, Stake, RecallResult

Original theory: 河床論 B≈ΔE/R + 意識張力論 R=T/M + 樁理論

Key properties (what makes this different from vector DB / graph RAG):
    - Experience-is-recording: no separate "memory write" step
    - Stimulus-triggered recall: passive resonance, not active search
    - Depth encodes importance: frequently verified = deeper = harder to forget
    - Emotional charge affects carving depth, not just retrieval ranking
    - Stake-anchored protection: identity memories resist decay
    - Natural silting: forgetting is reversible (silted ≠ deleted)
    - Confluence = emergent knowledge graph from co-activation
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

# ── Data Structures ──────────────────────────────────────


@dataclass
class Stake:
    """Cognitive anchor — core identity element that protects nearby riverbeds.

    Stakes are beliefs, traumas, missions, core values. Riverbeds near stakes
    decay extremely slowly (identity memories are near-eternal).
    """

    id: str
    label: str  # e.g. "I am a builder", "fear of abandonment"
    mass: float = 1.0  # Higher mass = larger protection radius
    position: list[float] = field(default_factory=list)  # Embedding vector
    created_at: float = 0.0

    def protection_radius(self) -> float:
        """Radius within which riverbeds get decay protection.

        Formula: base_radius × log(1 + mass).
        Mass 1.0 → radius 0.69. Mass 10.0 → radius 2.40.
        """
        return 0.5 * math.log(1.0 + self.mass)


@dataclass
class Confluence:
    """Connection between two riverbeds formed by co-activation.

    When two riverbeds are activated in the same experience, a confluence
    (junction) forms. Strength reflects co-occurrence frequency.
    """

    target_id: str
    strength: float = 0.0
    direction: float = 1.0  # +1 = forward, -1 = reverse, 0 = bidirectional


@dataclass
class Riverbed:
    """A single riverbed — the fundamental memory unit in RMT.

    Not "a stored memory" but "a path carved by experience".
    Depth = how many times and how intensely this path was traversed.
    """

    id: str
    path: str  # Semantic description / chunk text
    depth: float = 0.0
    emotional_charge: float = 0.0  # [-1, 1] valence × intensity
    last_flow: float = 0.0  # Timestamp of last activation
    created_at: float = 0.0
    flow_count: int = 0  # Total times this riverbed was traversed
    embedding: list[float] = field(default_factory=list)
    confluences: list[Confluence] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)  # file, heading, etc.

    # Derived from stake proximity — computed lazily
    _effective_decay_rate: float = -1.0

    def recall_priority(self, recency_bonus: float = 0.0) -> float:
        """Priority for recall ranking.

        Formula: depth × |emotional_charge| × (1 + recency_bonus)
        Deep + emotional + recent = surfaces first.
        """
        charge = max(abs(self.emotional_charge), 0.05)  # Floor at 0.05
        return self.depth * charge * (1.0 + recency_bonus)

    def add_confluence(self, target_id: str, strength: float = 1.0) -> None:
        """Add or strengthen a confluence to another riverbed."""
        for c in self.confluences:
            if c.target_id == target_id:
                c.strength += strength
                return
        self.confluences.append(Confluence(target_id=target_id, strength=strength))

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_effective_decay_rate", None)
        # Trim embedding for storage (store separately in vector DB)
        d["embedding"] = []  # Don't duplicate in JSON — ChromaDB has it
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Riverbed":
        confs = [Confluence(**c) for c in d.pop("confluences", [])]
        d.pop("_effective_decay_rate", None)
        return cls(**d, confluences=confs)


@dataclass
class RecallResult:
    """A single recalled memory with context."""

    riverbed_id: str
    text: str
    depth: float
    emotional_charge: float
    priority: float
    hops: int  # 0 = direct hit, 1+ = via confluence
    metadata: dict = field(default_factory=dict)


# ── Constants ────────────────────────────────────────────

DEFAULT_DECAY_LAMBDA = 0.001  # Per-hour decay rate (slow)
MIN_DEPTH = 0.001  # Never truly zero — silted but recoverable
MAX_DEPTH = 1000.0  # Cap to prevent runaway depth
CONFLUENCE_MIN_STRENGTH = 0.1  # Below this, confluence is pruned
RECENCY_HALF_LIFE_HOURS = 72  # 3 days for recency bonus halving
DEFAULT_ATTENTION_SPAN = 0.6  # Cosine similarity threshold for "nearby"
MAX_CONFLUENCE_HOPS = 3  # Maximum propagation depth


# ── Riverbed Memory Engine ───────────────────────────────


class RiverbedMemory:
    """Riverbed Memory Topology (RMT) — the core engine.

    Memory as naturally carved riverbeds. Experience = water flow that
    deepens paths. Recall = stimulus triggers resonance in deepest paths.

    Hybrid architecture:
    - Vector layer (ChromaDB via RAGIndex): stimulus projection / embedding
    - Riverbed layer (this module): depth, decay, emotion, confluence, stakes

    Usage::

        mem = RiverbedMemory(project_dir=".")
        mem.add_stake(Stake(id="builder", label="I am a builder", mass=5.0))

        # Experience carves riverbeds
        mem.experience("User corrected: always verify before commit",
                       emotional_charge=0.7)

        # Stimulus triggers recall (passive, not search)
        results = mem.recall("should I verify this change?")

        # Persistence
        mem.save()
    """

    def __init__(
        self,
        project_dir: str = "",
        cache_dir: str = "",
        rag_index=None,
    ):
        self.project_dir = project_dir or os.environ.get("CLAUDE_PROJECT_DIR", ".")
        self.cache_dir = cache_dir or os.path.join(
            self.project_dir, ".cc_cortex_cache", "riverbed"
        )
        self._rag = rag_index  # Optional RAGIndex for vector backbone
        self._riverbeds: dict[str, Riverbed] = {}
        self._stakes: list[Stake] = []
        self._topology_path = os.path.join(self.cache_dir, "topology.json")
        self._stakes_path = os.path.join(self.cache_dir, "stakes.json")
        self._dirty = False
        self._loaded = False

    # ── Stakes ───────────────────────────────────────────

    def add_stake(self, stake: Stake) -> None:
        """Plant a cognitive anchor (core identity / belief / mission)."""
        if not stake.created_at:
            stake.created_at = time.time()
        # Remove existing stake with same id
        self._stakes = [s for s in self._stakes if s.id != stake.id]
        self._stakes.append(stake)
        # Recompute decay rates for all riverbeds near this stake
        self._recompute_decay_rates()
        self._dirty = True

    def remove_stake(self, stake_id: str) -> bool:
        """Uproot a stake (growth / belief change). Returns True if found."""
        before = len(self._stakes)
        self._stakes = [s for s in self._stakes if s.id != stake_id]
        if len(self._stakes) < before:
            self._recompute_decay_rates()
            self._dirty = True
            return True
        return False

    def get_stakes(self) -> list[Stake]:
        return list(self._stakes)

    # ── Experience (Water Flow) ──────────────────────────

    def experience(
        self,
        text: str,
        emotional_charge: float = 0.0,
        metadata: dict | None = None,
        source_ids: list[str] | None = None,
    ) -> list[str]:
        """Record an experience — water flows and carves riverbeds.

        This is NOT "storing a memory". This is water flowing through cognitive
        terrain. The riverbed (memory) is a side effect of the flow.

        Args:
            text: The experience content (what happened).
            emotional_charge: Emotion intensity [-1, 1]. Higher = deeper carving.
                If CTEE is available, this comes from the tension field automatically.
            metadata: Optional metadata (file, heading, context).
            source_ids: If provided, only deepen these existing riverbeds
                instead of creating new ones. Used for re-activation / reinforcement.

        Returns:
            List of riverbed IDs that were carved/deepened.
        """
        self._ensure_loaded()
        now = time.time()
        charge = max(-1.0, min(1.0, emotional_charge))
        flow_intensity = max(abs(charge), 0.1)  # Minimum flow even for neutral

        carved_ids: list[str] = []

        if source_ids:
            # Reinforce existing riverbeds (recall = re-flow = reinforcement)
            for rid in source_ids:
                rb = self._riverbeds.get(rid)
                if rb:
                    self._flow(rb, flow_intensity, charge, now)
                    carved_ids.append(rid)
        else:
            # New experience — find or create riverbed
            existing = self._find_similar_riverbed(text)
            if existing and existing[1] > 0.85:
                # Similar enough → deepen existing (sub-linear scaling)
                rb = existing[0]
                self._flow(rb, flow_intensity, charge, now)
                carved_ids.append(rb.id)
            else:
                # New path — create riverbed
                rb_id = f"rb_{int(now * 1000) % 10**10}_{len(self._riverbeds)}"
                embedding = self._embed(text)
                rb = Riverbed(
                    id=rb_id,
                    path=text[:2000],  # Cap text length
                    depth=flow_intensity,
                    emotional_charge=charge,
                    last_flow=now,
                    created_at=now,
                    flow_count=1,
                    embedding=embedding,
                    metadata=metadata or {},
                )
                rb._effective_decay_rate = self._compute_decay_rate(rb)
                self._riverbeds[rb_id] = rb
                carved_ids.append(rb_id)

                # Also index in vector DB if available
                if self._rag:
                    try:
                        collection = self._rag._get_collection()
                        model = self._rag._get_model()
                        emb = model.encode([text], show_progress_bar=False).tolist()
                        collection.add(
                            ids=[rb_id],
                            embeddings=emb,
                            documents=[text[:2000]],
                            metadatas=[metadata or {}],
                        )
                    except Exception:
                        pass  # Vector layer is optional enhancement

        # Form confluences between co-activated riverbeds
        if len(carved_ids) > 1:
            self._form_confluences(carved_ids, flow_intensity)

        self._dirty = True
        return carved_ids

    def _flow(self, rb: Riverbed, intensity: float, charge: float, now: float) -> None:
        """Water flows through a riverbed — deepening it."""
        # Apply pending decay first
        self._apply_decay(rb, now)

        # Deepen (diminishing returns for very deep riverbeds)
        depth_gain = intensity / (1.0 + rb.depth * 0.01)
        rb.depth = min(rb.depth + depth_gain, MAX_DEPTH)

        # Blend emotional charge (weighted average, recent has more weight)
        alpha = 0.3  # New experience weight
        rb.emotional_charge = (1 - alpha) * rb.emotional_charge + alpha * charge

        rb.last_flow = now
        rb.flow_count += 1

    # ── Recall (Stimulus-Triggered Resonance) ────────────

    def recall(
        self,
        stimulus: str,
        top_k: int = 10,
        min_depth: float = 0.01,
        max_hops: int = MAX_CONFLUENCE_HOPS,
        attention_span: float = DEFAULT_ATTENTION_SPAN,
        current_emotion: float = 0.0,
    ) -> list[RecallResult]:
        """Stimulus triggers recall — water flows back through deepest riverbeds.

        This is NOT "searching". The stimulus lands on the cognitive terrain,
        and water naturally flows into the deepest nearby riverbeds.

        Args:
            stimulus: The triggering input (what's happening now).
            top_k: Maximum results to return.
            min_depth: Ignore riverbeds shallower than this.
            max_hops: Maximum confluence propagation depth.
            attention_span: Similarity threshold for "nearby" (0-1).
            current_emotion: Current emotional state — influences what is
                recalled (constructive memory, capability #13).

        Returns:
            List of RecallResult ordered by recall_priority (deepest first).
        """
        self._ensure_loaded()
        now = time.time()

        if not self._riverbeds:
            return []

        # Step 1: Project stimulus onto cognitive terrain
        nearby = self._find_nearby_riverbeds(stimulus, attention_span)

        # Step 2: Apply decay to all candidates
        for rb, _sim in nearby:
            self._apply_decay(rb, now)

        # Step 3: Filter by minimum depth
        nearby = [(rb, sim) for rb, sim in nearby if rb.depth >= min_depth]

        if not nearby:
            return []

        # Step 4: Propagate through confluences (multi-hop recall)
        activated: dict[str, tuple[Riverbed, int, float]] = {}  # id → (rb, hops, sim)
        for rb, sim in nearby:
            activated[rb.id] = (rb, 0, sim)

        if max_hops > 0:
            self._propagate_confluences(activated, max_hops)

        # Step 5: Rank by recall priority (depth × charge × recency)
        results: list[RecallResult] = []
        for rb_id, (rb, hops, _sim) in activated.items():
            dt_hours = (now - rb.last_flow) / 3600.0
            recency = math.exp(-0.693 * dt_hours / RECENCY_HALF_LIFE_HOURS)

            # Constructive memory: current emotion biases recall
            # Same-valence memories get a boost (you remember more sad things when sad)
            emotion_bias = 1.0
            if current_emotion != 0.0 and rb.emotional_charge != 0.0:
                alignment = current_emotion * rb.emotional_charge
                emotion_bias = 1.0 + 0.3 * max(0, alignment)  # Up to 30% boost

            priority = rb.recall_priority(recency) * emotion_bias

            results.append(RecallResult(
                riverbed_id=rb_id,
                text=rb.path,
                depth=round(rb.depth, 4),
                emotional_charge=round(rb.emotional_charge, 4),
                priority=round(priority, 4),
                hops=hops,
                metadata=rb.metadata,
            ))

        results.sort(key=lambda r: r.priority, reverse=True)

        # Step 6: Re-flow — recall itself reinforces the riverbed (light touch)
        for r in results[:top_k]:
            rb = self._riverbeds.get(r.riverbed_id)
            if rb:
                rb.last_flow = now
                rb.depth = min(rb.depth + 0.01, MAX_DEPTH)  # Tiny reinforcement

        self._dirty = True
        return results[:top_k]

    def _propagate_confluences(
        self,
        activated: dict[str, tuple[Riverbed, int, float]],
        max_hops: int,
    ) -> None:
        """Propagate activation through confluence connections (multi-hop)."""
        frontier = list(activated.keys())

        for hop in range(1, max_hops + 1):
            next_frontier = []
            for rb_id in frontier:
                rb = self._riverbeds.get(rb_id)
                if not rb:
                    continue
                for conf in rb.confluences:
                    if conf.target_id in activated:
                        continue  # Already activated
                    target = self._riverbeds.get(conf.target_id)
                    if not target:
                        continue
                    # Activation probability decreases with hop distance
                    if conf.strength > CONFLUENCE_MIN_STRENGTH:
                        activated[conf.target_id] = (target, hop, 0.0)
                        next_frontier.append(conf.target_id)
            frontier = next_frontier
            if not frontier:
                break

    # ── Decay (Natural Silting) ──────────────────────────

    def _apply_decay(self, rb: Riverbed, now: float) -> None:
        """Apply time-based exponential decay (natural silting).

        Riverbeds near stakes decay slower. Riverbeds far from stakes decay faster.
        Nothing truly reaches zero — silted but recoverable.
        """
        if rb.last_flow <= 0:
            return
        dt_hours = (now - rb.last_flow) / 3600.0
        if dt_hours <= 0:
            return

        decay_rate = rb._effective_decay_rate
        if decay_rate < 0:
            decay_rate = self._compute_decay_rate(rb)
            rb._effective_decay_rate = decay_rate

        rb.depth = max(MIN_DEPTH, rb.depth * math.exp(-decay_rate * dt_hours))

    def _compute_decay_rate(self, rb: Riverbed) -> float:
        """Compute effective decay rate based on stake proximity.

        Near stake → decay rate ≈ 0 (near-eternal).
        Far from all stakes → full default decay.
        """
        if not self._stakes or not rb.embedding:
            return DEFAULT_DECAY_LAMBDA

        min_distance = float("inf")
        for stake in self._stakes:
            if stake.position:
                dist = self._embedding_distance(rb.embedding, stake.position)
                if dist < min_distance:
                    min_distance = dist

        if min_distance == float("inf"):
            return DEFAULT_DECAY_LAMBDA

        # Find closest stake's protection radius
        def _stake_dist(s: Stake) -> float:
            if s.position:
                return self._embedding_distance(rb.embedding, s.position)
            return float("inf")

        closest_stake = min(self._stakes, key=_stake_dist)
        radius = closest_stake.protection_radius()

        # Inside radius → strongly protected. Outside → normal decay.
        # Formula: base_decay × (1 - exp(-distance / radius))
        protection = math.exp(-min_distance / max(radius, 0.01))
        return DEFAULT_DECAY_LAMBDA * (1.0 - 0.95 * protection)

    def _recompute_decay_rates(self) -> None:
        """Recompute all riverbeds' decay rates (after stake change)."""
        for rb in self._riverbeds.values():
            rb._effective_decay_rate = self._compute_decay_rate(rb)

    # ── Confluence Formation ─────────────────────────────

    def _form_confluences(self, riverbed_ids: list[str], strength: float) -> None:
        """Form confluences between co-activated riverbeds.

        When riverbeds are carved in the same experience, they connect.
        This is the natural knowledge graph — zero extra cost.
        """
        for i, id_a in enumerate(riverbed_ids):
            for id_b in riverbed_ids[i + 1:]:
                rb_a = self._riverbeds.get(id_a)
                rb_b = self._riverbeds.get(id_b)
                if rb_a and rb_b:
                    rb_a.add_confluence(id_b, strength)
                    rb_b.add_confluence(id_a, strength)

    # ── Embedding & Similarity ───────────────────────────

    def _embed(self, text: str) -> list[float]:
        """Get embedding vector for text. Uses RAG model if available."""
        if self._rag:
            try:
                model = self._rag._get_model()
                return model.encode([text], show_progress_bar=False).tolist()[0]
            except Exception:
                pass
        return []  # No vector backbone — pure topology mode

    def _embedding_distance(self, a: list[float], b: list[float]) -> float:
        """Cosine distance between two embedding vectors. 0=identical, 2=opposite."""
        if not a or not b or len(a) != len(b):
            return 2.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 2.0
        cosine_sim = dot / (norm_a * norm_b)
        return 1.0 - cosine_sim  # Convert similarity to distance

    def _find_similar_riverbed(
        self, text: str, threshold: float = 0.85
    ) -> Optional[tuple[Riverbed, float]]:
        """Find the most similar existing riverbed (for merging).

        Sub-linear scaling: 100 similar experiences = 1 deep riverbed.
        """
        embedding = self._embed(text)
        if not embedding:
            # No vector backbone — match by text prefix
            for rb in self._riverbeds.values():
                if rb.path[:100] == text[:100]:
                    return (rb, 1.0)
            return None

        best: Optional[tuple[Riverbed, float]] = None
        best_sim = 0.0

        for rb in self._riverbeds.values():
            if not rb.embedding:
                continue
            dist = self._embedding_distance(embedding, rb.embedding)
            sim = 1.0 - dist
            if sim > best_sim:
                best_sim = sim
                best = (rb, sim)

        if best and best_sim >= threshold:
            return best
        return None

    def _find_nearby_riverbeds(
        self,
        stimulus: str,
        attention_span: float = DEFAULT_ATTENTION_SPAN,
    ) -> list[tuple[Riverbed, float]]:
        """Project stimulus onto terrain, find nearby riverbeds.

        Uses vector similarity as the projection mechanism.
        attention_span = cosine similarity threshold for "nearby".
        """
        # Strategy 1: Use ChromaDB vector search if available (fastest)
        if self._rag:
            try:
                results = self._rag.search(
                    stimulus,
                    top_k=50,
                    min_score=attention_span,
                )
                nearby = []
                for r in results:
                    # Check if this chunk maps to a riverbed
                    rb = self._find_riverbed_by_metadata(r.get("file", ""), r.get("heading", ""))
                    if rb:
                        nearby.append((rb, r["score"]))
                if nearby:
                    return nearby
            except Exception:
                pass

        # Strategy 2: Direct embedding comparison (fallback)
        embedding = self._embed(stimulus)
        if not embedding:
            # No vector backbone — simple text match
            return self._text_match_fallback(stimulus)

        nearby = []
        for rb in self._riverbeds.values():
            if not rb.embedding:
                continue
            dist = self._embedding_distance(embedding, rb.embedding)
            sim = 1.0 - dist
            if sim >= attention_span:
                nearby.append((rb, sim))

        nearby.sort(key=lambda x: x[1], reverse=True)
        return nearby[:50]  # Cap candidates

    def _find_riverbed_by_metadata(self, file: str, heading: str) -> Optional[Riverbed]:
        """Find a riverbed by its source file and heading."""
        for rb in self._riverbeds.values():
            if rb.metadata.get("file") == file and rb.metadata.get("heading") == heading:
                return rb
        return None

    def _text_match_fallback(self, stimulus: str) -> list[tuple[Riverbed, float]]:
        """Fallback when no vector backbone: keyword matching."""
        words = set(stimulus.lower().split())
        if not words:
            return []
        results = []
        for rb in self._riverbeds.values():
            rb_words = set(rb.path.lower().split())
            overlap = len(words & rb_words)
            if overlap > 0:
                sim = overlap / max(len(words), len(rb_words))
                if sim > 0.1:
                    results.append((rb, sim))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:50]

    # ── Persistence ──────────────────────────────────────

    def _ensure_loaded(self) -> None:
        """Lazy load topology from disk."""
        if self._loaded:
            return
        self.load()
        self._loaded = True

    def load(self) -> bool:
        """Load riverbed topology and stakes from disk."""
        loaded = False

        if os.path.isfile(self._topology_path):
            try:
                with open(self._topology_path, encoding="utf-8") as f:
                    data = json.load(f)
                self._riverbeds = {
                    k: Riverbed.from_dict(v) for k, v in data.items()
                }
                loaded = True
            except Exception:
                self._riverbeds = {}

        if os.path.isfile(self._stakes_path):
            try:
                with open(self._stakes_path, encoding="utf-8") as f:
                    data = json.load(f)
                self._stakes = [Stake(**s) for s in data]
            except Exception:
                self._stakes = []

        self._recompute_decay_rates()
        self._loaded = True
        return loaded

    def save(self) -> dict:
        """Persist riverbed topology and stakes to disk.

        Returns:
            Stats: riverbeds_saved, stakes_saved.
        """
        os.makedirs(self.cache_dir, exist_ok=True)

        # Prune dead confluences before saving
        for rb in self._riverbeds.values():
            rb.confluences = [
                c for c in rb.confluences
                if c.strength >= CONFLUENCE_MIN_STRENGTH
                and c.target_id in self._riverbeds
            ]

        # Save topology
        topo = {k: v.to_dict() for k, v in self._riverbeds.items()}
        with open(self._topology_path, "w", encoding="utf-8") as f:
            json.dump(topo, f, indent=2, ensure_ascii=False)

        # Save stakes
        stakes = [asdict(s) for s in self._stakes]
        with open(self._stakes_path, "w", encoding="utf-8") as f:
            json.dump(stakes, f, indent=2, ensure_ascii=False)

        self._dirty = False
        return {
            "riverbeds_saved": len(self._riverbeds),
            "stakes_saved": len(self._stakes),
        }

    # ── Analytics ────────────────────────────────────────

    def stats(self) -> dict:
        """Get topology statistics."""
        self._ensure_loaded()
        now = time.time()

        if not self._riverbeds:
            return {
                "total_riverbeds": 0,
                "total_stakes": len(self._stakes),
                "total_confluences": 0,
            }

        depths = [rb.depth for rb in self._riverbeds.values()]
        charges = [abs(rb.emotional_charge) for rb in self._riverbeds.values()]
        total_confs = sum(len(rb.confluences) for rb in self._riverbeds.values())
        active = sum(1 for rb in self._riverbeds.values()
                     if (now - rb.last_flow) < 86400 * 7)  # Active in last week

        return {
            "total_riverbeds": len(self._riverbeds),
            "active_riverbeds_7d": active,
            "total_stakes": len(self._stakes),
            "total_confluences": total_confs // 2,  # Bidirectional
            "depth_max": round(max(depths), 4),
            "depth_avg": round(sum(depths) / len(depths), 4),
            "depth_median": round(sorted(depths)[len(depths) // 2], 4),
            "charge_avg": round(sum(charges) / len(charges), 4),
            "storage_mode": "hybrid" if self._rag else "topology_only",
        }

    def deepest(self, n: int = 10) -> list[dict]:
        """Return the N deepest riverbeds (strongest memories)."""
        self._ensure_loaded()
        rbs = sorted(self._riverbeds.values(), key=lambda r: r.depth, reverse=True)
        return [
            {
                "id": rb.id,
                "depth": round(rb.depth, 4),
                "charge": round(rb.emotional_charge, 4),
                "flow_count": rb.flow_count,
                "text": rb.path[:120],
                "confluences": len(rb.confluences),
            }
            for rb in rbs[:n]
        ]

    def global_decay(self) -> int:
        """Apply decay to ALL riverbeds (maintenance sweep).

        Returns number of riverbeds that silted below MIN_DEPTH threshold.
        """
        self._ensure_loaded()
        now = time.time()
        silted = 0
        for rb in self._riverbeds.values():
            old_depth = rb.depth
            self._apply_decay(rb, now)
            if rb.depth <= MIN_DEPTH < old_depth:
                silted += 1
        self._dirty = True
        return silted
