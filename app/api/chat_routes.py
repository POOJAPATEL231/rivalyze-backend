"""POST /api/v1/chat + GET /api/v1/chat/{chat_id} — ask questions about a
company's competitive intel. Answers from stored agent reports/signals first,
falling back to a live web search when the stored data doesn't cover the
question (see app/core/chatbot.py). Background-task + poll, same shape as the
existing /analyze -> /runs/{id} lifecycle.
"""
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi import Depends

from ..core import chat_store
from ..core import chatbot
from ..core.auth import require_token
from ..models import ChatEvent, ChatRequest, ChatResponse, ChatStatus

router = APIRouter(prefix="/api/v1")


@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(require_token)])
def chat(req: ChatRequest, background_tasks: BackgroundTasks) -> ChatResponse:
    chat_id = chat_store.create_chat()
    background_tasks.add_task(chatbot.start_chat, chat_id, req.company, req.question, req.run_id)
    return ChatResponse(chat_id=chat_id, status="processing")


@router.get("/chat/{chat_id}", response_model=ChatStatus, dependencies=[Depends(require_token)])
def get_chat(chat_id: str) -> ChatStatus:
    row = chat_store.get(chat_id)
    if row is None:
        raise HTTPException(status_code=404, detail="chat not found")
    return ChatStatus(
        chat_id=chat_id,
        status=row["status"],
        events=[ChatEvent(**e) for e in row["events"]],
        answer=row["answer"],
        source=row["source"],
        evidence_ids=row["evidence_ids"],
        error=row["error"],
    )
