# Serial MCP Server

![MCP](https://img.shields.io/badge/MCP-compatible-blue)\
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)\
![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)\
![Serial](https://img.shields.io/badge/Serial-RS232%2FUART-green)

`serial-mcp-server` is a stateful serial port Model Context Protocol (MCP) server using `stdio` transport for\
developer tooling and AI agents. Works out of the box with Claude Code, VS Code with Copilot, and any\
MCP-compatible runtime. The server uses [pyserial](https://github.com/pyserial/pyserial) for cross-platform\
serial compatibility with macOS, Windows, and Linux.

This fork is based on Enrico Santagati's original [es617/serial-mcp-server](https://github.com/es617/serial-mcp-server)\
implementation.

> **Example:** Let Claude Code list available serial ports, connect to your microcontroller, reset it via DTR, and read\
> the boot banner from your hardware.

## Demo

[Video walkthrough](https://www.youtube.com/watch?v=FdFdXjoyyAM) — connecting to a serial device, sending commands,\
reading responses, and creating plugins.

---

## Why this exists

If you’ve ever copy-pasted commands into `screen` or `minicom`, guessed baud rates, toggled DTR to kick a bootloader,\
and re-run the same test sequence 20 times — this is for you.

You have a serial device. You want an AI agent to talk to it — open a port, send commands, read responses, debug\
protocols. This server makes that possible.

It gives any MCP-compatible agent a full set of serial tools: listing ports, opening connections, reading, writing,\
line-oriented I/O, control line manipulation — plus protocol specs and device plugins, so the agent can reason about\
higher-level device behavior instead of just raw bytes.

The agent calls these tools, gets structured JSON back, and reasons about what to do next — without you manually typing\
commands into a terminal for every step.

**What agents can do with it:**

- **Develop and debug** — connect to your device, send commands, read responses, and diagnose issues conversationally\
  (boot banners, prompts, error codes).
- **Iterate on new firmware** — attach a protocol spec so the agent understands your command set, boot modes, and\
  output format as they evolve.
- **Automate test flows** — reset device via DTR, wait for prompt, run a command sequence, validate output.
- **Explore unknown devices** — probe command sets, discover prompts, infer message formats.
- **Build serial automation** — long-running test rigs, manufacturing bring-up, CI hardware smoke tests.

---

## Who is this for?

- **Embedded engineers** — faster iteration on serial protocols, conversational debugging, automated test sequences
- **Hobbyists and makers** — interact with serial devices without writing boilerplate; let the agent help\
  reverse-engineer simple protocols
- **QA and test engineers** — build repeatable serial test suites with plugin tools
- **Support and field engineers** — diagnose serial device issues interactively without specialized tooling
- **Researchers** — automate data collection from serial devices, explore device capabilities systematically

---

## Quickstart

Install the server from this repository, or from a wheel on its GitHub releases page:

```bash
pip install git+https://github.com/jonatsu/serial-mcp-server.git
```

The `serial-mcp-server` package on PyPI is the upstream project,\
[es617/serial-mcp-server](https://github.com/es617/serial-mcp-server), which does not have the features of this fork\
yet.

Then register it with your MCP client. The server is a standard stdio MCP server: the client starts the `serial_mcp`\
command and talks to it over stdin and stdout. With Claude Code, run:

```bash
claude mcp add serial -- serial_mcp
```

Most other clients read a JSON file with an `mcpServers` object, such as `.mcp.json` in a project for Claude Code,\
`.cursor/mcp.json` for Cursor, or `.gemini/settings.json` for Gemini CLI. Add this entry:

```json
{
  "mcpServers": {
    "serial": {
      "command": "serial_mcp",
      "args": [],
      "env": {}
    }
  }
}
```

To skip the install, let [uv](https://docs.astral.sh/uv/) fetch and run the server each time the client starts it.\
Replace the `serial_mcp` command with `uvx`:

```bash
claude mcp add serial -- uvx --from git+https://github.com/jonatsu/serial-mcp-server serial_mcp
```

```json
{
  "mcpServers": {
    "serial": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/jonatsu/serial-mcp-server",
        "serial_mcp"
      ],
      "env": {}
    }
  }
}
```

[Set up other MCP clients](#set-up-other-mcp-clients) lists where each client keeps the file and the clients that use\
another shape.

Then ask the agent, for example:

> "List available serial ports and connect to the one on /dev/ttyUSB0 at 115200 baud."

<p align="center"><img src="docs/assets/scan.gif" alt="Scanning serial ports" width="600"></p>

---

## What the agent can do

Once connected, the agent has full serial capabilities:

- **List ports** to find available serial devices
- **Open and close** connections with configurable baud rate, parity, stop bits, and encoding
- **Read and write** data in text, hex, or base64 format
- **Line-oriented I/O** — readline and read-until-delimiter for text protocols
- **Control lines** — set or pulse DTR and RTS for hardware reset and boot mode entry
- **Flush** input and output buffers
- **Attach protocol specs** to understand device-specific commands and data formats
- **Use plugins** for high-level device operations instead of raw reads/writes
- **Create specs and plugins** for new devices so future sessions start "knowing" your protocol
- **PTY mirroring** — attach screen, minicom, or custom scripts to the same serial session the agent is using

The agent can coordinate multi-step flows automatically — e.g., toggle reset, wait for prompt, send init sequence,\
stream output.

At a high level:

**Raw Serial → Protocol Spec → Plugin**

You can start with raw serial tools, then move up the stack as your device protocol becomes understood and repeatable.

---

## Documentation

| Document | What it covers |
| -- | -- |
| [Concepts](docs/concepts.md) | How the agent works with devices: the security model, specs, plugins, the PTY mirror, the port log, and scripts. |
| [Configuration](docs/configuration.md) | Every `SERIAL_MCP_*` variable, and the data directory. |
| [Tools](docs/tools.md) | Every MCP tool, with its arguments and results. |
| [Scripts](docs/scripts.md) | The serial script language and the `serial_mcp_script` command. |
| [Plugins](docs/plugins.md) | What plugins are, enabling and installing them, and writing one. |
| [Roadmap](docs/roadmap.md) | Known work that is not done yet. |
| [CHANGELOG](CHANGELOG.md) | What changed in each release. |
| [Contributing](CONTRIBUTING.md) | Development setup, code layout, conventions, and releases. |

---

## Set up other MCP clients

Every client starts the same command, `serial_mcp`, and passes `SERIAL_MCP_*` variables through its `env` setting. Most\
keep the server in a JSON file with the `mcpServers` shape from the [Quickstart](#quickstart):

| Client | File | Shape |
| -- | -- | -- |
| Claude Code | `.mcp.json` in the project root, or `claude mcp add` | `mcpServers` |
| Claude Desktop | `claude_desktop_config.json`, opened with Settings > Developer > Edit Config | `mcpServers` |
| Cursor | `.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` | `mcpServers` |
| Gemini CLI | `.gemini/settings.json` in the project, or `~/.gemini/settings.json` | `mcpServers` |
| VS Code with Copilot | `.vscode/mcp.json` in the project | `servers`, below |
| Codex CLI | `~/.codex/config.toml`, or `.codex/config.toml` in a trusted project | TOML, below |

A client started from a desktop launcher may not see the `PATH` of your shell, so it cannot find `serial_mcp`. Give the\
full path that `command -v serial_mcp` prints, or use the `uvx` form from the [Quickstart](#quickstart), which needs\
only `uvx` on that `PATH`.

### Claude Code

```bash
# Standard setup
claude mcp add serial -- serial_mcp

# Or run as a module
claude mcp add serial -- python -m serial_mcp_server

# Enable all plugins
claude mcp add serial -e SERIAL_MCP_PLUGINS=all -- serial_mcp

# Enable specific plugins only
claude mcp add serial -e SERIAL_MCP_PLUGINS=mydevice,ota -- serial_mcp

# Debug logging
claude mcp add serial -e SERIAL_MCP_LOG_LEVEL=DEBUG -- serial_mcp
```

### VS Code with Copilot

VS Code names the object `servers` and takes the transport as `type`:

```json
{
  "servers": {
    "serial": {
      "type": "stdio",
      "command": "serial_mcp",
      "args": [],
      "env": {
        "SERIAL_MCP_PLUGINS": "all"
      }
    }
  }
}
```

### Codex CLI

```toml
[mcp_servers.serial]
command = "serial_mcp"
args = []

[mcp_servers.serial.env]
SERIAL_MCP_PLUGINS = "all"
```

### Cursor

Cursor shows a warning for tool names that contain dots, such as `serial.open`, although the tools still work. Set\
`SERIAL_MCP_TOOL_SEPARATOR` to `_` in `env` to name them `serial_open` and avoid the warning.

## Configuration

The server reads `SERIAL_MCP_*` environment variables once, when it starts; the examples above set them with `-e` or\
`env`. The common ones are `SERIAL_MCP_PLUGINS` to enable plugins, `SERIAL_MCP_MIRROR` for the PTY mirror,\
`SERIAL_MCP_LOG` to record every connection, and `SERIAL_MCP_TOOL_SEPARATOR` for clients that reject dots in tool\
names. [Configuration](docs/configuration.md) lists every variable and its default.

---

## Tools

| Category | Tools |
| -- | -- |
| **Serial Core** | `serial.list_ports`, `serial.open`, `serial.close`, `serial.connection_status`, `serial.configure`, `serial.read`, `serial.write`, `serial.readline`, `serial.read_until`, `serial.flush`, `serial.send_file`, `serial.set_dtr`, `serial.set_rts`, `serial.pulse_dtr`, `serial.pulse_rts` |
| **Introspection** | `serial.connections.list` |
| **Port Log** | `serial.log.start`, `serial.log.stop`, `serial.log.status`, `serial.log.mark` |
| **Scripts** | `serial.script.run`, `serial.script.status`, `serial.script.cancel`, `serial.script.list`, `serial.script.template` |
| **Protocol Specs** | `serial.spec.template`, `serial.spec.register`, `serial.spec.list`, `serial.spec.attach`, `serial.spec.get`, `serial.spec.read`, `serial.spec.search` |
| **Tracing** | `serial.trace.status`, `serial.trace.tail` |
| **Plugins** | `serial.plugin.template`, `serial.plugin.list`, `serial.plugin.reload`, `serial.plugin.load` |

See the [serial script reference](docs/scripts.md) for script syntax and examples.

### Run a script without an agent

`serial_mcp_script` runs one script on a port with no agent and no MCP server. A CI job reads the outcome from the exit\
status and the result file:

```bash
serial_mcp_script tests/uboot-smoke.serial --port /dev/ttyUSB0 --baud 115200 \
  --var SERVER_IP=10.0.0.1 --result build/out.json
```

The exit status is `0` when the script passed, `1` when it failed, `2` for an error or unusable input, and `130` when\
interrupted. See [Running from the command line](docs/scripts.md#running-from-the-command-line) for every option.

---

## Protocol Specs

Specs are markdown files that describe a serial device's protocol — connection settings, message format, commands, and\
multi-step flows. They live in `.serial_mcp/specs/` and teach the agent what the byte stream means.

Without a spec, the agent can still open a port and exchange data. With a spec, it knows what commands to send, what\
responses to expect, and what the data means.

You can create specs by telling the agent about your device — paste a datasheet, describe the protocol, or just let it\
explore and document what it finds. The agent generates the spec file, registers it, and references it in future\
sessions. You can also write specs by hand.

---

## Plugins

Plugins add device-specific shortcut tools to the server. Instead of the agent composing raw read/write sequences, a\
plugin provides high-level operations like `mydevice.read_temp` or `ota.upload_firmware`. They are Python modules in\
`.serial_mcp/plugins/`, off until `SERIAL_MCP_PLUGINS` enables them, and the agent can draft new ones for you to\
review. [Plugins](docs/plugins.md) covers enabling, installing, and writing them.

---

## Tracing

Every tool call is traced to `.serial_mcp/traces/trace.jsonl` and an in-memory ring buffer of the last 2000 events.\
Tracing is on by default; `SERIAL_MCP_TRACE=0` turns it off. The agent reads recent events with `serial.trace.tail`;\
see [Tracing](docs/tools.md#tracing) for the event format.

---

## PTY Mirror

When the MCP server owns a serial port, most OSes prevent any other process from opening it. PTY mirroring creates a\
virtual clone port that external tools (screen, minicom, logic analyzers, custom scripts) can connect to\
simultaneously.

```bash
# Enable read-only mirror
claude mcp add serial \
  -e SERIAL_MCP_MIRROR=ro \
  -- serial_mcp

# After opening a connection, the response includes the mirror path:
# { "mirror": { "pty_path": "/dev/ttys004", "link": "/tmp/serial-mcp0", "mode": "ro" } }

# In another terminal:
screen /tmp/serial-mcp0 115200
```

| Mode | Behavior |
| -- | -- |
| `off` | No mirror (default). Only the MCP server can access the port. |
| `ro` | External tools see all serial data but cannot write to the device. |
| `rw` | External tools can both see data and write to the device. |

**Platform:** macOS and Linux only. On Windows, setting `SERIAL_MCP_MIRROR` to `ro`/`rw` logs a warning and is silently\
ignored. [Concepts](docs/concepts.md#pty-mirror--virtual-clone-ports) explains how the mirror works.

---

## Try without an agent

You can test the server interactively using the [MCP Inspector](https://github.com/modelcontextprotocol/inspector) — no\
Claude or other agent needed:

```bash
npx @modelcontextprotocol/inspector python -m serial_mcp_server
```

Open the URL with the auth token from the terminal output. The Inspector gives you a web UI to call any tool and see\
responses in real time.

---

## Known limitations

- **Single-client only.** The server handles one MCP session at a time (stdio transport). Multi-client transports\
  (HTTP/SSE) may be added later.
- **Exclusive access.** Without PTY mirroring, the MCP server must own the serial port exclusively.

---

## Safety

This server connects an AI agent to real hardware. That's the point — and it means the stakes are higher than\
pure-software tools.

**Plugins execute arbitrary code.** When plugins are enabled, the agent can create and run Python code on your machine\
with full server privileges. Review agent-generated plugins before loading them. Use `SERIAL_MCP_PLUGINS=name1,name2`\
to allow only specific plugins rather than `all`.

**Writes affect real devices.** A bad command sent to a serial device can trigger unintended behavior, disrupt other\
connected systems, or cause hardware damage (e.g., wiping flash, entering bootloader mode, triggering actuators).\
Consider what the agent can reach.

**Use tool approval deliberately.** When your MCP client prompts you to approve a tool call, consider whether you want\
to allow it once or always. "Always allow" is convenient but means the agent can repeat that action without further\
confirmation.

This software is provided as-is under the MIT License. You are responsible for what the agent does with your hardware.

---

## License

This project is licensed under the MIT License — see [LICENSE](LICENSE) for details.

## Acknowledgements

This project is built on top of the excellent [pyserial](https://github.com/pyserial/pyserial) library for\
cross-platform serial communication in Python.
