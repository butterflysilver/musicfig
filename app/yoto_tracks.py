#!/usr/bin/env python
"""The Pi's client for the Yoto MCP on Railway: a card's playable track URLs
(for streaming to a HomePod) and play / pause / resume / stop on a real Yoto
player (the "card to device" tag action).

The MCP is the single holder of the Yoto OAuth token (life-ops #69/#70); a Pi
never talks to Yoto directly. At tap time it either asks the tracks route for
signed, time-limited HTTPS URLs that pyatv can stream, or asks the players
route to start the card on the Yoto itself. One shared secret covers both.

Environment (set in the systemd unit by scripts/pi/install.sh):
  MUSICFIG_YOTO_TRACKS_URL   base URL of the Yoto MCP (default: the Railway service)
  MUSICFIG_YOTO_TRACKS_KEY_FILE  file holding the shared secret (root:musicfig 0640)
"""

import logging
import os
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://yoto-mcp-production.up.railway.app"
DEFAULT_KEY_FILE = "/etc/musicfig/yoto-tracks.key"
HEADER = "X-API-Key"
TIMEOUT_S = 25
# Player selector the MCP understands besides an id or a name: first online player.
ANY_ONLINE_PLAYER = "@online"
PLAYER_ACTIONS = ("play", "pause", "resume", "stop")


def _base_url() -> str:
    return (os.environ.get("MUSICFIG_YOTO_TRACKS_URL") or DEFAULT_BASE_URL).rstrip("/")


def _shared_key() -> Optional[str]:
    path = os.environ.get("MUSICFIG_YOTO_TRACKS_KEY_FILE") or DEFAULT_KEY_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            key = f.read().strip()
    except OSError:
        logger.error("Yoto tracks: shared key file %s is missing or unreadable", path)
        return None
    if not key:
        logger.error("Yoto tracks: shared key file %s is empty", path)
        return None
    return key


def configured() -> bool:
    return _shared_key() is not None


def fetch_tracks(card_id: str) -> list:
    """Return [(title, url), ...] for the card's playable tracks, in order.
    Empty list (with a logged reason) on any failure - never raises."""
    card_id = str(card_id or "").strip()  # str(): YAML parses an all-digit id as int
    if not card_id:
        logger.error("Yoto tracks: no card id")
        return []
    key = _shared_key()
    if key is None:
        return []
    url = f"{_base_url()}/api/cards/{quote(card_id, safe='')}/tracks"
    try:
        response = requests.get(url, headers={HEADER: key}, timeout=TIMEOUT_S)
    except requests.RequestException as e:
        logger.error("Yoto tracks: could not reach the Yoto MCP (%s)", e.__class__.__name__)
        return []
    if response.status_code == 404:
        logger.error("Yoto tracks: card %s not found", card_id)
        return []
    if response.status_code == 401:
        logger.error("Yoto tracks: the shared key was rejected")
        return []
    if response.status_code != 200:
        logger.error("Yoto tracks: HTTP %s from the Yoto MCP", response.status_code)
        return []
    try:
        payload = response.json()
    except ValueError:
        logger.error("Yoto tracks: non-JSON reply from the Yoto MCP")
        return []
    tracks = payload.get("tracks") if isinstance(payload, dict) else None
    if not isinstance(tracks, list):
        logger.error("Yoto tracks: unexpected reply shape")
        return []
    playable = [
        (str(t.get("title") or f"track {i}"), t["url"])
        for i, t in enumerate(tracks, start=1)
        if isinstance(t, dict) and t.get("playable") and isinstance(t.get("url"), str)
    ]
    logger.info("Yoto tracks: %s -> %d playable of %d (%s)",
                card_id, len(playable), len(tracks), payload.get("title"))
    return playable


def _player_path(player: Optional[str], action: str) -> str:
    selector = str(player or "").strip() or ANY_ONLINE_PLAYER
    return f"{_base_url()}/api/players/{quote(selector, safe='')}/{action}"


def _post_player(player: Optional[str], action: str, body: Optional[dict] = None) -> Optional[dict]:
    """POST a player command to the MCP. Returns the reply document on 200,
    None (with the reason logged, in the Pi's own words) on any failure."""
    if action not in PLAYER_ACTIONS:
        logger.error("Yoto player: unknown action %s", action)
        return None
    key = _shared_key()
    if key is None:
        return None
    url = _player_path(player, action)
    try:
        response = requests.post(url, headers={HEADER: key}, json=body, timeout=TIMEOUT_S)
    except requests.RequestException as e:
        logger.error("Yoto player: could not reach the Yoto MCP (%s)", e.__class__.__name__)
        return None
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if response.status_code == 200:
        return payload
    reason = payload.get("error") or f"HTTP {response.status_code}"
    if response.status_code == 409:
        name = (payload.get("player") or {}).get("name") if isinstance(payload.get("player"), dict) else None
        logger.error("Yoto player: %s is offline (switch it on, or carry it closer to Wi-Fi)",
                     name or player or "the player")
    elif response.status_code == 401:
        logger.error("Yoto player: the shared key was rejected")
    else:
        logger.error("Yoto player: %s (%s)", reason, action)
    return None


def play_card(card_id: str, player: Optional[str] = None) -> Optional[dict]:
    """Play a card on a Yoto player (id, name or @online; None = @online).
    Returns {"id", "name", "title"} of what is now playing, or None."""
    card_id = str(card_id or "").strip()
    if not card_id:
        logger.error("Yoto player: no card id")
        return None
    reply = _post_player(player, "play", {"card_id": card_id})
    if reply is None:
        return None
    found = reply.get("player") if isinstance(reply.get("player"), dict) else {}
    result = {"id": str(found.get("id") or ""), "name": str(found.get("name") or "Yoto"),
              "title": str(reply.get("title") or card_id)}
    if not result["id"]:
        logger.error("Yoto player: reply without a player id")
        return None
    logger.info("Yoto player: '%s' playing on %s", result["title"], result["name"])
    return result


def player_command(player: Optional[str], action: str) -> bool:
    """pause / resume / stop a player (id, name or @online). True on success."""
    if action == "play":
        logger.error("Yoto player: use play_card to play")
        return False
    return _post_player(player, action) is not None
