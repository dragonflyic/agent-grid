"""Background webhook processor with deduplication and coalescing.

This module implements a two-stage webhook processing architecture:
1. Webhook Ingestion: Events are immediately stored in the database
2. Webhook Processing: Background processor reads events after a quiet period,
   deduplicates by issue ID, and decides whether to launch agents
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from ..config import settings
from ..execution_grid import event_bus, EventType
from .database import get_database
from .public_api import WebhookEvent, utc_now

logger = logging.getLogger("agent_grid.webhook_processor")


class WebhookProcessor:
    """
    Background processor for webhook events.

    Implements:
    - Time-based debouncing per issue (waits for quiet period before processing)
    - Event coalescing (combines multiple events for same issue)
    - Smart decision making (e.g., don't launch if issue opened then immediately closed)
    """

    def __init__(
        self,
        quiet_period_seconds: int | None = None,
        poll_interval_seconds: int | None = None,
    ):
        """
        Initialize the webhook processor.

        Args:
            quiet_period_seconds: Time to wait after receiving an event before processing.
                                  Allows related events to arrive. Default from settings.
            poll_interval_seconds: How often to check for events to process.
                                   Default from settings.
        """
        self._db = get_database()
        self._quiet_period = timedelta(
            seconds=quiet_period_seconds or settings.webhook_dedup_quiet_period_seconds
        )
        self._poll_interval = poll_interval_seconds or settings.webhook_dedup_poll_interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background processing loop."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._process_loop())
        logger.info(
            f"Webhook processor started (quiet_period={self._quiet_period.total_seconds()}s, "
            f"poll_interval={self._poll_interval}s)"
        )

    async def stop(self) -> None:
        """Stop the background processing loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Webhook processor stopped")

    async def _process_loop(self) -> None:
        """Main processing loop."""
        while self._running:
            try:
                await self._process_batch()
            except Exception as e:
                logger.error(f"Error in webhook processor loop: {e}", exc_info=True)

            await asyncio.sleep(self._poll_interval)

    async def _process_batch(self) -> None:
        """Process a batch of webhook events."""
        # Get events that have been waiting longer than the quiet period
        cutoff_time = utc_now() - self._quiet_period
        events = await self._db.get_unprocessed_webhook_events(
            older_than=cutoff_time,
            limit=100,
        )

        if not events:
            return

        logger.debug(f"Processing {len(events)} webhook events")

        # Group events by (repo, issue_id) for coalescing
        events_by_issue: dict[tuple[str | None, str | None], list[WebhookEvent]] = defaultdict(list)
        for event in events:
            key = (event.repo, event.issue_id)
            events_by_issue[key].append(event)

        # Process each issue's events
        for (repo, issue_id), issue_events in events_by_issue.items():
            await self._process_issue_events(repo, issue_id, issue_events)

    async def _process_issue_events(
        self,
        repo: str | None,
        issue_id: str | None,
        events: list[WebhookEvent],
    ) -> None:
        """
        Process all events for a single issue.

        This method coalesces multiple events and makes a single decision
        about whether to trigger an agent.
        """
        if not events:
            return

        # Sort by received_at to process in order
        events = sorted(events, key=lambda e: e.received_at)

        # The primary event is the first one - others will be coalesced into it
        primary_event = events[0]

        # Analyze the event sequence to make a decision
        decision = self._analyze_event_sequence(events)

        if decision.should_trigger:
            # Publish the appropriate event to the event bus
            await self._publish_coalesced_event(primary_event, decision, events)
        else:
            logger.info(
                f"Skipping events for {repo}#{issue_id}: {decision.reason} "
                f"(coalesced {len(events)} events)"
            )

        # Mark all events as processed
        event_ids = [e.id for e in events]
        coalesced_into = primary_event.id if len(events) > 1 else None
        await self._db.mark_webhook_events_processed(event_ids, coalesced_into)

    def _analyze_event_sequence(self, events: list[WebhookEvent]) -> "ProcessingDecision":
        """
        Analyze a sequence of events to decide what action to take.

        Examples:
        - Issue opened → trigger
        - Issue opened, then labeled → trigger (with labels)
        - Issue opened, then closed → don't trigger
        - Issue labeled with trigger label → trigger
        - Issue unlabeled (removed trigger label) → don't trigger
        - Nudge request → trigger
        """
        if not events:
            return ProcessingDecision(should_trigger=False, reason="no events")

        # Track the final state
        actions = [e.action for e in events]
        event_types = [e.event_type for e in events]

        # Check for issue closed event - if present, don't launch
        if "closed" in actions:
            return ProcessingDecision(
                should_trigger=False,
                reason="issue was closed",
            )

        # Check for issue_comment with nudge
        for event in events:
            if event.event_type == "issue_comment" and event.action == "created":
                if event.payload:
                    try:
                        payload = json.loads(event.payload)
                        comment_body = payload.get("comment", {}).get("body", "")
                        if "@agent-grid nudge" in comment_body.lower():
                            return ProcessingDecision(
                                should_trigger=True,
                                event_type=EventType.NUDGE_REQUESTED,
                                reason="nudge command in comment",
                            )
                    except json.JSONDecodeError:
                        pass

        # Check for issue events
        has_opened = "opened" in actions
        has_labeled = "labeled" in actions

        # Collect all labels from the most recent event that has them
        final_labels: list[str] = []
        for event in reversed(events):
            if event.payload:
                try:
                    payload = json.loads(event.payload)
                    if "labels" in payload:
                        final_labels = payload.get("labels", [])
                        break
                    # Also check nested in issue object
                    issue_data = payload.get("issue", {})
                    if issue_data and "labels" in issue_data:
                        final_labels = [
                            label["name"] if isinstance(label, dict) else label
                            for label in issue_data.get("labels", [])
                        ]
                        break
                except json.JSONDecodeError:
                    pass

        # Determine trigger labels
        trigger_labels = {"agent", "automated", "agent-grid"}
        has_trigger_label = bool(set(final_labels) & trigger_labels)

        if has_opened and has_trigger_label:
            return ProcessingDecision(
                should_trigger=True,
                event_type=EventType.ISSUE_CREATED,
                reason="issue opened with trigger label",
                labels=final_labels,
            )

        if has_opened:
            # Opened without trigger label - check if label was added
            if has_trigger_label:
                return ProcessingDecision(
                    should_trigger=True,
                    event_type=EventType.ISSUE_CREATED,
                    reason="issue opened and labeled",
                    labels=final_labels,
                )
            # No trigger label
            return ProcessingDecision(
                should_trigger=False,
                reason="issue opened without trigger label",
            )

        if has_labeled and has_trigger_label:
            return ProcessingDecision(
                should_trigger=True,
                event_type=EventType.ISSUE_UPDATED,
                reason="trigger label added",
                labels=final_labels,
            )

        # Default: don't trigger
        return ProcessingDecision(
            should_trigger=False,
            reason=f"no actionable events (actions: {actions})",
        )

    async def _publish_coalesced_event(
        self,
        primary_event: WebhookEvent,
        decision: "ProcessingDecision",
        events: list[WebhookEvent],
    ) -> None:
        """Publish a coalesced event to the event bus."""
        # Get the most recent payload for full context
        payload_data: dict[str, Any] = {}
        for event in reversed(events):
            if event.payload:
                try:
                    payload_data = json.loads(event.payload)
                    break
                except json.JSONDecodeError:
                    pass

        # Build the event payload
        event_payload: dict[str, Any] = {
            "repo": primary_event.repo,
            "issue_id": primary_event.issue_id,
            "coalesced_events": len(events),
            "processing_reason": decision.reason,
        }

        # Add type-specific fields
        if decision.event_type == EventType.ISSUE_CREATED:
            issue_data = payload_data.get("issue", payload_data)
            event_payload.update({
                "title": issue_data.get("title"),
                "body": issue_data.get("body"),
                "labels": decision.labels or [],
                "html_url": issue_data.get("html_url"),
            })
        elif decision.event_type == EventType.ISSUE_UPDATED:
            issue_data = payload_data.get("issue", payload_data)
            event_payload.update({
                "action": "labeled",  # Coalesced action
                "title": issue_data.get("title"),
                "body": issue_data.get("body"),
                "state": issue_data.get("state"),
                "labels": decision.labels or [],
            })
        elif decision.event_type == EventType.NUDGE_REQUESTED:
            comment_data = payload_data.get("comment", {})
            event_payload.update({
                "source": "comment",
                "comment_body": comment_data.get("body", ""),
            })

        logger.info(
            f"Publishing {decision.event_type} for {primary_event.repo}#{primary_event.issue_id} "
            f"(coalesced {len(events)} events, reason: {decision.reason})"
        )

        await event_bus.publish(decision.event_type, event_payload)


class ProcessingDecision:
    """Result of analyzing a sequence of webhook events."""

    def __init__(
        self,
        should_trigger: bool,
        event_type: EventType | None = None,
        reason: str = "",
        labels: list[str] | None = None,
    ):
        self.should_trigger = should_trigger
        self.event_type = event_type
        self.reason = reason
        self.labels = labels


# Global instance
_webhook_processor: WebhookProcessor | None = None


def get_webhook_processor() -> WebhookProcessor:
    """Get the global webhook processor instance."""
    global _webhook_processor
    if _webhook_processor is None:
        _webhook_processor = WebhookProcessor()
    return _webhook_processor
