"""Tests for the proactive scanner module."""

import pytest

from agent_grid.coordinator.proactive_scanner import ProactiveScanner
from agent_grid.issue_tracker.public_api import IssueInfo, IssueStatus


def _make_issue(number: int, labels: list[str] | None = None, title: str = "Test") -> IssueInfo:
    return IssueInfo(
        id=str(number),
        number=number,
        title=title,
        body="Some description",
        labels=labels or [],
        repo_url="https://github.com/owner/repo",
        html_url=f"https://github.com/owner/repo/issues/{number}",
        status=IssueStatus.OPEN,
    )


class FakeTracker:
    """Fake issue tracker for testing."""

    def __init__(self, issues: list[IssueInfo]):
        self._issues = issues

    async def list_issues(self, repo, status=None, labels=None):
        return [i for i in self._issues if i.status == IssueStatus.OPEN]


class FakeDB:
    """Fake database for testing."""

    def __init__(self, states: dict | None = None):
        self._states = states or {}

    async def get_issue_state(self, issue_number: int, repo: str) -> dict | None:
        return self._states.get(issue_number)


class TestProactiveScannerFiltering:
    """Tests for ProactiveScanner issue filtering."""

    def _make_scanner(self, issues, states=None):
        scanner = ProactiveScanner.__new__(ProactiveScanner)
        scanner._tracker = FakeTracker(issues)
        scanner._db = FakeDB(states or {})
        return scanner

    @pytest.mark.asyncio
    async def test_filters_out_ag_labeled_issues(self):
        """Issues with any ag/* label should be excluded."""
        issues = [
            _make_issue(1, labels=["ag/todo"]),
            _make_issue(2, labels=["ag/in-progress"]),
            _make_issue(3, labels=["bug"]),
            _make_issue(4, labels=[]),
        ]
        scanner = self._make_scanner(issues)
        candidates = await scanner.scan("owner/repo")
        assert len(candidates) == 2
        assert {c.number for c in candidates} == {3, 4}

    @pytest.mark.asyncio
    async def test_filters_already_skipped(self):
        """Issues with proactive_skipped=True should be excluded."""
        issues = [
            _make_issue(1),
            _make_issue(2),
        ]
        states = {
            1: {"metadata": {"proactive_skipped": True}},
        }
        scanner = self._make_scanner(issues, states)
        candidates = await scanner.scan("owner/repo")
        assert len(candidates) == 1
        assert candidates[0].number == 2

    @pytest.mark.asyncio
    async def test_filters_already_picked(self):
        """Issues with proactive_picked=True should be excluded."""
        issues = [
            _make_issue(1),
            _make_issue(2),
        ]
        states = {
            1: {"metadata": {"proactive_picked": True}},
        }
        scanner = self._make_scanner(issues, states)
        candidates = await scanner.scan("owner/repo")
        assert len(candidates) == 1
        assert candidates[0].number == 2

    @pytest.mark.asyncio
    async def test_includes_unevaluated_issues(self):
        """Issues with no prior state should be included."""
        issues = [
            _make_issue(1),
            _make_issue(2),
            _make_issue(3),
        ]
        scanner = self._make_scanner(issues)
        candidates = await scanner.scan("owner/repo")
        assert len(candidates) == 3

    @pytest.mark.asyncio
    async def test_empty_repo(self):
        """No issues should return empty list."""
        scanner = self._make_scanner([])
        candidates = await scanner.scan("owner/repo")
        assert candidates == []

    @pytest.mark.asyncio
    async def test_all_labeled_returns_empty(self):
        """If all issues have ag/* labels, candidates should be empty."""
        issues = [
            _make_issue(1, labels=["ag/todo"]),
            _make_issue(2, labels=["ag/done"]),
            _make_issue(3, labels=["ag/sub-issue"]),
        ]
        scanner = self._make_scanner(issues)
        candidates = await scanner.scan("owner/repo")
        assert candidates == []

    @pytest.mark.asyncio
    async def test_metadata_none_does_not_crash(self):
        """Issues with metadata=None should not crash."""
        issues = [_make_issue(1)]
        states = {1: {"metadata": None}}
        scanner = self._make_scanner(issues, states)
        candidates = await scanner.scan("owner/repo")
        assert len(candidates) == 1


class TestProactiveScanConfig:
    """Tests for proactive scan configuration."""

    def test_default_disabled(self):
        """Proactive scan should be disabled by default."""
        from agent_grid.config import settings

        assert settings.proactive_scan_enabled is False

    def test_default_min_score(self):
        """Default proactive min score should be 9."""
        from agent_grid.config import settings

        assert settings.proactive_min_score == 9

    def test_default_max_per_cycle(self):
        """Default max per cycle should be 3."""
        from agent_grid.config import settings

        assert settings.proactive_max_per_cycle == 3
