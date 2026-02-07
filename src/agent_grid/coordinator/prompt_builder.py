"""Build agent prompts for different execution modes.

Modes:
- implement: Fresh implementation of an issue
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

    base = f"""You are a senior software engineer working on a GitHub issue.

## Repository
- Repo: {repo}

## Your Task
Issue #{issue.number}: {issue.title}

{issue.body or '(no description)'}

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
   - Push your branch
   - Create a PR using: gh pr create --title "..." --body "..."
   - Link the PR to the issue with "Closes #{issue.number}" in the body
   - After creating the PR, link it to the issue: gh pr edit --add-issue #{issue.number}
"""

    if mode == "implement":
        return base + f"""
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

    elif mode == "address_review":
        pr_number = context.get("pr_number")
        existing_branch = context.get("existing_branch", branch_name)
        review_comments = context.get("review_comments", "")

        prompt = base + f"""
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
        if checkpoint:
            prompt += f"""
## Previous Context
Here's what the previous agent run did, for your reference:
- Decisions made: {checkpoint.get('decisions_made', 'N/A')}
- Context: {checkpoint.get('context_summary', 'N/A')}
"""
        return prompt

    elif mode == "retry_with_feedback":
        closed_pr_number = context.get("closed_pr_number")
        human_feedback = context.get("human_feedback", "")
        what_not_to_do = context.get("what_not_to_do", "")
        new_branch = f"agent/{issue.number}-retry"

        prompt = base + f"""
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
        return prompt

    return base
