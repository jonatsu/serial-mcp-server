ruff := 'uv run --with ruff ruff'

# Run the repository's Ruff linter.
lint:
    {{ ruff }} check .

# Format repository code.
format:
    {{ ruff }} format .

# Check repository code without rewriting it.
format-check:
    {{ ruff }} format --check .

# Run the repository's lint and formatting checks.
check: lint format-check
