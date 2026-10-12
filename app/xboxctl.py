#!/usr/bin/env python
"""Xbox control module for Magic Box.

Controls Xbox console via Xbox Web API (SmartGlass).
"""

import asyncio
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Global state
_config = {}
_xbl_client = None
_http_client = None  # Keep reference to close on cleanup

TOKEN_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "xbox_tokens.json")


def load_config(tags: dict) -> None:
    """Load Xbox configuration from tags.yml."""
    global _config
    _config = {
        'client_id': tags.get('xbox_client_id', ''),
        'client_secret': tags.get('xbox_client_secret', ''),
        'device_id': tags.get('xbox_device_id', ''),
        # The console THIS pad drives (SmartGlass id); every action targets it
        # unless the caller names another. Without it the account's consoles
        # all answer - a game-room figure would wake the basement Xbox too.
        'console_id': str(tags.get('xbox_console_id', '') or '').strip(),
    }
    logger.info("Xbox config loaded")


def _command_was_sent(e: Exception) -> bool:
    """xbox-webapi 2.1 parses Xbox Live's reply into a CommandResponse model
    that the service has since outgrown; the HTTP call was already answered
    200 by then, so a pydantic ValidationError means 'sent, reply not
    understood' - not a failure. Anything else is a real one."""
    try:
        from pydantic import ValidationError
    except ImportError:  # pragma: no cover
        return False
    # Only the REPLY model counts; a validation error while building a request
    # would mean nothing was sent.
    return isinstance(e, ValidationError) and getattr(e, 'title', '') == 'CommandResponse'


def _target(console_id: Optional[str]) -> Optional[str]:
    """The console an action should hit: the explicit argument, else the pad's
    configured xbox_console_id, else None (= whatever the account lists)."""
    return console_id or _config.get('console_id') or None


def configured() -> bool:
    """Check if Xbox control is configured."""
    return os.path.exists(TOKEN_FILE)


async def _get_authenticated_client():
    """Get an authenticated Xbox Live client."""
    global _xbl_client, _http_client

    if _xbl_client:
        return _xbl_client

    import httpx
    from xbox.webapi.api.client import XboxLiveClient
    from xbox.webapi.authentication.manager import AuthenticationManager
    from xbox.webapi.authentication.models import OAuth2TokenResponse

    if not os.path.exists(TOKEN_FILE):
        logger.error("Xbox tokens not found. Run xbox_auth_debug.py first.")
        return None

    with open(TOKEN_FILE, 'r') as f:
        tokens = json.load(f)

    client_id = tokens.get('client_id', _config.get('client_id', ''))
    client_secret = _config.get('client_secret', '')

    if not client_secret:
        logger.error("Xbox client_secret not configured in tags.yml")
        return None

    # Create HTTP client session (stored globally for cleanup)
    _http_client = httpx.AsyncClient()
    http_client = _http_client

    # Create auth manager with session
    auth_mgr = AuthenticationManager(
        http_client,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://localhost:8080/auth/callback"
    )

    # Load OAuth tokens
    auth_mgr.oauth = OAuth2TokenResponse(
        access_token=tokens['access_token'],
        refresh_token=tokens['refresh_token'],
        token_type=tokens.get('token_type', 'bearer'),
        expires_in=tokens.get('expires_in', 3600),
        scope=tokens.get('scope', ''),
        user_id=tokens.get('user_id', '')
    )

    try:
        # The file holds no issue time, so the loaded access token looks
        # freshly minted to the library and refresh_tokens() would keep
        # it; an hour after sign-in Xbox Live then answers 401. Always
        # trade the refresh token for a new access token first.
        auth_mgr.oauth = await auth_mgr.refresh_oauth_token()
        await auth_mgr.refresh_tokens()

        # Save updated tokens
        updated_tokens = {
            'access_token': auth_mgr.oauth.access_token,
            'refresh_token': auth_mgr.oauth.refresh_token,
            'token_type': auth_mgr.oauth.token_type,
            'expires_in': auth_mgr.oauth.expires_in,
            'scope': auth_mgr.oauth.scope,
            'user_id': auth_mgr.oauth.user_id,
            'client_id': client_id,
        }
        with open(TOKEN_FILE, 'w') as f:
            json.dump(updated_tokens, f, indent=2)

        # Create Xbox Live client
        _xbl_client = XboxLiveClient(auth_mgr)
        logger.info("Xbox Live client authenticated successfully")
        return _xbl_client

    except Exception as e:
        logger.error(f"Xbox authentication failed: {e}")
        await http_client.aclose()
        return None


async def get_consoles():
    """Get list of Xbox consoles associated with the account."""
    client = await _get_authenticated_client()
    if not client:
        return []

    try:
        # Get SmartGlass console list
        response = await client.smartglass.get_console_list()
        consoles = response.result if hasattr(response, 'result') else []
        logger.info(f"Found {len(consoles)} Xbox console(s)")
        return consoles
    except Exception as e:
        logger.error(f"Failed to get consoles: {e}")
        return []


async def power_on(console_id: str = None) -> bool:
    """Power on an Xbox console."""
    client = await _get_authenticated_client()
    if not client:
        return False

    try:
        console_id = _target(console_id)
        if console_id:
            await client.smartglass.wake_up(console_id)
        else:
            # No console configured or named: wake every console on the account
            logger.warning("xbox_console_id not set in tags.yml; waking every console on the account")
            consoles = await get_consoles()
            for console in consoles:
                await client.smartglass.wake_up(console.id)
        logger.info("Xbox power on command sent")
        return True
    except Exception as e:
        if _command_was_sent(e):
            logger.info("Xbox power on command sent (reply not understood by xbox-webapi; ignoring)")
            return True
        logger.error(f"Failed to power on Xbox: {e}")
        return False


async def power_off(console_id: str = None) -> bool:
    """Power off an Xbox console."""
    client = await _get_authenticated_client()
    if not client:
        return False

    try:
        target_id = _target(console_id)
        if not target_id:
            consoles = await get_consoles()
            target_id = consoles[0].id if consoles else None

        if target_id:
            await client.smartglass.turn_off(target_id)
            logger.info("Xbox power off command sent")
            return True
        return False
    except Exception as e:
        if _command_was_sent(e):
            logger.info("Xbox power off command sent (reply not understood by xbox-webapi; ignoring)")
            return True
        logger.error(f"Failed to power off Xbox: {e}")
        return False


async def launch_app(app_id: str, console_id: str = None) -> bool:
    """Launch an app/game on Xbox."""
    client = await _get_authenticated_client()
    if not client:
        return False

    try:
        target_id = _target(console_id)
        if not target_id:
            consoles = await get_consoles()
            target_id = consoles[0].id if consoles else None

        if target_id:
            # Launch the app
            await client.smartglass.launch_app(target_id, app_id)
            logger.info(f"Launched app {app_id} on Xbox")
            return True
        return False
    except Exception as e:
        if _command_was_sent(e):
            logger.info("Xbox launch command sent (reply not understood by xbox-webapi; ignoring)")
            return True
        logger.error(f"Failed to launch app: {e}")
        return False


async def get_console_status(console_id: str = None) -> Optional[dict]:
    """Get status of an Xbox console."""
    client = await _get_authenticated_client()
    if not client:
        return None

    try:
        response = await client.smartglass.get_console_status(_target(console_id))
        return response
    except Exception as e:
        logger.error(f"Failed to get console status: {e}")
        return None


# Sync wrappers for use from non-async code

def _run(coro):
    """Run one action on its own event loop and forget the cached client
    afterwards: the httpx client and XboxLiveClient are bound to the loop
    they were created on, and that loop is closed here - reusing them from
    the next action raised 'Event loop is closed' (the red/blue flash). The
    tokens stay in TOKEN_FILE, so the next action only pays the refresh."""
    global _xbl_client, _http_client
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        if _http_client is not None:
            try:
                loop.run_until_complete(_http_client.aclose())
            except Exception:
                pass
        _xbl_client = None
        _http_client = None
        loop.close()


def sync_get_consoles():
    """Synchronous wrapper for get_consoles."""
    return _run(get_consoles())


def sync_power_on(console_id: str = None) -> bool:
    """Synchronous wrapper for power_on."""
    return _run(power_on(console_id))


def sync_power_off(console_id: str = None) -> bool:
    """Synchronous wrapper for power_off."""
    return _run(power_off(console_id))


def sync_launch_app(app_id: str, console_id: str = None) -> bool:
    """Synchronous wrapper for launch_app."""
    return _run(launch_app(app_id, console_id))


# Test function
async def test_connection():
    """Test Xbox connection and list consoles."""
    print("\n=== Xbox Connection Test ===\n")

    client = await _get_authenticated_client()
    if not client:
        print("Failed to authenticate!")
        return False

    print("Authenticated successfully!")

    consoles = await get_consoles()
    if consoles:
        print(f"\nFound {len(consoles)} console(s):")
        for c in consoles:
            print(f"  - {c.name} (ID: {c.id})")
            print(f"    Power State: {c.power_state}")
    else:
        print("No consoles found.")

    return True


if __name__ == "__main__":
    asyncio.run(test_connection())
