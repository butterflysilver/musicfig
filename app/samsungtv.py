#!/usr/bin/env python
"""
Samsung SmartThings TV integration for Musicfig.
Enables HDMI input switching on Samsung TVs via SmartThings API.
"""

import logging
import requests

logger = logging.getLogger(__name__)

# SmartThings API configuration
API_BASE = "https://api.smartthings.com/v1"

# Configuration - set these in tags.yml
SMARTTHINGS_TOKEN = None      # Personal Access Token from account.smartthings.com/tokens
SMARTTHINGS_TV_DEVICE_ID = None  # Device ID of the Samsung TV
SMARTTHINGS_APPLETV_INPUT = None  # Which input is Apple TV? (HDMI1, HDMI2, HDMI3, HDMI4)

# Configuration loaded flag
_config_loaded = False


def _get_headers():
    """Get API request headers with auth token."""
    return {
        "Authorization": f"Bearer {SMARTTHINGS_TOKEN}",
        "Content-Type": "application/json"
    }


def list_devices():
    """
    List all devices in SmartThings account.
    Useful for finding your TV's device ID.

    Returns:
        list of device dicts, or empty list on failure
    """
    if not SMARTTHINGS_TOKEN:
        logger.error("SmartThings token not configured")
        return []

    try:
        response = requests.get(
            f"{API_BASE}/devices",
            headers=_get_headers(),
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        return data.get("items", [])
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to list devices: {e}")
        return []


def list_tvs():
    """
    List only Samsung TV devices.

    Returns:
        list of (device_id, name, label) tuples
    """
    devices = list_devices()
    tvs = []

    for device in devices:
        # Check if it's a TV by looking at capabilities or device type
        device_type = device.get("deviceTypeName", "").lower()
        name = device.get("name", "Unknown")
        label = device.get("label", name)
        device_id = device.get("deviceId", "")

        # Samsung TVs often have "samsung" in the type or have TV capabilities
        capabilities = []
        for component in device.get("components", []):
            capabilities.extend([c.get("id", "") for c in component.get("capabilities", [])])

        if ("tv" in device_type.lower() or
            "samsung" in device_type.lower() or
            "mediaInputSource" in capabilities or
            "samsungvd.mediaInputSource" in capabilities):
            tvs.append((device_id, name, label))
            logger.info(f"Found TV: {label} (ID: {device_id})")

    return tvs


def is_tv_on(device_id=None):
    """
    Check if the TV is currently on.

    Args:
        device_id: Device ID (uses configured TV if not specified)

    Returns:
        True if TV is on, False if off or unknown
    """
    status = get_device_status(device_id)
    if not status:
        return False

    try:
        # Check the switch capability for on/off state
        main = status.get("components", {}).get("main", {})
        switch_state = main.get("switch", {}).get("switch", {}).get("value")
        if switch_state:
            return switch_state.lower() == "on"

        # Fallback: check if we can get any meaningful data (TV responds when on)
        return bool(main)
    except Exception as e:
        logger.warning(f"Could not determine TV power state: {e}")
        return False


def get_device_status(device_id=None):
    """
    Get current status of a device.

    Args:
        device_id: Device ID (uses configured TV if not specified)

    Returns:
        dict with device status, or None on failure
    """
    device_id = device_id or SMARTTHINGS_TV_DEVICE_ID

    if not SMARTTHINGS_TOKEN:
        logger.error("SmartThings token not configured")
        return None

    if not device_id:
        logger.error("No device ID specified")
        return None

    try:
        response = requests.get(
            f"{API_BASE}/devices/{device_id}/status",
            headers=_get_headers(),
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to get device status: {e}")
        return None


def get_current_input(device_id=None):
    """
    Get the current input source of the TV.

    Args:
        device_id: Device ID (uses configured TV if not specified)

    Returns:
        Current input source string (e.g., "HDMI3", "digitalTv") or None on failure
    """
    device_id = device_id or SMARTTHINGS_TV_DEVICE_ID

    if not SMARTTHINGS_TOKEN or not device_id:
        return None

    try:
        response = requests.get(
            f"{API_BASE}/devices/{device_id}/status",
            headers=_get_headers(),
            timeout=10
        )
        response.raise_for_status()
        status = response.json()

        # Try to find current input in the status
        # It's usually under main component, mediaInputSource capability
        main = status.get("components", {}).get("main", {})

        # Try standard capability first
        input_source = main.get("mediaInputSource", {}).get("inputSource", {}).get("value")
        if input_source:
            return input_source

        # Try Samsung-specific capability
        input_source = main.get("samsungvd.mediaInputSource", {}).get("inputSource", {}).get("value")
        if input_source:
            return input_source

        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to get current input: {e}")
        return None


def set_input_source(input_source, device_id=None):
    """
    Set the TV input source (HDMI1, HDMI2, etc.).

    Args:
        input_source: The input to switch to (HDMI1, HDMI2, HDMI3, HDMI4, etc.)
        device_id: Device ID (uses configured TV if not specified)

    Returns:
        True if successful, False otherwise
    """
    device_id = device_id or SMARTTHINGS_TV_DEVICE_ID

    if not SMARTTHINGS_TOKEN:
        logger.error("SmartThings token not configured")
        return False

    if not device_id:
        logger.error("No device ID specified")
        return False

    # Try standard capability first, then Samsung-specific
    capabilities_to_try = [
        "mediaInputSource",
        "samsungvd.mediaInputSource"
    ]

    for capability in capabilities_to_try:
        payload = {
            "commands": [{
                "component": "main",
                "capability": capability,
                "command": "setInputSource",
                "arguments": [input_source]
            }]
        }

        try:
            logger.info(f"Setting input to {input_source} using {capability}...")
            response = requests.post(
                f"{API_BASE}/devices/{device_id}/commands",
                headers=_get_headers(),
                json=payload,
                timeout=10
            )

            if response.status_code == 200:
                logger.info(f"Successfully switched to {input_source}")
                return True
            elif response.status_code == 422:
                # Try next capability
                logger.debug(f"Capability {capability} not supported, trying next...")
                continue
            else:
                logger.warning(f"Command returned status {response.status_code}: {response.text}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to set input source: {e}")

    logger.error(f"Failed to switch to {input_source}")
    return False


def send_key(key, device_id=None):
    """
    Send a remote control key press to the TV.

    Args:
        key: Key code. Valid values: UP, DOWN, LEFT, RIGHT, OK, BACK, EXIT,
             MENU, HOME, MUTE, PLAY, PAUSE, STOP, REWIND, FF, PLAY_BACK, SOURCE
        device_id: Device ID (uses configured TV if not specified)

    Returns:
        True if successful, False otherwise
    """
    device_id = device_id or SMARTTHINGS_TV_DEVICE_ID

    if not SMARTTHINGS_TOKEN or not device_id:
        return False

    # Try Samsung-specific remote control capability
    payload = {
        "commands": [{
            "component": "main",
            "capability": "samsungvd.remoteControl",
            "command": "send",
            "arguments": [key]
        }]
    }

    try:
        logger.info(f"Sending key: {key}")
        response = requests.post(
            f"{API_BASE}/devices/{device_id}/commands",
            headers=_get_headers(),
            json=payload,
            timeout=10
        )

        if response.status_code == 200:
            logger.info(f"Key {key} sent successfully")
            return True
        else:
            logger.warning(f"Send key returned status {response.status_code}: {response.text}")
            return False

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send key: {e}")
        return False


def switch_to_appletv(auto_turn_on=True):
    """
    Switch Samsung TV to Apple TV input and confirm selection.
    Optionally turns on both devices if they're off.

    Args:
        auto_turn_on: If True, turn on TV and Apple TV if they're off

    Returns:
        True if successful, False otherwise
    """
    import time
    import app.appletv as appletv

    if not SMARTTHINGS_APPLETV_INPUT:
        logger.warning("Apple TV input not configured (smartthings_appletv_input)")
        return False

    # Check and turn on devices if needed
    if auto_turn_on:
        # Check Samsung TV
        if not is_tv_on():
            logger.info("Samsung TV is off, turning on...")
            if turn_on():
                # Wait for TV to boot up
                time.sleep(5)
            else:
                logger.error("Failed to turn on Samsung TV")
                return False

        # Check Apple TV
        if not appletv.is_on_sync():
            logger.info("Apple TV is off/standby, waking up...")
            if appletv.turn_on():
                # Wait for Apple TV to wake
                time.sleep(3)
                # Dismiss any wake screen with OK
                send_key("OK")
                time.sleep(0.5)
            else:
                logger.warning("Could not wake Apple TV, continuing anyway...")

    # Always send switch command - even if already on HDMI3, Samsung's UI
    # might be overlaying it (home screen, TV Plus, etc.)
    # The SmartThings API reports the HDMI connection, not what's displayed
    logger.info(f"Switching Samsung TV to Apple TV input: {SMARTTHINGS_APPLETV_INPUT}")
    if set_input_source(SMARTTHINGS_APPLETV_INPUT):
        # Wait for input switch and any overlay to appear
        time.sleep(0.8)

        # EXIT closes Samsung overlays and goes full screen to the input
        send_key("EXIT")
        return True

    return False


def turn_on(device_id=None):
    """
    Turn on the TV (may not work on all models via WiFi).

    Args:
        device_id: Device ID (uses configured TV if not specified)

    Returns:
        True if successful, False otherwise
    """
    device_id = device_id or SMARTTHINGS_TV_DEVICE_ID

    if not SMARTTHINGS_TOKEN or not device_id:
        return False

    payload = {
        "commands": [{
            "component": "main",
            "capability": "switch",
            "command": "on"
        }]
    }

    try:
        response = requests.post(
            f"{API_BASE}/devices/{device_id}/commands",
            headers=_get_headers(),
            json=payload,
            timeout=10
        )
        return response.status_code == 200
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to turn on TV: {e}")
        return False


def turn_off(device_id=None):
    """
    Turn off the TV.

    Args:
        device_id: Device ID (uses configured TV if not specified)

    Returns:
        True if successful, False otherwise
    """
    device_id = device_id or SMARTTHINGS_TV_DEVICE_ID

    if not SMARTTHINGS_TOKEN or not device_id:
        return False

    payload = {
        "commands": [{
            "component": "main",
            "capability": "switch",
            "command": "off"
        }]
    }

    try:
        response = requests.post(
            f"{API_BASE}/devices/{device_id}/commands",
            headers=_get_headers(),
            json=payload,
            timeout=10
        )
        return response.status_code == 200
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to turn off TV: {e}")
        return False


# Configuration loading from tags.yml
def reset_config():
    """Reset config loaded flag to allow reloading."""
    global _config_loaded
    _config_loaded = False


def load_config(tags):
    """Load Samsung TV configuration from tags dict."""
    global SMARTTHINGS_TOKEN, SMARTTHINGS_TV_DEVICE_ID, SMARTTHINGS_APPLETV_INPUT
    global _config_loaded

    if _config_loaded:
        return

    if 'smartthings_token' in tags:
        SMARTTHINGS_TOKEN = tags['smartthings_token']
        logger.info("Loaded SmartThings token from config")

    if 'smartthings_tv_device_id' in tags:
        SMARTTHINGS_TV_DEVICE_ID = tags['smartthings_tv_device_id']
        logger.info(f"SmartThings TV device ID from config: {SMARTTHINGS_TV_DEVICE_ID}")

    if 'smartthings_appletv_input' in tags:
        SMARTTHINGS_APPLETV_INPUT = tags['smartthings_appletv_input']
        logger.info(f"Apple TV input from config: {SMARTTHINGS_APPLETV_INPUT}")

    _config_loaded = True


def activated():
    """Check if Samsung SmartThings integration is configured and available."""
    # requests is always available (imported at top)
    # Check if we have the minimum config to attempt switching
    return bool(SMARTTHINGS_TOKEN or SMARTTHINGS_TV_DEVICE_ID)


def configured():
    """Check if Samsung TV is fully configured."""
    return bool(SMARTTHINGS_TOKEN and SMARTTHINGS_TV_DEVICE_ID and SMARTTHINGS_APPLETV_INPUT)


# CLI for testing and setup
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == "list":
            if len(sys.argv) < 3:
                print("Usage: python samsungtv.py list <token>")
                print("\nGet your token from: https://account.smartthings.com/tokens")
                sys.exit(1)

            SMARTTHINGS_TOKEN = sys.argv[2]
            print("Listing all devices...")
            devices = list_devices()

            if devices:
                print(f"\nFound {len(devices)} devices:\n")
                for device in devices:
                    device_id = device.get("deviceId", "")
                    name = device.get("name", "Unknown")
                    label = device.get("label", name)
                    device_type = device.get("deviceTypeName", "Unknown")
                    print(f"  {label}")
                    print(f"    ID: {device_id}")
                    print(f"    Type: {device_type}")
                    print()
            else:
                print("No devices found")

        elif command == "tvs":
            if len(sys.argv) < 3:
                print("Usage: python samsungtv.py tvs <token>")
                sys.exit(1)

            SMARTTHINGS_TOKEN = sys.argv[2]
            print("Finding Samsung TVs...")
            tvs = list_tvs()

            if tvs:
                print(f"\nFound {len(tvs)} TV(s):\n")
                for device_id, name, label in tvs:
                    print(f"  {label}")
                    print(f"    Device ID: {device_id}")
                    print(f"\n  Add to tags.yml:")
                    print(f'    smartthings_tv_device_id: "{device_id}"')
                    print()
            else:
                print("No TVs found")

        elif command == "status":
            if len(sys.argv) < 4:
                print("Usage: python samsungtv.py status <token> <device_id>")
                sys.exit(1)

            SMARTTHINGS_TOKEN = sys.argv[2]
            SMARTTHINGS_TV_DEVICE_ID = sys.argv[3]

            print(f"Getting status for device {SMARTTHINGS_TV_DEVICE_ID}...")
            status = get_device_status()

            if status:
                import json
                print(json.dumps(status, indent=2))
            else:
                print("Failed to get status")

        elif command == "switch":
            if len(sys.argv) < 5:
                print("Usage: python samsungtv.py switch <token> <device_id> <input>")
                print("Inputs: HDMI1, HDMI2, HDMI3, HDMI4")
                sys.exit(1)

            SMARTTHINGS_TOKEN = sys.argv[2]
            SMARTTHINGS_TV_DEVICE_ID = sys.argv[3]
            input_source = sys.argv[4]

            print(f"Switching to {input_source}...")
            if set_input_source(input_source):
                print("Success!")
            else:
                print("Failed to switch input")

        else:
            print(f"Unknown command: {command}")
            print("Commands: list, tvs, status, switch")
    else:
        print("Samsung SmartThings TV CLI")
        print()
        print("Setup:")
        print("  1. Get API token from: https://account.smartthings.com/tokens")
        print("  2. Run: python samsungtv.py tvs <token>")
        print("  3. Copy the device ID to tags.yml")
        print()
        print("Commands:")
        print("  list <token>                    - List all SmartThings devices")
        print("  tvs <token>                     - List Samsung TVs only")
        print("  status <token> <device_id>      - Get TV status")
        print("  switch <token> <device_id> <input> - Switch HDMI input")
