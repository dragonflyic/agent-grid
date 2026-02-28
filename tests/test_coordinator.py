"""Tests for coordinator module."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from agent_grid.coordinator import NudgeRequest


class TestNudgeRequest:
    """Tests for NudgeRequest model."""

    def test_create_nudge_request(self):
        """Test creating a nudge request."""
        nudge = NudgeRequest(
            id=uuid4(),
            issue_id="123",
            priority=5,
        )
        assert nudge.issue_id == "123"
        assert nudge.priority == 5
        assert nudge.processed_at is None

    def test_nudge_with_source_execution(self):
        """Test nudge with source execution."""
        source_id = uuid4()
        nudge = NudgeRequest(
            id=uuid4(),
            issue_id="456",
            source_execution_id=source_id,
        )
        assert nudge.source_execution_id == source_id


class TestScanner:
    """Tests for Scanner filtering logic."""

    def test_handled_labels_includes_epic(self):
        """ag/epic issues must not be re-scanned."""
        from agent_grid.coordinator.scanner import HANDLED_LABELS

        assert "ag/epic" in HANDLED_LABELS

    def test_handled_labels_excludes_sub_issue(self):
        """ag/sub-issue must NOT be in HANDLED_LABELS so sub-issues auto-launch."""
        from agent_grid.coordinator.scanner import HANDLED_LABELS

        assert "ag/sub-issue" not in HANDLED_LABELS

    def test_handled_labels_covers_all_non_todo_states(self):
        """Every ag/* label except ag/todo and ag/sub-issue should be in HANDLED_LABELS.

        ag/sub-issue is excluded so sub-issues created by the planner are
        automatically picked up by the scanner and launched.

        ag/proactive is in HANDLED_LABELS but NOT in AG_LABELS — it's an
        informational label that persists through transition_to() calls.
        """
        from agent_grid.coordinator.scanner import HANDLED_LABELS
        from agent_grid.issue_tracker.label_manager import AG_LABELS

        # ag/todo triggers processing, ag/sub-issue auto-launches after planning
        non_actionable = AG_LABELS - {"ag/todo", "ag/sub-issue"}
        # ag/proactive is in HANDLED_LABELS but not in AG_LABELS
        assert non_actionable == HANDLED_LABELS - {"ag/proactive"}


class TestDatabaseMethods:
    """Tests for Database method completeness."""

    def test_get_issue_id_for_execution_exists(self):
        """Database must have get_issue_id_for_execution — scheduler.py:124 calls it."""
        from agent_grid.coordinator.database import Database

        assert hasattr(Database, "get_issue_id_for_execution")

    def test_get_issue_id_for_execution_signature(self):
        """get_issue_id_for_execution must accept execution_id parameter."""
        import inspect

        from agent_grid.coordinator.database import Database

        sig = inspect.signature(Database.get_issue_id_for_execution)
        params = list(sig.parameters.keys())
        assert "execution_id" in params


class TestBudgetManager:
    """Tests for BudgetManager logic."""

    def test_max_concurrent_default(self):
        """Test default max concurrent is set."""
        from agent_grid.config import settings

        assert settings.max_concurrent_executions > 0


class TestPRMonitorTimestamps:
    """Tests for PR monitor timestamp normalization."""

    def test_normalize_strips_z_suffix(self):
        """GitHub Z suffix should be stripped for comparison."""
        from agent_grid.coordinator.pr_monitor import _normalize_timestamp

        assert _normalize_timestamp("2026-02-14T15:35:22Z") == "2026-02-14T15:35:22"

    def test_normalize_strips_microseconds(self):
        """Python microseconds should be stripped for comparison."""
        from agent_grid.coordinator.pr_monitor import _normalize_timestamp

        assert _normalize_timestamp("2026-02-14T15:30:00.123456") == "2026-02-14T15:30:00"

    def test_normalized_timestamps_compare_correctly(self):
        """Normalized timestamps should compare correctly regardless of source format."""
        from agent_grid.coordinator.pr_monitor import _normalize_timestamp

        github_ts = "2026-02-14T15:35:22Z"
        python_ts = "2026-02-14T15:30:00.123456"
        assert _normalize_timestamp(github_ts) > _normalize_timestamp(python_ts)

    def test_normalize_empty_string(self):
        """Empty string should return empty string."""
        from agent_grid.coordinator.pr_monitor import _normalize_timestamp

        assert _normalize_timestamp("") == ""


class TestScheduler:
    """Tests for Scheduler logic."""

    def test_should_auto_launch_with_ag_label(self):
        """Test auto-launch detection with ag/ label."""
        from agent_grid.coordinator.scheduler import Scheduler

        scheduler = Scheduler()
        assert scheduler._should_auto_launch(["ag/todo"]) is True
        assert scheduler._should_auto_launch(["ag/in-progress"]) is True
        assert scheduler._should_auto_launch(["ag/sub-issue"]) is True

    def test_should_not_auto_launch_without_label(self):
        """Test no auto-launch without trigger labels."""
        from agent_grid.coordinator.scheduler import Scheduler

        scheduler = Scheduler()
        assert scheduler._should_auto_launch([]) is False
        assert scheduler._should_auto_launch(["bug", "enhancement"]) is False

    def test_extract_repo_from_url(self):
        """Test repository extraction from URL."""
        from agent_grid.coordinator.scheduler import Scheduler

        scheduler = Scheduler()
        assert scheduler._extract_repo_from_url("https://github.com/owner/repo.git") == "owner/repo"
        assert scheduler._extract_repo_from_url("https://github.com/owner/repo") == "owner/repo"

    def test_prompt_builder(self):
        """Test prompt generation via prompt_builder."""
        from agent_grid.coordinator.prompt_builder import build_prompt
        from agent_grid.issue_tracker.public_api import IssueInfo

        issue = IssueInfo(
            id="42",
            number=42,
            title="Fix bug",
            body="The app crashes on startup",
            labels=[],
            repo_url="https://github.com/owner/repo",
            html_url="https://github.com/owner/repo/issues/42",
        )
        prompt = build_prompt(issue, "owner/repo", mode="implement")
        assert "Fix bug" in prompt
        assert "crashes on startup" in prompt


class TestCheckRunWebhook:
    """Tests for _handle_check_run_event webhook handler."""

    @pytest.mark.asyncio
    @patch("agent_grid.issue_tracker.webhook_handler.event_bus")
    async def test_check_run_with_pull_requests(self, mock_event_bus):
        """check_run with pull_requests populated uses PR head ref, sha, and number."""
        from agent_grid.execution_grid import EventType
        from agent_grid.issue_tracker.webhook_handler import _handle_check_run_event

        mock_event_bus.publish = AsyncMock()

        data = {
            "action": "completed",
            "check_run": {
                "conclusion": "failure",
                "name": "ci-test",
                "id": 111,
                "html_url": "https://github.com/owner/repo/runs/111",
                "output": {},
                "pull_requests": [
                    {
                        "number": 99,
                        "head": {"ref": "agent/42", "sha": "abc123"},
                    }
                ],
            },
            "repository": {"full_name": "owner/repo"},
        }

        await _handle_check_run_event(data)

        mock_event_bus.publish.assert_called_once()
        call_args = mock_event_bus.publish.call_args
        assert call_args[0][0] == EventType.CHECK_RUN_FAILED
        payload = call_args[0][1]
        assert payload["branch"] == "agent/42"
        assert payload["head_sha"] == "abc123"
        assert payload["pr_number"] == 99
        assert payload["repo"] == "owner/repo"

    @pytest.mark.asyncio
    @patch("agent_grid.issue_tracker.webhook_handler.event_bus")
    async def test_check_run_fallback_to_check_suite(self, mock_event_bus):
        """check_run with empty pull_requests falls back to check_suite.head_branch."""
        from agent_grid.execution_grid import EventType
        from agent_grid.issue_tracker.webhook_handler import _handle_check_run_event

        mock_event_bus.publish = AsyncMock()

        data = {
            "action": "completed",
            "check_run": {
                "conclusion": "failure",
                "name": "ci-test",
                "id": 222,
                "html_url": "https://github.com/owner/repo/runs/222",
                "head_sha": "abc123",
                "output": {},
                "pull_requests": [],
                "check_suite": {"head_branch": "agent/42"},
            },
            "repository": {"full_name": "owner/repo"},
        }

        await _handle_check_run_event(data)

        mock_event_bus.publish.assert_called_once()
        call_args = mock_event_bus.publish.call_args
        assert call_args[0][0] == EventType.CHECK_RUN_FAILED
        payload = call_args[0][1]
        assert payload["branch"] == "agent/42"
        assert payload["head_sha"] == "abc123"
        assert payload["pr_number"] is None

    @pytest.mark.asyncio
    @patch("agent_grid.issue_tracker.webhook_handler.event_bus")
    async def test_check_run_non_agent_branch_dropped(self, mock_event_bus):
        """check_run on a non-agent branch (e.g. main) should not publish."""
        from agent_grid.issue_tracker.webhook_handler import _handle_check_run_event

        mock_event_bus.publish = AsyncMock()

        data = {
            "action": "completed",
            "check_run": {
                "conclusion": "failure",
                "name": "ci-test",
                "id": 333,
                "html_url": "https://github.com/owner/repo/runs/333",
                "head_sha": "def456",
                "output": {},
                "pull_requests": [],
                "check_suite": {"head_branch": "main"},
            },
            "repository": {"full_name": "owner/repo"},
        }

        await _handle_check_run_event(data)

        mock_event_bus.publish.assert_not_called()

    @pytest.mark.asyncio
    @patch("agent_grid.issue_tracker.webhook_handler.event_bus")
    async def test_check_run_success_conclusion_dropped(self, mock_event_bus):
        """check_run with conclusion=success should not publish."""
        from agent_grid.issue_tracker.webhook_handler import _handle_check_run_event

        mock_event_bus.publish = AsyncMock()

        data = {
            "action": "completed",
            "check_run": {
                "conclusion": "success",
                "name": "ci-test",
                "id": 444,
                "html_url": "https://github.com/owner/repo/runs/444",
                "head_sha": "ghi789",
                "output": {},
                "pull_requests": [
                    {
                        "number": 10,
                        "head": {"ref": "agent/7", "sha": "ghi789"},
                    }
                ],
            },
            "repository": {"full_name": "owner/repo"},
        }

        await _handle_check_run_event(data)

        mock_event_bus.publish.assert_not_called()
