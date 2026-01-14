#!/usr/bin/env python
"""
Philips Hue Sync Box integration for Musicfig.
Enables HDMI input switching before launching content on Apple TV.
"""

import asyncio
import logging
from aiohuesyncbox import HueSyncBox, InvalidState

logger = logging.getLogger(__name__)

# Cache for Hue Sync Box connection
_box = None
_box_id = None

# Configuration - set these in tags.yml
SYNCBOX_HOST = None      # IP address of Hue Sync Box
SYNCBOX_ID = None        # Device ID (looks like "C43212345678")
SYNCBOX_TOKEN = None     # Access token from registration
SYNCBOX_APPLETV_INPUT = None  # Which input Apple TV is on (input1, input2, input3, input4)

# Configuration loaded flag
_config_loaded = False


async def discover_syncboxes(timeout=5):
    """
    Discover Hue Sync Boxes on the network using zeroconf.
    Returns list of (host, id) tuples.
    """
    try:
        from zeroconf import ServiceBrowser, Zeroconf
        import socket

        discovered = []

        class SyncBoxListener:
            def add_service(self, zc, type_, name):
                info = zc.get_service_info(type_, name)
                if info:
                    host = socket.inet_ntoa(info.addresses[0])
                    # ID is in the service name
                    box_id = name.split('.')[0].replace('HueSyncBox-', '')
                    discovered.append((host, box_id))
                    logger.info(f"Discovered Hue Sync Box: {host} (ID: {box_id})")

            def remove_service(self, zc, type_, name):
                pass

            def update_service(self, zc, type_, name):
                pass

        zc = Zeroconf()
        listener = SyncBoxListener()
        browser = ServiceBrowser(zc, "_huesync._tcp.local.", listener)

        await asyncio.sleep(timeout)
        zc.close()

        return discovered
    except ImportError:
        logger.warning("zeroconf not installed - cannot discover Sync Boxes")
        logger.info("Install with: pip install zeroconf")
        return []
    except Exception as e:
        logger.error(f"Discovery failed: {e}")
        return []


async def connect():
    """Connect to the Hue Sync Box using stored credentials."""
    global _box, _box_id

    if not SYNCBOX_HOST or not SYNCBOX_ID:
        logger.error("Hue Sync Box not configured (need host and id)")
        return None

    if not SYNCBOX_TOKEN:
        logger.error("Hue Sync Box not registered (need access token)")
        logger.info("Run register_syncbox() first to pair with the device")
        return None

    # Return cached connection if valid
    if _box is not None and _box_id == SYNCBOX_ID:
        try:
            await _box.execution.update()
            return _box
        except Exception:
            logger.info("Cached Sync Box connection lost, reconnecting...")
            await disconnect()

    try:
        logger.info(f"Connecting to Hue Sync Box at {SYNCBOX_HOST}...")
        _box = HueSyncBox(SYNCBOX_HOST, SYNCBOX_ID, SYNCBOX_TOKEN)
        await _box.initialize()
        _box_id = SYNCBOX_ID
        logger.info(f"Connected to Hue Sync Box: {_box.device.name}")
        return _box
    except Exception as e:
        logger.error(f"Failed to connect to Hue Sync Box: {e}")
        return None


async def disconnect():
    """Disconnect from Hue Sync Box."""
    global _box, _box_id
    if _box:
        await _box.close()
        logger.info("Disconnected from Hue Sync Box")
        _box = None
        _box_id = None


async def register_syncbox(host, box_id, app_name="Musicfig", device_name="MagicBox"):
    """
    Register with a Hue Sync Box to get an access token.

    User must press the button on the Sync Box within 30 seconds.

    Args:
        host: IP address of the Sync Box
        box_id: Device ID (from discovery or Hue app)
        app_name: Name of your application
        device_name: Name of your device

    Returns:
        dict with 'access_token' and 'registration_id', or None on failure
    """
    box = HueSyncBox(host, box_id)

    print(f"\n{'='*50}")
    print("PRESS THE BUTTON ON YOUR HUE SYNC BOX NOW!")
    print(f"{'='*50}\n")

    registration_info = None
    attempts = 0
    max_attempts = 30  # 30 seconds

    while not registration_info and attempts < max_attempts:
        try:
            registration_info = await box.register(app_name, device_name)
            logger.info("Registration successful!")
        except InvalidState:
            attempts += 1
            if attempts % 5 == 0:
                print(f"Waiting for button press... ({max_attempts - attempts}s remaining)")
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Registration error: {e}")
            await box.close()
            return None

    await box.close()

    if registration_info:
        print(f"\nRegistration successful!")
        print(f"Access Token: {registration_info['access_token']}")
        print(f"Registration ID: {registration_info['registration_id']}")
        print(f"\nAdd this to your tags.yml:")
        print(f'syncbox_token: "{registration_info["access_token"]}"')
        return registration_info
    else:
        print("\nRegistration timed out. Please try again.")
        return None


async def switch_to_appletv():
    """
    Switch Hue Sync Box to Apple TV input and enable video sync.

    Returns:
        True if successful, False otherwise
    """
    if not SYNCBOX_APPLETV_INPUT:
        logger.warning("Apple TV input not configured (syncbox_appletv_input)")
        return False

    box = await connect()
    if not box:
        return False

    try:
        logger.info(f"Switching to Apple TV input: {SYNCBOX_APPLETV_INPUT}")
        await box.execution.set_state(
            sync_active=True,
            mode="video",
            hdmi_source=SYNCBOX_APPLETV_INPUT
        )
        logger.info("HDMI switch successful, sync enabled")
        return True
    except Exception as e:
        logger.error(f"Failed to switch HDMI input: {e}")
        return False


async def switch_input(hdmi_input, sync_active=True, mode="video"):
    """
    Switch to a specific HDMI input.

    Args:
        hdmi_input: "input1", "input2", "input3", or "input4"
        sync_active: Whether to enable light sync
        mode: "video", "music", "game", or "passthrough"

    Returns:
        True if successful, False otherwise
    """
    box = await connect()
    if not box:
        return False

    try:
        logger.info(f"Switching to {hdmi_input} (sync={sync_active}, mode={mode})")
        await box.execution.set_state(
            sync_active=sync_active,
            mode=mode,
            hdmi_source=hdmi_input
        )
        return True
    except Exception as e:
        logger.error(f"Failed to switch input: {e}")
        return False


async def get_status():
    """Get current Hue Sync Box status."""
    box = await connect()
    if not box:
        return None

    try:
        await box.execution.update()
        return {
            "name": box.device.name,
            "sync_active": box.execution.sync_active,
            "mode": box.execution.mode,
            "hdmi_source": box.execution.hdmi_source,
            "brightness": box.execution.brightness,
            "intensity": box.execution.intensity,
        }
    except Exception as e:
        logger.error(f"Failed to get status: {e}")
        return None


def switch_to_appletv_sync():
    """Synchronous wrapper for switch_to_appletv()."""
    return asyncio.run(switch_to_appletv())


def switch_input_sync(hdmi_input, sync_active=True, mode="video"):
    """Synchronous wrapper for switch_input()."""
    return asyncio.run(switch_input(hdmi_input, sync_active, mode))


# Configuration loading from tags.yml
def reset_config():
    """Reset config loaded flag to allow reloading."""
    global _config_loaded
    _config_loaded = False


def load_config(tags):
    """Load Hue Sync Box configuration from tags dict."""
    global SYNCBOX_HOST, SYNCBOX_ID, SYNCBOX_TOKEN, SYNCBOX_APPLETV_INPUT
    global _config_loaded

    if _config_loaded:
        return

    if 'syncbox_host' in tags:
        SYNCBOX_HOST = tags['syncbox_host']
        logger.info(f"Sync Box host from config: {SYNCBOX_HOST}")

    if 'syncbox_id' in tags:
        SYNCBOX_ID = tags['syncbox_id']
        logger.info(f"Sync Box ID from config: {SYNCBOX_ID}")

    if 'syncbox_token' in tags:
        SYNCBOX_TOKEN = tags['syncbox_token']
        logger.info("Loaded Sync Box access token from config")

    if 'syncbox_appletv_input' in tags:
        SYNCBOX_APPLETV_INPUT = tags['syncbox_appletv_input']
        logger.info(f"Apple TV input from config: {SYNCBOX_APPLETV_INPUT}")

    _config_loaded = True


def activated():
    """Check if Hue Sync Box integration is configured and available."""
    # aiohuesyncbox is imported at top - if unavailable, module wouldn't load
    # Check if we have the minimum config to attempt switching
    return bool(SYNCBOX_HOST or SYNCBOX_ID)


def configured():
    """Check if Hue Sync Box is fully configured."""
    return bool(SYNCBOX_HOST and SYNCBOX_ID and SYNCBOX_TOKEN and SYNCBOX_APPLETV_INPUT)


# CLI for testing and registration
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    async def main():
        global SYNCBOX_HOST, SYNCBOX_ID, SYNCBOX_TOKEN

        if len(sys.argv) > 1:
            command = sys.argv[1]

            if command == "discover":
                print("Scanning for Hue Sync Boxes...")
                boxes = await discover_syncboxes()
                if boxes:
                    print("\nFound Hue Sync Boxes:")
                    for host, box_id in boxes:
                        print(f"  - {host} (ID: {box_id})")
                else:
                    print("No Hue Sync Boxes found")
                    print("Make sure your Sync Box is on the same network")

            elif command == "register":
                if len(sys.argv) < 4:
                    print("Usage: python huesyncbox.py register <host> <id>")
                    print("Example: python huesyncbox.py register 192.168.0.100 C43212345678")
                    return
                host = sys.argv[2]
                box_id = sys.argv[3]
                await register_syncbox(host, box_id)

            elif command == "status":
                if len(sys.argv) < 5:
                    print("Usage: python huesyncbox.py status <host> <id> <token>")
                    return
                SYNCBOX_HOST = sys.argv[2]
                SYNCBOX_ID = sys.argv[3]
                SYNCBOX_TOKEN = sys.argv[4]
                status = await get_status()
                if status:
                    print(f"\nHue Sync Box Status:")
                    for key, value in status.items():
                        print(f"  {key}: {value}")

            elif command == "switch":
                if len(sys.argv) < 6:
                    print("Usage: python huesyncbox.py switch <host> <id> <token> <input>")
                    print("Inputs: input1, input2, input3, input4")
                    return
                SYNCBOX_HOST = sys.argv[2]
                SYNCBOX_ID = sys.argv[3]
                SYNCBOX_TOKEN = sys.argv[4]
                hdmi_input = sys.argv[5]
                if await switch_input(hdmi_input):
                    print(f"Switched to {hdmi_input}")
                else:
                    print("Switch failed")

            else:
                print(f"Unknown command: {command}")
                print("Commands: discover, register, status, switch")
        else:
            print("Hue Sync Box CLI")
            print("Commands:")
            print("  discover              - Find Sync Boxes on network")
            print("  register <host> <id>  - Register with a Sync Box")
            print("  status <host> <id> <token> - Get current status")
            print("  switch <host> <id> <token> <input> - Switch HDMI input")

    asyncio.run(main())
