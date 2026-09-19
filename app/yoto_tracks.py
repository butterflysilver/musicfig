#!/usr/bin/env python
"""Playable track URLs for a Yoto card, fetched from the Yoto MCP on Railway.

The MCP is the single holder of the Yoto OAuth token (life-ops #69/#70); a Pi
never talks to Yoto directly. It asks the MCP's tracks route at tap time and
gets back signed, time-limited HTTPS URLs that pyatv can stream to a HomePod.

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
    card_id = (card_id or "").strip()
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
