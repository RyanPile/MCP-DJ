"""Im using the triple qoute comments for debugging so they 
can be ignored for the most part."""

import os
from pathlib import Path
from dotenv import load_dotenv
import base64
import hashlib
import secrets
from urllib.parse import urlencode, urlparse, parse_qs
import webbrowser
import httpx
import json
import time

# Finding filepath and loading the local .env file
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

#Getting the Client Id and Redirect URI from .env file
CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")

"""
print("Looking for .env at:", PROJECT_ROOT / ".env")
print("File exists:", (PROJECT_ROOT / ".env").is_file())
print("Redirect value:", repr(REDIRECT_URI))

print("Client ID found:", CLIENT_ID is not None)
print("Redirect URI found", REDIRECT_URI is not None)
"""

# Creating a challenge using the SHA256 algorithm
# Verifier is a string, encoding with ascii converts it to bytes for the hash
def make_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii")
    return encoded.rstrip("=")

"""
test_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
print("Test challenge:", make_code_challenge(test_verifier))
"""

#creating a random 43 character string 32 bytes
code_verifier = secrets.token_urlsafe(32)
code_challenge = make_code_challenge(code_verifier)

"""
print("Verifier secret:", code_verifier)
print("Verifier length:", len(code_verifier))
print("Challenge length:", len(code_challenge))
"""

#Spotify recommended additional state to protect against unwanted requests 
#smaller version of the verifier essentially
state = secrets.token_urlsafe(16)

""" print("State length:", len(state)) """

scope = "playlist-read-private playlist-modify-private playlist-modify-public"
AUTH_URL = "https://accounts.spotify.com/authorize"

authorization_params = {
    "client_id": CLIENT_ID,
    "response_type": "code",
    "redirect_uri": REDIRECT_URI,
    "scope": scope,
    "state": state,
    "code_challenge_method": "S256",
    "code_challenge": code_challenge,
}

#formatting the url by encoding the parameters 
authorization_url = (
    f"{AUTH_URL}?{urlencode(authorization_params)}"
)

"""
print(authorization_url)
"""

# Opens spotify's authorization url
webbrowser.open(authorization_url)

#Redirected URL spotify gives
redirected_url = input(
    "Paste URL from browser: "
)

#parsing the url converting url text into a dictionary
query_params = parse_qs(urlparse(redirected_url).query)

#checks for the parameters in the parsed url
returned_code = query_params.get("code", [None])[0]
returned_state = query_params.get("state", [None])[0]
returned_error = query_params.get("error", [None])[0]

if returned_state != state:
    raise ValueError("The returned state does not match.")
if returned_error:
    raise RuntimeError(f"Spotify authorization failed: {returned_error}")
if returned_code is None:
    raise ValueError("The callback URL did not contain an authorization code.")

authorization_code = returned_code
print("State matched; authorization code received.")

# Token request from spotify's token endpoint
token_response = httpx.post(
    "https://accounts.spotify.com/api/token",
    data={
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
        "code": authorization_code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier,
    },
    # Spotify requires this header format
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    timeout=30.0,
)

# Checking for https errors from spotify and parsing json response into a dictionary
token_response.raise_for_status()
token_data = token_response.json()
token_data["expires_at"] = time.time() + token_data["expires_in"]

#Saving the token in a json and converting from the dictionary to json text 
token_path = PROJECT_ROOT / ".spotify_token.json"
token_path.write_text(
    json.dumps(token_data, indent=2),
    encoding="utf-8",
)

print("Token file saved:", token_path.name)