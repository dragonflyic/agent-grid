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


class TestBudgetManager:
    """Tests for BudgetManager logic."""

    def test_max_concurrent_default(self):
        """Test default max concurrent is set."""
        from agent_grid.config import settings

        assert settings.max_concurrent_executions > 0


class TestScheduler:
    """Tests for Scheduler logic."""

    def test_should_auto_launch_with_agent_label(self):
        """Test auto-launch detection with agent label."""
        from agent_grid.coordinator.scheduler import Scheduler

        scheduler = Scheduler()
        assert scheduler._should_auto_launch(["agent"]) is True
        assert scheduler._should_auto_launch(["automated"]) is True
        assert scheduler._should_auto_launch(["agent-grid"]) is True

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

    def test_generate_prompt(self):
        """Test prompt generation."""
        from agent_grid.coordinator.scheduler import Scheduler

        scheduler = Scheduler()
        prompt = scheduler._generate_prompt("Fix bug", "The app crashes on startup", "42", "owner/repo")
        assert "Fix bug" in prompt
        assert "crashes on startup" in prompt
        assert "#42" in prompt
        assert "agent/42" in prompt  # Branch name should be included
