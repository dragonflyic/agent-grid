"""Tests for CI failure polling (CIMonitor)."""

import pytest

from agent_grid.coordinator.ci_monitor import CIMonitor


def _make_pr(number: int, branch: str, sha: str) -> dict:
    return {
        "number": number,
        "head": {"ref": branch, "sha": sha},
    }


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeHTTPClient:
    """Fake httpx client returning canned PR lists and check runs."""

    def __init__(self, prs=None, check_runs_by_sha=None):
        self._prs = prs or []
        self._check_runs = check_runs_by_sha or {}

    async def get(self, url, **kwargs):
        if "/pulls" in url:
            return FakeResponse(self._prs)
        if "/check-runs" in url:
            # Extract sha from URL: /repos/{repo}/commits/{sha}/check-runs
            sha = url.split("/commits/")[1].split("/check-runs")[0]
            return FakeResponse({"check_runs": self._check_runs.get(sha, [])})
        return FakeResponse({})


class FakeDB:
    def __init__(self, states=None):
        self._states = states or {}
        self._cron_state = {}

    async def get_issue_state(self, issue_number, repo):
        return self._states.get(issue_number)

    async def set_cron_state(self, key, value):
        self._cron_state[key] = value


class FakeGitHubClient:
    """Minimal fake that passes isinstance check for GitHubClient."""

    def __init__(self, http_client):
        self._client = http_client

    async def get_check_runs_for_ref(self, repo, ref, *, status="completed"):
        try:
            response = await self._client.get(
                f"/repos/{repo}/commits/{ref}/check-runs",
                params={"status": status, "per_page": 100},
            )
            response.raise_for_status()
            return response.json().get("check_runs", [])
        except Exception:
            return []


def _make_monitor(prs=None, check_runs_by_sha=None, states=None):
    """Build a CIMonitor with injected fakes."""
    from agent_grid.issue_tracker.github_client import GitHubClient

    monitor = CIMonitor.__new__(CIMonitor)
    http = FakeHTTPClient(prs=prs, check_runs_by_sha=check_runs_by_sha)
    # Create a GitHubClient-like object that passes isinstance
    fake_gh = GitHubClient.__new__(GitHubClient)
    fake_gh._client = http

    async def _get_check_runs(repo, ref, *, status="completed"):
        try:
            response = await http.get(
                f"/repos/{repo}/commits/{ref}/check-runs",
                params={"status": status, "per_page": 100},
            )
            return response.json().get("check_runs", [])
        except Exception:
            return []

    fake_gh.get_check_runs_for_ref = _get_check_runs
    monitor._tracker = fake_gh
    monitor._db = FakeDB(states=states)
    return monitor


class TestCIMonitor:
    @pytest.mark.asyncio
    async def test_skips_non_agent_branches(self):
        prs = [_make_pr(1, "feature/foo", "sha1")]
        monitor = _make_monitor(
            prs=prs,
            check_runs_by_sha={
                "sha1": [{"conclusion": "failure", "name": "test", "id": 1, "html_url": ""}],
            },
        )
        failures = await monitor.check_ci_failures("owner/repo")
        assert failures == []

    @pytest.mark.asyncio
    async def test_deduplicates_already_seen_sha(self):
        prs = [_make_pr(1, "agent/42", "sha-already-seen")]
        monitor = _make_monitor(
            prs=prs,
            check_runs_by_sha={
                "sha-already-seen": [{"conclusion": "failure", "name": "test", "id": 1, "html_url": ""}],
            },
            states={42: {"metadata": {"last_ci_check_sha": "sha-already-seen"}}},
        )
        failures = await monitor.check_ci_failures("owner/repo")
        assert failures == []

    @pytest.mark.asyncio
    async def test_returns_failure(self):
        prs = [_make_pr(5, "agent/99", "sha-new")]
        monitor = _make_monitor(
            prs=prs,
            check_runs_by_sha={
                "sha-new": [{"conclusion": "failure", "name": "lint", "id": 777, "html_url": "https://example.com"}],
            },
        )
        failures = await monitor.check_ci_failures("owner/repo")
        assert len(failures) == 1
        f = failures[0]
        assert f["branch"] == "agent/99"
        assert f["head_sha"] == "sha-new"
        assert f["pr_number"] == 5
        assert f["check_name"] == "lint"
        assert f["job_id"] == 777

    @pytest.mark.asyncio
    async def test_success_check_runs_ignored(self):
        prs = [_make_pr(1, "agent/10", "sha1")]
        monitor = _make_monitor(
            prs=prs,
            check_runs_by_sha={
                "sha1": [{"conclusion": "success", "name": "test", "id": 1, "html_url": ""}],
            },
        )
        failures = await monitor.check_ci_failures("owner/repo")
        assert failures == []

    @pytest.mark.asyncio
    async def test_one_failure_per_pr(self):
        prs = [_make_pr(1, "agent/10", "sha1")]
        monitor = _make_monitor(
            prs=prs,
            check_runs_by_sha={
                "sha1": [
                    {"conclusion": "failure", "name": "lint", "id": 1, "html_url": ""},
                    {"conclusion": "failure", "name": "test", "id": 2, "html_url": ""},
                ],
            },
        )
        failures = await monitor.check_ci_failures("owner/repo")
        assert len(failures) == 1  # Only one per PR

    @pytest.mark.asyncio
    async def test_updates_cron_state(self):
        monitor = _make_monitor(prs=[])
        await monitor.check_ci_failures("owner/repo")
        assert "last_ci_poll" in monitor._db._cron_state
