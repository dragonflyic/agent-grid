"""GitHub API implementation of issue tracker."""

import logging
import re
from datetime import datetime

import httpx

from .public_api import Comment, IssueInfo, IssueStatus, IssueTracker

logger = logging.getLogger("agent_grid.github_client")


class GitHubClient(IssueTracker):
    """
    GitHub implementation of the issue tracker interface.

    Uses GitHub's first-class sub-issues API for parent/child relationships.
    See: https://docs.github.com/en/rest/issues/sub-issues

    Conventions for blocking relationships (stored in issue body):
    - Blocked by: "Blocked by: #1, #2, #3" on its own line

    Labels used:
    - "in-progress": marks an issue as in progress
    """

    BASE_URL = "https://api.github.com"
    IN_PROGRESS_LABEL = "in-progress"

    # Regex patterns for parsing blocking relationships from issue body
    BLOCKED_BY_PATTERN = re.compile(r"^Blocked by:\s*(.+)$", re.MULTILINE)
    ISSUE_REF_PATTERN = re.compile(r"#(\d+)")

    def __init__(self, token: str | None = None):
        self._static_token = token  # static token override (for tests)
        self._app_auth = None  # lazy-loaded
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def _ensure_auth(self) -> None:
        """Ensure the client has a valid Authorization header."""
        if self._static_token:
            self._client.headers["Authorization"] = f"Bearer {self._static_token}"
            return
        from ..github_app import get_github_app_auth

        if self._app_auth is None:
            self._app_auth = get_github_app_auth()
        token = await self._app_auth.get_installation_token()
        self._client.headers["Authorization"] = f"Bearer {token}"

    async def get_issue(self, repo: str, issue_id: str) -> IssueInfo:
        """Get information about a GitHub issue including comments and parent."""
        await self._ensure_auth()
        # Fetch issue
        response = await self._client.get(f"/repos/{repo}/issues/{issue_id}")
        response.raise_for_status()
        data = response.json()

        # Fetch parent issue if this is a sub-issue
        parent_id = await self._fetch_parent_id(repo, issue_id)

        # Fetch comments
        comments = await self._fetch_comments(repo, issue_id)

        return self._parse_issue(repo, data, comments=comments, parent_id=parent_id)

    async def _fetch_parent_id(self, repo: str, issue_id: str) -> str | None:
        """Fetch the parent issue ID using GitHub's sub-issues API."""
        try:
            response = await self._client.get(f"/repos/{repo}/issues/{issue_id}/parent")
            if response.status_code == 200:
                parent_data = response.json()
                return str(parent_data["number"])
        except Exception:
            pass
        return None

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
                created_at = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                user = item.get("user") or {}
                comments.append(
                    Comment(
                        id=str(item["id"]),
                        body=item["body"] or "",
                        author=user.get("login", ""),
                        author_type=user.get("type", ""),
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
        await self._ensure_auth()
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

        issues = []
        page = 1

        while True:
            params["page"] = page
            response = await self._client.get(f"/repos/{repo}/issues", params=params)
            response.raise_for_status()
            data = response.json()

            if not data:
                break

            for item in data:
                # Skip pull requests (GitHub API returns them with issues)
                if "pull_request" in item:
                    continue
                issue = self._parse_issue(repo, item)

                # Additional filter for in-progress status
                if status == IssueStatus.IN_PROGRESS and issue.status != IssueStatus.IN_PROGRESS:
                    continue

                issues.append(issue)

            if len(data) < 100:
                break
            page += 1

        return issues

    async def list_subissues(self, repo: str, parent_id: str) -> list[IssueInfo]:
        """List all subissues of a parent issue using GitHub's sub-issues API."""
        await self._ensure_auth()
        subissues = []
        page = 1

        while True:
            response = await self._client.get(
                f"/repos/{repo}/issues/{parent_id}/sub_issues",
                params={"per_page": 100, "page": page},
            )
            response.raise_for_status()
            data = response.json()

            if not data:
                break

            for item in data:
                issue = self._parse_issue(repo, item, parent_id=parent_id)
                subissues.append(issue)

            if len(data) < 100:
                break
            page += 1

        return subissues

    async def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        blocked_by: list[str] | None = None,
    ) -> IssueInfo:
        """Create a new issue."""
        await self._ensure_auth()
        # Build body with blocking metadata (parent is handled via sub-issues API)
        full_body = self._build_body(body, blocked_by)

        response = await self._client.post(
            f"/repos/{repo}/issues",
            json={
                "title": title,
                "body": full_body,
                "labels": labels if labels else None,
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
        """Create a subissue under a parent issue using GitHub's sub-issues API."""
        await self._ensure_auth()
        # First create the issue and get the raw response to extract GitHub's issue ID
        full_body = self._build_body(body, None)

        create_response = await self._client.post(
            f"/repos/{repo}/issues",
            json={
                "title": title,
                "body": full_body,
                "labels": labels if labels else None,
            },
        )
        create_response.raise_for_status()
        issue_data = create_response.json()

        # Get the GitHub issue ID (not the number)
        github_issue_id = issue_data["id"]

        # Add it as a sub-issue to the parent using the GitHub ID
        response = await self._client.post(
            f"/repos/{repo}/issues/{parent_id}/sub_issues",
            json={"sub_issue_id": github_issue_id},
        )
        response.raise_for_status()

        # Return the parsed issue with parent_id set
        issue = self._parse_issue(repo, issue_data, parent_id=parent_id)
        return issue

    async def update_issue(
        self,
        repo: str,
        issue_id: str,
        title: str | None = None,
        body: str | None = None,
        status: IssueStatus | None = None,
        labels: list[str] | None = None,
        blocked_by: list[str] | None = None,
    ) -> IssueInfo:
        """Update an issue's fields.

        Note: parent_id is managed via GitHub's sub-issues API, not here.
        """
        await self._ensure_auth()
        # Get current issue to preserve unspecified fields
        current = await self.get_issue(repo, issue_id)

        update_data: dict = {}

        if title is not None:
            update_data["title"] = title

        # Handle body update - need to preserve/update blocking metadata
        if body is not None or blocked_by is not None:
            new_body = body if body is not None else self._strip_metadata(current.body or "")
            new_blocked = blocked_by if blocked_by is not None else current.blocked_by
            update_data["body"] = self._build_body(new_body, new_blocked)

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
        await self._ensure_auth()
        response = await self._client.post(
            f"/repos/{repo}/issues/{issue_id}/comments",
            json={"body": body},
        )
        response.raise_for_status()

    async def update_issue_status(self, repo: str, issue_id: str, status: IssueStatus) -> None:
        """Update the status of an issue."""
        await self._ensure_auth()
        await self.update_issue(repo, issue_id, status=status)

    async def get_actions_job_logs(self, repo: str, job_id: int) -> str:
        """Download logs for a GitHub Actions job.

        The Actions API returns a 302 redirect to a log download URL.
        We follow the redirect and return the log text, truncated to
        the last ~3000 chars (the tail is most useful for build errors).
        """
        await self._ensure_auth()
        try:
            response = await self._client.get(
                f"/repos/{repo}/actions/jobs/{job_id}/logs",
                follow_redirects=True,
            )
            if response.status_code == 200:
                logs = response.text
                # Return tail — build errors are at the end
                if len(logs) > 3000:
                    return f"... (truncated)\n{logs[-3000:]}"
                return logs
        except Exception:
            pass
        return ""

    async def assign_issue(self, repo: str, issue_id: str, assignee: str) -> None:
        """Assign an issue to a user."""
        await self._ensure_auth()
        if not assignee:
            return
        try:
            await self._client.post(
                f"/repos/{repo}/issues/{issue_id}/assignees",
                json={"assignees": [assignee]},
            )
        except Exception as e:
            logger.warning(f"Failed to assign issue #{issue_id} to {assignee}: {e}")

    async def request_pr_reviewers(self, repo: str, pr_number: int, reviewers: list[str]) -> None:
        """Request reviewers on a pull request."""
        await self._ensure_auth()
        if not reviewers:
            return
        try:
            await self._client.post(
                f"/repos/{repo}/pulls/{pr_number}/requested_reviewers",
                json={"reviewers": reviewers},
            )
        except Exception as e:
            logger.warning(f"Failed to request reviewers on PR #{pr_number}: {e}")

    async def create_pr(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        labels: list[str] | None = None,
        reviewers: list[str] | None = None,
    ) -> dict | None:
        """Create a pull request. Returns the PR data dict or None on failure."""
        await self._ensure_auth()
        try:
            response = await self._client.post(
                f"/repos/{repo}/pulls",
                json={
                    "title": title,
                    "body": body,
                    "head": head,
                    "base": base,
                },
            )
            response.raise_for_status()
            pr_data = response.json()
            pr_number = pr_data["number"]

            if labels:
                for label in labels:
                    await self.add_label(repo, str(pr_number), label)

            if reviewers:
                await self.request_pr_reviewers(repo, pr_number, reviewers)

            return pr_data
        except Exception as e:
            logger.error(f"Failed to create PR for {repo} from {head}: {e}")
            return None

    async def get_pr_by_branch(self, repo: str, branch: str) -> dict | None:
        """Find an open PR for the given head branch.

        Returns the raw PR dict or None if no PR exists for that branch.
        """
        await self._ensure_auth()
        owner = repo.split("/")[0]
        try:
            response = await self._client.get(
                f"/repos/{repo}/pulls",
                params={"head": f"{owner}:{branch}", "state": "open", "per_page": 1},
            )
            response.raise_for_status()
            prs = response.json()
            return prs[0] if prs else None
        except Exception as e:
            logger.warning(f"Failed to look up PR for branch {branch}: {e}")
            return None

    async def add_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
        """Post a comment on a pull request."""
        await self._ensure_auth()
        try:
            await self._client.post(
                f"/repos/{repo}/issues/{pr_number}/comments",
                json={"body": body},
            )
        except Exception as e:
            logger.warning(f"Failed to comment on PR #{pr_number}: {e}")

    async def get_check_runs_for_ref(
        self,
        repo: str,
        ref: str,
        *,
        status: str = "completed",
    ) -> list[dict]:
        """Return check runs for a commit SHA or branch ref.

        Calls GET /repos/{repo}/commits/{ref}/check-runs.
        Returns the raw list of check_run dicts (empty list on error).
        """
        await self._ensure_auth()
        try:
            response = await self._client.get(
                f"/repos/{repo}/commits/{ref}/check-runs",
                params={"status": status, "per_page": 100},
            )
            response.raise_for_status()
            return response.json().get("check_runs", [])
        except Exception as e:
            logger.warning(f"Failed to fetch check runs for {repo}@{ref}: {e}")
            return []

    async def list_open_prs(self, repo: str, **params) -> list[dict]:
        """List open pull requests."""
        await self._ensure_auth()
        try:
            response = await self._client.get(
                f"/repos/{repo}/pulls",
                params={"state": "open", **params},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to fetch PRs for {repo}: {e}")
            return []

    async def get_pr_reviews(self, repo: str, pr_number: int) -> list[dict]:
        """Get reviews for a pull request."""
        await self._ensure_auth()
        try:
            response = await self._client.get(
                f"/repos/{repo}/pulls/{pr_number}/reviews",
                params={"per_page": 100},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to fetch reviews for PR #{pr_number}: {e}")
            return []

    async def get_pr_comments(self, repo: str, pr_number: int) -> list[dict]:
        """Get inline/review comments for a pull request."""
        await self._ensure_auth()
        try:
            response = await self._client.get(
                f"/repos/{repo}/pulls/{pr_number}/comments",
                params={"per_page": 100},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to fetch comments for PR #{pr_number}: {e}")
            return []

    async def get_pr_data(self, repo: str, pr_number: int) -> dict | None:
        """Fetch a single PR by number."""
        await self._ensure_auth()
        try:
            response = await self._client.get(f"/repos/{repo}/pulls/{pr_number}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to fetch PR #{pr_number}: {e}")
            return None

    async def get_issue_comments_since(self, repo: str, issue_id: str, since: str | None = None) -> list[dict]:
        """Fetch issue comments, optionally since a timestamp."""
        await self._ensure_auth()
        try:
            params: dict = {"per_page": 50}
            if since:
                params["since"] = since
            response = await self._client.get(
                f"/repos/{repo}/issues/{issue_id}/comments",
                params=params,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to fetch comments for issue #{issue_id}: {e}")
            return []

    async def add_label(self, repo: str, issue_id: str, label: str) -> None:
        """Add a label to an issue."""
        await self._ensure_auth()
        try:
            await self._client.post(
                f"/repos/{repo}/issues/{issue_id}/labels",
                json={"labels": [label]},
            )
        except Exception:
            pass  # Label may already exist or not be valid

    async def remove_label(self, repo: str, issue_id: str, label: str) -> None:
        """Remove a label from an issue."""
        await self._ensure_auth()
        try:
            await self._client.delete(
                f"/repos/{repo}/issues/{issue_id}/labels/{label}",
            )
        except Exception:
            pass  # Label may not exist

    async def create_label(self, repo: str, name: str, color: str) -> bool:
        """Create a label in the repo. Returns True if created."""
        await self._ensure_auth()
        try:
            resp = await self._client.post(
                f"/repos/{repo}/labels",
                json={"name": name, "color": color},
            )
            resp.raise_for_status()
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 422:
                return False  # Already exists
            logger.warning(f"Failed to create label {name}: {e.response.status_code}")
            return False
        except Exception as e:
            logger.error(f"Error creating label {name}: {e}")
            return False

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    # Keep private aliases for internal use in update_issue
    async def _add_label(self, repo: str, issue_id: str, label: str) -> None:
        await self.add_label(repo, issue_id, label)

    async def _remove_label(self, repo: str, issue_id: str, label: str) -> None:
        await self.remove_label(repo, issue_id, label)

    def _build_body(
        self,
        body: str,
        blocked_by: list[str] | None,
    ) -> str:
        """Build issue body with blocking metadata."""
        parts = []

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
        # Remove Blocked by: line
        body = self.BLOCKED_BY_PATTERN.sub("", body)
        # Clean up extra whitespace at the start
        return body.lstrip("\n")

    def _parse_issue(
        self,
        repo: str,
        data: dict,
        comments: list[Comment] | None = None,
        parent_id: str | None = None,
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

        # Extract author
        user = data.get("user") or {}
        author = user.get("login", "")

        # Extract assignees and node_id
        assignees = [a["login"] for a in data.get("assignees", []) if a.get("login")]
        node_id = data.get("node_id")

        return IssueInfo(
            id=str(data["number"]),
            number=data["number"],
            title=data["title"],
            body=self._strip_metadata(body) or None,
            author=author,
            status=status,
            labels=labels,
            assignees=assignees,
            node_id=node_id,
            repo_url=f"https://github.com/{repo}",
            html_url=data["html_url"],
            parent_id=parent_id,
            blocked_by=blocked_by,
            comments=comments or [],
            created_at=created_at,
            updated_at=updated_at,
        )
