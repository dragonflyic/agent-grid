"""Tests for GitHubAppAuth module."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from agent_grid.github_app import GitHubAppAuth, get_github_app_auth


def _generate_test_rsa_key_pem() -> str:
    """Generate a fresh RSA private key and return its PEM string."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=NoEncryption(),
    )
    return pem_bytes.decode("utf-8")


# Generate once for the module -- fast enough and avoids repetition
TEST_PRIVATE_KEY_PEM = _generate_test_rsa_key_pem()


class TestGenerateJwt:
    """Tests for JWT generation."""

    def test_generate_jwt_has_correct_claims(self):
        """Verify the JWT contains correct iss, iat, and exp claims with RS256."""
        auth = GitHubAppAuth(
            app_id="12345",
            private_key=TEST_PRIVATE_KEY_PEM,
            installation_id="67890",
        )

        token = auth._generate_jwt()

        # Decode without verification to inspect claims
        claims = jwt.decode(token, options={"verify_signature": False})

        assert claims["iss"] == "12345"
        assert "iat" in claims
        assert "exp" in claims

        # iat should be ~60 seconds in the past, exp ~540 seconds in the future
        now = datetime.now(timezone.utc).timestamp()
        assert claims["iat"] <= now
        assert claims["iat"] >= now - 120  # generous tolerance
        assert claims["exp"] > now
        assert claims["exp"] <= now + 600  # generous tolerance

        # Verify the signature is valid RS256 by decoding with the public key
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        private_key = load_pem_private_key(TEST_PRIVATE_KEY_PEM.encode(), password=None)
        public_key = private_key.public_key()
        verified_claims = jwt.decode(token, public_key, algorithms=["RS256"])
        assert verified_claims["iss"] == "12345"

    def test_generate_jwt_header_algorithm(self):
        """Verify the JWT header specifies RS256."""
        auth = GitHubAppAuth(
            app_id="12345",
            private_key=TEST_PRIVATE_KEY_PEM,
            installation_id="67890",
        )

        token = auth._generate_jwt()
        header = jwt.get_unverified_header(token)
        assert header["alg"] == "RS256"


class TestGetInstallationToken:
    """Tests for installation token retrieval."""

    @pytest.mark.asyncio
    async def test_get_installation_token_fresh(self):
        """First call should hit the GitHub API and return the token."""
        auth = GitHubAppAuth(
            app_id="12345",
            private_key=TEST_PRIVATE_KEY_PEM,
            installation_id="67890",
        )

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "token": "ghs_test_token_abc123",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)

        with patch("agent_grid.github_app.httpx.AsyncClient", return_value=mock_client_instance):
            token = await auth.get_installation_token()

        assert token == "ghs_test_token_abc123"

        # Verify the API was called with correct URL
        mock_client_instance.post.assert_called_once()
        call_args = mock_client_instance.post.call_args
        assert "installations/67890/access_tokens" in call_args[0][0]

        # Verify Authorization header contains a Bearer JWT
        headers = call_args[1].get("headers", call_args[0][1] if len(call_args[0]) > 1 else {})
        assert "Bearer" in headers.get("Authorization", "")

    @pytest.mark.asyncio
    async def test_get_installation_token_cached(self):
        """If a cached token has more than 5 minutes until expiry, return it without API call."""
        auth = GitHubAppAuth(
            app_id="12345",
            private_key=TEST_PRIVATE_KEY_PEM,
            installation_id="67890",
        )

        # Pre-populate the cache with a token that expires far in the future
        auth._cached_token = "ghs_cached_token"
        auth._token_expires_at = datetime(2099, 1, 1, tzinfo=timezone.utc)

        with patch("agent_grid.github_app.httpx.AsyncClient") as mock_client_cls:
            token = await auth.get_installation_token()

        assert token == "ghs_cached_token"
        # AsyncClient should never have been instantiated
        mock_client_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_installation_token_expired_refreshes(self):
        """If the cached token is expired (or within 5 min of expiry), refresh via API."""
        auth = GitHubAppAuth(
            app_id="12345",
            private_key=TEST_PRIVATE_KEY_PEM,
            installation_id="67890",
        )

        # Pre-populate cache with an expired token
        auth._cached_token = "ghs_old_token"
        auth._token_expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "token": "ghs_refreshed_token",
            "expires_at": "2099-06-01T00:00:00Z",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)

        with patch("agent_grid.github_app.httpx.AsyncClient", return_value=mock_client_instance):
            token = await auth.get_installation_token()

        assert token == "ghs_refreshed_token"
        mock_client_instance.post.assert_called_once()


class TestSingleton:
    """Tests for the module-level singleton."""

    def test_get_github_app_auth_returns_same_instance(self):
        """get_github_app_auth() should return the same object on repeated calls."""
        import agent_grid.github_app as mod

        # Reset the singleton
        mod._github_app_auth = None

        with patch.object(mod, "settings") as mock_settings:
            mock_settings.github_app_id = "111"
            mock_settings.github_app_private_key = TEST_PRIVATE_KEY_PEM
            mock_settings.github_app_installation_id = "222"

            first = get_github_app_auth()
            second = get_github_app_auth()

        assert first is second

        # Clean up
        mod._github_app_auth = None
