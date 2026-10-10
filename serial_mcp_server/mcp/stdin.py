"""The MCP client's messages from stdin, read by a daemon thread so that Ctrl-C can end the server."""

from __future__ import annotations

import os
import sys
import threading
from typing import Self

import anyio
import anyio.from_thread
import anyio.lowlevel
from anyio.lowlevel import EventLoopToken
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream


class StdinLines:
    """An async iterator over the lines read from file descriptor *fd*, standard input by default.

    The SDK's own reader waits for each line in an AnyIO worker thread. That thread cannot be interrupted and is not
    a daemon thread, so after a Ctrl-C both the cancellation and the interpreter's exit waited until the client sent
    a line or closed stdin. The daemon thread here holds up neither; the loop waits only on a memory stream. It reads
    the descriptor with ``os.read`` rather than through ``sys.stdin``, because a daemon thread blocked inside a
    buffered file holds that file's lock, and the interpreter aborts when it cannot take the lock at exit.
    """

    def __init__(self, fd: int | None = None) -> None:
        self._fd: int = fd if fd is not None else sys.stdin.fileno()
        send, receive = anyio.create_memory_object_stream[str](0)
        self._send: MemoryObjectSendStream[str] = send
        self._receive: MemoryObjectReceiveStream[str] = receive
        self._thread: threading.Thread | None = None

    def __aiter__(self) -> Self:
        if self._thread is None:
            self._thread = threading.Thread(
                target=_pump,
                args=(self._fd, self._send, anyio.lowlevel.current_token()),
                name="mcp-stdin",
                daemon=True,
            )
            self._thread.start()
        return self

    async def __anext__(self) -> str:
        try:
            return await self._receive.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration from None


READ_SIZE = 65536


def _pump(fd: int, send: MemoryObjectSendStream[str], token: EventLoopToken) -> None:
    """Hand each line read from *fd* to the event loop, then close *send* at end of file."""
    try:
        pending = b""
        while chunk := os.read(fd, READ_SIZE):
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                # A UTF-8 newline byte never sits inside a multi-byte character, so each line decodes alone.
                anyio.from_thread.run(send.send, line.decode("utf-8", errors="replace") + "\n", token=token)
        if pending:
            anyio.from_thread.run(send.send, pending.decode("utf-8", errors="replace"), token=token)
        anyio.from_thread.run_sync(send.close, token=token)
    except (OSError, anyio.RunFinishedError, anyio.BrokenResourceError, anyio.ClosedResourceError):
        pass  # stdin failed or the server stopped first; nothing waits for the rest of stdin
