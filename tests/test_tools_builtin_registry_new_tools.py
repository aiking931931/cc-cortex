"""Tests for 2.15.0 reference tools wired into the default registry."""

from __future__ import annotations

import pytest

from concinno.tools.registry import get_default_registry

EXPECTED_NEW_TOOLS = (
    "PdfRead",
    "PdfExtract",
    "HtmlToText",
    "DuckDbQuery",
    "RssFetch",
)


def test_all_new_tools_registered_as_deferred():
    reg = get_default_registry()
    deferred = set(reg.list_deferred())
    for name in EXPECTED_NEW_TOOLS:
        assert name in deferred, f"{name} missing from deferred registry"


def test_select_exact_names():
    reg = get_default_registry()
    for name in EXPECTED_NEW_TOOLS:
        hits = reg.search(f"select:{name}")
        assert len(hits) == 1, f"select:{name} returned {len(hits)} hits"
        assert hits[0].name == name
        assert hits[0].source == "deferred"


def test_keyword_search_pdf():
    reg = get_default_registry()
    hits = reg.search("pdf")
    names = [h.name for h in hits]
    assert "PdfRead" in names or "PdfExtract" in names


def test_keyword_search_sql():
    reg = get_default_registry()
    # "duckdb" / "sql" should surface DuckDbQuery.
    hits = reg.search("duckdb")
    names = [h.name for h in hits]
    assert "DuckDbQuery" in names


def test_keyword_search_rss():
    reg = get_default_registry()
    hits = reg.search("rss")
    names = [h.name for h in hits]
    assert "RssFetch" in names


def test_keyword_search_html():
    reg = get_default_registry()
    hits = reg.search("html")
    names = [h.name for h in hits]
    assert "HtmlToText" in names


def test_new_tools_no_collision_with_core():
    reg = get_default_registry()
    core = set(reg.list_core())
    for name in EXPECTED_NEW_TOOLS:
        assert name not in core, f"{name} unexpectedly promoted to core"


def test_descriptions_mention_install_hint():
    reg = get_default_registry()
    for name in EXPECTED_NEW_TOOLS:
        hits = reg.search(f"select:{name}")
        assert hits
        # Description must advertise the pip install hint so the LLM can
        # recover when the optional extras are missing.
        desc = hits[0].description.lower()
        assert "install" in desc or "pip" in desc, (
            f"{name} description missing install hint"
        )


@pytest.mark.parametrize(
    "name,module_attr",
    [
        ("PdfRead", "concinno.tools.builtin.pdf:PdfRead"),
        ("PdfExtract", "concinno.tools.builtin.pdf:PdfExtract"),
        ("HtmlToText", "concinno.tools.builtin.html:HtmlToText"),
        ("DuckDbQuery", "concinno.tools.builtin.sql:DuckDbQuery"),
        ("RssFetch", "concinno.tools.builtin.rss:RssFetch"),
    ],
)
def test_lazy_get_resolves_when_deps_available(name, module_attr):
    """``reg.get(name)`` should lazy-import and cache when extras are
    installed. When extras are missing ``get`` returns None (logged, not
    raised) — we skip in that case since the behaviour is tested by the
    per-tool ``call()`` returning a pip-install error message.
    """
    module = module_attr.split(":", 1)[0]
    # Skip cleanly if the optional dep for the underlying module is not
    # present — the tool class itself imports fine (lazy inside call),
    # so reg.get will still resolve. The per-tool call tests cover the
    # missing-dep behaviour.
    reg = get_default_registry()
    tool = reg.get(name)
    assert tool is not None, f"{name} failed to lazy-import from {module}"
    assert tool.name  # tool protocol: has a name
    assert callable(tool.call)
