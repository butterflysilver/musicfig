#!/usr/bin/env python

from app import webhook
from mutagen.mp3 import MP3
import app.spotify as spotify
import app.appletv as appletv
import app.huesyncbox as huesyncbox
import app.samsungtv as samsungtv
import app.homepod as homepod
import app.xboxctl as xboxctl
import app.tags as nfctags
import binascii
import logging
import os
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

class Dimensions():

    def __init__(self):
        try:
            self.dev = self.init_usb()
        except (ValueError, usb.core.USBError) as e:
            logger.error('Failed to initialize USB device: %s' % e)
            self.dev = None

    def init_usb(self):
        dev = usb.core.find(idVendor=0x0e6f, idProduct=0x0241)

        if dev is None:
            logger.error('Lego Dimensions pad not found')
            raise ValueError('Device not found')

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

        try:
            self.dev.write(1, message)
        except usb.core.USBError as e:
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
        try:
            inwards_packet = self.dev.read(0x81, 32, timeout = 100)
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
            logger.warning('USB error: %s' % e)
            return
        except Exception as e:
            logger.warning('NFC read error: %s' % e)
            return

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
        tags = nfc.tags
        self.base = Dimensions()
        logger.info("Lego Dimensions base activated.")
        self.initMp3()
        try:
            switch_lights = tags['lights']
        except KeyError:
            switch_lights = True
        logger.info('Lightshow is %s' % switch_lights) #("disabled", "enabled")[switch_lights])
        if switch_lights:
            self.base.switch_pad(0,self.GREEN)
        else:
            self.base.switch_pad(0,self.OFF)
        while True:
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
                        if spotify.activated():
                            spotify.pause()
                if status == 'added':
                    if switch_lights:
                        self.base.switch_pad(pad = pad, colour = self.BLUE)

                    # Reload the tags config file
                    nfc.load_tags()
                    tags = nfc.tags
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
                    else:
                        # Unknown tag. Display UID.
                        logger.info('Discovered new tag: %s' % identifier)
                        self.base.switch_pad(pad, self.RED)
