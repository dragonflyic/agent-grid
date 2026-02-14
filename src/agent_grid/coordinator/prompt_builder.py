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
   - If the repo has a `/ship` skill (check .claude/skills/), use it:
     `/ship` with a commit message, PR title, and body that references "Closes #{issue.number}"
   - Otherwise, manually:
     - Push your branch
     - Create a PR using: gh pr create --title "..." --body "Closes #{issue.number}\\n\\n..."
     - Link PR to issue: gh pr edit --add-issue #{issue.number}

## Skills
Check the `.claude/skills/` directory in the repo for available skills.
Skills contain repo-specific coding standards, workflows, and tools.
Follow any auto-triggered skills (user-invocable: false) — they define the repo's conventions.
"""

    if mode == "plan":
        return f"""You are a senior tech lead planning work decomposition for a complex GitHub issue.

## Repository
- Repo: {repo}

## Parent Issue #{issue.number}: {issue.title}

{issue.body or "(no description)"}

## Your Task
Explore the codebase thoroughly, then decompose this issue into small, independent sub-tasks.

### Step 1: Explore
- Read the README, CLAUDE.md, and key config files (pyproject.toml, package.json, etc.)
- Understand the architecture and code structure
- Identify the files and modules relevant to this issue

### Step 2: Plan
Break the issue into sub-tasks where each sub-task:
- Can be done in a single PR (< 200 lines changed)
- Has a clear, specific scope
- Includes concrete file paths that will need changes

### Step 3: Create Sub-Issues
For each sub-task, create a GitHub sub-issue:
```bash
gh issue create --repo {repo} --title "[Sub #{issue.number}] <title>" --body "<body>" --label "ag/sub-issue"
```

Each sub-issue body should include:
- "Part of #{issue.number}" on the first line
- What to implement and why
- Specific files to modify
- Acceptance criteria as a checklist

If a sub-task depends on another, add the label "ag/waiting" too:
```bash
gh issue create --repo {repo} \
  --title "[Sub #{issue.number}] <title>" --body "<body>" \
  --label "ag/sub-issue" --label "ag/waiting"
```

### Step 4: Post Plan Summary
After creating all sub-issues, post a summary comment on the parent issue:
```bash
gh issue comment {issue.number} --repo {repo} --body "## Implementation Plan

<brief summary of approach>

### Sub-tasks
- #<N>: <title>
- #<N>: <title>
...

### Risks
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

After implementation:
```bash
git push -u origin {new_branch}
```
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
## IMPORTANT: CI check "{check_name}" failed on your PR #{pr_number}

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

Do NOT create a new PR. Your commits will be added to the existing PR #{pr_number}.
Do NOT force push or squash.
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
