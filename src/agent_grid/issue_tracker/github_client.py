"""GitHub API implementation of issue tracker."""

import re
from datetime import datetime

import httpx

from ..common.models import Comment, IssueInfo, IssueStatus
from ..config import settings
from .public_api import IssueTracker


class GitHubClient(IssueTracker):
    """
    GitHub implementation of the issue tracker interface.

    Conventions used for relationships (stored in issue body):
    - Parent reference: "Parent: #123" on its own line
    - Blocked by: "Blocked by: #1, #2, #3" on its own line

    Labels used:
    - "subissue": marks an issue as a subissue
    - "in-progress": marks an issue as in progress
    """

    BASE_URL = "https://api.github.com"
    SUBISSUE_LABEL = "subissue"
    IN_PROGRESS_LABEL = "in-progress"

    # Regex patterns for parsing relationships from issue body
    PARENT_PATTERN = re.compile(r"^Parent:\s*#(\d+)\s*$", re.MULTILINE)
    BLOCKED_BY_PATTERN = re.compile(r"^Blocked by:\s*(.+)$", re.MULTILINE)
    ISSUE_REF_PATTERN = re.compile(r"#(\d+)")

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
        """Get information about a GitHub issue including comments."""
        # Fetch issue
        response = await self._client.get(f"/repos/{repo}/issues/{issue_id}")
        response.raise_for_status()
        data = response.json()

        # Fetch comments
        comments = await self._fetch_comments(repo, issue_id)

        return self._parse_issue(repo, data, comments=comments)

    async def _fetch_comments(self, repo: str, issue_id: str) -> list[Comment]:
        """Fetch all comments for an issue."""
        comments = []
        page = 1

        while True:
            response = await self._client.get(
                f"/repos/{repo}/issues/{issue_id}/comments",
                params={"per_page": 100, "page": page},
            )
            response.raise_for_status()
            data = response.json()

            if not data:
                break

            for item in data:
                created_at = datetime.fromisoformat(
                    item["created_at"].replace("Z", "+00:00")
                )
                comments.append(
                    Comment(
                        id=str(item["id"]),
                        body=item["body"] or "",
                        created_at=created_at,
                    )
                )

            if len(data) < 100:
                break
            page += 1

        return comments

    async def list_issues(
        self,
        repo: str,
        status: IssueStatus | None = None,
        labels: list[str] | None = None,
    ) -> list[IssueInfo]:
        """List issues with optional filters."""
        params: dict = {"per_page": 100}

        # Map status to GitHub state
        if status == IssueStatus.CLOSED:
            params["state"] = "closed"
        elif status in (IssueStatus.OPEN, IssueStatus.IN_PROGRESS):
            params["state"] = "open"
        else:
            params["state"] = "all"

        # Add label filters
        if labels:
            params["labels"] = ",".join(labels)

        response = await self._client.get(f"/repos/{repo}/issues", params=params)
        response.raise_for_status()

        issues = []
        for data in response.json():
            # Skip pull requests (GitHub API returns them with issues)
            if "pull_request" in data:
                continue
            issue = self._parse_issue(repo, data)

            # Additional filter for in-progress status
            if status == IssueStatus.IN_PROGRESS and issue.status != IssueStatus.IN_PROGRESS:
                continue

            issues.append(issue)

        return issues

    async def list_subissues(self, repo: str, parent_id: str) -> list[IssueInfo]:
        """List all subissues of a parent issue."""
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

        for data in response.json():
            if "pull_request" in data:
                continue

            issue = self._parse_issue(repo, data)
            if issue.parent_id == parent_id:
                subissues.append(issue)

        return subissues

    async def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        parent_id: str | None = None,
        blocked_by: list[str] | None = None,
    ) -> IssueInfo:
        """Create a new issue."""
        # Build body with relationship metadata
        full_body = self._build_body(body, parent_id, blocked_by)

        # Build labels
        all_labels = list(labels) if labels else []
        if parent_id and self.SUBISSUE_LABEL not in all_labels:
            all_labels.append(self.SUBISSUE_LABEL)

        response = await self._client.post(
            f"/repos/{repo}/issues",
            json={
                "title": title,
                "body": full_body,
                "labels": all_labels if all_labels else None,
            },
        )
        response.raise_for_status()

        data = response.json()
        return self._parse_issue(repo, data)

    async def create_subissue(
        self,
        repo: str,
        parent_id: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> IssueInfo:
        """Create a subissue under a parent issue."""
        return await self.create_issue(
            repo=repo,
            title=title,
            body=body,
            labels=labels,
            parent_id=parent_id,
        )

    async def update_issue(
        self,
        repo: str,
        issue_id: str,
        title: str | None = None,
        body: str | None = None,
        status: IssueStatus | None = None,
        labels: list[str] | None = None,
        parent_id: str | None = None,
        blocked_by: list[str] | None = None,
    ) -> IssueInfo:
        """Update an issue's fields."""
        # Get current issue to preserve unspecified fields
        current = await self.get_issue(repo, issue_id)

        update_data: dict = {}

        if title is not None:
            update_data["title"] = title

        # Handle body update - need to preserve/update relationship metadata
        if body is not None or parent_id is not None or blocked_by is not None:
            # Use provided values or fall back to current
            new_body = body if body is not None else self._strip_metadata(current.body or "")
            new_parent = parent_id if parent_id is not None else current.parent_id
            new_blocked = blocked_by if blocked_by is not None else current.blocked_by

            update_data["body"] = self._build_body(new_body, new_parent, new_blocked)

        if status is not None:
            update_data["state"] = "closed" if status == IssueStatus.CLOSED else "open"

        if labels is not None:
            update_data["labels"] = labels

        if update_data:
            response = await self._client.patch(
                f"/repos/{repo}/issues/{issue_id}",
                json=update_data,
            )
            response.raise_for_status()

        # Handle in-progress label separately
        if status == IssueStatus.IN_PROGRESS:
            await self._add_label(repo, issue_id, self.IN_PROGRESS_LABEL)
        elif status in (IssueStatus.OPEN, IssueStatus.CLOSED):
            await self._remove_label(repo, issue_id, self.IN_PROGRESS_LABEL)

        return await self.get_issue(repo, issue_id)

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
        await self.update_issue(repo, issue_id, status=status)

    async def get_blocked_issues(self, repo: str, issue_id: str) -> list[IssueInfo]:
        """Get issues that are blocked by the given issue."""
        # We need to search all open issues and check their blocked_by field
        all_issues = await self.list_issues(repo, status=IssueStatus.OPEN)
        blocked = []

        for issue in all_issues:
            if issue_id in issue.blocked_by:
                blocked.append(issue)

        return blocked

    async def is_blocked(self, repo: str, issue_id: str) -> bool:
        """Check if an issue is blocked by any open issues."""
        issue = await self.get_issue(repo, issue_id)

        for blocker_id in issue.blocked_by:
            try:
                blocker = await self.get_issue(repo, blocker_id)
                if blocker.status != IssueStatus.CLOSED:
                    return True
            except httpx.HTTPStatusError:
                # Blocker doesn't exist, ignore
                continue

        return False

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def _add_label(self, repo: str, issue_id: str, label: str) -> None:
        """Add a label to an issue."""
        try:
            await self._client.post(
                f"/repos/{repo}/issues/{issue_id}/labels",
                json={"labels": [label]},
            )
        except httpx.HTTPStatusError:
            pass  # Label may already exist or not be valid

    async def _remove_label(self, repo: str, issue_id: str, label: str) -> None:
        """Remove a label from an issue."""
        try:
            await self._client.delete(
                f"/repos/{repo}/issues/{issue_id}/labels/{label}",
            )
        except httpx.HTTPStatusError:
            pass  # Label may not exist

    def _build_body(
        self,
        body: str,
        parent_id: str | None,
        blocked_by: list[str] | None,
    ) -> str:
        """Build issue body with relationship metadata."""
        parts = []

        # Add parent reference
        if parent_id:
            parts.append(f"Parent: #{parent_id}")

        # Add blocked by references
        if blocked_by:
            refs = ", ".join(f"#{b}" for b in blocked_by)
            parts.append(f"Blocked by: {refs}")

        # Add separator if we have metadata
        if parts:
            parts.append("")

        # Add main body
        parts.append(body)

        return "\n".join(parts)

    def _strip_metadata(self, body: str) -> str:
        """Strip relationship metadata from issue body."""
        # Remove Parent: line
        body = self.PARENT_PATTERN.sub("", body)
        # Remove Blocked by: line
        body = self.BLOCKED_BY_PATTERN.sub("", body)
        # Clean up extra whitespace at the start
        return body.lstrip("\n")

    def _parse_issue(
        self,
        repo: str,
        data: dict,
        comments: list[Comment] | None = None,
    ) -> IssueInfo:
        """Parse GitHub API response into IssueInfo."""
        labels = [label["name"] for label in data.get("labels", [])]
        body = data.get("body") or ""

        # Determine status
        state = data.get("state", "open")
        if state == "closed":
            status = IssueStatus.CLOSED
        elif self.IN_PROGRESS_LABEL in labels:
            status = IssueStatus.IN_PROGRESS
        else:
            status = IssueStatus.OPEN

        # Parse parent_id from body
        parent_id = None
        parent_match = self.PARENT_PATTERN.search(body)
        if parent_match:
            parent_id = parent_match.group(1)

        # Parse blocked_by from body
        blocked_by: list[str] = []
        blocked_match = self.BLOCKED_BY_PATTERN.search(body)
        if blocked_match:
            blocked_refs = blocked_match.group(1)
            blocked_by = self.ISSUE_REF_PATTERN.findall(blocked_refs)

        # Parse timestamps
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
            body=self._strip_metadata(body) or None,
            status=status,
            labels=labels,
            repo_url=f"https://github.com/{repo}",
            html_url=data["html_url"],
            parent_id=parent_id,
            blocked_by=blocked_by,
            comments=comments or [],
            created_at=created_at,
            updated_at=updated_at,
        )
