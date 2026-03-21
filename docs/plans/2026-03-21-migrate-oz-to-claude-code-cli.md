# Migrate from Oz to Claude Code CLI on Fly Machines

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the Warp Oz execution backend with Claude Code CLI running on Fly Machines — same skills, same CLAUDE.md, same experience as local dev — with subscription auth, session resume on failure, and full event streaming for observability.

**Architecture:** The coordinator (App Runner) stays lightweight — it schedules work and handles webhooks. Fly Machines run Claude Code CLI (`claude -p`) as ephemeral workers. Each machine has subscription credentials + plugins baked into the Docker image. Session JSONL files are synced to S3 so machines can resume from where they left off if they die. Events are streamed back to the coordinator via HTTP callbacks.

**Tech Stack:** Claude Agent SDK v0.1.50 (`claude-agent-sdk`), Claude Code CLI (bundled with SDK), Fly Machines, S3 (session persistence), FastAPI (coordinator), PostgreSQL

**Research validation (March 2026):** No existing platform does what we're building. OpenHands, Open SWE, SWE-agent, Devin, Factory — all use their own agent frameworks, NOT Claude Code. The only way to get the full Claude Code experience (skills, CLAUDE.md, hooks, plugins) headlessly is to run the CLI or Agent SDK directly. Our architecture is the correct approach.

---

## Architecture

```
Coordinator (App Runner — lightweight, always up)
  │
  ├── Receives webhook / cron trigger
  ├── Sanity check (cheap LLM call)
  ├── Spawns Fly Machine with:
  │     • claude -p "..." --session-id {EXECUTION_UUID} --output-format stream-json
  │     • subscription auth (.credentials.json)
  │     • all plugins pre-installed in Docker image
  │     • repo cloned to /workspace/repo
  │
  ├── Machine streams events → coordinator API (batched POST)
  ├── Machine completes → callback with result, branch, PR, cost, session_id
  ├── Session JSONL uploaded to S3 for resume
  │
  └── If machine dies / timeout:
        → New machine downloads session from S3
        → claude --resume {SESSION_ID} -p "continue" --output-format stream-json
        → Picks up exactly where it left off
```

### Key Design Decisions

1. **Coordinator doesn't execute.** Stays lightweight (App Runner 512MB). Fly Machines do the work.

2. **Docker image = developer's machine.** Claude Agent SDK (bundles CLI) + Node.js + git + gh + plugins — all baked in. Same skills, same CLAUDE.md, same hooks as local dev.

3. **Use `claude-agent-sdk` v0.1.50 (not old `claude-code-sdk` v0.0.25).** The new SDK bundles the CLI, has proper session management, hooks callbacks in Python, subagent support, and permission modes. The old package is frozen. The worker can use EITHER the SDK (Python, for programmatic control) OR the CLI directly (`claude -p`, for simplicity). We use the CLI for the worker entrypoint (simpler, proven in spike test) and the SDK is available for future advanced use cases (subagents, Python hooks).

4. **Subscription auth first, API key fallback.** `.credentials.json` stored in Secrets Manager, written to machine on startup. If rate-limited, worker retries with `ANTHROPIC_API_KEY`. Spike test confirmed: subscription auth works headlessly with `claude -p`, shows as `apiKeySource: "none"` with `rateLimitType: "five_hour"` (Max tier).

5. **Session resume via S3.** Sessions stored as JSONL in `~/.claude/projects/<encoded-cwd>/`. On completion/failure, uploaded to S3. On resume, downloaded to the same path. `--session-id EXECUTION_UUID` ensures consistent IDs. Spike test confirmed: `--session-id UUID` creates file at predictable path, `--resume SESSION_ID` restores full conversation history.

6. **Single agent per issue.** No scout → implement split. One `claude -p` run handles everything. The agent explores, assesses feasibility, posts its plan on the issue (via `gh`), and either implements or exits early if blocked. Same behavior as a developer running Claude Code locally.

7. **Event streaming for observability.** Worker captures `--output-format stream-json --verbose` output and POSTs key events (tool calls, text) to coordinator API. Full event log uploaded to S3 for replay. Spike test confirmed: stream-json gives every event including hooks, tool calls, cost tracking (`total_cost_usd`), rate limit info, and session IDs.

8. **Deploy resilience.** Worker uploads session to S3 BEFORE callback to coordinator. If coordinator is restarting during deploy, session is safe. Stale reaper auto-resumes from S3 instead of marking as failed.

### Spike Test Results (validated March 21, 2026)

| Test | Result |
|---|---|
| `--output-format stream-json --verbose` | Works — every event as JSONL |
| Subscription auth headless | Works — `apiKeySource: "none"`, Max tier |
| Plugins/skills loaded | 19 plugins, 62 skills, 46 agents |
| `--session-id UUID` | Works — creates `{UUID}.jsonl` at predictable path |
| `--resume SESSION_ID` | Works — full conversation history restored |
| Cost tracking | `total_cost_usd` in result event |
| Rate limit info | `rate_limit_event` with status/tier/resets |

---

### Task 1: Credentials refresh script

**Files:**
- Create: `scripts/refresh-claude-credentials.sh`

A script that runs on a developer's machine (or CI) to refresh Claude subscription credentials and upload them to AWS Secrets Manager.

```bash
#!/usr/bin/env bash
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
SECRET_NAME="agent-grid/claude-credentials"

# Ensure claude is authenticated
if ! claude auth status 2>/dev/null | grep -q '"loggedIn": true'; then
    echo "Not logged in. Run: claude auth login"
    exit 1
fi

CREDS_FILE="$HOME/.claude/.credentials.json"
if [ ! -f "$CREDS_FILE" ]; then
    echo "Error: $CREDS_FILE not found"
    exit 1
fi

# Upload to Secrets Manager (create or update)
aws secretsmanager put-secret-value \
    --region "$REGION" \
    --secret-id "$SECRET_NAME" \
    --secret-string "$(cat "$CREDS_FILE")" \
    2>/dev/null || \
aws secretsmanager create-secret \
    --region "$REGION" \
    --name "$SECRET_NAME" \
    --secret-string "$(cat "$CREDS_FILE")"

echo "Credentials uploaded to: $SECRET_NAME"
```

**Commit:** `feat: add credential refresh script for Claude subscription auth`

---

### Task 2: Update worker Docker image

**Files:**
- Modify: `Dockerfile.worker`

Replace the old worker image with one that has Claude Code CLI properly installed, with plugins and auth support.

**Key changes:**
- Base: `python:3.12-slim` (keep)
- Install: Node.js 20, git, gh, jq (keep)
- **CHANGE:** `pip install claude-agent-sdk==0.1.50` (new SDK — bundles CLI, no separate `npm install` needed)
- **CHANGE:** `pip install boto3 httpx` (for S3 session sync and coordinator callbacks)
- Non-root user: `agent` (keep — Claude Code requires non-root for `--dangerously-skip-permissions`)
- **NEW:** Pre-create `~/.claude/` directory for credentials injection
- **REMOVE:** Old `npm install -g @anthropic-ai/claude-code` (SDK bundles it)
- **REMOVE:** Old `pip install claude-code-sdk` (replaced by `claude-agent-sdk`)

Note: The `claude-agent-sdk` Python package bundles the Claude Code CLI binary. After `pip install claude-agent-sdk`, the `claude` command is available. No separate Node.js npm install needed for the CLI itself (though Node.js is still needed as a runtime for the CLI).

The entrypoint will be a new script (Task 3).

**Commit:** `feat: update worker Docker image with claude-agent-sdk v0.1.50`

---

### Task 3: New worker entrypoint

**Files:**
- Create: `scripts/worker-entrypoint-v2.sh`

This replaces the old `worker-entrypoint.sh`. The new entrypoint:

1. **Load credentials from Secrets Manager** → write to `~/.claude/.credentials.json`
2. **Clone repo** to `/workspace/repo`
3. **Download session from S3** (if resuming)
4. **Run Claude Code CLI:**
   ```bash
   claude -p "$PROMPT" \
     --session-id "$EXECUTION_ID" \
     --output-format stream-json \
     --verbose \
     --dangerously-skip-permissions \
     --max-turns ${MAX_TURNS:-200} \
     --max-budget-usd ${MAX_BUDGET_USD:-5.0} \
     2>/workspace/stderr.log \
     | tee /workspace/events.jsonl \
     | python3 /workspace/stream-to-coordinator.py
   ```
   If resuming: `claude --resume "$SESSION_ID" -p "$PROMPT" --output-format stream-json --verbose --dangerously-skip-permissions ...`

   **Note:** `--verbose` is required with `--output-format stream-json` (discovered in spike test).

5. **Detect artifacts:** branch name, PR number (via `gh pr list`)
6. **Upload session to S3 FIRST (before callback):** `~/.claude/projects/-workspace-repo/*.jsonl` → `s3://agent-grid-sessions/{EXECUTION_ID}/`. This ensures the session is safe even if the coordinator is restarting during a deploy.
7. **POST callback to coordinator (best effort — non-fatal):**
   ```json
   {
     "execution_id": "...",
     "status": "completed",
     "result": "first 10KB of result",
     "branch": "agent/2105",
     "pr_number": 2144,
     "cost_usd": 1.23,
     "session_id": "...",
     "session_s3_key": "sessions/{EXECUTION_ID}/"
   }
   ```
8. **On failure:** Same callback with `status: "failed"`, still upload session for resume

**The `stream-to-coordinator.py` helper** reads stream-json lines from stdin and POSTs batches of events to the coordinator's `/api/agent-events` endpoint every 5 seconds or 20 events (whichever comes first). This gives near-real-time observability without overwhelming the coordinator.

**Commit:** `feat: new worker entrypoint with CLI, session sync, event streaming`

---

### Task 4: Add agent events streaming endpoint

**Files:**
- Modify: `src/agent_grid/coordinator/public_api.py`

Add a new endpoint for workers to POST events in real-time:

```python
@coordinator_router.post("/agent-events")
async def receive_agent_events(request: Request) -> dict:
    """Receive batched agent events from a worker."""
    events = await request.json()
    db = get_database()
    for event in events:
        await db.record_agent_event(
            execution_id=UUID(event["execution_id"]),
            message_type=event.get("type", ""),
            content=event.get("content", "")[:10000],
            tool_name=event.get("tool_name"),
            tool_id=event.get("tool_id"),
        )
    return {"received": len(events)}
```

**Commit:** `feat: add /api/agent-events endpoint for real-time worker events`

---

### Task 5: Create ClaudeCodeGrid execution backend

**Files:**
- Create: `src/agent_grid/execution_grid/claude_code_grid.py`

This implements `ExecutionGrid` by spawning Fly Machines that run Claude Code CLI. Similar to the existing `FlyExecutionGrid` but adapted for the new worker.

**Key differences from old FlyExecutionGrid:**
- Passes `SESSION_ID` (= execution UUID) as env var
- Passes `RESUME_SESSION_ID` if this is a retry/resume
- Passes `S3_SESSION_BUCKET` for session persistence
- Passes `CLAUDE_CREDENTIALS_SECRET` for auth
- Passes `COORDINATOR_URL` for event streaming + completion callback
- Does NOT pass the full prompt as env var (too large) — instead, stores prompt in S3 and passes the S3 key

**Key differences from OzExecutionGrid:**
- No polling loop — uses HTTP callback (like old Fly)
- Session resume on retry: passes `RESUME_SESSION_ID` to new machine
- `handle_agent_result()` stores session_s3_key in execution record

**Callback handler:** Same as old `FlyExecutionGrid.handle_agent_result()` but also:
- Stores `session_s3_key` in execution metadata
- Runs `on_execution_completed` callback for PR detection/creation
- If result callback has `cost_usd`, persists it

**Commit:** `feat: add ClaudeCodeGrid execution backend (Fly + Claude CLI)`

---

### Task 6: Create Claude Code callbacks (PR detection, DB updates)

**Files:**
- Create: `src/agent_grid/coordinator/claude_code_callbacks.py`

Same pattern as `oz_callbacks.py` — wired during startup. Handles:
- Fallback PR detection (search for branch, create PR if needed)
- Cost persistence
- DB execution result update

Reuse `_create_pr_for_execution` logic from `oz_callbacks.py` but with `removesuffix(".git")` (already fixed).

**Commit:** `feat: add Claude Code execution callbacks`

---

### Task 7: Wire up new backend in config, service, and main

**Files:**
- Modify: `src/agent_grid/config.py`
- Modify: `src/agent_grid/execution_grid/service.py`
- Modify: `src/agent_grid/execution_grid/__init__.py`
- Modify: `src/agent_grid/main.py`

**Config changes:**
```python
execution_backend: Literal["oz", "fly", "claude-code"] = "claude-code"

# Claude Code worker settings
max_turns_per_execution: int = 200
max_budget_per_execution_usd: float = 5.0
claude_credentials_secret: str = "agent-grid/claude-credentials"
session_s3_bucket: str = "agent-grid-sessions"
```

**Service changes:** Add `"claude-code"` case to `get_execution_grid()`.

**Main changes:**
- On startup: initialize `ClaudeCodeGrid`, wire callbacks
- On shutdown: close grid (cancel running machines)

**Commit:** `feat: wire Claude Code backend as default execution grid`

---

### Task 8: Simplify the pipeline (remove scout split)

**Files:**
- Modify: `src/agent_grid/coordinator/management_loop.py`
- Modify: `src/agent_grid/coordinator/scheduler.py`
- Modify: `src/agent_grid/coordinator/agent_launcher.py`
- Modify: `src/agent_grid/coordinator/prompt_builder.py`

**Changes:**
- After sanity check, launch `launch_simple()` directly (not `launch_scout()`)
- Remove: `launch_scout`, `parse_scout_result`, `handle_scout_completed`
- Remove: scout mode from prompt builder
- Remove: scout handling from `_handle_agent_completed`
- Update implement prompt with strong "explore and assess first" instructions:
  ```
  ## Before Writing Any Code

  1. Explore the codebase thoroughly. Read relevant files, understand the
     architecture, check recent git history for related changes.

  2. Assess feasibility. If you genuinely need human input that you cannot
     determine from the code (credentials, business policy decisions,
     choosing between fundamentally different product directions):
     - Post a comment explaining exactly what you need:
       gh issue comment {issue.number} --repo {repo} --body "**Blocked — need clarification:**\n\n<your question>"
     - Then EXIT immediately. Do not attempt to implement.

  3. If feasible, post your implementation plan before coding:
     gh issue comment {issue.number} --repo {repo} --body "## Implementation Plan\n\n<your plan>"

  4. Then implement, test, and push.
  ```

**Commit:** `feat: simplify pipeline — single agent per issue, no scout split`

---

### Task 9: Add session resume to retry/CI-fix flows

**Files:**
- Modify: `src/agent_grid/coordinator/agent_launcher.py`
- Modify: `src/agent_grid/coordinator/database.py`

When retrying a failed execution or fixing CI, pass the previous session ID so the new machine can resume:

```python
async def launch_ci_fix(self, repo, check_info):
    # ... existing code ...
    # Get previous session for this issue
    prev_execution = await self._db.get_execution_for_issue(issue_id)
    resume_session_id = str(prev_execution.id) if prev_execution else None

    launched = await self.claim_and_launch(
        issue_id=issue_id,
        ...,
        context={
            ...,
            "resume_session_id": resume_session_id,
        },
    )
```

The `ClaudeCodeGrid.launch_agent()` passes `resume_session_id` as an env var to the Fly Machine, and the worker entrypoint downloads the session from S3 and uses `--resume`.

Also update `_reap_stale_in_progress` and `_check_in_progress` in the management loop: when an execution is reaped (timed out or orphaned), check if a session exists in S3. If yes, **auto-resume** instead of marking as failed — spawn a new machine with `RESUME_SESSION_ID` so the agent picks up where it left off. This handles the deploy interruption case gracefully.

**Commit:** `feat: session resume for retries, CI fixes, and deploy recovery`

---

### Task 10: Update callback endpoint for new worker format

**Files:**
- Modify: `src/agent_grid/coordinator/public_api.py`

Update `/api/agent-status` to accept the new fields from the v2 worker:
- `cost_usd` (float, from Claude CLI's `total_cost_usd`)
- `session_id` (string)
- `session_s3_key` (string)

Store session info in execution metadata for future resume.

**Commit:** `feat: update agent-status callback for session and cost data`

---

### Task 11: Tests

**Files:**
- Create: `tests/test_claude_code_grid.py`
- Modify: existing tests that reference Oz

Test:
- `ClaudeCodeGrid.launch_agent` creates execution and spawns machine
- `handle_agent_result` updates DB, detects PR
- Callback wiring (PR creation, cost persistence)
- Session resume: context passed correctly to new machine

**Commit:** `test: add Claude Code grid tests`

---

### Task 12: Clean up Oz code and update dependencies

**Files:**
- Delete: `src/agent_grid/execution_grid/oz_grid.py`
- Delete: `src/agent_grid/coordinator/oz_callbacks.py`
- Modify: `src/agent_grid/execution_grid/__init__.py` (remove Oz imports)
- Modify: `src/agent_grid/execution_grid/service.py` (remove Oz branch)
- Modify: `src/agent_grid/main.py` (remove Oz startup/shutdown)
- Modify: `pyproject.toml`:
  - Remove: `oz-agent-sdk`
  - Remove: `claude-code-sdk` (old frozen package)
  - Add: `claude-agent-sdk = "^0.1.50"` (new official SDK)
  - Add: `boto3` (for S3 session persistence)

Also remove any remaining imports of `claude_code_sdk` in `agent_runner.py` and update to `claude_agent_sdk` if the local runner is kept.

**Commit:** `chore: remove Oz backend, update to claude-agent-sdk v0.1.50`

---

## S3 Session Storage Layout

```
s3://agent-grid-sessions/
  {EXECUTION_UUID}/
    session.jsonl          # Claude Code session history
    events.jsonl           # Full stream-json event log
    prompt.txt             # Original prompt (if too large for env var)
```

## Environment Variables Passed to Fly Machine

```bash
EXECUTION_ID          # UUID — also used as session ID
REPO_URL              # https://github.com/owner/repo.git
ISSUE_NUMBER          # For logging
MODE                  # implement, fix_ci, address_review, rebase, retry
PROMPT_S3_KEY         # S3 key for the prompt (avoids env var size limits)
RESUME_SESSION_ID     # Previous session UUID (for resume)
COORDINATOR_URL       # https://coordinator.example.com
GITHUB_TOKEN          # Installation token for gh CLI
ANTHROPIC_API_KEY     # Fallback auth (if subscription fails)
CLAUDE_CREDENTIALS_SECRET  # Secrets Manager key for subscription auth
S3_SESSION_BUCKET     # Bucket for session persistence
MAX_TURNS             # --max-turns value
MAX_BUDGET_USD        # --max-budget-usd value
AWS_REGION            # For Secrets Manager + S3
```

## Migration Checklist

Before deploying:

- [ ] Run `scripts/refresh-claude-credentials.sh` to upload subscription credentials
- [ ] Create S3 bucket: `agent-grid-sessions`
- [ ] Verify `ANTHROPIC_API_KEY` is in Secrets Manager as fallback
- [ ] Build and push new worker Docker image
- [ ] Update App Runner env: `AGENT_GRID_EXECUTION_BACKEND=claude-code`
- [ ] Verify Fly Machine can pull new worker image
- [ ] Test locally with one issue
- [ ] Deploy and verify with a real issue
