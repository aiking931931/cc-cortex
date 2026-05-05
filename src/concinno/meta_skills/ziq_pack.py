"""concinno.meta_skills.ziq_pack — Top-k tool selection via ZIQ posterior.

@module meta_skills.ziq_pack
@responsibility Given a pool of 10+ tools and a natural-language query,
    return the top-k most relevant tools. Combines a structural prior
    (character-level TF-IDF over tool name + description — SPS slot)
    with FTRL-style online outcome learning (per-tool success rate +
    latency EMA, persisted under ``~/.concinno/ziq_tool_stats.json``).
@dependencies concinno.tool_executor.Tool (hard), stdlib only.
    Concinno's real ZIQ engine
    (``concinno.ziq_retrieval.ZIQRetrieval`` + ``concinno.ziq_router``)
    is tuned for knowledge-source routing, not tool selection — we
    do NOT try to shoehorn that API here. Instead we implement the
    same SPS × FTRL shape with a minimal in-module kernel.
    # TODO: hook real ZIQ engine once tool-selection-domain SPS lands.
@exports ZIQRoutedSkillPack

Formula
-------
::

    posterior(t | query) ∝ softmax(
        alpha * SPS(query, t)
      + beta  * FTRL_success(t)
      - gamma * FTRL_latency(t)
    )

- ``SPS`` — character-level TF-IDF cosine between the query and the
  tool's ``name + description`` text. Char n-grams beat word tokens for
  short queries and cross-language (繁體中文 + English docstrings)
  matching. Built entirely from stdlib ``collections.Counter``.
- ``FTRL_success`` — EMA of per-call success (half-life ~100 calls).
- ``FTRL_latency`` — EMA of per-call latency in seconds, z-scored
  across the current pool at selection time so ``gamma`` has a
  consistent scale.

State persistence
-----------------
Stats live at ``~/.concinno/ziq_tool_stats.json``:

::

    {
        "version": 1,
        "updated": <unix_ts>,
        "tools": {
            "<tool_name>": {
                "n": <int>,
                "success_ema": <float 0..1>,
                "latency_ema_ms": <float>,
                "last_seen": <unix_ts>
            }
        }
    }

EMA half-life 100 uses ``alpha_ema = 1 - 0.5 ** (1/100) ≈ 0.00693``.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..tool_executor import Tool

logger = logging.getLogger("concinno.meta_skills.ziq_pack")

# ── State location ───────────────────────────────────────────────────
_STATE_DIR = Path.home() / ".concinno"
_STATE_FILE = "ziq_tool_stats.json"

# ── EMA tuning ────────────────────────────────────────────────────────
# Half-life of 100 calls keeps the FTRL responsive on short workflows
# while not thrashing on one-off failures. See formula in module header.
_EMA_HALF_LIFE = 100
_EMA_ALPHA = 1.0 - 0.5 ** (1.0 / _EMA_HALF_LIFE)  # ≈ 0.00693


def _state_path() -> Path:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _STATE_DIR / _STATE_FILE


# ── SPS: char-level TF-IDF ───────────────────────────────────────────

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")


def _char_ngrams(text: str, n: int = 3) -> list[str]:
    """Return overlapping char n-grams + whole ASCII/CJK tokens.

    Mixing n-grams with whole tokens gives both fuzzy (partial-match)
    and exact-match signal. Lowercasing first so case doesn't split the
    vocabulary.
    """
    text = text.lower()
    tokens = _TOKEN_RE.findall(text)
    grams: list[str] = list(tokens)
    padded = f"  {text}  "
    for i in range(len(padded) - n + 1):
        g = padded[i : i + n].strip()
        if g:
            grams.append(g)
    return grams


@dataclass(frozen=True)
class _ToolVec:
    """Precomputed per-tool TF + doc metadata, reused across queries."""

    name: str
    tf: Counter[str]
    norm: float  # L2 of TF-IDF vector (filled in lazily; see _sps_score)


# ── FTRL state ───────────────────────────────────────────────────────


def _load_stats(path: Path | None = None) -> dict[str, Any]:
    """Load the stats blob. Missing/corrupt file → fresh dict.

    Corrupt-file recovery is important for a library — a single bad
    write must not brick subsequent sessions.
    """
    p = path or _state_path()
    if not p.exists():
        return {"version": 1, "updated": 0.0, "tools": {}}
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("ziq_pack: stats load failed (%s); resetting", exc)
        return {"version": 1, "updated": 0.0, "tools": {}}
    if not isinstance(data, dict) or "tools" not in data:
        return {"version": 1, "updated": 0.0, "tools": {}}
    return data


def _save_stats(data: dict[str, Any], path: Path | None = None) -> None:
    p = path or _state_path()
    data["updated"] = time.time()
    try:
        with p.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError as exc:  # pragma: no cover - disk-full edge
        logger.debug("ziq_pack: stats save failed %s", exc)


# ── Pack ─────────────────────────────────────────────────────────────


class ZIQRoutedSkillPack:
    """Select top-k tools from a pool via SPS × FTRL posterior.

    Construction cost is O(N) where N = ``len(tools)`` (TF build once).
    :meth:`select_top_k` is O(N * V) where V = distinct grams in query;
    fine for the 10-500 tool regime this class is designed for. Past
    500 tools migrate to a real embedding index (concinno.ziq_retrieval
    or FAISS) — see module TODO.
    """

    def __init__(
        self,
        tools: list[Tool],
        *,
        k: int = 3,
        alpha: float = 1.0,
        beta: float = 0.5,
        gamma: float = 0.1,
        stats_path: Path | None = None,
    ) -> None:
        if k < 1:
            msg = "k must be >= 1"
            raise ValueError(msg)
        if not tools:
            msg = "ZIQRoutedSkillPack requires at least one tool"
            raise ValueError(msg)
        self._tools_by_name: dict[str, Tool] = {t.name: t for t in tools}
        if len(self._tools_by_name) != len(tools):
            msg = "duplicate tool names in pool"
            raise ValueError(msg)
        self.k = k
        self.alpha = alpha
        self.beta = beta
        self.gamma = gamma
        self._stats_path = stats_path

        # Precompute TF per tool. Done once at init.
        self._vecs: dict[str, _ToolVec] = {}
        self._df: Counter[str] = Counter()
        for t in tools:
            text = f"{t.name} {getattr(t, 'description', '')}"
            tf = Counter(_char_ngrams(text))
            self._vecs[t.name] = _ToolVec(name=t.name, tf=tf, norm=0.0)
            self._df.update(tf.keys())
        self._n_docs = len(tools)
        # Pre-compute per-tool IDF-weighted L2 norms for cosine denom.
        self._tool_norms: dict[str, float] = {}
        for name, vec in self._vecs.items():
            self._tool_norms[name] = self._l2_idf(vec.tf)

    # ── Public API ───────────────────────────────────────────────

    def select_top_k(self, query: str) -> list[Tool]:
        """Return the top-k tools for ``query`` by posterior.

        Empty query → top-k by FTRL alone (still deterministic; ties
        broken by insertion order).
        """
        scores = self._score_all(query)
        ranked = sorted(
            scores.items(),
            key=lambda x: (-x[1], list(self._tools_by_name.keys()).index(x[0])),
        )
        names = [n for n, _ in ranked[: self.k]]
        return [self._tools_by_name[n] for n in names]

    def update_outcome(
        self,
        tool_name: str,
        *,
        success: bool,
        latency_ms: float,
    ) -> None:
        """FTRL feedback update for one tool call.

        No-ops silently when ``tool_name`` isn't in the pool — callers
        may be draining old outcomes from a previous config.
        """
        if tool_name not in self._tools_by_name:
            logger.debug("ziq_pack: outcome for unknown tool %r; dropped", tool_name)
            return
        stats = _load_stats(self._stats_path)
        tools_dict = stats.setdefault("tools", {})
        entry = tools_dict.setdefault(
            tool_name,
            {"n": 0, "success_ema": 0.5, "latency_ema_ms": float(latency_ms), "last_seen": 0.0},
        )
        n = int(entry.get("n", 0))
        success_val = 1.0 if success else 0.0
        if n == 0:
            entry["success_ema"] = success_val
            entry["latency_ema_ms"] = float(latency_ms)
        else:
            entry["success_ema"] = (
                (1.0 - _EMA_ALPHA) * float(entry["success_ema"])
                + _EMA_ALPHA * success_val
            )
            entry["latency_ema_ms"] = (
                (1.0 - _EMA_ALPHA) * float(entry["latency_ema_ms"])
                + _EMA_ALPHA * float(latency_ms)
            )
        entry["n"] = n + 1
        entry["last_seen"] = time.time()
        _save_stats(stats, self._stats_path)

    def debug_scores(self, query: str) -> dict[str, float]:
        """Return the full name→posterior map. For tests + telemetry."""
        return self._score_all(query)

    # ── Scoring internals ────────────────────────────────────────

    def _score_all(self, query: str) -> dict[str, float]:
        sps_scores = self._sps_scores(query)
        stats = _load_stats(self._stats_path)
        tools_dict = stats.get("tools", {})
        # Gather latencies in the current pool for z-score normalisation.
        latencies: list[float] = []
        for name in self._tools_by_name:
            entry = tools_dict.get(name, {})
            latencies.append(float(entry.get("latency_ema_ms", 0.0)))
        if latencies:
            mean_lat = sum(latencies) / len(latencies)
            var = sum((x - mean_lat) ** 2 for x in latencies) / len(latencies)
            std_lat = math.sqrt(var) if var > 0 else 1.0
        else:
            mean_lat, std_lat = 0.0, 1.0

        raw: dict[str, float] = {}
        for name in self._tools_by_name:
            entry = tools_dict.get(name, {})
            success = float(entry.get("success_ema", 0.5))
            latency = float(entry.get("latency_ema_ms", mean_lat))
            lat_z = (latency - mean_lat) / std_lat
            raw[name] = (
                self.alpha * sps_scores.get(name, 0.0)
                + self.beta * success
                - self.gamma * lat_z
            )
        # Softmax for stable posterior.
        if not raw:
            return {}
        max_val = max(raw.values())
        exps = {k: math.exp(v - max_val) for k, v in raw.items()}
        total = sum(exps.values()) or 1.0
        return {k: v / total for k, v in exps.items()}

    def _sps_scores(self, query: str) -> dict[str, float]:
        """Char-TF-IDF cosine between query and each tool."""
        query_tf = Counter(_char_ngrams(query))
        if not query_tf:
            return {name: 0.0 for name in self._tools_by_name}
        q_norm = self._l2_idf(query_tf)
        if q_norm == 0.0:
            return {name: 0.0 for name in self._tools_by_name}
        out: dict[str, float] = {}
        for name, vec in self._vecs.items():
            tool_norm = self._tool_norms[name]
            if tool_norm == 0.0:
                out[name] = 0.0
                continue
            dot = 0.0
            for gram, q_count in query_tf.items():
                t_count = vec.tf.get(gram, 0)
                if not t_count:
                    continue
                idf = self._idf(gram)
                dot += q_count * idf * t_count * idf
            out[name] = dot / (q_norm * tool_norm)
        return out

    def _idf(self, gram: str) -> float:
        df = self._df.get(gram, 0)
        # Add-one smoothing so OOV grams in the query don't nuke the score.
        return math.log((1.0 + self._n_docs) / (1.0 + df)) + 1.0

    def _l2_idf(self, tf: Counter[str]) -> float:
        s = 0.0
        for gram, count in tf.items():
            idf = self._idf(gram)
            s += (count * idf) ** 2
        return math.sqrt(s)
