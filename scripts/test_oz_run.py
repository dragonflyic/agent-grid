"""Quick smoke test for Oz execution grid.

Submits a simple agent run to Oz, polls for completion, and prints results.
Usage: python scripts/test_oz_run.py
"""

import asyncio
import os
import re
import sys
import time

# Load .env
from pathlib import Path

env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            value = value.strip("'\"")
            os.environ.setdefault(key.strip(), value)

WARP_API_KEY = os.environ.get("AGENT_GRID_WARP_API_KEY", "")
OZ_ENVIRONMENT_ID = os.environ.get("AGENT_GRID_OZ_ENVIRONMENT_ID", "")
TARGET_REPO = os.environ.get("AGENT_GRID_TARGET_REPO", "")

if not WARP_API_KEY:
    print("ERROR: AGENT_GRID_WARP_API_KEY not set")
    sys.exit(1)


async def main():
    from oz_agent_sdk import AsyncOzAPI

    client = AsyncOzAPI(api_key=WARP_API_KEY)

    # Simple prompt — just list repo files (fast, no side effects)
    prompt = (
        f"List the top-level files in the {TARGET_REPO} repository "
        "and briefly describe the project structure. Do NOT make any changes."
    )

    config = {}
    if OZ_ENVIRONMENT_ID:
        config["environment_id"] = OZ_ENVIRONMENT_ID

    print(f"Submitting Oz run...")
    print(f"  Environment: {OZ_ENVIRONMENT_ID or '(default)'}")
    print(f"  Repo: {TARGET_REPO}")
    print()

    try:
        response = await client.agent.run(
            prompt=prompt,
            config=config if config else None,
            title="Smoke test — read-only",
        )
    except Exception as e:
        print(f"FAILED to submit run: {e}")
        await client.close()
        sys.exit(1)

    run_id = response.run_id
    print(f"Run created: {run_id}")
    if hasattr(response, "session_link") and response.session_link:
        print(f"Session: {response.session_link}")
    print()

    # Poll for completion
    terminal_states = {"SUCCEEDED", "FAILED", "CANCELLED"}
    start = time.time()
    poll_interval = 10

    while True:
        elapsed = time.time() - start
        try:
            run = await client.agent.runs.retrieve(run_id)
        except Exception as e:
            print(f"  [{elapsed:.0f}s] Poll error: {e}")
            await asyncio.sleep(poll_interval)
            continue

        state = run.state
        print(f"  [{elapsed:.0f}s] State: {state}")

        if state in terminal_states:
            print()
            print(f"=== Run {state} (took {elapsed:.0f}s) ===")

            # Print result message
            if run.status_message:
                print(f"Message: {run.status_message.message}")

            # Check for artifacts
            if run.artifacts:
                print(f"Artifacts ({len(run.artifacts)}):")
                for artifact in run.artifacts:
                    print(f"  - type={artifact.artifact_type}")
                    if artifact.artifact_type == "PULL_REQUEST":
                        print(f"    branch={artifact.data.branch}")
                        print(f"    url={artifact.data.url}")
            else:
                print("No artifacts (expected for read-only test)")

            # Print session link if available
            if hasattr(run, "session_link") and run.session_link:
                print(f"Session: {run.session_link}")

            # Print cost info if available
            if hasattr(run, "request_usage") and run.request_usage:
                usage = run.request_usage
                print(f"Usage: {usage}")

            break

        if elapsed > 300:
            print("TIMEOUT — giving up after 5 minutes")
            break

        await asyncio.sleep(poll_interval)

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
