#!/usr/bin/env python
"""
Apple TV integration for Musicfig using pyatv.
Enables launching Disney+, Netflix, and other streaming content via deep links.
"""

import asyncio
import re
import logging
import threading
import time
import http.client
import ipaddress
import urllib.error
import urllib.parse
import urllib.request
# Optional dependency: pyatv is in requirements-extras.txt, not requirements.txt
# (it needs a compiler on the Pi). Without it the Apple TV tag actions are
# simply unavailable and everything else runs.
try:
    import pyatv
    from pyatv.const import Protocol
except ImportError:  # pragma: no cover - depends on the install
    pyatv = None
    Protocol = None

logger = logging.getLogger(__name__)

# Cache for Apple TV connection. Each tag action runs under its own
# asyncio.run(), so the cache also remembers which event loop made the
# connection: a pyatv connection bound to a loop that has since been closed
# looks alive (power_state is a cached property) but every command on it
# fails, which showed up as "Open URL failed" on the second tap.
_atv = None
_atv_name = None
_atv_loop = None

# One Apple TV action at a time, process-wide. Lift-to-pause runs on its own
# thread (lego's appletv-pause) while launches run on the pad thread, each
# under its own asyncio.run() over the cache above: the stale-loop check in
# connect() cannot tell a finished loop from another thread's LIVE one, so a
# pause and a launch in flight together could close each other's session
# mid-command ("Open URL failed" right after a quick lift / re-place). Held
# for the whole action, the previous action's loop has always finished by the
# time the next one looks at the cache, and the only session it ever closes
# is a dead one. A launch arriving during a pause waits exactly as long as
# that pause still takes; it is never skipped.
_action_lock = threading.Lock()

# Configuration - set these in config.py or tags.yml
APPLE_TV_NAME = None  # Will be auto-discovered if None
APPLE_TV_IP = None    # Optional: specify IP directly
APPLE_TV_ID = None    # Device identifier
COMPANION_CREDENTIALS = None
AIRPLAY_CREDENTIALS = None
# The "Deeplink" tvOS app (celerityapps, TestFlight) runs a small web server
# on the Apple TV that opens any URL/scheme tvOS knows: GET /open?target=<url>.
# Base URL of that server (http://<apple-tv-ip>[:port]); None = not used.
DEEPLINK_SERVER = None
DEEPLINK_TIMEOUT = 4  # seconds; the server is on the LAN, anything slower is "not running"
socket_timeout = __import__("socket").timeout
# tvOS suspends the app (and its server) as soon as another app is in front,
# so a refused connection is normal: bring the app to the front over pyatv
# (the author's own Shortcut starts with "Open App on Apple TV: DeepLink"),
# give it a moment, and ask again. Bundle id from the Game Room app list.
DEEPLINK_APP_ID = "com.celerity.DeepLink"
DEEPLINK_FOREGROUND_WAIT = 2.5
FIRST_WAKE_WINDOW = 8  # seconds to give the first turn_on before resending it
ACTION_WAIT_S = 120  # longest a launch waits for a pause still on the wire (Linux SYN retries ~127 s)


def _is_ip(value) -> bool:
    """pyatv.scan(hosts=...) wants IP addresses; a `.local` name or a typo in
    appletv_ip raised AddressValueError straight out of the pad loop."""
    try:
        ipaddress.ip_address(str(value or "").strip())
        return True
    except ValueError:
        return False


def _run_action(coro_factory):
    """asyncio.run() one tag action's coroutine with the Apple TV to itself
    (see _action_lock). Every sync entry point that touches the connection
    cache goes through here; the lock is released however the action ends."""
    if not _action_lock.acquire(blocking=False):
        logger.info("Apple TV busy with another action; %s waits for it to finish",
                    threading.current_thread().name)
        # Bounded: a pause stuck in a TCP connect with no pyatv timeout must
        # not hold a launch forever, and a future same-thread re-entrant call
        # fails loudly here instead of hanging the pad loop.
        if not _action_lock.acquire(timeout=ACTION_WAIT_S):
            logger.error("Apple TV still busy after %ss; giving up on this action", ACTION_WAIT_S)
            return False
    try:
        return asyncio.run(coro_factory())
    finally:
        _action_lock.release()


def _guarded(label, coro_factory):
    """Run one tag action's coroutine; any exception is logged and becomes
    False. The pad loop has no try/except of its own and the service is
    Restart=always, so an escape here darkens every pad."""
    try:
        return _run_action(coro_factory)
    except Exception as e:
        logger.error(f"{label} failed: {e.__class__.__name__}: {e}")
        return False


async def scan_for_apple_tvs(timeout=5):
    """Scan network for Apple TVs and return list of found devices. With an
    IP configured the scan is unicast to that host and returns as soon as it
    answers (a reconnect around the HDMI-CEC sleep/wake then costs well under
    a second instead of the full multicast timeout)."""
    logger.info("Scanning for Apple TVs...")
    hosts = [APPLE_TV_IP] if _is_ip(APPLE_TV_IP) else None
    atvs = await pyatv.scan(asyncio.get_event_loop(), timeout=timeout, hosts=hosts)

    for atv in atvs:
        logger.info(f"Found: {atv.name} at {atv.address}")

    return atvs


async def _close_quietly(atv, timeout=3):
    """pyatv's close() returns the disconnect tasks it just scheduled; left
    un-awaited they surface at asyncio.run() shutdown as "unhandled exception
    ... Event loop is closed" ERROR lines (one per lift-to-pause) when the
    connection came from an earlier action's loop. Await them here and
    swallow whatever a dead transport raises. The cap matters on the same-loop
    path: Companion teardown is three request/response round trips, and a
    half-open session after turn_off() would otherwise sit in them for pyatv's
    5 s per message before the reconnect could start."""
    try:
        tasks = atv.close()
    except Exception as e:
        logger.debug(f"Apple TV close raised {e}")
        return
    if not tasks:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout)
    except asyncio.TimeoutError:
        logger.info(f"Apple TV close did not finish within {timeout}s; continuing")
    except Exception as e:
        logger.debug(f"Apple TV close tasks raised {e}")


async def connect(name=None, ip=None):
    """Connect to an Apple TV by name or IP address."""
    global _atv, _atv_name, _atv_loop

    # Return cached connection if still valid
    if _atv is not None:
        if _atv_loop is not asyncio.get_running_loop():
            logger.info("Cached Apple TV connection belongs to a finished event loop, reconnecting...")
            await _close_quietly(_atv)
            _atv = None
        else:
            try:
                # Cheap sanity check that the facade is still usable (pyatv
                # raises once close() has blocked the interface); power_state
                # is a property, so no await.
                _atv.power.power_state
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
        _atv_loop = asyncio.get_running_loop()
        logger.info(f"Connected to {target.name}")
        return _atv
    except Exception as e:
        logger.error(f"Failed to connect to Apple TV: {e}")
        return None


async def disconnect():
    """Disconnect from Apple TV."""
    global _atv, _atv_name, _atv_loop
    if _atv:
        # Short cap: this runs inside the wake/launch sequence (up to four
        # reconnects), so a box that stopped answering must not stall it.
        await _close_quietly(_atv, timeout=1)
        logger.info(f"Disconnected from {_atv_name}")
        _atv = None
        _atv_name = None
        _atv_loop = None


async def _reconnect(name=None, ip=None):
    """Drop the current connection and make a fresh one. pyatv's Companion
    session does not survive the Apple TV sleeping: commands after a
    turn_off()/turn_on() fail ("Open URL failed") until we reconnect."""
    await disconnect()
    return await connect(name=name, ip=ip)


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
        # pyatv's power_state is a plain (cached, push-updated) property, not
        # a coroutine: awaiting it raised TypeError and made every caller
        # believe the box was off.
        power_state = atv.power.power_state
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


async def _cec_wake(atv, timeout=25, name=None, ip=None):
    """Sleep and wake the Apple TV so HDMI-CEC makes the TV turn on and
    switch to its input (an already-awake Apple TV launching an app does not
    trigger that). Returns (woke, atv): ``atv`` is a FRESH connection to use
    for the launch, because the session that put the device to sleep is dead
    afterwards and ``power_state`` on it never changes (the 2026-10-09
    "did not wake within 25s" + "Open URL failed" failure)."""
    await atv.power.turn_off()
    await asyncio.sleep(4)
    atv = await _reconnect(name=name, ip=ip)
    if atv is None:
        logger.error("Apple TV not reachable after sleeping it")
        return False, None
    # A turn_on sent while the box is still dropping into sleep is lost (the
    # Apple TV HD misses it every time, the 4K now and then), so give the first
    # one only a short window and resend on a fresh session; the second gets
    # the rest of the timeout. Cold-wake cost on the HD: ~15 s instead of ~35.
    first = min(FIRST_WAKE_WINDOW, int(timeout))
    woke = await _turn_on_and_wait(atv, first)
    if not woke:
        logger.info("Apple TV did not report awake within %ss; sending turn_on again", first)
        again = await _reconnect(name=name, ip=ip)
        if again is not None:
            atv = again
            woke = await _turn_on_and_wait(atv, max(10, int(timeout) - first))
    if woke:
        await asyncio.sleep(2)  # let tvOS settle before a launch
        logger.info("Apple TV woken for HDMI-CEC input switch")
    else:
        logger.error("Apple TV did not report awake after two turn_on attempts")
    # Either way launch on a fresh session: the one that sent turn_on may be stale too.
    fresh = await _reconnect(name=name, ip=ip)
    return woke, fresh or atv


async def _turn_on_and_wait(atv, timeout):
    """turn_on, then poll power_state for up to ``timeout`` seconds."""
    await atv.power.turn_on()
    for _ in range(int(timeout)):
        await asyncio.sleep(1)
        if str(atv.power.power_state) == "PowerState.On":
            return True
    return False


async def _launch_with_retry(atv, url, name=None, ip=None):
    """launch_app with one retry on a fresh connection; tvOS answers
    "Open URL failed" on a session that has gone stale."""
    for attempt in (1, 2):
        try:
            logger.info(f"Launching: {url}")
            await atv.apps.launch_app(url)
            logger.info("Launch successful")
            return True, atv
        except Exception as e:
            logger.error(f"Failed to launch app (attempt {attempt}): {e}")
            if attempt == 1:
                await asyncio.sleep(2)
                fresh = await _reconnect(name=name, ip=ip)
                if fresh is None:
                    break
                atv = fresh
                try:  # "Open URL failed" is also what a sleeping box answers: make sure it is awake
                    await atv.power.turn_on()
                    await asyncio.sleep(3)
                except Exception as e2:
                    logger.debug(f"turn_on before the retry raised {e2}")
    return False, atv


OK_KEYS = ("select", "play_pause", "play")


def parse_ok_delays(ok_delay, n):
    """``ok_delay`` is one number (same gap before every key) or a list /
    "12, 5" string: the first key after 12 s, each later key 5 s after the
    previous; the last value repeats for extra keys. Returns ``n`` floats,
    non-numbers become 0."""
    if isinstance(ok_delay, (list, tuple)):
        parts = list(ok_delay)
    else:
        parts = str(ok_delay if ok_delay is not None else 0).replace(";", ",").split(",")
    delays = []
    for part in parts:
        try:
            delays.append(max(0.0, float(part)))
        except (TypeError, ValueError):
            logger.warning("ok_delay %r is not a number; ignoring it", part)
            delays.append(0.0)
    if not delays:
        delays = [0.0]
    while len(delays) < n:
        delays.append(delays[-1])
    return delays[:n]


def parse_ok_keys(ok_key):
    """``ok_key`` may be one key or a sequence ("select, play_pause", or a
    list): the Apple TV app shows a "Who's watching?" chooser first, so a
    Select is needed before Play. Unknown names become select, with a warning."""
    if isinstance(ok_key, (list, tuple)):
        parts = [str(k) for k in ok_key]
    else:
        parts = str(ok_key or "select").replace(";", ",").split(",")
    keys = []
    for k in parts:
        k = k.strip().lower()
        if not k:
            continue
        if k not in OK_KEYS:
            logger.warning("ok_key %r unknown (want one of %s); using select", k, ", ".join(OK_KEYS))
            k = "select"
        keys.append(k)
    return keys or ["select"]


async def _press_keys(atv, keys, delay):
    """For each key in order: wait its delay (see parse_ok_delays), press."""
    for key, wait in zip(keys, parse_ok_delays(delay, len(keys))):
        try:
            await asyncio.sleep(wait)
            await getattr(atv.remote_control, key)()
            logger.info(f"Apple TV: {key} pressed")
        except Exception as e:
            logger.error(f"Failed to press {key} on Apple TV: {e}")
            return False
    return True


async def launch_sequence_async(url, cec_wake=False, ok_delay=0, atv_name=None, atv_ip=None, ok_key="select"):
    """One tag action: optionally sleep/wake for HDMI-CEC, launch the deep
    link (one retry on a fresh connection), optionally press a remote key
    after ok_delay seconds: "select" for an account/profile chooser,
    "play_pause" (or "play") for apps that land on the title page and wait,
    like the Apple TV app; "select, play_pause" presses both. ok_delay is one
    gap for every key or "12, 5" (first key after 12 s, then 5 s apart).
    Returns True if the launch succeeded."""
    keys = parse_ok_keys(ok_key)
    delays = parse_ok_delays(ok_delay, len(keys))
    atv = await connect(name=atv_name, ip=atv_ip)
    if not atv:
        return False
    if cec_wake:
        try:
            woke, fresh = await _cec_wake(atv, name=atv_name, ip=atv_ip)
            if fresh is not None:
                atv = fresh
        except Exception as e:
            woke = False
            logger.error(f"Apple TV sleep/wake failed: {e}")
            fresh = await _reconnect(name=atv_name, ip=atv_ip)
            if fresh is not None:
                atv = fresh
        if not woke:
            # launching an app wakes the device anyway; only the input switch may be lost
            logger.warning("Continuing with the launch without the HDMI-CEC wake")
    ok, atv = await _launch_with_retry(atv, url, name=atv_name, ip=atv_ip)
    if not ok:
        return False
    if any(d > 0 for d in delays):
        await _press_keys(atv, keys, delays)
    return True


def is_on_sync(atv_name=None, atv_ip=None):
    """Synchronous wrapper for is_on(); False when the box is asleep OR
    unreachable, and never raises (it runs inside the pad loop)."""
    return bool(_guarded("Apple TV power check", lambda: is_on(atv_name=atv_name, atv_ip=atv_ip)))


async def cec_wake_async(atv_name=None, atv_ip=None):
    """Sleep/wake the Apple TV on its own (HDMI-CEC turns the TV on and to
    its input) without launching anything. True when it reported awake."""
    atv = await connect(name=atv_name, ip=atv_ip)
    if not atv:
        return False
    woke, _ = await _cec_wake(atv, name=atv_name, ip=atv_ip)
    return woke


def turn_on():
    """Synchronous wrapper for turn_on_async()."""
    return _run_action(turn_on_async)


async def launch_app(url_or_bundle_id, atv_name=None, atv_ip=None):
    """
    Launch an app or deep link on Apple TV.

    Args:
        url_or_bundle_id: Either a deep link URL or app bundle ID
            - Disney+: "https://www.disneyplus.com/video/{content_id}"
            - Netflix: "https://www.netflix.com/title/{id}"
            - YouTube: "youtube://www.youtube.com/watch?v={id}" (see youtube_deep_link)
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

    return _run_action(lambda: launch_app(url, atv_name=atv_name))


def launch_netflix(content_id, atv_name=None):
    """Launch Netflix content on Apple TV."""
    if content_id.startswith("http"):
        url = content_id
    else:
        url = f"https://www.netflix.com/title/{content_id}"

    return _run_action(lambda: launch_app(url, atv_name=atv_name))


def youtube_deep_link(video):
    """The link form tvOS actually opens in the YouTube app.

    tvOS (26, Apple TV 4K) rejects plain https://www.youtube.com/... and
    https://youtu.be/... with "Open URL failed"; the app's own scheme,
    youtube://www.youtube.com/watch?v=<id>, opens the video. Accepts a bare
    video id, a watch URL, a youtu.be short link or an existing youtube:// link.
    """
    video = str(video or "").strip()
    video_id = video
    m = (re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", video)
         or re.search(r"youtu\.be/([A-Za-z0-9_-]{6,})", video)
         or re.search(r"youtube://(?:v|www\.youtube\.com/v)/([A-Za-z0-9_-]{6,})", video)
         or re.search(r"youtube://([A-Za-z0-9_-]{6,})$", video))
    if m:
        video_id = m.group(1)
    if not video_id:
        logger.warning("YouTube tag has no video id")
    return f"youtube://www.youtube.com/watch?v={video_id}"


def launch_youtube(video_id, atv_name=None, cec_wake=False, ok_delay=0):
    """Launch a YouTube video on the Apple TV. cec_wake sleeps/wakes the Apple
    TV first so the TV switches to its input; ok_delay > 0 presses Select that
    many seconds after the launch (YouTube's account chooser)."""
    return _guarded("YouTube launch", lambda: launch_sequence_async(
        youtube_deep_link(video_id), cec_wake=cec_wake, ok_delay=ok_delay, atv_name=atv_name))


def launch_url(url, atv_name=None, cec_wake=False, ok_delay=0, ok_key="select"):
    """Launch any deep link / URL scheme on the Apple TV (the `appletv_url:`
    tag action): whatever string tvOS will open, passed through untouched.
    Same optional sleep/wake and Select press as the youtube action."""
    url = str(url or "").strip()
    if not url:
        logger.warning("appletv_url tag is empty")
        return False
    return _guarded("Apple TV URL launch", lambda: launch_sequence_async(
        url, cec_wake=cec_wake, ok_delay=ok_delay, atv_name=atv_name, ok_key=ok_key))


def deeplink_open_url(target, server=None):
    """URL the Deeplink app's web server opens for ``target`` (its documented
    GET /open: https://celerityapps.com/deeplinks/docs). A target with a
    scheme (``something://``) is sent as ``target=``; anything else is taken
    as the display name of a deep link already saved in the app and sent as
    ``name=`` (404 when no such name exists)."""
    base = str(server or DEEPLINK_SERVER or "").strip().rstrip("/")
    if base and "://" not in base:
        base = "http://" + base  # tags.yml may just name the box: Game-Room.local
    target = str(target).strip()
    key = "target" if "://" in target else "name"
    # The app's server takes the URL raw (its docs and the author's working
    # Apple TV app example: target=https://tv.apple.com/us/movie/...), so keep
    # scheme, path and any existing %xx escapes as-is and only escape what
    # would split our own query string.
    return f"{base}/open?{key}={urllib.parse.quote(target, safe=':/@%')}"


def _deeplink_get(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read(512)


def launch_deeplink(target, atv_name=None, cec_wake=False, ok_delay=0, ok_key="select"):
    """The `deeplink:` tag action. Ask the Deeplink app on the Apple TV to
    open ``target`` (anything it supports: https pages, app URL schemes such
    as plutotv:// or vlc-x-callback://, raw .m3u8 streams). The app's server
    does not always come back after the Apple TV reboots (force-quit and
    reopen the app fixes it), so when it does not answer the same target is
    handed to pyatv's launch_app instead and the fallback is logged in plain
    words. cec_wake (a cold start) sleeps/wakes the box over pyatv BEFORE the
    Deeplink server is asked, so the TV is on and on the right input whoever
    ends up opening the target (a sleeping box has no web server anyway); the
    pyatv fallback then launches without a second sleep/wake. The ok_delay key
    press happens on either path, since the TV app waits for Play after the
    page opens no matter who opened it."""
    target = str(target or "").strip()
    if not target:
        logger.warning("deeplink tag is empty")
        return False
    delays = parse_ok_delays(ok_delay, len(parse_ok_keys(ok_key)))
    if DEEPLINK_SERVER:
        if cec_wake:
            if _guarded("Apple TV HDMI-CEC wake", lambda: cec_wake_async(atv_name)):
                logger.info("Apple TV woken for HDMI-CEC before the Deeplink request")
            else:
                # The box may have been slept by our own turn_off with no wake
                # delivered; a server request on a sleeping box times out and
                # would be read as "opened". Make sure it is awake first.
                logger.warning("HDMI-CEC wake before the Deeplink request did not complete; sending turn_on")
                _guarded("Apple TV turn_on", turn_on_async)
                time.sleep(3)
            cec_wake = False  # done (or tried twice): the pyatv fallback must not sleep the box again
        url = deeplink_open_url(target)
        for attempt in (1, 2):
            try:
                status, body = _deeplink_get(url, DEEPLINK_TIMEOUT)
                if 200 <= status < 300:
                    logger.info(f"Deeplink app opened {target}")
                    if any(d > 0 for d in delays):
                        _guarded("Deeplink key press", lambda: press_after_async(delays, ok_key, atv_name=atv_name))
                    return True
                logger.warning(f"Deeplink server answered {status} for {target}: {body[:120]!r}; trying pyatv")
                break
            except urllib.error.HTTPError as e:
                if e.code == 404 and "://" not in target:
                    logger.error(f"Deeplink app has no saved deep link named {target!r}; nothing to fall back to")
                    return False
                logger.warning(f"Deeplink server answered {e.code} for {target}; trying pyatv")
                break
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
                reason = getattr(e, "reason", e)
                if isinstance(reason, (TimeoutError, socket_timeout)) or isinstance(e, (TimeoutError, socket_timeout)):
                    # The server answered the TCP connect but never replied: it most
                    # likely opened the target and got suspended behind it. Opening it
                    # again (or sleeping/waking the box) would fight the app that is now
                    # playing, so treat this as done.
                    logger.warning(f"Deeplink server took longer than {DEEPLINK_TIMEOUT}s to answer for {target}; "
                                   "assuming it opened the target")
                    return True
                if attempt == 1 and DEEPLINK_APP_ID and isinstance(reason, ConnectionRefusedError):
                    logger.info("Deeplink server refused the connection (app suspended); bringing it to the front")
                    if _guarded("DeepLink app foreground", lambda: _foreground_deeplink_app(atv_name)):
                        continue
                logger.warning(f"Deeplink server at {DEEPLINK_SERVER} not answering ({e}); "
                               "on the Apple TV force-quit and reopen the Deeplink app. Trying pyatv")
                break
    else:
        logger.info("deeplink_server not configured; opening via pyatv")
    return _guarded("Deeplink pyatv fallback", lambda: launch_sequence_async(
        target, cec_wake=cec_wake, ok_delay=ok_delay, atv_name=atv_name, ok_key=ok_key))


async def _foreground_deeplink_app(atv_name=None):
    """Launch the DeepLink app by bundle id so its web server comes back,
    then wait for it to start listening. True when the launch went through."""
    atv = await connect(name=atv_name)
    if not atv:
        return False
    try:
        await atv.apps.launch_app(DEEPLINK_APP_ID)
        await asyncio.sleep(DEEPLINK_FOREGROUND_WAIT)
        return True
    except Exception as e:
        logger.warning(f"Could not open the DeepLink app ({DEEPLINK_APP_ID}): {e}")
        return False


async def press_after_async(delay, key="select", atv_name=None):
    """Wait ``delay`` seconds, press a key, for each key in ``key`` (one name
    or a sequence), over pyatv."""
    atv = await connect(name=atv_name)
    if not atv:
        return False
    return await _press_keys(atv, parse_ok_keys(key), delay)


def pause(atv_name=None):
    """Lift-to-pause: one Play/Pause press on the Apple TV (a toggle - if the
    video was already paused by hand this resumes it; Apple exposes no
    pause-only command over Companion)."""
    try:
        return _run_action(lambda: press_after_async(0, "play_pause", atv_name=atv_name))
    except Exception as e:
        logger.error(f"Apple TV pause failed: {e}")
        return False


def list_apple_tvs():
    """List all Apple TVs on the network."""
    atvs = asyncio.run(scan_for_apple_tvs())
    return [(str(atv.address), atv.name) for atv in atvs]


# Configuration loading from tags.yml
_config_loaded = False


def reset_config():
    """Forget the loaded config and mark the cached connection stale, so the
    next action reconnects with whatever credentials tags.yml holds now (a
    connection made before a pairing existed would otherwise be reused
    without them). This runs on the pad thread with no event loop, where
    pyatv's close() cannot schedule its disconnect tasks (it raised "no
    running event loop" and leaked the Companion socket per reload); leaving
    `_atv` in place with `_atv_loop = None` makes the next connect() take the
    stale-loop branch and close it properly."""
    global _config_loaded, _atv_loop
    _config_loaded = False
    _atv_loop = None


def load_config(tags):
    """Load Apple TV configuration from tags dict."""
    global APPLE_TV_NAME, APPLE_TV_IP, APPLE_TV_ID, DEEPLINK_SERVER, DEEPLINK_APP_ID
    global COMPANION_CREDENTIALS, AIRPLAY_CREDENTIALS
    global _config_loaded

    if _config_loaded:
        return

    if 'appletv_name' in tags:
        APPLE_TV_NAME = tags['appletv_name']
        logger.info(f"Apple TV name from config: {APPLE_TV_NAME}")

    if 'appletv_ip' in tags:
        APPLE_TV_IP = str(tags['appletv_ip'] or '').strip() or None
        if APPLE_TV_IP and not _is_ip(APPLE_TV_IP):
            logger.warning(f"appletv_ip {APPLE_TV_IP!r} is not an IP address; using multicast scans "
                           "(use the Apple TV's IP, not its .local name)")
            APPLE_TV_IP = None
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
    if 'deeplink_server' in tags:
        DEEPLINK_SERVER = str(tags['deeplink_server'] or '').strip().rstrip('/') or None
        if DEEPLINK_SERVER and '://' not in DEEPLINK_SERVER:
            DEEPLINK_SERVER = 'http://' + DEEPLINK_SERVER
        logger.info(f"Deeplink app server from config: {DEEPLINK_SERVER}")
    if 'deeplink_app_id' in tags:
        DEEPLINK_APP_ID = str(tags['deeplink_app_id'] or '').strip() or None
        logger.info(f"Deeplink app id from config: {DEEPLINK_APP_ID}")

    _config_loaded = True


def activated():
    """Check if Apple TV integration is available (pyatv installed).

    Apple TVs are auto-discovered on the network, so with pyatv present this
    is always True; without it the Disney/Netflix/YouTube tag actions log a
    warning and do nothing.
    """
    if pyatv is None:
        logger.warning("Apple TV action skipped: pyatv is not installed (pip install -r requirements-extras.txt)")
        return False
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
