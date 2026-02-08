"""Filesystem-based issue tracker using markdown files."""

import re
from datetime import datetime
from pathlib import Path

import yaml

from ..config import settings
from .public_api import Comment, IssueInfo, IssueStatus, IssueTracker, utc_now


class FilesystemClient(IssueTracker):
    """
    Filesystem implementation of the issue tracker interface.

    Issues are stored as markdown files with YAML frontmatter:

    ```markdown
    ---
    id: 1
    title: "Issue title"
    status: open
    labels:
      - bug
    parent_id: null
    blocked_by:
      - 2
    created_at: 2024-01-15T10:00:00Z
    updated_at: 2024-01-15T10:00:00Z
    ---

    Issue description goes here.

    ## Comments

    ### 2024-01-15T10:30:00Z
    First comment text

    ### 2024-01-15T11:00:00Z
    Second comment text
    ```
    """

    FRONTMATTER_PATTERN = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
    COMMENTS_PATTERN = re.compile(r"## Comments\n(.*)", re.DOTALL)
    COMMENT_PATTERN = re.compile(
        r"### (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\n(.*?)(?=\n### |\Z)",
        re.DOTALL,
    )

    def __init__(self, issues_dir: str | Path | None = None):
        self._issues_dir = Path(issues_dir or settings.issues_directory)
        self._issues_dir.mkdir(parents=True, exist_ok=True)
        self._next_id_file = self._issues_dir / ".next_id"

    def _get_next_id(self) -> int:
        """Get and increment the next issue ID."""
        if self._next_id_file.exists():
            current = int(self._next_id_file.read_text().strip())
        else:
            current = 1
        self._next_id_file.write_text(str(current + 1))
        return current

    def _issue_path(self, issue_id: str) -> Path:
        """Get the file path for an issue."""
        return self._issues_dir / f"{issue_id}.md"

    def _parse_issue(self, issue_id: str, content: str) -> IssueInfo:
        """Parse a markdown file into an IssueInfo."""
        # Extract frontmatter
        frontmatter_match = self.FRONTMATTER_PATTERN.match(content)
        if not frontmatter_match:
            raise ValueError(f"Invalid issue format: missing frontmatter in {issue_id}")

        frontmatter = yaml.safe_load(frontmatter_match.group(1))
        body_start = frontmatter_match.end()

        # Extract body (everything after frontmatter, before ## Comments)
        remaining = content[body_start:]
        comments_match = self.COMMENTS_PATTERN.search(remaining)

        if comments_match:
            body = remaining[: comments_match.start()].strip()
            comments_section = comments_match.group(1)
        else:
            body = remaining.strip()
            comments_section = ""

        # Parse comments
        comments: list[Comment] = []
        for match in self.COMMENT_PATTERN.finditer(comments_section):
            timestamp_str = match.group(1)
            comment_body = match.group(2).strip()

            # Parse timestamp
            timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))

            comments.append(
                Comment(
                    id=str(len(comments) + 1),
                    body=comment_body,
                    created_at=timestamp,
                )
            )

        # Parse timestamps from frontmatter
        created_at = frontmatter.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        updated_at = frontmatter.get("updated_at")
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))

        # Convert blocked_by to strings (YAML may parse as ints)
        blocked_by_raw = frontmatter.get("blocked_by", [])
        blocked_by = [str(b) for b in blocked_by_raw] if blocked_by_raw else []

        return IssueInfo(
            id=str(frontmatter.get("id", issue_id)),
            number=int(frontmatter.get("id", issue_id)),
            title=frontmatter.get("title", "Untitled"),
            body=body or None,
            status=IssueStatus(frontmatter.get("status", "open")),
            labels=frontmatter.get("labels", []),
            repo_url=f"file://{self._issues_dir}",
            html_url=f"file://{self._issue_path(issue_id)}",
            parent_id=str(frontmatter["parent_id"]) if frontmatter.get("parent_id") else None,
            blocked_by=blocked_by,
            comments=comments,
            created_at=created_at,
            updated_at=updated_at,
        )

    def _serialize_issue(self, issue: IssueInfo) -> str:
        """Serialize an IssueInfo to markdown format."""
        frontmatter = {
            "id": int(issue.id),
            "title": issue.title,
            "status": issue.status.value,
            "labels": issue.labels,
            "parent_id": int(issue.parent_id) if issue.parent_id else None,
            "blocked_by": [int(b) for b in issue.blocked_by] if issue.blocked_by else [],
            "created_at": issue.created_at.isoformat() if issue.created_at else None,
            "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
        }

        parts = [
            "---",
            yaml.dump(frontmatter, default_flow_style=False, sort_keys=False).strip(),
            "---",
            "",
            issue.body or "",
        ]

        if issue.comments:
            parts.extend(["", "## Comments", ""])
            for comment in issue.comments:
                parts.extend(
                    [
                        f"### {comment.created_at.isoformat()}",
                        comment.body,
                        "",
                    ]
                )

        return "\n".join(parts)

    async def get_issue(self, repo: str, issue_id: str) -> IssueInfo:
        """Get information about an issue."""
        path = self._issue_path(issue_id)
        if not path.exists():
            raise FileNotFoundError(f"Issue {issue_id} not found")

        content = path.read_text()
        return self._parse_issue(issue_id, content)

    async def list_subissues(self, repo: str, parent_id: str) -> list[IssueInfo]:
        """List all subissues of a parent issue."""
        subissues: list[IssueInfo] = []

        for path in self._issues_dir.glob("*.md"):
            if path.name.startswith("."):
                continue

            issue_id = path.stem
            try:
                issue = await self.get_issue(repo, issue_id)
                if issue.parent_id == parent_id:
                    subissues.append(issue)
            except (ValueError, FileNotFoundError):
                continue

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
        return await self.create_issue(
            repo=repo,
            title=title,
            body=body,
            labels=labels,
            parent_id=parent_id,
        )

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
        issue_id = self._get_next_id()
        now = utc_now()

        issue = IssueInfo(
            id=str(issue_id),
            number=issue_id,
            title=title,
            body=body,
            status=IssueStatus.OPEN,
            labels=labels or [],
            repo_url=f"file://{self._issues_dir}",
            html_url=f"file://{self._issue_path(str(issue_id))}",
            parent_id=parent_id,
            blocked_by=blocked_by or [],
            comments=[],
            created_at=now,
            updated_at=now,
        )

        content = self._serialize_issue(issue)
        self._issue_path(str(issue_id)).write_text(content)

        return issue

    async def add_comment(self, repo: str, issue_id: str, body: str) -> None:
        """Add a comment to an issue."""
        issue = await self.get_issue(repo, issue_id)

        new_comment = Comment(
            id=str(len(issue.comments) + 1),
            body=body,
            created_at=utc_now(),
        )
        issue.comments.append(new_comment)
        issue.updated_at = utc_now()

        content = self._serialize_issue(issue)
        self._issue_path(issue_id).write_text(content)

    async def update_issue_status(self, repo: str, issue_id: str, status: IssueStatus) -> None:
        """Update the status of an issue."""
        issue = await self.get_issue(repo, issue_id)
        issue.status = status
        issue.updated_at = utc_now()

        content = self._serialize_issue(issue)
        self._issue_path(issue_id).write_text(content)

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
        issue = await self.get_issue(repo, issue_id)

        if title is not None:
            issue.title = title
        if body is not None:
            issue.body = body
        if status is not None:
            issue.status = status
        if labels is not None:
            issue.labels = labels
        if parent_id is not None:
            issue.parent_id = parent_id
        if blocked_by is not None:
            issue.blocked_by = blocked_by

        issue.updated_at = utc_now()

        content = self._serialize_issue(issue)
        self._issue_path(issue_id).write_text(content)

        return issue

    async def list_issues(
        self,
        repo: str,
        status: IssueStatus | None = None,
        labels: list[str] | None = None,
    ) -> list[IssueInfo]:
        """List all issues with optional filters."""
        issues: list[IssueInfo] = []

        for path in self._issues_dir.glob("*.md"):
            if path.name.startswith("."):
                continue

            issue_id = path.stem
            try:
                issue = await self.get_issue(repo, issue_id)

                # Apply filters
                if status and issue.status != status:
                    continue
                if labels and not set(labels).issubset(set(issue.labels)):
                    continue

                issues.append(issue)
            except (ValueError, FileNotFoundError):
                continue

        # Sort by ID
        issues.sort(key=lambda i: int(i.id))
        return issues

    async def get_blocked_issues(self, repo: str, issue_id: str) -> list[IssueInfo]:
        """Get issues that are blocked by the given issue."""
        blocked: list[IssueInfo] = []

        for path in self._issues_dir.glob("*.md"):
            if path.name.startswith("."):
                continue

            try:
                issue = await self.get_issue(repo, path.stem)
                if issue_id in issue.blocked_by:
                    blocked.append(issue)
            except (ValueError, FileNotFoundError):
                continue

        return blocked

    async def is_blocked(self, repo: str, issue_id: str) -> bool:
        """Check if an issue is blocked by any open issues."""
        issue = await self.get_issue(repo, issue_id)

        for blocker_id in issue.blocked_by:
            try:
                blocker = await self.get_issue(repo, blocker_id)
                if blocker.status != IssueStatus.CLOSED:
                    return True
            except FileNotFoundError:
                continue

        return False

    async def close(self) -> None:
        """Close any open connections (no-op for filesystem)."""
        pass
