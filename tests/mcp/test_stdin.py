"""Tests for serial_mcp_server.mcp.stdin — stdin lines read by a daemon thread."""

from __future__ import annotations

import json
import os
import select
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
    lines = StdinLines(read_fd)
    try:
        started = time.monotonic()
        with anyio.move_on_after(0.2):
            async for _line in lines:
                pass

        assert time.monotonic() - started < 2
    finally:
        os.close(write_fd)  # end of file ends the reader thread before its descriptor closes
        if lines._thread is not None:  # started by the first iteration
            lines._thread.join(timeout=2)
        os.close(read_fd)


def _read_response_line(fd: int, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    response = bytearray()
    while b"\n" not in response:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            pytest.fail("the server did not finish its initialize response before the startup deadline")
        chunk = os.read(fd, 65536)
        if not chunk:
            pytest.fail("the server exited before finishing its initialize response")
        response.extend(chunk)
    return bytes(response)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT to a child process is POSIX-only")
@pytest.mark.parametrize("ignore_sigint", [False, True], ids=["launcher", "ignored-launcher"])
def test_the_server_exits_on_sigint_while_its_client_keeps_stdin_open(tmp_path, ignore_sigint):
    env = {**os.environ, "SERIAL_MCP_SPEC_ROOT": str(tmp_path / ".serial_mcp")}
    # A background launcher can ignore SIGINT; enable the signal under test before running the real entry point.
    bootstrap = (
        "import runpy, signal; signal.signal(signal.SIGINT, signal.default_int_handler); "
        "runpy.run_module('serial_mcp_server', run_name='__main__', alter_sys=True)"
    )
    command = [sys.executable, "-X", "faulthandler", "-c", bootstrap]
    if ignore_sigint:
        # Ignore only inside the launcher; exec preserves that disposition without changing pytest's signals.
        command = [
            sys.executable,
            "-c",
            "import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_IGN); "
            "os.execv(sys.executable, sys.argv[1:])",
            *command,
        ]
    server = subprocess.Popen(  # noqa: S603 - fixed interpreter and test-owned launcher arguments
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        assert server.stdin is not None
        assert server.stdout is not None
        assert server.stderr is not None
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
        assert json.loads(_read_response_line(server.stdout.fileno(), timeout=10))["id"] == 1

        server.send_signal(signal.SIGINT)

        try:
            status = server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # The hang is intermittent; faulthandler writes every thread's stack to stderr on SIGABRT.
            server.send_signal(signal.SIGABRT)
            server.wait(timeout=5)
            stderr = server.stderr.read().decode(errors="replace")
            pytest.fail(f"the server was still running 5 s after SIGINT; its stderr:\n{stderr}")
        assert status == 0
        assert b"Traceback" not in server.stderr.read()
    finally:
        if server.poll() is None:
            server.kill()
            server.wait(timeout=5)
        if server.stdin is not None:
            server.stdin.close()
        if server.stdout is not None:
            server.stdout.close()
        if server.stderr is not None:
            server.stderr.close()


async def test_a_line_split_across_reads_inside_a_multibyte_character_arrives_whole(monkeypatch):
    import serial_mcp_server.mcp.stdin as stdin_module

    monkeypatch.setattr(
        stdin_module, "READ_SIZE", 1
    )  # every byte is its own read, so every boundary is tried
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, '{"name": "Jyväskylä €"}\r\n{"b": 2}\n'.encode())
        os.close(write_fd)

        lines = [line async for line in StdinLines(read_fd)]
    finally:
        os.close(read_fd)

    assert lines == ['{"name": "Jyväskylä €"}\r\n', '{"b": 2}\n']
    assert [json.loads(line) for line in lines] == [{"name": "Jyväskylä €"}, {"b": 2}]
