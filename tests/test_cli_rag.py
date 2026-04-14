"""Tests for the ``cc rag`` CLI subcommand family.

Exercises cmd_rag_namespaces / cmd_rag_route / cmd_rag_weights end-to-end
without pulling in chromadb or sentence-transformers (heavy optional
deps). Pure keyword + confidence-band logic only.
"""

from __future__ import annotations

import argparse
import os
import tempfile

from cc_cortex.cli.main import (
    cmd_rag_namespaces,
    cmd_rag_route,
    cmd_rag_weights,
)


class TestRagNamespaces:
    def test_lists_all_five(self, capsys):
        cmd_rag_namespaces(argparse.Namespace())
        out = capsys.readouterr().out
        for ns in ("knowledge", "memory", "cognition", "skills", "context"):
            assert ns in out
        assert "description" in out


class TestRagRoute:
    def test_high_confidence_returns_single(self, monkeypatch, capsys):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        ns = argparse.Namespace(query="correction feedback", confidence=0.1)
        cmd_rag_route(ns)
        out = capsys.readouterr().out
        assert "high-confidence" in out
        assert "memory" in out

    def test_medium_confidence_multiple(self, monkeypatch, capsys):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        ns = argparse.Namespace(query="rule skill plan", confidence=0.4)
        cmd_rag_route(ns)
        out = capsys.readouterr().out
        assert "medium-confidence" in out

    def test_low_confidence_all(self, monkeypatch, capsys):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        ns = argparse.Namespace(query="anything", confidence=0.9)
        cmd_rag_route(ns)
        out = capsys.readouterr().out
        assert "low-confidence" in out
        # All 5 namespaces should be in output
        for name in ("knowledge", "memory", "cognition", "skills", "context"):
            assert name in out

    def test_uses_project_cache_when_set(self, monkeypatch, capsys):
        tmp = tempfile.mkdtemp()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", tmp)
        ns = argparse.Namespace(query="test", confidence=0.5)
        cmd_rag_route(ns)
        # Should not crash — cache path resolution worked
        out = capsys.readouterr().out
        assert "Routed to" in out


class TestRagWeights:
    def test_no_project_dir_exits(self, monkeypatch):
        import pytest

        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        with pytest.raises(SystemExit) as exc:
            cmd_rag_weights(argparse.Namespace())
        assert exc.value.code == 1

    def test_empty_state_shows_defaults(self, monkeypatch, capsys):
        tmp = tempfile.mkdtemp()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", tmp)
        cmd_rag_weights(argparse.Namespace())
        out = capsys.readouterr().out
        assert "source" in out
        assert "weight" in out
        # All 5 source types should appear
        for stype in ("correction", "rule", "skill", "handoff", "memory"):
            assert stype in out

    def test_weights_reflect_recorded_feedback(self, monkeypatch, capsys):
        """After feedback() recording, the 'used' counter on correction > 0.

        ``feedback()`` increments ``used`` for source types in the list
        argument (``hits`` only moves when ``rerank()`` encounters the
        source, which we intentionally don't exercise here).
        """
        from cc_cortex.ziq_retrieval import ZIQRetrieval

        tmp = tempfile.mkdtemp()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", tmp)
        cache_dir = os.path.join(tmp, ".cc_cortex_cache")

        ziq = ZIQRetrieval(cache_dir=cache_dir)
        ziq.feedback(["corrections/feedback_x.md"])

        cmd_rag_weights(argparse.Namespace())
        out = capsys.readouterr().out
        lines = out.split("\n")
        correction_line = next(
            line for line in lines if line.startswith("correction")
        )
        # Columns: source, weight, hits, used, use_rate
        parts = correction_line.split()
        used = int(parts[3])
        assert used >= 1
