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

## Yoto

A Pi never logs in to Yoto. Both Yoto actions go through the Railway Yoto MCP
with the household shared secret in `/etc/musicfig/yoto-tracks.key`
(`MUSICFIG_YOTO_TRACKS_URL` / `MUSICFIG_YOTO_TRACKS_KEY_FILE` in the unit):

* `yoto_airplay: <card id>` - the card's signed track URLs, streamed to a HomePod.
* `yoto: <card id>` - the card started on a real Yoto player
  (`yoto_player`: id, name, `"@room"` via `room_yoto_players`, default `@online`).
  Lifting the figure stops the player. The log says `Yoto player: 'Title'
  playing on <name>`; a red flash with `... is offline` means the Yoto is asleep
  or off Wi-Fi, `the shared key was rejected` means the key file is stale.

## Figures page (optional)

`scripts/report_pad.py` sends this pad's figure map to gsd-cloud
(`POST /api/pads/report`), which shows every figure across every room at
`/figures`. The installer adds `musicfig-report.path` (fires when
`/etc/musicfig/tags.yml` changes) and `musicfig-report.timer` (hourly); both
run `musicfig-report.service` with the pad's own environment, so the room and
the tags file are exactly what musicfig uses.

* Enable it by putting the gsd-cloud `MUSICFIG_REPORT_KEY` in
  `/etc/musicfig/pad-report.key` (`root:musicfig 0640`, `scp` it). Without the
  file the service exits 0 and logs "pad reporting is not set up here".
* What is sent: per figure the UID, `name`, the one action it does in this
  room (resolved like the pad resolves it: room overrides, `@room`), the
  target (URL / card id / file), a title (Yoto card titles from the Yoto MCP,
  Apple TV titles from the link slug, or an explicit `title:` on the tag) and
  the device (Apple TV name, HomePod, Yoto player). Figures with no action in
  this room are sent as `none` so the page can show them as unmapped.
* What is never sent: any key matching `credential|token|secret|password|
  api_key|hook` at any depth (Apple TV pairing credentials, the sync box
  token, Slack webhooks) and any value that looks like an opaque blob. The
  script refuses to send a payload that still contains one.
* Check: `sudo -u musicfig /opt/musicfig/.venv/bin/python
  /opt/musicfig/scripts/report_pad.py --dry-run` prints the payload;
  `journalctl -u musicfig-report` shows the sends.

## Network watchdog

A Pi 5's Wi-Fi chip occasionally wedges: the router still shows the Pi
online and the kiosk keeps cycling the photos it already has, but nothing
passes and ssh is dead. The hardware watchdog (`RuntimeWatchdogSec` from the
kiosk recipe) only catches a hung kernel, so the installer adds
`musicfig-netwatch.timer`, which runs `scripts/pi/netwatch.sh` (copied to
`/usr/local/sbin/musicfig-netwatch`, root-owned) every 2 minutes:

| Consecutive misses pinging the gateway | Action |
|---|---|
| 1 | logged |
| 2+ | `nmcli radio wifi off` / `on` (autoconnect rejoins) |
| 4+ | `systemctl reboot` - but never within 20 minutes of boot, so a router that is down means one reboot per ~20 minutes, not a boot loop |

The counter lives in `/run/musicfig-netwatch/` (tmpfs, cleared by the reboot)
and resets on the first successful ping. `journalctl -u musicfig-netwatch`
shows every blip and what was done about it; a healthy Pi logs nothing.
`tests/test_netwatch.py` drives the ladder with fake `ping`/`nmcli`/`systemctl`.

## Useful commands

```
sudo systemctl status musicfig        # is it up?
journalctl -u musicfig -b             # this boot's log
sudo systemctl restart musicfig       # after editing tags.yml or config.py
bash install.sh                       # update to latest master
```
