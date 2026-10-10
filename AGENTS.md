# Agent Instructions

An MCP server that lets agents drive serial devices. [CONTRIBUTING.md](CONTRIBUTING.md) holds the conventions; this\
file lists what must not be missed.

## Before you commit

- Run `just check` (Ruff, formatting, import layers, basedpyright) and `uv run --extra test python -m pytest tests -q`.\
  Both must pass; CI runs the same.
- Run `just integration` after changing `port/`, `portlog/`, `script/`, or the server's stdio handling. It drives real\
  processes on pseudo-terminals for about 90 seconds and needs Linux.
- Never open a real serial device. Tests and the harness use pseudo-terminals, and a device attached to this machine\
  may be in use.

## Type checking

basedpyright runs in `standard` mode (`[tool.basedpyright]` in `pyproject.toml`). Fix what it reports when the report\
is real. When the checker misreads correct code, configure it or add one `# pyright: ignore[<rule>]` with a comment\
saying why; never contort working code to satisfy it. In `tests/`, three rules are warnings on purpose; leave those\
warnings alone unless you are already changing that code.

## Code rules

- Async code uses AnyIO, never `asyncio` directly; choose the cancellation behavior of every blocking call as\
  CONTRIBUTING's "Blocking calls and cancellation" describes.
- Only `serial_mcp_server/config.py` reads the environment. A new `SERIAL_MCP_*` variable needs a row in\
  `docs/configuration.md`; a test fails without it.
- Imports follow the package layers in `pyproject.toml`; `lint-imports` fails on a violation.
- When a tool's behavior changes, change its description and property descriptions in the same commit, written for\
  agents: lead with when to use the tool.
- Plugins import only from `serial_mcp_server.plugin_api`; its `__all__` changes only with a CHANGELOG entry.

## Documents

- CONTRIBUTING's "Documentation" section says which file owns which kind of text. Record follow-up work in\
  `docs/roadmap.md`, and user-visible changes under `## Unreleased` in `CHANGELOG.md`.
- Markdown prose stops at 120 characters, and a multi-line paragraph ends each line but the last with a `\` hard break.\
  mdformat and markdownlint run in pre-commit. Rewrap long prose with the maintainer's rewrap script; ask for it rather\
  than rewrapping by hand.
