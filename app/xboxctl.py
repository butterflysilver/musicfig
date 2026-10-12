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


async def _send(client, device_id: str, command_type: str, command: str, params=None) -> bool:
    """POST one SmartGlass command and judge it by Xbox Live's own verdict.
    xbox-webapi 2.1 parses the reply into a model the service has outgrown
    (a pydantic error) and, when it does parse, never looks at status.errorCode,
    so a wake Live could not deliver (ErrorCallingWNS: the console is shut down,
    offline, or not in Sleep) looked like success. The body mirrors the library's
    _send_one_shot_command; only status.errorCode == OK counts as delivered."""
    sg = client.smartglass
    body = {
        'destination': 'Xbox',
        'type': command_type,
        'command': command,
        'sessionId': sg._smartglass_session_id,
        'sourceId': 'com.microsoft.smartglass',
        'parameters': params or [{}],
        'linkedXboxId': device_id,
    }
    resp = await client.session.post(f'{sg.SG_URL}/commands', json=body, headers=sg.HEADERS_SG)
    resp.raise_for_status()
    try:
        status = (resp.json() or {}).get('status') or {}
    except ValueError:
        status = {}
    code = status.get('errorCode')
    if code != 'OK':
        logger.error("Xbox %s/%s not delivered to %s: %s %s", command_type, command, device_id,
                     code or 'no status in reply', status.get('errorMessage') or '')
        return False
    return True


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

    try:
        with open(TOKEN_FILE, 'r') as f:
            tokens = json.load(f)
        tokens['access_token'], tokens['refresh_token']
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.error("Xbox token file %s is unreadable or incomplete (%s); sign in again", TOKEN_FILE, e.__class__.__name__)
        return None

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
    """Wake the pad's console (or, with none configured, every console on the account)."""
    client = await _get_authenticated_client()
    if not client:
        return False

    try:
        console_id = _target(console_id)
        if console_id:
            targets = [console_id]
        else:
            logger.warning("xbox_console_id not set in tags.yml; waking every console on the account")
            targets = [c.id for c in await get_consoles()]
        ok = False
        for target in targets:
            ok = await _send(client, target, 'Power', 'WakeUp') or ok
        if ok:
            logger.info("Xbox power on command delivered")
        return ok
    except Exception as e:
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
        if not target_id:
            return False
        if await _send(client, target_id, 'Power', 'TurnOff'):
            logger.info("Xbox power off command delivered")
            return True
        return False
    except Exception as e:
        logger.error(f"Failed to power off Xbox: {e}")
        return False


async def launch_app(app_id: str, console_id: str = None) -> bool:
    """Launch an app/game (Microsoft Store product id) on Xbox."""
    client = await _get_authenticated_client()
    if not client:
        return False

    try:
        target_id = _target(console_id)
        if not target_id:
            consoles = await get_consoles()
            target_id = consoles[0].id if consoles else None
        if not target_id:
            return False
        if await _send(client, target_id, 'Shell', 'ActivateApplicationWithOneStoreProductId',
                       [{'oneStoreProductId': app_id}]):
            logger.info(f"Launched app {app_id} on Xbox")
            return True
        return False
    except Exception as e:
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
