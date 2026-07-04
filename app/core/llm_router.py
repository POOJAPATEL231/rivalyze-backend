"""LLM router — STUB.

Owner: Mihir (full impl is Phase 2 of execution_plan.md, Sat 13:00–15:00 +
17:30–18:30). This stub exists so the review agent (Phase 3) can import the
FROZEN interface and run end-to-end against the no-backend path.

Frozen signature (from mihir_task_overview.md §Key Interfaces):
    complete(
        task_class: Literal["extract", "reason"],
        prompt: str,
        schema: type[BaseModel],
        emit: Callable[[dict], None],
    ) -> tuple[BaseModel, str]

When Phase 2 ships, the real `complete` replaces this definition. Callers
(agents) do not need to change.

Behaviour in the stub:
    - No API keys configured: raise RuntimeError("all lanes exhausted").
      The review agent's outer try/except converts this to low_signal.
    - MOCK_MODE=1: build a default instance of the requested schema and
      return it on lane "mock". Useful for smoke tests and the demo.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Literal, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_TASK_CLASSES = ("extract", "reason")


def complete(
    task_class: Literal["extract", "reason"],
    prompt: str,
    schema: type[T],
    emit: Callable[[dict], None] | None = None,
) -> tuple[T, str]:
    """STUB: return a default schema instance when MOCK_MODE=1; else raise.

    Phase 2 replaces this with the lane-ordered httpx calls, backoff,
    JSON repair, budget enforcement, and demo-reserve key resolution
    described in execution_plan.md Phase 2.
    """
    if task_class not in _TASK_CLASSES:
        raise ValueError(f"task_class must be one of {_TASK_CLASSES}, got {task_class!r}")

    mock_mode = os.getenv("MOCK_MODE", "0") == "1"
    if mock_mode:
        # Build a zero-content instance of the requested schema.
        # The review agent's _flatten_complaint + low_signal guards
        # will turn the empty result into something sensible for tests.
        try:
            instance = schema.model_construct()  # bypass validation — fields default
        except Exception:
            # Some schemas (e.g. with required competitor field) need *something*.
            kwargs = {}
            if "competitor" in schema.model_fields:
                kwargs["competitor"] = ""
            instance = schema.model_construct(**kwargs)
        # Best-effort: if the schema has a `competitor` field and the
        # prompt mentions "{competitor} customer complaints problems MONTH",
        # lift the name out so the mock looks like a real extraction.
        if "competitor" in schema.model_fields:
            try:
                import re
                m = re.search(r"complaints about ([A-Z][\w& .'-]{1,40})", prompt)
                if m:
                    object.__setattr__(instance, "competitor", m.group(1).strip())
            except Exception:
                pass
        if emit is not None:
            try:
                emit({"agent": "router", "msg": f"router-stub · mock complete: {schema.__name__}"})
            except Exception:
                pass
        return instance, "mock"

    # No backend wired up — match Phase 2's "all lanes exhausted" behaviour
    # so callers' except branches (review agent) degrade to low_signal.
    if emit is not None:
        try:
            emit({"agent": "router", "msg": "router-stub · no lanes configured (set MOCK_MODE=1)"})
        except Exception:
            pass
    raise RuntimeError("all lanes exhausted (router stub: no MOCK_MODE, no API keys)")
