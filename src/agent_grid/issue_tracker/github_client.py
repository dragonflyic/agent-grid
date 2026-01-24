"""GitHub API implementation of issue tracker."""

import httpx
from datetime import datetime

from ..common.models import IssueInfo, IssueStatus
from ..config import settings
from .public_api import IssueTracker


class GitHubClient(IssueTracker):
    """GitHub implementation of the issue tracker interface."""

    BASE_URL = "https://api.github.com"
    SUBISSUE_LABEL = "subissue"

    def __init__(self, token: str | None = None):
        self._token = token or settings.github_token
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def get_issue(self, repo: str, issue_id: str) -> IssueInfo:
        """Get information about a GitHub issue."""
        response = await self._client.get(f"/repos/{repo}/issues/{issue_id}")
        response.raise_for_status()
        data = response.json()
        return self._parse_issue(repo, data)

    async def list_subissues(self, repo: str, parent_id: str) -> list[IssueInfo]:
        """
        List all subissues of a parent issue.

        Subissues are identified by:
        1. Having the 'subissue' label
        2. Having a body that references the parent issue
        """
        # Get issues with subissue label
        response = await self._client.get(
            f"/repos/{repo}/issues",
            params={
                "labels": self.SUBISSUE_LABEL,
                "state": "all",
                "per_page": 100,
            },
        )
        response.raise_for_status()

        subissues = []
        parent_ref = f"#{parent_id}"

        for data in response.json():
            # Check if this issue references the parent
            body = data.get("body") or ""
            if parent_ref in body or f"Parent: {parent_ref}" in body:
                issue = self._parse_issue(repo, data, parent_id=parent_id)
                subissues.append(issue)

        return subissues

    async def create_subissue(
        self,
        repo: str,
        parent_id: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> IssueInfo:
        """Create a subissue under a parent issue."""
        # Prepend parent reference to body
        full_body = f"Parent: #{parent_id}\n\n{body}"

        # Ensure subissue label is included
        all_labels = list(labels) if labels else []
        if self.SUBISSUE_LABEL not in all_labels:
            all_labels.append(self.SUBISSUE_LABEL)

        response = await self._client.post(
            f"/repos/{repo}/issues",
            json={
                "title": title,
                "body": full_body,
                "labels": all_labels,
            },
        )
        response.raise_for_status()

        data = response.json()
        return self._parse_issue(repo, data, parent_id=parent_id)

    async def add_comment(self, repo: str, issue_id: str, body: str) -> None:
        """Add a comment to an issue."""
        response = await self._client.post(
            f"/repos/{repo}/issues/{issue_id}/comments",
            json={"body": body},
        )
        response.raise_for_status()

    async def update_issue_status(
        self, repo: str, issue_id: str, status: IssueStatus
    ) -> None:
        """Update the status of an issue."""
        # Map status to GitHub state
        state = "open" if status in (IssueStatus.OPEN, IssueStatus.IN_PROGRESS) else "closed"

        # For in_progress, we could add a label
        labels_to_add = []
        labels_to_remove = []

        if status == IssueStatus.IN_PROGRESS:
            labels_to_add.append("in-progress")
        else:
            labels_to_remove.append("in-progress")

        # Update state
        response = await self._client.patch(
            f"/repos/{repo}/issues/{issue_id}",
            json={"state": state},
        )
        response.raise_for_status()

        # Update labels if needed
        if labels_to_add:
            await self._client.post(
                f"/repos/{repo}/issues/{issue_id}/labels",
                json={"labels": labels_to_add},
            )

        for label in labels_to_remove:
            await self._client.delete(
                f"/repos/{repo}/issues/{issue_id}/labels/{label}",
            )

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    def _parse_issue(
        self, repo: str, data: dict, parent_id: str | None = None
    ) -> IssueInfo:
        """Parse GitHub API response into IssueInfo."""
        labels = [label["name"] for label in data.get("labels", [])]

        # Determine status
        state = data.get("state", "open")
        if state == "closed":
            status = IssueStatus.CLOSED
        elif "in-progress" in labels:
            status = IssueStatus.IN_PROGRESS
        else:
            status = IssueStatus.OPEN

        created_at = None
        if data.get("created_at"):
            created_at = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))

        updated_at = None
        if data.get("updated_at"):
            updated_at = datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))

        return IssueInfo(
            id=str(data["number"]),
            number=data["number"],
            title=data["title"],
            body=data.get("body"),
            status=status,
            labels=labels,
            repo_url=f"https://github.com/{repo}",
            html_url=data["html_url"],
            parent_id=parent_id,
            created_at=created_at,
            updated_at=updated_at,
        )
