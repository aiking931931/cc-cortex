# Contributing to CC Cortex

Thank you for your interest in contributing to CC Cortex! This document outlines the process and standards for contributions.

## Getting Started

1. **Fork the repository** and clone your fork
2. **Create a branch** from `main`:
   ```bash
   git checkout -b feat/my-feature
   ```
3. **Install dev dependencies**:
   ```bash
   pip install -e ".[dev]"
   ```
   This installs `ruff`, `pytest`, and `pytest-cov` — the only dev-time dependencies.

## Development Rules

### Zero External Dependencies

This is the **most important rule** in CC Cortex:

> The `cc_cortex` package must have **zero runtime dependencies** beyond the Python standard library.

Why:
- Hooks run on every Claude Code tool call. Import latency matters.
- No supply chain attack surface.
- Works on any system with Python 3.10+ out of the box.

If your feature needs something outside stdlib, consider:
1. Can you implement it with stdlib? (Usually yes.)
2. Can it be an optional extra? (e.g., `pip install cc-cortex[viz]`)
3. If neither works, it probably doesn't belong in core.

### Code Style

We use **ruff** for linting and formatting:

```bash
# Check for issues
ruff check src/ tests/

# Auto-fix what's possible
ruff check --fix src/ tests/

# Format
ruff format src/ tests/
```

Key style points:
- **Line length**: 100 characters
- **Quotes**: Double quotes
- **Imports**: sorted, grouped (stdlib / third-party / local)
- **Type hints**: Required for all public functions
- **Docstrings**: Required for all public classes and functions (Google style)

### Testing

We use **pytest**. All changes must pass the full test suite:

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=cc_cortex --cov-report=term-missing

# Run a specific test file
pytest tests/test_destruction_guard.py

# Run tests matching a pattern
pytest -k "test_sentinel"
```

**Testing requirements:**
- Every new module must have a corresponding test file
- Every bug fix must include a regression test
- Target: >90% line coverage for new code
- Tests must be deterministic (no flaky tests)
- Tests must not require network access or external services

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add hook_custom_rules module
fix: sentinel false positive on legitimate retries
docs: update quickstart for Windows users
test: add edge cases for multi-instance lock
refactor: extract config validation to separate module
chore: update ruff to 0.4.x
```

## Pull Request Process

### Before Submitting

1. **Run the full check suite:**
   ```bash
   ruff check src/ tests/
   ruff format --check src/ tests/
   pytest
   ```

2. **Verify zero new dependencies:**
   ```bash
   # Should show only stdlib imports
   grep -r "^import\|^from" src/cc_cortex/ | grep -v "cc_cortex" | sort -u
   ```

3. **Update documentation** if you added/changed public API

4. **Add yourself to CONTRIBUTORS** (if you'd like)

### PR Template

When creating a pull request, please include:

```markdown
## Summary
- What does this PR do? (1-3 bullet points)

## Type
- [ ] Bug fix
- [ ] New feature
- [ ] Refactoring
- [ ] Documentation
- [ ] Tests

## Checklist
- [ ] `ruff check` passes
- [ ] `ruff format --check` passes
- [ ] `pytest` passes (all 1090+ tests)
- [ ] No new external dependencies
- [ ] New code has tests
- [ ] Documentation updated (if applicable)

## Test Plan
- How did you verify this works?
```

### Review Process

1. All PRs need at least one approving review
2. CI must pass (ruff + pytest + zero-dependency check)
3. Maintainers may request changes — this is normal and collaborative
4. Once approved, a maintainer will merge via squash-merge

## Architecture Guidelines

### Adding a New Module

1. Create the module in the appropriate category:
   ```
   src/cc_cortex/modules/<category>/my_module.py
   ```

2. Follow the module interface:
   ```python
   """One-line description of what this module does."""

   from cc_cortex.core import HookResult

   def check(tool_name: str, tool_input: dict) -> HookResult:
       """Main hook entry point.

       Args:
           tool_name: The Claude Code tool being called
           tool_input: The tool's input parameters

       Returns:
           HookResult with allow/deny/warn decision
       """
       ...
   ```

3. Register it in the module catalog (`src/cc_cortex/modules/__init__.py`)

4. Add tests in `tests/test_my_module.py`

5. Document it in README.md's module table

### Module Categories

| Category | Purpose | When to add here |
|----------|---------|-----------------|
| `safety/` | Prevent destructive actions | Blocking dangerous operations |
| `memory/` | Persist knowledge across sessions | State management, learning |
| `coordination/` | Multi-instance orchestration | Locks, task distribution |
| `optimization/` | Reduce waste, improve efficiency | Token saving, cleanup |

## Reporting Issues

Please include:
1. Python version (`python --version`)
2. CC Cortex version (`cc-cortex --version`)
3. OS and Claude Code version
4. Steps to reproduce
5. Expected vs actual behavior
6. Relevant `cc_config.json` settings

## Code of Conduct

Be respectful, constructive, and collaborative. We're building tools to make AI-assisted development better for everyone.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
