"""In-memory chat session store.

Unlike runs (Postgres, restart-safe — a re-analysis can take minutes and the
UI polls it across page loads), a chat answer takes a few seconds and is
disposable: losing an in-flight chat on a process restart just means the user
re-asks. So this stays a plain process-global dict behind a lock, no schema
migration needed. If chat history ever needs to survive restarts or span
multiple processes, promote this to a Postgres table (or Redis) behind the
same get/create/append/finish function signatures.
"""
import threading
import time
import uuid

_lock = threading.Lock()
_chats: dict[str, dict] = {}


def create_chat() -> str:
    chat_id = f"chat-{uuid.uuid4().hex[:12]}"
    with _lock:
        _chats[chat_id] = {
            "status": "processing", "events": [], "answer": None,
            "source": None, "evidence_ids": [], "error": None, "t0": time.time(),
        }
    return chat_id


def emit(chat_id: str, agent: str, msg: str) -> None:
    with _lock:
        chat = _chats.get(chat_id)
        if chat is None:
            return
        chat["events"].append({"t": round(time.time() - chat["t0"], 1), "agent": agent, "msg": msg})


def finish(chat_id: str, answer: str, source: str, evidence_ids: list[str]) -> None:
    with _lock:
        chat = _chats.get(chat_id)
        if chat is None:
            return
        chat.update(status="completed", answer=answer, source=source, evidence_ids=evidence_ids)


def fail(chat_id: str, error: str) -> None:
    with _lock:
        chat = _chats.get(chat_id)
        if chat is None:
            return
        chat.update(status="failed", error=error)


def get(chat_id: str) -> dict | None:
    with _lock:
        chat = _chats.get(chat_id)
        return dict(chat) if chat is not None else None
