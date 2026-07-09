"""Per-connection state shared across message handlers."""
from dataclasses import dataclass, field

from app.models.resident import Resident


@dataclass
class ConnectionContext:
    """State for one WebSocket connection.

    `resident` is a detached ORM snapshot (loaded in the start_chat session,
    read-only afterwards — safe because sessions use expire_on_commit=False).
    Handlers that need fresh/mutable rows must re-fetch by id in their own
    session.
    """

    user_id: str
    user_name: str
    conversation_id: str | None = None
    resident: Resident | None = None
    chat_messages: list[dict] = field(default_factory=list)
    memory_context: dict | None = None
    encounter_context: str | None = None  # B2: scene context for this chat

    @property
    def in_chat(self) -> bool:
        return self.conversation_id is not None and self.resident is not None

    def reset_chat(self) -> None:
        self.conversation_id = None
        self.resident = None
        self.chat_messages = []
        self.memory_context = None
        self.encounter_context = None
