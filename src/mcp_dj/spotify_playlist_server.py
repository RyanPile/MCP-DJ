"""
Spotify playlist MCP server.

MCP Tools:
    get_my_playlists    - list the playlists you own or follow
    get_playlist        - fetch playlist's details (only if you own it)
    get_playlist_items  - fetch a page of playlist's tracks (only if you own it)

Auth: reads the login tokens saved in .spotify_token.json made by ./scripts/spotify_auth_attempt.py
The tokens stay inside this server. No tool takes a token or file path as input,
and no tool returns one, so the model never sees them.

.env:
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
from fastmcp import FastMCP

load_dotenv()

# Logs go to stderr, which keeps stdout free for the MCP protocol.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
PROJECT_ROOT = Path(__file__).resolve().parents[2] # src/mcp_dj/spotify_playlist_server.py is two levels down from the project root
TOKEN_FILE = PROJECT_ROOT / ".spotify_token.json" 

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"

# Checking this stops the model from sneaking extra characters (like "/" or "?") into the URL we build.
SPOTIFY_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{22}$")

mcp = FastMCP(name="Spotify Playlists")

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
            raise RuntimeError("Not logged in to Spotify. Run ./scripts/spotify_auth_attempt.py first.")

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
            raise RuntimeError("Spotify login expired. Run ./scripts/spotify_auth_attempt.py again.")

        payload = resp.json()
        tokens = {
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token", tokens["refresh_token"]),
            "expires_at": time.time() + payload["expires_in"],
        }
        _save_tokens(tokens)
        logger.info("Refreshed Spotify access token")
        return tokens["access_token"]

# ---------------------------------------------------
# -------------- HELPER FUNCTIONS--------------------
# ---------------------------------------------------

async def spotify_get(path: str, params: dict | None = None) -> dict:
    """GET a Spotify API path. Shortens error messages."""
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{API_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )

    # From Spotify developer docs:
    # 401 for bad or expired tokens
    # 403 for bad OAuth requests
    # 404 for missing resources
    # 429 for app rate limiting
    if resp.status_code == 401:
        raise RuntimeError("Spotify rejected the login. Run ./scripts/spotify_auth_attempt.py again.")
    if resp.status_code == 403:
        raise RuntimeError("Spotify denied access. Re-run ./scripts/spotify_auth_attempt.py to grant permissions.")
    if resp.status_code == 404:
        raise RuntimeError("Not found on Spotify.")
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "a few")
        raise RuntimeError(f"Spotify rate limit hit; retry in {retry} seconds.")
    if resp.status_code != 200:
        raise RuntimeError(f"Spotify API error (status {resp.status_code}).")
    return resp.json()


async def get_user_id() -> str:
    """Get the logged-in user's Spotify ID. Different than Client ID."""
    global _user_id
    if _user_id is None:
        _user_id = (await spotify_get("/me"))["id"]
    return _user_id


def check_playlist_id(playlist_id: str) -> None:
    """Reject anything that isn't shaped like a Spotify ID."""
    if not SPOTIFY_ID_PATTERN.match(playlist_id):
        raise ValueError("playlist_id must be a 22-character Spotify ID.")


async def ensure_owner(owner_id: str) -> None:
    """Refuse playlists owned by other people."""
    if owner_id != await get_user_id():
        raise PermissionError("This playlist isn't owned by the user, so it can't be fetched.")


def format_item(entry: dict) -> dict | None:
    """Trim one playlist entry down to the fields we want to return."""
    item = entry.get("item")
    if not item:  # unavailable tracks come back as null
        return None
    return {
        "name": item["name"],
        "artists": [a["name"] for a in item.get("artists")],
        "album": (item.get("album")).get("name"),
        "duration_ms": item.get("duration_ms"),
        "is_local": entry.get("is_local"),  # True for files uploaded from your computer
        "uri": item["uri"],
        "added_at": entry.get("added_at"),
    }

# ---------------------------------------------------
# ----------------- MCP TOOLS -----------------------
# ---------------------------------------------------

@mcp.tool
async def get_my_playlists(limit: int = 20, offset: int = 0) -> dict:
    """List the playlists the user owns or follows.

    limit is 1-50. To get the next page, pass the returned next_offset as offset.
    owned_by_me tells you whether get_playlist can fetch that playlist.
    """
    limit = max(1, min(limit, 50))  # Spotify's allowed range is 1-50
    offset = max(0, offset) # Offset can't be negative
    me = await get_user_id()
    data = await spotify_get("/me/playlists", {"limit": limit, "offset": offset})

    playlists = []
    for p in data["items"]:
        if not p:  # Spotify can return null entries; skip them
            continue
        # Count can be missing if the playlist is empty or unavailable, so default to None
        count = (p.get("items") or {}).get("total")
        playlists.append(
            {
                "id": p["id"],
                "name": p["name"],
                "description": p.get("description"),
                "owner": p["owner"].get("display_name") or p["owner"]["id"],
                "owned_by_me": p["owner"]["id"] == me,
                "public": p.get("public"),
                "collaborative": p.get("collaborative"),
                "track_count": count,
                "url": p["external_urls"].get("spotify"),
            }
        )

    return {
        "total": data["total"],
        # None means there are no more pages
        "next_offset": offset + len(data["items"]) if data.get("next") else None,
        "playlists": playlists,
    }


@mcp.tool
async def get_playlist(playlist_id: str) -> dict:
    """Fetch one playlist's details (name, description, track count). Only works
    for playlists the user owns.

    This does not return the tracks. Use get_playlist_items for those.
    playlist_id is the Spotify ID from get_my_playlists.
    """
    check_playlist_id(playlist_id)
    p = await spotify_get(f"/playlists/{playlist_id}")
    await ensure_owner(p["owner"]["id"])

    # Only the track count is kept; the tracks themselves come from get_playlist_items
    # Count can be missing if the playlist is empty or unavailable, so default to None
    count = (p.get("items") or {}).get("total")
    return {
        "id": p["id"],
        "name": p["name"],
        "description": p.get("description"),
        "public": p.get("public"),
        "collaborative": p.get("collaborative"),
        "url": p["external_urls"].get("spotify"),
        "track_count": count,
    }


@mcp.tool
async def get_playlist_items(playlist_id: str, limit: int = 20, offset: int = 0) -> dict:
    """Fetch one page of tracks from a playlist. Only works for playlists the user owns.

    limit is 1-50. To get the next page, pass the returned next_offset as offset.
    """
    check_playlist_id(playlist_id)
    limit = max(1, min(limit, 50))  # Spotify's allowed range is 1-50
    offset = max(0, offset) # Offset can't be negative

    # One more safety check making sure playlist is owned by the user.
    owner = await spotify_get(f"/playlists/{playlist_id}", {"fields": "owner.id"})
    await ensure_owner(owner["owner"]["id"])

    data = await spotify_get(
        f"/playlists/{playlist_id}/items",
        {
            "limit": limit,
            "offset": offset,
            "additional_types": "track", 
        },
    )

    # format_item returns None for unavailable tracks, so filter those out
    tracks = [t for t in map(format_item, data["items"]) if t]

    return {
        "total": data["total"],
        # None means there are no more pages
        "next_offset": offset + len(data["items"]) if data.get("next") else None,
        "tracks": tracks,
    }


# Start the server over stdio when the file is run directly
if __name__ == "__main__":
    mcp.run(transport="stdio")
