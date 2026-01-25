"""Tests for execution grid module."""

import pytest
from uuid import uuid4

from agent_grid.execution_grid import AgentExecution, ExecutionStatus
from agent_grid.execution_grid.repo_manager import RepoManager
from agent_grid.execution_grid.event_publisher import ExecutionEventPublisher


class TestRepoManager:
    """Tests for RepoManager."""

    @pytest.fixture
    def repo_manager(self, tmp_path):
        """Create a repo manager with temp directory."""
        return RepoManager(base_path=str(tmp_path))

    def test_get_execution_path(self, repo_manager):
        """Test execution path generation."""
        execution_id = uuid4()
        path = repo_manager.get_execution_path(execution_id)
        assert str(execution_id) in str(path)

    @pytest.mark.asyncio
    async def test_cleanup(self, repo_manager):
        """Test cleanup of execution directory."""
        execution_id = uuid4()
        path = repo_manager.get_execution_path(execution_id)
        path.mkdir(parents=True)
        assert path.exists()

        await repo_manager.cleanup(execution_id)
        assert not path.exists()


class TestExecutionEventPublisher:
    """Tests for ExecutionEventPublisher."""

    @pytest.fixture
    def publisher(self):
        """Create an event publisher."""
        return ExecutionEventPublisher()

    @pytest.mark.asyncio
    async def test_agent_started(self, publisher):
        """Test agent started event publishing."""
        # This should not raise
        await publisher.agent_started(
            execution_id=uuid4(),
            issue_id="123",
            repo_url="https://github.com/test/repo.git",
        )

    @pytest.mark.asyncio
    async def test_agent_completed(self, publisher):
        """Test agent completed event publishing."""
        await publisher.agent_completed(
            execution_id=uuid4(),
            result="Success",
        )

    @pytest.mark.asyncio
    async def test_agent_failed(self, publisher):
        """Test agent failed event publishing."""
        await publisher.agent_failed(
            execution_id=uuid4(),
            error="Test error",
        )


class TestAgentExecution:
    """Tests for AgentExecution model."""

    def test_create_execution(self):
        """Test creating an execution."""
        execution = AgentExecution(
            id=uuid4(),
            issue_id="123",
            repo_url="https://github.com/test/repo.git",
        )
        assert execution.status == ExecutionStatus.PENDING
        assert execution.started_at is None
        assert execution.completed_at is None

    def test_execution_status_transitions(self):
        """Test execution status can be updated."""
        execution = AgentExecution(
            id=uuid4(),
            issue_id="123",
            repo_url="https://github.com/test/repo.git",
        )
        execution.status = ExecutionStatus.RUNNING
        assert execution.status == ExecutionStatus.RUNNING

        execution.status = ExecutionStatus.COMPLETED
        assert execution.status == ExecutionStatus.COMPLETED
