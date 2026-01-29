"""GitHub webhook processing with deduplication support.

Webhooks are stored in a queue for background processing rather than
being published directly to the event bus. This allows for:
- Deduplication of duplicate webhook deliveries (using X-GitHub-Delivery)
- Coalescing multiple events for the same issue (e.g., opened + labeled)
- Smart decision making (e.g., don't launch if opened then closed)
"""

import hashlib
import hmac
import json
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request

from ..config import settings
from ..coordinator.database import get_database
from ..coordinator.public_api import WebhookEvent, utc_now

logger = logging.getLogger("agent_grid.webhook_handler")

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
    x_github_delivery: str | None = Header(None, alias="X-GitHub-Delivery"),
) -> dict[str, Any]:
    """
    Handle incoming GitHub webhooks.

    Instead of processing events immediately, this stores them in a queue
    for background processing. The background processor will:
    - Deduplicate events by delivery ID
    - Coalesce multiple events for the same issue
    - Make smart decisions about when to launch agents

    The X-GitHub-Delivery header is used as an idempotency key to prevent
    duplicate processing if GitHub retries webhook delivery.
    """
    payload = await request.body()

    # Verify signature if secret is configured
    if settings.github_webhook_secret:
        if not verify_signature(payload, x_hub_signature_256, settings.github_webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid signature")

    # Handle ping immediately (no need to queue)
    if x_github_event == "ping":
        return {"status": "pong"}

    data = await request.json()

    # Generate delivery ID if not provided (for testing)
    delivery_id = x_github_delivery or str(uuid4())

    # Extract common fields
    action = data.get("action")
    repo = data.get("repository", {})
    repo_full_name = repo.get("full_name", "")

    # Get issue ID based on event type
    issue_id: str | None = None
    if x_github_event == "issues":
        issue = data.get("issue", {})
        issue_id = str(issue.get("number")) if issue.get("number") else None
    elif x_github_event == "issue_comment":
        issue = data.get("issue", {})
        issue_id = str(issue.get("number")) if issue.get("number") else None

    # Store in webhook queue for background processing
    webhook_event = WebhookEvent(
        id=uuid4(),
        delivery_id=delivery_id,
        event_type=x_github_event,
        action=action,
        repo=repo_full_name or None,
        issue_id=issue_id,
        payload=json.dumps(data),
        processed=False,
        received_at=utc_now(),
    )

    db = get_database()
    inserted = await db.create_webhook_event(webhook_event)

    if inserted:
        logger.info(
            f"Queued webhook event: delivery_id={delivery_id}, "
            f"type={x_github_event}, action={action}, "
            f"repo={repo_full_name}, issue={issue_id}"
        )
        return {"status": "queued", "delivery_id": delivery_id}
    else:
        # Duplicate delivery - idempotent response
        logger.debug(f"Duplicate webhook ignored: delivery_id={delivery_id}")
        return {"status": "duplicate", "delivery_id": delivery_id}
