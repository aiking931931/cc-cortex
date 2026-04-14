"""Tests for cc_cortex.wiredo_change_type classifier."""
from __future__ import annotations

import pytest

from cc_cortex.wiredo_change_type import (
    CHANGE_TYPES,
    detect_change_type,
    detect_from_command,
    detect_from_path,
)


# ── detect_from_path ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/cc_cortex/hooks/on_stop.py", "hook"),
        ("src/cc_cortex/guards/foo_guard.py", "hook"),
        ("src/cc_cortex/cli/main.py", "cli"),
        ("scripts/deploy.py", "cli"),
        ("src/cc_cortex/my_cli.py", "cli"),
        ("src/frontend/components/Button.tsx", "frontend"),
        ("app/web/page.jsx", "frontend"),
        ("api/routes/users.py", "backend"),
        ("server/handlers/webhook.py", "backend"),
        ("tests/test_foo.py", "test_only"),
        ("tests/unit/test_bar.py", "test_only"),
        ("foo_test.py", "test_only"),
        ("frontend/__tests__/Button.spec.ts", "test_only"),
        ("docs/README.md", "docs_only"),
        ("CHANGELOG.md", "docs_only"),
        ("assets/hero.png", "image"),
        ("public/logo.svg", "image"),
        ("audio/theme.mp3", "audio"),
        ("video/intro.mp4", "video"),
        ("report.docx", "word_doc"),
        ("contract.doc", "word_doc"),
        ("migrations/001_init.sql", "migration"),
        ("alembic/versions/abc_migrate.py", "migration"),
        ("src/cc_cortex/escalation.py", "library"),
        (".claude/skills/foo/SKILL.md", "ai_prompt"),
        ("agents/planner/prompt.md", "ai_prompt"),  # agents/ marker wins
        ("dist/cc_cortex-1.14.0.tar.gz", "build_artifact"),
        ("dist/pkg.whl", "build_artifact"),
        ("query.sql", "db_query"),
    ],
)
def test_detect_from_path(path, expected):
    assert detect_from_path(path) == expected


def test_detect_from_path_empty_returns_none():
    assert detect_from_path("") is None


def test_detect_from_path_unknown_extension_returns_none():
    assert detect_from_path("random/foo.xyz") is None


def test_detect_from_path_windows_separator():
    # Backslashes should be normalized.
    assert detect_from_path("src\\cc_cortex\\hooks\\on_stop.py") == "hook"


# ── detect_from_command ─────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("python deploy.py --target=prod", "deploy"),
        ("kubectl apply -f k8s/", "deploy"),
        ("rsync -av dist/ server:/var/www/", "deploy"),
        ("docker push ghcr.io/foo/bar:latest", "deploy"),
        ("python -m build", "build_artifact"),
        ("twine upload dist/*", "build_artifact"),
        ("docker build -t myimg .", "build_artifact"),
        ("npm run build", "build_artifact"),
        ("alembic upgrade head", "migration"),
        ("alembic downgrade -1", "migration"),
        ("psql -c 'SELECT count(*) FROM users'", "db_query"),
        ("sqlite3 app.db 'SELECT * FROM logs'", "db_query"),
    ],
)
def test_detect_from_command(cmd, expected):
    assert detect_from_command(cmd) == expected


def test_detect_from_command_empty_returns_none():
    assert detect_from_command("") is None


def test_detect_from_command_non_delivery_returns_none():
    assert detect_from_command("ls -la") is None
    assert detect_from_command("python -c 'print(1)'") is None


# ── detect_change_type (aggregate) ──────────────────────────────


def test_aggregate_empty_returns_default():
    assert detect_change_type() == "other"
    assert detect_change_type(default="custom") == "custom"


def test_aggregate_command_wins_over_paths():
    got = detect_change_type(
        paths=["src/cc_cortex/foo.py"], commands=["python deploy.py"]
    )
    assert got == "deploy"


def test_aggregate_mode_from_paths():
    got = detect_change_type(
        paths=[
            "src/cc_cortex/foo.py",
            "src/cc_cortex/bar.py",
            "tests/test_foo.py",
        ]
    )
    # 2 library + 1 test → library (mode)
    assert got == "library"


def test_aggregate_docs_only_requires_clean_sweep():
    # Mixed docs + code should NOT be docs_only.
    got = detect_change_type(
        paths=["README.md", "src/cc_cortex/foo.py"]
    )
    assert got != "docs_only"
    # Pure docs sweep IS docs_only.
    got2 = detect_change_type(paths=["README.md", "docs/guide.md"])
    assert got2 == "docs_only"


def test_aggregate_tie_prefers_non_library():
    got = detect_change_type(
        paths=[
            "src/cc_cortex/foo.py",  # library
            "src/cc_cortex/hooks/bar.py",  # hook
        ]
    )
    # 1 library + 1 hook, tie → should prefer hook (more specific)
    assert got == "hook"


def test_aggregate_unknown_paths_fall_back_to_default():
    got = detect_change_type(paths=["random/foo.xyz", "other/bar.unknown"])
    assert got == "other"


def test_aggregate_filters_empty_strings():
    got = detect_change_type(paths=["", "src/cc_cortex/foo.py", ""])
    assert got == "library"


def test_change_types_constant_matches_loader_set():
    from cc_cortex.wiredo_loader import CHANGE_TYPES as LOADER_TYPES

    assert set(CHANGE_TYPES) == set(LOADER_TYPES)
