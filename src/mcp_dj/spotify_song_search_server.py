"""
Spotify song search MCP server (Client Credentials flow).

No browser login: the server authenticates as your Spotify *app*, not as a user.
Fine for catalog search; it cannot access user data (playlists, library, playback).

.env:
    SPOTIFY_CLIENT_ID=your_client_id
    SPOTIFY_CLIENT_SECRET=your_client_secret
    SPOTIFY_MARKET=US

Note: with the stdio transport, stdout is the MCP protocol channel. Never print()
to it; log to stderr only.

MCP Name in Config: spotify-song-search
"""

import asyncio
import logging
import os
import time

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

# Read the variables from the .env file into the environment
load_dotenv()

# Logs go to stderr by default, which keeps stdout free for the MCP protocol
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Credentials and settings from the environment (a missing ID or secret raises KeyError)
CLIENT_ID = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
MARKET = os.environ.get("SPOTIFY_MARKET", "US")  # country whose catalog we search, default to US

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"

mcp = FastMCP(name="Spotify Song Search")

# Cache the access token so we don't request a new one on every search
_token: str | None = None
_token_expires_at: float = 0.0  # Unix time when the cached token stops being valid
_token_lock = asyncio.Lock()  # stops two simultaneous searches from both fetching a token


async def get_access_token() -> str:
    """Return a cached app token, requesting a new one when it expires (~1 hour)."""
    global _token, _token_expires_at
    async with _token_lock:
        # Reuse the cached token if it hasn't expired yet
        if _token and time.time() < _token_expires_at:
            return _token

        # Otherwise ask Spotify for a new token using our client ID and secret
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(CLIENT_ID, CLIENT_SECRET),  # sends HTTP Basic auth
            )
        resp.raise_for_status()  # raise an error on any 4xx/5xx response
        payload = resp.json()

        _token = payload["access_token"]
        # Expire 60 seconds early so we never send a token that's about to lapse
        _token_expires_at = time.time() + payload.get("expires_in", 3600) - 60
        logger.info("Obtained new Spotify app token")
        return _token


@mcp.tool
async def search_songs(query: str, limit: int = 5) -> list[dict]:
    """Search for songs on Spotify.

    Supports Spotify field filters inside the query, e.g.
    'track:Doxy artist:Miles Davis' or 'genre:jazz year:1955-1960'.
    limit is 1-10 (Spotify's maximum for search).
    """
    # Keep limit inside Spotify's allowed range of 1-10
    limit = max(1, min(limit, 10))
    logger.info("Searching for songs: %s", query)
    token = await get_access_token()

    # Call the search endpoint, asking for tracks only
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            SEARCH_URL,
            params={"q": query, "type": "track", "limit": limit, "market": MARKET},
            headers={"Authorization": f"Bearer {token}"},
        )

    # 429 means we're sending too many requests; Spotify says how long to wait
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "a few")
        raise RuntimeError(f"Spotify rate limit hit; retry in {retry} seconds.")
    resp.raise_for_status()

    # Keep only the fields we need so the response stays small
    return [
        {
            "name": t["name"],
            "artists": [a["name"] for a in t["artists"]],  # a track can have several artists
            "album": t["album"]["name"],
            "release_date": t["album"].get("release_date"),
            "duration_ms": t["duration_ms"],
            "uri": t["uri"],  # Spotify's ID for the track, usable in other API calls
            "url": t["external_urls"].get("spotify"),  # link to open it in Spotify
        }
        for t in resp.json()["tracks"]["items"]
    ]


# Start the server over stdio when the file is run directly
if __name__ == "__main__":
    mcp.run(transport="stdio")