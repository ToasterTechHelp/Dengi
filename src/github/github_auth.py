# github_auth.py
from __future__ import annotations
import time, jwt
from dataclasses import dataclass
from typing import Optional
from cryptography.hazmat.primitives import serialization

@dataclass
class GitHubAuth:
    def auth_header(self) -> dict:  # overridden
        return {}

@dataclass
class TokenAuth(GitHubAuth):
    token: str
    def auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

class AppAuth(GitHubAuth):
    """
    GitHub App → installation token (rotates ~every hour).
    Use minimal perms: contents:read/write, pull_requests:write, checks:write.
    """
    def __init__(self, app_id: str, installation_id: str, private_key_pem: str, base_url="https://api.github.com", session=None):
        import requests
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        self.base_url = base_url.rstrip("/")
        self._sess = session or requests.Session()
        self._cached_token: Optional[str] = None
        self._exp: float = 0.0  # epoch seconds

    def _issue_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": self.app_id}
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    def _refresh_installation_token(self) -> None:
        # Step 1: app JWT
        app_jwt = self._issue_jwt()
        headers = {"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json"}
        # Step 2: exchange for installation token
        url = f"{self.base_url}/app/installations/{self.installation_id}/access_tokens"
        resp = self._sess.post(url, headers=headers, timeout=30)
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"Failed to get installation token: {resp.status_code} {resp.text[:300]}")
        data = resp.json()
        self._cached_token = data["token"]
        # expire a bit early to be safe
        self._exp = time.time() + 50 * 60

    def auth_header(self) -> dict:
        if not self._cached_token or time.time() >= self._exp:
            self._refresh_installation_token()
        return {"Authorization": f"Bearer {self._cached_token}"}
