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
import csv
import difflib
import heapq
import math
from pathlib import Path

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

# The source CSV at <project root>/data/raw/.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET_PATH = PROJECT_ROOT / "data" / "raw" / "spotify-tracks-dataset-detailed.csv"

# Audio features the recommender will use from the dataset
DATASET_FEATURE_RANGES = {
    "danceability": 1.0,
    "energy": 1.0,
    "valence": 1.0,
    "acousticness": 1.0,
    "instrumentalness": 1.0,
    "liveness": 1.0,
    "speechiness": 1.0,
    "tempo": 250.0,
    "loudness": 60.0,
}

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

@mcp.tool
def recommend_dataset_tracks(
    genre: str | None = None,
    seed_track_id: str | None = None,
    target_danceability: float | None = None,
    target_energy: float | None = None,
    target_valence: float | None = None,
    target_acousticness: float | None = None,
    target_instrumentalness: float | None = None,
    target_liveness: float | None = None,
    target_speechiness: float | None = None,
    target_tempo: float | None = None,
    target_loudness: float | None = None,
    limit: int = 10,
) -> list[dict]:
    """Recommend tracks from the local Spotify dataset.

    Filter by the dataset's exact genre label and/or rank by closeness to the
    given audio-feature targets. Pass a seed_track_id that exists in the
    dataset to use that track's features as reference. Most feature values are 
    0-1, tempo is BPM, and loudness is dB.

    When no feature targets or seed are provided, results are ordered by the
    dataset's popularity field. These are dataset candidates, not a live
    Spotify availability check. For the hybrid result, pair them with
    search_songs; dataset feature scores only apply to tracks in the dataset.
    """
    if not DATASET_PATH.is_file():
        raise FileNotFoundError(
            f"Dataset CSV not found at {DATASET_PATH}. Put "
            "spotify-tracks-dataset-detailed.csv in the project's "
            "data/raw folder"
        )

    # Using dict to map the target features to column names
    limit = 10
    seed_track_id = seed_track_id.strip() if seed_track_id else None
    target_values = {
        "danceability": target_danceability,
        "energy": target_energy,
        "valence": target_valence,
        "acousticness": target_acousticness,
        "instrumentalness": target_instrumentalness,
        "liveness": target_liveness,
        "speechiness": target_speechiness,
        "tempo": target_tempo,
        "loudness": target_loudness,
    }

    # Catching errors for invalid feature values 
    for feature, value in target_values.items():
        if value is None:
            continue
        if not math.isfinite(value):
            raise ValueError(f"target_{feature} must be a valid number.")
        if feature in {
            "danceability", "energy", "valence", "acousticness",
            "instrumentalness", "liveness", "speechiness",
        } and not 0.0 <= value <= 1.0:
            raise ValueError(f"target_{feature} must be between 0 and 1.")
        if feature == "tempo" and not 0.0 <= value <= 250.0:
            raise ValueError("target_tempo must be between 0 and 250 BPM.")
        if feature == "loudness" and not -60.0 <= value <= 0.0:
            raise ValueError("target_loudness must be between -60 and 0 dB.")

    # Checking if the seed track is in the dataset
    # Using the seed track's audio features and targets
    seed_row = None
    if seed_track_id:
        with DATASET_PATH.open("r", encoding="utf-8-sig", newline="") as dataset_file:
            for row in csv.DictReader(dataset_file):
                if row.get("track_id", "").strip() == seed_track_id.strip():
                    seed_row = row
                    break
        if seed_row is None:
            raise ValueError(
                "seed_track_id was not found in the local dataset. Use a track "
                "from the dataset, or provide target audio-feature values instead."
            )
        # Copy the seed tracks features to any unspecified 
        for feature in DATASET_FEATURE_RANGES:
            if target_values[feature] is None:
                try:
                    target_values[feature] = float(seed_row[feature])
                except (KeyError, TypeError, ValueError):
                    pass

    # Keeping targets specified or used above from the seed track
    active_targets = {
        feature: value
        for feature, value in target_values.items()
        if value is not None
    }

    # Collects already seen genres and prevents repeated ids holding
    # best results in a list
    requested_genre = genre.strip().casefold() if genre else None
    seen_genres: set[str] = set()
    seen_track_ids: set[str] = set()
    best_rows: list[tuple[float, float, int, dict]] = []

    # Song filtering and collection
    with DATASET_PATH.open("r", encoding="utf-8-sig", newline="") as dataset_file:
        for row_number, row in enumerate(csv.DictReader(dataset_file)):
            track_id = row.get("track_id", "").strip()
            row_genre = row.get("track_genre", "").strip()
            if row_genre:
                seen_genres.add(row_genre)
            if not track_id or track_id == seed_track_id:
                continue
            if requested_genre and row_genre.casefold() != requested_genre:
                continue
            if track_id in seen_track_ids:
                continue

            # Converting CSV features to numeric values
            try:
                popularity = float(row.get("popularity", "0") or 0)
                features = {
                    feature: float(row[feature])
                    for feature in active_targets
                }
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(popularity) or any(
                not math.isfinite(value) for value in features.values()
            ):
                continue
            seen_track_ids.add(track_id)

            # Using squared distance here for ranking and to normalize features
            # Divides by feature range to normalize bpm and db
            # Squares the distance between target and track value for negative values
            # Averages the feature differences and square roots them giving us a match score
            if active_targets:
                squared_distance = sum(
                    ((features[feature] - target) / DATASET_FEATURE_RANGES[feature]) ** 2
                    for feature, target in active_targets.items()
                ) / len(active_targets)
                match_score = max(0.0, 1.0 - math.sqrt(squared_distance))
            else:
                match_score = 0.0

            # This is the result that is being sent back to Claude
            artists = [name.strip() for name in row.get("artists", "").split(";") if name.strip()]
            result = {
                "track_id": track_id,
                "uri": f"spotify:track:{track_id}",
                "track_name": row.get("track_name", ""),
                "artists": artists,
                "album_name": row.get("album_name", ""),
                "genre": row_genre,
                "dataset_popularity": int(popularity),
                "match_score": round(match_score * 100, 1) if active_targets else None,
                "audio_features": features,
                "source": "dataset",
            }
            # Making sure we stay withing our song limit
            # Using a heap to make it easier to keep track of and replace worse results
            candidate = (match_score, popularity, -row_number, result)
            if len(best_rows) < limit:
                heapq.heappush(best_rows, candidate)
            elif candidate > best_rows[0]:
                heapq.heapreplace(best_rows, candidate)

    # Looks for similar genres or labels if the one requested does not exist
    if requested_genre and not best_rows:
        suggestions = difflib.get_close_matches(
            genre.strip(), sorted(seen_genres), n=5, cutoff=0.35
        )
        hint = f" Similar dataset genres: {', '.join(suggestions)}." if suggestions else ""
        raise ValueError(f"No dataset tracks matched genre '{genre}'.{hint}")

    # Sorts candidates from best to worst
    return [candidate[3] for candidate in sorted(best_rows, reverse=True)]

# Start the server over stdio when the file is run directly
if __name__ == "__main__":
    mcp.run(transport="stdio")