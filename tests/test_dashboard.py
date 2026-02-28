"""Tests for the pipeline dashboard API and audit trail."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_grid.dry_run import DryRunDatabase
from agent_grid.issue_tracker.public_api import IssueInfo, IssueStatus

# ---------------------------------------------------------------------------
# DryRunDatabase pipeline event tests
# ---------------------------------------------------------------------------


class TestPipelineEventsDB:
    """Test pipeline event CRUD on DryRunDatabase."""

    @pytest.fixture
    def db(self):
        return DryRunDatabase()

    @pytest.mark.asyncio
    async def test_record_and_retrieve(self, db):
        await db.record_pipeline_event(1, "org/repo", "classified", "classify", {"category": "SIMPLE"})
        await db.record_pipeline_event(2, "org/repo", "launched", "launch", {"mode": "implement"})

        events = await db.get_pipeline_events("org/repo")
        assert len(events) == 2
        assert events[0]["issue_number"] == 2
        assert events[0]["event_type"] == "launched"
        assert events[1]["issue_number"] == 1

    @pytest.mark.asyncio
    async def test_filter_by_issue(self, db):
        await db.record_pipeline_event(1, "org/repo", "classified", "classify")
        await db.record_pipeline_event(2, "org/repo", "classified", "classify")

        events = await db.get_pipeline_events("org/repo", issue_number=1)
        assert len(events) == 1
        assert events[0]["issue_number"] == 1

    @pytest.mark.asyncio
    async def test_filter_by_event_type(self, db):
        await db.record_pipeline_event(1, "org/repo", "classified", "classify")
        await db.record_pipeline_event(1, "org/repo", "launched", "launch")

        events = await db.get_pipeline_events("org/repo", event_type="launched")
        assert len(events) == 1
        assert events[0]["event_type"] == "launched"

    @pytest.mark.asyncio
    async def test_pagination(self, db):
        for i in range(10):
            await db.record_pipeline_event(i, "org/repo", "classified", "classify")

        page = await db.get_pipeline_events("org/repo", limit=3, offset=0)
        assert len(page) == 3

        page2 = await db.get_pipeline_events("org/repo", limit=3, offset=3)
        assert len(page2) == 3
        assert page[0]["issue_number"] != page2[0]["issue_number"]

    @pytest.mark.asyncio
    async def test_pipeline_stats(self, db):
        await db.upsert_issue_state(1, "org/repo", classification="SIMPLE")
        await db.upsert_issue_state(2, "org/repo", classification="SIMPLE")
        await db.upsert_issue_state(3, "org/repo", classification="COMPLEX")

        stats = await db.get_pipeline_stats("org/repo")
        assert stats["classifications"]["SIMPLE"] == 2
        assert stats["classifications"]["COMPLEX"] == 1
        assert stats["total_tracked_issues"] == 3

    @pytest.mark.asyncio
    async def test_list_all_issue_states(self, db):
        await db.upsert_issue_state(1, "org/repo", classification="SIMPLE")
        await db.upsert_issue_state(2, "org/repo", classification="COMPLEX")
        await db.upsert_issue_state(3, "other/repo", classification="SIMPLE")

        states = await db.list_all_issue_states("org/repo")
        assert len(states) == 2


# ---------------------------------------------------------------------------
# Dashboard API endpoint tests
# ---------------------------------------------------------------------------


def _make_issue(number, labels=None, title="Test issue"):
    return IssueInfo(
        id=str(number),
        number=number,
        title=title,
        body="test body",
        status=IssueStatus.OPEN,
        labels=labels or [],
        repo_url="https://github.com/org/repo",
        html_url=f"https://github.com/org/repo/issues/{number}",
        created_at=datetime.now(timezone.utc),
    )


class TestDashboardOverview:
    @pytest.mark.asyncio
    async def test_overview_returns_funnel(self):
        """GET /api/dashboard/overview returns pipeline funnel data."""
        from agent_grid.coordinator.dashboard_api import pipeline_overview

        mock_tracker = AsyncMock()
        mock_tracker.list_issues.return_value = [
            _make_issue(1, ["ag/todo"]),
            _make_issue(2, ["ag/in-progress"]),
            _make_issue(3),
            _make_issue(4),
        ]

        db = DryRunDatabase()
        await db.upsert_issue_state(1, "org/repo", classification="SIMPLE")
        await db.upsert_issue_state(2, "org/repo", classification="SIMPLE")

        mock_budget_mgr = AsyncMock()
        mock_budget_mgr.get_budget_status.return_value = {
            "concurrent_executions": 1,
            "max_concurrent": 5,
            "tokens_used": 0,
            "duration_seconds": 0,
        }

        with (
            patch("agent_grid.issue_tracker.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.database.get_database", return_value=db),
            patch("agent_grid.coordinator.budget_manager.get_budget_manager", return_value=mock_budget_mgr),
            patch("agent_grid.config.settings", MagicMock(target_repo="org/repo")),
        ):
            result = await pipeline_overview()

        assert result["total_open_issues"] == 4
        assert result["labeled_issues"] == 2
        assert result["unlabeled_issues"] == 2
        assert result["issues_by_label"]["ag/todo"] == 1
        assert result["issues_by_label"]["ag/in-progress"] == 1


class TestDashboardIssues:
    @pytest.mark.asyncio
    async def test_issues_merges_db_state(self):
        """GET /api/dashboard/issues merges GitHub issues with DB state."""
        from agent_grid.coordinator.dashboard_api import list_issues

        mock_tracker = AsyncMock()
        mock_tracker.list_issues.return_value = [
            _make_issue(1, ["ag/in-progress"]),
            _make_issue(2),
        ]

        db = DryRunDatabase()
        await db.upsert_issue_state(1, "org/repo", classification="SIMPLE", metadata={"confidence_score": 8})

        with (
            patch("agent_grid.issue_tracker.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.database.get_database", return_value=db),
            patch("agent_grid.config.settings", MagicMock(target_repo="org/repo")),
        ):
            result = await list_issues()

        assert len(result) == 2
        issue1 = next(i for i in result if i["issue_number"] == 1)
        assert issue1["pipeline_stage"] == "in-progress"
        assert issue1["classification"] == "SIMPLE"
        assert issue1["confidence_score"] == 8

        issue2 = next(i for i in result if i["issue_number"] == 2)
        assert issue2["pipeline_stage"] == "unlabeled"
        assert issue2["classification"] is None

    @pytest.mark.asyncio
    async def test_issues_filter_by_stage(self):
        """Issues can be filtered by pipeline stage."""
        from agent_grid.coordinator.dashboard_api import list_issues

        mock_tracker = AsyncMock()
        mock_tracker.list_issues.return_value = [
            _make_issue(1, ["ag/in-progress"]),
            _make_issue(2),
        ]

        db = DryRunDatabase()

        with (
            patch("agent_grid.issue_tracker.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.database.get_database", return_value=db),
            patch("agent_grid.config.settings", MagicMock(target_repo="org/repo")),
        ):
            result = await list_issues(stage="unlabeled")

        assert len(result) == 1
        assert result[0]["issue_number"] == 2


class TestDashboardActions:
    @pytest.mark.asyncio
    async def test_activate_adds_label(self):
        """POST /actions/activate adds ag/todo label and records event."""
        from agent_grid.coordinator.dashboard_api import ActivateRequest, activate_issues

        db = DryRunDatabase()
        mock_labels = AsyncMock()

        req = ActivateRequest(issue_numbers=[1, 2])

        with (
            patch("agent_grid.issue_tracker.label_manager.get_label_manager", return_value=mock_labels),
            patch("agent_grid.coordinator.database.get_database", return_value=db),
            patch("agent_grid.config.settings", MagicMock(target_repo="org/repo")),
        ):
            result = await activate_issues(req)

        assert result["activated"] == [1, 2]
        assert mock_labels.add_label.call_count == 2

        events = await db.get_pipeline_events("org/repo")
        assert len(events) == 2
        assert all(e["event_type"] == "manual_activate" for e in events)

    @pytest.mark.asyncio
    async def test_classify_runs_classifier(self):
        """POST /actions/classify runs classifier and records events."""
        from agent_grid.coordinator.dashboard_api import ClassifyRequest, classify_issues

        db = DryRunDatabase()
        mock_tracker = AsyncMock()
        mock_tracker.get_issue.return_value = _make_issue(1)

        mock_classification = MagicMock()
        mock_classification.category = "SIMPLE"
        mock_classification.reason = "Small change"
        mock_classification.estimated_complexity = 2

        mock_classifier = AsyncMock()
        mock_classifier.classify.return_value = mock_classification

        req = ClassifyRequest(issue_numbers=[1])

        with (
            patch("agent_grid.issue_tracker.get_issue_tracker", return_value=mock_tracker),
            patch("agent_grid.coordinator.database.get_database", return_value=db),
            patch("agent_grid.coordinator.classifier.get_classifier", return_value=mock_classifier),
            patch("agent_grid.config.settings", MagicMock(target_repo="org/repo")),
        ):
            result = await classify_issues(req)

        assert result["results"][0]["classification"] == "SIMPLE"
        assert result["results"][0]["reason"] == "Small change"

        state = await db.get_issue_state(1, "org/repo")
        assert state["classification"] == "SIMPLE"

        events = await db.get_pipeline_events("org/repo")
        assert len(events) == 1
        assert events[0]["event_type"] == "manual_classify"

    @pytest.mark.asyncio
    async def test_retry_resets_issue(self):
        """POST /actions/retry resets issue to ag/todo."""
        from agent_grid.coordinator.dashboard_api import retry_issue

        db = DryRunDatabase()
        await db.upsert_issue_state(1, "org/repo", classification="SKIP", retry_count=3)

        mock_labels = AsyncMock()

        with (
            patch("agent_grid.issue_tracker.label_manager.get_label_manager", return_value=mock_labels),
            patch("agent_grid.coordinator.database.get_database", return_value=db),
            patch("agent_grid.config.settings", MagicMock(target_repo="org/repo")),
        ):
            result = await retry_issue(1)

        assert result["status"] == "retried"
        mock_labels.transition_to.assert_called_once_with("org/repo", "1", "ag/todo")

        state = await db.get_issue_state(1, "org/repo")
        assert state["retry_count"] == 0

        events = await db.get_pipeline_events("org/repo")
        assert events[0]["event_type"] == "manual_retry"
