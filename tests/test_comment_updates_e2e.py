"""
End-to-end test for GitHub issue comment updates.

This test verifies the flow:
1. Create a blocked issue with an agent comment
2. Add human comment updates to the issue
3. Verify blocker_resolver detects the human comments
4. Verify the issue becomes unblocked and ready for agent launch

Requirements:
- AGENT_GRID_GITHUB_TOKEN environment variable must be set
- Creates real GitHub issues (with [TEST] prefix) and closes them after test

Run with:
    export AGENT_GRID_GITHUB_TOKEN=ghp_...
    poetry run pytest tests/test_comment_updates_e2e.py -v -s

Note: These tests will be skipped in CI if the GitHub token is not available.
"""

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_grid.coordinator.blocker_resolver import BlockerResolver
from agent_grid.issue_tracker import IssueStatus
from agent_grid.issue_tracker.github_client import GitHubClient
from agent_grid.issue_tracker.metadata import embed_metadata, extract_metadata

# Test configuration
TEST_REPO = "dragonflyic/agent-grid"


def get_github_token():
    """Get GitHub token from environment."""
    token = os.environ.get("AGENT_GRID_GITHUB_TOKEN")
    if not token:
        pytest.skip("AGENT_GRID_GITHUB_TOKEN not set")
    return token


@pytest.fixture
def temp_repo_dir():
    """Create temporary directory for repo operations."""
    with tempfile.TemporaryDirectory(prefix="agent-grid-test-") as tmpdir:
        yield Path(tmpdir)


class TestCommentUpdatesE2E:
    """
    End-to-end tests for GitHub issue comment updates and blocker resolution.

    These tests create real issues with comments and verify that the blocker
    resolver correctly detects human responses to blocked issues.
    """

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)  # 5 minute timeout
    async def test_blocked_issue_with_human_comment(self):
        """
        Test the full blocker resolution flow:
        1. Create a test issue
        2. Add an agent blocking comment with metadata
        3. Add a human response comment
        4. Verify blocker_resolver detects the human comment
        5. Clean up
        """
        token = get_github_token()
        github_client = GitHubClient(token=token)
        issue = None

        try:
            # Step 1: Create a test issue
            print("\n=== Step 1: Creating test issue ===")
            issue = await github_client.create_issue(
                repo=TEST_REPO,
                title="[TEST] Blocked issue - needs clarification",
                body="""This is an automated test issue for comment updates.

This issue simulates a blocked state where the agent needs human clarification.

The test will:
1. Add an agent blocking comment
2. Add a human response
3. Verify blocker resolver detects the response

This issue will be automatically closed after the test completes.""",
                labels=["test", "automated", "ag/blocked"],
            )
            print(f"Created issue #{issue.id}: {issue.html_url}")

            # Step 2: Add an agent blocking comment with metadata
            print("\n=== Step 2: Adding agent blocking comment ===")
            blocking_question = """**Agent needs clarification:**

Could you please clarify the following:
- What specific approach should I take?
- Are there any constraints I should be aware of?

Please respond with your answers so I can proceed."""

            blocking_comment = embed_metadata(
                blocking_question,
                {"type": "blocked", "reason": "Needs clarification on approach"},
            )
            await github_client.add_comment(TEST_REPO, issue.id, blocking_comment)
            print("Added blocking comment with metadata")

            # Step 3: Add a human response comment
            print("\n=== Step 3: Adding human response comment ===")
            human_response = """Thanks for asking! Here's the clarification:

- Use the standard approach with error handling
- Make sure to add unit tests
- Follow the existing code patterns in the repo

Hope this helps!"""

            await github_client.add_comment(TEST_REPO, issue.id, human_response)
            print("Added human response comment")

            # Step 4: Fetch the issue with comments and verify structure
            print("\n=== Step 4: Verifying issue structure ===")
            issue_with_comments = await github_client.get_issue(TEST_REPO, issue.id)
            print(f"Issue has {len(issue_with_comments.comments)} comments")

            # Verify we have at least 2 comments
            assert len(issue_with_comments.comments) >= 2, "Issue should have at least 2 comments"

            # Verify the blocking comment has metadata
            blocking_comment_found = False
            human_comment_found = False

            for i, comment in enumerate(issue_with_comments.comments):
                meta = extract_metadata(comment.body)
                print(f"Comment {i}: has_metadata={meta is not None}, " f"type={meta.get('type') if meta else 'N/A'}")

                if meta and meta.get("type") == "blocked":
                    blocking_comment_found = True
                elif meta is None and "clarification" in comment.body.lower():
                    human_comment_found = True

            assert blocking_comment_found, "Blocking comment with metadata not found"
            assert human_comment_found, "Human response comment not found"

            # Step 5: Use blocker resolver to detect the human response
            print("\n=== Step 5: Testing blocker resolver ===")
            blocker_resolver = BlockerResolver()
            blocker_resolver._tracker = github_client

            # Check if the issue is detected as unblocked
            unblocked_issues = await blocker_resolver.check_blocked_issues(TEST_REPO)
            print(f"Blocker resolver found {len(unblocked_issues)} unblocked issues")

            # Verify our issue is in the unblocked list
            issue_ids = [str(i.id) for i in unblocked_issues]
            assert issue.id in issue_ids, f"Issue #{issue.id} should be detected as unblocked"

            # Verify the unblocked issue has comments
            unblocked_issue = next((i for i in unblocked_issues if str(i.id) == issue.id), None)
            assert unblocked_issue is not None
            assert len(unblocked_issue.comments) >= 2, "Unblocked issue should have comments"

            print("\n=== Test PASSED ===")
            print(f"✅ Issue #{issue.id} correctly detected as unblocked after human comment")

        finally:
            # Cleanup - close the issue
            if issue:
                print(f"\n=== Cleanup: Closing issue #{issue.id} ===")
                try:
                    await github_client.add_comment(
                        TEST_REPO,
                        issue.id,
                        "✅ Test completed successfully. Closing issue.",
                    )
                    await github_client.update_issue_status(TEST_REPO, issue.id, IssueStatus.CLOSED)
                    print(f"Issue #{issue.id} closed")
                except Exception as e:
                    print(f"Warning: Failed to close issue: {e}")
            await github_client.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    async def test_blocked_issue_without_human_response(self):
        """
        Test that blocked issues WITHOUT human responses are not detected as unblocked.

        This is the negative test case to ensure blocker_resolver doesn't
        incorrectly detect issues as unblocked.
        """
        token = get_github_token()
        github_client = GitHubClient(token=token)
        issue = None

        try:
            # Step 1: Create a test issue
            print("\n=== Step 1: Creating test issue ===")
            issue = await github_client.create_issue(
                repo=TEST_REPO,
                title="[TEST] Blocked issue - no human response",
                body="""This is an automated test issue for comment updates.

This issue tests that blocker_resolver does NOT detect an issue as unblocked
when there's only an agent comment but no human response.

This issue will be automatically closed after the test completes.""",
                labels=["test", "automated", "ag/blocked"],
            )
            print(f"Created issue #{issue.id}: {issue.html_url}")

            # Step 2: Add an agent blocking comment with metadata
            print("\n=== Step 2: Adding agent blocking comment ===")
            blocking_comment = embed_metadata(
                "**Agent needs clarification:**\n\nPlease provide more details.",
                {"type": "blocked", "reason": "Needs more details"},
            )
            await github_client.add_comment(TEST_REPO, issue.id, blocking_comment)
            print("Added blocking comment with metadata")

            # Step 3: Add another agent comment (not a human response)
            print("\n=== Step 3: Adding another agent comment ===")
            agent_followup = embed_metadata(
                "Still waiting for clarification...",
                {"type": "reminder", "reason": "Following up"},
            )
            await github_client.add_comment(TEST_REPO, issue.id, agent_followup)
            print("Added agent follow-up comment")

            # Step 4: Verify blocker resolver does NOT detect this as unblocked
            print("\n=== Step 4: Testing blocker resolver ===")
            blocker_resolver = BlockerResolver()
            blocker_resolver._tracker = github_client

            unblocked_issues = await blocker_resolver.check_blocked_issues(TEST_REPO)
            print(f"Blocker resolver found {len(unblocked_issues)} unblocked issues")

            # Verify our issue is NOT in the unblocked list
            issue_ids = [str(i.id) for i in unblocked_issues]
            assert issue.id not in issue_ids, (
                f"Issue #{issue.id} should NOT be detected as unblocked " f"(no human response present)"
            )

            print("\n=== Test PASSED ===")
            print(f"✅ Issue #{issue.id} correctly remains blocked (no human response)")

        finally:
            # Cleanup
            if issue:
                print(f"\n=== Cleanup: Closing issue #{issue.id} ===")
                try:
                    await github_client.add_comment(
                        TEST_REPO,
                        issue.id,
                        "✅ Test completed successfully. Closing issue.",
                    )
                    await github_client.update_issue_status(TEST_REPO, issue.id, IssueStatus.CLOSED)
                    print(f"Issue #{issue.id} closed")
                except Exception as e:
                    print(f"Warning: Failed to close issue: {e}")
            await github_client.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    async def test_multiple_human_comments(self):
        """
        Test that blocker_resolver handles multiple human comments correctly.

        Verifies that when multiple humans respond, the issue is still
        correctly detected as unblocked.
        """
        token = get_github_token()
        github_client = GitHubClient(token=token)
        issue = None

        try:
            # Step 1: Create a test issue
            print("\n=== Step 1: Creating test issue ===")
            issue = await github_client.create_issue(
                repo=TEST_REPO,
                title="[TEST] Blocked issue - multiple human responses",
                body="""This is an automated test issue for comment updates.

This tests multiple human responses to a blocked issue.

This issue will be automatically closed after the test completes.""",
                labels=["test", "automated", "ag/blocked"],
            )
            print(f"Created issue #{issue.id}: {issue.html_url}")

            # Step 2: Add blocking comment
            print("\n=== Step 2: Adding blocking comment ===")
            blocking_comment = embed_metadata(
                "**Agent needs clarification:**\n\nWhat approach should I use?",
                {"type": "blocked", "reason": "Needs approach clarification"},
            )
            await github_client.add_comment(TEST_REPO, issue.id, blocking_comment)

            # Step 3: Add multiple human responses
            print("\n=== Step 3: Adding multiple human responses ===")
            await github_client.add_comment(TEST_REPO, issue.id, "I think approach A would work.")
            await github_client.add_comment(TEST_REPO, issue.id, "Actually, let me add: make sure to test thoroughly.")
            print("Added 2 human response comments")

            # Step 4: Verify blocker resolver detects this as unblocked
            print("\n=== Step 4: Testing blocker resolver ===")
            blocker_resolver = BlockerResolver()
            blocker_resolver._tracker = github_client

            unblocked_issues = await blocker_resolver.check_blocked_issues(TEST_REPO)

            # Verify our issue is in the unblocked list
            issue_ids = [str(i.id) for i in unblocked_issues]
            assert issue.id in issue_ids, f"Issue #{issue.id} should be detected as unblocked"

            print("\n=== Test PASSED ===")
            print(f"✅ Issue #{issue.id} correctly detected with multiple human comments")

        finally:
            # Cleanup
            if issue:
                print(f"\n=== Cleanup: Closing issue #{issue.id} ===")
                try:
                    await github_client.add_comment(
                        TEST_REPO,
                        issue.id,
                        "✅ Test completed successfully. Closing issue.",
                    )
                    await github_client.update_issue_status(TEST_REPO, issue.id, IssueStatus.CLOSED)
                except Exception as e:
                    print(f"Warning: Failed to close issue: {e}")
            await github_client.close()
