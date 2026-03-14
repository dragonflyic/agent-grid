"""Phase 2: Classify issues using Claude API.

Calls Claude to classify each issue as SIMPLE, COMPLEX, BLOCKED, or SKIP.
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


class Classification:
    """Result of classifying an issue."""

    def __init__(
        self,
        category: str,
        reason: str,
        blocking_question: str | None = None,
        estimated_complexity: int = 5,
        dependencies: list[int] | None = None,
    ):
        self.category = category
        self.reason = reason
        self.blocking_question = blocking_question
        self.estimated_complexity = estimated_complexity
        self.dependencies = dependencies or []


CLASSIFICATION_PROMPT = """You are a senior tech lead. Given this GitHub issue, classify it.

Issue Title: {title}
Issue Body:
{body}

Labels: {labels}
{reference_statuses}
Classify as ONE of:
A. SIMPLE — Single PR by one agent. < 200 lines changed, single concern.
B. COMPLEX — Needs decomposition. Multiple files/concerns, needs a plan.
   Use this when you'd want to confirm the approach with the user first.
C. BLOCKED — Genuinely needs human input that a developer with full codebase
   access CANNOT figure out. Examples: credentials needed, business policy
   decisions, external service access, choosing between fundamentally different
   product directions. Do NOT use BLOCKED for vague descriptions — the agent
   can read the codebase and figure out what to do.
   IMPORTANT: Do NOT classify as BLOCKED if the issue mentions dependencies on
   other issues/PRs that are already CLOSED or MERGED. A resolved dependency
   is not a blocker.
D. SKIP — Not suitable for AI (too risky, needs domain expertise beyond code,
   or completely nonsensical with no actionable work).

Respond as JSON:
{{
  "category": "SIMPLE" | "COMPLEX" | "BLOCKED" | "SKIP",
  "reason": "one sentence explaining why",
  "blocking_question": "question for human, only if BLOCKED",
  "estimated_complexity": 1-10,
  "dependencies": [list of issue numbers this depends on, if any]
}}

Respond ONLY with the JSON object, no markdown fences."""


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
                lines = [l for l in lines if not l.strip().startswith("```")]
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

    async def classify(self, issue: IssueInfo, repo: str | None = None) -> Classification:
        """Classify a single issue.

        Args:
            issue: The issue to classify.
            repo: Repository in owner/name format. When provided, referenced
                  issues/PRs in the body are resolved to their current status
                  so the LLM can make informed dependency decisions.
        """
        reference_statuses = ""
        if repo:
            reference_statuses = await self._resolve_references(issue, repo)

        prompt = CLASSIFICATION_PROMPT.format(
            title=issue.title,
            body=issue.body or "(no description)",
            labels=", ".join(issue.labels) if issue.labels else "(none)",
            reference_statuses=reference_statuses,
        )

        try:
            response = await self._client.messages.create(
                model=settings.classification_model,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.content[0].text.strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                lines = text.split("\n")
                # Remove first line (```json) and last line (```)
                lines = [line for line in lines if not line.strip().startswith("```")]
                text = "\n".join(lines).strip()
            data = json.loads(text)

            classification = Classification(
                category=data["category"],
                reason=data.get("reason", ""),
                blocking_question=data.get("blocking_question"),
                estimated_complexity=data.get("estimated_complexity", 5),
                dependencies=data.get("dependencies", []),
            )
            logger.info(f"Issue #{issue.number}: classified as {classification.category} — {classification.reason}")
            return classification

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"Failed to parse classification for issue #{issue.number}: {e}")
            return Classification(category="SKIP", reason="Classification parse error, defaulting to SKIP")

        except Exception as e:
            logger.error(f"Classification API error for issue #{issue.number}: {e}")
            return Classification(category="SKIP", reason=f"Classification error: {e}")

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
