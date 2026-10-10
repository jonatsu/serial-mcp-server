"""Running blocking calls that must finish, even when the request that started them is cancelled."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, TypeVarTuple

import anyio
import anyio.to_thread

T = TypeVar("T")
Ts = TypeVarTuple("Ts")


# The checker assumes a CancelScope may swallow its body's exception and fall through, leaving no return value.
# This scope is never cancelled and has no deadline, so it cannot.
async def run_to_completion(func: Callable[[*Ts], T], *args: *Ts) -> T:  # pyright: ignore[reportReturnType]
    """Run *func* in a worker thread and wait for it, shielded from cancellation.

    Use it for work that completes a change already begun, such as releasing a port or closing a log.
    ``anyio.to_thread.run_sync`` alone waits for a thread that has started, but its opening checkpoint
    raises a pending cancellation before the thread starts, so the cleanup would never run. A cancellation
    that arrives meanwhile is delivered at the caller's next checkpoint.

    The MCP SDK cancels a request through an AnyIO cancel scope, which this shield holds against. A task
    cancelled directly with ``Task.cancel()``, outside any cancel scope, goes through the shield.
    """
    with anyio.CancelScope(shield=True):
        return await anyio.to_thread.run_sync(func, *args)
