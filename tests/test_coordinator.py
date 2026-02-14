"""Tests for coordinator module."""

from uuid import uuid4

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

    def test_handled_labels_includes_sub_issue(self):
        """ag/sub-issue issues must not be re-scanned."""
        from agent_grid.coordinator.scanner import HANDLED_LABELS

        assert "ag/sub-issue" in HANDLED_LABELS

    def test_handled_labels_covers_all_non_todo_states(self):
        """Every ag/* label except ag/todo should be in HANDLED_LABELS."""
        from agent_grid.coordinator.scanner import HANDLED_LABELS
        from agent_grid.issue_tracker.label_manager import AG_LABELS

        # ag/todo is the only label that should trigger processing
        non_actionable = AG_LABELS - {"ag/todo"}
        assert non_actionable == HANDLED_LABELS


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
