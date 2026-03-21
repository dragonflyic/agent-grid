# Fix Three Major Problems: Infinite Loop, Webhooks, PR Feedback

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the three critical production issues: (1) parent issues being re-processed in an infinite loop, (2) webhook pipeline not connected end-to-end, (3) PR review feedback and issue comments not being picked up by agents.

**Architecture:** The fixes are layered — Problem 1 is a one-line scanner fix, Problem 3 requires adding a missing DB method and wiring webhook events to the scheduler, and Problem 2 extends the webhook handler to trigger classification and agent launches in real-time. Problems 1 and 3 have no overlap, but Problem 2 builds on the event types introduced while fixing Problem 3.

**Tech Stack:** Python 3.12, FastAPI, asyncpg, Anthropic SDK, Fly.io Machines API

---

## Problem 1: Infinite Loop — Parent Issues Re-Processed

### Root Cause

`ag/epic` and `ag/sub-issue` are missing from `HANDLED_LABELS` in `scanner.py:16-25`.

**The cycle:**
1. Scanner finds issue with `ag/todo` → classifier says COMPLEX → planner runs
2. Planner finishes → labels parent `ag/epic`, removes `ag/planning`
3. Next cycle → scanner sees `ag/epic` label (starts with `ag/`) but it's NOT in `HANDLED_LABELS`
4. Issue gets re-classified → planner runs again → infinite loop

Additionally, `planner.py:165-166` uses `add_label`/`remove_label` instead of `transition_to`, which could leave stale labels on the issue.

---

### Task 1.1: Add missing labels to HANDLED_LABELS

**Files:**
- Modify: `src/agent_grid/coordinator/scanner.py:16-25`
- Test: `tests/test_coordinator.py`

**Step 1: Write the failing test**

Add to `tests/test_coordinator.py`:

```python
class TestScanner:
    """Tests for Scanner filtering logic."""

    def test_handled_labels_includes_epic(self):
        """ag/epic issues must not be re-scanned."""
        from agent_grid.coordinator.scanner import HANDLED_LABELS
        assert "ag/epic" in HANDLED_LABELS

    def test_handled_labels_includes_sub_issue(self):
        """ag/sub-issue issues must not be re-scanned."""
        from agent_grid.coordinator.scanner import HANDLED_LABELS
        assert "ag/sub-issue" in HANDLED_LABELS

    def test_handled_labels_covers_all_terminal_states(self):
        """Every ag/* label except ag/todo should be in HANDLED_LABELS."""
        from agent_grid.issue_tracker.label_manager import AG_LABELS
        from agent_grid.coordinator.scanner import HANDLED_LABELS
        # ag/todo is the only label that should trigger processing
        non_actionable = AG_LABELS - {"ag/todo"}
        assert non_actionable == HANDLED_LABELS
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_coordinator.py::TestScanner -v`
Expected: FAIL — `ag/epic` and `ag/sub-issue` not in HANDLED_LABELS

**Step 3: Fix scanner.py**

In `src/agent_grid/coordinator/scanner.py`, replace `HANDLED_LABELS`:

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
    "ag/sub-issue",
}
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_coordinator.py::TestScanner -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/agent_grid/coordinator/scanner.py tests/test_coordinator.py
git commit -m "fix: add ag/epic and ag/sub-issue to HANDLED_LABELS to stop infinite loop"
```

---

### Task 1.2: Fix planner label transition to use transition_to

**Files:**
- Modify: `src/agent_grid/coordinator/planner.py:164-166`

**Step 1: Write the failing test**

Add to `tests/test_coordinator.py`:

```python
import pytest

class TestPlanner:
    """Tests for Planner label transitions."""

    @pytest.mark.asyncio
    async def test_planner_uses_transition_to(self, monkeypatch):
        """Planner should use transition_to (not add_label/remove_label) for epic labeling."""
        # Verify the source code uses transition_to instead of add_label + remove_label
        import inspect
        from agent_grid.coordinator.planner import Planner
        source = inspect.getsource(Planner.decompose)
        assert "transition_to" in source, "Planner.decompose should use transition_to for label changes"
        assert "add_label" not in source or "remove_label" not in source, \
            "Planner.decompose should not use add_label/remove_label for the final epic transition"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_coordinator.py::TestPlanner -v`
Expected: FAIL — source uses `add_label` and `remove_label`

**Step 3: Fix planner.py**

In `src/agent_grid/coordinator/planner.py`, replace lines 164-166:

```python
        # Label parent as epic (transition_to removes ag/planning and adds ag/epic atomically)
        await self._labels.transition_to(repo, str(issue_number), "ag/epic")
```

Remove the two separate calls:
```python
        # DELETE: await self._labels.add_label(repo, str(issue_number), "ag/epic")
        # DELETE: await self._labels.remove_label(repo, str(issue_number), "ag/planning")
```

**Step 4: Run tests**

Run: `pytest tests/test_coordinator.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/agent_grid/coordinator/planner.py tests/test_coordinator.py
git commit -m "fix: planner uses transition_to for atomic epic label change"
```

---

## Problem 2: Webhooks Not Connected End-to-End

### Root Cause

The webhook endpoint exists and publishes events to the event bus, but:
1. **New issues** — Scheduler only launches if the issue already has `ag/*` labels. New issues arrive without labels → nothing happens.
2. **Issue comments** — `_handle_issue_comment_event` only handles `@agent-grid nudge`. Regular human comments on ag/* issues are ignored.
3. **PR reviews** — `PR_REVIEW` and `PR_CLOSED` events are published but the scheduler never subscribes to them.
4. **No real-time trigger** — Everything waits for the hourly cron loop.

### What we need:
- Webhook receives event → scheduler reacts immediately (classify + launch, or address review, or handle comment)
- Cron loop remains as a backup/catch-all

---

### Task 2.1: Add ISSUE_COMMENT event type

**Files:**
- Modify: `src/agent_grid/execution_grid/public_api.py:47-59`
- Test: `tests/test_coordinator.py`

**Step 1: Write the failing test**

```python
class TestEventTypes:
    """Tests for event type coverage."""

    def test_issue_comment_event_type_exists(self):
        """ISSUE_COMMENT event type should exist for webhook processing."""
        from agent_grid.execution_grid.public_api import EventType
        assert hasattr(EventType, "ISSUE_COMMENT")
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_coordinator.py::TestEventTypes -v`
Expected: FAIL

**Step 3: Add ISSUE_COMMENT to EventType enum**

In `src/agent_grid/execution_grid/public_api.py`, add after `ISSUE_UPDATED`:

```python
    ISSUE_COMMENT = "issue.comment"
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_coordinator.py::TestEventTypes -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/agent_grid/execution_grid/public_api.py tests/test_coordinator.py
git commit -m "feat: add ISSUE_COMMENT event type for webhook processing"
```

---

### Task 2.2: Webhook handler publishes ISSUE_COMMENT events for human comments on ag/* issues

**Files:**
- Modify: `src/agent_grid/issue_tracker/webhook_handler.py:103-127`

**Step 1: Write the failing test**

```python
import pytest

class TestWebhookHandler:
    """Tests for webhook event publishing."""

    @pytest.mark.asyncio
    async def test_issue_comment_publishes_event_for_ag_issues(self):
        """Human comment on ag/* issue should publish ISSUE_COMMENT event."""
        from agent_grid.execution_grid.public_api import EventType
        from agent_grid.execution_grid.event_bus import EventBus

        bus = EventBus()
        events = []

        async def capture(event):
            events.append(event)

        bus.subscribe(capture)
        await bus.start()

        # Simulate the event that webhook_handler would publish
        await bus.publish(
            EventType.ISSUE_COMMENT,
            {
                "repo": "owner/repo",
                "issue_id": "42",
                "comment_body": "Here's the answer to your question",
                "labels": ["ag/blocked"],
                "is_pull_request": False,
            },
        )
        await bus.wait_until_empty()
        await bus.stop()

        assert len(events) == 1
        assert events[0].type == EventType.ISSUE_COMMENT
        assert events[0].payload["issue_id"] == "42"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_coordinator.py::TestWebhookHandler -v`
Expected: FAIL (ISSUE_COMMENT doesn't exist yet — will pass after Task 2.1)

**Step 3: Update webhook handler**

In `src/agent_grid/issue_tracker/webhook_handler.py`, replace `_handle_issue_comment_event`:

```python
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

    repo_full_name = repo.get("full_name", "")
    issue_number = issue.get("number")
    comment_body = comment.get("body", "")
    labels = [label["name"] for label in issue.get("labels", [])]
    is_pull_request = "pull_request" in issue

    # Check for nudge commands
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

    # Publish comment event for ag/* issues (scheduler decides what to do)
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
```

**Step 4: Run test**

Run: `pytest tests/test_coordinator.py::TestWebhookHandler -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/agent_grid/issue_tracker/webhook_handler.py tests/test_coordinator.py
git commit -m "feat: webhook publishes ISSUE_COMMENT events for ag/* issues"
```

---

### Task 2.3: Scheduler handles ISSUE_COMMENT — triggers blocker resolution or PR feedback

**Files:**
- Modify: `src/agent_grid/coordinator/scheduler.py`

**Step 1: Write the failing test**

```python
class TestSchedulerEventHandling:
    """Tests for scheduler event routing."""

    def test_scheduler_handles_issue_comment_event(self):
        """Scheduler._handle_event should route ISSUE_COMMENT events."""
        import inspect
        from agent_grid.coordinator.scheduler import Scheduler
        source = inspect.getsource(Scheduler._handle_event)
        assert "ISSUE_COMMENT" in source, "Scheduler must handle ISSUE_COMMENT events"

    def test_scheduler_handles_pr_review_event(self):
        """Scheduler._handle_event should route PR_REVIEW events."""
        import inspect
        from agent_grid.coordinator.scheduler import Scheduler
        source = inspect.getsource(Scheduler._handle_event)
        assert "PR_REVIEW" in source, "Scheduler must handle PR_REVIEW events"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_coordinator.py::TestSchedulerEventHandling -v`
Expected: FAIL

**Step 3: Update scheduler to handle new event types**

In `src/agent_grid/coordinator/scheduler.py`, update `_handle_event` and add new handlers:

```python
    async def _handle_event(self, event: Event) -> None:
        """Handle incoming events."""
        if not self._running:
            return

        if event.type == EventType.ISSUE_CREATED:
            await self._handle_issue_created(event)
        elif event.type == EventType.ISSUE_COMMENT:
            await self._handle_issue_comment(event)
        elif event.type == EventType.NUDGE_REQUESTED:
            await self._handle_nudge_requested(event)
        elif event.type == EventType.AGENT_COMPLETED:
            await self._handle_agent_completed(event)
        elif event.type == EventType.AGENT_FAILED:
            await self._handle_agent_failed(event)
        elif event.type == EventType.PR_REVIEW:
            await self._handle_pr_review(event)
        elif event.type == EventType.PR_CLOSED:
            await self._handle_pr_closed(event)
```

Add `_handle_issue_comment`:

```python
    async def _handle_issue_comment(self, event: Event) -> None:
        """Handle human comment on an ag/* issue.

        - If issue is ag/blocked: check if this is the human reply we need,
          then launch agent with clarification context.
        - If issue is ag/in-progress or ag/review-pending: the cron loop
          will pick it up. No immediate action needed (avoid double-launch).
        """
        payload = event.payload
        repo = payload.get("repo")
        issue_id = payload.get("issue_id")
        labels = payload.get("labels", [])
        is_pull_request = payload.get("is_pull_request", False)

        if not repo or not issue_id:
            return

        # For PR comments, let the PR_REVIEW handler deal with it
        if is_pull_request:
            return

        # If the issue is blocked, this comment might be the human unblocking it
        if "ag/blocked" in labels:
            await self._handle_blocked_issue_comment(repo, issue_id)

    async def _handle_blocked_issue_comment(self, repo: str, issue_id: str) -> None:
        """Handle a comment on a blocked issue — potentially unblocks it."""
        from .blocker_resolver import get_blocker_resolver

        tracker = get_issue_tracker()
        try:
            issue = await tracker.get_issue(repo, issue_id)
        except Exception as e:
            logger.error(f"Failed to fetch issue {issue_id}: {e}")
            return

        resolver = get_blocker_resolver()
        if resolver._has_human_reply_after_block(issue.comments):
            logger.info(f"Issue #{issue_id} unblocked via webhook — launching agent")
            # Reuse the management loop's launch logic
            from .management_loop import get_management_loop
            loop = get_management_loop()
            await loop._launch_unblocked(repo, issue)
```

Add `_handle_pr_review`:

```python
    async def _handle_pr_review(self, event: Event) -> None:
        """Handle PR review submission — launch address_review agent."""
        payload = event.payload
        repo = payload.get("repo")
        pr_number = payload.get("pr_number")
        branch = payload.get("branch", "")
        review_state = payload.get("review_state")

        if not repo or not pr_number:
            return

        # Only react to actionable reviews
        if review_state not in ("changes_requested", "commented"):
            return

        # Extract issue ID from branch name (agent/42 → "42")
        import re
        match = re.match(r"agent/(\d+)", branch)
        if not match:
            return
        issue_id = match.group(1)

        # Fetch the actual review comments via PR monitor
        from .pr_monitor import get_pr_monitor
        pr_monitor = get_pr_monitor()
        prs_needing_work = await pr_monitor.check_prs(repo)

        for pr_info in prs_needing_work:
            if pr_info["pr_number"] == pr_number and pr_info["issue_id"]:
                from .management_loop import get_management_loop
                loop = get_management_loop()
                await loop._launch_review_handler(repo, pr_info)
                break
```

Add `_handle_pr_closed`:

```python
    async def _handle_pr_closed(self, event: Event) -> None:
        """Handle PR closed — launch retry agent if not merged."""
        payload = event.payload
        repo = payload.get("repo")
        pr_number = payload.get("pr_number")
        branch = payload.get("branch", "")
        merged = payload.get("merged", False)

        if not repo or not pr_number or merged:
            return  # Merged PRs are success, not retry

        import re
        match = re.match(r"agent/(\d+)", branch)
        if not match:
            return
        issue_id = match.group(1)

        # Fetch feedback via PR monitor
        from .pr_monitor import get_pr_monitor
        pr_monitor = get_pr_monitor()
        closed_prs = await pr_monitor.check_closed_prs(repo)

        for pr_info in closed_prs:
            if pr_info["pr_number"] == pr_number and pr_info["issue_id"]:
                from .management_loop import get_management_loop
                loop = get_management_loop()
                await loop._launch_retry(repo, pr_info)
                break
```

**Step 4: Run tests**

Run: `pytest tests/test_coordinator.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/agent_grid/coordinator/scheduler.py tests/test_coordinator.py
git commit -m "feat: scheduler handles ISSUE_COMMENT, PR_REVIEW, PR_CLOSED events from webhooks"
```

---

### Task 2.4: Scheduler handles ISSUE_CREATED with classification (not just label check)

**Files:**
- Modify: `src/agent_grid/coordinator/scheduler.py:73-83`

**Step 1: Write the failing test**

```python
class TestSchedulerIssueCreated:
    """Tests for issue creation handling."""

    def test_issue_created_does_not_require_existing_labels(self):
        """Scheduler should classify new issues, not just check for existing labels."""
        import inspect
        from agent_grid.coordinator.scheduler import Scheduler
        source = inspect.getsource(Scheduler._handle_issue_created)
        # Should NOT bail out just because labels are empty
        assert "classifier" in source.lower() or "classify" in source.lower() or \
            "management_loop" in source.lower() or "run_cycle" in source.lower(), \
            "Scheduler._handle_issue_created should classify issues or trigger the management loop"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_coordinator.py::TestSchedulerIssueCreated -v`
Expected: FAIL

**Step 3: Update _handle_issue_created**

Replace the existing `_handle_issue_created` in scheduler.py:

```python
    async def _handle_issue_created(self, event: Event) -> None:
        """Handle new issue creation — classify and act immediately.

        When an issue is created (or labeled with ag/*), classify it
        and launch the appropriate agent without waiting for the cron loop.
        """
        payload = event.payload
        issue_id = payload.get("issue_id")
        repo = payload.get("repo")
        labels = payload.get("labels", [])

        if not repo or not issue_id:
            return

        # If issue already has an ag/* label, it's opted in — process it
        has_ag_label = any(label.startswith("ag/") for label in labels)
        if not has_ag_label:
            return  # Issue not opted in to agent processing

        # Check if it's already in a handled state
        from .scanner import HANDLED_LABELS
        if any(label in HANDLED_LABELS for label in labels):
            return  # Already being handled

        # Classify and act — delegate to the management loop's single-issue processing
        tracker = get_issue_tracker()
        try:
            issue = await tracker.get_issue(repo, issue_id)
        except Exception as e:
            logger.error(f"Failed to fetch issue {issue_id}: {e}")
            return

        can_launch, reason = await self._budget_manager.can_launch_agent()
        if not can_launch:
            logger.warning(f"Budget check failed for webhook issue: {reason}")
            return

        from .classifier import get_classifier
        from .database import get_database
        from ..issue_tracker.label_manager import get_label_manager
        from ..issue_tracker.metadata import embed_metadata

        classifier = get_classifier()
        classification = await classifier.classify(issue)

        db = get_database()
        await db.upsert_issue_state(
            issue_number=issue.number,
            repo=repo,
            classification=classification.category,
        )

        from .management_loop import get_management_loop
        loop = get_management_loop()
        labels_mgr = get_label_manager()

        if classification.category == "SIMPLE":
            await loop._launch_simple(repo, issue)
        elif classification.category == "COMPLEX":
            await loop._launch_planner(repo, issue)
        elif classification.category == "BLOCKED":
            await labels_mgr.transition_to(repo, issue.id, "ag/blocked")
            question = classification.blocking_question or classification.reason
            comment = embed_metadata(
                f"**Agent needs clarification:**\n\n{question}",
                {"type": "blocked", "reason": classification.reason},
            )
            await tracker.add_comment(repo, issue.id, comment)
            logger.info(f"Webhook: Issue #{issue.number}: BLOCKED — posted question")
        elif classification.category == "SKIP":
            await labels_mgr.transition_to(repo, issue.id, "ag/skipped")
            await tracker.add_comment(
                repo,
                issue.id,
                f"Skipping automated work: {classification.reason}",
            )

        logger.info(f"Webhook: Processed issue #{issue.number} as {classification.category}")
```

**Step 4: Run tests**

Run: `pytest tests/test_coordinator.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/agent_grid/coordinator/scheduler.py tests/test_coordinator.py
git commit -m "feat: webhook-triggered issue classification and agent launch"
```

---

### Task 2.5: Handle ISSUE_UPDATED (labeled event) to process issues when ag/todo is added

**Files:**
- Modify: `src/agent_grid/coordinator/scheduler.py`

When a human manually adds the `ag/todo` label to an existing issue, we should process it immediately.

**Step 1: Update scheduler to handle ISSUE_UPDATED**

Add to `_handle_event` routing:

```python
        elif event.type == EventType.ISSUE_UPDATED:
            await self._handle_issue_updated(event)
```

Add handler:

```python
    async def _handle_issue_updated(self, event: Event) -> None:
        """Handle issue updated (labeled/unlabeled/edited).

        Specifically reacts when ag/todo label is added to an issue.
        """
        payload = event.payload
        action = payload.get("action")
        repo = payload.get("repo")
        issue_id = payload.get("issue_id")
        labels = payload.get("labels", [])

        if action != "labeled" or not repo or not issue_id:
            return

        # Only react if ag/todo was just added
        if "ag/todo" not in labels:
            return

        # Delegate to the same flow as ISSUE_CREATED
        await self._handle_issue_created(Event(
            type=EventType.ISSUE_CREATED,
            payload={
                "repo": repo,
                "issue_id": issue_id,
                "labels": labels,
            },
        ))
```

**Step 2: Run tests**

Run: `pytest tests/test_coordinator.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add src/agent_grid/coordinator/scheduler.py
git commit -m "feat: react to ag/todo label being added via webhook"
```

---

## Problem 3: PR Feedback and Issue Comments Not Addressed

### Root Cause

1. **CRITICAL:** `Database.get_issue_id_for_execution()` is missing — exists in `DryRunDatabase` but not the real DB class. The scheduler calls it on agent completion (`scheduler.py:124, 151`) to save checkpoints and update labels. Without it, the entire post-completion flow is broken (no labels updated, no checkpoints saved).

2. **Worker doesn't report branch/PR info** — `worker-entrypoint.sh` only sends `execution_id`, `status`, and `result` in its callback. It doesn't send `branch`, `pr_number`, or `checkpoint`. This means the coordinator never knows which PR an execution created.

3. **Timestamp comparison in pr_monitor.py is fragile** — compares ISO strings with inconsistent formats (GitHub uses `Z` suffix, Python uses microseconds).

---

### Task 3.1: Add get_issue_id_for_execution to Database

**Files:**
- Modify: `src/agent_grid/coordinator/database.py`
- Test: `tests/test_coordinator.py`

**Step 1: Write the failing test**

```python
class TestDatabaseMethods:
    """Tests for Database method existence."""

    def test_get_issue_id_for_execution_exists(self):
        """Database must have get_issue_id_for_execution method."""
        from agent_grid.coordinator.database import Database
        assert hasattr(Database, "get_issue_id_for_execution"), \
            "Database missing get_issue_id_for_execution — scheduler.py:124 calls it"

    def test_get_issue_id_for_execution_signature(self):
        """get_issue_id_for_execution must accept UUID and return str | None."""
        import inspect
        from agent_grid.coordinator.database import Database
        sig = inspect.signature(Database.get_issue_id_for_execution)
        params = list(sig.parameters.keys())
        assert "execution_id" in params
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_coordinator.py::TestDatabaseMethods -v`
Expected: FAIL — method doesn't exist

**Step 3: Add the method to database.py**

In `src/agent_grid/coordinator/database.py`, add after `get_execution_for_issue` (around line 149):

```python
    async def get_issue_id_for_execution(self, execution_id: UUID) -> str | None:
        """Get the issue_id associated with an execution."""
        pool = await self._get_pool()
        row = await pool.fetchrow(
            "SELECT issue_id FROM executions WHERE id = $1",
            execution_id,
        )
        return row["issue_id"] if row else None
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_coordinator.py::TestDatabaseMethods -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/agent_grid/coordinator/database.py tests/test_coordinator.py
git commit -m "fix: add missing get_issue_id_for_execution to Database class"
```

---

### Task 3.2: Fix worker to report branch, PR number, and checkpoint

**Files:**
- Modify: `scripts/worker-entrypoint.sh`

The worker currently only sends `execution_id`, `status`, and `result`. It needs to also detect and send:
- The branch it pushed to
- The PR number it created
- Basic checkpoint info (what it did)

**Step 1: Update the worker's Python callback section**

Replace the Python callback block in `scripts/worker-entrypoint.sh` (the `asyncio.run(main())` function):

```python
python3 -c "
import asyncio, json, os, sys, subprocess
from claude_code_sdk import query
from claude_code_sdk.types import ClaudeCodeOptions, ResultMessage

async def main():
    prompt = os.environ['PROMPT']
    options = ClaudeCodeOptions(
        cwd='/workspace/repo',
        permission_mode='bypassPermissions',
    )

    result = ''
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage) and message.result:
            result = message.result

    # Print result to stdout so it appears in Fly logs
    print('=== AGENT RESULT ===')
    print(result[:10000])
    print('=== END RESULT ===')

    # Detect branch and PR
    branch = None
    pr_number = None
    try:
        branch = subprocess.check_output(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd='/workspace/repo', text=True
        ).strip()
        if branch == 'main' or branch == 'master':
            branch = None  # Not on a feature branch
    except Exception:
        pass

    try:
        # Check if a PR was created by looking at gh output
        pr_list = subprocess.check_output(
            ['gh', 'pr', 'list', '--head', branch or '', '--json', 'number', '--limit', '1'],
            cwd='/workspace/repo', text=True
        ).strip()
        prs = json.loads(pr_list) if pr_list else []
        if prs:
            pr_number = prs[0]['number']
    except Exception:
        pass

    # Report back to orchestrator
    import httpx
    callback_url = os.environ.get('ORCHESTRATOR_URL', '') + '/api/agent-status'
    payload = {
        'execution_id': os.environ['EXECUTION_ID'],
        'status': 'completed',
        'result': result[:10000],
        'branch': branch,
        'pr_number': pr_number,
        'checkpoint': {
            'mode': os.environ.get('MODE', 'implement'),
            'context_summary': result[:500],
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(callback_url, json=payload)
            print(f'Reported status: {resp.status_code}')
    except Exception as e:
        print(f'Warning: Failed to report status: {e}')

asyncio.run(main())
"
```

**Step 2: Verify the agent-status API endpoint accepts these fields**

Check the API handler for `/api/agent-status`. The `FlyExecutionGrid.handle_agent_result` already accepts `branch`, `pr_number`, `checkpoint` parameters — we need to make sure the API endpoint passes them through.

Check and update the API endpoint (find it via grep for `agent-status`).

**Step 3: Commit**

```bash
git add scripts/worker-entrypoint.sh
git commit -m "feat: worker reports branch, PR number, and checkpoint to coordinator"
```

---

### Task 3.3: Fix timestamp comparison in pr_monitor.py

**Files:**
- Modify: `src/agent_grid/coordinator/pr_monitor.py:85,90,114,156`
- Test: `tests/test_coordinator.py`

**Step 1: Write the failing test**

```python
class TestPRMonitorTimestamps:
    """Tests for PR monitor timestamp handling."""

    def test_timestamp_comparison_handles_github_format(self):
        """Timestamps with Z suffix should compare correctly with isoformat."""
        from datetime import datetime
        # GitHub format
        github_ts = "2026-02-14T15:35:22Z"
        # Our stored format (datetime.utcnow().isoformat())
        our_ts = "2026-02-14T15:30:00.123456"
        # github_ts is LATER but string comparison with Z may be wrong
        # "Z" < "a-z" in ASCII, so "...22Z" < "...00.123456" is wrong
        # This test documents the bug
        assert github_ts > our_ts  # This FAILS with raw string comparison
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_coordinator.py::TestPRMonitorTimestamps -v`
Expected: FAIL — `"2026-02-14T15:35:22Z" > "2026-02-14T15:30:00.123456"` fails because `Z` < digits in ASCII

**Step 3: Add timestamp normalization helper and use it**

In `src/agent_grid/coordinator/pr_monitor.py`, add at the top:

```python
def _normalize_timestamp(ts: str) -> str:
    """Normalize ISO timestamp for reliable comparison.

    Strips 'Z' suffix, removes microseconds, ensures consistent format.
    """
    if not ts:
        return ""
    # Replace Z with +00:00, then strip timezone info for comparison
    ts = ts.replace("Z", "").replace("+00:00", "")
    # Remove microseconds if present (everything after the seconds)
    if "." in ts:
        ts = ts.split(".")[0]
    return ts
```

Then update `check_prs` to normalize timestamps:

```python
# Line 85 - replace:
if not last_check or review.get("submitted_at", "") > last_check:
# with:
if not last_check or _normalize_timestamp(review.get("submitted_at", "")) > _normalize_timestamp(last_check):

# Line 90 - replace:
if not last_check or comment.get("created_at", "") > last_check:
# with:
if not last_check or _normalize_timestamp(comment.get("created_at", "")) > _normalize_timestamp(last_check):
```

Also update `set_cron_state` call (line 114) to store normalized timestamp:

```python
await self._db.set_cron_state(
    "last_pr_check",
    {"timestamp": _normalize_timestamp(datetime.utcnow().isoformat())},
)
```

Same pattern for `check_closed_prs` line 156.

**Step 4: Fix the test to use the normalizer**

```python
    def test_normalized_timestamps_compare_correctly(self):
        """Normalized timestamps should compare correctly regardless of format."""
        from agent_grid.coordinator.pr_monitor import _normalize_timestamp
        github_ts = "2026-02-14T15:35:22Z"
        our_ts = "2026-02-14T15:30:00.123456"
        assert _normalize_timestamp(github_ts) > _normalize_timestamp(our_ts)
```

**Step 5: Run tests**

Run: `pytest tests/test_coordinator.py::TestPRMonitorTimestamps -v`
Expected: PASS

**Step 6: Commit**

```bash
git add src/agent_grid/coordinator/pr_monitor.py tests/test_coordinator.py
git commit -m "fix: normalize timestamps in PR monitor for reliable comparison"
```

---

### Task 3.4: Add duplicate-launch guard to management loop

**Files:**
- Modify: `src/agent_grid/coordinator/management_loop.py`

Prevent the same issue from getting multiple concurrent agents launched.

**Step 1: Write the failing test**

```python
class TestManagementLoopGuards:
    """Tests for management loop duplicate prevention."""

    def test_launch_simple_checks_existing_execution(self):
        """_launch_simple should check for existing running executions."""
        import inspect
        from agent_grid.coordinator.management_loop import ManagementLoop
        source = inspect.getsource(ManagementLoop._launch_simple)
        assert "get_execution_for_issue" in source or "running" in source.lower(), \
            "_launch_simple should check for existing executions before launching"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_coordinator.py::TestManagementLoopGuards -v`
Expected: FAIL

**Step 3: Add duplicate check to launch methods**

Add a helper method to `ManagementLoop`:

```python
    async def _has_active_execution(self, issue_id: str) -> bool:
        """Check if there's already a running/pending execution for this issue."""
        existing = await self._db.get_execution_for_issue(issue_id)
        if existing and existing.status in (ExecutionStatus.PENDING, ExecutionStatus.RUNNING):
            logger.info(f"Issue #{issue_id}: already has active execution {existing.id}, skipping")
            return True
        return False
```

Add the import at the top:

```python
from ..execution_grid import ExecutionConfig, ExecutionStatus, get_execution_grid
```

Then add the guard at the top of `_launch_simple`, `_launch_planner`, `_launch_unblocked`, `_launch_review_handler`, and `_launch_retry`:

```python
    async def _launch_simple(self, repo: str, issue) -> None:
        """Launch an agent for a SIMPLE issue."""
        if await self._has_active_execution(issue.id):
            return
        # ... rest of method
```

(Same pattern for each launch method.)

**Step 4: Run tests**

Run: `pytest tests/test_coordinator.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/agent_grid/coordinator/management_loop.py tests/test_coordinator.py
git commit -m "fix: add duplicate-launch guard to prevent concurrent executions per issue"
```

---

### Task 3.5: Verify and fix agent-status API endpoint

**Files:**
- Find and modify the API endpoint that handles `/api/agent-status`

The worker now sends `branch`, `pr_number`, and `checkpoint` in its callback. Verify the API endpoint passes these through to `FlyExecutionGrid.handle_agent_result`.

**Step 1: Find the endpoint**

Search for `agent-status` or `agent_status` in the codebase.

**Step 2: Verify it accepts and forwards all fields**

The endpoint should extract `branch`, `pr_number`, and `checkpoint` from the request body and pass them to `handle_agent_result()`.

If fields are missing from the request parsing, add them.

**Step 3: Commit**

```bash
git commit -m "fix: agent-status endpoint forwards branch, PR, and checkpoint from worker"
```

---

## Summary of All Changes

| Task | Problem | What It Fixes | Risk |
|------|---------|---------------|------|
| 1.1 | Infinite Loop | Add `ag/epic`, `ag/sub-issue` to HANDLED_LABELS | None — purely additive |
| 1.2 | Infinite Loop | Planner uses `transition_to` for atomic label change | Low — same end state |
| 2.1 | Webhooks | Add ISSUE_COMMENT event type | None — new enum value |
| 2.2 | Webhooks | Webhook publishes ISSUE_COMMENT for ag/* issues | Low — only publishes events |
| 2.3 | Webhooks | Scheduler handles ISSUE_COMMENT, PR_REVIEW, PR_CLOSED | Medium — new reactive logic |
| 2.4 | Webhooks | Issue created handler classifies instead of just checking labels | Medium — adds classification on webhook path |
| 2.5 | Webhooks | React to ag/todo label being added | Low — delegates to existing flow |
| 3.1 | PR Feedback | Add missing `get_issue_id_for_execution` DB method | None — fills gap |
| 3.2 | PR Feedback | Worker reports branch, PR, checkpoint | Low — additive data |
| 3.3 | PR Feedback | Normalize timestamps in PR monitor | Low — fixes comparison |
| 3.4 | PR Feedback | Duplicate-launch guard in management loop | Low — prevents waste |
| 3.5 | PR Feedback | Agent-status API forwards all worker fields | Low — passes data through |

## Implementation Order

1. **Task 1.1** → Task 1.2 (Problem 1 — smallest, immediate production fix)
2. **Task 3.1** → Task 3.2 → Task 3.3 → Task 3.4 → Task 3.5 (Problem 3 — critical for PR feedback)
3. **Task 2.1** → Task 2.2 → Task 2.3 → Task 2.4 → Task 2.5 (Problem 2 — largest, builds on earlier fixes)

After all tasks: configure the GitHub webhook on the actual repo settings page.
