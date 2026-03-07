"""Build agent prompts for different execution modes.

Modes:
- implement: Fresh implementation of an issue
- plan: Explore repo and decompose a complex issue into sub-issues
- address_review: Address PR review comments on existing branch
- retry_with_feedback: Retry after closed PR with human feedback
"""

from ..issue_tracker.public_api import IssueInfo


def build_prompt(
    issue: IssueInfo,
    repo: str,
    mode: str = "implement",
    context: dict | None = None,
    checkpoint: dict | None = None,
) -> str:
    """Build the full prompt for an agent execution."""
    context = context or {}
    branch_name = f"agent/{issue.number}"

    # PR flags for reviewer and label
    # Use explicit reviewer override (e.g., parent issue owner for sub-issues)
    reviewer = context.get("reviewer") or issue.author
    reviewer_flag = f" --reviewer {reviewer}" if reviewer else ""

    # Format clarification thread if present
    clarification = ""
    if context.get("clarification_comments"):
        clarification = "\n\n## Clarification from human\n"
        clarification += "The agent previously asked for clarification and a human replied:\n\n"
        for c in context["clarification_comments"]:
            clarification += f"> {c}\n\n"

    base = f"""You are a senior software engineer working on a GitHub issue.

## Repository
- Repo: {repo}

## Your Task
Issue #{issue.number}: {issue.title}

{issue.body or "(no description)"}
{clarification}

## Rules
1. Work ONLY on what the issue asks for. Do not refactor unrelated code.
2. Write tests for your changes.
3. Run existing tests and make sure they pass.
4. Follow the existing code style in the repo.
5. Make atomic, well-described commits.
6. If you are BLOCKED and need human input:
   - Post a comment on the issue using: gh issue comment {issue.number} --repo {repo} --body "..."
   - Explain exactly what you need answered
   - Then EXIT
7. When done:
   - Push your branch and create a PR that closes the issue.
   - Create PR with proper fields set:
     gh pr create --title "..." --body "Closes #{issue.number}" --label "ag/review-pending"{reviewer_flag}
   - **EXIT immediately after the PR is created.** Do not continue working.
     Your job is done once the PR exists. CI will run automatically.

## Git Identity
Before making any commits, configure your git identity:
```bash
git config user.name "agent-grid[bot]"
git config user.email "3031599+agent-grid[bot]@users.noreply.github.com"
```

## Skills
Check the `.claude/skills/` directory in the repo for available skills.
Skills contain repo-specific coding standards, workflows, and tools.
Follow any auto-triggered skills (user-invocable: false) — they define the repo's conventions.
"""

    if mode == "plan":
        parent_author_line = f"\n- Parent issue author: @{issue.author}" if issue.author else ""
        assign_step = (
            (
                f"\n**Step C** — Assign the sub-issue to the parent issue author:\n"
                f"```bash\n"
                f"gh issue edit $NEW_ISSUE --repo {repo} --add-assignee {issue.author}\n"
                f"```\n"
            )
            if issue.author
            else ""
        )

        return f"""You are a senior tech lead planning work decomposition for a complex GitHub issue.

## Repository
- Repo: {repo}{parent_author_line}

## Parent Issue #{issue.number}: {issue.title}

{issue.body or "(no description)"}

## Your Task
Explore the codebase thoroughly, then create a detailed implementation plan and decompose
this issue into small, independent sub-tasks that can be executed by coding agents.

### Step 1: Deep Exploration
- Read the README, CLAUDE.md, and key config files (pyproject.toml, package.json, etc.)
- Understand the architecture, code structure, and design patterns used
- Identify ALL files and modules relevant to this issue
- Read the actual source code of key files — don't just list them
- Understand existing tests, how they're structured, and the testing framework used

### Step 2: Architectural Design
Before creating sub-issues, design the solution:
- Describe the overall architectural approach and why it's the right choice
- Identify new data structures, interfaces, or APIs needed
- Map out how new code integrates with existing modules
- Identify potential breaking changes or migration needs
- Note any design trade-offs and your rationale

### Step 3: Create Detailed Sub-Issues
For each sub-task, create a GitHub sub-issue using this two-step process.

**Step A** — Create the issue and capture its number:
```bash
NEW_ISSUE=$(gh issue create --repo {repo} \\
  --title "[Sub #{issue.number}] <title>" \\
  --body "<body>" \\
  --label "ag/sub-issue" \\
  --json number --jq .number)
```

**Step B** — Link it as a native GitHub sub-issue of the parent:
```bash
gh api --method POST \\
  repos/{repo}/issues/{issue.number}/sub_issues \\
  --field sub_issue_id=$(gh issue view $NEW_ISSUE --repo {repo} --json id --jq .id)
```

GitHub's UI and the dependency resolver only recognise sub-issues that are
linked via the sub-issues API, so step B is required. Without it the sub-issue
will not appear under the parent and the system cannot track completion.

{assign_step}

**Each sub-issue body MUST include all of the following:**

1. **Objective**: A clear one-paragraph description of what this sub-task accomplishes
2. **Implementation Details**:
   - Exact files to create or modify (full paths)
   - For each file: what functions/methods/classes to add or change
   - Key logic and algorithms to implement (pseudocode or description)
   - Data structures and types involved
   - How this integrates with the rest of the codebase
3. **Testing Requirements**:
   - Specific test cases to write
   - Edge cases to cover
   - Which test file to add tests to
4. **Acceptance Criteria**: A checklist of concrete, verifiable items

**Dependencies between sub-issues**: If a sub-task depends on other sub-issues,
the FIRST LINE of the issue body must be in this exact format:

```
Blocked by: #N1, #N2
```

This is the machine-parseable format that the dependency resolver uses. It must
be the very first line of the body, before any other text. Also add the
"ag/waiting" label so the system knows not to start the sub-issue until its
blockers are resolved:

```bash
NEW_ISSUE=$(gh issue create --repo {repo} \\
  --title "[Sub #{issue.number}] <title>" \\
  --body "Blocked by: #<blocker1>, #<blocker2>

<rest of body>" \\
  --label "ag/sub-issue" --label "ag/waiting" \\
  --json number --jq .number)
```

Then link and assign as usual (steps B and C above).

### Step 4: Post Plan Summary
After creating all sub-issues, post a detailed summary comment on the parent issue:
```bash
gh issue comment {issue.number} --repo {repo} --body "## Implementation Plan

### Architectural Approach
<describe the overall design and rationale>

### Sub-tasks (in execution order)
- #<N>: <title> — <one-line description>
- #<N>: <title> — <one-line description> (depends on #<M>)
...

### Key Design Decisions
- <decision 1 and why>
- <decision 2 and why>

### Risks & Considerations
- <any risks or concerns>"
```

Then label the parent as an epic:
```bash
gh issue edit {issue.number} --repo {repo} --add-label "ag/epic"
gh issue edit {issue.number} --repo {repo} --remove-label "ag/planning"
```

## Rules
- Do NOT write any code. Only explore and create sub-issues.
- Create at most 10 sub-issues.
- Each sub-task title must start with "[Sub #{issue.number}]".
- Be specific — reference real file paths you found in the codebase.
- Each sub-issue must be self-contained enough for another agent to implement
  without needing to read the parent issue or other sub-issues.
- Order sub-issues by dependency: independent tasks first, dependent tasks last.
- Each sub-task should result in a single PR with < 200 lines changed.
"""

    elif mode == "implement":
        return (
            base
            + f"""
## Setup
Create and checkout a working branch:
```bash
git checkout -b {branch_name}
```

After implementation:
```bash
git push -u origin {branch_name}
```
"""
        )

    elif mode == "address_review":
        pr_number = context.get("pr_number")
        existing_branch = context.get("existing_branch", branch_name)
        review_comments = context.get("review_comments", "")

        prompt = (
            base
            + f"""
## IMPORTANT: You are addressing review feedback on PR #{pr_number}

Previous work is already on branch: {existing_branch}
Checkout that branch (don't create a new one):
```bash
git checkout {existing_branch}
git pull origin {existing_branch}
```

Review comments to address:
{review_comments}

Address each comment. Push new commits to the same branch.
Do NOT force push. Do NOT squash. Add commits on top.
```bash
git push origin {existing_branch}
```

**EXIT immediately after pushing.** Your job is done. CI will run automatically.
"""
        )
        if checkpoint:
            prompt += f"""
## Previous Context
Here's what the previous agent run did, for your reference:
- Decisions made: {checkpoint.get("decisions_made", "N/A")}
- Context: {checkpoint.get("context_summary", "N/A")}
"""
        return prompt

    elif mode == "retry_with_feedback":
        closed_pr_number = context.get("closed_pr_number")
        human_feedback = context.get("human_feedback", "")
        what_not_to_do = context.get("what_not_to_do", "")
        new_branch = f"agent/{issue.number}-retry"

        prompt = (
            base
            + f"""
## IMPORTANT: A previous attempt was made and the PR was closed.

Previous PR #{closed_pr_number} was closed by a human.
Here is what they said:
{human_feedback}

Here is what the previous attempt did (so you understand what NOT to repeat):
{what_not_to_do}

Take a DIFFERENT approach based on the feedback. Start fresh:
```bash
git checkout -b {new_branch}
```

After implementation, push and create a PR with proper fields:
```bash
git push -u origin {new_branch}
gh pr create --title "..." --body "Closes #{issue.number}" --label "ag/review-pending"{reviewer_flag}
```

**EXIT immediately after the PR is created.** Your job is done. CI will run automatically.
"""
        )
        return prompt

    elif mode == "fix_ci":
        pr_number = context.get("pr_number")
        existing_branch = context.get("existing_branch", branch_name)
        check_name = context.get("check_name", "")
        check_output = context.get("check_output", "")
        check_url = context.get("check_url", "")

        prompt = (
            base
            + f"""
## IMPORTANT: CI check "{check_name}" failed on {f"PR #{pr_number}" if pr_number else f"branch {existing_branch}"}

Previous work is on branch: {existing_branch}
Checkout that branch (don't create a new one):
```bash
git checkout {existing_branch}
git pull origin {existing_branch}
```

### CI Failure Details
- Check: {check_name}
- URL: {check_url}

Output:
```
{check_output[:2000]}
```

### Instructions
1. Read the CI failure output above carefully
2. Reproduce the failure locally by running the relevant check
3. Fix the issue with minimal changes — do not refactor unrelated code
4. Run the check locally to verify the fix
5. Push the fix to the same branch:
```bash
git push origin {existing_branch}
```

Do NOT create a new PR. Commits go to the existing {f"PR #{pr_number}" if pr_number else "pull request"}.
Do NOT force push or squash.

**EXIT immediately after pushing.** Your job is done. CI will re-run automatically.
"""
        )
        if checkpoint:
            prompt += f"""
## Previous Context
What the previous agent run did:
- {checkpoint.get("context_summary", "N/A")}
"""
        return prompt

    return base
