"""Search chain — STUB.

Owner: Tushar (full impl consumes the cache module published in Phase 1).
This stub exists so the review agent (Phase 3) can import the FROZEN
interface and the low-signal degradation path can be exercised.

Frozen signature (inferred from mihir_task_overview.md + execution_plan.md):
    async def search(query: str, emit: Callable) -> list[dict]
        -> list of { "url": str, "title": str, "content": str }

When Tushar's real search_chain.py ships, this file is replaced verbatim.
The review agent's contract is just: items are dicts with "url"/"title"/"content".
"""

from __future__ import annotations

import logging
import os
from typing import Callable

logger = logging.getLogger(__name__)


async def search(query: str, emit: Callable[[dict], None] | None = None) -> list[dict]:
    """STUB: return [].

    The review agent treats an empty result as a low-signal competitor.
    """
    if os.getenv("RIVALYZE_SEARCH_STUB_NOISY", "0") == "1" and emit is not None:
        try:
            emit({"agent": "search", "msg": f"search-stub · miss: {query}"})
        except Exception:
            pass
    return []
