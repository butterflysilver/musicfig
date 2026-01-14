#!/usr/bin/env python
"""HomePod AirPlay streaming module for Magic Box.

Streams MP3 files to HomePod Mini speakers via AirPlay using pyatv.
"""

import asyncio
import logging
import os
import threading
from typing import Optional

import pyatv

logger = logging.getLogger(__name__)

# Global state
_config = {}
_cached_devices = {}
_cache_lock = threading.Lock()


def load_config(tags: dict) -> None:
    """Load HomePod configuration from tags.yml."""
    global _config
    _config = {
        'homepods': tags.get('homepods', {}),
        'default_homepod': tags.get('default_homepod', ''),
        'mp3_dir': tags.get('mp3_dir', ''),
    }
    logger.info(f"HomePod config loaded: {len(_config['homepods'])} targets configured")


def configured() -> bool:
    """Check if HomePod streaming is configured."""
    return bool(_config.get('homepods') or _config.get('default_homepod'))


async def _scan_devices() -> dict:
    """Scan for AirPlay devices and cache results."""
    global _cached_devices

    with _cache_lock:
        if _cached_devices:
            return _cached_devices

    logger.info("Scanning for AirPlay devices...")
    devices = await pyatv.scan(asyncio.get_event_loop(), timeout=5)

    device_map = {}
    for device in devices:
        device_map[device.name] = device
        device_map[str(device.address)] = device
        logger.debug(f"Found: {device.name} @ {device.address}")

    with _cache_lock:
        _cached_devices = device_map

    return device_map


async def _get_device(target: str) -> Optional[pyatv.interface.AppleTV]:
    """Get a pyatv device by name or IP address."""
    devices = await _scan_devices()

    if target in devices:
        return devices[target]

    # Try partial match
    for name, device in devices.items():
        if target.lower() in name.lower():
            return device

    logger.warning(f"HomePod '{target}' not found")
    return None


async def _stream_file_async(file_path: str, target: str) -> bool:
    """Stream an audio file to a HomePod (async implementation)."""
    if not os.path.exists(file_path):
        logger.error(f"Audio file not found: {file_path}")
        return False

    device_config = await _get_device(target)
    if not device_config:
        return False

    logger.info(f"Connecting to {device_config.name}...")

    try:
        atv = await pyatv.connect(device_config, asyncio.get_event_loop())

        try:
            logger.info(f"Streaming {os.path.basename(file_path)} to {device_config.name}")
            await atv.stream.stream_file(file_path)
            logger.info("Streaming started successfully")
            return True
        finally:
            atv.close()

    except Exception as e:
        logger.error(f"Failed to stream to HomePod: {e}")
        return False


def stream_file(file_path: str, target: str = None) -> bool:
    """
    Stream an audio file to a HomePod.

    Args:
        file_path: Path to the MP3 file (absolute or relative to mp3_dir)
        target: HomePod name or IP (uses default if not specified)

    Returns:
        True if streaming started successfully
    """
    # Resolve file path
    if not os.path.isabs(file_path):
        mp3_dir = _config.get('mp3_dir', '')
        if mp3_dir:
            file_path = os.path.join(mp3_dir, file_path)

    # Resolve target
    if target is None:
        target = _config.get('default_homepod', '')

    if not target:
        logger.error("No HomePod target specified and no default configured")
        return False

    # Run async function in new event loop (called from sync context)
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_stream_file_async(file_path, target))
        loop.close()
        return result
    except Exception as e:
        logger.error(f"HomePod streaming error: {e}")
        return False


def stream_playlist(playlist_dir: str, target: str = None, shuffle: bool = False) -> bool:
    """
    Stream a playlist (folder of MP3s) to a HomePod.

    Args:
        playlist_dir: Directory containing MP3 files
        target: HomePod name or IP
        shuffle: Whether to shuffle the playlist

    Returns:
        True if streaming started
    """
    import glob
    import random

    mp3_dir = _config.get('mp3_dir', '')
    full_path = os.path.join(mp3_dir, playlist_dir) if mp3_dir else playlist_dir

    mp3_files = glob.glob(os.path.join(full_path, '*.mp3'))

    if not mp3_files:
        logger.warning(f"No MP3 files found in {playlist_dir}")
        return False

    if shuffle:
        random.shuffle(mp3_files)

    # Stream first file (playlist continuation would need more work)
    # For now, just play the first track
    logger.info(f"Playing playlist: {len(mp3_files)} tracks")
    return stream_file(mp3_files[0], target)


def clear_cache() -> None:
    """Clear the device cache (call if network changes)."""
    global _cached_devices
    with _cache_lock:
        _cached_devices = {}
    logger.info("HomePod device cache cleared")


async def list_devices() -> list:
    """List all available AirPlay devices."""
    devices = await _scan_devices()
    return list(set(d.name for d in devices.values()))
