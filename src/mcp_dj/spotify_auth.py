"""
Shared Spotify login and API helpers for the MCP servers.

Handles everything the servers have in common:
    - reading and refreshing the user token saved in .spotify_token.json
    - making Spotify API requests (GET, POST, PUT, DELETE) with safe error messages
    - looking up the logged-in user's ID
    - checking Spotify IDs and playlist ownership

The tokens stay inside this module. Nothing here returns a token to a caller outside
the module except the HTTP helper below, and the servers never pass tokens to the model.

.env (in the project root):
    SPOTIFY_CLIENT_ID=your_client_id

Note: with the stdio transport, stdout is the MCP protocol channel. Never print()
to it; log to stderr only.
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# This file lives at src/mcp_dj/spotify_auth.py, two levels below the project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger(__name__)

CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
TOKEN_FILE = PROJECT_ROOT / ".spotify_token.json"
LOGIN_SCRIPT = "./scripts/spotify_auth_attempt.py"  # named in error messages

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"

# Spotify IDs are 22 letters/digits. Checking this stops the model from sneaking
# extra characters (like "/" or "?") into the URL we build.
SPOTIFY_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{22}$")

_token_lock = asyncio.Lock()  # stops two requests from refreshing the token at once
_user_id: str | None = None  # cached after the first lookup

# ---------------------------------------------------
# -------------- TOKEN HANDLING ---------------------
# ---------------------------------------------------

def _load_tokens() -> dict | None:
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_tokens(tokens: dict) -> None:
    # Write to a temp file, then swap it in, so a crash can't leave a half-written file
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens))
    try:
        tmp.chmod(0o600)  # Extra safety: only the user can read/write the token file.
    except OSError:
        pass
    tmp.replace(TOKEN_FILE)


async def get_access_token() -> str:
    """Return a valid user access token, refreshes token if it has expired."""
    async with _token_lock:
        tokens = _load_tokens()
        if not tokens:
            raise RuntimeError(f"Not logged in to Spotify. Run {LOGIN_SCRIPT} first.")

        # If token still valid, return it
        if time.time() < tokens["expires_at"]:
            return tokens["access_token"]

        # If token expired, get refreshed token
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                },
            )
        # Check for successful refresh (Code 200)
        if resp.status_code != 200:
            raise RuntimeError(f"Spotify login expired. Run {LOGIN_SCRIPT} again.")

        payload = resp.json()
        tokens = {
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token", tokens["refresh_token"]),
            # Expire 60 seconds early so we never send a token that's about to lapse
            "expires_at": time.time() + payload["expires_in"] - 60,
        }
        _save_tokens(tokens)
        logger.info("Refreshed Spotify access token")
        return tokens["access_token"]

# ---------------------------------------------------
# -------------- API HELPERS ------------------------
# ---------------------------------------------------

def _error_detail(resp: httpx.Response) -> str:
    """Pull Spotify's short error description out of a failed response, if there is one."""
    try:
        return f": {str(resp.json()['error']['message'])[:200]}"
    except (ValueError, KeyError, TypeError):
        return ""


async def spotify_request(
    method: str,
    path: str,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict:
    """Send a request to the Spotify API. Shortens error messages.

    method is "GET", "POST", "PUT" or "DELETE". Returns the JSON response, or an
    empty dict when Spotify sends no body (some writes do this).
    """
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.request(
            method,
            f"{API_URL}{path}",
            params=params,
            json=json_body,  # httpx leaves the body out when this is None
            headers={"Authorization": f"Bearer {token}"},
        )

    # From Spotify developer docs:
    # 401 for bad or expired tokens
    # 403 for bad OAuth requests (or missing scopes)
    # 404 for missing resources
    # 429 for app rate limiting
    if resp.status_code == 401:
        raise RuntimeError(f"Spotify rejected the login. Run {LOGIN_SCRIPT} again.")
    if resp.status_code == 403:
        raise RuntimeError(f"Spotify denied access. Re-run {LOGIN_SCRIPT} to grant permissions.")
    if resp.status_code == 404:
        raise RuntimeError("Not found on Spotify.")
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "a few")
        raise RuntimeError(f"Spotify rate limit hit; retry in {retry} seconds.")
    if not resp.is_success:  # any other non-2xx status, e.g. 400 for a bad request
        raise RuntimeError(f"Spotify API error (status {resp.status_code}){_error_detail(resp)}")

    return resp.json() if resp.content else {}


async def spotify_get(path: str, params: dict | None = None) -> dict:
    """Shortcut for GET requests."""
    return await spotify_request("GET", path, params=params)


async def get_user_id() -> str:
    """Get the logged-in user's Spotify ID. Different than Client ID."""
    global _user_id
    if _user_id is None:
        _user_id = (await spotify_get("/me"))["id"]
    return _user_id


def check_spotify_id(value: str, label: str = "id") -> None:
    """Reject anything that isn't shaped like a Spotify ID."""
    if not SPOTIFY_ID_PATTERN.match(value):
        raise ValueError(f"{label} must be a 22-character Spotify ID.")


def check_playlist_id(playlist_id: str) -> None:
    """Reject anything that isn't shaped like a playlist ID."""
    check_spotify_id(playlist_id, "playlist_id")


async def ensure_owner(owner_id: str) -> None:
    """Refuse playlists owned by other people."""
    if owner_id != await get_user_id():
        raise PermissionError("This playlist isn't owned by the user, so this action isn't allowed.")