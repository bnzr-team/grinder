"""Timeout utilities for connector operations.

Provides async timeout wrappers that raise ConnectorTimeoutError
instead of asyncio.TimeoutError for consistent error handling.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar

from grinder.connectors.errors import ConnectorTimeoutError

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

T = TypeVar("T")


async def wait_for_with_op(
    coro: Coroutine[Any, Any, T],
    timeout_ms: int,
    op: str,
) -> T:
    """Wait for coroutine with timeout, raising ConnectorTimeoutError on timeout.

    Args:
        coro: Coroutine to await
        timeout_ms: Timeout in milliseconds (0 = no timeout)
        op: Operation name for error message (e.g., "connect", "read")

    Returns:
        Result of the coroutine

    Raises:
        ConnectorTimeoutError: If timeout is exceeded
    """
    if timeout_ms <= 0:
        return await coro

    try:
        return await asyncio.wait_for(coro, timeout=timeout_ms / 1000)
    except TimeoutError as e:
        raise ConnectorTimeoutError(op=op, timeout_ms=timeout_ms) from e


async def cancel_tasks_with_timeout(
    tasks: set[asyncio.Task[Any]],
    timeout_ms: int,
    *,
    task_name_prefix: str = "",
) -> tuple[int, int]:
    """Cancel tasks and wait for completion with timeout.

    Cancels all tasks and waits for them to complete. If timeout is exceeded,
    logs a warning but does not raise (graceful degradation).

    Args:
        tasks: Set of tasks to cancel
        timeout_ms: Timeout for waiting on cancelled tasks
        task_name_prefix: Optional prefix to filter tasks by name

    Returns:
        Tuple of (cancelled_count, timeout_count)
    """
    if not tasks:
        return (0, 0)

    # Filter by prefix if specified
    to_cancel = tasks
    if task_name_prefix:
        to_cancel = {t for t in tasks if t.get_name().startswith(task_name_prefix)}

    if not to_cancel:
        return (0, 0)

    # Cancel all tasks
    for task in to_cancel:
        if not task.done():
            task.cancel()

    cancelled_count = len(to_cancel)
    timeout_count = 0

    # Wait for tasks to complete with timeout
    if timeout_ms > 0:
        try:
            await asyncio.wait_for(
                asyncio.gather(*to_cancel, return_exceptions=True),
                timeout=timeout_ms / 1000,
            )
        except TimeoutError:
            # Some tasks didn't complete in time
            timeout_count = sum(1 for t in to_cancel if not t.done())

    return (cancelled_count, timeout_count)


def create_named_task(
    coro: Coroutine[Any, Any, T],
    name: str,
    tasks_set: set[asyncio.Task[Any]] | None = None,
) -> asyncio.Task[T]:
    """Create a named task and optionally track it in a set.

    Args:
        coro: Coroutine to run as task
        name: Task name for debugging and filtering
        tasks_set: Optional set to add task to for tracking

    Returns:
        Created task
    """
    task = asyncio.create_task(coro, name=name)
    if tasks_set is not None:
        tasks_set.add(task)
        task.add_done_callback(tasks_set.discard)
    return task
