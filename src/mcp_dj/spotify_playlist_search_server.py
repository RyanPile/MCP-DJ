"""
Spotify playlist MCP server.

MCP Tools:
    get_my_playlists    - list the playlists you own or follow
    get_playlist        - fetch playlist's details (only if you own it)
    get_playlist_items  - fetch a page of playlist's tracks (only if you own it)

Auth: handled by spotify_auth.py, which reads the login tokens saved in .spotify_token.json
made by ./scripts/spotify_auth_attempt.py. The tokens stay inside that module. No tool
takes a token or file path as input, and no tool returns one, so the model never sees them.

.env:
    SPOTIFY_CLIENT_ID=your_client_id

Note: with the stdio transport, stdout is the MCP protocol channel. Never print()
to it; log to stderr only.
"""

import logging

from fastmcp import FastMCP

# Shared login and API helpers (see spotify_auth.py)
from spotify_auth import (
    check_playlist_id, 
    ensure_owner, 
    get_user_id, 
    spotify_get
)

# Logs go to stderr, which keeps stdout free for the MCP protocol.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP(name="Spotify Playlists")

# ---------------------------------------------------
# -------------- HELPER FUNCTIONS--------------------
# ---------------------------------------------------

def format_item(entry: dict) -> dict | None:
    """Trim one playlist entry down to the fields we want to return."""
    item = entry.get("item")
    if not item:  # unavailable tracks come back as null
        return None
    return {
        "name": item["name"],
        "artists": [a["name"] for a in item.get("artists") or []],
        "album": (item.get("album") or {}).get("name"),
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
        # The playlist's current version; can be passed to the remove/reorder tools
        "snapshot_id": p.get("snapshot_id"),
    }


@mcp.tool
async def get_playlist_items(playlist_id: str, limit: int = 20, offset: int = 0) -> dict:
    """Fetch one page of tracks from a playlist. Only works for playlists the user owns.

    limit is 1-50. To get the next page, pass the returned next_offset as offset.
    Each track's position is its zero-based place in the whole playlist.
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

    tracks = []
    for i, entry in enumerate(data["items"]):
        track = format_item(entry)
        if track:  # skip unavailable tracks, but their slots still count toward positions
            track["position"] = offset + i
            tracks.append(track)

    return {
        "total": data["total"],
        # None means there are no more pages
        "next_offset": offset + len(data["items"]) if data.get("next") else None,
        "tracks": tracks,
    }


# Start the server over stdio when the file is run directly
if __name__ == "__main__":
    mcp.run(transport="stdio")