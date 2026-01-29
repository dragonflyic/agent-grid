"""Tests for webhook deduplication processor."""

import json
import pytest
from datetime import timedelta
from uuid import uuid4

from agent_grid.coordinator.public_api import WebhookEvent, utc_now
from agent_grid.coordinator.webhook_processor import WebhookProcessor, ProcessingDecision
from agent_grid.execution_grid import EventType


class TestProcessingDecision:
    """Tests for ProcessingDecision model."""

    def test_should_trigger_false(self):
        """Test decision with no trigger."""
        decision = ProcessingDecision(should_trigger=False, reason="no events")
        assert decision.should_trigger is False
        assert decision.event_type is None

    def test_should_trigger_true(self):
        """Test decision with trigger."""
        decision = ProcessingDecision(
            should_trigger=True,
            event_type=EventType.ISSUE_CREATED,
            reason="issue opened with trigger label",
            labels=["agent"],
        )
        assert decision.should_trigger is True
        assert decision.event_type == EventType.ISSUE_CREATED
        assert "agent" in decision.labels


class TestWebhookProcessorAnalysis:
    """Tests for WebhookProcessor event analysis logic."""

    def create_event(
        self,
        event_type: str = "issues",
        action: str = "opened",
        labels: list[str] | None = None,
        include_issue_data: bool = True,
    ) -> WebhookEvent:
        """Helper to create test webhook events."""
        payload = {"action": action}
        if include_issue_data:
            payload["issue"] = {
                "number": 123,
                "title": "Test Issue",
                "body": "Test body",
                "state": "open",
                "labels": [{"name": label} for label in (labels or [])],
            }
        return WebhookEvent(
            id=uuid4(),
            delivery_id=str(uuid4()),
            event_type=event_type,
            action=action,
            repo="owner/repo",
            issue_id="123",
            payload=json.dumps(payload),
            received_at=utc_now(),
        )

    def test_issue_opened_with_trigger_label(self):
        """Test: issue opened with agent label → should trigger."""
        processor = WebhookProcessor()
        events = [self.create_event(action="opened", labels=["agent"])]

        decision = processor._analyze_event_sequence(events)

        assert decision.should_trigger is True
        assert decision.event_type == EventType.ISSUE_CREATED
        assert "agent" in decision.labels

    def test_issue_opened_without_trigger_label(self):
        """Test: issue opened without trigger label → should not trigger."""
        processor = WebhookProcessor()
        events = [self.create_event(action="opened", labels=["bug"])]

        decision = processor._analyze_event_sequence(events)

        assert decision.should_trigger is False
        assert "without trigger label" in decision.reason

    def test_issue_opened_then_labeled(self):
        """Test: issue opened, then labeled with agent → should trigger."""
        processor = WebhookProcessor()
        events = [
            self.create_event(action="opened", labels=[]),
            self.create_event(action="labeled", labels=["agent"]),
        ]

        decision = processor._analyze_event_sequence(events)

        assert decision.should_trigger is True
        assert decision.event_type == EventType.ISSUE_CREATED

    def test_issue_opened_then_closed(self):
        """Test: issue opened, then closed → should NOT trigger."""
        processor = WebhookProcessor()
        events = [
            self.create_event(action="opened", labels=["agent"]),
            self.create_event(action="closed", labels=["agent"]),
        ]

        decision = processor._analyze_event_sequence(events)

        assert decision.should_trigger is False
        assert "closed" in decision.reason

    def test_issue_labeled_with_trigger(self):
        """Test: existing issue gets trigger label → should trigger."""
        processor = WebhookProcessor()
        events = [self.create_event(action="labeled", labels=["agent"])]

        decision = processor._analyze_event_sequence(events)

        assert decision.should_trigger is True
        assert decision.event_type == EventType.ISSUE_UPDATED

    def test_nudge_in_comment(self):
        """Test: comment with nudge command → should trigger."""
        processor = WebhookProcessor()
        payload = {
            "action": "created",
            "issue": {"number": 123},
            "comment": {"body": "Please @agent-grid nudge this issue"},
        }
        events = [
            WebhookEvent(
                id=uuid4(),
                delivery_id=str(uuid4()),
                event_type="issue_comment",
                action="created",
                repo="owner/repo",
                issue_id="123",
                payload=json.dumps(payload),
                received_at=utc_now(),
            )
        ]

        decision = processor._analyze_event_sequence(events)

        assert decision.should_trigger is True
        assert decision.event_type == EventType.NUDGE_REQUESTED

    def test_empty_events(self):
        """Test: no events → should not trigger."""
        processor = WebhookProcessor()
        decision = processor._analyze_event_sequence([])

        assert decision.should_trigger is False
        assert decision.reason == "no events"

    def test_coalesced_events_opened_multiple_labeled(self):
        """Test: opened + multiple label events → single trigger with final labels."""
        processor = WebhookProcessor()
        events = [
            self.create_event(action="opened", labels=[]),
            self.create_event(action="labeled", labels=["bug"]),
            self.create_event(action="labeled", labels=["bug", "agent"]),
        ]

        decision = processor._analyze_event_sequence(events)

        assert decision.should_trigger is True
        assert decision.event_type == EventType.ISSUE_CREATED
        assert "agent" in decision.labels
        assert "bug" in decision.labels

    def test_automated_label_triggers(self):
        """Test: 'automated' label also triggers."""
        processor = WebhookProcessor()
        events = [self.create_event(action="opened", labels=["automated"])]

        decision = processor._analyze_event_sequence(events)

        assert decision.should_trigger is True

    def test_agent_grid_label_triggers(self):
        """Test: 'agent-grid' label also triggers."""
        processor = WebhookProcessor()
        events = [self.create_event(action="opened", labels=["agent-grid"])]

        decision = processor._analyze_event_sequence(events)

        assert decision.should_trigger is True


class TestWebhookEventModel:
    """Tests for WebhookEvent model."""

    def test_create_webhook_event(self):
        """Test creating a webhook event."""
        event = WebhookEvent(
            id=uuid4(),
            delivery_id="abc-123",
            event_type="issues",
            action="opened",
            repo="owner/repo",
            issue_id="42",
            payload='{"test": true}',
        )
        assert event.delivery_id == "abc-123"
        assert event.event_type == "issues"
        assert event.processed is False
        assert event.coalesced_into is None

    def test_webhook_event_defaults(self):
        """Test webhook event default values."""
        event = WebhookEvent(
            id=uuid4(),
            delivery_id="xyz-456",
            event_type="ping",
        )
        assert event.action is None
        assert event.repo is None
        assert event.issue_id is None
        assert event.payload is None
        assert event.processed is False


class TestWebhookProcessorConfig:
    """Tests for WebhookProcessor configuration."""

    def test_custom_quiet_period(self):
        """Test custom quiet period configuration."""
        processor = WebhookProcessor(quiet_period_seconds=10)
        assert processor._quiet_period == timedelta(seconds=10)

    def test_custom_poll_interval(self):
        """Test custom poll interval configuration."""
        processor = WebhookProcessor(poll_interval_seconds=5)
        assert processor._poll_interval == 5

    def test_default_config_from_settings(self):
        """Test default configuration from settings."""
        from agent_grid.config import settings

        processor = WebhookProcessor()
        assert processor._quiet_period == timedelta(
            seconds=settings.webhook_dedup_quiet_period_seconds
        )
        assert processor._poll_interval == settings.webhook_dedup_poll_interval_seconds
