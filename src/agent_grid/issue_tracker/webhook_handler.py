"""GitHub webhook processing."""

import hashlib
import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from ..common import event_bus, EventType
from ..config import settings

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

    data = await request.json()

    # Handle different event types
    if x_github_event == "issues":
        await _handle_issue_event(data)
    elif x_github_event == "issue_comment":
        await _handle_issue_comment_event(data)
    elif x_github_event == "ping":
        return {"status": "pong"}

    return {"status": "ok"}


async def _handle_issue_event(data: dict[str, Any]) -> None:
    """Handle issue opened/edited/closed events."""
    action = data.get("action")
    issue = data.get("issue", {})
    repo = data.get("repository", {})

    repo_full_name = repo.get("full_name", "")
    issue_number = issue.get("number")

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
    """Handle issue comment events."""
    action = data.get("action")
    issue = data.get("issue", {})
    comment = data.get("comment", {})
    repo = data.get("repository", {})

    if action != "created":
        return

    repo_full_name = repo.get("full_name", "")
    issue_number = issue.get("number")
    comment_body = comment.get("body", "")

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
