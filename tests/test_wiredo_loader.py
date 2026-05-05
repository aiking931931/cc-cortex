"""Tests for concinno.wiredo_loader three-tier on-demand loader."""
from __future__ import annotations

import pytest

from concinno.wiredo_loader import (
    ALPHA_COMPLEX_MAX,
    ALPHA_COMPLICATED_MAX,
    ALPHA_SIMPLE_MAX,
    CHANGE_TYPES,
    DIMENSIONS,
    ROUTING,
    WiredoLoader,
    build_wiredo_prompt,
    estimate_tokens,
    tier_for_alpha,
)


@pytest.fixture
def loader():
    return WiredoLoader()


def test_dimensions_constant():
    assert DIMENSIONS == ("W", "I", "R", "E", "D", "O")


def test_change_types_count_17():
    assert len(CHANGE_TYPES) == 17
    assert "vscode_extension" in CHANGE_TYPES


def test_tier_boundaries():
    assert tier_for_alpha(0.0) == "simple"
    assert tier_for_alpha(ALPHA_SIMPLE_MAX - 0.01) == "simple"
    assert tier_for_alpha(ALPHA_SIMPLE_MAX) == "complicated"
    assert tier_for_alpha(ALPHA_COMPLICATED_MAX - 0.01) == "complicated"
    assert tier_for_alpha(ALPHA_COMPLICATED_MAX) == "complex"
    assert tier_for_alpha(ALPHA_COMPLEX_MAX - 0.01) == "complex"
    assert tier_for_alpha(ALPHA_COMPLEX_MAX) == "chaotic"
    assert tier_for_alpha(1.0) == "chaotic"


def test_estimate_tokens_is_len_div_4():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 100) == 25
    assert estimate_tokens("a" * 999) == 249


def test_routing_has_all_change_types_except_docs_only():
    for ct in CHANGE_TYPES:
        assert ct in ROUTING, f"{ct} missing from routing table"
    assert ROUTING["docs_only"] == ()


def test_load_core_nonempty(loader):
    core = loader.load_core()
    assert "WIREDOJudge" in core
    assert "SIX DIMENSIONS" not in core  # dims are in dims/, not core


def test_load_routing_contains_all_types(loader):
    r = loader.load_routing()
    for ct in CHANGE_TYPES:
        assert ct in r, f"{ct} missing from routing.md"


def test_load_all_dims(loader):
    dims = loader.load_all_dims()
    assert set(dims.keys()) == set(DIMENSIONS)
    for d, text in dims.items():
        assert text, f"dim {d} is empty"
        assert text.startswith(f"{d} ("), f"dim {d} body malformed"


def test_load_all_recipes(loader):
    recipes = loader.load_all_recipes()
    assert set(recipes.keys()) == set(CHANGE_TYPES)
    for ct, text in recipes.items():
        assert text, f"recipe {ct} is empty"


def test_unknown_dimension_raises(loader):
    with pytest.raises(ValueError, match="unknown dimension"):
        loader.load_dim("Z")


def test_unknown_change_type_raises(loader):
    with pytest.raises(ValueError, match="unknown change_type"):
        loader.load_recipe("nonexistent")


def test_build_simple_tier_is_core_only(loader):
    p = loader.build_prompt(change_type="library", alpha_t=0.10)
    assert "WIREDOJudge" in p  # core included
    assert "frontend (web UI" not in p  # no recipe
    assert "W (Wired)" not in p  # no L2 dims


def test_build_complicated_tier_includes_dims_and_routing(loader):
    p = loader.build_prompt(change_type="library", alpha_t=0.40)
    assert "W (Wired)" in p
    assert "D (Defended) — STRONGEST" in p
    assert "library (importable)" in p  # routing table mentions it
    assert "library (importable Python/JS/Rust module)" not in p  # no L3


def test_build_complex_tier_includes_matching_recipe(loader):
    p = loader.build_prompt(change_type="library", alpha_t=0.70)
    assert "library (importable Python/JS/Rust module)" in p
    assert "frontend (web UI / React / HTML)" not in p  # other recipes excluded


def test_build_chaotic_tier_no_change_type_includes_all_recipes(loader):
    # Chaotic + all recipes exceeds the 2500t default budget, so we must
    # raise max_tokens to see the unshrunken output.
    p = loader.build_prompt(change_type=None, alpha_t=0.95, max_tokens=5000)
    for ct in CHANGE_TYPES:
        # every recipe has its header line starting with "<change_type> ("
        assert f"{ct} (" in p, f"chaotic missed {ct}"


def test_build_chaotic_with_change_type_still_focused(loader):
    p = loader.build_prompt(change_type="backend", alpha_t=0.95)
    assert "backend (API endpoint" in p
    # with specific change_type, other recipes are NOT included
    assert "frontend (web UI / React / HTML)" not in p


def test_build_budget_shrinks_to_core(loader):
    # 50t budget forces everything dropped except core.
    p = loader.build_prompt(change_type="library", alpha_t=0.95, max_tokens=50)
    assert "WIREDOJudge" in p
    assert "library (importable Python/JS/Rust module)" not in p
    assert "W (Wired)" not in p


def test_build_budget_keeps_dims_drops_recipes(loader):
    # Post-2.11.0 sizes: core ~1053t (route schema added), routing ~408t,
    # dims ~607t, all_recipes ~1150t.
    # Budget 2200t → core+routing+dims (~2068t) fits but +recipes (~3218t) doesn't.
    # Expected: recipes dropped, dims kept.
    p = loader.build_prompt(change_type=None, alpha_t=0.95, max_tokens=2200)
    assert "W (Wired)" in p  # dims kept
    assert "frontend (web UI / React / HTML)" not in p  # recipes dropped


def test_build_invalid_change_type_raises(loader):
    with pytest.raises(ValueError, match="unknown change_type"):
        loader.build_prompt(change_type="bogus", alpha_t=0.5)


def test_build_invalid_alpha_raises(loader):
    with pytest.raises(ValueError, match="alpha_t must be in"):
        loader.build_prompt(change_type=None, alpha_t=1.5)
    with pytest.raises(ValueError, match="alpha_t must be in"):
        loader.build_prompt(change_type=None, alpha_t=-0.1)


def test_build_wiredo_prompt_module_function():
    p = build_wiredo_prompt(change_type="hook", alpha_t=0.60)
    assert "WIREDOJudge" in p
    assert "hook (CC/CCC guard" in p


def test_cache_clear_reloads(loader, tmp_path):
    # Create an alternate templates dir, verify loader respects it.
    fake = tmp_path / "wiredo"
    (fake / "dims").mkdir(parents=True)
    (fake / "recipes").mkdir()
    (fake / "core.md").write_text("FAKE_CORE")
    (fake / "routing.md").write_text("FAKE_ROUTING")
    for d in DIMENSIONS:
        (fake / "dims" / f"{d}.md").write_text(f"FAKE_DIM_{d}")
    for c in CHANGE_TYPES:
        (fake / "recipes" / f"{c}.md").write_text(f"FAKE_RECIPE_{c}")

    alt = WiredoLoader(templates_dir=fake)
    p = alt.build_prompt(change_type="library", alpha_t=0.70)
    assert "FAKE_CORE" in p
    assert "FAKE_RECIPE_library" in p
    assert "FAKE_DIM_W" in p


def test_missing_templates_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        WiredoLoader(templates_dir=tmp_path / "does_not_exist")


def test_token_budget_reduction_vs_original():
    """Verify the three-tier loader actually shrinks vs. old static body."""
    # Old _WIREDO_BODY was ~2750t. New default at alpha_t=0.40 should be
    # substantially smaller.
    p = build_wiredo_prompt(change_type=None, alpha_t=0.40)
    tokens = estimate_tokens(p)
    assert tokens < 2500, f"complicated default not shrunk: {tokens}t"
    assert tokens > 500, f"complicated default suspiciously small: {tokens}t"
