"""Tests for concinno.handoff_templates — bundled template loader."""

from __future__ import annotations

import pytest

from concinno.handoff_templates import (
    GENERIC_TEMPLATE_NAME,
    SPECIALIZED_TEMPLATE_NAMES,
    TEMPLATES_DIR,
    TemplateNotFoundError,
    list_templates,
    load_template,
)


def test_generic_template_loads():
    body = load_template(GENERIC_TEMPLATE_NAME)
    assert "## §0" in body
    assert "## §1" in body
    assert "## §5" in body


@pytest.mark.parametrize("name", SPECIALIZED_TEMPLATE_NAMES)
def test_each_specialized_template_loads(name):
    body = load_template(name)
    assert body.strip()


def test_unknown_template_raises_lookuperror():
    with pytest.raises(TemplateNotFoundError):
        load_template("not-a-real-template")
    # Inherits LookupError so the broader catch works too.
    with pytest.raises(LookupError):
        load_template("also-not-real")


def test_list_templates_includes_generic_first():
    out = list_templates()
    assert out[0] == GENERIC_TEMPLATE_NAME
    assert set(out[1:]) == set(SPECIALIZED_TEMPLATE_NAMES)


def test_templates_dir_exists():
    assert TEMPLATES_DIR.is_dir()
    assert (TEMPLATES_DIR / "specialized").is_dir()


def test_each_specialized_has_section_marker():
    """Sanity: every specialized template uses non-overlapping §-prefix."""
    expected_prefixes = {
        "benchmark": "§B",
        "release": "§R",
        "research": "§S",
        "build": "§I",
    }
    for name, prefix in expected_prefixes.items():
        body = load_template(name)
        assert prefix in body, f"{name} missing {prefix} sections"
