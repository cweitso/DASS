from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.db.session import SessionLocal


def get_db() -> Iterator[Session]:
    """Per-request session. Closing without a commit rolls the request back."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
