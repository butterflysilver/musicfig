#!/usr/bin/env python

from app import webhook
from mutagen.mp3 import MP3
import app.spotify as spotify
import app.appletv as appletv
import app.huesyncbox as huesyncbox
import app.samsungtv as samsungtv
import app.homepod as homepod
import app.xboxctl as xboxctl
import app.yoto as yoto
import app.yoto_tracks as yoto_tracks
import app.tags as nfctags
import binascii
import logging
import os
import socket
import shlex
import subprocess
import sys
import threading
import time
import random

# Windows: Load libusb before importing usb.core
if sys.platform == 'win32':
    import ctypes
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    libusb_path = os.path.join(base_dir, 'venv', 'Lib', 'site-packages', 'libusb', '_platform', 'windows', 'x86_64', 'libusb-1.0.dll')
    if os.path.exists(libusb_path):
        # os.environ['PYUSB_DEBUG'] = 'debug'  # Disabled - too verbose
        ctypes.CDLL(libusb_path)

import usb.core
import usb.util
import app.mp3player as mp3player
import glob

logger = logging.getLogger(__name__)

# Which room this pad is in (the Pi's hostname unless MUSICFIG_ROOM says
# otherwise); tags can behave differently per room, see tags.resolve_for_room.
ROOM = os.environ.get('MUSICFIG_ROOM') or socket.gethostname().split('.')[0]

# How often to look for a pad that is missing or was unplugged.
RECONNECT_INTERVAL = 3.0

# A pad that WAS working and has been gone this long makes the process exit
# so systemd (Restart=always) brings it back with a fresh libusb context:
# libusb's cached device list can miss a device that reappears without an
# add event (seen with a sysfs unbind/bind on the game-room Pi, 2026-09-18).
PAD_GONE_RESTART_AFTER = 60.0

# libusb errnos that mean the pad is gone or wedged (unplugged, port reset,
# hub dropped it, endpoint stalled): EIO, ENODEV, EPIPE. Anything else is
# treated as transient.
_PAD_GONE_ERRNOS = (5, 19, 32)


class Dimensions():
    """The LEGO Dimensions pad. Survives the pad being absent at start-up
    or unplugged while running: every call is a no-op until reconnect()
    brings it back, and nothing here spins or raises."""

    def __init__(self):
        self.dev = None
        self._retry_at = 0.0
        self._last_error = None  # last failure logged, so repeats stay quiet
        self._gone_since = None  # set when a working pad disappears
        self.reconnect()

    def reconnect(self):
        """(Re)open the pad if it is not open. Returns True only at the
        moment a pad comes online so the caller can restore its lights.
        Attempts are spaced RECONNECT_INTERVAL apart and the wait is
        slept here, so a missing pad costs no CPU."""
        if self.dev is not None:
            return False
        now = time.monotonic()
        if now < self._retry_at:
            time.sleep(min(self._retry_at - now, 0.5))
            return False
        self._retry_at = now + RECONNECT_INTERVAL
        try:
            self.dev = self.init_usb()
        except (ValueError, usb.core.USBError) as e:
            self.dev = None
            if str(e) != self._last_error:
                logger.warning('LEGO pad not available (%s); retrying every %ss'
                               % (e, RECONNECT_INTERVAL))
                self._last_error = str(e)
            if self._gone_since is not None                     and now - self._gone_since > PAD_GONE_RESTART_AFTER:
                logger.error('LEGO pad gone for %.0fs; exiting so systemd restarts '
                             'musicfig with a fresh USB view' % (now - self._gone_since))
                logging.shutdown()
                os._exit(3)  # startLego may run off the main thread; be certain
            return False
        self._last_error = None
        self._gone_since = None
        logger.info('LEGO pad connected')
        return True

    def _lost(self, err):
        """Forget a pad that stopped answering; reconnect() takes it from here."""
        logger.warning('LEGO pad lost (%s); waiting for it to come back' % err)
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass
        self.dev = None
        self._gone_since = time.monotonic()
        self._retry_at = self._gone_since + RECONNECT_INTERVAL

    @staticmethod
    def _is_gone(err):
        return (getattr(err, 'errno', None) in _PAD_GONE_ERRNOS
                or 'No such device' in str(err))

    def init_usb(self):
        dev = usb.core.find(idVendor=0x0e6f, idProduct=0x0241)

        if dev is None:
            raise ValueError('pad not found on USB')

        # Windows with WinUSB doesn't need kernel driver detach
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except NotImplementedError:
            pass  # Windows doesn't support this

        # Initialise portal
        dev.set_configuration()

        # Claim the interface explicitly (needed for WinUSB)
        usb.util.claim_interface(dev, 0)
        logger.info('USB interface claimed')

        dev.write(1,[0x55, 0x0f, 0xb0, 0x01, 0x28, 0x63, 0x29, 0x20, 0x4c,
                     0x45, 0x47, 0x4f, 0x20, 0x32, 0x30, 0x31, 0x34, 0xf7,
                     0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                     0x00, 0x00, 0x00, 0x00, 0x00])
        return dev

    def send_command(self, command):
        checksum = 0
        for word in command:
            checksum = checksum + word
            if checksum >= 256:
                checksum -= 256

        message = command + [checksum]

        while len(message) < 32:
            message.append(0x00)

        dev = self.dev  # snapshot: the main loop may drop it from another thread
        if dev is None:
            return
        try:
            dev.write(1, message)
        except usb.core.USBError as e:
            if self._is_gone(e):
                self._lost(e)
            else:
                logger.warning('USB write error: %s' % e)

    def switch_pad(self, pad, colour):
        self.send_command([0x55, 0x06, 0xc0, 0x02, pad, colour[0], 
                          colour[1], colour[2],])
        return

    def fade_pad(self, pad, pulse_time, pulse_count, colour):
        self.send_command([0x55, 0x08, 0xc2, 0x0f, pad, pulse_time, 
                          pulse_count, colour[0], colour[1], colour[2],])
        return

    def flash_pad(self, pad, on_length, off_length, pulse_count, colour):
        self.send_command([0x55, 0x09, 0xc3, 0x03, pad,
                          on_length, off_length, pulse_count,
                          colour[0], colour[1], colour[2],])
        return

    def update_nfc(self):
        dev = self.dev
        if dev is None:
            return
        try:
            inwards_packet = dev.read(0x81, 32, timeout = 100)
            bytelist = list(inwards_packet)
            if not bytelist:
                return
            # Only log NFC events, not every USB packet
            if bytelist[0] == 0x56:
                pad_num = bytelist[2]
                uid_bytes = bytelist[6:13]
                identifier = binascii.hexlify(bytearray(uid_bytes)).decode("utf-8")
                identifier = identifier.replace('000000','')
                removed = bool(bytelist[5])
                if removed:
                    response = 'removed:%s:%s' % (pad_num, identifier)
                else:
                    response = 'added:%s:%s' % (pad_num, identifier)
                return response
        except usb.core.USBTimeoutError:
            # Normal timeout, no data available
            return
        except usb.core.USBError as e:
            if self._is_gone(e):
                self._lost(e)
            else:
                logger.warning('USB error: %s' % e)
                time.sleep(0.1)  # never spin on a repeating error
            return
        except Exception as e:
            logger.warning('NFC read error: %s' % e)
            time.sleep(0.1)
            return

def run_tag_hook(cmd, identifier, pad, timeout=30):
    """Run MUSICFIG_ON_TAG_CMD in the background and log its outcome.

    The command is a shell string from the unit's environment (operator
    controlled, not tag controlled). It is waited on with a timeout so the
    child is reaped and a hung hook cannot pile up.
    """
    def worker():
        try:
            result = subprocess.run(cmd, shell=True, timeout=timeout,
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
            if result.returncode == 0:
                logger.info('tag %s on pad %s: on-tag hook ok' % (identifier, pad))
            else:
                err = result.stderr.decode('utf-8', 'replace').strip().splitlines()
                logger.warning('on-tag hook exited %s: %s' % (
                    result.returncode, err[-1] if err else '(no output)'))
        except subprocess.TimeoutExpired:
            logger.warning('on-tag hook timed out after %ss' % timeout)
        except OSError as e:
            logger.warning('on-tag hook failed to start: %s' % e)
    threading.Thread(target=worker, name='on-tag-hook', daemon=True).start()


class Base():
    def __init__(self):
        self.OFF = [0, 0, 0]
        self.RED = [255, 0, 0]
        self.GREEN = [0, 255, 0]
        self.BLUE = [0, 0, 255]
        self.PINK = [255, 192, 203]
        self.ORANGE = [255, 165, 0]
        self.PURPLE = [255, 0, 255]
        self.LBLUE = [173, 216, 230]  # Light blue (was incorrectly white)
        self.OLIVE = [128, 128, 0]
        # Use list of actual colors instead of strings for eval()
        self.COLOURS = [
            self.RED, self.GREEN, self.BLUE, self.PINK,
            self.ORANGE, self.PURPLE, self.LBLUE, self.OLIVE
        ]
        self.base = self.startLego()

    def randomLightshow(self,duration = 60):
        logger.info("Lightshow started for %s seconds." % duration)
        self.lightshowThread = threading.currentThread()
        t = time.perf_counter()
        while getattr(self.lightshowThread, "do_run", True) and (time.perf_counter() - t) < duration:
            pad = random.randint(0,2)
            self.colour = random.randint(0, len(self.COLOURS) - 1)
            self.base.switch_pad(pad, self.COLOURS[self.colour])
            time.sleep(round(random.uniform(0,0.5), 1))
        self.base.switch_pad(0,self.OFF)

    def startLightshow(self,duration_ms):
        if switch_lights:
            self.lightshowThread = threading.Thread(target=self.randomLightshow,
                args=([(duration_ms / 1000)]))
            self.lightshowThread.daemon = True
            self.lightshowThread.start()

    def initMp3(self):
        self.p = mp3player.Player()

        def monitor():
            global mp3state
            global mp3elapsed
            # Daemon thread - runs forever monitoring player state
            while True:
                state = self.p.event_queue.get(block=True, timeout=None)
                mp3state = str(state[0]).replace('PlayerState.', '')
                mp3elapsed = state[1]

        monitor_thread = threading.Thread(target=monitor, name="monitor", daemon=True)
        monitor_thread.start() 

    def startMp3(self, filename, mp3_dir, is_playlist=False):
        global mp3_duration
        # load an mp3 file
        if not is_playlist:
            mp3file = mp3_dir + filename
            logger.info('Playing %s.' % filename)
            self.p.open(mp3file)
            self.p.play()

            audio = MP3(mp3file)
            mp3_duration = audio.info.length
            self.startLightshow(mp3_duration * 1000)
        else:
            self.p.playlist(filename)
            mp3_duration = 0
            if filename:
                for file_mp3 in filename:
                    audio = MP3(file_mp3)
                    mp3_duration = mp3_duration + audio.info.length
            else:
                logger.info('Check the folder, maybe empty!!!')
            self.startLightshow(mp3_duration * 1000)

    def stopMp3(self):
        global mp3state
        mp3state = 'STOPPED'

    def pauseMp3(self):
        global mp3state
        if mp3state == 'PLAYING':
            self.p.pause()
            logger.info('Track paused.')
            mp3state = 'PAUSED'
            return

    def playMp3(self, filename, mp3_dir):
        global t
        global mp3state
        spotify.pause()
        if previous_tag == current_tag and mp3state == 'PAUSED':
            # Resume
            logger.info("Resuming mp3 track.")
            self.p.play()
            remaining = mp3_duration - mp3elapsed
            if remaining >= 0.1:
                self.startLightshow(remaining * 1000)
                return
        # New play 
        self.stopMp3()
        self.startMp3(filename, mp3_dir)
        mp3state = 'PLAYING'

    def playPlaylist(self, playlist_filename, mp3_dir, shuffle=False):
        global mp3state
        list_mp3_to_play = []
        spotify.pause()

        mp3list = mp3_dir +'/'+ playlist_filename + '/*.mp3'
        ##logger.debug(mp3list)

        list_mp3_to_play = glob.glob(mp3list)

        if not list_mp3_to_play:
            logger.warning('Playlist folder "%s" is empty - no MP3 files found' % playlist_filename)
            self.base.flash_pad(pad=0, on_length=10, off_length=10,
                               pulse_count=3, colour=self.ORANGE)
            return

        if shuffle:
            random.shuffle(list_mp3_to_play)
        ##logger.debug(list_mp3_to_play)

        self.startMp3(list_mp3_to_play, mp3_dir, True)
        mp3state = 'PLAYING'

    def switchHdmiToAppleTv(self, tags):
        """
        Switch HDMI input to Apple TV using available methods.
        Tries Hue Sync Box first, then Samsung SmartThings TV.

        Returns:
            True if HDMI was switched, False otherwise
        """
        hdmi_switched = False

        # Try Hue Sync Box first (load config before checking)
        huesyncbox.load_config(tags)
        if huesyncbox.configured():
            logger.info('Switching HDMI via Hue Sync Box...')
            hdmi_switched = huesyncbox.switch_to_appletv_sync()

        # Fall back to Samsung SmartThings TV
        if not hdmi_switched:
            samsungtv.load_config(tags)
            if samsungtv.configured():
                logger.info('Switching HDMI via Samsung SmartThings...')
                hdmi_switched = samsungtv.switch_to_appletv()

        return hdmi_switched

    def startLego(self):
        global current_tag
        global previous_tag
        global mp3state
        global p
        global switch_lights
        current_tag = None
        previous_tag = None
        mp3state = None
        nfc = nfctags.Tags()
        nfc.load_tags()
        tags = nfctags.resolve_for_room(nfc.tags, ROOM)
        logger.info('Pad room: %s' % ROOM)
        self.base = Dimensions()
        logger.info("Lego Dimensions base activated.")
        self.initMp3()
        try:
            switch_lights = tags['lights']
        except KeyError:
            switch_lights = True
        logger.info('Lightshow is %s' % switch_lights) #("disabled", "enabled")[switch_lights])
        homepod.warm_up()  # discover AirPlay devices now, not on the first tap
        if switch_lights:
            self.base.switch_pad(0,self.GREEN)
        else:
            self.base.switch_pad(0,self.OFF)
        while True:
            if self.base.reconnect():
                # A pad just came (back) online: give it its idle colour.
                if switch_lights:
                    self.base.switch_pad(0, self.GREEN)
                else:
                    self.base.switch_pad(0, self.OFF)
            tag = self.base.update_nfc()
            if tag:
                status = tag.split(':')[0]
                pad = int(tag.split(':')[1])
                identifier = tag.split(':')[2]
                if status == 'removed':
                    if identifier == current_tag:
                        try:
                            self.lightshowThread.do_run = False
                            self.lightshowThread.join()
                        except AttributeError:
                            pass  # No lightshow thread running
                        self.pauseMp3()
                        homepod.stop()
                        if spotify.activated():
                            spotify.pause()
                if status == 'added':
                    # Optional hook: any tag placed on the pad runs this
                    # command (e.g. wake the room's wall display) on its own
                    # thread, so a slow hook never delays the music and the
                    # child is always reaped (no zombies).
                    on_tag_cmd = os.environ.get('MUSICFIG_ON_TAG_CMD')
                    if on_tag_cmd:
                        run_tag_hook(on_tag_cmd, identifier, pad)
                    if switch_lights:
                        self.base.switch_pad(pad = pad, colour = self.BLUE)

                    # Reload the tags config file, resolved for this room
                    nfc.load_tags()
                    tags = nfctags.resolve_for_room(nfc.tags, ROOM)
                    try:
                        mp3_dir = tags['mp3_dir'] + '/'
                    except KeyError:
                        mp3_dir = os.path.dirname(os.path.abspath(__file__)) + '/../music/'
                    ##logger.debug(mp3_dir)

                    # Stop any current songs and light shows
                    try:
                        self.lightshowThread.do_run = False
                        self.lightshowThread.join()
                    except AttributeError:
                        pass  # No lightshow thread running
                    homepod.stop()

                    if (identifier in tags['identifier']):
                        if current_tag is None:
                            previous_tag = identifier
                        else:
                            previous_tag = current_tag
                        current_tag = identifier
                        # A tag has been matched
                        if ('playlist' in tags['identifier'][identifier]):
                            playlist = tags['identifier'][identifier]['playlist']
                            if ('shuffle' in tags['identifier'][identifier]):
                                shuffle = True
                            else:
                                shuffle = False
                            self.playPlaylist(playlist, mp3_dir, shuffle)
                        if ('mp3' in tags['identifier'][identifier]):
                            filename = tags['identifier'][identifier]['mp3']
                            self.playMp3(filename, mp3_dir)
                        if ('slack' in tags['identifier'][identifier]):
                            webhook.Requests.post(tags['slack_hook'],{'text': tags['identifier'][identifier]['slack']})
                        if ('command' in tags['identifier'][identifier]):
                            command = tags['identifier'][identifier]['command']
                            logger.info('Running command: %s' % command)
                            try:
                                # Use subprocess for safer command execution
                                # shell=True required for complex commands, but input is from trusted config
                                subprocess.run(command, shell=True, check=False, timeout=30)
                            except subprocess.TimeoutExpired:
                                logger.warning('Command timed out after 30s: %s' % command)
                            except Exception as e:
                                logger.error('Command execution failed: %s' % e)
                        if ('spotify' in tags['identifier'][identifier]) and spotify.activated():
                            if current_tag == previous_tag:
                                self.startLightshow(spotify.resume())
                                continue
                            try:
                                position_ms = int(tags['identifier'][identifier]['position_ms'])
                            except (KeyError, ValueError):
                                position_ms = 0
                            self.stopMp3()
                            duration_ms = spotify.spotcast(tags['identifier'][identifier]['spotify'],
                                                           position_ms)
                            if duration_ms > 0:
                                self.startLightshow(duration_ms)
                            else:
                                self.base.flash_pad(pad = pad, on_length = 10, off_length = 10,
                                                    pulse_count = 6, colour = self.RED)
                        if ('spotify' in tags['identifier'][identifier]) and not spotify.activated():
                            current_tag = previous_tag
                        # Disney+ / Apple TV deep link
                        if ('disney' in tags['identifier'][identifier]) and appletv.activated():
                            self.stopMp3()
                            appletv.load_config(tags)
                            self.switchHdmiToAppleTv(tags)
                            disney_url = tags['identifier'][identifier]['disney']
                            logger.info('Launching Disney+: %s' % disney_url)
                            if appletv.launch_disney(disney_url):
                                self.base.switch_pad(pad, self.PURPLE)
                                # Wait for Disney+ to load movie page, then press OK to play
                                time.sleep(4)
                                samsungtv.send_key("OK")
                            else:
                                self.base.flash_pad(pad=pad, on_length=10, off_length=10,
                                                   pulse_count=6, colour=self.RED)
                        # Netflix deep link
                        if ('netflix' in tags['identifier'][identifier]) and appletv.activated():
                            self.stopMp3()
                            appletv.load_config(tags)
                            self.switchHdmiToAppleTv(tags)
                            netflix_url = tags['identifier'][identifier]['netflix']
                            logger.info('Launching Netflix: %s' % netflix_url)
                            if appletv.launch_netflix(netflix_url):
                                self.base.switch_pad(pad, self.PURPLE)
                                # Wait for Netflix to load (profile selection + content load)
                                time.sleep(6)
                                # First OK might dismiss profile selection or start content
                                samsungtv.send_key("OK")
                                time.sleep(1)
                                # Second OK to start playing
                                samsungtv.send_key("OK")
                            else:
                                self.base.flash_pad(pad=pad, on_length=10, off_length=10,
                                                   pulse_count=6, colour=self.RED)
                        # YouTube deep link
                        if ('youtube' in tags['identifier'][identifier]) and appletv.activated():
                            self.stopMp3()
                            appletv.load_config(tags)
                            self.switchHdmiToAppleTv(tags)
                            youtube_url = tags['identifier'][identifier]['youtube']
                            logger.info('Launching YouTube: %s' % youtube_url)
                            if appletv.launch_youtube(youtube_url):
                                self.base.switch_pad(pad, self.PURPLE)
                                # Wait for YouTube to load, then press OK to play
                                time.sleep(4)
                                samsungtv.send_key("OK")
                            else:
                                self.base.flash_pad(pad=pad, on_length=10, off_length=10,
                                                   pulse_count=6, colour=self.RED)
                        # HomePod / AirPlay streaming
                        if ('airplay' in tags['identifier'][identifier]):
                            self.stopMp3()
                            homepod.load_config(tags)
                            airplay_file = tags['identifier'][identifier]['airplay']
                            # Check for optional target HomePod
                            airplay_target = tags['identifier'][identifier].get('homepod', None)
                            logger.info('Streaming to HomePod: %s' % airplay_file)
                            if homepod.stream_file(airplay_file, airplay_target):
                                self.base.switch_pad(pad, self.LBLUE)  # Light blue for audio
                            else:
                                self.base.flash_pad(pad=pad, on_length=10, off_length=10,
                                                   pulse_count=6, colour=self.RED)
                        # Yoto card -> HomePod: signed track URLs from the Yoto MCP,
                        # streamed in order; lifting the tag stops it (homepod.stop()).
                        if ('yoto_airplay' in tags['identifier'][identifier]):
                            self.stopMp3()
                            homepod.load_config(tags)
                            card_id = tags['identifier'][identifier]['yoto_airplay']
                            airplay_target = tags['identifier'][identifier].get('homepod', None)
                            logger.info('Yoto card %s -> HomePod %s' % (card_id, airplay_target or 'default'))
                            urls = [u for _t, u in yoto_tracks.fetch_tracks(card_id)]
                            if urls and homepod.stream_urls(urls, airplay_target):
                                self.base.switch_pad(pad, self.LBLUE)
                            else:
                                self.base.flash_pad(pad=pad, on_length=10, off_length=10,
                                                   pulse_count=6, colour=self.RED)
                        # Xbox control - power on Xbox and switch TV input
                        if ('xbox' in tags['identifier'][identifier]):
                            self.stopMp3()
                            xboxctl.load_config(tags)
                            logger.info('Powering on Xbox...')
                            # Switch TV via menu: HOME → LEFT → DOWN → RIGHT → DOWN×3 → OK
                            samsungtv.load_config(tags)
                            if samsungtv.configured():
                                logger.info('Switching TV to Xbox via menu navigation...')
                                # Navigate Samsung TV to Connected Devices → Xbox
                                nav_keys = [
                                    ("HOME", 1.5),
                                    ("LEFT", 0.5),
                                    ("DOWN", 0.5),   # To Connected Devices
                                    ("RIGHT", 0.5),  # Step into list
                                    ("DOWN", 0.3),
                                    ("DOWN", 0.3),
                                    ("DOWN", 0.3),   # Xbox (4th item)
                                    ("OK", 0.0)
                                ]
                                for key, delay in nav_keys:
                                    if not samsungtv.send_key(key):
                                        logger.warning('TV key %s failed' % key)
                                    if delay > 0:
                                        time.sleep(delay)
                                logger.info('TV switch to Xbox complete')
                            # Power on Xbox
                            if xboxctl.sync_power_on():
                                self.base.switch_pad(pad, self.OLIVE)  # Yellow-green for Xbox
                                # Launch specific app if provided
                                xbox_app = tags['identifier'][identifier].get('xbox_app', None)
                                if xbox_app:
                                    time.sleep(3)  # Wait for Xbox to wake
                                    logger.info('Launching Xbox app: %s' % xbox_app)
                                    xboxctl.sync_launch_app(xbox_app)
                            else:
                                self.base.flash_pad(pad=pad, on_length=10, off_length=10,
                                                   pulse_count=6, colour=self.RED)
                        # Yoto player - play card from library
                        if ('yoto' in tags['identifier'][identifier]):
                            self.stopMp3()
                            yoto.load_config(tags)
                            card_id = tags['identifier'][identifier]['yoto']
                            # Optional: specify which player
                            player_id = tags['identifier'][identifier].get('yoto_player', None)
                            logger.info('Playing Yoto card: %s' % card_id)
                            if yoto.sync_play_card(card_id, player_id):
                                self.base.switch_pad(pad, self.PINK)  # Pink for Yoto
                            else:
                                self.base.flash_pad(pad=pad, on_length=10, off_length=10,
                                                   pulse_count=6, colour=self.RED)
                    else:
                        # Unknown tag. Display UID.
                        logger.info('Discovered new tag: %s' % identifier)
                        self.base.switch_pad(pad, self.RED)
