from __future__ import annotations

from .models import Event, utcnow


class EventLog:
    def __init__(self) -> None:
        self._events: list[Event] = []
        self._next_seq = 1

    def append(self, event_type: str, **data: object) -> Event:
        event = Event(
            seq=self._next_seq,
            event_type=event_type,
            created_at=utcnow(),
            data=dict(data),
        )
        self._events.append(event)
        self._next_seq += 1
        return event

    def since(self, seq: int) -> tuple[Event, ...]:
        return tuple(event for event in self._events if event.seq > seq)

    def all(self) -> tuple[Event, ...]:
        return tuple(self._events)

    @property
    def latest_seq(self) -> int:
        if not self._events:
            return 0
        return self._events[-1].seq
