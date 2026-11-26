"""R1-FIX-03: Non-blocking RunEventBus with Bounded Replay Journal, Last-Event-ID & HTTP 410."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Deque, Dict, List, Optional, Set, Tuple

from app.schemas.events import SSEEvent


class EventReplayExpiredError(Exception):
    """Raised when a Last-Event-ID cursor falls outside the retained replay buffer (HTTP 410 Gone)."""

    error_code: str = "event_replay_expired"


@dataclass
class _Subscriber:
    queue: asyncio.Queue[Optional[SSEEvent]]
    closed: bool = False
    overflowed: bool = False


class RunEventBus:
    """Decouples Agent Runtime event emission from SSE subscriber lifecycle.

    Guarantees:
    - publish() is strictly non-blocking (never stalls non-stream or slow-consumer runs).
    - Slow subscribers are disconnected/marked overflowed instead of blocking Runtime or peers.
    - Bounded replay journal retains recent events; terminal_event is preserved independently.
    - Last-Event-ID cursor validation runs BEFORE SSE streaming starts (HTTP 410 on expired cursor).
    - Terminal event is published at most once (atomic single-confirmation path).
    """

    def __init__(
        self,
        run_id: str,
        *,
        max_buffer_size: int = 512,
        subscriber_queue_size: int = 256,
        max_subscribers: int = 32,
    ):
        self.run_id = run_id
        self.max_buffer_size = max(1, int(max_buffer_size))
        self.subscriber_queue_size = max(1, int(subscriber_queue_size))
        self.max_subscribers = max(1, int(max_subscribers))
        self._seq: int = 0
        self._buffer: Deque[Tuple[int, SSEEvent]] = deque(maxlen=self.max_buffer_size)
        self._terminal_seq: Optional[int] = None
        self.terminal_event: Optional[SSEEvent] = None
        self._subscribers: Set[int] = set()
        self._sub_map: Dict[int, _Subscriber] = {}
        self._sub_counter: int = 0
        self.dropped_for_slow_subscribers: int = 0

    @property
    def latest_seq(self) -> int:
        return self._seq

    @property
    def latest_event_id(self) -> Optional[str]:
        return f"{self.run_id}-{self._seq}" if self._seq > 0 else None

    @property
    def is_finalized(self) -> bool:
        return self.terminal_event is not None

    def _next_event(self, event_name: str, status: str, data: Dict[str, Any]) -> Tuple[int, SSEEvent]:
        self._seq += 1
        evt = SSEEvent(
            run_id=self.run_id,
            event_id=f"{self.run_id}-{self._seq}",
            event=event_name,  # type: ignore[arg-type]
            status=status,
            data=data,
        )
        return self._seq, evt

    def _fanout(self, evt: SSEEvent, *, is_terminal: bool = False) -> None:
        for sid in list(self._subscribers):
            sub = self._sub_map.get(sid)
            if sub is None or sub.closed:
                continue
            try:
                sub.queue.put_nowait(evt)
                if is_terminal:
                    sub.closed = True
                    try:
                        sub.queue.put_nowait(None)
                    except asyncio.QueueFull:
                        pass
            except asyncio.QueueFull:
                self.dropped_for_slow_subscribers += 1
                sub.overflowed = True
                sub.closed = True
                # Signal disconnect to slow subscriber without blocking Runtime
                try:
                    sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    sub.queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    def publish(self, event_name: str, status: str, data: Dict[str, Any]) -> Optional[SSEEvent]:
        """Publishes a non-terminal event. Blocked automatically after terminal event (R1-FIX-04 B.5)."""
        if self.terminal_event is not None:
            return None
        seq, evt = self._next_event(event_name, status, data)
        self._buffer.append((seq, evt))
        self._fanout(evt, is_terminal=False)
        return evt

    def publish_terminal(self, event_name: str, status: str, data: Dict[str, Any]) -> Optional[SSEEvent]:
        """Atomically publishes the single terminal event (completed/failed/cancelled)."""
        if self.terminal_event is not None:
            return None
        seq, evt = self._next_event(event_name, status, data)
        self._terminal_seq = seq
        self.terminal_event = evt
        self._buffer.append((seq, evt))
        self._fanout(evt, is_terminal=True)
        return evt

    def parse_cursor(self, last_event_id: str) -> int:
        prefix = f"{self.run_id}-"
        if not last_event_id or not last_event_id.startswith(prefix):
            raise ValueError("cursor_run_mismatch")
        raw_seq = last_event_id[len(prefix):]
        if not raw_seq.isdigit():
            raise ValueError("invalid_cursor_format")
        return int(raw_seq)

    def validate_cursor(self, last_event_id: str) -> int:
        """Validates cursor ownership and retention window BEFORE SSE streaming begins."""
        cursor_seq = self.parse_cursor(last_event_id)
        if cursor_seq > self._seq:
            raise ValueError("cursor_ahead_of_stream")
        if cursor_seq == self._seq:
            return cursor_seq
        if not self._buffer:
            raise EventReplayExpiredError("Replay buffer is empty")
        oldest_seq = self._buffer[0][0]
        # To replay seq > cursor_seq without any gap, oldest_seq must be <= cursor_seq + 1
        if cursor_seq + 1 < oldest_seq:
            raise EventReplayExpiredError(
                f"Cursor {last_event_id} expired (oldest retained seq={oldest_seq})"
            )
        return cursor_seq

    def replay_after(self, last_event_id: str) -> List[SSEEvent]:
        cursor_seq = self.validate_cursor(last_event_id)
        out = [evt for seq, evt in self._buffer if seq > cursor_seq]
        if (
            self.terminal_event is not None
            and self._terminal_seq is not None
            and self._terminal_seq > cursor_seq
            and (not out or out[-1].event_id != self.terminal_event.event_id)
        ):
            out.append(self.terminal_event)
        return out

    def subscribe(self) -> int:
        if len(self._subscribers) >= self.max_subscribers:
            raise RuntimeError("max_subscribers_exceeded")
        self._sub_counter += 1
        sid = self._sub_counter
        sub = _Subscriber(queue=asyncio.Queue(maxsize=self.subscriber_queue_size))
        self._sub_map[sid] = sub
        self._subscribers.add(sid)
        return sid

    def unsubscribe(self, sid: int) -> None:
        self._subscribers.discard(sid)
        sub = self._sub_map.pop(sid, None)
        if sub is not None:
            sub.closed = True

    async def stream(
        self,
        *,
        last_event_id: Optional[str] = None,
        heartbeat_seconds: float = 2.0,
    ) -> AsyncGenerator[str, None]:
        """FIX-211: Subscribe BEFORE snapshotting replay so mid-replay terminal events are never lost."""
        # Subscribe first so any event published during snapshot is captured in sub.queue
        sid = self.subscribe()
        sub = self._sub_map[sid]
        try:
            cursor_seq = 0
            if last_event_id is not None:
                cursor_seq = self.parse_cursor(last_event_id)
                replay_events = self.replay_after(last_event_id)
            else:
                replay_events = [evt for _, evt in self._buffer]
                if self.terminal_event is not None and (
                    not replay_events or replay_events[-1].event_id != self.terminal_event.event_id
                ):
                    replay_events.append(self.terminal_event)

            last_sent_seq = cursor_seq
            for evt in replay_events:
                try:
                    ev_s = self.parse_cursor(evt.event_id)
                except ValueError:
                    ev_s = last_sent_seq + 1
                if ev_s <= last_sent_seq:
                    continue
                last_sent_seq = ev_s
                yield evt.to_sse()
                if self.terminal_event is not None and evt.event_id == self.terminal_event.event_id:
                    return

            if self.terminal_event is not None:
                # NG-06: Drain any intermediate queued events before terminal_event; never duplicate terminal_event
                while not sub.queue.empty():
                    q_evt = sub.queue.get_nowait()
                    if q_evt is None:
                        break
                    try:
                        q_seq = self.parse_cursor(q_evt.event_id)
                    except ValueError:
                        q_seq = last_sent_seq + 1
                    if q_seq <= last_sent_seq:
                        continue
                    last_sent_seq = q_seq
                    yield q_evt.to_sse()
                    if q_evt.event_id == self.terminal_event.event_id:
                        return
                if not sub.overflowed and self._terminal_seq is not None and self._terminal_seq > last_sent_seq:
                    yield self.terminal_event.to_sse()
                return

            while True:
                try:
                    evt = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat_seconds)
                except TimeoutError:
                    if self.terminal_event is not None:
                        if not sub.overflowed and (self._terminal_seq is None or self._terminal_seq > last_sent_seq):
                            yield self.terminal_event.to_sse()
                        break
                    yield ": heartbeat\n\n"
                    continue

                if evt is None:
                    # CR-06: On slow-subscriber overflow, do NOT skip straight to terminal_event pretending continuity!
                    if not sub.overflowed and self.terminal_event is not None and (
                        self._terminal_seq is None or self._terminal_seq > last_sent_seq
                    ):
                        yield self.terminal_event.to_sse()
                    break
                try:
                    ev_seq = self.parse_cursor(evt.event_id)
                except ValueError:
                    ev_seq = last_sent_seq + 1
                if ev_seq <= last_sent_seq:
                    continue
                last_sent_seq = ev_seq
                yield evt.to_sse()
                if self.terminal_event is not None and evt.event_id == self.terminal_event.event_id:
                    break
        finally:
            # Disconnect policy (FIX-211): only detach subscriber; NEVER cancel the Agent Run!
            self.unsubscribe(sid)
