"""Tests for serial_mcp_server.mcp.stdin — stdin lines read by a daemon thread."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import anyio
import pytest

from serial_mcp_server.mcp.stdin import StdinLines


async def test_lines_arrive_in_order_and_end_of_file_ends_the_iteration():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, '{"a": 1}\n{"b": "é"}\nlast'.encode())
        os.close(write_fd)

        lines = [line async for line in StdinLines(read_fd)]
    finally:
        os.close(read_fd)

    assert lines == ['{"a": 1}\n', '{"b": "é"}\n', "last"]


async def test_a_cancelled_wait_ends_although_stdin_stays_open():
    read_fd, write_fd = os.pipe()
    try:
        lines = StdinLines(read_fd)
        started = time.monotonic()
        with anyio.move_on_after(0.2):
            async for _line in lines:
                pass

        assert time.monotonic() - started < 2
    finally:
        os.close(write_fd)  # end of file ends the reader thread before its descriptor closes
        lines._thread.join(timeout=2)
        os.close(read_fd)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT to a child process is POSIX-only")
def test_the_server_exits_on_sigint_while_its_client_keeps_stdin_open(tmp_path):
    env = {**os.environ, "SERIAL_MCP_SPEC_ROOT": str(tmp_path / ".serial_mcp")}
    server = subprocess.Popen(
        [sys.executable, "-m", "serial_mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        }
        server.stdin.write((json.dumps(initialize) + "\n").encode())
        server.stdin.flush()
        assert json.loads(server.stdout.readline())["id"] == 1  # the server is up and serving

        server.send_signal(signal.SIGINT)

        assert server.wait(timeout=5) == 0
        assert b"Traceback" not in server.stderr.read()
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()
        server.stdin.close()
