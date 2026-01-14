#!/usr/bin/env python
"""
Apple TV integration for Musicfig using pyatv.
Enables launching Disney+, Netflix, and other streaming content via deep links.
"""

import asyncio
import logging
import pyatv
from pyatv.const import Protocol

logger = logging.getLogger(__name__)

# Cache for Apple TV connection
_atv = None
_atv_name = None

# Configuration - set these in config.py or tags.yml
APPLE_TV_NAME = None  # Will be auto-discovered if None
APPLE_TV_IP = None    # Optional: specify IP directly
APPLE_TV_ID = None    # Device identifier
COMPANION_CREDENTIALS = None
AIRPLAY_CREDENTIALS = None


async def scan_for_apple_tvs(timeout=5):
    """Scan network for Apple TVs and return list of found devices."""
    logger.info("Scanning for Apple TVs...")
    atvs = await pyatv.scan(asyncio.get_event_loop(), timeout=timeout)

    for atv in atvs:
        logger.info(f"Found: {atv.name} at {atv.address}")

    return atvs


async def connect(name=None, ip=None):
    """Connect to an Apple TV by name or IP address."""
    global _atv, _atv_name

    # Return cached connection if still valid
    if _atv is not None:
        try:
            # Test if connection is still alive
            await _atv.power.power_state
            return _atv
        except Exception:
            logger.info("Cached Apple TV connection lost, reconnecting...")
            _atv = None

    # Scan for devices
    atvs = await scan_for_apple_tvs()

    if not atvs:
        logger.error("No Apple TVs found on network")
        return None

    # Find the requested device
    target = None

    # First try by ID if we have it
    if APPLE_TV_ID:
        for atv in atvs:
            if APPLE_TV_ID in atv.all_identifiers:
                target = atv
                logger.info(f"Found Apple TV by ID: {atv.name}")
                break

    # Then try by IP
    if not target and (ip or APPLE_TV_IP):
        search_ip = ip or APPLE_TV_IP
        for atv in atvs:
            if str(atv.address) == search_ip:
                target = atv
                break

    # Then try by name
    if not target and (name or APPLE_TV_NAME):
        search_name = name or APPLE_TV_NAME
        for atv in atvs:
            if search_name.lower() in atv.name.lower():
                target = atv
                break

    # Fall back to first found
    if not target:
        target = atvs[0]
        logger.info(f"Using first found Apple TV: {target.name}")

    if not target:
        logger.error(f"Apple TV not found (name={name}, ip={ip})")
        return None

    logger.info(f"Connecting to {target.name}...")

    # Set credentials if we have them
    if COMPANION_CREDENTIALS:
        target.set_credentials(Protocol.Companion, COMPANION_CREDENTIALS)
        logger.info("Set Companion credentials")
    if AIRPLAY_CREDENTIALS:
        target.set_credentials(Protocol.AirPlay, AIRPLAY_CREDENTIALS)
        logger.info("Set AirPlay credentials")

    try:
        _atv = await pyatv.connect(target, asyncio.get_event_loop())
        _atv_name = target.name
        logger.info(f"Connected to {target.name}")
        return _atv
    except Exception as e:
        logger.error(f"Failed to connect to Apple TV: {e}")
        return None


async def disconnect():
    """Disconnect from Apple TV."""
    global _atv, _atv_name
    if _atv:
        _atv.close()
        logger.info(f"Disconnected from {_atv_name}")
        _atv = None
        _atv_name = None


async def is_on(atv_name=None, atv_ip=None):
    """
    Check if Apple TV is currently on (not in standby).

    Returns:
        True if Apple TV is on, False if off/standby or unreachable
    """
    atv = await connect(name=atv_name, ip=atv_ip)
    if not atv:
        return False

    try:
        power_state = await atv.power.power_state
        # PowerState.On = device is awake, PowerState.Off = standby
        is_awake = str(power_state) == "PowerState.On"
        logger.info(f"Apple TV power state: {power_state} (on={is_awake})")
        return is_awake
    except Exception as e:
        logger.warning(f"Could not get Apple TV power state: {e}")
        return False


async def turn_on_async(atv_name=None, atv_ip=None):
    """
    Turn on Apple TV (wake from standby).

    Returns:
        True if successful, False otherwise
    """
    atv = await connect(name=atv_name, ip=atv_ip)
    if not atv:
        return False

    try:
        await atv.power.turn_on()
        logger.info("Apple TV turn on command sent")
        return True
    except Exception as e:
        logger.error(f"Failed to turn on Apple TV: {e}")
        return False


def is_on_sync():
    """Synchronous wrapper for is_on()."""
    return asyncio.run(is_on())


def turn_on():
    """Synchronous wrapper for turn_on_async()."""
    return asyncio.run(turn_on_async())


async def launch_app(url_or_bundle_id, atv_name=None, atv_ip=None):
    """
    Launch an app or deep link on Apple TV.

    Args:
        url_or_bundle_id: Either a deep link URL or app bundle ID
            - Disney+: "https://www.disneyplus.com/video/{content_id}"
            - Netflix: "https://www.netflix.com/title/{id}"
            - YouTube: "https://www.youtube.com/watch?v={id}"
            - Bundle ID: "com.disney.disneyplus"
        atv_name: Name of Apple TV to connect to (optional)
        atv_ip: IP address of Apple TV (optional)

    Returns:
        True if successful, False otherwise
    """
    atv = await connect(name=atv_name, ip=atv_ip)

    if not atv:
        return False

    try:
        logger.info(f"Launching: {url_or_bundle_id}")
        await atv.apps.launch_app(url_or_bundle_id)
        logger.info("Launch successful")
        return True
    except Exception as e:
        logger.error(f"Failed to launch app: {e}")
        return False


async def get_app_list():
    """Get list of installed apps on Apple TV."""
    atv = await connect()
    if not atv:
        return []

    try:
        apps = await atv.apps.app_list()
        return apps
    except Exception as e:
        logger.error(f"Failed to get app list: {e}")
        return []


def launch_disney(content_id, atv_name=None):
    """
    Launch Disney+ content on Apple TV.

    Args:
        content_id: The Disney+ content identifier (from the URL)
            Example: For https://www.disneyplus.com/movies/frozen/4uKGzAJi3ROz
            The content_id is "4uKGzAJi3ROz"
        atv_name: Name of Apple TV (optional)

    Returns:
        True if successful, False otherwise
    """
    # Construct the deep link URL
    if content_id.startswith("http"):
        url = content_id  # Already a full URL
    else:
        url = f"https://www.disneyplus.com/video/{content_id}"

    return asyncio.run(launch_app(url, atv_name=atv_name))


def launch_netflix(content_id, atv_name=None):
    """Launch Netflix content on Apple TV."""
    if content_id.startswith("http"):
        url = content_id
    else:
        url = f"https://www.netflix.com/title/{content_id}"

    return asyncio.run(launch_app(url, atv_name=atv_name))


def launch_youtube(video_id, atv_name=None):
    """Launch YouTube video on Apple TV."""
    if video_id.startswith("http"):
        url = video_id
    else:
        url = f"https://www.youtube.com/watch?v={video_id}"

    return asyncio.run(launch_app(url, atv_name=atv_name))


def launch_url(url, atv_name=None):
    """Launch any deep link URL on Apple TV."""
    return asyncio.run(launch_app(url, atv_name=atv_name))


def list_apple_tvs():
    """List all Apple TVs on the network."""
    atvs = asyncio.run(scan_for_apple_tvs())
    return [(str(atv.address), atv.name) for atv in atvs]


# Configuration loading from tags.yml
_config_loaded = False


def reset_config():
    """Reset config loaded flag to allow reloading."""
    global _config_loaded
    _config_loaded = False


def load_config(tags):
    """Load Apple TV configuration from tags dict."""
    global APPLE_TV_NAME, APPLE_TV_IP, APPLE_TV_ID
    global COMPANION_CREDENTIALS, AIRPLAY_CREDENTIALS
    global _config_loaded

    if _config_loaded:
        return

    if 'appletv_name' in tags:
        APPLE_TV_NAME = tags['appletv_name']
        logger.info(f"Apple TV name from config: {APPLE_TV_NAME}")

    if 'appletv_ip' in tags:
        APPLE_TV_IP = tags['appletv_ip']
        logger.info(f"Apple TV IP from config: {APPLE_TV_IP}")

    if 'appletv_id' in tags:
        APPLE_TV_ID = tags['appletv_id']
        logger.info(f"Apple TV ID from config: {APPLE_TV_ID}")

    if 'appletv_companion_credentials' in tags:
        COMPANION_CREDENTIALS = tags['appletv_companion_credentials']
        logger.info("Loaded Companion credentials from config")

    if 'appletv_airplay_credentials' in tags:
        AIRPLAY_CREDENTIALS = tags['appletv_airplay_credentials']
        logger.info("Loaded AirPlay credentials from config")

    _config_loaded = True


def activated():
    """Check if Apple TV integration is available."""
    # pyatv is imported at top - if unavailable, module wouldn't load
    # Return True since Apple TV can be auto-discovered on network
    return True


# Test function
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("Scanning for Apple TVs...")
    devices = list_apple_tvs()

    if devices:
        print("\nFound Apple TVs:")
        for ip, name in devices:
            print(f"  - {name} ({ip})")
    else:
        print("No Apple TVs found")
