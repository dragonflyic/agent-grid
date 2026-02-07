"""Phase 2: Classify issues using Claude API.

Calls Claude to classify each issue as SIMPLE, COMPLEX, BLOCKED, or SKIP.
"""

import json
import logging

import anthropic

from ..config import settings
from ..issue_tracker.public_api import IssueInfo

logger = logging.getLogger("agent_grid.classifier")


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

Classify as ONE of:
A. SIMPLE — Can be done in a single PR by one agent. Estimated: < 200 lines changed, single concern, clear scope.
B. COMPLEX — Needs decomposition into sub-tasks. Estimated: multiple files/concerns, needs a plan first.
C. BLOCKED — Missing information, ambiguous requirements, needs human clarification before work can begin.
D. SKIP — Not suitable for AI (too creative, too risky, requires domain expertise beyond code).

Respond as JSON:
{{
  "category": "SIMPLE" | "COMPLEX" | "BLOCKED" | "SKIP",
  "reason": "one sentence explaining why",
  "blocking_question": "question for human, only if BLOCKED",
  "estimated_complexity": 1-10,
  "dependencies": [list of issue numbers this depends on, if any]
}}

Respond ONLY with the JSON object, no markdown fences."""


class Classifier:
    """Classifies GitHub issues using Claude API."""

    def __init__(self):
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def classify(self, issue: IssueInfo) -> Classification:
        """Classify a single issue."""
        prompt = CLASSIFICATION_PROMPT.format(
            title=issue.title,
            body=issue.body or "(no description)",
            labels=", ".join(issue.labels) if issue.labels else "(none)",
        )

        try:
            response = await self._client.messages.create(
                model=settings.classification_model,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.content[0].text.strip()
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
            return Classification(category="SIMPLE", reason="Classification parse error, defaulting to SIMPLE")

        except Exception as e:
            logger.error(f"Classification API error for issue #{issue.number}: {e}")
            return Classification(category="SKIP", reason=f"Classification error: {e}")


_classifier: Classifier | None = None


def get_classifier() -> Classifier:
    global _classifier
    if _classifier is None:
        _classifier = Classifier()
    return _classifier
