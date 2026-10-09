ruff := 'uv run --with ruff ruff'

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

# Run the repository's lint and formatting checks.
check: lint format-check
