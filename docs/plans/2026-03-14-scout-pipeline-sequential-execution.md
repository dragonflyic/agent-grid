# Scout Pipeline & Sequential Sub-Issue Execution

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the blind LLM classifier with a scout agent that explores the codebase before deciding how to proceed, and execute sub-issues sequentially to avoid merge conflicts.

**Architecture:** The current classify→act pipeline (single LLM call with no codebase context) is replaced with a sanity-check→scout→act pipeline. The sanity check is a cheap LLM call that filters nonsense. The scout is a full Oz agent run that explores the repo, designs a plan, and returns a structured verdict. Sub-issues created by decomposition execute one at a time, each starting only after the previous PR is merged.

**Tech Stack:** FastAPI, Claude API (sanity check), Oz SDK (scout), SQLAlchemy, GitHub API

---

## Current Flow vs New Flow

```
CURRENT:
  scan → classify(LLM) → quality_gate(LLM) → launch implement/planner

NEW:
  scan → sanity_check(LLM) → launch scout(Oz) → on completion:
    → implement: launch implement agent with scout's plan
    → decompose: create sub-issues, launch first one only
    → needs_human: post question, ag/blocked
```

## Key Design Decisions

1. **Scout output**: The scout writes a JSON file `.scout-result.json` in the repo root as its final action. The coordinator parses `execution.result` text for a `<!-- SCOUT_RESULT -->` marker followed by JSON.
2. **Sequential execution**: Sub-issues are created in order. First gets `ag/todo`, rest get `ag/queued`. When a sub-issue PR merges, the next `ag/queued` sibling transitions to `ag/todo`.
3. **Progress tracking**: Parent issue gets a living progress comment showing step status (done/in-progress/queued).
4. **Sub-issue order**: Stored in parent's `issue_state.metadata.sub_issue_order: [N1, N2, N3]`.
5. **Quality gate removed**: The scout agent replaces the quality gate — it can actually read the code to assess feasibility.
6. **Planner mode removed**: The scout replaces the planner. Sub-issue creation moves to the coordinator (not the agent), giving us control over labels and ordering.

---

### Task 1: Add new labels

**Files:**
- Modify: `src/agent_grid/issue_tracker/label_manager.py:14-26`
- Modify: `src/agent_grid/coordinator/scanner.py:19-30`
- Modify: `src/agent_grid/coordinator/status_comment.py:18-52`

**Step 1: Add ag/scouting and ag/queued to AG_LABELS**

In `label_manager.py`, add to the `AG_LABELS` set:
```python
AG_LABELS = {
    "ag/todo",
    "ag/in-progress",
    "ag/blocked",
    "ag/waiting",
    "ag/planning",
    "ag/review-pending",
    "ag/done",
    "ag/failed",
    "ag/skipped",
    "ag/sub-issue",
    "ag/epic",
    "ag/scouting",   # NEW: scout agent is exploring/planning
    "ag/queued",     # NEW: sub-issue waiting for its turn
}
```

**Step 2: Add to HANDLED_LABELS in scanner.py**

```python
HANDLED_LABELS = {
    "ag/in-progress",
    "ag/blocked",
    "ag/waiting",
    "ag/planning",
    "ag/review-pending",
    "ag/done",
    "ag/failed",
    "ag/skipped",
    "ag/epic",
    "ag/proactive",
    "ag/scouting",   # NEW
    "ag/queued",     # NEW
}
```

**Step 3: Add scouting status to status_comment.py**

Add to `status_map`:
```python
"scouting": ("Scouting", "An agent is exploring the codebase and planning the approach."),
```

Add to `emoji_map`:
```python
"scouting": "\U0001f50d",  # magnifying glass
```

**Step 4: Add scouting to agent_launcher.py stage_map**

In `claim_and_launch()`, add to `stage_map`:
```python
"scout": "scouting",
```

**Step 5: Commit**

```bash
git add src/agent_grid/issue_tracker/label_manager.py src/agent_grid/coordinator/scanner.py src/agent_grid/coordinator/status_comment.py src/agent_grid/coordinator/agent_launcher.py
git commit -m "feat: add ag/scouting and ag/queued labels"
```

---

### Task 2: Simplify classifier to sanity check

**Files:**
- Modify: `src/agent_grid/coordinator/classifier.py`

**Step 1: Replace CLASSIFICATION_PROMPT with sanity check prompt**

```python
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
```

**Step 2: Simplify classify method**

Rename to `sanity_check` and simplify the return type:

```python
class SanityResult:
    """Result of the sanity check."""
    def __init__(self, verdict: str, reason: str):
        self.verdict = verdict  # "PROCEED" or "SKIP"
        self.reason = reason

class Classifier:
    """Lightweight sanity check — filters nonsense before scout launch."""

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
```

**Note:** Keep `_resolve_references` and the old `classify` method temporarily for backward compatibility. Mark `classify` as deprecated.

**Step 3: Commit**

```bash
git add src/agent_grid/coordinator/classifier.py
git commit -m "feat: add sanity_check method to classifier (lightweight nonsense filter)"
```

---

### Task 3: Add scout mode to prompt builder

**Files:**
- Modify: `src/agent_grid/coordinator/prompt_builder.py`

**Step 1: Add "scout" mode**

Add this before the final `return base`:

```python
elif mode == "scout":
    return f"""You are a senior tech lead scouting a GitHub issue before any implementation begins.

## Repository
- Repo: {repo}

## Issue #{issue.number}: {issue.title}

{issue.body or "(no description)"}

## Your Task

Explore the codebase thoroughly and produce an implementation plan. Do NOT write any code or create branches.

### Step 1: Deep Exploration
- Read the README, CLAUDE.md, and key config files
- Find ALL files relevant to this issue
- Read the actual source code — don't just list file names
- Understand existing tests and testing patterns
- Check recent git history for related changes

### Step 2: Feasibility Assessment
- Can this be done in a single PR (< 300 lines changed)?
- Are there any genuine blockers (missing credentials, unclear requirements)?
- What's the right architectural approach?

### Step 3: Produce Your Verdict

After exploration, output your verdict as a JSON block between markers.
This MUST be the last thing you output:

<!-- SCOUT_RESULT -->
```json
{{
  "verdict": "implement" | "decompose" | "needs_human",
  "plan": "Detailed step-by-step implementation plan. Be specific about files, functions, and changes.",
  "estimated_files": ["list", "of", "files", "to", "change"],
  "estimated_lines": 150,
  "steps": [
    {{
      "title": "Step title (only if verdict is decompose)",
      "description": "What this step does",
      "files": ["files", "involved"],
      "depends_on": []
    }}
  ],
  "question": "Question for human (only if verdict is needs_human)",
  "reason": "Why you chose this verdict"
}}
```
<!-- /SCOUT_RESULT -->

### Verdict Guidelines
- **implement**: This can be done in one PR. Provide a detailed plan.
- **decompose**: This needs multiple sequential PRs. Provide ordered steps.
  Each step should be independently mergeable. Later steps may build on earlier ones.
  Keep it to 5 steps max.
- **needs_human**: You genuinely cannot proceed without human input.
  Only use this for things a developer with full codebase access truly cannot determine:
  credentials, business policy decisions, choosing between fundamentally different product directions.

## Rules
- Do NOT create branches, commits, or PRs
- Do NOT create issues
- Do NOT modify any files
- ONLY explore and produce your verdict
- Your verdict JSON MUST be the last thing you output
"""
```

**Step 2: Commit**

```bash
git add src/agent_grid/coordinator/prompt_builder.py
git commit -m "feat: add scout mode to prompt builder"
```

---

### Task 4: Scout launcher and completion handler

**Files:**
- Modify: `src/agent_grid/coordinator/agent_launcher.py`
- Modify: `src/agent_grid/coordinator/scheduler.py`

**Step 1: Add launch_scout to agent_launcher.py**

```python
async def launch_scout(self, repo: str, issue: IssueInfo) -> bool:
    """Launch a scout agent to explore the codebase and plan the approach."""
    if await self.has_active_execution(issue.id):
        return False

    labels = get_label_manager()
    await labels.transition_to(repo, issue.id, "ag/scouting")

    prompt = build_prompt(issue, repo, mode="scout")
    repo_url = f"https://github.com/{repo}.git"

    launched = await self.claim_and_launch(
        issue_id=issue.id,
        repo_url=repo_url,
        prompt=prompt,
        mode="scout",
        issue_number=issue.number,
    )
    if launched:
        logger.info(f"Issue #{issue.number}: launched scout agent")
    else:
        await labels.transition_to(repo, issue.id, "ag/todo")
    return launched
```

**Step 2: Add scout result parser to agent_launcher.py**

```python
def parse_scout_result(self, result_text: str) -> dict | None:
    """Parse structured scout output from execution result."""
    if not result_text:
        return None
    marker = "<!-- SCOUT_RESULT -->"
    end_marker = "<!-- /SCOUT_RESULT -->"
    start = result_text.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = result_text.find(end_marker, start)
    if end == -1:
        end = len(result_text)
    json_text = result_text[start:end].strip()
    # Strip markdown code fences if present
    if json_text.startswith("```"):
        lines = json_text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        json_text = "\n".join(lines).strip()
    try:
        return json.loads(json_text)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Failed to parse scout result: {e}")
        return None
```

Add `import json` at top of agent_launcher.py if not already there.

**Step 3: Add handle_scout_completed to agent_launcher.py**

```python
async def handle_scout_completed(
    self, repo: str, issue_id: str, execution_id, scout_result: dict
) -> None:
    """Act on a scout agent's verdict."""
    verdict = scout_result.get("verdict", "implement")
    plan = scout_result.get("plan", "")
    issue = await self._tracker.get_issue(repo, issue_id)

    if verdict == "needs_human":
        labels = get_label_manager()
        await labels.transition_to(repo, issue_id, "ag/blocked")
        question = scout_result.get("question", scout_result.get("reason", ""))
        from .status_comment import get_status_comment_manager
        from ..issue_tracker.metadata import embed_metadata
        comment = embed_metadata(
            f"**Agent needs clarification:**\n\n{question}",
            {"type": "blocked", "reason": f"scout: {scout_result.get('reason', '')}"},
        )
        await self._tracker.add_comment(repo, issue_id, comment)
        logger.info(f"Issue #{issue.number}: scout verdict=needs_human")
        return

    if verdict == "decompose":
        await self._create_sequential_sub_issues(
            repo, issue, scout_result
        )
        return

    # Default: implement
    context = {"scout_plan": plan}
    reviewer = await self.resolve_reviewer(repo, issue)
    if reviewer:
        context["reviewer"] = reviewer

    labels = get_label_manager()
    await labels.transition_to(repo, issue_id, "ag/in-progress")

    prompt = build_prompt(issue, repo, mode="implement", context=context)
    repo_url = f"https://github.com/{repo}.git"
    launched = await self.claim_and_launch(
        issue_id=issue_id,
        repo_url=repo_url,
        prompt=prompt,
        mode="implement",
        issue_number=issue.number,
        context=context,
    )
    if launched:
        logger.info(f"Issue #{issue.number}: scout verdict=implement — launched agent")
    else:
        await labels.transition_to(repo, issue_id, "ag/todo")
```

**Step 4: Add _create_sequential_sub_issues to agent_launcher.py**

```python
async def _create_sequential_sub_issues(
    self, repo: str, parent_issue, scout_result: dict
) -> None:
    """Create sub-issues from scout decomposition, first gets ag/todo, rest get ag/queued."""
    steps = scout_result.get("steps", [])
    if not steps:
        logger.warning(f"Issue #{parent_issue.number}: scout decompose but no steps")
        return

    labels_mgr = get_label_manager()
    await labels_mgr.transition_to(repo, parent_issue.id, "ag/epic")

    sub_issue_order = []
    for i, step in enumerate(steps):
        title = f"[Sub #{parent_issue.number}] {step.get('title', f'Step {i+1}')}"
        body_parts = [
            f"Part of #{parent_issue.number}\n",
            f"## Objective\n{step.get('description', '')}\n",
        ]
        if step.get("files"):
            body_parts.append(f"## Files\n" + "\n".join(f"- `{f}`" for f in step["files"]) + "\n")

        label_list = ["ag/sub-issue"]
        if i == 0:
            label_list.append("ag/todo")
        else:
            label_list.append("ag/queued")

        body = "\n".join(body_parts)

        try:
            sub = await self._tracker.create_issue(
                repo, title=title, body=body, labels=label_list,
            )
            sub_issue_order.append(sub.number)
            # Assign to parent author
            if parent_issue.author:
                await self._tracker.assign_issue(repo, str(sub.number), parent_issue.author)
            logger.info(
                f"Created sub-issue #{sub.number}: {title} "
                f"({'ag/todo' if i == 0 else 'ag/queued'})"
            )
        except Exception as e:
            logger.error(f"Failed to create sub-issue for #{parent_issue.number}: {e}")

    # Store order in parent metadata
    await self._db.merge_issue_metadata(
        issue_number=parent_issue.number,
        repo=repo,
        metadata_update={"sub_issue_order": sub_issue_order},
    )

    # Post progress comment
    await self._post_progress_comment(repo, parent_issue, sub_issue_order, steps)
    logger.info(
        f"Issue #{parent_issue.number}: decomposed into {len(sub_issue_order)} "
        f"sequential sub-issues"
    )

async def _post_progress_comment(
    self, repo: str, parent_issue, sub_issue_order: list[int], steps: list[dict]
) -> None:
    """Post a progress tracking comment on the parent issue."""
    lines = [f"## Implementation Plan ({len(sub_issue_order)} steps)\n"]
    for i, (num, step) in enumerate(zip(sub_issue_order, steps)):
        title = step.get("title", f"Step {i+1}")
        if i == 0:
            icon = "\U0001f7e1"  # yellow circle — next up
            status = "next up"
        else:
            icon = "\u23f3"  # hourglass — queued
            status = "queued"
        lines.append(f"{i+1}. {icon} #{num} {title} — {status}")

    lines.append(f"\nSteps execute sequentially. Merge each PR to trigger the next step.")

    await self._tracker.add_comment(repo, parent_issue.id, "\n".join(lines))
```

**Step 5: Commit**

```bash
git add src/agent_grid/coordinator/agent_launcher.py
git commit -m "feat: add scout launcher, result parser, and sequential sub-issue creation"
```

---

### Task 5: Handle scout completion in scheduler

**Files:**
- Modify: `src/agent_grid/coordinator/scheduler.py`

**Step 1: Update _handle_agent_completed for mode="scout"**

In the `_handle_agent_completed` method, after the execution mode check block (`if execution.mode == "plan":`), add a scout handler BEFORE the existing `else:` block:

```python
if execution.mode == "plan":
    # Planning done — transition to epic
    await labels_mgr.transition_to(repo, issue_id, "ag/epic")
    logger.info(f"Plan completed for issue #{issue_id} — transitioned to ag/epic")
elif execution.mode == "scout":
    # Scout done — parse result and act on verdict
    from .agent_launcher import get_agent_launcher
    launcher = get_agent_launcher()
    scout_result = launcher.parse_scout_result(execution.result or "")
    if scout_result:
        await launcher.handle_scout_completed(
            repo, issue_id, execution.id, scout_result
        )
    else:
        # Scout didn't produce parseable output — fall back to implement
        logger.warning(f"Issue #{issue_id}: scout result not parseable, falling back to implement")
        await launcher.handle_scout_completed(
            repo, issue_id, execution.id,
            {"verdict": "implement", "plan": execution.result or "", "reason": "scout output fallback"},
        )
else:
    # Implementation done — mark for review and notify owner
    ...existing code...
```

**Step 2: Add sequential advancement on PR merge**

In `_handle_pr_closed`, after the `if merged:` block that transitions to ag/done, add:

```python
if merged:
    # ...existing ag/done transition code...

    # Check if this is a sub-issue — advance the queue
    await self._advance_sub_issue_queue(repo, issue_id)
```

**Step 3: Add _advance_sub_issue_queue method**

```python
async def _advance_sub_issue_queue(self, repo: str, issue_id: str) -> None:
    """When a sub-issue PR is merged, activate the next queued sibling."""
    tracker = get_issue_tracker()
    try:
        issue = await tracker.get_issue(repo, issue_id)
    except Exception:
        return

    # Check if this issue has a parent (is a sub-issue)
    if not issue.parent_id:
        return

    # Get the parent's sub-issue order from metadata
    parent_state = await self._db.get_issue_state(int(issue.parent_id), repo)
    if not parent_state:
        return
    metadata = parent_state.get("metadata") or {}
    if isinstance(metadata, str):
        import json
        metadata = json.loads(metadata)

    sub_order = metadata.get("sub_issue_order", [])
    if not sub_order:
        return

    # Find the next queued sub-issue in order
    labels_mgr = get_label_manager()
    activated = False
    for sub_num in sub_order:
        if str(sub_num) == issue_id:
            continue  # Skip the one that just merged
        try:
            sub = await tracker.get_issue(repo, str(sub_num))
            if "ag/queued" in sub.labels:
                await labels_mgr.transition_to(repo, str(sub_num), "ag/todo")
                logger.info(
                    f"Sub-issue #{sub_num}: activated (next in queue after #{issue_id} merged)"
                )
                activated = True
                break  # Only activate one
        except Exception as e:
            logger.warning(f"Failed to check sub-issue #{sub_num}: {e}")

    # Update progress comment on parent
    if activated:
        await self._update_progress_comment(repo, issue.parent_id, sub_order)

async def _update_progress_comment(self, repo: str, parent_id: str, sub_order: list[int]) -> None:
    """Update the progress comment on the parent issue."""
    tracker = get_issue_tracker()
    lines = [f"## Implementation Plan ({len(sub_order)} steps)\n"]

    for i, sub_num in enumerate(sub_order):
        try:
            sub = await tracker.get_issue(repo, str(sub_num))
            title = sub.title.replace(f"[Sub #{parent_id}] ", "")
            if "ag/done" in sub.labels or sub.status.value == "closed":
                icon = "\u2705"  # check mark
                status = "merged"
            elif "ag/in-progress" in sub.labels or "ag/review-pending" in sub.labels:
                icon = "\U0001f7e2"  # green circle
                status = "in progress"
            elif "ag/todo" in sub.labels:
                icon = "\U0001f7e1"  # yellow circle
                status = "next up"
            elif "ag/failed" in sub.labels:
                icon = "\u274c"  # X
                status = "failed"
            else:
                icon = "\u23f3"  # hourglass
                status = "queued"
            lines.append(f"{i+1}. {icon} #{sub_num} {title} — {status}")
        except Exception:
            lines.append(f"{i+1}. \u2753 #{sub_num} — unable to fetch")

    lines.append(f"\nSteps execute sequentially. Merge each PR to trigger the next step.")

    await self._update_status(repo, parent_id, "in_progress", "\n".join(lines))
```

**Step 4: Commit**

```bash
git add src/agent_grid/coordinator/scheduler.py
git commit -m "feat: handle scout completion and sequential sub-issue advancement"
```

---

### Task 6: Update implement prompt to use scout plan

**Files:**
- Modify: `src/agent_grid/coordinator/prompt_builder.py`

**Step 1: Add scout_plan context to implement mode**

In the `mode == "implement"` section, after the base prompt and before the setup block:

```python
elif mode == "implement":
    scout_plan = ""
    if context and context.get("scout_plan"):
        scout_plan = f"""
## Implementation Plan (from scout)

A scout agent has already explored the codebase and produced this plan for you.
Follow this plan — it reflects the actual codebase state:

{context['scout_plan']}
"""

    return (
        base
        + scout_plan
        + f"""
## Setup
Create and checkout a working branch:
...existing code...
"""
    )
```

**Step 2: Commit**

```bash
git add src/agent_grid/coordinator/prompt_builder.py
git commit -m "feat: pass scout plan as context to implement agent"
```

---

### Task 7: Update management loop and scheduler to use new flow

**Files:**
- Modify: `src/agent_grid/coordinator/management_loop.py`
- Modify: `src/agent_grid/coordinator/scheduler.py`

**Step 1: Replace Phase 2+3 in management_loop.py**

Replace the classify+act loop (lines ~89-151) with:

```python
# Phase 2: Sanity check and launch scouts
classifier = get_classifier()
budget = get_budget_manager()
labels = get_label_manager()

for issue in candidates:
    can_launch, reason = await budget.can_launch_agent()
    if not can_launch:
        logger.info(f"Budget limit reached: {reason}. Stopping new assignments.")
        await self._db.record_pipeline_event(
            issue.number, repo, "budget_blocked", "launch", {"reason": reason}
        )
        break

    sanity = await classifier.sanity_check(issue)

    await self._db.upsert_issue_state(
        issue_number=issue.number,
        repo=repo,
        classification=sanity.verdict,
    )
    await self._db.record_pipeline_event(
        issue.number, repo, "sanity_check", "classify",
        {"verdict": sanity.verdict, "reason": sanity.reason},
    )

    if sanity.verdict == "SKIP":
        await labels.transition_to(repo, issue.id, "ag/skipped")
        await self._tracker.add_comment(
            repo, issue.id,
            f"Skipping: {sanity.reason}",
        )
        logger.info(f"Issue #{issue.number}: SKIPPED — {sanity.reason}")
        continue

    # Launch scout agent
    await launcher.launch_scout(repo, issue)
```

**Step 2: Replace _classify_and_act in scheduler.py**

Replace the existing `_classify_and_act` method body with the same sanity check + scout launch flow:

```python
async def _classify_and_act(self, repo: str, issue_id: str) -> None:
    """Sanity-check an issue, then launch a scout agent."""
    from ..config import settings

    can_launch, reason = await self._budget_manager.can_launch_agent()
    if not can_launch:
        logger.warning(f"Budget check failed for webhook issue: {reason}")
        return

    tracker = get_issue_tracker()
    try:
        issue = await tracker.get_issue(repo, issue_id)
    except Exception as e:
        logger.error(f"Failed to fetch issue {issue_id}: {e}")
        return

    from .classifier import get_classifier
    classifier = get_classifier()
    sanity = await classifier.sanity_check(issue)

    await self._db.upsert_issue_state(
        issue_number=issue.number,
        repo=repo,
        classification=sanity.verdict,
    )
    await self._db.record_pipeline_event(
        issue.number, repo, "sanity_check", "classify",
        {"verdict": sanity.verdict, "reason": sanity.reason, "source": "webhook"},
    )

    if sanity.verdict == "SKIP":
        labels = get_label_manager()
        await labels.transition_to(repo, issue.id, "ag/skipped")
        await tracker.add_comment(repo, issue.id, f"Skipping: {sanity.reason}")
        logger.info(f"Webhook: Issue #{issue.number}: SKIPPED")
        return

    from .agent_launcher import get_agent_launcher
    launcher = get_agent_launcher()
    await launcher.launch_scout(repo, issue)
    logger.info(f"Webhook: Issue #{issue.number}: launched scout")
```

**Step 3: Remove quality gate calls from management loop and scheduler**

Delete the quality gate check blocks in both files (the scout replaces this functionality).

**Step 4: Commit**

```bash
git add src/agent_grid/coordinator/management_loop.py src/agent_grid/coordinator/scheduler.py
git commit -m "feat: replace classify+act with sanity_check+scout pipeline"
```

---

### Task 8: Update dashboard classify action

**Files:**
- Modify: `src/agent_grid/coordinator/dashboard_api.py`

**Step 1: Update the /actions/classify endpoint**

The dashboard manual classify action should use the new sanity check:

```python
sanity = await classifier.sanity_check(issue)
await db.upsert_issue_state(
    issue_number=num,
    repo=actual_repo,
    classification=sanity.verdict,
)
```

**Step 2: Commit**

```bash
git add src/agent_grid/coordinator/dashboard_api.py
git commit -m "feat: update dashboard classify action to use sanity check"
```

---

### Task 9: Update tests

**Files:**
- Modify: `tests/test_coordinator.py` — update classification tests
- Modify: `tests/test_e2e.py` — update e2e flow
- Modify: `src/agent_grid/e2e_test.py` — update e2e test
- Modify: `src/agent_grid/e2e_complex_test.py` — update complex e2e

**Step 1: Update any test that calls classifier.classify() to use sanity_check()**

Find all test files that reference `classify` and update them to use `sanity_check`.

**Step 2: Add test for parse_scout_result**

```python
def test_parse_scout_result():
    from agent_grid.coordinator.agent_launcher import AgentLauncher
    launcher = AgentLauncher.__new__(AgentLauncher)

    # Valid result
    text = '''Some exploration output...
<!-- SCOUT_RESULT -->
```json
{"verdict": "implement", "plan": "Do X then Y", "reason": "Simple change"}
```
<!-- /SCOUT_RESULT -->'''
    result = launcher.parse_scout_result(text)
    assert result["verdict"] == "implement"
    assert result["plan"] == "Do X then Y"

    # No marker
    assert launcher.parse_scout_result("just some text") is None

    # Empty
    assert launcher.parse_scout_result("") is None
```

**Step 3: Commit**

```bash
git add tests/
git commit -m "test: update tests for sanity check + scout pipeline"
```

---

### Task 10: Clean up deprecated code

**Files:**
- Modify: `src/agent_grid/coordinator/classifier.py` — remove old `classify` method
- Modify: `src/agent_grid/coordinator/management_loop.py` — remove quality gate imports
- Review: `src/agent_grid/coordinator/planner.py` — keep but stop calling from management loop
- Review: `src/agent_grid/coordinator/quality_gate.py` — keep but stop calling

**Step 1: Remove old classify method from classifier.py**

Delete the `classify()` method, `CLASSIFICATION_PROMPT`, and old `Classification` class. Keep `_resolve_references` as it may still be useful.

**Step 2: Remove quality gate imports and calls**

Remove quality gate imports from management_loop.py and scheduler.py.

**Step 3: Commit**

```bash
git add src/agent_grid/coordinator/
git commit -m "chore: remove deprecated classify, quality gate, and planner integration"
```

---

## Migration Notes

- **Backward compatibility**: Issues already in-flight (ag/in-progress, ag/review-pending) continue with existing handlers. Only new issues entering the pipeline use the scout flow.
- **Label creation**: Run `ensure_labels_exist()` on first deploy to create ag/scouting and ag/queued labels.
- **Planner mode**: The "plan" prompt mode is kept in prompt_builder.py but no longer called from the management loop. It can be removed in a future cleanup.
- **Budget impact**: Each issue now costs 2 Oz runs (scout + implement) instead of 1. Decomposed issues cost 1 scout + N implement runs.
- **Rollback**: To revert, restore the old `_classify_and_act` method and management loop Phase 2+3. The new labels are harmless.
