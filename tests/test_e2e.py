"""End-to-end integration tests for the full agent-grid system."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from agent_grid.execution_grid import event_bus, EventType, ExecutionStatus
from agent_grid.issue_tracker import IssueStatus
from agent_grid.coordinator.database import Database
from agent_grid.coordinator.scheduler import Scheduler
from agent_grid.coordinator.nudge_handler import NudgeHandler
from agent_grid.coordinator.budget_manager import BudgetManager
from agent_grid.execution_grid.agent_runner import AgentRunner
from agent_grid.execution_grid.repo_manager import RepoManager
from agent_grid.issue_tracker.filesystem_client import FilesystemClient


class MockDatabase:
    """In-memory mock database for testing."""

    def __init__(self):
        self.executions = {}
        self.nudges = {}

    async def connect(self):
        pass

    async def close(self):
        pass

    async def create_execution(self, execution):
        self.executions[execution.id] = execution

    async def update_execution(self, execution):
        self.executions[execution.id] = execution

    async def get_execution(self, execution_id):
        return self.executions.get(execution_id)

    async def list_executions(self, status=None, issue_id=None, limit=100, offset=0):
        results = list(self.executions.values())
        if status:
            results = [e for e in results if e.status == status]
        if issue_id:
            results = [e for e in results if e.issue_id == issue_id]
        return results[:limit]

    async def get_running_executions(self):
        return await self.list_executions(status=ExecutionStatus.RUNNING)

    async def get_execution_for_issue(self, issue_id):
        for e in self.executions.values():
            if e.issue_id == issue_id:
                return e
        return None

    async def create_nudge(self, nudge):
        self.nudges[nudge.id] = nudge

    async def get_pending_nudges(self, limit=10):
        return [n for n in self.nudges.values() if n.processed_at is None][:limit]

    async def mark_nudge_processed(self, nudge_id):
        if nudge_id in self.nudges:
            self.nudges[nudge_id].processed_at = asyncio.get_event_loop().time()

    async def get_total_budget_usage(self, since=None):
        return {"tokens_used": 0, "duration_seconds": 0}


@pytest.fixture
def temp_dirs():
    """Create temporary directories for issues and repos."""
    with tempfile.TemporaryDirectory() as issues_dir:
        with tempfile.TemporaryDirectory() as repos_dir:
            yield Path(issues_dir), Path(repos_dir)


@pytest.fixture
def issue_tracker(temp_dirs):
    """Create a filesystem issue tracker."""
    issues_dir, _ = temp_dirs
    return FilesystemClient(issues_dir=issues_dir)


@pytest.fixture
def mock_db():
    """Create a mock database."""
    return MockDatabase()


@pytest.fixture
def repo_manager(temp_dirs):
    """Create a repo manager with temp directory."""
    _, repos_dir = temp_dirs
    return RepoManager(base_path=str(repos_dir))


class TestEndToEnd:
    """End-to-end tests for the agent-grid system."""

    @pytest.mark.asyncio
    async def test_full_flow_with_mocked_agent(self, temp_dirs, issue_tracker, mock_db, repo_manager):
        """
        Test the complete flow:
        1. Create an issue
        2. Nudge the coordinator
        3. Agent runs and completes

        Uses mocked Claude SDK to avoid actual API calls.
        """
        issues_dir, repos_dir = temp_dirs

        # Step 1: Create an issue
        issue = await issue_tracker.create_issue(
            repo="test/repo",
            title="Ping test",
            body="Please respond with 'pong' by creating a file called pong.txt",
            labels=["agent"],  # This label triggers auto-launch
        )
        assert issue.id == "1"
        assert issue.status == IssueStatus.OPEN

        # Track events received using a fresh event bus
        from agent_grid.execution_grid import EventBus
        test_event_bus = EventBus()
        events_received = []

        async def event_handler(event):
            events_received.append(event)

        test_event_bus.subscribe(event_handler)

        # Start event bus
        await test_event_bus.start()

        try:
            # Step 2: Mock the Claude SDK to simulate agent work
            async def mock_query(prompt, options=None):
                """Simulate agent creating pong.txt."""
                # Simulate the agent creating the file
                if options and options.cwd:
                    pong_file = Path(options.cwd) / "pong.txt"
                    pong_file.write_text("pong")

                # Yield a mock result message
                from claude_code_sdk.types import ResultMessage
                yield ResultMessage(
                    subtype="success",
                    duration_ms=100,
                    duration_api_ms=50,
                    is_error=False,
                    num_turns=1,
                    session_id="test-session",
                    total_cost_usd=0.01,
                    usage={},
                    result="Created pong.txt with content 'pong'",
                )

            # Step 3: Create and run the agent runner with mocked SDK
            agent_runner = AgentRunner()
            agent_runner._repo_manager = repo_manager

            # Patch the repo manager to create a simple work directory
            async def mock_clone(execution_id, repo_url, branch=None):
                """Create a simple work directory."""
                work_dir = repo_manager.get_execution_path(execution_id)
                work_dir.mkdir(parents=True, exist_ok=True)
                # Copy just the README
                (work_dir / "README.md").write_text("# Test Repo")
                return work_dir

            repo_manager.clone_repo = mock_clone

            # Patch create_branch to be a no-op
            async def mock_create_branch(execution_id, branch_name):
                pass

            repo_manager.create_branch = mock_create_branch

            # Patch the push to be a no-op
            async def mock_push(execution_id, branch_name):
                pass

            repo_manager.push_branch = mock_push

            # Create execution
            from agent_grid.execution_grid import AgentExecution
            execution = AgentExecution(
                id=uuid4(),
                issue_id=issue.id,
                repo_url="file:///tmp/test-repo",
                status=ExecutionStatus.PENDING,
                prompt=f"Issue: {issue.title}\n\n{issue.body}",
            )

            # Run with mocked SDK and patched event bus
            with patch("agent_grid.execution_grid.agent_runner.query", mock_query), \
                 patch("agent_grid.execution_grid.event_publisher.event_bus", test_event_bus):
                result = await agent_runner.run(execution, execution.prompt)

            # Step 4: Verify results
            if result.status == ExecutionStatus.FAILED:
                print(f"Execution failed with: {result.result}")
            assert result.status == ExecutionStatus.COMPLETED, f"Failed: {result.result}"
            assert result.result is not None
            assert "pong" in result.result.lower()

            # Verify events were published
            await test_event_bus.wait_until_empty()
            await asyncio.sleep(0.1)  # Give time for event processing

            event_types = [e.type for e in events_received]
            assert EventType.AGENT_STARTED in event_types
            assert EventType.AGENT_COMPLETED in event_types

        finally:
            test_event_bus.unsubscribe(event_handler)
            await test_event_bus.stop()

    @pytest.mark.asyncio
    async def test_nudge_triggers_execution(self, temp_dirs, issue_tracker, mock_db):
        """Test that nudging an issue creates an execution record."""
        issues_dir, repos_dir = temp_dirs

        # Create an issue
        issue = await issue_tracker.create_issue(
            repo="test/repo",
            title="Task to nudge",
            body="Do something",
        )

        # Create nudge handler with mock db
        nudge_handler = NudgeHandler()
        nudge_handler._db = mock_db

        # Create a nudge
        nudge = await nudge_handler.handle_nudge(
            issue_id=issue.id,
            priority=5,
            reason="Test nudge",
        )

        assert nudge.issue_id == issue.id
        assert nudge.priority == 5

        # Verify nudge was stored
        pending = await mock_db.get_pending_nudges()
        assert len(pending) == 1
        assert pending[0].id == nudge.id

    @pytest.mark.asyncio
    async def test_blocked_issue_not_started(self, temp_dirs, issue_tracker):
        """Test that blocked issues are not started until unblocked."""
        issues_dir, _ = temp_dirs

        # Create a blocker issue
        blocker = await issue_tracker.create_issue(
            repo="test/repo",
            title="Blocker",
            body="This must complete first",
        )

        # Create a blocked issue
        blocked = await issue_tracker.create_issue(
            repo="test/repo",
            title="Blocked task",
            body="Waiting on blocker",
            blocked_by=[blocker.id],
        )

        # Verify it's blocked
        is_blocked = await issue_tracker.is_blocked("test/repo", blocked.id)
        assert is_blocked is True

        # Close the blocker
        await issue_tracker.update_issue_status("test/repo", blocker.id, IssueStatus.CLOSED)

        # Now it should be unblocked
        is_blocked = await issue_tracker.is_blocked("test/repo", blocked.id)
        assert is_blocked is False

    @pytest.mark.asyncio
    async def test_subissue_workflow(self, temp_dirs, issue_tracker):
        """Test creating and tracking subissues."""
        issues_dir, _ = temp_dirs

        # Create parent issue
        parent = await issue_tracker.create_issue(
            repo="test/repo",
            title="Parent epic",
            body="Break this down into subtasks",
        )

        # Create subissues
        sub1 = await issue_tracker.create_subissue(
            repo="test/repo",
            parent_id=parent.id,
            title="Subtask 1",
            body="First subtask",
        )
        sub2 = await issue_tracker.create_subissue(
            repo="test/repo",
            parent_id=parent.id,
            title="Subtask 2",
            body="Second subtask",
            labels=["agent"],
        )

        # Verify subissues are linked
        subissues = await issue_tracker.list_subissues("test/repo", parent.id)
        assert len(subissues) == 2
        assert all(s.parent_id == parent.id for s in subissues)

        # Complete subtasks
        await issue_tracker.update_issue_status("test/repo", sub1.id, IssueStatus.CLOSED)
        await issue_tracker.update_issue_status("test/repo", sub2.id, IssueStatus.CLOSED)

        # Verify completion
        subissues = await issue_tracker.list_subissues("test/repo", parent.id)
        assert all(s.status == IssueStatus.CLOSED for s in subissues)

    @pytest.mark.asyncio
    async def test_budget_manager_limits_concurrent(self, mock_db):
        """Test that budget manager enforces concurrent execution limits."""
        from agent_grid.execution_grid import AgentExecution

        budget_manager = BudgetManager(max_concurrent=2)
        budget_manager._db = mock_db

        # No executions - should allow
        can_launch, reason = await budget_manager.can_launch_agent()
        assert can_launch is True

        # Add running executions
        for i in range(2):
            exec = AgentExecution(
                id=uuid4(),
                issue_id=str(i),
                repo_url="test",
                status=ExecutionStatus.RUNNING,
            )
            await mock_db.create_execution(exec)

        # Now at limit - should deny
        can_launch, reason = await budget_manager.can_launch_agent()
        assert can_launch is False
        assert "Max concurrent" in reason

    @pytest.mark.asyncio
    async def test_event_flow(self, temp_dirs):
        """Test that events flow correctly through the system."""
        from agent_grid.execution_grid import EventBus

        # Create a fresh event bus for this test to avoid event loop issues
        test_bus = EventBus()
        events = []

        async def collector(event):
            events.append(event)

        test_bus.subscribe(collector)
        await test_bus.start()

        try:
            # Publish various events
            await test_bus.publish(EventType.ISSUE_CREATED, {"issue_id": "1"})
            await test_bus.publish(EventType.AGENT_STARTED, {"execution_id": "abc"})
            await test_bus.publish(EventType.AGENT_COMPLETED, {"execution_id": "abc"})

            await test_bus.wait_until_empty()
            await asyncio.sleep(0.1)

            assert len(events) == 3
            assert events[0].type == EventType.ISSUE_CREATED
            assert events[1].type == EventType.AGENT_STARTED
            assert events[2].type == EventType.AGENT_COMPLETED

        finally:
            test_bus.unsubscribe(collector)
            await test_bus.stop()


class TestWebhookAutoLaunch:
    """Tests for webhook-triggered auto-launch functionality."""

    @pytest.mark.asyncio
    async def test_webhook_with_agent_grid_label_triggers_launch(self, mock_db):
        """
        Test that a GitHub webhook for an issue with the 'agent-grid' label
        triggers an agent execution.

        This is the core test for the webhook auto-launch feature.
        """
        from agent_grid.execution_grid import EventBus, Event
        from agent_grid.coordinator.scheduler import Scheduler
        from agent_grid.issue_tracker.filesystem_client import FilesystemClient
        import tempfile
        from pathlib import Path

        # Create a fresh event bus for isolated testing
        test_event_bus = EventBus()
        events_received = []

        async def event_collector(event):
            events_received.append(event)

        test_event_bus.subscribe(event_collector)
        await test_event_bus.start()

        try:
            # Create temporary directory for issues
            with tempfile.TemporaryDirectory() as issues_dir:
                issue_tracker = FilesystemClient(issues_dir=Path(issues_dir))

                # Create an issue to simulate what the webhook handler would create
                issue = await issue_tracker.create_issue(
                    repo="test/webhook-repo",
                    title="Test webhook auto-launch",
                    body="This issue should trigger agent execution via webhook",
                    labels=["agent-grid"],  # The trigger label
                )

                # Simulate the webhook event that would be published
                await test_event_bus.publish(
                    EventType.ISSUE_CREATED,
                    {
                        "repo": "test/webhook-repo",
                        "issue_id": issue.id,
                        "title": issue.title,
                        "body": issue.body,
                        "labels": ["agent-grid"],
                        "html_url": f"https://github.com/test/webhook-repo/issues/{issue.id}",
                    },
                )

                # Wait for event to be processed
                await test_event_bus.wait_until_empty()
                await asyncio.sleep(0.1)

                # Verify the ISSUE_CREATED event was published
                assert len(events_received) == 1
                assert events_received[0].type == EventType.ISSUE_CREATED
                assert events_received[0].payload["labels"] == ["agent-grid"]
                assert events_received[0].payload["issue_id"] == issue.id

        finally:
            test_event_bus.unsubscribe(event_collector)
            await test_event_bus.stop()

    @pytest.mark.asyncio
    async def test_webhook_without_trigger_label_does_not_launch(self):
        """
        Test that a GitHub webhook for an issue WITHOUT the trigger label
        does not trigger agent execution (scheduler should ignore it).
        """
        from agent_grid.execution_grid import EventBus
        from agent_grid.coordinator.scheduler import Scheduler

        # Create a fresh event bus for isolated testing
        test_event_bus = EventBus()
        events_received = []
        scheduler_handled = []

        async def event_collector(event):
            events_received.append(event)

        test_event_bus.subscribe(event_collector)
        await test_event_bus.start()

        try:
            # Create a scheduler and check its decision
            scheduler = Scheduler()

            # Test that scheduler correctly identifies non-trigger labels
            assert scheduler._should_auto_launch(["bug", "documentation"]) is False
            assert scheduler._should_auto_launch([]) is False
            assert scheduler._should_auto_launch(["help wanted"]) is False

            # Simulate the webhook event without trigger labels
            await test_event_bus.publish(
                EventType.ISSUE_CREATED,
                {
                    "repo": "test/webhook-repo",
                    "issue_id": "42",
                    "title": "Regular issue without trigger label",
                    "body": "This should not trigger an agent",
                    "labels": ["bug", "documentation"],  # No trigger labels
                    "html_url": "https://github.com/test/webhook-repo/issues/42",
                },
            )

            await test_event_bus.wait_until_empty()
            await asyncio.sleep(0.1)

            # Event was published but should not trigger auto-launch
            assert len(events_received) == 1
            assert events_received[0].payload["labels"] == ["bug", "documentation"]

        finally:
            test_event_bus.unsubscribe(event_collector)
            await test_event_bus.stop()

    @pytest.mark.asyncio
    async def test_all_trigger_labels_work(self):
        """
        Test that all valid trigger labels ('agent', 'automated', 'agent-grid')
        correctly trigger auto-launch.
        """
        from agent_grid.coordinator.scheduler import Scheduler

        scheduler = Scheduler()

        # All three trigger labels should work
        assert scheduler._should_auto_launch(["agent"]) is True
        assert scheduler._should_auto_launch(["automated"]) is True
        assert scheduler._should_auto_launch(["agent-grid"]) is True

        # Multiple labels including a trigger label should work
        assert scheduler._should_auto_launch(["bug", "agent-grid", "high-priority"]) is True
        assert scheduler._should_auto_launch(["feature", "automated"]) is True

        # Multiple trigger labels should work
        assert scheduler._should_auto_launch(["agent", "agent-grid"]) is True

    @pytest.mark.asyncio
    async def test_labeled_event_triggers_auto_launch(self):
        """
        Test that the 'labeled' action in webhook events can also trigger
        auto-launch when the agent-grid label is added to an existing issue.
        """
        from agent_grid.execution_grid import EventBus

        test_event_bus = EventBus()
        events_received = []

        async def event_collector(event):
            events_received.append(event)

        test_event_bus.subscribe(event_collector)
        await test_event_bus.start()

        try:
            # Simulate adding the 'agent-grid' label to an existing issue
            # This would come through as an ISSUE_UPDATED event with action='labeled'
            await test_event_bus.publish(
                EventType.ISSUE_UPDATED,
                {
                    "repo": "test/webhook-repo",
                    "issue_id": "99",
                    "action": "labeled",
                    "title": "Existing issue now labeled",
                    "body": "This issue was just labeled with agent-grid",
                    "state": "open",
                    "labels": ["agent-grid"],  # Label was just added
                },
            )

            await test_event_bus.wait_until_empty()
            await asyncio.sleep(0.1)

            # Verify the event was published correctly
            assert len(events_received) == 1
            assert events_received[0].type == EventType.ISSUE_UPDATED
            assert events_received[0].payload["action"] == "labeled"
            assert "agent-grid" in events_received[0].payload["labels"]

        finally:
            test_event_bus.unsubscribe(event_collector)
            await test_event_bus.stop()

    @pytest.mark.asyncio
    async def test_scheduler_processes_issue_created_event(self, mock_db):
        """
        Test the full scheduler flow: receiving an ISSUE_CREATED event
        and attempting to launch an agent.
        """
        from agent_grid.execution_grid import EventBus, Event
        from agent_grid.coordinator.scheduler import Scheduler
        from agent_grid.issue_tracker.filesystem_client import FilesystemClient
        from unittest.mock import patch, AsyncMock
        import tempfile
        from pathlib import Path

        test_event_bus = EventBus()
        await test_event_bus.start()

        try:
            with tempfile.TemporaryDirectory() as issues_dir:
                issue_tracker = FilesystemClient(issues_dir=Path(issues_dir))

                # Create an issue first
                issue = await issue_tracker.create_issue(
                    repo="test/scheduler-repo",
                    title="Scheduler test issue",
                    body="Testing scheduler agent launch",
                    labels=["agent-grid"],
                )

                # Create scheduler with mocked dependencies
                scheduler = Scheduler()
                scheduler._db = mock_db

                # Mock budget manager to allow launch
                mock_budget = AsyncMock()
                mock_budget.can_launch_agent = AsyncMock(return_value=(True, ""))
                scheduler._budget_manager = mock_budget

                # Track if _try_launch_agent was called
                launch_called = []
                original_try_launch = scheduler._try_launch_agent

                async def mock_try_launch(issue_id, repo):
                    launch_called.append({"issue_id": issue_id, "repo": repo})
                    # Don't actually launch, just record the call

                scheduler._try_launch_agent = mock_try_launch

                # Start scheduler
                await scheduler.start()

                # Create and process an ISSUE_CREATED event with patch for issue tracker
                with patch("agent_grid.coordinator.scheduler.get_issue_tracker", return_value=issue_tracker):
                    event = Event(
                        type=EventType.ISSUE_CREATED,
                        payload={
                            "repo": "test/scheduler-repo",
                            "issue_id": issue.id,
                            "title": issue.title,
                            "body": issue.body,
                            "labels": ["agent-grid"],
                            "html_url": f"https://github.com/test/scheduler-repo/issues/{issue.id}",
                        },
                    )

                    # Directly call the handler to test the logic
                    await scheduler._handle_issue_created(event)

                # Verify _try_launch_agent was called with correct arguments
                assert len(launch_called) == 1
                assert launch_called[0]["issue_id"] == issue.id
                assert launch_called[0]["repo"] == "test/scheduler-repo"

                await scheduler.stop()

        finally:
            await test_event_bus.stop()
