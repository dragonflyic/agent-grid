"""AgentLauncher: shared launch logic used by ManagementLoop and Scheduler.

Extracted from ManagementLoop to avoid Scheduler depending on ManagementLoop's
private methods.
"""

import logging
import re
from uuid import uuid4

from ..config import settings
from ..execution_grid import AgentExecution, ExecutionConfig, ExecutionStatus, get_execution_grid, utc_now
from ..issue_tracker import get_issue_tracker
from ..issue_tracker.label_manager import get_label_manager
from ..issue_tracker.metadata import embed_metadata, extract_metadata
from .database import get_database
from .prompt_builder import build_prompt

logger = logging.getLogger("agent_grid.launcher")


class AgentLauncher:
    """Shared agent launch logic for ManagementLoop and Scheduler."""

    def __init__(self):
        self._db = get_database()
        self._tracker = get_issue_tracker()

    async def claim_and_launch(
        self,
        issue_id: str,
        repo_url: str,
        prompt: str,
        mode: str = "implement",
        issue_number: int | None = None,
        context: dict | None = None,
    ) -> bool:
        """Atomically claim an issue and launch the agent.

        Claims the DB row FIRST to prevent races, then launches.
        Returns True if the agent was launched, False if claim failed.
        """
        execution_id = uuid4()
        execution = AgentExecution(
            id=execution_id,
            repo_url=repo_url,
            status=ExecutionStatus.PENDING,
            prompt=prompt,
            mode=mode,
            started_at=utc_now(),
        )

        claimed = await self._db.try_claim_issue(execution, issue_id=issue_id)
        if not claimed:
            logger.info(f"Issue #{issue_id}: already has active execution, skipping")
            repo = repo_url.replace("https://github.com/", "").replace(".git", "")
            await self._db.record_pipeline_event(
                int(issue_id) if issue_id.isdigit() else 0,
                repo,
                "launch_failed",
                "launch",
                {"reason": "duplicate_execution"},
            )
            return False

        config = ExecutionConfig(repo_url=repo_url, prompt=prompt)
        grid = get_execution_grid()
        try:
            await grid.launch_agent(
                config,
                mode=mode,
                execution_id=execution_id,
                issue_number=issue_number,
                context=context,
            )
        except Exception as e:
            logger.error(f"Failed to launch agent for issue #{issue_id}: {e}")
            execution.status = ExecutionStatus.FAILED
            execution.result = f"Launch failed: {e}"
            await self._db.update_execution(execution)
            repo = repo_url.replace("https://github.com/", "").replace(".git", "")
            await self._db.record_pipeline_event(
                int(issue_id) if issue_id.isdigit() else 0,
                repo,
                "launch_failed",
                "launch",
                {"reason": str(e)},
            )
            return False

        # Record successful launch
        repo = repo_url.replace("https://github.com/", "").replace(".git", "")
        await self._db.record_pipeline_event(
            int(issue_id) if issue_id.isdigit() else 0,
            repo,
            "launched",
            "launch",
            {"mode": mode, "execution_id": str(execution_id)},
        )

        # Post status comment on the issue
        stage_map = {
            "implement": "launched",
            "plan": "planning",
            "fix_ci": "ci_fix",
            "address_review": "addressing_review",
            "retry_with_feedback": "retrying",
            "rebase": "rebasing",
        }
        await self._post_status(repo, issue_id, stage_map.get(mode, "in_progress"))

        return True

    async def _post_status(self, repo: str, issue_id: str, stage: str, detail: str | None = None) -> None:
        """Post or update the status comment on the issue (fire-and-forget)."""
        try:
            from .status_comment import get_status_comment_manager

            mgr = get_status_comment_manager()
            await mgr.post_or_update(repo, issue_id, stage, detail)
        except Exception:
            logger.warning(f"Failed to post status comment for issue #{issue_id}", exc_info=True)

    async def has_active_execution(self, issue_id: str) -> bool:
        """Check if there's already a running/pending execution for this issue."""
        existing = await self._db.get_execution_for_issue(issue_id)
        if existing and existing.status in (ExecutionStatus.PENDING, ExecutionStatus.RUNNING):
            logger.info(f"Issue #{issue_id}: already has active execution {existing.id}, skipping")
            return True
        return False

    async def resolve_reviewer(self, repo: str, issue) -> str | None:
        """Resolve the right reviewer for an issue.

        For sub-issues, look up the parent issue's author.
        """
        if "ag/sub-issue" not in issue.labels:
            return None

        parent_number = None
        title_match = re.search(r"\[Sub #(\d+)\]", issue.title)
        if title_match:
            parent_number = title_match.group(1)
        elif issue.body:
            body_match = re.search(r"Part of #(\d+)", issue.body)
            if body_match:
                parent_number = body_match.group(1)

        if not parent_number:
            return None

        try:
            parent = await self._tracker.get_issue(repo, parent_number)
            if parent and parent.author:
                return parent.author
        except Exception as e:
            logger.warning(f"Issue #{issue.number}: failed to resolve parent #{parent_number} author: {e}")

        return None

    async def launch_simple(self, repo: str, issue) -> None:
        """Launch an agent for a SIMPLE issue."""
        if await self.has_active_execution(issue.id):
            return

        labels = get_label_manager()
        await labels.transition_to(repo, issue.id, "ag/in-progress")

        reviewer = await self.resolve_reviewer(repo, issue)
        context = {"reviewer": reviewer} if reviewer else None
        prompt = build_prompt(issue, repo, mode="implement", context=context)
        repo_url = f"https://github.com/{repo}.git"

        launched = await self.claim_and_launch(
            issue_id=issue.id,
            repo_url=repo_url,
            prompt=prompt,
            mode="implement",
            issue_number=issue.number,
        )
        if launched:
            logger.info(f"Issue #{issue.number}: SIMPLE — launched agent")
        else:
            await labels.transition_to(repo, issue.id, "ag/todo")

    async def launch_unblocked(self, repo: str, issue) -> None:
        """Launch an agent for a previously-blocked issue that got a human reply."""
        if await self.has_active_execution(issue.id):
            return

        labels = get_label_manager()
        await labels.transition_to(repo, issue.id, "ag/in-progress")

        clarification_comments = []
        last_block_idx = None
        for i, comment in enumerate(issue.comments):
            meta = extract_metadata(comment.body)
            if meta and meta.get("type") == "blocked":
                last_block_idx = i

        if last_block_idx is not None:
            for comment in issue.comments[last_block_idx + 1 :]:
                if extract_metadata(comment.body) is None:
                    clarification_comments.append(comment.body)

        context = {"clarification_comments": clarification_comments}
        reviewer = await self.resolve_reviewer(repo, issue)
        if reviewer:
            context["reviewer"] = reviewer
        prompt = build_prompt(issue, repo, mode="implement", context=context)

        launched = await self.claim_and_launch(
            issue_id=issue.id,
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
            mode="implement",
            issue_number=issue.number,
        )
        if launched:
            logger.info(f"Issue #{issue.number}: UNBLOCKED — launched agent")
        else:
            await labels.transition_to(repo, issue.id, "ag/todo")

    async def launch_planner(self, repo: str, issue) -> None:
        """Launch an agent to decompose a COMPLEX issue."""
        if await self.has_active_execution(issue.id):
            return

        labels = get_label_manager()
        await labels.transition_to(repo, issue.id, "ag/planning")

        prompt = build_prompt(issue, repo, mode="plan")

        launched = await self.claim_and_launch(
            issue_id=issue.id,
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
            mode="plan",
            issue_number=issue.number,
        )
        if launched:
            logger.info(f"Issue #{issue.number}: COMPLEX — launched planner agent")
        else:
            await labels.transition_to(repo, issue.id, "ag/todo")

    async def launch_review_handler(self, repo: str, pr_info: dict) -> None:
        """Launch an agent to address PR review comments."""
        issue_id = pr_info["issue_id"]
        if await self.has_active_execution(issue_id):
            return

        issue = await self._tracker.get_issue(repo, issue_id)
        checkpoint = await self._db.get_latest_checkpoint(issue_id)

        context = {
            "pr_number": pr_info["pr_number"],
            "existing_branch": pr_info["branch"],
            "review_comments": pr_info["review_comments"],
        }

        reviewer = await self.resolve_reviewer(repo, issue)
        if reviewer:
            context["reviewer"] = reviewer

        prompt = build_prompt(issue, repo, mode="address_review", context=context, checkpoint=checkpoint)

        launched = await self.claim_and_launch(
            issue_id=issue_id,
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
            mode="address_review",
            issue_number=int(issue_id),
            context=context,
        )
        if launched:
            logger.info(f"PR #{pr_info['pr_number']}: launched review handler agent")

    async def launch_retry(self, repo: str, pr_info: dict) -> None:
        """Launch a retry agent for a closed PR with feedback."""
        issue_id = pr_info["issue_id"]
        if await self.has_active_execution(issue_id):
            return

        issue = await self._tracker.get_issue(repo, issue_id)
        checkpoint = await self._db.get_latest_checkpoint(issue_id)

        issue_state = await self._db.get_issue_state(int(issue_id), repo)
        retry_count = (issue_state or {}).get("retry_count", 0)
        if retry_count >= settings.max_retries_per_issue:
            labels = get_label_manager()
            await labels.transition_to(repo, issue_id, "ag/failed")
            await self._tracker.add_comment(
                repo,
                issue_id,
                f"Max retries ({settings.max_retries_per_issue}) reached. Needs human intervention.",
            )
            return

        context = {
            "closed_pr_number": pr_info["pr_number"],
            "human_feedback": pr_info["human_feedback"],
            "what_not_to_do": checkpoint.get("context_summary", "") if checkpoint else "",
        }

        reviewer = await self.resolve_reviewer(repo, issue)
        if reviewer:
            context["reviewer"] = reviewer

        prompt = build_prompt(issue, repo, mode="retry_with_feedback", context=context, checkpoint=checkpoint)

        labels = get_label_manager()
        await labels.transition_to(repo, issue_id, "ag/in-progress")

        launched = await self.claim_and_launch(
            issue_id=issue_id,
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
            mode="retry_with_feedback",
            issue_number=int(issue_id),
            context=context,
        )
        if launched:
            await self._db.upsert_issue_state(
                issue_number=int(issue_id),
                repo=repo,
                retry_count=retry_count + 1,
            )
            logger.info(f"Issue #{issue_id}: retry #{retry_count + 1} — launched agent")
        else:
            await labels.transition_to(repo, issue_id, "ag/todo")

    async def launch_ci_fix(self, repo: str, check_info: dict) -> bool:
        """Launch an agent to fix a failing CI check."""
        branch = check_info.get("branch", "")
        match = re.match(r"agent/(\d+)(?:-|$)", branch)
        if not match:
            return False
        issue_id = match.group(1)

        if await self.has_active_execution(issue_id):
            return False

        issue = await self._tracker.get_issue(repo, issue_id)
        checkpoint = await self._db.get_latest_checkpoint(issue_id)

        context = {
            "existing_branch": branch,
            "pr_number": check_info.get("pr_number"),
            "check_name": check_info.get("check_name", ""),
            "check_output": check_info.get("check_output", ""),
            "check_url": check_info.get("check_url", ""),
        }

        prompt = build_prompt(issue, repo, mode="fix_ci", context=context, checkpoint=checkpoint)

        launched = await self.claim_and_launch(
            issue_id=issue_id,
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
            mode="fix_ci",
            issue_number=int(issue_id),
            context=context,
        )
        if launched:
            logger.info(f"Issue #{issue_id}: launched CI fix agent for '{check_info.get('check_name')}'")
        return launched

    async def launch_rebase(self, repo: str, pr_info: dict) -> bool:
        """Launch an agent to rebase a PR branch and resolve merge conflicts."""
        issue_id = pr_info["issue_id"]
        if await self.has_active_execution(issue_id):
            return False

        issue = await self._tracker.get_issue(repo, issue_id)
        context = {
            "pr_number": pr_info["pr_number"],
            "existing_branch": pr_info["branch"],
        }
        prompt = build_prompt(issue, repo, mode="rebase", context=context)

        launched = await self.claim_and_launch(
            issue_id=issue_id,
            repo_url=f"https://github.com/{repo}.git",
            prompt=prompt,
            mode="rebase",
            issue_number=int(issue_id),
            context=context,
        )
        if launched:
            logger.info(f"PR #{pr_info['pr_number']}: launched rebase agent for merge conflicts")
        return launched

    async def enrich_check_output(self, repo: str, check_info: dict) -> dict:
        """If check_output is empty, fetch actual CI logs via job ID."""
        if check_info.get("check_output") or not check_info.get("job_id"):
            return check_info
        try:
            logs = await self._tracker.get_actions_job_logs(repo, check_info["job_id"])
            if logs:
                return {**check_info, "check_output": logs}
        except Exception as e:
            logger.warning(f"Failed to fetch CI logs for job {check_info.get('job_id')}: {e}")
        return check_info

    async def run_quality_gate(
        self,
        repo: str,
        issue,
        classification,
        is_proactive: bool,
    ) -> str:
        """Run the quality gate on an issue.

        Returns "proceed", "blocked", or "skipped".
        """
        from .quality_gate import get_quality_gate

        quality_gate = get_quality_gate()
        labels = get_label_manager()

        assessment = await quality_gate.evaluate(
            issue=issue,
            classification=classification,
            is_proactive=is_proactive,
        )

        # Store assessment in issue_state metadata (atomic merge)
        await self._db.merge_issue_metadata(
            issue_number=issue.number,
            repo=repo,
            metadata_update={
                "confidence_score": assessment.score,
                "confidence_verdict": assessment.verdict,
                "risk_flags": assessment.risk_flags,
                "green_flags": assessment.green_flags,
            },
        )

        if quality_gate.should_clarify(assessment, is_proactive):
            await labels.transition_to(repo, issue.id, "ag/blocked")
            question = assessment.clarification_question or assessment.explanation
            owner_tag = f"@{issue.author} " if issue.author else ""
            comment = embed_metadata(
                f"**Agent confidence check — needs clarification:**\n\n"
                f"{owner_tag}{question}\n\n"
                f"_Confidence: {assessment.score}/10. "
                f"Risk flags: {', '.join(assessment.risk_flags)}_",
                {"type": "blocked", "reason": f"quality_gate: {assessment.explanation}"},
            )
            await self._tracker.add_comment(repo, issue.id, comment)
            logger.info(
                f"Issue #{issue.number}: quality gate blocked "
                f"(score={assessment.score}/10, flags={assessment.risk_flags})"
            )
            await self._db.record_pipeline_event(
                issue.number,
                repo,
                "quality_gate",
                "quality_gate",
                {"score": assessment.score, "verdict": "blocked", "risk_flags": assessment.risk_flags},
            )
            return "blocked"

        if not quality_gate.should_proceed(assessment, is_proactive):
            if not is_proactive:
                await labels.transition_to(repo, issue.id, "ag/skipped")
                await self._tracker.add_comment(
                    repo,
                    issue.id,
                    f"Skipping: confidence too low ({assessment.score}/10). {assessment.explanation}",
                )
            logger.info(f"Issue #{issue.number}: quality gate skipped (score={assessment.score}/10)")
            await self._db.record_pipeline_event(
                issue.number,
                repo,
                "quality_gate",
                "quality_gate",
                {"score": assessment.score, "verdict": "skipped", "is_proactive": is_proactive},
            )
            return "skipped"

        await self._db.record_pipeline_event(
            issue.number,
            repo,
            "quality_gate",
            "quality_gate",
            {
                "score": assessment.score,
                "verdict": "proceed",
                "risk_flags": assessment.risk_flags,
                "green_flags": assessment.green_flags,
            },
        )
        return "proceed"


_agent_launcher: AgentLauncher | None = None


def get_agent_launcher() -> AgentLauncher:
    global _agent_launcher
    if _agent_launcher is None:
        _agent_launcher = AgentLauncher()
    return _agent_launcher
