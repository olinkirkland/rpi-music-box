"""Live events from the box to any open web pages.

The box thread publishes; each open page (one web server thread each)
subscribes and gets its own queue. A page that stops reading is dropped
rather than allowed to hold anything up.
"""

from __future__ import annotations

import logging
import queue
import threading

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self, max_queued: int = 100) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()
        self._max_queued = max_queued
        # The latest value of each "state" event, for pages that just connected.
        self._latest: dict[str, dict] = {"now": {"state": "idle"}}

    STATE_EVENTS = ("now", "volume", "wifi")

    @property
    def now(self) -> dict:
        """The latest "now playing" state."""
        return self.latest("now") or {"state": "idle"}

    def latest(self, event: str) -> dict | None:
        with self._lock:
            value = self._latest.get(event)
            return dict(value) if value is not None else None

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._max_queued)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: str, data) -> None:
        with self._lock:
            if event in self.STATE_EVENTS:
                self._latest[event] = dict(data)
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                log.debug("dropping a web page that stopped reading events")
                self.unsubscribe(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)
