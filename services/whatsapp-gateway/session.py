"""Per-user conversation state for the WhatsApp assistant.

What the assistant remembers between messages: the user's saved address, the
last Places results (with their place_ids, which never reach the user), the
place currently being discussed, and a short message history. In-memory for
now, like the task store; R2 moves it to Redis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from maps import Place, PlaceDetails

MAX_HISTORY_MESSAGES = 20


@dataclass
class Session:
    user_phone: str
    location: str | None = None
    # The WhatsApp profile name, used to say who Siyona is calling for.
    user_name: str | None = None
    places: list[Place] = field(default_factory=list)
    # The place last looked up with place_details: what "them" and "there" refer to.
    current_place: PlaceDetails | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def remember(self, user_text: str, reply: str) -> None:
        self.history.extend(
            [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}]
        )
        del self.history[:-MAX_HISTORY_MESSAGES]

    # The last call this user made through the assistant, for "yes, book it" follow-ups.
    last_call_task_id: str | None = None
    last_call_place_id: str | None = None

    def note_call(
        self, task_id: str, place_id: str | None, callee: str | None, summary: str
    ) -> None:
        """Put a finished call's outcome into the conversation so follow-ups can use it."""
        self.last_call_task_id, self.last_call_place_id = task_id, place_id
        who = callee or "the store"
        self.history.append(
            {"role": "assistant", "content": f"(Call to {who} finished: {summary})"}
        )
        del self.history[:-MAX_HISTORY_MESSAGES]

    @property
    def place_ids(self) -> set[str]:
        ids = {place.place_id for place in self.places}
        if self.current_place is not None:
            ids.add(self.current_place.place_id)
        return ids


SESSIONS: dict[str, Session] = {}


def session_for(user_phone: str) -> Session:
    session = SESSIONS.get(user_phone)
    if session is None:
        session = SESSIONS[user_phone] = Session(user_phone)
    return session
