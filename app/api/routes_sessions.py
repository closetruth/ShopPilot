from fastapi import APIRouter, HTTPException

from app.api.deps import DbSession
from app.api.schemas import MessageResponse
from app.db.repository import get_session, list_messages


router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


@router.get("/{session_id}/messages", response_model=list[MessageResponse])
async def session_messages(
    session_id: str,
    db: DbSession,
) -> list[MessageResponse]:
    if get_session(db, session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return [
        MessageResponse.model_validate(row)
        for row in list_messages(db, session_id)
    ]
