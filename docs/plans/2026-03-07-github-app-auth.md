# GitHub App Authentication Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the static GitHub PAT (`github_token`) with GitHub App authentication so agent-grid acts as a bot identity (`fresa-agent-grid[bot]`) on the `dragonflyic` org.

**Architecture:** A new `github_app.py` module generates short-lived installation tokens from the App's private key. The coordinator uses these tokens for all GitHub API calls. Workers receive a fresh installation token (valid 1 hour) as `GITHUB_TOKEN` — no worker-side changes needed.

**Tech Stack:** PyJWT with cryptography backend (RS256 signing), GitHub App REST API

---

### Task 1: Add PyJWT dependency

**Files:**
- Modify: `pyproject.toml:9-20`

**Step 1: Add PyJWT[crypto] to dependencies**

In `pyproject.toml`, add `PyJWT` with the `crypto` extra (provides `cryptography` for RS256):

```toml
[tool.poetry.dependencies]
# ... existing deps ...
PyJWT = {version = "^2.8", extras = ["crypto"]}
```

Add it after the `anthropic` line (line 19).

**Step 2: Install the dependency**

Run: `poetry lock --no-update && poetry install`
Expected: PyJWT and cryptography installed successfully

**Step 3: Commit**

```bash
git add pyproject.toml poetry.lock
git commit -m "feat: add PyJWT[crypto] dependency for GitHub App auth"
```

---

### Task 2: Update config settings

**Files:**
- Modify: `src/agent_grid/config.py:18-20`

**Step 1: Replace github_token with GitHub App settings**

Replace lines 18-20 in `config.py`:

```python
    # GitHub (only used when issue_tracker_type is "github")
    github_token: str = ""
    github_webhook_secret: str = ""
```

With:

```python
    # GitHub App authentication (only used when issue_tracker_type is "github")
    github_app_id: str = ""
    github_app_private_key: str = ""  # PEM-encoded private key content
    github_app_installation_id: str = ""
    github_webhook_secret: str = ""
```

**Step 2: Verify config loads**

Run: `python -c "from agent_grid.config import Settings; s = Settings(); print(s.github_app_id)"`
Expected: prints empty string, no errors

**Step 3: Commit**

```bash
git add src/agent_grid/config.py
git commit -m "feat: replace github_token config with GitHub App settings"
```

---

### Task 3: Create github_app.py module

**Files:**
- Create: `src/agent_grid/github_app.py`
- Create: `tests/test_github_app.py`

**Step 1: Write the test**

Create `tests/test_github_app.py`:

```python
"""Tests for GitHub App token generation."""

import time
from unittest.mock import AsyncMock, patch

import jwt
import pytest

from agent_grid.github_app import GitHubAppAuth


# Generate a real RSA key pair for testing
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

_test_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TEST_PEM = _test_private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()


class TestGitHubAppAuth:
    def setup_method(self):
        self.auth = GitHubAppAuth(
            app_id="12345",
            private_key=TEST_PEM,
            installation_id="67890",
        )

    def test_generate_jwt(self):
        token = self.auth._generate_jwt()
        decoded = jwt.decode(token, _test_private_key.public_key(), algorithms=["RS256"])
        assert decoded["iss"] == "12345"
        assert "exp" in decoded
        assert "iat" in decoded
        # JWT should expire in 10 minutes
        assert decoded["exp"] - decoded["iat"] == 600

    @pytest.mark.asyncio
    async def test_get_installation_token_fresh(self):
        mock_response = AsyncMock()
        mock_response.json.return_value = {
            "token": "ghs_test_token_abc",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        mock_response.raise_for_status = lambda: None

        with patch("agent_grid.github_app.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            token = await self.auth.get_installation_token()
            assert token == "ghs_test_token_abc"

            # Verify the API was called correctly
            mock_client.post.assert_called_once()
            call_url = mock_client.post.call_args[0][0]
            assert "/app/installations/67890/access_tokens" in call_url

    @pytest.mark.asyncio
    async def test_get_installation_token_cached(self):
        """Token should be cached and not re-fetched."""
        self.auth._cached_token = "ghs_cached"
        self.auth._token_expires_at = time.time() + 3600  # expires in 1 hour

        token = await self.auth.get_installation_token()
        assert token == "ghs_cached"

    @pytest.mark.asyncio
    async def test_get_installation_token_expired_refreshes(self):
        """Expired token should trigger a refresh."""
        self.auth._cached_token = "ghs_old"
        self.auth._token_expires_at = time.time() - 60  # expired 1 minute ago

        mock_response = AsyncMock()
        mock_response.json.return_value = {
            "token": "ghs_refreshed",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        mock_response.raise_for_status = lambda: None

        with patch("agent_grid.github_app.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            token = await self.auth.get_installation_token()
            assert token == "ghs_refreshed"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_github_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_grid.github_app'`

**Step 3: Write the implementation**

Create `src/agent_grid/github_app.py`:

```python
"""GitHub App authentication.

Generates short-lived installation tokens from a GitHub App's
private key. Tokens are cached and refreshed 5 minutes before expiry.
"""

import logging
import time

import httpx
import jwt

from .config import settings

logger = logging.getLogger("agent_grid.github_app")


class GitHubAppAuth:
    """Manages GitHub App JWT generation and installation token lifecycle."""

    GITHUB_API_BASE = "https://api.github.com"

    def __init__(
        self,
        app_id: str | None = None,
        private_key: str | None = None,
        installation_id: str | None = None,
    ):
        self._app_id = app_id or settings.github_app_id
        self._private_key = private_key or settings.github_app_private_key
        self._installation_id = installation_id or settings.github_app_installation_id
        self._cached_token: str | None = None
        self._token_expires_at: float = 0

    def _generate_jwt(self) -> str:
        """Generate a JWT signed with the App's private key (10-min expiry)."""
        now = int(time.time())
        payload = {
            "iat": now - 60,  # issued 60s ago to account for clock drift
            "exp": now + 540,  # 10 min total (600s from iat)
            "iss": self._app_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def get_installation_token(self) -> str:
        """Get a valid installation token, refreshing if needed.

        Tokens are cached and refreshed 5 minutes before expiry.
        """
        if self._cached_token and time.time() < self._token_expires_at - 300:
            return self._cached_token

        token_jwt = self._generate_jwt()
        url = f"{self.GITHUB_API_BASE}/app/installations/{self._installation_id}/access_tokens"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token_jwt}",
                    "Accept": "application/vnd.github+json",
                },
            )
            response.raise_for_status()

        data = response.json()
        self._cached_token = data["token"]
        # Parse ISO 8601 expiry to epoch; default to 55 min from now
        expires_at_str = data.get("expires_at", "")
        if expires_at_str:
            from datetime import datetime, timezone
            self._token_expires_at = datetime.fromisoformat(
                expires_at_str.replace("Z", "+00:00")
            ).timestamp()
        else:
            self._token_expires_at = time.time() + 3300

        logger.info("Refreshed GitHub App installation token")
        return self._cached_token


# Module-level singleton
_github_app_auth: GitHubAppAuth | None = None


def get_github_app_auth() -> GitHubAppAuth:
    """Get the global GitHubAppAuth instance."""
    global _github_app_auth
    if _github_app_auth is None:
        _github_app_auth = GitHubAppAuth()
    return _github_app_auth
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_github_app.py -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
git add src/agent_grid/github_app.py tests/test_github_app.py
git commit -m "feat: add GitHubAppAuth module for installation token management"
```

---

### Task 4: Update GitHubClient to use GitHub App tokens

**Files:**
- Modify: `src/agent_grid/issue_tracker/github_client.py:36-46`
- Modify: `tests/test_github_client.py:244-256`

**Step 1: Modify GitHubClient.__init__**

Replace lines 36-46 in `github_client.py`:

```python
    def __init__(self, token: str | None = None):
        self._token = token or settings.github_token
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
```

With:

```python
    def __init__(self, token: str | None = None):
        self._token = token  # static token override (for tests)
        self._app_auth = None if token else None  # lazy-loaded
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def _get_auth_headers(self) -> dict[str, str]:
        """Get authorization headers with a fresh token."""
        if self._token:
            token = self._token
        else:
            from ..github_app import get_github_app_auth
            if self._app_auth is None:
                self._app_auth = get_github_app_auth()
            token = await self._app_auth.get_installation_token()
        return {"Authorization": f"Bearer {token}"}
```

Then update every method that uses `self._client` to inject auth headers. Since `self._client` was constructed with the token baked into default headers, we need to change the approach. The simplest way: add a helper method that makes requests with fresh auth headers.

Actually, a cleaner approach — add a request wrapper:

```python
    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make an authenticated request, refreshing the token if needed."""
        headers = kwargs.pop("headers", {})
        headers.update(await self._get_auth_headers())
        return await getattr(self._client, method)(url, headers=headers, **kwargs)
```

This would require changing every `self._client.get(...)` to `self._request("get", ...)` throughout the file. That's a large refactor.

**Simpler approach**: Keep the httpx client but refresh its default Authorization header before each request. Add a method that updates the client headers:

Replace the `__init__` with:

```python
    def __init__(self, token: str | None = None):
        self._static_token = token  # static token override (for tests)
        self._app_auth = None  # lazy-loaded
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def _ensure_auth(self) -> None:
        """Ensure the client has a valid Authorization header."""
        if self._static_token:
            self._client.headers["Authorization"] = f"Bearer {self._static_token}"
            return
        from ..github_app import get_github_app_auth
        if self._app_auth is None:
            self._app_auth = get_github_app_auth()
        token = await self._app_auth.get_installation_token()
        self._client.headers["Authorization"] = f"Bearer {token}"
```

Then add `await self._ensure_auth()` at the top of every public method: `get_issue`, `list_issues`, `list_subissues`, `create_issue`, `create_subissue`, `update_issue`, `add_comment`, `update_issue_status`, `get_actions_job_logs`, `assign_issue`, `request_pr_reviewers`, `get_pr_by_branch`, `add_pr_comment`, `get_check_runs_for_ref`, `list_open_prs`, `get_pr_reviews`, `get_pr_comments`, `get_pr_data`, `get_issue_comments_since`, `add_label`, `remove_label`, `create_label`.

Add this as the first line in each of those methods:
```python
        await self._ensure_auth()
```

**Step 2: Update integration test**

In `tests/test_github_client.py`, line 256 currently does:
```python
        return GitHubClient(token=os.environ["AGENT_GRID_GITHUB_TOKEN"])
```

Keep this as-is — the `token` parameter still works as a static override for tests. No change needed here.

**Step 3: Run existing tests**

Run: `pytest tests/test_github_client.py -v -k "not integration"`
Expected: PASS (unit tests should still work)

**Step 4: Commit**

```bash
git add src/agent_grid/issue_tracker/github_client.py
git commit -m "feat: GitHubClient uses GitHub App tokens with static token fallback"
```

---

### Task 5: Update ProjectManager to use GitHub App tokens

**Files:**
- Modify: `src/agent_grid/issue_tracker/project_manager.py:23-30`

**Step 1: Update ProjectManager.__init__**

Replace lines 23-30:

```python
    def __init__(self):
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
```

With:

```python
    def __init__(self):
        self._app_auth = None  # lazy-loaded
        self._client = httpx.AsyncClient(
            headers={
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def _ensure_auth(self) -> None:
        """Ensure the client has a valid Authorization header."""
        from ..github_app import get_github_app_auth
        if self._app_auth is None:
            self._app_auth = get_github_app_auth()
        token = await self._app_auth.get_installation_token()
        self._client.headers["Authorization"] = f"Bearer {token}"
```

Then add `await self._ensure_auth()` as the first line in `_graphql()` (line 49), since all GraphQL calls go through it:

```python
    async def _graphql(self, query: str, variables: dict | None = None) -> dict | None:
        await self._ensure_auth()
        try:
            # ... rest unchanged
```

**Step 2: Run tests**

Run: `pytest tests/ -v -k "not integration and not e2e"`
Expected: PASS

**Step 3: Commit**

```bash
git add src/agent_grid/issue_tracker/project_manager.py
git commit -m "feat: ProjectManager uses GitHub App tokens"
```

---

### Task 6: Update FlyMachinesClient to use GitHub App tokens

**Files:**
- Modify: `src/agent_grid/fly/machines.py:55-63`

**Step 1: Update spawn_worker to get a fresh installation token**

In `spawn_worker()`, replace line 63:

```python
                    "GITHUB_TOKEN": settings.github_token,
```

With:

```python
                    "GITHUB_TOKEN": await self._get_github_token(),
```

And add the helper method to the class (after `__init__`):

```python
    async def _get_github_token(self) -> str:
        """Get a fresh GitHub installation token for the worker."""
        from ..github_app import get_github_app_auth
        return await get_github_app_auth().get_installation_token()
```

**Step 2: Run tests**

Run: `pytest tests/ -v -k "not integration and not e2e"`
Expected: PASS

**Step 3: Commit**

```bash
git add src/agent_grid/fly/machines.py
git commit -m "feat: Fly worker gets fresh GitHub App token at spawn time"
```

---

### Task 7: Update Terraform — secrets module

**Files:**
- Modify: `infrastructure/terraform/modules/secrets/main.tf:22-37`
- Modify: `infrastructure/terraform/modules/secrets/variables.tf:33-38`
- Modify: `infrastructure/terraform/modules/secrets/outputs.tf:6-9`

**Step 1: Update variables.tf**

Replace `github_token` variable (lines 33-38) with three new variables:

```hcl
variable "github_app_id" {
  description = "GitHub App ID"
  type        = string
  default     = ""
}

variable "github_app_private_key" {
  description = "GitHub App private key (PEM-encoded)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "github_app_installation_id" {
  description = "GitHub App installation ID for the org"
  type        = string
  default     = ""
}
```

Keep `github_webhook_secret` unchanged.

**Step 2: Update main.tf**

Replace the `github` secret resource (lines 22-37):

```hcl
resource "aws_secretsmanager_secret" "github" {
  count       = var.github_app_id != "" ? 1 : 0
  name        = "${var.project_name}/github"
  description = "GitHub App credentials for Agent Grid"

  tags = var.tags
}

resource "aws_secretsmanager_secret_version" "github" {
  count     = var.github_app_id != "" ? 1 : 0
  secret_id = aws_secretsmanager_secret.github[0].id
  secret_string = jsonencode({
    app_id          = var.github_app_id
    private_key     = var.github_app_private_key
    installation_id = var.github_app_installation_id
    webhook_secret  = var.github_webhook_secret
  })
}
```

**Step 3: Update outputs.tf**

Replace line 8 condition:

```hcl
output "github_secret_arn" {
  description = "ARN of the GitHub secret"
  value       = var.github_app_id != "" ? aws_secretsmanager_secret.github[0].arn : ""
}
```

And update `all_secret_arns` (line 20):

```hcl
    var.github_app_id != "" ? aws_secretsmanager_secret.github[0].arn : "",
```

**Step 4: Commit**

```bash
git add infrastructure/terraform/modules/secrets/
git commit -m "feat: terraform secrets module stores GitHub App credentials"
```

---

### Task 8: Update Terraform — dev environment

**Files:**
- Modify: `infrastructure/terraform/environments/dev/variables.tf:19-24`
- Modify: `infrastructure/terraform/environments/dev/main.tf:34-37,127-128,164-171,217-218`
- Modify: `infrastructure/terraform/environments/dev/terraform.tfvars.example:10-13`

**Step 1: Update variables.tf**

Replace the `github_token` variable (lines 19-24) with:

```hcl
variable "github_app_id" {
  description = "GitHub App ID"
  type        = string
  default     = ""
}

variable "github_app_private_key" {
  description = "GitHub App private key (PEM-encoded)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "github_app_installation_id" {
  description = "GitHub App installation ID for the dragonflyic org"
  type        = string
  default     = ""
}
```

Keep `github_webhook_secret` unchanged.

**Step 2: Update main.tf — GitHub provider**

Replace lines 34-37 (the `provider "github"` block). The GitHub Terraform provider can use a GitHub App for auth:

```hcl
provider "github" {
  owner = var.github_org
  app_auth {
    id              = var.github_app_id
    installation_id = var.github_app_installation_id
    pem_file        = var.github_app_private_key
  }
}
```

**Step 3: Update main.tf — secrets module**

Replace lines 127-128:

```hcl
  github_app_id              = var.github_app_id
  github_app_private_key     = var.github_app_private_key
  github_app_installation_id = var.github_app_installation_id
  github_webhook_secret      = var.github_webhook_secret
```

**Step 4: Update main.tf — App Runner environment_secrets**

Replace lines 168-171:

```hcl
    var.github_app_id != "" ? {
      AGENT_GRID_GITHUB_APP_ID              = "${module.secrets.github_secret_arn}:app_id::"
      AGENT_GRID_GITHUB_APP_PRIVATE_KEY     = "${module.secrets.github_secret_arn}:private_key::"
      AGENT_GRID_GITHUB_APP_INSTALLATION_ID = "${module.secrets.github_secret_arn}:installation_id::"
      AGENT_GRID_GITHUB_WEBHOOK_SECRET      = "${module.secrets.github_secret_arn}:webhook_secret::"
    } : {},
```

**Step 5: Update main.tf — ECS Scheduled Task environment_secrets**

Replace lines 217-219:

```hcl
    var.github_app_id != "" ? {
      AGENT_GRID_GITHUB_APP_ID              = "${module.secrets.github_secret_arn}:app_id::"
      AGENT_GRID_GITHUB_APP_PRIVATE_KEY     = "${module.secrets.github_secret_arn}:private_key::"
      AGENT_GRID_GITHUB_APP_INSTALLATION_ID = "${module.secrets.github_secret_arn}:installation_id::"
    } : {},
```

**Step 6: Update terraform.tfvars.example**

Replace lines 10-13:

```hcl
# Optional: GitHub App integration
# github_app_id              = "123456"
# github_app_private_key     = "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
# github_app_installation_id = "78901234"
# github_webhook_secret      = "your-webhook-secret"
# github_org                 = "dragonflyic"
```

**Step 7: Commit**

```bash
git add infrastructure/terraform/environments/dev/
git commit -m "feat: terraform dev env uses GitHub App credentials"
```

---

### Task 9: Update CI/CD and Spacelift config

**Files:**
- Modify: `.github/workflows/infra.yml:18`
- Modify: `.spacelift/config.yml:23`

**Step 1: Update GitHub Actions workflow**

In `.github/workflows/infra.yml`, replace line 18:

```yaml
  TF_VAR_github_token: ${{ secrets.TF_VAR_GITHUB_TOKEN }}
```

With:

```yaml
  TF_VAR_github_app_id: ${{ secrets.TF_VAR_GITHUB_APP_ID }}
  TF_VAR_github_app_private_key: ${{ secrets.TF_VAR_GITHUB_APP_PRIVATE_KEY }}
  TF_VAR_github_app_installation_id: ${{ secrets.TF_VAR_GITHUB_APP_INSTALLATION_ID }}
```

**Step 2: Update Spacelift config**

In `.spacelift/config.yml`, replace line 23:

```yaml
    # TF_VAR_github_app_id: (sensitive - set in Spacelift)
    # TF_VAR_github_app_private_key: (sensitive - set in Spacelift)
    # TF_VAR_github_app_installation_id: (sensitive - set in Spacelift)
```

**Step 3: Commit**

```bash
git add .github/workflows/infra.yml .spacelift/config.yml
git commit -m "feat: update CI/CD configs for GitHub App credentials"
```

---

### Task 10: Update .env.example and README

**Files:**
- Modify: `.env.example:1-4`
- Modify: `README.md:62,123`

**Step 1: Update .env.example**

Replace lines 1-4:

```
# === Required ===

# GitHub App credentials (create at https://github.com/settings/apps)
AGENT_GRID_GITHUB_APP_ID=123456
AGENT_GRID_GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----
...
-----END RSA PRIVATE KEY-----"
AGENT_GRID_GITHUB_APP_INSTALLATION_ID=78901234
```

**Step 2: Update README.md**

Replace line 62:
```
| `GITHUB_TOKEN` | - | GitHub API token (required for github tracker) |
```
With:
```
| `GITHUB_APP_ID` | - | GitHub App ID (required for github tracker) |
| `GITHUB_APP_PRIVATE_KEY` | - | GitHub App private key PEM content |
| `GITHUB_APP_INSTALLATION_ID` | - | GitHub App installation ID for org |
```

Replace line 123:
```
export AGENT_GRID_GITHUB_TOKEN=your_token
```
With:
```
export AGENT_GRID_GITHUB_APP_ID=your_app_id
export AGENT_GRID_GITHUB_APP_PRIVATE_KEY="$(cat path/to/private-key.pem)"
export AGENT_GRID_GITHUB_APP_INSTALLATION_ID=your_installation_id
```

**Step 3: Commit**

```bash
git add .env.example README.md
git commit -m "docs: update env example and README for GitHub App auth"
```

---

### Task 11: Update e2e tests and cleanup script

**Files:**
- Modify: `src/agent_grid/e2e_test.py:14,66-67`
- Modify: `src/agent_grid/e2e_complex_test.py:13,43-44`
- Modify: `scripts/cleanup_stuck_issues.py:7`

**Step 1: Update e2e_test.py**

Replace line 14 docstring reference:
```python
    AGENT_GRID_GITHUB_APP_ID=... \\
    AGENT_GRID_GITHUB_APP_PRIVATE_KEY="$(cat key.pem)" \\
    AGENT_GRID_GITHUB_APP_INSTALLATION_ID=... \\
```

Replace lines 66-67:
```python
    if not settings.github_app_id:
        errors.append("AGENT_GRID_GITHUB_APP_ID=...")
```

**Step 2: Update e2e_complex_test.py**

Same pattern — replace `github_token` references with `github_app_id` check.

**Step 3: Update cleanup_stuck_issues.py**

Replace the comment on line 7 referencing `GITHUB_TOKEN` to reference the new env vars.

**Step 4: Commit**

```bash
git add src/agent_grid/e2e_test.py src/agent_grid/e2e_complex_test.py scripts/cleanup_stuck_issues.py
git commit -m "chore: update e2e tests and scripts for GitHub App auth"
```

---

### Task 12: Run full test suite and verify

**Step 1: Run all unit tests**

Run: `pytest tests/ -v -k "not integration and not e2e"`
Expected: All tests PASS

**Step 2: Run linter**

Run: `ruff check src/ tests/`
Expected: No errors

**Step 3: Verify config loading with new env vars**

Run:
```bash
AGENT_GRID_GITHUB_APP_ID=test AGENT_GRID_GITHUB_APP_PRIVATE_KEY=test AGENT_GRID_GITHUB_APP_INSTALLATION_ID=test \
  python -c "from agent_grid.config import settings; print(settings.github_app_id, settings.github_app_installation_id)"
```
Expected: `test test`

**Step 4: Final commit (if any fixes needed)**

```bash
git add -A
git commit -m "fix: address any issues from full test run"
```

---

## Post-Implementation: GitHub App Setup (Manual)

After code is deployed, you need to:

1. **Create the GitHub App** at https://github.com/organizations/dragonflyic/settings/apps/new
   - Name: `fresa-agent-grid` (or similar)
   - Homepage URL: your coordinator URL
   - Webhook URL: `https://<coordinator>/webhooks/github`
   - Webhook secret: same as `github_webhook_secret`
   - Permissions:
     - Repository: Contents (Read & Write), Issues (Read & Write), Pull Requests (Read & Write), Checks (Read), Actions (Read)
   - Subscribe to events: Issues, Issue comment
   - Where can this app be installed: Only on this account

2. **Generate a private key** (download the `.pem` file)

3. **Install the App** on all repos in `dragonflyic` org

4. **Get the installation ID**: `GET /app/installations` using the JWT, or find it in the App's "Install" page URL

5. **Store credentials**: Update AWS Secrets Manager / terraform.tfvars with `app_id`, `private_key`, `installation_id`

6. **Remove the old GitHub PAT** from Secrets Manager / CI secrets
