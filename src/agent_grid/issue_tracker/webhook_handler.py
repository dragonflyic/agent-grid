"""GitHub webhook processing."""

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from ..config import settings
from ..execution_grid import EventType, event_bus

logger = logging.getLogger("agent_grid.webhook")

webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def verify_signature(payload: bytes, signature: str | None, secret: str) -> bool:
    """Verify GitHub webhook signature."""
    if not signature or not secret:
        return False

    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(f"sha256={expected}", signature)


@webhook_router.post("/github")
async def handle_github_webhook(
    request: Request,
    x_github_event: str = Header(..., alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
) -> dict[str, Any]:
    """
    Handle incoming GitHub webhooks.

    Processes issue events and publishes them to the event bus.
    """
    payload = await request.body()

    # Verify signature if secret is configured
    if settings.github_webhook_secret:
        if not verify_signature(payload, x_hub_signature_256, settings.github_webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid signature")

    # Parse JSON from already-read payload (don't re-read stream with request.json())
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(f"Failed to parse webhook JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    try:
        if x_github_event == "issues":
            await _handle_issue_event(data)
        elif x_github_event == "issue_comment":
            await _handle_issue_comment_event(data)
        elif x_github_event == "pull_request_review":
            await _handle_pr_review_event(data)
        elif x_github_event == "pull_request_review_comment":
            await _handle_pr_review_comment_event(data)
        elif x_github_event == "pull_request":
            await _handle_pr_event(data)
        elif x_github_event == "ping":
            return {"status": "pong"}
    except Exception:
        logger.exception(f"Error processing {x_github_event} webhook")
        raise HTTPException(status_code=500, detail="Webhook processing failed")

    return {"status": "ok"}


async def _handle_issue_event(data: dict[str, Any]) -> None:
    """Handle issue opened/edited/closed events."""
    action = data.get("action")
    issue = data.get("issue", {})
    repo = data.get("repository", {})

    repo_full_name = repo.get("full_name")
    issue_number = issue.get("number")

    if not repo_full_name or issue_number is None:
        logger.warning(f"Invalid issue event: repo={repo_full_name}, issue={issue_number}")
        return

    if action == "opened":
        await event_bus.publish(
            EventType.ISSUE_CREATED,
            {
                "repo": repo_full_name,
                "issue_id": str(issue_number),
                "title": issue.get("title"),
                "body": issue.get("body"),
                "labels": [label["name"] for label in issue.get("labels", [])],
                "html_url": issue.get("html_url"),
            },
        )
    elif action in ("edited", "labeled", "unlabeled", "assigned", "closed", "reopened"):
        await event_bus.publish(
            EventType.ISSUE_UPDATED,
            {
                "repo": repo_full_name,
                "issue_id": str(issue_number),
                "action": action,
                "title": issue.get("title"),
                "body": issue.get("body"),
                "state": issue.get("state"),
                "labels": [label["name"] for label in issue.get("labels", [])],
            },
        )


async def _handle_issue_comment_event(data: dict[str, Any]) -> None:
    """Handle issue comment events.

    Publishes ISSUE_COMMENT for human comments on ag/* issues,
    and NUDGE_REQUESTED for @agent-grid nudge commands.
    """
    action = data.get("action")
    issue = data.get("issue", {})
    comment = data.get("comment", {})
    repo = data.get("repository", {})

    if action != "created":
        return

    repo_full_name = repo.get("full_name")
    issue_number = issue.get("number")

    if not repo_full_name or issue_number is None:
        return
    comment_body = comment.get("body", "")
    labels = [label["name"] for label in issue.get("labels", [])]
    is_pull_request = "pull_request" in issue

    # Check for nudge commands in comments
    if "@agent-grid nudge" in comment_body.lower():
        await event_bus.publish(
            EventType.NUDGE_REQUESTED,
            {
                "repo": repo_full_name,
                "issue_id": str(issue_number),
                "source": "comment",
                "comment_body": comment_body,
            },
        )
        return

    # Publish comment event for ag/* issues so scheduler can react
    has_ag_label = any(label.startswith("ag/") for label in labels)
    if has_ag_label:
        await event_bus.publish(
            EventType.ISSUE_COMMENT,
            {
                "repo": repo_full_name,
                "issue_id": str(issue_number),
                "comment_body": comment_body,
                "labels": labels,
                "is_pull_request": is_pull_request,
                "comment_author": comment.get("user", {}).get("login", ""),
            },
        )


async def _handle_pr_review_event(data: dict[str, Any]) -> None:
    """Handle PR review submission."""
    action = data.get("action")
    if action != "submitted":
        return
    pr = data.get("pull_request", {})
    head_branch = pr.get("head", {}).get("ref", "")
    if not head_branch.startswith("agent/"):
        return
    repo = data.get("repository", {}).get("full_name", "")
    await event_bus.publish(
        EventType.PR_REVIEW,
        {
            "repo": repo,
            "pr_number": pr.get("number"),
            "branch": head_branch,
            "review_state": data.get("review", {}).get("state"),
        },
    )


async def _handle_pr_review_comment_event(data: dict[str, Any]) -> None:
    """Handle inline PR review comments (comments on specific lines of code)."""
    action = data.get("action")
    if action != "created":
        return
    pr = data.get("pull_request", {})
    head_branch = pr.get("head", {}).get("ref", "")
    if not head_branch.startswith("agent/"):
        return
    repo = data.get("repository", {}).get("full_name", "")
    await event_bus.publish(
        EventType.PR_REVIEW,
        {
            "repo": repo,
            "pr_number": pr.get("number"),
            "branch": head_branch,
            "review_state": "commented",
        },
    )


async def _handle_pr_event(data: dict[str, Any]) -> None:
    """Handle PR closed/merged events."""
    action = data.get("action")
    if action != "closed":
        return
    pr = data.get("pull_request", {})
    head_branch = pr.get("head", {}).get("ref", "")
    if not head_branch.startswith("agent/"):
        return
    repo = data.get("repository", {}).get("full_name", "")
    merged = pr.get("merged", False)
    await event_bus.publish(
        EventType.PR_CLOSED,
        {
            "repo": repo,
            "pr_number": pr.get("number"),
            "branch": head_branch,
            "merged": merged,
        },
    )
