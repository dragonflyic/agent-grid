#!/usr/bin/env python3
"""Read Claude Code stream-json from stdin and POST events to coordinator.

Batches events and sends every 5 seconds or 20 events, whichever comes first.
Non-fatal — if the coordinator is down, events are dropped (they're still in events.jsonl).
"""

import asyncio
import json
import os
import sys
import time

import httpx

COORDINATOR_URL = os.environ.get("COORDINATOR_URL", "")
EXECUTION_ID = os.environ.get("EXECUTION_ID", "")
BATCH_SIZE = 20
FLUSH_INTERVAL = 5.0  # seconds


async def main():
    if not COORDINATOR_URL or not EXECUTION_ID:
        # No coordinator — just pass through stdin to stdout
        for line in sys.stdin:
            sys.stdout.write(line)
            sys.stdout.flush()
        return

    batch: list[dict] = []
    last_flush = time.time()
    endpoint = f"{COORDINATOR_URL}/api/agent-events"

    async with httpx.AsyncClient(timeout=10.0) as client:
        for line in sys.stdin:
            # Pass through to stdout (for tee to capture in events.jsonl)
            sys.stdout.write(line)
            sys.stdout.flush()

            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
                event_type = event.get("type", "")

                # Only send interesting events (skip system init, partial messages)
                if event_type in ("assistant", "tool_result", "result", "system"):
                    batch_event: dict = {
                        "execution_id": EXECUTION_ID,
                        "type": event_type,
                    }

                    if event_type == "assistant":
                        content = event.get("message", {}).get("content", [])
                        for block in content:
                            if isinstance(block, dict):
                                if block.get("type") == "text":
                                    batch_event["content"] = block.get("text", "")[:500]
                                elif block.get("type") == "tool_use":
                                    batch_event["content"] = json.dumps(
                                        block.get("input", {})
                                    )[:500]
                                    batch_event["tool_name"] = block.get("name")
                                    batch_event["tool_id"] = block.get("id")
                                    batch.append(dict(batch_event))
                                    continue
                        if "content" in batch_event:
                            batch.append(batch_event)

                    elif event_type == "tool_result":
                        content = event.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                str(c.get("text", ""))
                                for c in content
                                if isinstance(c, dict)
                            )
                        batch_event["content"] = str(content)[:500]
                        batch_event["tool_name"] = event.get("tool_name")
                        batch_event["tool_id"] = event.get("tool_use_id")
                        batch.append(batch_event)

                    elif event_type == "result":
                        batch_event["content"] = event.get("result", "")[:500]
                        batch.append(batch_event)

                    elif event_type == "system" and event.get("subtype") not in (
                        "init",
                        "hook_started",
                        "hook_response",
                    ):
                        batch_event["content"] = json.dumps(event)[:500]
                        batch.append(batch_event)

            except (json.JSONDecodeError, KeyError):
                pass

            # Flush if batch is full or interval elapsed
            now = time.time()
            if len(batch) >= BATCH_SIZE or (
                batch and now - last_flush >= FLUSH_INTERVAL
            ):
                try:
                    await client.post(endpoint, json=batch)
                except Exception:
                    pass  # Non-fatal — events are in events.jsonl
                batch = []
                last_flush = now

        # Final flush
        if batch:
            try:
                async with httpx.AsyncClient(timeout=10.0) as final_client:
                    await final_client.post(endpoint, json=batch)
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
