"""Sanity-check issues using Claude API.

Quick LLM triage: PROCEED (send to scout agent) or SKIP (nonsense/spam).
"""

import json
import logging
import re

import anthropic

from ..config import settings
from ..issue_tracker.public_api import IssueInfo

logger = logging.getLogger("agent_grid.classifier")


class SanityResult:
    """Result of the sanity check."""

    def __init__(self, verdict: str, reason: str):
        self.verdict = verdict  # "PROCEED" or "SKIP"
        self.reason = reason


SANITY_CHECK_PROMPT = """You are triaging a GitHub issue for an automated coding agent.

Issue Title: {title}
Issue Body:
{body}

Labels: {labels}

Decide: should this issue be sent to a coding agent for exploration and implementation?

Answer SKIP if the issue is:
- Completely nonsensical or spam
- A discussion/question with no actionable work
- Requesting access, credentials, or admin actions that code can't solve
- A duplicate of another issue

Answer PROCEED for everything else — even if the issue is vague, complex, or
might turn out to be infeasible. The coding agent will explore the codebase
and figure out the right approach.

Respond as JSON:
{{
  "verdict": "PROCEED" | "SKIP",
  "reason": "one sentence explaining why"
}}

Respond ONLY with the JSON object, no markdown fences."""


class Classifier:
    """Classifies GitHub issues using Claude API."""

    def __init__(self):
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def sanity_check(self, issue: IssueInfo) -> SanityResult:
        """Quick LLM check: is this issue actionable or nonsense?"""
        prompt = SANITY_CHECK_PROMPT.format(
            title=issue.title,
            body=issue.body or "(no description)",
            labels=", ".join(issue.labels) if issue.labels else "(none)",
        )
        try:
            response = await self._client.messages.create(
                model=settings.classification_model,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [line for line in lines if not line.strip().startswith("```")]
                text = "\n".join(lines).strip()
            data = json.loads(text)
            result = SanityResult(
                verdict=data.get("verdict", "PROCEED"),
                reason=data.get("reason", ""),
            )
            logger.info(f"Issue #{issue.number}: sanity check {result.verdict} — {result.reason}")
            return result
        except Exception as e:
            logger.error(f"Sanity check failed for issue #{issue.number}: {e}")
            return SanityResult(verdict="PROCEED", reason="Sanity check error, defaulting to PROCEED")

    async def _resolve_references(self, issue: IssueInfo, repo: str) -> str:
        """Resolve #N references in the issue body to their current status.

        Fetches each referenced issue/PR from GitHub and returns a formatted
        block for the classification prompt, e.g.:

            Referenced issues/PRs (current status):
            - #2100 (Introduce a browser hosting abstraction): MERGED
            - #1500 (Add caching layer): OPEN
        """
        if not issue.body:
            return ""

        refs = set(re.findall(r"#(\d+)", issue.body))
        refs.discard(str(issue.number))  # skip self-references

        if not refs:
            return ""

        # Cap at 10 to avoid excessive API calls
        sorted_refs = sorted(refs, key=int)[:10]

        from ..issue_tracker import get_issue_tracker

        tracker = get_issue_tracker()
        statuses: list[str] = []

        for ref_num in sorted_refs:
            ref_status = await tracker.get_reference_status(repo, ref_num)
            title = ref_status["title"]
            status = ref_status["status"]
            if title:
                title_short = title[:80]
                statuses.append(f"- #{ref_num} ({title_short}): {status}")
            else:
                statuses.append(f"- #{ref_num}: {status}")

        if not statuses:
            return ""

        return "\nReferenced issues/PRs (current status):\n" + "\n".join(statuses) + "\n"


_classifier: Classifier | None = None


def get_classifier() -> Classifier:
    global _classifier
    if _classifier is None:
        _classifier = Classifier()
    return _classifier
