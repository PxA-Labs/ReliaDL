# Contributing Guide — ReliaDL

> **Audience**: Open Source Contributors, Internal Team
> **Reading time**: ~5 minutes

---

## 1. Getting Started

### 1.1 Development Setup

```bash
# Clone the repository
git clone https://github.com/PxA-Labs/ChunkGuard.git
cd ChunkGuard

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# or: .venv\Scripts\activate  # Windows

# Install in development mode with all extras
pip install -e ".[dev,test,docs]"

# Verify setup
pytest tests/unit/ -v
```

### 1.2 Dependencies

```
[dev extras]
ruff>=0.2.0          # Linter + formatter
mypy>=1.8.0          # Type checker
pre-commit>=3.5.0    # Git hooks

[test extras]
pytest>=8.0.0
pytest-asyncio>=0.23.0
pytest-cov>=4.1.0
coverage>=7.4.0

[docs extras]
mkdocs>=1.5.0
mkdocs-material>=9.5.0
```

---

## 2. Development Workflow

### 2.1 Branch Strategy

```
main ──────────────── production-ready, tagged releases
  │
  ├── develop ─────── integration branch, latest features
  │     │
  │     ├── feature/chunk-size-auto-adjust
  │     ├── feature/s3-protocol-adapter
  │     ├── fix/state-file-race-condition
  │     └── docs/update-api-reference
  │
  └── release/1.1.0 ── release candidates
```

### 2.2 Commit Message Format

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short description>

[optional body]

[optional footer(s)]
```

**Types**: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`

**Examples**:
```
feat(chunk-manager): add auto-adjustment for chunk count exceeding 100K
fix(retry-handler): prevent negative delay on first retry attempt
docs(api): add examples for authenticated download
test(assembler): add fault injection test for disk-full scenario
perf(hash): use streaming hash to reduce peak memory usage
```

### 2.3 Pull Request Process & Automated Quality Gates
 
 1. Create a feature branch from `develop`
 2. Make your changes with clear, focused commits following [Conventional Commits](https://www.conventionalcommits.org/)
 3. Ensure your PR Title and Description satisfy CI validation:
    - **PR Title**: Must use conventional commit syntax (e.g., `feat(core): add proxy support`) and be at least 10 characters.
    - **PR Description**: Must be at least 30 characters and describe the changes without empty placeholders.
 4. Ensure Novelty Verification passes: No debug placeholders (`# TODO`, `# FIXME`, `placeholder_value_here`, `dummy_value`) in production code.
 5. Ensure all unit and integration tests pass: `python -m unittest discover -s tests` or `pytest -v`
 6. Ensure code quality: `ruff check .` and `mypy src/`
 7. Update documentation in `docs/` and changelog if public APIs or behaviors change.
 8. Open a PR targeting `develop` (or `master` for hotfixes)
 9. Request review from CODEOWNERS maintainers
 10. Squash-merge after approval

---

## 3. Code Quality Standards

### 3.1 Code Style

- **Formatter**: `ruff format` (Black-compatible)
- **Linter**: `ruff check` (Flake8-compatible with extended rules)
- **Type Checking**: `mypy --strict`
- **Line Length**: 100 characters
- **Docstrings**: Google style

### 3.2 Pre-commit Hooks

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.2.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.8.0
    hooks:
      - id: mypy
        additional_dependencies: [pydantic, httpx]
```

Install with: `pre-commit install`

### 3.3 Documentation Standards

- All public functions/classes must have docstrings
- All parameters must have type annotations
- Complex algorithms must have inline comments explaining "why", not "what"
- Breaking changes must be documented in CHANGELOG.md

---

## 4. Testing Requirements

### 4.1 For All PRs

- [ ] All existing tests pass
- [ ] New code has unit tests
- [ ] Coverage does not decrease
- [ ] No type errors (`mypy --strict`)
- [ ] No lint warnings (`ruff check`)

### 4.2 For Feature PRs

- [ ] Integration tests for I/O behavior
- [ ] Edge cases tested (empty input, boundary values, errors)
- [ ] Documentation updated (API Reference, User Guide if user-facing)

### 4.3 For Bug Fix PRs

- [ ] Regression test that fails without the fix and passes with it
- [ ] Root cause documented in PR description

---

## 5. Architecture Guidelines

### 5.1 Adding a New Module

1. Create the module in `src/`
2. Define its public interface (classes, functions, types)
3. Add unit tests in `tests/unit/test_<module>.py`
4. Import in `src/__init__.py` if part of public API
5. Document in `docs/API_REFERENCE.md`

### 5.2 Modifying Existing Components

- Preserve backward compatibility for public APIs
- Deprecate before removing (at least one minor version)
- Update Architecture docs if component interactions change
- Update Data Flow docs if state machine changes

### 5.3 Error Handling

- Define custom exceptions in `src/exceptions.py`
- Never catch `Exception` or `BaseException` broadly
- Include context in error messages (chunk_index, url, byte_range)
- Classify errors as retryable or non-retryable

---

## 6. Release Process

```
1. Update version in pyproject.toml
2. Update CHANGELOG.md with all changes since last release
3. Create release branch: release/x.y.z
4. Run full test suite on all platforms
5. Create GitHub release with tag vx.y.z
6. Publish to PyPI: python -m build && twine upload dist/*
7. Merge release branch to main and develop
```

---

## 7. Contact

- **Maintainers**: See CODEOWNERS file
- **Issues**: GitHub Issues for bugs and feature requests
- **Discussions**: GitHub Discussions for questions and ideas

