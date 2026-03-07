"""GitHub App authentication module.

Generates JWTs and exchanges them for short-lived installation access tokens
via the GitHub API.  Tokens are cached and automatically refreshed when they
approach expiry.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt

from agent_grid.config import settings

# ---------------------------------------------------------------------------
# Minimum remaining lifetime before we consider a cached token stale.
# ---------------------------------------------------------------------------
_TOKEN_REFRESH_MARGIN = timedelta(minutes=5)


class GitHubAppAuth:
    """Manages GitHub App authentication and installation token lifecycle.

    Parameters
    ----------
    app_id:
        The GitHub App ID.  Falls back to ``settings.github_app_id``.
    private_key:
        PEM-encoded RSA private key for the App.  Falls back to
        ``settings.github_app_private_key``.
    installation_id:
        The installation ID for the target organisation/repo.  Falls back to
        ``settings.github_app_installation_id``.
    """

    def __init__(
        self,
        app_id: str | None = None,
        private_key: str | None = None,
        installation_id: str | None = None,
    ) -> None:
        self._app_id = app_id or settings.github_app_id
        self._private_key = private_key or settings.github_app_private_key
        self._installation_id = installation_id or settings.github_app_installation_id

        # Cached installation token state
        self._cached_token: str | None = None
        self._token_expires_at: datetime | None = None

    # ------------------------------------------------------------------
    # JWT generation
    # ------------------------------------------------------------------

    def _generate_jwt(self) -> str:
        """Create a short-lived RS256 JWT for authenticating as the GitHub App.

        The token is valid for 10 minutes (GitHub's maximum) with a 60-second
        clock-skew buffer on the ``iat`` claim.
        """
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 540,
            "iss": self._app_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    # ------------------------------------------------------------------
    # Installation token
    # ------------------------------------------------------------------

    async def get_installation_token(self) -> str:
        """Return a valid installation access token, refreshing if needed.

        The token is cached and reused as long as it has more than 5 minutes
        of remaining validity.
        """
        if self._cached_token and self._token_expires_at:
            remaining = self._token_expires_at - datetime.now(timezone.utc)
            if remaining > _TOKEN_REFRESH_MARGIN:
                return self._cached_token

        app_jwt = self._generate_jwt()
        url = f"https://api.github.com/app/installations/{self._installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers)
            response.raise_for_status()

        data = response.json()
        self._cached_token = data["token"]
        self._token_expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))

        return self._cached_token


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_github_app_auth: GitHubAppAuth | None = None


def get_github_app_auth() -> GitHubAppAuth:
    """Return (and lazily create) the module-level ``GitHubAppAuth`` singleton."""
    global _github_app_auth  # noqa: PLW0603
    if _github_app_auth is None:
        _github_app_auth = GitHubAppAuth()
    return _github_app_auth
