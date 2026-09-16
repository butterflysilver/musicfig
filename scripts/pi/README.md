# musicfig on a Raspberry Pi (Bookworm / Trixie)

`install.sh` puts musicfig on a Raspberry Pi as a systemd service under its own
system user, so it can share the box with the GSD wall-display kiosk
(gsd-cloud `scripts/pi-kiosk/provision.sh`: user `kiosk`, cage + Chromium on
tty1). musicfig uses a different user, no tty, no display, and binds port 5000
on localhost only, so the two never touch. Re-running the script is the update
path (`git pull --ff-only`, `pip install`, service restart).

Tracker: [life-ops #62](https://github.com/butterflysilver/life-ops/issues/62).

## Prerequisites

* A Pi already provisioned by the gsd-cloud kiosk recipe, or any Raspberry Pi
  OS Lite (64-bit) Bookworm or Trixie with SSH and a sudo-capable user.
  The read-only overlay must be OFF (the recipe writes to `/opt`, `/etc`, `/var/lib`).
* A LEGO Dimensions toy pad plugged into USB. **Only model 3000061482 (the
  PS3 / PS4 / Wii U pad, USB `0e6f:0241`) works.** The Xbox 360 pad
  (3000061480) uses a different protocol and is not supported.
* Internet on the Pi for apt, git and pip.

## Install / update

From your laptop (Git checks the script out with LF thanks to `.gitattributes`):

```
scp scripts/pi/install.sh kiosk@<room>.local:
ssh -t kiosk@<room>.local 'bash install.sh'
```

If the Pi complains about `\r` (`$'\r': command not found`), the script picked
up CRLF somewhere; fix it on the Pi with `sed -i 's/\r$//' install.sh`.

To test a branch before it is merged: `MUSICFIG_BRANCH=feat/pi-install-recipe bash install.sh`.

## File map

| Path | What |
|------|------|
| `/opt/musicfig` | git checkout (owner `musicfig`) + `.venv/` |
| `/etc/musicfig/config.py` | Flask/Spotify settings, `0640 root:musicfig` (holds secrets) |
| `/etc/musicfig/tags.yml` | tag UID -> action map; `mp3_dir` preset to `/var/lib/musicfig/music` |
| `/var/lib/musicfig/music/` | drop MP3s and playlist folders here |
| `/var/lib/musicfig/musicfig.log` | rotating app log (journald on the kiosk Pis is volatile, this is not) |
| `/var/lib/musicfig/cache/` | Spotify song metadata sqlite cache |
| `/var/lib/musicfig/yoto-*.json` | Yoto tokens/config (see below) |
| `/etc/systemd/system/musicfig.service` | the unit; env vars point the app at the paths above |
| `/etc/udev/rules.d/99-lego.rules` | makes the pad readable without root |

## First tag

1. `journalctl -u musicfig -f` (or `tail -f /var/lib/musicfig/musicfig.log`).
2. Put a tag or minifig on the pad. The log prints its UID.
3. `sudo nano /etc/musicfig/tags.yml`, add an entry under `identifier:`
   (`mp3: song.mp3` for a file in `music/`, `playlist: folder` for a folder,
   `spotify: track:...` once Spotify is set up). `tags.yml-sample` has examples.
4. Copy MP3s in: `scp song.mp3 kiosk@<room>.local:/tmp/ && ssh kiosk@<room>.local 'sudo mv /tmp/song.mp3 /var/lib/musicfig/music/ && sudo chown musicfig:musicfig /var/lib/musicfig/music/song.mp3'`.
5. `sudo systemctl restart musicfig`, lift the tag off and put it back.

## Audio

Pi 5 has no headphone jack; sound goes over HDMI to the TV/touchscreen. The
recipe does not install PipeWire or PulseAudio; musicfig talks to ALSA directly.

* `aplay -l` lists the cards (HDMI shows as `vc4-hdmi-0` / `vc4-hdmi-1`).
* `sudo -u musicfig speaker-test -c2 -t wav -l1` proves the service user can play.
* Wrong default card? Create `/etc/asound.conf` with `defaults.pcm.card N` and
  `defaults.ctl.card N` (N from `aplay -l`), then `sudo systemctl restart musicfig`.
* `Device or resource busy` while the kiosk's Chromium has the card open: ALSA
  dmix is per-user by default; add `ipc_perm 0660` / `ipc_gid audio` to a
  dmix definition in `/etc/asound.conf`. Chromium on a silent dashboard usually
  never opens the device, so try the simple setup first.

## Spotify (optional)

1. `sudo nano /etc/musicfig/config.py`, fill in `CLIENT_ID` / `CLIENT_SECRET`
   from your Spotify developer app, whose redirect URI must be exactly
   `http://localhost:5000/callback`. `sudo systemctl restart musicfig`.
2. The app only listens on the Pi's localhost, so do the OAuth dance through an
   SSH tunnel from your laptop: `ssh -L 5000:127.0.0.1:5000 kiosk@<room>.local`.
3. With the tunnel open, browse to <http://localhost:5000/> on the laptop, log in
   to Spotify, and the callback lands on the Pi. The log says `Spotify activated.`
4. Tokens live in the process; a restart needs the dance again (upstream behaviour).

## Yoto (phase 2)

`yoto-api` is installed and the service reads `/var/lib/musicfig/yoto-tokens.json`
and `yoto-config.json` if they exist, but do not log in per Pi: the plan in
[life-ops #62](https://github.com/butterflysilver/life-ops/issues/62) is to
feed every Pi from the Railway yoto-mcp token family so refreshes happen in one
place. Until then, `yoto:` entries in `tags.yml` log an error and do nothing else.

## Useful commands

```
sudo systemctl status musicfig        # is it up?
journalctl -u musicfig -b             # this boot's log
sudo systemctl restart musicfig       # after editing tags.yml or config.py
bash install.sh                       # update to latest master
```
