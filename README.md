# Musicfig - Raspberry Pi fleet edition

> **This is a fork of [meltaxa/musicfig](https://github.com/meltaxa/musicfig)** (MIT), extended so that a
> LEGO Dimensions pad can live on every Raspberry Pi wall display in a house: tap a figure, and the room's
> screen wakes, a song plays on the panel or streams to that room's HomePod, or a film launches on the
> Apple TV. The same figure can do something different in every room.
>
> Everything in this fork was built with [Claude Code](https://claude.com/claude-code) working alongside the
> owner, in the open: each feature shipped through a branch, a pull request, an adversarial review pass and a
> test on real hardware before it was merged. The original project's documentation follows further down.

## What the fork adds

| Area | What it does |
| --- | --- |
| **Pi install recipe** | `scripts/pi/install.sh`: idempotent install on Raspberry Pi OS Lite (Bookworm/Trixie): dedicated `musicfig` system user, Python venv, systemd unit, udev rule for the pad, config in `/etc/musicfig`, music and logs in `/var/lib/musicfig`. Re-run to update. Coexists with a kiosk browser on the same Pi. |
| **Wake the display on tap** | `MUSICFIG_ON_TAG_CMD` runs a command on its own thread whenever a tag lands, for example `gsd-screen on` to un-blank the room's wall panel. |
| **Pad hot-plug** | The pad can be missing at boot or unplugged while running: one log line, quiet retries every 3 s, automatic reconnect, and the idle colour restored. No log spam, no crash. |
| **Headless-safe audio** | The audio device opens lazily and a missing sink (HDMI switched away, no speaker) never brings the service down. |
| **HomePod / AirPlay streaming** | `airplay:` tags stream a local file to a HomePod through [pyatv](https://pyatv.dev). Streaming runs on a background loop, so the pad keeps reading; lifting the figure stops the stream. |
| **Yoto card to HomePod** | `yoto_airplay: <card id>` streams a Yoto card (audiobooks, MYO uploads) to the room's HomePod. The Pi asks a small Yoto MCP service for the card's signed track URLs at tap time (one shared secret per household, never a Yoto token on the Pi) and pyatv streams them in order. Lift the figure to stop. |
| **Room-aware tags** | Each Pi knows its room (`MUSICFIG_ROOM`, default: hostname). A tag can carry per-room overrides and `homepod: "@room"` targets that room's speaker. |
| **Optional integrations** | Apple TV (Disney+, Netflix, YouTube deep links), HomePod, Hue Sync Box and Xbox are optional imports: a Pi without them still runs. `install.sh` installs the extras by default (`MUSICFIG_EXTRAS=no` to skip). |
| **Tests** | `python -m unittest` (see `tests/`). |

## How a house looks

```
                 ┌──────────── each room ────────────┐
   figure ──tap──►  LEGO pad ──USB──► Raspberry Pi 5  │
                 │                    │  musicfig     │
                 │        ┌───────────┼────────────┐  │
                 │        ▼           ▼            ▼  │
                 │   wall panel   panel speaker   LAN │
                 │   (wake/blank)  (MP3, ALSA)      │  │
                 └──────────────────────────────────┼──┘
                                                    ▼
                          HomePod (AirPlay) · Apple TV (pyatv) · TV input switch
```

## Quick start on a Raspberry Pi

```bash
# on a fresh Raspberry Pi OS Lite (64-bit), as a user with sudo:
git clone https://github.com/butterflysilver/musicfig.git
cd musicfig
sudo bash scripts/pi/install.sh          # MUSICFIG_BRANCH=<branch> to pin a branch
sudo tail -f /var/lib/musicfig/musicfig.log
```

Put MP3s in `/var/lib/musicfig/music`, map tags in `/etc/musicfig/tags.yml`, tap a figure. The log prints the
UID of every new tag it sees. Full details, including the wall-display wake hook and how it coexists with a
kiosk, are in [`scripts/pi/README.md`](scripts/pi/README.md).

## Configuration examples (demo data)

All identifiers below are the upstream sample tags or made up; nothing here is a real household config.

```yaml
mp3_dir: /var/lib/musicfig/music
lights: on

# Which HomePod belongs to which room (room = MUSICFIG_ROOM, i.e. the Pi's hostname).
room_homepods:
  living-room: Living Room HomePod
  playroom: Playroom HomePod

identifier:
  05631b62124:
    name: Peter Pan
    playlist: peterpan            # a folder of MP3s under mp3_dir, played on the panel speaker
    shuffle: on

  04ec806a0b4080:
    name: Gramatik
    mp3: justjammin.mp3

  04aabbccddee80:                 # made-up UID
    name: Sleepy song (room aware)
    airplay: lullaby.mp3          # default: stream to this room's HomePod...
    homepod: "@room"
    rooms:
      kitchen:                    # ...except in the kitchen, which has no HomePod:
        airplay: null             # null drops an inherited key
        mp3: lullaby.mp3          # play on the panel speaker instead

  04ffeeddccbb81:                 # made-up UID
    name: Cinderella
    disney: https://www.disneyplus.com/movies/cinderella/VJPw3bEy9iHj   # launches on the Apple TV
```

Notes:

* HomePods stream without pairing when the Home app's *Allow Speaker & TV Access* is set to *Everyone* (or
  *Anyone on the Same Network*). Apple TVs need a one-time PIN pairing; see the pyatv docs.
* A room with no entry in `room_homepods` makes `"@room"` resolve to nothing, so the tag falls back to its
  other actions rather than playing on the wrong speaker.

## Security and privacy

* The Yoto shared secret lives in `/etc/musicfig/yoto-tracks.key` (root-owned, readable by the service user only). Copy it with `scp`; it is never in the repo or the unit file.
* `tags.yml` and `config.py` are **git-ignored on purpose**: they hold pairing credentials and API tokens.
  Never commit them, never paste them into an issue. Copy them to a Pi with `scp`.
* The service runs as an unprivileged system user; the pad is the only USB device it can open (udev rule).
* Nothing listens on the internet. The web UI binds to localhost; AirPlay and Apple TV traffic stays on the LAN.
* The examples in this README are demo data. Real device names, addresses and identifiers live only in the
  private config.

## How this was built

This fork is a working example of "AI pair-built" home automation:

1. Each change starts as a spec in an issue tracker, is implemented on a branch by Claude Code, and goes
   through a hostile self-review pass (a stop hook that asks for a senior-dev critique before finishing).
2. Everything is verified on the actual hardware before it counts as done: pads unplugged mid-play,
   Pis rebooted headless, HomePods listened to.
3. Bugs found that way became fixes here: an unquoted systemd `Environment=` that split on spaces, a JACK
   segfault when the HDMI sink vanished, a libusb device list that never sees a pad re-appear after a sysfs
   rebind, and a pad that was simply faulty.

Contributions welcome. The upstream project appears unmaintained; generic fixes from here are offered
upstream as pull requests, and this fork is kept as the living version.

---

*Original project documentation follows.*

# Musicfig (upstream README)
<p/>
Make your LEGO Minifigures play music using a Raspberry Pi and a LEGO Dimensions toy pad.
<p align="center">
  <img src="https://cdn-images-1.medium.com/max/800/1*v3m7mg7Y_Vzy2y8O8gKXMQ.jpeg" alt="Musicfig rig"/>
</p>
<p/>
Leveraging the NFC chip in a LEGO Dimensions tag, Minifigues can be assigned songs and cue music like a jukebox.
<p/>
Jukebox control is not limited to LEGO minifigures. Disney Infinity characters, Amiibos, Skylanders, NFC tags, stickers or cards can become a Musicfig.
<p/>
During song play, the LEGO Dimensions pad will light up for the duration of the song.
<p/>
For Spotify users, the album art of the currently playing track is displayed on your local Musicfig app site, as demonstrated on the 
<a href="https://nowplaying.musicfig.com?github">https://nowplaying.musicfig.com</a> site: <p/>
<p align="center">
  <!--- 
  Github will by default use it's Camo CDN to cache images (https://github.blog/2014-01-28-proxying-user-images/). 
  To override this, on the origin web server add the header Cache-Control no-cache. Also if you are using 
  Cloudflare set the Browser Cache TTL to respect existing headers. The nowplaying.png image is a Puppeteer 
  screenshot and updated every 5 minutes displaying what Meltaxa is actually listening to on the Musicfig.
  --->
  <img src="https://musicfig.com/images/nowplaying.png?github" alt="Musicfig now playing" width=60%/><br>
Musicfig's now playing page.
</p>

# You will require

| Hardware | NFC tags |
| --- | --- |
| <img src="https://cdn-images-1.medium.com/max/400/1*CAcSKjlKsD9Ld-iuKsCY-Q.jpeg"><br><ul><li>Raspberry Pi with Python 3.8 installed.</li><li>LEGO Dimensions pad from either a PS3, PS4 or Wii game console. The Xbox version is not supported.</li></ul>| <img src="https://cdn-images-1.medium.com/max/400/1*UtAav5Iu2iOGxoS7a1nzTg.png"><br>From Lego Dimensions character discs, Disney Infinity character toys, NFC NTAG213 tags, stickers or cards. |

To play music you can use the following **options**:

* MP3 files;
* A Spotify Premium subscription account. For an enhanced experience, Spotify is recommended but not required.

# Feature list

* Support LEGO Dimensions toy pads for PlayStation and Wii.
* A lightshow will display on the LEGO Dimensions pad during song play.
    * The lightshow can be enabled or disabled via the tags.yml file. Default: lights = on.
* Play MP3 files.
* Play Spotify music.
    * The Musicfig web application will show in real time the currently playing Spotify track's album art.
* Musicfig allows automatic offline mode, for only MP3 music playing.
* Removing an active tag during song play will pause the track. Adding it back will resume play.

# Install

* Complete installation instructions is available in the Medium article "[My LEGO Minifigures Play Spotify](https://medium.com/@mellican/my-lego-minifigures-play-spotify-dc397e83280e)".

# Quick Install (without Spotify)

This allows Musicfig to play in offline mode, by accessing local MP3 files. 

**Raspberry Pi OS Bookworm/Trixie (venv, system user, systemd, coexists with the GSD kiosk):**
use the maintained recipe in [`scripts/pi/README.md`](scripts/pi/README.md) instead of the steps below.

Firstly, connect your LEGO Dimensions toy pad to the Raspberry Pi via the USB port.

Now connect the speakers to the Raspberry Pi.

Follow these steps to install and config the Musicfig software:

* Install Python 3.8+.
* Clone this repository and run the install.sh script. Musicfig will start automatically.
* Copy your mp3 files to the music folder.
* Place your first tag on the pad and watch the console or musicfig.log as the app discovers the UID value.
* Edit the tags.yml file with the UID and the mp3 file to be played.
* Place the tag off and on again. 
* A track should now start playing locally.

# Using Docker

A Musicfig docker image is available from Docker hub. Before using the image, you will need to bootstrap
the Raspberry Pi to configure the LEGO usb device which the container will need access to:

```
curl -L https://raw.githubusercontent.com/meltaxa/musicfig/master/install.sh | bash -s -- --docker
```

The bootstrap script also downloads the example config.py and tags.yml files. Place these in a directory
of your choosing, say /home/pi/musicfig and update these files accordingly. See the complete install
instructions in the Medium article 
"[My LEGO Minifigures Play Spotify](https://medium.com/@mellican/my-lego-minifigures-play-spotify-dc397e83280e)" 
for Spotify configuration steps.

Next, find the USB bus and device mappings for the LEGO pad. Make sure it is plugged in. Look for Id "0e6f:0241":
```
lsusb | grep 0e6f:0241
```

Example output:
```
Bus 001 Device 008: ID 0e6f:0241 Logic3 
```

When running Docker, the device path will correspond to the bus and device numbers. For example, Bus 001 and Device 008 would correspond to: /dev/bus/usb/001/008.

Run Musicfig Docker example (change the config directory mount and usb device bus accordingly):
```
docker run -v /home/pi/musicfig:/config -p 5000:5000 --device=/dev/bus/usb/001/008 --device=/dev/snd meltaxa/musicfig
```
In this example, the /home/pi/musicfig is the directory where you store the config.py and tags.yml files.

# Stopping and Starting

To stop Musicfig:
```
sudo systemctl stop musicfig
```

To Start Musicfig:
```
sudo systemctl start musicfig
```

# Updating

* Re-run the install.sh script to pull down the latest code. 

# Everything is Awesome!
<p align="center">
  <img src="https://musicfig.com/images/1.jpg" width=80%>
</p>
Send feedback and photos of your Musicfigs and rigs over in the <a href="https://github.com/meltaxa/musicfig/discussions">Discussions</a>
section.
