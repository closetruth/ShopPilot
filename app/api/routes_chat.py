from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent

from app.api.deps import DbSession
from app.api.schemas import ChatRequest
from app.db.repository import add_message, get_session
from app.services.chat_service import (
    SessionNotFound,
    ensure_session,
    stream_chat,
)
from app.services.extract_service import extract_aftersales


router = APIRouter(prefix="/api/v1", tags=["chat"])


@router.post(
    "/chat",
    response_class=EventSourceResponse,
    response_model=None,
)
async def chat(payload: ChatRequest, db: DbSession) -> Response:
    if payload.session_id and get_session(db, payload.session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if payload.mode == "extract":
        session = await ensure_session(db, payload.session_id)
        add_message(db, session.id, "user", payload.message)
        try:
            result = await extract_aftersales(payload.message)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail="Upstream extraction failed",
            ) from exc
        return JSONResponse(content=result.model_dump(mode="json"))

    async def events() -> AsyncIterator[ServerSentEvent]:
        try:
            async for event in stream_chat(
                db,
                payload.session_id,
                payload.message,
            ):
                yield ServerSentEvent(data=event)
        except SessionNotFound as exc:
            yield ServerSentEvent(
                data={"type": "error", "message": str(exc)}
            )

    return EventSourceResponse(events())
