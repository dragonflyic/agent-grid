"""Quality gate: confidence check before launching agents.

Runs a Claude call to assess whether an issue is clear and scoped enough
for an autonomous agent to handle without going rogue. Applies to both
ag/todo (opted-in) and proactive issues.

Lessons encoded from analysing 99 closed-without-merge agent PRs:
- "The Hydra": recursive decomposition of open-ended issues
- Wrong design choices on issues with multiple valid approaches
- Band-aid fixes on vague/screenshot-only issues
- 226K-line bombs on underspecified UI issues
"""

import json
import logging

import anthropic

from ..config import settings
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.public_api import IssueInfo

logger = logging.getLogger("agent_grid.quality_gate")


class ConfidenceAssessment:
    """Result of the quality gate evaluation."""

    def __init__(
        self,
        score: int,
        verdict: str,
        risk_flags: list[str],
        green_flags: list[str],
        explanation: str,
        clarification_question: str | None = None,
    ):
        self.score = score  # 1-10
        self.verdict = verdict  # "proceed", "clarify", "skip"
        self.risk_flags = risk_flags
        self.green_flags = green_flags
        self.explanation = explanation
        self.clarification_question = clarification_question


QUALITY_GATE_PROMPT = """You are a risk assessor for an autonomous coding agent system.
Given a GitHub issue, assess whether an AI coding agent can confidently solve it
and produce a PR that will actually get merged — without going rogue, wasting
resources, or making the wrong design choices.

Issue Title: {title}
Issue Body:
{body}

Labels: {labels}
Classification: {classification}
Is Sub-Issue: {is_sub_issue}
Nesting Depth: {nesting_depth} (0 = top-level, 1 = sub-issue, 2+ = deeply nested)

## RED FLAGS (lower the score)
- Open-ended scope ("fix all X", "clean up remaining", "address all errors")
- Vague description or screenshot-only with no acceptance criteria
- Multiple valid implementation approaches where picking wrong = PR rejected
  (e.g. "filter results" could mean change default behavior OR add query param)
- Modifying existing API/endpoint behavior that other code depends on
- Nesting depth >= 2 — sub-issue of sub-issue chains spiral out of control
- Author said "will revisit" or has exploratory/brainstorming tone
- Scope likely > 500 lines of changes
- No clear definition of done
- Issue body is empty or just a title
- Requires product/design decisions the agent cannot make

## GREEN FLAGS (raise the score)
- Exact files and changes specified with full paths
- Additive work: new file, new test, new config, new documentation
- Single concern, <200 lines expected
- Clear acceptance criteria or a checklist
- No design decisions needed — the "how" is obvious from the "what"
- Bug report with clear reproduction steps and expected behavior
- Well-defined API to implement (e.g. "add endpoint X that returns Y")
- Issue was written as a sub-task with specific implementation details

Respond as JSON:
{{
  "score": <1-10>,
  "verdict": "proceed" | "clarify" | "skip",
  "risk_flags": ["flag1", "flag2"],
  "green_flags": ["flag1", "flag2"],
  "explanation": "one sentence explaining the score",
  "clarification_question": "question for the issue author (only if verdict is clarify, else null)"
}}

Scoring guide:
- 9-10: Crystal clear. Agent knows exactly what to do. Will merge.
- 7-8: Mostly clear. Proceed for opted-in issues, skip for proactive.
- 4-6: Ambiguous. Ask the author for clarification before starting.
- 1-3: Too vague or risky. Skip entirely.

Respond ONLY with the JSON object, no markdown fences."""


class QualityGate:
    """Evaluates issue confidence before launching agents."""

    def __init__(self):
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._tracker = get_issue_tracker()

    async def evaluate(
        self,
        issue: IssueInfo,
        classification: str = "SIMPLE",
        is_proactive: bool = False,
    ) -> ConfidenceAssessment:
        """Evaluate an issue's confidence for agent execution.

        Args:
            issue: The GitHub issue to evaluate.
            classification: Classification category string (e.g. "SIMPLE").
            is_proactive: True if this is a proactive scan (higher bar).

        Returns:
            ConfidenceAssessment with score, verdict, and flags.
        """
        repo = issue.repo_url.replace("https://github.com/", "")
        nesting_depth = await self._compute_nesting_depth(issue, repo)

        prompt = QUALITY_GATE_PROMPT.format(
            title=issue.title,
            body=issue.body or "(no description)",
            labels=", ".join(issue.labels) if issue.labels else "(none)",
            classification=classification,
            is_sub_issue=issue.parent_id is not None,
            nesting_depth=nesting_depth,
        )

        try:
            response = await self._client.messages.create(
                model=settings.quality_gate_model,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.content[0].text.strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [line for line in lines if not line.strip().startswith("```")]
                text = "\n".join(lines).strip()
            data = json.loads(text)

            assessment = ConfidenceAssessment(
                score=data.get("score", 5),
                verdict=data.get("verdict", "clarify"),
                risk_flags=data.get("risk_flags", []),
                green_flags=data.get("green_flags", []),
                explanation=data.get("explanation", ""),
                clarification_question=data.get("clarification_question"),
            )
            logger.info(
                f"Issue #{issue.number}: quality gate score={assessment.score}/10 "
                f"verdict={assessment.verdict} "
                f"risks={assessment.risk_flags}"
            )
            return assessment

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"Failed to parse quality gate response for issue #{issue.number}: {e}")
            # Default to "clarify" on parse failure — safer than proceeding blindly
            return ConfidenceAssessment(
                score=5,
                verdict="clarify",
                risk_flags=["parse_error"],
                green_flags=[],
                explanation=f"Quality gate parse error: {e}",
                clarification_question="Could you clarify the scope and acceptance criteria for this issue?",
            )

        except Exception as e:
            logger.error(f"Quality gate API error for issue #{issue.number}: {e}")
            return ConfidenceAssessment(
                score=5,
                verdict="clarify",
                risk_flags=["api_error"],
                green_flags=[],
                explanation=f"Quality gate error: {e}",
                clarification_question="Could you clarify the scope and acceptance criteria for this issue?",
            )

    async def _compute_nesting_depth(self, issue: IssueInfo, repo: str) -> int:
        """Walk the parent chain to detect deeply nested sub-issues.

        Returns 0 for top-level issues, 1 for sub-issues, 2+ for
        sub-issues of sub-issues (the "Hydra" pattern).
        """
        if not issue.parent_id:
            return 0

        depth = 1
        current_parent_id = issue.parent_id

        while current_parent_id and depth < 5:  # Hard cap to prevent infinite loops
            try:
                parent = await self._tracker.get_issue(repo, current_parent_id)
                if parent.parent_id:
                    depth += 1
                    current_parent_id = parent.parent_id
                else:
                    break
            except Exception:
                break

        return depth

    def should_proceed(self, assessment: ConfidenceAssessment, is_proactive: bool) -> bool:
        """Determine if the agent should proceed with this issue."""
        if is_proactive:
            return assessment.score >= settings.proactive_min_score and assessment.verdict == "proceed"
        return assessment.verdict == "proceed"

    def should_clarify(self, assessment: ConfidenceAssessment, is_proactive: bool) -> bool:
        """Determine if clarification should be requested.

        Never requests clarification for proactive issues — we just skip silently.
        """
        if is_proactive:
            return False
        return assessment.verdict == "clarify"


_quality_gate: QualityGate | None = None


def get_quality_gate() -> QualityGate:
    global _quality_gate
    if _quality_gate is None:
        _quality_gate = QualityGate()
    return _quality_gate
