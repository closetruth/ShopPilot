from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.db.models import MessageRow, SessionRow

def create_session(db: Session) -> SessionRow:
    row = SessionRow()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row

def get_session(db: Session, session_id: str) -> SessionRow | None:
    return db.get(SessionRow, session_id)

def add_message(db: Session, session_id: str, role: str, content: str) -> MessageRow:
    next_seq = db.scalar(
        select(func.coalesce(func.max(MessageRow.seq), 0)).where(MessageRow.session_id == session_id)
    )
    msg = MessageRow(session_id=session_id, role=role, content=content, seq=int(next_seq) + 1)
    db.add(msg)
    session = db.get(SessionRow, session_id)
    if session:
        from datetime import datetime, timezone
        session.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(msg)
    return msg

def list_messages(db: Session, session_id: str) -> list[MessageRow]:
    return list(
        db.scalars(
            select(MessageRow).where(MessageRow.session_id == session_id).order_by(MessageRow.seq.asc())
        )
    )
