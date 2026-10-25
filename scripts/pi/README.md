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
| `/var/lib/musicfig/pads-off` | exists while `gsd-pads off` is in force (see below) |
| `/etc/systemd/system/musicfig.service` | the unit; env vars point the app at the paths above |
| `/usr/local/bin/gsd-pads` | `scripts/pi/pads.py`: blank / restore the pad's lights (root-owned copy, Pi `python3`) |
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

## Network page (optional)

`scripts/report_network.py` sends this pad's view of the house network to
gsd-cloud (`POST /api/network/report`), which shows every device across every
room and whether the mesh and the pads are healthy at `/network`. Each pad is
a sensor: it knows which mesh node it hangs off, how strong the link is, what
the network watchdog has seen, and what answers on its LAN. The installer adds
`musicfig-network.timer` (4 minutes after boot, then every 5 minutes), which
runs `musicfig-network.service` as the unprivileged `musicfig` user (plus the
`systemd-journal` group, so it can read the watchdog's journal; unprivileged
`ping` works on Raspberry Pi OS (`ping_group_range` sysctl / file caps),
`ip neigh` and `nmcli` are read-only). Root never executes the
`musicfig`-owned venv.

* Enabled by the same `/etc/musicfig/pad-report.key` as the Figures page;
  without the file the service exits 0 and logs "network reporting is not set
  up here".
* What is sent: `self` (ip, mac, uptime, the Wi-Fi link's ssid / bssid /
  channel / freq_mhz / signal_pct from NetworkManager's last scan - `nmcli
  --rescan no`, so the report never triggers a scan - and the watchdog's
  current failure count, number of "netwatch:" lines this boot and the last
  one) and `lan`: after a `ping -c1 -W1` of every host in the pad's own /24
  (bounded at ~6 s, never any other subnet), every `ip -4 neigh` row on that
  /24 with a MAC in state REACHABLE / STALE / DELAY / PROBE - other subnets
  (docker bridges, VPNs) are ignored and a device learned on two interfaces
  is one row with its best state - plus an mDNS `name` from one batched
  `avahi-resolve-address` call where it is installed: avahi gives up on the
  misses after its own ~5 s and exits non-zero, and the hits it printed are
  kept; a hard cap 2 s past that kills it, losing whatever it had not
  flushed. Names are cut to 63 characters. Every address and MAC of the pad
  itself is left out of `lan` (a dual-homed Pi sees its own Ethernet port
  from Wi-Fi); the list is capped at 512 rows. Nothing else - no hostnames
  the pad did not see itself.
* A missing tool (`nmcli`, `ip`, `avahi-resolve-address`, `journalctl`) or a
  timeout never fails the report: that piece is null / empty, and
  `--dry-run` logs why.
* The figure report's secret guard runs on this payload too, although nothing
  in it can carry a credential; a neighbour's name it dislikes is dropped,
  not a reason to refuse the whole report.
* Check: the faithful dry run is the unit's own identity, because `sudo -u
  musicfig` cannot add the `systemd-journal` group (so the netwatch part
  would read as zeros):

  ```
  sudo systemd-run -p User=musicfig -p Group=musicfig -p SupplementaryGroups=systemd-journal \
    --wait --pipe /opt/musicfig/.venv/bin/python /opt/musicfig/scripts/report_network.py --dry-run
  ```

  `--no-sweep` skips the pings; an unknown flag exits 2 without sending;
  `journalctl -u musicfig-network` shows the sends.

## Pad lights: the dark house (`gsd-pads`)

`gsd-screen off` blanks a room's panel; `gsd-pads` does the same for the LEGO
pad's lights, so the two together are the "dark house" command
([life-ops #75](https://github.com/butterflysilver/life-ops/issues/75)):

```
ssh kitchen gsd-screen off; ssh kitchen gsd-pads off     # dark house
ssh kitchen gsd-screen on;  ssh kitchen gsd-pads on      # morning
ssh kitchen gsd-pads status                              # "off (1 pad)" / "on (no pad attached)"
```

* `off`: every pad goes dark and stays dark through tag events - the tap's
  blue, the action colours, the lightshow and the lift's idle repaint are all
  dropped in the pad driver (`app/lego.py`, `Dimensions.switch_pad` /
  `fade_pad` / `flash_pad`); figures still play. The state is written to
  `/var/lib/musicfig/pads-off`, so after a restart of musicfig in the night
  (the pad-gone exit, a watchdog reboot) - or a pad re-plugged - the service
  paints the pad dark again as soon as it has it (the pad's own power-on
  light show would otherwise leave it lit until morning).
* `on`: the file goes, and the pad loop repaints the idle colour (green with
  `lights: on`, dark with `lights: off`).
* `status`: the state plus the pad count (0 or 1), exit 0 either way.

The service owns the USB pad, so the command does not open it: it asks the
running musicfig over its localhost web port (`GET /pads`, `POST
/pads/off|on`, `app/pads.py`; the same `127.0.0.1:5000` as the Spotify
pages). Any user on the Pi may run it (the ssh aliases land as `kiosk`), no
sudoers rule is involved, and it is standard library only, so the Pi's
`/usr/bin/python3` runs it. With no pad plugged in the state is still recorded
and applied when one appears (exit 0, `off (no pad attached; applies when one
is plugged in)`); when musicfig is not running it says so and exits 1
(`systemctl is-active musicfig`). `tests/test_pads.py` covers the flag, the
gate, the repaint, the hook and the CLI with the pad driver stubbed.

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
| 2+ | `nmcli radio wifi off` / `on` (autoconnect rejoins) - unless a figure is playing, in which case the bounce waits a cycle, at most three times, then goes ahead anyway |
| 4+ | `systemctl reboot` - but never within 20 minutes of boot, and each consecutive watchdog reboot doubles that wait (20, 40, 80, then 160 minutes, the cap), so a router or mesh extender that is down for hours costs a handful of reboots, not one every ~22 minutes; and, until 12 misses, only once two bounces in a row have left the Wi-Fi link down |
| 12+ | `systemctl reboot` regardless of the link state and the bounce count (the uptime rule above still applies) |

The link check is NetworkManager's `nmcli -t -f GENERAL.STATE dev show wlan0`
(`NETWATCH_IFACE` to change the device): exactly `100 (connected)` is up,
anything else - or no answer at all - is down. Two bounces that do not bring
the link back are a wedged chip, which is what the reboot is for. A link that
is up while the gateway still does not answer looks like the mesh extender or
the router's uplink being down, which a reboot cannot fix, so it is logged as
"link up, gateway unreachable - mesh/uplink fault; reboot deferred (N/12
consecutive failures)" and the reboot waits. It only waits, though: a wedged
chip can still report "connected" (the router listed both Pis that wedged on
2026-10-12 as online), so at 12 misses (24 minutes, 3 x the reboot threshold)
the reboot goes ahead whatever the link says, logged with "hard cap reached"
and the link state. The bounces-without-link counter resets, with a log line,
as soon as the link is seen up again.

The playing check is `/run/musicfig/playing` (plus `systemctl is-active
musicfig`): musicfig keeps the file while a local MP3 or a HomePod stream is
running (`app/lego.py`; either plays fine without the gateway, and the bounce
- or the reboot behind it - would cut it). `RuntimeDirectory=musicfig` in the
unit creates `/run/musicfig` for the service and removes it when musicfig
stops, so a crash never leaves a stale marker. Deferring the bounce also
defers the reboot, since the reboot wants the bounces first - which is what
the 12-miss cap leaves room for: two bounces, each after three deferrals,
still land before it.

The failure, bounce and deferral counters live in `/run/musicfig-netwatch/`
(tmpfs, cleared by the reboot) and reset on the first successful ping. Two
files survive the reboot:
`/var/lib/musicfig/netwatch-reboots` holds the number of consecutive
watchdog-caused reboots (the backoff multiplier; reset by the first good
ping) and `/var/lib/musicfig/netwatch.log` gets a timestamped copy of every
bounce, reboot, skipped reboot and recovery line, kept to the last 500 lines.
The pads' journal is volatile, so that log is the only record of what the
watchdog did before a reboot. `journalctl -u musicfig-netwatch -g 'netwatch:'`
shows every blip this boot and what was done about it (the script itself is
silent on a healthy Pi; systemd still logs each 2-minute run, hence the `-g`
filter). `tests/test_netwatch.py` drives the ladder, the backoff, the link
gate, the playback deferral and the log with fake `ping`/`nmcli`/`systemctl`.

## Useful commands

```
sudo systemctl status musicfig        # is it up?
journalctl -u musicfig -b             # this boot's log
sudo systemctl restart musicfig       # after editing tags.yml or config.py
gsd-pads off|on|status                # pad lights (with gsd-screen: the dark house)
bash install.sh                       # update to latest master
```
