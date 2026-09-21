from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import SessionRow
from app.db.repository import (
    add_message,
    create_session,
    get_session,
    list_messages,
)
from app.llm.factory import get_chat_model
from app.prompts.chat import CUSTOMER_SERVICE_SYSTEM, build_chat_messages
from app.services.context_manager import trim_messages


class SessionNotFound(Exception):
    """Raised when a requested chat session does not exist."""


async def ensure_session(
    db: Session, session_id: str | None
) -> SessionRow:
    if session_id is None:
        return create_session(db)

    session = get_session(db, session_id)
    if session is None:
        raise SessionNotFound(f"Session not found: {session_id}")
    return session


def _chunk_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


async def stream_chat(
    db: Session, session_id: str | None, message: str
) -> AsyncIterator[dict[str, str]]:
    session = await ensure_session(db, session_id)
    yield {"type": "session", "session_id": session.id}

    add_message(db, session.id, "user", message)
    history = [
        {"role": row.role, "content": row.content}
        for row in list_messages(db, session.id)
    ]
    settings = get_settings()
    trimmed = trim_messages(
        history,
        system_text=CUSTOMER_SERVICE_SYSTEM,
        max_tokens=settings.max_context_tokens,
        max_turns=settings.max_history_turns,
    )
    model_messages = build_chat_messages(
        CUSTOMER_SERVICE_SYSTEM, trimmed
    )

    assistant_parts: list[str] = []
    try:
        async for chunk in get_chat_model().astream(model_messages):
            text = _chunk_text(chunk.content)
            if not text:
                continue
            assistant_parts.append(text)
            yield {"type": "token", "content": text}
    except Exception as exc:
        yield {"type": "error", "message": str(exc)}
        return

    add_message(
        db, session.id, "assistant", "".join(assistant_parts)
    )
    yield {"type": "done"}
