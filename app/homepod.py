#!/usr/bin/env python
"""HomePod AirPlay streaming module for Magic Box.

Streams MP3 files to HomePod Mini speakers via AirPlay (RAOP) using pyatv.

All pyatv work runs on one long-lived asyncio loop in a daemon thread, so
stream_file() returns as soon as audio is flowing and the pad keeps reading
tags while a track plays. stop() ends the current stream (tag lifted, or a
new tag landed) and closes the connection cleanly.
"""

import asyncio
import logging
import os
import shutil
import threading
from typing import Optional

# Optional dependency (requirements-extras.txt); see appletv.py.
try:
    import pyatv
except ImportError:  # pragma: no cover - depends on the install
    pyatv = None

logger = logging.getLogger(__name__)

# How long stream_file() waits for the HomePod to start playing before
# reporting failure (scan up to 5 s + connect + stream set-up).
START_TIMEOUT = 20.0

# Global state
_config = {}
_cached_devices = {}
_cache_lock = threading.Lock()

# Background loop + the one stream that may be running on it.
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_loop_lock = threading.Lock()
_current = None  # concurrent.futures.Future of the running stream
_current_lock = threading.Lock()


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


def _get_loop() -> asyncio.AbstractEventLoop:
    """The shared background loop, started on first use."""
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(
                target=_loop.run_forever, name='homepod-airplay', daemon=True)
            _loop_thread.start()
        return _loop


async def _scan_devices() -> dict:
    """Scan for AirPlay devices and cache results."""
    global _cached_devices

    with _cache_lock:
        if _cached_devices:
            return _cached_devices

    logger.info("Scanning for AirPlay devices...")
    devices = await pyatv.scan(asyncio.get_running_loop(), timeout=5)

    device_map = {}
    for device in devices:
        device_map[device.name] = device
        device_map[str(device.address)] = device
        logger.debug(f"Found: {device.name} @ {device.address}")

    with _cache_lock:
        _cached_devices = device_map

    return device_map


async def _get_device(target: str):
    """Get a pyatv device config by name or IP address."""
    devices = await _scan_devices()

    if target in devices:
        return devices[target]

    # Try partial match
    for name, device in devices.items():
        if target.lower() in name.lower():
            return device

    logger.warning(f"HomePod '{target}' not found")
    return None


def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


# pyatv decodes MP3/WAV/FLAC/OGG only (miniaudio); Yoto serves AAC in an MP4
# container, which plays as silence. Remote sources are therefore transcoded
# to MP3 through ffmpeg and piped into pyatv. MUSICFIG_TRANSCODE=no disables it.
FFMPEG = os.environ.get('MUSICFIG_FFMPEG') or 'ffmpeg'


def _transcode_enabled() -> bool:
    return os.environ.get('MUSICFIG_TRANSCODE', 'yes').lower() not in ('no', '0', 'false')


def ffmpeg_command(url: str) -> list:
    """ffmpeg argv that turns any remote track into an MP3 stream on stdout."""
    return [FFMPEG, '-nostdin', '-loglevel', 'error', '-i', url, '-vn',
            '-f', 'mp3', '-b:a', '192k', '-']


async def _play_source(atv, source: str) -> None:
    """Stream one source: local files and MP3 URLs directly; other remote
    files through ffmpeg (killed on cancel or failure)."""
    if not (_is_url(source) and _transcode_enabled() and shutil.which(FFMPEG)):
        if _is_url(source) and _transcode_enabled():
            logger.warning("ffmpeg not found; streaming the URL as-is (AAC will be silent)")
        await atv.stream.stream_file(source)
        return
    proc = await asyncio.create_subprocess_exec(
        *ffmpeg_command(source), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        await atv.stream.stream_file(proc.stdout)
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


async def _stream_sources_async(sources: list, target: str, started: threading.Event,
                                ok: list) -> None:
    """Stream sources (local paths or HTTPS URLs) to a HomePod one after the
    other on a single connection. Sets `started` once the first one is
    flowing (ok[0] = True) or once it is clear it will not (ok[0] = False)."""
    atv = None
    try:
        usable = []
        for source in sources:
            if _is_url(source) or os.path.exists(source):
                usable.append(source)
            else:
                logger.error(f"Audio file not found: {source}")
        sources = usable
        if not sources:
            return

        device_config = await _get_device(target)
        if not device_config:
            return

        logger.info(f"Connecting to {device_config.name}...")
        atv = await pyatv.connect(device_config, asyncio.get_running_loop())
        failures = 0
        for index, source in enumerate(sources, start=1):
            label = source if _is_url(source) else os.path.basename(source)
            logger.info(f"Streaming {index}/{len(sources)} {label[:80]} to {device_config.name}")
            if index == 1:
                ok[0] = True
                started.set()
            try:
                await _play_source(atv, source)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # One bad track (expired signed URL, unsupported file) must not
                # end the whole card; three in a row means something is wrong.
                failures += 1
                logger.error(f"Track {index}/{len(sources)} failed: {e}")
                if failures >= 3:
                    logger.error("Three tracks failed in a row; giving up on this stream")
                    break
        logger.info(f"Finished streaming {len(sources)} source(s)")
    except asyncio.CancelledError:
        logger.info("HomePod stream stopped")
        raise
    except Exception as e:
        logger.error(f"Failed to stream to HomePod: {e}")
    finally:
        if atv is not None:
            try:
                atv.close()
            except Exception:
                pass
        started.set()


def stream_file(file_path: str, target: str = None) -> bool:
    """
    Start streaming an audio file to a HomePod and return once audio is
    flowing. The stream then runs in the background until it ends or stop()
    is called. Returns False (with a warning) when pyatv is not installed.

    Args:
        file_path: Path to the MP3 file (absolute or relative to mp3_dir)
        target: HomePod name or IP (uses default if not specified)

    Returns:
        True if streaming started
    """
    if pyatv is None:
        logger.warning("HomePod stream skipped: pyatv is not installed (pip install -r requirements-extras.txt)")
        return False
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

    return _start([file_path], target)


def stream_urls(urls: list, target: str = None) -> bool:
    """Stream HTTPS URLs (e.g. a Yoto card's signed tracks) to a HomePod in
    order, in the background. Returns True once the first one is playing."""
    if pyatv is None:
        logger.warning("HomePod stream skipped: pyatv is not installed (pip install -r requirements-extras.txt)")
        return False
    urls = [u for u in urls if isinstance(u, str) and _is_url(u)]
    if not urls:
        logger.error("No playable URLs to stream")
        return False
    if target is None:
        target = _config.get('default_homepod', '')
    if not target:
        logger.error("No HomePod target specified and no default configured")
        return False
    return _start(urls, target)


def _start(sources: list, target: str) -> bool:
    global _current
    stop()  # one stream at a time
    started = threading.Event()
    ok = [False]
    try:
        fut = asyncio.run_coroutine_threadsafe(
            _stream_sources_async(sources, target, started, ok), _get_loop())
    except Exception as e:
        logger.error(f"HomePod streaming error: {e}")
        return False
    with _current_lock:
        _current = fut
    if not started.wait(START_TIMEOUT):
        logger.error(f"HomePod did not start playing within {START_TIMEOUT:.0f}s")
        stop()
        return False
    return ok[0]


def stop() -> None:
    """Stop the current stream, if any. Safe to call at any time."""
    global _current
    with _current_lock:
        fut, _current = _current, None
    if fut is None or fut.done():
        return
    fut.cancel()
    try:
        fut.result(timeout=5)
    except Exception:
        pass  # cancelled, or failed on its own - either way it is over


def is_streaming() -> bool:
    with _current_lock:
        return _current is not None and not _current.done()


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
