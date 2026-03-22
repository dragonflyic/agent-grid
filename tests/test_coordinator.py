"""Tests for coordinator module."""

from datetime import datetime, timezone
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


class TestPlanModePrompt:
    """Tests for plan-mode prompt with native sub-issue linking."""

    def _make_issue(self, author=""):
        from agent_grid.issue_tracker.public_api import IssueInfo

        return IssueInfo(
            id="10",
            number=10,
            title="Complex task",
            body="Needs decomposition",
            labels=[],
            repo_url="https://github.com/owner/repo",
            html_url="https://github.com/owner/repo/issues/10",
            author=author,
        )

    def test_plan_prompt_includes_sub_issue_api_linking(self):
        """Plan prompt must instruct the agent to link via the sub_issues API."""
        from agent_grid.coordinator.prompt_builder import build_prompt

        prompt = build_prompt(self._make_issue(), "owner/repo", mode="plan")
        assert "sub_issues" in prompt

    def test_plan_prompt_includes_blocked_by_format(self):
        """Plan prompt must document the 'Blocked by:' format for dependency resolver."""
        from agent_grid.coordinator.prompt_builder import build_prompt

        prompt = build_prompt(self._make_issue(), "owner/repo", mode="plan")
        assert "Blocked by:" in prompt

    def test_plan_prompt_includes_author_when_present(self):
        """Plan prompt includes parent issue author when available."""
        from agent_grid.coordinator.prompt_builder import build_prompt

        prompt = build_prompt(self._make_issue(author="alice"), "owner/repo", mode="plan")
        assert "@alice" in prompt

    def test_plan_prompt_no_author_when_empty(self):
        """Plan prompt omits author line when author is empty."""
        from agent_grid.coordinator.prompt_builder import build_prompt

        prompt = build_prompt(self._make_issue(author=""), "owner/repo", mode="plan")
        assert "Parent issue author:" not in prompt

    def test_plan_prompt_includes_two_step_creation(self):
        """Plan prompt must show the two-step create-then-link process."""
        from agent_grid.coordinator.prompt_builder import build_prompt

        prompt = build_prompt(self._make_issue(), "owner/repo", mode="plan")
        # Step A: capture issue number
        assert "--json number --jq .number" in prompt
        # Step B: API link call
        assert "gh api --method POST" in prompt
        assert "repos/owner/repo/issues/10/sub_issues" in prompt

    def test_plan_prompt_includes_assignee_step(self):
        """Plan prompt must instruct the agent to assign sub-issues."""
        from agent_grid.coordinator.prompt_builder import build_prompt

        prompt = build_prompt(self._make_issue(author="bob"), "owner/repo", mode="plan")
        assert "--add-assignee bob" in prompt

    def test_plan_prompt_blocked_by_first_line_instruction(self):
        """Plan prompt must instruct that Blocked by: is the FIRST LINE of the body."""
        from agent_grid.coordinator.prompt_builder import build_prompt

        prompt = build_prompt(self._make_issue(), "owner/repo", mode="plan")
        assert "first line" in prompt.lower() or "FIRST LINE" in prompt

    def test_plan_prompt_does_not_break_other_modes(self):
        """Other mode prompts must remain unaffected by plan-mode changes."""
        from agent_grid.coordinator.prompt_builder import build_prompt

        issue = self._make_issue(author="alice")

        implement = build_prompt(issue, "owner/repo", mode="implement")
        assert "git checkout -b agent/10" in implement
        assert "sub_issues" not in implement

        review = build_prompt(
            issue,
            "owner/repo",
            mode="address_review",
            context={"pr_number": 5, "review_comments": "fix typo"},
        )
        assert "addressing review feedback" in review.lower()
        assert "sub_issues" not in review

        fix_ci = build_prompt(
            issue,
            "owner/repo",
            mode="fix_ci",
            context={"pr_number": 5, "check_name": "tests", "check_output": "fail"},
        )
        assert "CI check" in fix_ci
        assert "sub_issues" not in fix_ci

        retry = build_prompt(
            issue,
            "owner/repo",
            mode="retry_with_feedback",
            context={"closed_pr_number": 3, "human_feedback": "wrong approach"},
        )
        assert "previous attempt" in retry.lower()
        assert "sub_issues" not in retry


class TestPRCreationPrompt:
    """Tests for PR creation — coordinator creates PRs, not the agent."""

    def _make_issue(self, author=""):
        from agent_grid.issue_tracker.public_api import IssueInfo

        return IssueInfo(
            id="42",
            number=42,
            title="Fix bug",
            body="The app crashes",
            labels=[],
            repo_url="https://github.com/owner/repo",
            html_url="https://github.com/owner/repo/issues/42",
            author=author,
        )

    def test_implement_prompt_does_not_create_pr(self):
        """Agent should not run gh pr create — coordinator handles it."""
        from agent_grid.coordinator.prompt_builder import build_prompt

        prompt = build_prompt(self._make_issue(author="alice"), "owner/repo", mode="implement")
        assert "gh pr create" not in prompt
        assert "Do NOT create a PR" in prompt

    def test_implement_prompt_pushes_branch(self):
        """Agent should push the branch so coordinator can create the PR."""
        from agent_grid.coordinator.prompt_builder import build_prompt

        prompt = build_prompt(self._make_issue(), "owner/repo", mode="implement")
        assert "git push" in prompt

    def test_retry_prompt_does_not_create_pr(self):
        """Retry mode should also not create PR — coordinator handles it."""
        from agent_grid.coordinator.prompt_builder import build_prompt

        prompt = build_prompt(
            self._make_issue(author="bob"),
            "owner/repo",
            mode="retry_with_feedback",
            context={"closed_pr_number": 3, "human_feedback": "wrong"},
        )
        assert "gh pr create" not in prompt
        assert "Do NOT create a PR" in prompt


class TestPlannerBlockedBy:
    """Tests for planner auto-adding Blocked by: to sub-issue body."""

    @pytest.mark.asyncio
    async def test_blocked_by_added_from_depends_on(self):
        """Sub-issues with depends_on should have Blocked by: in body."""
        from agent_grid.coordinator.planner import Planner
        from agent_grid.issue_tracker.public_api import IssueInfo, IssueStatus

        # Build a fake planner that doesn't call the LLM
        planner = Planner.__new__(Planner)

        created = []
        assign_calls = []

        class FakeTracker:
            async def create_subissue(self, repo, parent_id, title, body, labels=None):
                issue = IssueInfo(
                    id=str(100 + len(created)),
                    number=100 + len(created),
                    title=title,
                    body=body,
                    status=IssueStatus.OPEN,
                    labels=labels or [],
                    repo_url=f"https://github.com/{repo}",
                    html_url=f"https://github.com/{repo}/issues/{100 + len(created)}",
                )
                created.append({"issue": issue, "body": body, "labels": labels})
                return issue

            async def add_comment(self, repo, issue_id, body):
                pass

            async def assign_issue(self, repo, issue_id, assignee):
                assign_calls.append((issue_id, assignee))

        class FakeLabels:
            async def transition_to(self, repo, issue_id, label):
                pass

        planner._tracker = FakeTracker()
        planner._labels = FakeLabels()
        planner._client = None  # Won't be used

        # Simulate what happens after LLM returns a plan
        import json

        plan = {
            "plan_summary": "Test plan",
            "sub_tasks": [
                {
                    "title": "Task A",
                    "description": "First task",
                    "acceptance_criteria": ["AC1"],
                    "depends_on": [],
                    "estimated_files": ["a.py"],
                },
                {
                    "title": "Task B",
                    "description": "Depends on A",
                    "acceptance_criteria": ["AC2"],
                    "depends_on": [0],
                    "estimated_files": ["b.py"],
                },
            ],
            "risks": [],
        }

        # Monkey-patch the LLM call to return our plan
        class FakeResponse:
            class Content:
                text = json.dumps(plan)

            content = [Content()]

        class FakeClient:
            class messages:
                @staticmethod
                async def create(**kwargs):
                    return FakeResponse()

        planner._client = FakeClient()

        from unittest.mock import patch

        mock_status_mgr = AsyncMock()
        with (
            patch("agent_grid.coordinator.planner.embed_metadata", side_effect=lambda text, meta: text),
            patch("agent_grid.coordinator.status_comment.get_status_comment_manager", return_value=mock_status_mgr),
        ):
            result = await planner.decompose("owner/repo", 1, "Parent", "Body")

        assert len(result) == 2

        # First sub-issue: no Blocked by
        first_body = created[0]["body"]
        assert not first_body.startswith("Blocked by:")

        # Second sub-issue: should have Blocked by: #100
        second_body = created[1]["body"]
        assert second_body.startswith("Blocked by: #100")

        # Second should have ag/waiting label
        assert "ag/waiting" in created[1]["labels"]


class TestCheckInProgressLabelTransition:
    """Tests that _check_in_progress transitions timed-out issues to ag/failed."""

    @pytest.mark.asyncio
    async def test_timeout_transitions_label_to_failed(self):
        """Timed-out execution must transition label to ag/failed and record event."""
        from agent_grid.coordinator.management_loop import ManagementLoop
        from agent_grid.execution_grid import AgentExecution, ExecutionStatus

        loop = ManagementLoop.__new__(ManagementLoop)

        exec_id = uuid4()
        old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        execution = AgentExecution(
            id=exec_id,
            repo_url="https://github.com/owner/repo",
            status=ExecutionStatus.RUNNING,
            prompt="test",
            mode="implement",
        )
        execution.started_at = old_time
        execution.created_at = old_time

        mock_db = AsyncMock()
        mock_db.get_running_executions = AsyncMock(return_value=[execution])
        mock_db.list_executions = AsyncMock(return_value=[])
        mock_db.update_execution = AsyncMock()
        mock_db.get_issue_id_for_execution = AsyncMock(return_value="42")
        mock_db.record_pipeline_event = AsyncMock()
        loop._db = mock_db

        mock_labels = AsyncMock()
        mock_grid = AsyncMock()

        with (
            patch("agent_grid.coordinator.management_loop.get_label_manager", return_value=mock_labels),
            patch("agent_grid.coordinator.management_loop.get_execution_grid", return_value=mock_grid),
            patch("agent_grid.coordinator.management_loop.settings") as mock_settings,
        ):
            mock_settings.execution_timeout_seconds = 3600

            await loop._check_in_progress("owner/repo")

        # Execution marked failed
        mock_db.update_execution.assert_called_once()
        updated = mock_db.update_execution.call_args[0][0]
        assert updated.status == ExecutionStatus.FAILED
        assert updated.result == "Timed out"

        # Label transitioned to ag/failed
        mock_labels.transition_to.assert_called_once_with("owner/repo", "42", "ag/failed")

        # Pipeline event recorded
        mock_db.record_pipeline_event.assert_called_once()
        event_kwargs = mock_db.record_pipeline_event.call_args[1]
        assert event_kwargs["issue_number"] == 42
        assert event_kwargs["event_type"] == "execution_timeout"

    @pytest.mark.asyncio
    async def test_non_timed_out_execution_not_transitioned(self):
        """Execution within timeout should not be touched."""
        from agent_grid.coordinator.management_loop import ManagementLoop
        from agent_grid.execution_grid import AgentExecution, ExecutionStatus, utc_now

        loop = ManagementLoop.__new__(ManagementLoop)

        execution = AgentExecution(
            id=uuid4(),
            repo_url="https://github.com/owner/repo",
            status=ExecutionStatus.RUNNING,
            prompt="test",
            mode="implement",
        )
        execution.started_at = utc_now()
        execution.created_at = utc_now()

        mock_db = AsyncMock()
        mock_db.get_running_executions = AsyncMock(return_value=[execution])
        mock_db.list_executions = AsyncMock(return_value=[])
        loop._db = mock_db

        mock_grid = AsyncMock()

        with (
            patch("agent_grid.coordinator.management_loop.get_execution_grid", return_value=mock_grid),
            patch("agent_grid.coordinator.management_loop.settings") as mock_settings,
        ):
            mock_settings.execution_timeout_seconds = 3600

            await loop._check_in_progress("owner/repo")

        mock_db.update_execution.assert_not_called()


class TestReapStaleInProgress:
    """Tests for _reap_stale_in_progress phase 4b."""

    @pytest.mark.asyncio
    async def test_reaps_stuck_issue(self):
        """Issue with ag/in-progress but failed execution should be reaped."""
        from agent_grid.coordinator.management_loop import ManagementLoop
        from agent_grid.execution_grid import AgentExecution, ExecutionStatus
        from agent_grid.issue_tracker.public_api import IssueInfo, IssueStatus

        loop = ManagementLoop.__new__(ManagementLoop)

        stuck_issue = IssueInfo(
            id="99",
            number=99,
            title="Stuck issue",
            body="",
            labels=["ag/in-progress"],
            status=IssueStatus.OPEN,
            repo_url="https://github.com/owner/repo",
            html_url="https://github.com/owner/repo/issues/99",
        )

        failed_exec = AgentExecution(
            id=uuid4(),
            issue_id="99",
            repo_url="https://github.com/owner/repo",
            status=ExecutionStatus.FAILED,
            prompt="test",
            mode="implement",
        )

        mock_db = AsyncMock()
        mock_db.get_execution_for_issue = AsyncMock(return_value=failed_exec)
        mock_db.record_pipeline_event = AsyncMock()
        loop._db = mock_db

        mock_tracker = AsyncMock()
        mock_tracker.list_issues = AsyncMock(return_value=[stuck_issue])
        mock_labels = AsyncMock()

        with (
            patch("agent_grid.coordinator.management_loop.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.management_loop.get_label_manager", return_value=mock_labels),
        ):
            await loop._reap_stale_in_progress("owner/repo")

        mock_labels.transition_to.assert_called_once_with("owner/repo", "99", "ag/failed")
        mock_db.record_pipeline_event.assert_called_once()
        event_kwargs = mock_db.record_pipeline_event.call_args[1]
        assert event_kwargs["event_type"] == "stale_reaped"

    @pytest.mark.asyncio
    async def test_skips_active_execution(self):
        """Issue with a running execution should not be reaped."""
        from agent_grid.coordinator.management_loop import ManagementLoop
        from agent_grid.execution_grid import AgentExecution, ExecutionStatus
        from agent_grid.issue_tracker.public_api import IssueInfo, IssueStatus

        loop = ManagementLoop.__new__(ManagementLoop)

        active_issue = IssueInfo(
            id="50",
            number=50,
            title="Active issue",
            body="",
            labels=["ag/in-progress"],
            status=IssueStatus.OPEN,
            repo_url="https://github.com/owner/repo",
            html_url="https://github.com/owner/repo/issues/50",
        )

        running_exec = AgentExecution(
            id=uuid4(),
            issue_id="50",
            repo_url="https://github.com/owner/repo",
            status=ExecutionStatus.RUNNING,
            prompt="test",
            mode="implement",
        )

        mock_db = AsyncMock()
        mock_db.get_execution_for_issue = AsyncMock(return_value=running_exec)
        loop._db = mock_db

        mock_tracker = AsyncMock()
        mock_tracker.list_issues = AsyncMock(return_value=[active_issue])
        mock_labels = AsyncMock()

        with (
            patch("agent_grid.coordinator.management_loop.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.management_loop.get_label_manager", return_value=mock_labels),
        ):
            await loop._reap_stale_in_progress("owner/repo")

        mock_labels.transition_to.assert_not_called()


class TestAutoRetryFailed:
    """Tests for _auto_retry_failed phase 4c."""

    @pytest.mark.asyncio
    async def test_retries_failed_issue_under_max(self):
        """ag/failed issue with retry_count < max should be re-launched."""
        from agent_grid.coordinator.management_loop import ManagementLoop
        from agent_grid.issue_tracker.public_api import IssueInfo, IssueStatus

        loop = ManagementLoop.__new__(ManagementLoop)

        failed_issue = IssueInfo(
            id="42",
            number=42,
            title="Failed issue",
            body="Fix something",
            labels=["ag/failed"],
            status=IssueStatus.OPEN,
            repo_url="https://github.com/owner/repo",
            html_url="https://github.com/owner/repo/issues/42",
        )

        mock_db = AsyncMock()
        mock_db.get_issue_state = AsyncMock(return_value={"retry_count": 0})
        mock_db.get_latest_checkpoint = AsyncMock(return_value=None)
        mock_db.upsert_issue_state = AsyncMock()
        mock_db.record_pipeline_event = AsyncMock()
        loop._db = mock_db

        mock_tracker = AsyncMock()
        mock_tracker.list_issues = AsyncMock(return_value=[failed_issue])
        mock_labels = AsyncMock()
        mock_budget = AsyncMock()
        mock_budget.can_launch_agent = AsyncMock(return_value=(True, ""))

        mock_launcher = AsyncMock()
        mock_launcher.has_active_execution = AsyncMock(return_value=False)
        mock_launcher.resolve_reviewer = AsyncMock(return_value=None)
        mock_launcher.claim_and_launch = AsyncMock(return_value=True)

        with (
            patch("agent_grid.coordinator.management_loop.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.management_loop.get_label_manager", return_value=mock_labels),
            patch("agent_grid.coordinator.management_loop.get_budget_manager", return_value=mock_budget),
            patch("agent_grid.coordinator.management_loop.get_agent_launcher", return_value=mock_launcher),
            patch("agent_grid.coordinator.management_loop.settings") as mock_settings,
        ):
            mock_settings.max_retries_per_issue = 2
            mock_settings.max_auto_retries_per_cycle = 10
            await loop._auto_retry_failed("owner/repo")

        # Should transition to in-progress then launch
        mock_labels.transition_to.assert_any_call("owner/repo", "42", "ag/in-progress")
        mock_launcher.claim_and_launch.assert_called_once()
        mock_db.upsert_issue_state.assert_called_once_with(issue_number=42, repo="owner/repo", retry_count=1)
        # Should record auto_retry pipeline event
        retry_event_calls = [
            c for c in mock_db.record_pipeline_event.call_args_list if c[1].get("event_type") == "auto_retry"
        ]
        assert len(retry_event_calls) == 1

    @pytest.mark.asyncio
    async def test_skips_issue_at_max_retries(self):
        """ag/failed issue with retry_count >= max should not be retried."""
        from agent_grid.coordinator.management_loop import ManagementLoop
        from agent_grid.issue_tracker.public_api import IssueInfo, IssueStatus

        loop = ManagementLoop.__new__(ManagementLoop)

        failed_issue = IssueInfo(
            id="42",
            number=42,
            title="Failed issue",
            body="Fix something",
            labels=["ag/failed"],
            status=IssueStatus.OPEN,
            repo_url="https://github.com/owner/repo",
            html_url="https://github.com/owner/repo/issues/42",
        )

        mock_db = AsyncMock()
        mock_db.get_issue_state = AsyncMock(return_value={"retry_count": 2})
        loop._db = mock_db

        mock_tracker = AsyncMock()
        mock_tracker.list_issues = AsyncMock(return_value=[failed_issue])
        mock_labels = AsyncMock()
        mock_budget = AsyncMock()
        mock_budget.can_launch_agent = AsyncMock(return_value=(True, ""))

        with (
            patch("agent_grid.coordinator.management_loop.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.management_loop.get_label_manager", return_value=mock_labels),
            patch("agent_grid.coordinator.management_loop.get_budget_manager", return_value=mock_budget),
            patch("agent_grid.coordinator.management_loop.settings") as mock_settings,
        ):
            mock_settings.max_retries_per_issue = 2
            mock_settings.max_auto_retries_per_cycle = 10
            await loop._auto_retry_failed("owner/repo")

        mock_labels.transition_to.assert_not_called()

    @pytest.mark.asyncio
    async def test_respects_budget_limit(self):
        """Auto-retry should stop when budget is exhausted."""
        from agent_grid.coordinator.management_loop import ManagementLoop
        from agent_grid.issue_tracker.public_api import IssueInfo, IssueStatus

        loop = ManagementLoop.__new__(ManagementLoop)

        failed_issue = IssueInfo(
            id="42",
            number=42,
            title="Failed issue",
            body="Fix something",
            labels=["ag/failed"],
            status=IssueStatus.OPEN,
            repo_url="https://github.com/owner/repo",
            html_url="https://github.com/owner/repo/issues/42",
        )

        mock_db = AsyncMock()
        loop._db = mock_db

        mock_tracker = AsyncMock()
        mock_tracker.list_issues = AsyncMock(return_value=[failed_issue])
        mock_labels = AsyncMock()
        mock_budget = AsyncMock()
        mock_budget.can_launch_agent = AsyncMock(return_value=(False, "daily limit reached"))

        with (
            patch("agent_grid.coordinator.management_loop.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.management_loop.get_label_manager", return_value=mock_labels),
            patch("agent_grid.coordinator.management_loop.get_budget_manager", return_value=mock_budget),
            patch("agent_grid.coordinator.management_loop.settings") as mock_settings,
        ):
            mock_settings.max_retries_per_issue = 2
            mock_settings.max_auto_retries_per_cycle = 10
            await loop._auto_retry_failed("owner/repo")

        # Should not attempt to launch
        mock_labels.transition_to.assert_not_called()
        mock_db.get_issue_state.assert_not_called()


class TestResolveReviewer:
    """Tests for resolve_reviewer — parent issue author lookup for sub-issues."""

    @pytest.mark.asyncio
    async def test_returns_parent_author_for_sub_issue(self):
        """Sub-issue should resolve to parent issue's author."""
        from agent_grid.coordinator.agent_launcher import AgentLauncher
        from agent_grid.issue_tracker.public_api import IssueInfo, IssueStatus

        launcher = AgentLauncher.__new__(AgentLauncher)

        sub_issue = IssueInfo(
            id="100",
            number=100,
            title="[Sub #50] Do something",
            body="Part of #50",
            author="bot-user",
            labels=["ag/sub-issue"],
            status=IssueStatus.OPEN,
            repo_url="https://github.com/owner/repo",
            html_url="https://github.com/owner/repo/issues/100",
        )

        parent_issue = IssueInfo(
            id="50",
            number=50,
            title="Parent issue",
            body="",
            author="human-user",
            labels=[],
            status=IssueStatus.OPEN,
            repo_url="https://github.com/owner/repo",
            html_url="https://github.com/owner/repo/issues/50",
        )

        mock_tracker = AsyncMock()
        mock_tracker.get_issue = AsyncMock(return_value=parent_issue)
        launcher._tracker = mock_tracker

        reviewer = await launcher.resolve_reviewer("owner/repo", sub_issue)
        assert reviewer == "human-user"
        mock_tracker.get_issue.assert_called_once_with("owner/repo", "50")

    @pytest.mark.asyncio
    async def test_returns_none_for_non_sub_issue(self):
        """Non-sub-issue should return None (use default author)."""
        from agent_grid.coordinator.agent_launcher import AgentLauncher
        from agent_grid.issue_tracker.public_api import IssueInfo, IssueStatus

        launcher = AgentLauncher.__new__(AgentLauncher)

        regular_issue = IssueInfo(
            id="100",
            number=100,
            title="Regular issue",
            body="Fix a bug",
            author="human-user",
            labels=[],
            status=IssueStatus.OPEN,
            repo_url="https://github.com/owner/repo",
            html_url="https://github.com/owner/repo/issues/100",
        )

        reviewer = await launcher.resolve_reviewer("owner/repo", regular_issue)
        assert reviewer is None
