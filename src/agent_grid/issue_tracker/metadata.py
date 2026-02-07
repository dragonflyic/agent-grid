"""Parse and embed structured metadata in GitHub issue comments.

Metadata is stored as hidden HTML comments:
<!-- TECH_LEAD_AGENT_META {"key": "value"} -->
"""

import json
import re

METADATA_PATTERN = re.compile(
    r"<!--\s*TECH_LEAD_AGENT_META\s*(\{.*?\})\s*-->",
    re.DOTALL,
)


def embed_metadata(comment_body: str, metadata: dict) -> str:
    """Append hidden metadata to a comment body."""
    meta_str = json.dumps(metadata, separators=(",", ":"))
    return f"{comment_body}\n\n<!-- TECH_LEAD_AGENT_META {meta_str} -->"


def extract_metadata(comment_body: str) -> dict | None:
    """Extract metadata from a comment body, if present."""
    match = METADATA_PATTERN.search(comment_body)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return None


def strip_metadata(comment_body: str) -> str:
    """Remove metadata from a comment body."""
    return METADATA_PATTERN.sub("", comment_body).strip()
