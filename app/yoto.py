#!/usr/bin/env python
"""Yoto player control module for Magic Box.

Controls Yoto players via yoto-api library.
"""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# Global state
_config = {}
_yoto_manager = None

# File paths
TOKEN_FILE = os.path.expanduser("~/.yoto-tokens.json")
CONFIG_FILE = os.path.expanduser("~/.config/mcp-yoto/config.json")


def load_config(tags: dict) -> None:
    """Load Yoto configuration from tags.yml."""
    global _config
    _config = {
        'client_id': tags.get('yoto_client_id', ''),
        'default_player': tags.get('yoto_default_player', ''),
    }
    logger.info("Yoto config loaded")


def configured() -> bool:
    """Check if Yoto is configured with valid tokens."""
    return os.path.exists(TOKEN_FILE) and os.path.exists(CONFIG_FILE)


def _get_authenticated_manager():
    """Get an authenticated Yoto manager."""
    global _yoto_manager

    if _yoto_manager:
        return _yoto_manager

    try:
        from yoto_api import Token, YotoManager
    except ImportError:
        logger.error("yoto-api not installed. Run: pip install yoto-api")
        return None

    if not os.path.exists(CONFIG_FILE):
        logger.error("Yoto config not found at %s. Run yoto_setup first.", CONFIG_FILE)
        return None

    if not os.path.exists(TOKEN_FILE):
        logger.error("Yoto tokens not found at %s. Run yoto_login first.", TOKEN_FILE)
        return None

    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
        with open(TOKEN_FILE, 'r') as f:
            tokens = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Failed to read config/token files: {e}")
        return None

    client_id = config.get('client_id')
    if not client_id:
        logger.error("No client_id in Yoto config file")
        return None

    try:
        from datetime import datetime

        # Create manager with client_id
        _yoto_manager = YotoManager(client_id=client_id)

        # Set tokens
        valid_until = None
        if tokens.get('valid_until'):
            try:
                valid_until = datetime.fromisoformat(tokens['valid_until'])
            except (ValueError, TypeError):
                pass

        _yoto_manager.token = Token(
            access_token=tokens['access_token'],
            refresh_token=tokens['refresh_token'],
            token_type=tokens.get('token_type', 'Bearer'),
            scope='',
            valid_until=valid_until,
        )

        # Refresh token if needed
        _yoto_manager.check_and_refresh_token()

        logger.info("Yoto manager authenticated successfully")
        return _yoto_manager
    except Exception as e:
        logger.error(f"Yoto authentication failed: {e}")
        return None


def _find_player(players: list, player_id: str = None):
    """Find a player by ID or use default/first online."""
    if player_id:
        for p in players:
            if p.id == player_id or getattr(p, 'name', '') == player_id:
                return p
    elif _config.get('default_player'):
        for p in players:
            if p.id == _config['default_player'] or getattr(p, 'name', '') == _config['default_player']:
                return p
    else:
        # First online player
        for p in players:
            if getattr(p, 'online', False):
                return p
        # Fallback to first player
        if players:
            return players[0]
    return None


def get_players() -> list:
    """Get list of Yoto players."""
    manager = _get_authenticated_manager()
    if not manager:
        return []

    try:
        manager.update_players_status()
        players = list(manager.players.values())
        logger.info(f"Found {len(players)} Yoto player(s)")
        return players
    except Exception as e:
        logger.error(f"Failed to get players: {e}")
        return []


def get_library() -> list:
    """Get the Yoto card library."""
    manager = _get_authenticated_manager()
    if not manager:
        return []

    try:
        manager.update_library()
        cards = list(manager.library.values())
        logger.info(f"Found {len(cards)} cards in library")
        return cards
    except Exception as e:
        logger.error(f"Failed to get library: {e}")
        return []


def play_card(card_id: str, player_id: str = None) -> bool:
    """Play a card on a Yoto player.

    Args:
        card_id: The card ID to play
        player_id: Optional player ID (uses default or first online if not specified)

    Returns:
        True if successful, False otherwise
    """
    manager = _get_authenticated_manager()
    if not manager:
        return False

    try:
        # Get available players
        manager.update_players_status()
        players = list(manager.players.values())

        if not players:
            logger.error("No Yoto players found")
            return False

        target_player = _find_player(players, player_id)

        if not target_player:
            logger.error("No suitable Yoto player found")
            return False

        # Connect to MQTT events (required for playback commands)
        logger.info("Connecting to Yoto events...")
        manager.connect_to_events()
        time.sleep(1)  # Give MQTT time to connect

        # Play the card
        manager.play_card(target_player.id, card_id)
        logger.info(f"Playing card {card_id} on {target_player.name}")

        # Disconnect MQTT
        manager.disconnect()
        return True

    except Exception as e:
        logger.error(f"Failed to play card: {e}")
        # Try to disconnect on error
        try:
            manager.disconnect()
        except Exception:
            pass
        return False


def pause_player(player_id: str = None) -> bool:
    """Pause playback on a Yoto player."""
    manager = _get_authenticated_manager()
    if not manager:
        return False

    try:
        manager.update_players_status()
        players = list(manager.players.values())
        target = _find_player(players, player_id)

        if not target:
            logger.error("No suitable Yoto player found")
            return False

        # Connect to MQTT events (required for playback commands)
        manager.connect_to_events()
        time.sleep(1)

        manager.pause_player(target.id)
        logger.info(f"Paused {target.name}")

        manager.disconnect()
        return True
    except Exception as e:
        logger.error(f"Failed to pause: {e}")
        try:
            manager.disconnect()
        except Exception:
            pass
        return False


def resume_player(player_id: str = None) -> bool:
    """Resume playback on a Yoto player."""
    manager = _get_authenticated_manager()
    if not manager:
        return False

    try:
        manager.update_players_status()
        players = list(manager.players.values())
        target = _find_player(players, player_id)

        if not target:
            logger.error("No suitable Yoto player found")
            return False

        # Connect to MQTT events (required for playback commands)
        manager.connect_to_events()
        time.sleep(1)

        manager.resume_player(target.id)
        logger.info(f"Resumed {target.name}")

        manager.disconnect()
        return True
    except Exception as e:
        logger.error(f"Failed to resume: {e}")
        try:
            manager.disconnect()
        except Exception:
            pass
        return False


def stop_player(player_id: str = None) -> bool:
    """Stop playback on a Yoto player."""
    manager = _get_authenticated_manager()
    if not manager:
        return False

    try:
        manager.update_players_status()
        players = list(manager.players.values())
        target = _find_player(players, player_id)

        if not target:
            logger.error("No suitable Yoto player found")
            return False

        # Connect to MQTT events (required for playback commands)
        manager.connect_to_events()
        time.sleep(1)

        manager.stop_player(target.id)
        logger.info(f"Stopped {target.name}")

        manager.disconnect()
        return True
    except Exception as e:
        logger.error(f"Failed to stop: {e}")
        try:
            manager.disconnect()
        except Exception:
            pass
        return False


# Sync wrappers for use from non-async code (lego.py)
# All functions are now sync, so these are simple pass-throughs

def sync_get_players():
    """Synchronous wrapper for get_players."""
    return get_players()


def sync_get_library():
    """Synchronous wrapper for get_library."""
    return get_library()


def sync_play_card(card_id: str, player_id: str = None) -> bool:
    """Synchronous wrapper for play_card."""
    return play_card(card_id, player_id)


def sync_pause(player_id: str = None) -> bool:
    """Synchronous wrapper for pause_player."""
    return pause_player(player_id)


def sync_resume(player_id: str = None) -> bool:
    """Synchronous wrapper for resume_player."""
    return resume_player(player_id)


def sync_stop(player_id: str = None) -> bool:
    """Synchronous wrapper for stop_player."""
    return stop_player(player_id)


def test_connection() -> bool:
    """Test Yoto connection and list players."""
    print("\n=== Yoto Connection Test ===\n")

    manager = _get_authenticated_manager()
    if not manager:
        print("Failed to authenticate!")
        return False

    print("Authenticated successfully!")

    players = get_players()
    if players:
        print(f"\nFound {len(players)} player(s):")
        for p in players:
            status = "online" if getattr(p, 'online', False) else "offline"
            print(f"  - {p.name} (ID: {p.id}) [{status}]")
    else:
        print("No players found.")

    cards = get_library()
    if cards:
        print(f"\nLibrary has {len(cards)} cards")
        # Show first 5
        for c in cards[:5]:
            print(f"  - {c.title} (ID: {c.id})")
        if len(cards) > 5:
            print(f"  ... and {len(cards) - 5} more")

    return True


if __name__ == "__main__":
    test_connection()
