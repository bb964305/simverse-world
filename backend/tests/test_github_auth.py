"""GitHubOAuth.exchange_code goes through the shared HTTP client (P1-2)."""
import pytest
from unittest.mock import AsyncMock, patch
import httpx

from app.services.github_auth import GitHubOAuth, GitHubUser


@pytest.mark.anyio
async def test_exchange_code_returns_github_user():
    oauth = GitHubOAuth(
        client_id="test-id",
        client_secret="test-secret",
        redirect_uri="http://localhost:8000/auth/github/callback",
    )
    token_response = httpx.Response(
        200,
        json={"access_token": "test-token", "token_type": "bearer"},
        request=httpx.Request("POST", "https://github.com/login/oauth/access_token"),
    )
    user_response = httpx.Response(
        200,
        json={"id": 42, "login": "octocat", "name": "Octo Cat", "email": "octo@example.com"},
        request=httpx.Request("GET", "https://api.github.com/user"),
    )

    with patch("app.services.github_auth.get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=token_response)
        mock_client.get = AsyncMock(return_value=user_response)
        mock_get_client.return_value = mock_client

        user = await oauth.exchange_code("test-code")

    assert isinstance(user, GitHubUser)
    assert user.id == 42
    assert user.login == "octocat"
    assert user.name == "Octo Cat"
    assert user.email == "octo@example.com"
