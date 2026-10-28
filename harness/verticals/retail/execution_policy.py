"""Host-owned admission for fresh GPU work in a scoped agent execution.

Schedulers install a synchronous callback in their task context. It receives
the worker's maximum credit reservation before any provider call is launched.
Ordinary interactive chats have no additional admission policy. Tool arguments
cannot set or replace this callback; child asyncio tasks inherit the context.
"""
from contextvars import ContextVar
import inspect
from typing import Callable

FRESH_INSPECTION_ADMISSION: ContextVar[Callable[[str], None] | None] = ContextVar(
    "rocoto_fresh_inspection_admission", default=None,
)
RETRIEVAL_VIEW: ContextVar[object | None] = ContextVar("rocoto_run_retrieval_view", default=None)


def admit_fresh_inspection(maximum_usd: str) -> None:
    callback = FRESH_INSPECTION_ADMISSION.get()
    if callback is not None:
        result = callback(maximum_usd)
        if result is not None:
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("Fresh inspection admission must synchronously return None or raise to deny")
