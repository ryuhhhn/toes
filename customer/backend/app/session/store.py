"""Session storage.

In-process by design: zero infrastructure for a hackathon, and the protocol below is the
whole surface a Redis implementation would need to satisfy.

Operational consequence, repeated here because it is the single most likely cause of a
mid-demo failure: run exactly one uvicorn worker, and no --reload. Carts live in this
process and a reload takes them with it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from app.config import get_settings
from app.session.models import Session

log = logging.getLogger(__name__)


@runtime_checkable
class SessionStore(Protocol):
    async def get(self, session_id: str) -> Session | None: ...
    async def save(self, session: Session) -> Session: ...
    async def delete(self, session_id: str) -> None: ...
    async def create(self, merchant_id: str, session_id: str | None = None) -> Session: ...


class InMemorySessionStore:
    def __init__(self, ttl_seconds: int | None = None):
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl_seconds or get_settings().session_ttl_seconds

    async def create(self, merchant_id: str, session_id: str | None = None) -> Session:
        session = Session(merchant_id=merchant_id)
        if session_id:
            session.id = session_id
        self._sessions[session.id] = session
        log.info("session %s created for merchant %s", session.id, merchant_id)
        return session

    async def get(self, session_id: str) -> Session | None:
        self._sweep()
        return self._sessions.get(session_id)

    async def get_or_create(self, session_id: str | None, merchant_id: str) -> Session:
        if session_id:
            existing = await self.get(session_id)
            if existing is not None:
                return existing
            return await self.create(merchant_id, session_id=session_id)
        return await self.create(merchant_id)

    async def save(self, session: Session) -> Session:
        session.touch()
        self._sessions[session.id] = session
        return session

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def all(self) -> list[Session]:
        return list(self._sessions.values())

    def _sweep(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._ttl)
        expired = [sid for sid, s in self._sessions.items() if s.updated_at < cutoff]
        for session_id in expired:
            self._sessions.pop(session_id, None)
        if expired:
            log.info("swept %d expired session(s)", len(expired))


_store: InMemorySessionStore | None = None


def get_session_store() -> InMemorySessionStore:
    global _store
    if _store is None:
        _store = InMemorySessionStore()
    return _store
