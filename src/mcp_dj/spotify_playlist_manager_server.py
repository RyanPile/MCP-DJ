"""
Spotify playlist manager MCP server.

MCP Tools:
    create_playlist          - make a new, empty playlist
    add_items_to_playlist    - add tracks to a playlist (only if you own it)
    remove_items_from_playlist - remove tracks from a playlist (only if you own it)
    update_playlist_details  - change name, description, or public/private (only if you own it)
    reorder_playlist_items   - move tracks within a playlist (only if you own it)
    sort_playlist            - sort a whole playlist, with a local backup (only if you own it)

Auth: handled by spotify_auth.py, which reads the login tokens saved in .spotify_token.json
made by ./scripts/spotify_auth_attempt.py. The tokens stay inside that module. No tool
takes a token or file path as input, and no tool returns one, so the model never sees them.

Scopes needed (set in spotify_auth_attempt.py, then log in again):
    playlist-read-private playlist-modify-private playlist-modify-public

.env:
    SPOTIFY_CLIENT_ID=your_client_id

Note: with the stdio transport, stdout is the MCP protocol channel. Never print()
to it; log to stderr only.
"""

import json
import logging
import re
import time

from fastmcp import FastMCP

# Shared login and API helpers (see spotify_auth.py)
from spotify_auth import (
    PROJECT_ROOT,
    check_playlist_id,
    ensure_owner,
    spotify_get,
    spotify_request,
)
# Logs go to stderr, MCP documentation says to handle them this way
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP(name="Spotify Playlist Manager")

# Safety limits. These are our own guards, not Spotify's (Spotify allows up to 100 items per request).
MAX_ITEMS_PER_CALL = 50
MAX_NAME_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 300

# Only accept full track URIs like spotify:track:4iV5W9uYEdYUVa79Axb7Rh
TRACK_URI_PATTERN = re.compile(r"^spotify:track:[A-Za-z0-9]{22}$")

# Settings for sort_playlist
SORT_KEYS = ("title", "artist", "album", "duration", "added_at")
MAX_SORT_ITEMS = 500  # Our own cap, can be adjusted but may inflate api calls
REPLACE_BATCH_SIZE = 100  # Spotify's limit per replace/add request
BACKUP_DIR = PROJECT_ROOT / "playlist_backups"  # Where the backups for the playlists changes are stored. Included in .gitignore

# ---------------------------------------------------
# -------------- HELPER FUNCTIONS--------------------
# ---------------------------------------------------

async def require_owned_playlist(playlist_id: str) -> None:
    """Check the ID's shape, then make sure the playlist belongs to the user."""
    check_playlist_id(playlist_id)
    # Ask Spotify for just the owner's ID to keep the response small
    owner = await spotify_get(f"/playlists/{playlist_id}", {"fields": "owner.id"})
    await ensure_owner(owner["owner"]["id"])


def check_track_uris(uris: list[str]) -> None:
    """Make sure the list is non-empty, not too long, and every entry is a track URI."""
    if not uris:
        raise ValueError("uris must contain at least one track URI.")
    if len(uris) > MAX_ITEMS_PER_CALL:
        raise ValueError(f"At most {MAX_ITEMS_PER_CALL} tracks can be sent per call.")
    for uri in uris:
        if not TRACK_URI_PATTERN.match(uri):
            raise ValueError(f"Invalid track URI: {uri!r}. Expected spotify:track:<22 characters>.")


def check_text(value: str, label: str, max_length: int) -> str:
    """Trim whitespace and enforce a maximum length. Returns the cleaned text."""
    value = value.strip()
    if len(value) > max_length:
        raise ValueError(f"{label} must be at most {max_length} characters.")
    return value


async def fetch_all_playlist_entries(playlist_id: str, max_items: int = 500) -> list[dict]:
    """Read every entry of a playlist, in playlist order (50 per request).

    Entries are returned as Spotify sends them, so an entry's index in the list is its
    zero-based position in the playlist. Unavailable tracks stay in the list (with an
    empty "item") so the positions don't shift. Raises ValueError if the playlist has
    more than max_items entries.
    """
    entries: list[dict] = []
    offset = 0
    while True:
        page = await spotify_get(
            f"/playlists/{playlist_id}/items",
            {
                "limit": 50,  # Spotify's maximum page size
                "offset": offset,
                "additional_types": "track,episode",  # so episodes show up as episodes
            },
        )
        if page["total"] > max_items:
            raise ValueError(f"Playlist has {page['total']} items; the limit here is {max_items}.")
        entries.extend(page["items"])
        offset += len(page["items"])
        # Stop on the last page (or if Spotify ever sends an empty page, to avoid looping forever)
        if not page.get("next") or not page["items"]:
            return entries


def find_unsortable(entries: list[dict]) -> list[int]:
    """Return the positions of entries we can't safely re-add (local files, episodes,
    unavailable tracks). Rewriting the playlist would silently drop these."""
    bad = []
    for position, entry in enumerate(entries):
        item = entry.get("item")
        if (
            entry.get("is_local")
            or not item
            or item.get("type") != "track"
            or not TRACK_URI_PATTERN.match(item.get("uri") or "")
        ):
            bad.append(position)
    return bad


def get_sort_value(entry: dict, sort_by: str):
    """Pull out the value one entry is sorted on. Text is lowercased so 'a' and 'A' match."""
    item = entry["item"]
    if sort_by == "title":
        return item["name"].casefold()
    if sort_by == "artist":
        return ", ".join(a["name"] for a in item.get("artists") or []).casefold()
    if sort_by == "album":
        return ((item.get("album") or {}).get("name") or "").casefold()
    if sort_by == "duration":
        return item.get("duration_ms") or 0
    return entry.get("added_at")  # "added_at": ISO dates sort correctly as text; can be None


def sort_entries(entries: list[dict], sort_by: str, descending: bool) -> list[dict]:
    """Sort entries. Python's sort is stable, so tracks that tie keep their current order."""
    # Entries with no value (only possible for added_at on very old playlists) go last
    present = [e for e in entries if get_sort_value(e, sort_by) is not None]
    missing = [e for e in entries if get_sort_value(e, sort_by) is None]
    present.sort(key=lambda e: get_sort_value(e, sort_by), reverse=descending)
    return present + missing


def parse_sort_keys(sort_by: list[str]) -> list[tuple[str, bool]]:
    """Turn ["artist", "-duration"] into [("artist", False), ("duration", True)].

    A leading "-" means that field is sorted descending.
    """
    if not sort_by:
        raise ValueError("sort_by must list at least one field.")
    keys = []
    seen = set()
    for raw in sort_by:
        name = raw.strip()
        descending = name.startswith("-")
        if descending:
            name = name[1:]
        if name not in SORT_KEYS:
            raise ValueError(f"Unknown sort field {raw!r}. Use: {', '.join(SORT_KEYS)}.")
        if name in seen:
            raise ValueError(f"Sort field {name!r} is listed more than once.")
        seen.add(name)
        keys.append((name, descending))
    return keys


def sort_entries_multi(entries: list[dict], sort_keys: list[tuple[str, bool]]) -> list[dict]:
    """Sort by several fields, most important first.

    We sort by the least important field first and the most important last. Each sort
    is stable (ties keep their current order), so the earlier sorts act as tie-breakers.
    """
    for name, descending in reversed(sort_keys):
        entries = sort_entries(entries, name, descending)
    return entries


def save_backup(playlist_id: str, snapshot_id: str, uris: list[str]) -> str:
    """Save the playlist's current track order to a local file. Returns the file's name.

    Only URIs are stored (no titles or artists). The model never reads this file.
    """
    BACKUP_DIR.mkdir(exist_ok=True)
    name = f"{playlist_id}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path = BACKUP_DIR / name
    path.write_text(
        json.dumps(
            {"playlist_id": playlist_id, "snapshot_id": snapshot_id, "uris": uris}, indent=2
        )
    )
    try:
        path.chmod(0o600)  # only the user can read it
    except OSError:
        pass
    return name


async def replace_playlist_items(playlist_id: str, uris: list[str]) -> str | None:
    """Make the playlist contain exactly these tracks, in this order.

    The first batch replaces everything in one request (no empty moment in between).
    Playlists over 100 tracks append the rest in further batches, so those are not atomic.
    Returns the latest snapshot ID.
    """
    path = f"/playlists/{playlist_id}/items"
    data = await spotify_request(
        "PUT", path, json_body={"uris": uris[:REPLACE_BATCH_SIZE]}
    )
    snapshot_id = data.get("snapshot_id")
    for start in range(REPLACE_BATCH_SIZE, len(uris), REPLACE_BATCH_SIZE):
        data = await spotify_request(
            "POST", path, json_body={"uris": uris[start : start + REPLACE_BATCH_SIZE]}
        )
        snapshot_id = data.get("snapshot_id")
    return snapshot_id

# ---------------------------------------------------
# ----------------- MCP TOOLS -----------------------
# ---------------------------------------------------

@mcp.tool
async def create_playlist(name: str, description: str = "", public: bool = False) -> dict:
    """Create a new, empty playlist owned by the user.

    The playlist is private unless public=True. Use add_items_to_playlist to fill it.
    Returns the new playlist's id, which the other tools need.
    """
    name = check_text(name, "name", MAX_NAME_LENGTH)
    if not name:
        raise ValueError("name can't be empty.")
    description = check_text(description, "description", MAX_DESCRIPTION_LENGTH)

    body = {"name": name, "public": public}
    if description:  # only send a description if there is one
        body["description"] = description

    p = await spotify_request("POST", "/me/playlists", json_body=body)
    return {
        "id": p["id"],
        "name": p["name"],
        "public": p.get("public"),
        "url": p["external_urls"].get("spotify"),
    }


@mcp.tool
async def add_items_to_playlist(
    playlist_id: str, uris: list[str], position: int | None = None
) -> dict:
    """Add tracks to a playlist the user owns.

    uris is a list of track URIs like 'spotify:track:4iV5W9uYEdYUVa79Axb7Rh' (up to 50).
    position is a zero-based index to insert at (0 = the very start). If left out,
    the tracks are added to the end, in the order given.
    """
    check_track_uris(uris)
    if position is not None and position < 0:
        raise ValueError("position can't be negative.")
    await require_owned_playlist(playlist_id)

    body: dict = {"uris": uris}
    if position is not None:
        body["position"] = position

    data = await spotify_request("POST", f"/playlists/{playlist_id}/items", json_body=body)
    return {"added": len(uris), "snapshot_id": data.get("snapshot_id")}


@mcp.tool
async def remove_items_from_playlist(
    playlist_id: str, uris: list[str], snapshot_id: str | None = None
) -> dict:
    """Remove tracks from a playlist the user owns.

    uris is a list of track URIs like 'spotify:track:4iV5W9uYEdYUVa79Axb7Rh' (up to 50).
    Use get_playlist_items first to find the URIs of the tracks to remove. If a track
    appears more than once in the playlist, every copy may be removed.

    snapshot_id (optional) is the playlist version you read the tracks from.
    """
    check_track_uris(uris)
    if snapshot_id is not None and len(snapshot_id) > 200:
        raise ValueError("snapshot_id looks invalid.")
    await require_owned_playlist(playlist_id)

    # Spotify wants each URI wrapped in its own object: {"items": [{"uri": ...}, ...]}
    body: dict = {"items": [{"uri": uri} for uri in uris]}
    if snapshot_id:
        body["snapshot_id"] = snapshot_id

    data = await spotify_request("DELETE", f"/playlists/{playlist_id}/items", json_body=body)
    # "requested" is how many URIs we asked Spotify to remove, not a confirmed count
    return {"requested": len(uris), "snapshot_id": data.get("snapshot_id")}


@mcp.tool
async def update_playlist_details(
    playlist_id: str,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """Change a playlist's name, description, or public/private setting. Only works
    for playlists the user owns.

    Only the fields you provide are changed. At least one must be provided.
    """
    body: dict = {}
    if name is not None:
        name = check_text(name, "name", MAX_NAME_LENGTH)
        if not name:
            raise ValueError("name can't be empty.")
        body["name"] = name
    if description is not None:
        body["description"] = check_text(description, "description", MAX_DESCRIPTION_LENGTH)
    if not body:
        raise ValueError("Provide at least of name or description.")

    await require_owned_playlist(playlist_id)
    await spotify_request("PUT", f"/playlists/{playlist_id}", json_body=body)
    return {"updated": list(body.keys())}  # which fields were changed


@mcp.tool
async def reorder_playlist_items(
    playlist_id: str,
    range_start: int,
    insert_before: int,
    range_length: int = 1,
    snapshot_id: str | None = None,
) -> dict:
    """Move one or more tracks to a new position in a playlist the user owns.

    Positions are zero-based. range_start is the first track to move, range_length is
    how many tracks to move (default 1), and insert_before is the position they are
    moved in front of. Example: in a 10-track playlist, range_start=0, insert_before=10
    moves the first track to the end; range_start=9, insert_before=0 moves the last
    track to the start.

    snapshot_id (optional) makes the move fail if the playlist changed since you read it.
    """
    if range_start < 0 or insert_before < 0:
        raise ValueError("range_start and insert_before can't be negative.")
    if range_length < 1:
        raise ValueError("range_length must be at least 1.")
    if snapshot_id is not None and len(snapshot_id) > 200:
        raise ValueError("snapshot_id looks invalid.")
    await require_owned_playlist(playlist_id)

    body: dict = {
        "range_start": range_start,
        "insert_before": insert_before,
        "range_length": range_length,
    }
    if snapshot_id:
        body["snapshot_id"] = snapshot_id

    # The same endpoint also *replaces* items when "uris" is sent. We never send "uris"
    # here, so this tool can only reorder.
    data = await spotify_request("PUT", f"/playlists/{playlist_id}/items", json_body=body)
    return {"snapshot_id": data.get("snapshot_id")}


@mcp.tool
async def sort_playlist(playlist_id: str, sort_by: list[str]) -> dict:
    """Sort an entire playlist the user owns, by one or more fields.

    sort_by is a list of fields, most important first. Valid fields: title, artist,
    album, duration, added_at. Each sorts ascending (A-Z, shortest first, oldest first);
    put a '-' in front of a field to sort it descending. Tracks that tie on every field
    keep their current order.
    Example: ["artist", "album", "-duration"] sorts by artist, then by album within each
    artist, then longest track first within each album.

    The playlist's current order is saved to a local backup file before anything changes.
    Refuses playlists containing local files, podcast episodes, or unavailable tracks,
    because rewriting the playlist would drop them. Playlists over 100 tracks are
    rewritten in several requests, so they are briefly in a partial state.
    """
    sort_keys = parse_sort_keys(sort_by)  # validates the fields and reads any "-" prefixes
    check_playlist_id(playlist_id)

    # One request gives us the owner (for the safety check) and the playlist's version
    info = await spotify_get(f"/playlists/{playlist_id}", {"fields": "owner.id,snapshot_id"})
    await ensure_owner(info["owner"]["id"])
    snapshot_before = info["snapshot_id"]

    # Read the whole playlist, then make sure nothing in it would get lost
    entries = await fetch_all_playlist_entries(playlist_id, MAX_SORT_ITEMS)
    if not entries:
        raise ValueError("The playlist is empty, so there is nothing to sort.")
    bad = find_unsortable(entries)
    if bad:
        shown = ", ".join(str(p) for p in bad[:5])
        more = f" (and {len(bad) - 5} more)" if len(bad) > 5 else ""
        raise ValueError(
            f"Can't sort: {len(bad)} entries are local files, episodes, or unavailable "
            f"tracks (positions {shown}{more}). Sorting would delete them."
        )

    old_uris = [e["item"]["uri"] for e in entries]
    new_uris = [e["item"]["uri"] for e in sort_entries_multi(entries, sort_keys)]
    if new_uris == old_uris:  # already in order, so don't touch the playlist
        return {"changed": False, "sorted_by": sort_by, "total": len(old_uris)}

    # Save the original order first, in case something goes wrong later
    backup_name = save_backup(playlist_id, snapshot_before, old_uris)

    # If the playlist was edited while we were reading it, our sorted list is out of date
    latest = await spotify_get(f"/playlists/{playlist_id}", {"fields": "snapshot_id"})
    if latest["snapshot_id"] != snapshot_before:
        raise RuntimeError("The playlist changed while sorting. Nothing was modified; try again.")

    try:
        snapshot_after = await replace_playlist_items(playlist_id, new_uris)
    except Exception as exc:
        raise RuntimeError(
            f"Sorting failed partway ({exc}). The original order is saved in {backup_name}."
        ) from exc

    # Read the playlist back and confirm it matches what we meant to write
    final = await fetch_all_playlist_entries(playlist_id, MAX_SORT_ITEMS)
    final_uris = [(e.get("item") or {}).get("uri") for e in final]
    if final_uris != new_uris:
        raise RuntimeError(
            f"The playlist doesn't match the sorted order after writing. "
            f"The original order is saved in {backup_name}."
        )

    return {
        "changed": True,
        "sorted_by": sort_by,
        "total": len(new_uris),
        "backup": backup_name,
        "snapshot_id": snapshot_after,
    }


# Start the server over stdio when the file is run directly
if __name__ == "__main__":
    mcp.run(transport="stdio")