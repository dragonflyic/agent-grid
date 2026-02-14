"""In-memory SQS-compatible event bus."""

import asyncio
import logging
from typing import Awaitable, Callable

from ..config import settings
from .public_api import Event, EventType, utc_now

logger = logging.getLogger("agent_grid.event_bus")


class EventBus:
    """
    In-memory event bus with SQS-compatible interface.

    Designed to be swapped out for real SQS later.
    """

    def __init__(self, max_size: int | None = None):
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_size or settings.event_bus_max_size)
        self._subscribers: dict[EventType | None, list[Callable[[Event], Awaitable[None]]]] = {}
        self._running = False
        self._consumer_task: asyncio.Task | None = None

    async def publish(self, event_type: EventType, payload: dict | None = None) -> None:
        """Publish an event to the bus."""
        event = Event(
            type=event_type,
            timestamp=utc_now(),
            payload=payload or {},
        )
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.error(f"Event bus queue full ({self._queue.maxsize}), dropping event: {event_type}")

    def subscribe(
        self,
        handler: Callable[[Event], Awaitable[None]],
        event_type: EventType | None = None,
    ) -> None:
        """
        Subscribe to events.

        Args:
            handler: Async function to call when event is received.
            event_type: Specific event type to subscribe to, or None for all events.
        """
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)

    def unsubscribe(
        self,
        handler: Callable[[Event], Awaitable[None]],
        event_type: EventType | None = None,
    ) -> None:
        """Unsubscribe from events."""
        if event_type in self._subscribers:
            self._subscribers[event_type] = [h for h in self._subscribers[event_type] if h != handler]

    async def _dispatch(self, event: Event) -> None:
        """Dispatch an event to all matching subscribers."""
        handlers: list[Callable[[Event], Awaitable[None]]] = []

        # Get handlers for this specific event type
        if event.type in self._subscribers:
            handlers.extend(self._subscribers[event.type])

        # Get handlers subscribed to all events
        if None in self._subscribers:
            handlers.extend(self._subscribers[None])

        # Run all handlers concurrently, log any errors
        if handlers:
            results = await asyncio.gather(
                *[handler(event) for handler in handlers],
                return_exceptions=True,
            )
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(
                        f"Event handler failed for {event.type}: {result}",
                        exc_info=result,
                    )

    async def _consume_loop(self) -> None:
        """Main consumer loop that processes events from the queue."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                try:
                    await self._dispatch(event)
                except Exception:
                    logger.exception(f"Error dispatching event {event.type}")
                finally:
                    self._queue.task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected error in event bus consumer loop")

    async def start(self) -> None:
        """Start the event bus consumer."""
        if self._running:
            return
        self._running = True
        self._consumer_task = asyncio.create_task(self._consume_loop())

    async def stop(self) -> None:
        """Stop the event bus consumer."""
        self._running = False
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None

    async def wait_until_empty(self) -> None:
        """Wait until all events in the queue have been processed."""
        await self._queue.join()

    @property
    def pending_count(self) -> int:
        """Number of events waiting to be processed."""
        return self._queue.qsize()


# Global event bus instance
event_bus = EventBus()
