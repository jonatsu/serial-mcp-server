ruff := 'uv run ruff'

# Lint the repository, or only the given paths.
[positional-arguments]
lint *paths='.':
    {{ ruff }} check "$@"

# Format the repository in place, or only the given paths.
[positional-arguments]
format *paths='.':
    {{ ruff }} format "$@"

# Check formatting without rewriting anything, for the repository or only the given paths.
[positional-arguments]
format-check *paths='.':
    {{ ruff }} format --check "$@"

# Check that imports follow the package layers (contracts in pyproject.toml).
lint-imports:
    uv run lint-imports

# Type-check the package, tests, tools, and examples (settings in pyproject.toml, [tool.basedpyright]).
typecheck:
    uv run --extra test basedpyright

# Run the repository's lint, formatting, import-layer, and type checks.
check: lint format-check lint-imports typecheck

# Run the slow end-to-end harness against the demo device (about 90 s).
integration:
    uv run python tests/integration/run.py
