#!/usr/bin/env python

import ctypes
import time
from enum import Enum
import threading
import queue
from queue import Empty
import mpg123
import logging
import random
import os
import sys
from ctypes.util import find_library

logger = logging.getLogger(__name__)

# Windows: Set MPG123_MODDIR to find audio output plugins
if sys.platform == 'win32':
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plugins_dir = os.path.join(base_dir, 'mpg123-1.32.10-x86-64', 'plugins')
    if os.path.exists(plugins_dir):
        os.environ['MPG123_MODDIR'] = plugins_dir

class mpg123_frameinfo(ctypes.Structure):
    _fields_ = [
        ('version',     ctypes.c_ubyte),
        ('layer',       ctypes.c_int),
        ('rate',        ctypes.c_long),
        ('mode',        ctypes.c_ubyte),
        ('mode_ext',    ctypes.c_int),
        ('framesize',   ctypes.c_int),
        ('flags',       ctypes.c_ubyte),
        ('emphasis',    ctypes.c_int),
        ('bitrate',     ctypes.c_int),
        ('abr_rate',    ctypes.c_int),
        ('vbr',         ctypes.c_ubyte)
    ]


class ExtMpg123(mpg123.Mpg123):
    SEEK_SET = 0
    SEEK_CURRENT = 1
    SEEK_END = 2

    def __init__(self, filename=None, library_path=None):
        if not library_path:
            library_path = find_library('mpg123')

        if not library_path:
            library_path = find_library('libmpg123-0')

        # Windows: Try loading from musicfig directory
        if not library_path:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            dll_path = os.path.join(base_dir, 'libmpg123-0.dll')
            if os.path.exists(dll_path):
                library_path = dll_path

        if not library_path:
            raise self.LibInitializationException('libmpg123 not found.')

        super().__init__(filename, library_path)

    def open(self, filename):
        errcode = self._lib.mpg123_open(self.handle, filename.encode())
        if errcode != mpg123.OK:
            raise self.OpenFileException(self.plain_strerror(errcode))

    def timeframe(self, tsec):
        t = ctypes.c_double(tsec)
        errcode = self._lib.mpg123_timeframe(self.handle, t)
        if errcode >= mpg123.OK:
            return errcode
        else:
            raise self.LengthException(self.plain_strerror(errcode))

    def seek_frame(self, pos, whence=SEEK_SET):
        px = ctypes.c_long(pos)
        errcode = self._lib.mpg123_seek_frame(self.handle, px, whence)
        if errcode >= mpg123.OK:
            return errcode
        else:
            raise self.LengthException(self.plain_strerror(errcode))

    def tellframe(self):
        errcode = self._lib.mpg123_tellframe(self.handle)
        if errcode >= mpg123.OK:
            return errcode
        else:
            raise self.LengthException(self.plain_strerror(errcode))

    def info(self):
        px = mpg123_frameinfo()
        errcode = self._lib.mpg123_info(self.handle, ctypes.pointer(px))
        if errcode != mpg123.OK:
            raise self.ID3Exception(self.plain_strerror(errcode))
        return px

    _samples_per_frame = [
        # version 1, layers 1,2,3
        [384, 1152, 1152],
        # version 2, layers 1,2,3
        [384, 1152, 576],
        # version 2.5, layers 1,2,3
        [384, 1152, 576]
    ]

    def frame_seconds(self, frame):
        info = self.info()
        return ExtMpg123._samples_per_frame[info.version][info.layer - 1] * frame / info.rate

class ExtOut123(mpg123.Out123):
    def __init__(self, library_path=None):
        # Windows: Try loading from musicfig directory
        if not library_path:
            library_path = find_library('out123')
        if not library_path:
            library_path = find_library('libout123-0')
        if not library_path:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            dll_path = os.path.join(base_dir, 'libout123-0.dll')
            if os.path.exists(dll_path):
                library_path = dll_path
        # Open the output with an explicit driver instead of libout123's
        # auto-pick. With no audio sink present (Pi with the HDMI switched to
        # another input) the auto-pick falls through to the JACK module, which
        # segfaults the whole service; ALSA just returns an error we can handle.
        driver = os.environ.get('MUSICFIG_AUDIO_DRIVER', 'alsa' if sys.platform.startswith('linux') else None)
        device = os.environ.get('MUSICFIG_AUDIO_DEVICE') or None
        self.handle = None
        if not self._lib:
            self._lib = self.init_library(library_path)
        self._lib.out123_new.restype = ctypes.c_void_p
        self.c_handle = self._lib.out123_new()
        self.handle = ctypes.c_void_p(self.c_handle)
        errcode = self._lib.out123_open(self.handle,
                                        ctypes.c_char_p(driver.encode() if driver else None),
                                        ctypes.c_char_p(device.encode() if device else None))
        if errcode != mpg123.OK:
            raise self.OpenException(self.plain_strerror(errcode))

    def pause(self):
        self._lib.out123_pause(self.handle)

    def resume(self):
        self._lib.out123_continue(self.handle)

    def stop(self):
        self._lib.out123_stop(self.handle)


class AudioOutputError(Exception):
    """No usable audio output right now (e.g. HDMI not connected)."""


class LazyOut123:
    """Opens the audio output on first use and survives it being absent.

    A kiosk Pi may boot with nothing on its HDMI port (shared screen behind a
    switch, TV off). Opening ALSA then fails, and musicfig must still run so
    the pad, the lights and the wake-on-tag hook keep working; the output is
    retried on every play attempt, so audio appears as soon as a sink does.
    """
    _RETRY_LOG_EVERY = 60  # seconds between "still no audio" warnings

    def __init__(self):
        self._out = None
        self._last_warn = 0.0

    def _ensure(self):
        if self._out is None:
            try:
                self._out = ExtOut123()
                logger.info("Audio output opened (%s)" % os.environ.get('MUSICFIG_AUDIO_DRIVER', 'alsa'))
            except Exception as e:  # OpenException, LibInitializationException, OSError
                now = time.monotonic()
                if now - self._last_warn > self._RETRY_LOG_EVERY:
                    logger.warning("No audio output yet (%s) - is a display/HDMI connected? Will retry on next play." % e)
                    self._last_warn = now
                raise AudioOutputError(str(e))
        return self._out

    def start(self, rate, channels, encoding):
        return self._ensure().start(rate, channels, encoding)

    def play(self, frame):
        return self._ensure().play(frame)

    def pause(self):
        if self._out is not None:
            self._out.pause()

    def resume(self):
        if self._out is not None:
            self._out.resume()

    def stop(self):
        if self._out is not None:
            self._out.stop()


class PlayerState(Enum):
    UNINITALISED = 0
    INITALISED = 1,  # Drivers loaded
    LOADED = 2,  # MP3 loaded of x seconds
    READY = 3,  # Ready to play a time x
    PLAYING = 4,  # Playing a file at time x
    PAUSED = 5,  # Paused at time x
    FINISHED = 6,  # Finished
    PLAYLIST = 7   #Playing a playlist


class Player:

    class Command(Enum):
        LOAD = 1,
        PLAY = 2,
        PAUSE = 3,
        PLAYLIST = 4,
        SEEK = 5

    class IllegalStateException(Exception):
        pass

    def __init__(self):
        self.mp3 = ExtMpg123()
        self.out = LazyOut123()

        self.command_queue = queue.Queue(maxsize=1)
        self.event_queue = queue.Queue()
        self.playlist_queue = queue.Queue()

        self._current_state = PlayerState.INITALISED
        self.event_queue.put((self._current_state, None))
        threading.Thread(target=self._run_player, daemon=True, name="Player").start()

    def _run_player(self):
        while True:
            command = self.command_queue.get(block=True, timeout=None)
            if command[0] == Player.Command.LOAD:
                if self._current_state in [PlayerState.PLAYING]:
                    self.out.pause()

                self.mp3.open(command[1])
                tf = self.mp3.frame_length()
                self.track_length = self.mp3.frame_seconds(tf)
                self.frames_per_second = tf // self.track_length
                self.update_per_frame_count = round(self.frames_per_second / 5)
                self.to_time = self.track_length
                self._set_state(PlayerState.LOADED, self.track_length)
                self._set_state(PlayerState.READY, 0)

            elif command[0] == Player.Command.PLAY:

                if command[1] is not None:
                    tf = self.mp3.timeframe(command[1])
                    self.mp3.seek_frame(tf)
                self.to_time = self.track_length if command[2] is None else command[2]

                if self._current_state in [PlayerState.READY, PlayerState.PLAYING]:
                    self._play()

                elif self._current_state in [PlayerState.PAUSED]:
                    self.out.resume()
                    self._play()

            elif command[0] == Player.Command.PAUSE:
                self.out.pause()
                current_frame = self.mp3.tellframe()
                current_time = self.mp3.frame_seconds(current_frame)
                self._set_state(PlayerState.PAUSED, current_time)

            elif command[0] == Player.Command.SEEK:
                if self._current_state in \
                            [PlayerState.READY, PlayerState.PLAYING, PlayerState.PAUSED, PlayerState.FINISHED]:

                    tf = self.mp3.timeframe(command[1])
                    self.mp3.seek_frame(tf)
                    if self._current_state == PlayerState.FINISHED:
                        self._set_state(PlayerState.PAUSED, command[1])
                    else:
                        self.event_queue.put((self._current_state, command[1]))

                if self._current_state in [PlayerState.PLAYING]:
                    self._play()
            elif command[0] == Player.Command.PLAYLIST:
                if self._current_state in [PlayerState.PLAYING]:
                    self.out.pause()

                for song in command[1]:
                    self.playlist_queue.put(song)

                self._set_state(PlayerState.PLAYLIST)
                self._play_playlist()
            else:
                # what happened?
                pass

    def _play_playlist(self):
        while True:
            try:
                song_mp3 = self.playlist_queue.get(block=False)
                self.mp3.open(song_mp3)
                tf = self.mp3.frame_length()
                self.track_length = self.mp3.frame_seconds(tf)
                self.frames_per_second = tf // self.track_length
                self.update_per_frame_count = round(self.frames_per_second / 5)
                self.to_time = self.track_length
                fc = self.mp3.tellframe()
                current_time = self.mp3.frame_seconds(fc)
                self._set_state(PlayerState.PLAYING, current_time)

                to_frame = self.mp3.timeframe(self.to_time) + 1

                for frame in self.mp3.iter_frames(self.out.start):
                    self.out.play(frame)

                    fc += 1
                    if fc > to_frame:
                        current_time = self.mp3.frame_seconds(self.mp3.tellframe())
                        self._set_state(PlayerState.PAUSED, current_time)
                        return

                    if fc % self.update_per_frame_count == 0:
                        current_time = self.mp3.frame_seconds(self.mp3.tellframe())
                        self.event_queue.put((PlayerState.PLAYING, current_time))

                    if not self.command_queue.empty():
                        return
            except AudioOutputError:
                break
            except Empty:
                break
        self._set_state(PlayerState.FINISHED)

    def _play(self):
        fc = self.mp3.tellframe()
        current_time = self.mp3.frame_seconds(fc)
        self._set_state(PlayerState.PLAYING, current_time)

        to_frame = self.mp3.timeframe(self.to_time) + 1

        try:
            for frame in self.mp3.iter_frames(self.out.start):
                self.out.play(frame)

                fc += 1
                if fc > to_frame:
                    current_time = self.mp3.frame_seconds(self.mp3.tellframe())
                    self._set_state(PlayerState.PAUSED, current_time)
                    return

                if fc % self.update_per_frame_count == 0:
                    current_time = self.mp3.frame_seconds(self.mp3.tellframe())
                    self.event_queue.put((PlayerState.PLAYING, current_time))

                if not self.command_queue.empty():
                    return
        except AudioOutputError:
            pass  # already logged; finish quietly, retried on the next tag
        self._set_state(PlayerState.FINISHED)

    def _set_state(self, state, param=None):
        self._current_state = state
        self.event_queue.put((state, param))

    def open(self, filename):
        self.command_queue.put((Player.Command.LOAD, filename))

    def pause(self):
        self.command_queue.put((Player.Command.PAUSE, None))

    def play(self, from_time=None, to_time=None):
        self.command_queue.put((Player.Command.PLAY, from_time, to_time))

    def seek(self, tsec):
        self.command_queue.put((Player.Command.SEEK, tsec))

    def playlist(self, filename_list):
        self.command_queue.put((Player.Command.PLAYLIST, filename_list))
