"""Provider-agnostic session store.

Message history is keyed purely by `session_id` and stores canonical
`ChatMessage` objects -- never anything provider-shaped. This is what makes
mid-conversation failover "zero context loss": the next provider in the
chain receives the exact same canonical history the previous provider saw,
plus whatever new turn triggered the call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock

from schemas.contracts import ChatMessage, MessageRole


@dataclass
class SessionRecord:
    session_id: str
    messages: list[ChatMessage] = field(default_factory=list)
    # Tracks which provider actually served each turn, for observability /
    # demo output only -- never fed back into the canonical history.
    served_by: list[str] = field(default_factory=list)


class InMemorySessionStore:
    """Simple process-local session store. Swappable for a Redis-backed
    implementation in a multi-instance deployment without changing the
    router's interface."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._sessions: dict[str, SessionRecord] = {}

    def get_or_create(self, session_id: str) -> SessionRecord:
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                record = SessionRecord(session_id=session_id)
                self._sessions[session_id] = record
            return record

    def append_user_message(self, session_id: str, content: str) -> SessionRecord:
        with self._lock:
            record = self._sessions.setdefault(session_id, SessionRecord(session_id=session_id))
            record.messages.append(ChatMessage(role=MessageRole.user, content=content))
            return record

    def append_assistant_message(self, session_id: str, content: str, provider: str) -> None:
        with self._lock:
            record = self._sessions.setdefault(session_id, SessionRecord(session_id=session_id))
            record.messages.append(ChatMessage(role=MessageRole.assistant, content=content))
            record.served_by.append(provider)

    def history(self, session_id: str) -> list[ChatMessage]:
        with self._lock:
            record = self._sessions.get(session_id)
            return list(record.messages) if record else []

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
