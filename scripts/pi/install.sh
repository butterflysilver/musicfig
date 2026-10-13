#!/usr/bin/env bash
#
# musicfig Raspberry Pi installer (Raspberry Pi OS Lite Bookworm/Trixie, 64-bit).
#
# Installs musicfig as a systemd service under its own system user so it can
# share a Pi with the GSD wall-display kiosk (user `kiosk`, cage + Chromium on
# tty1). Safe to re-run: every step is idempotent and a re-run doubles as the
# update path (git pull --ff-only, pip install, service restart).
#
# Usage (from your laptop):
#   scp scripts/pi/install.sh kiosk@<room>.local:
#   ssh -t kiosk@<room>.local 'bash install.sh'
#
# Optional environment overrides:
#   MUSICFIG_REPO    git URL to clone (default: butterflysilver/musicfig)
#   MUSICFIG_BRANCH  branch to check out (default: master)
#
# Layout:
#   /opt/musicfig            code checkout + .venv (owner musicfig)
#   /etc/musicfig            config.py (Spotify secrets, 0640) + tags.yml
#   /var/lib/musicfig        service home: music/, cache/, musicfig.log
#
set -euo pipefail

MUSICFIG_REPO="${MUSICFIG_REPO:-https://github.com/butterflysilver/musicfig.git}"
MUSICFIG_BRANCH="${MUSICFIG_BRANCH:-master}"

SVC_USER="musicfig"
SVC_HOME="/var/lib/musicfig"
APP_DIR="/opt/musicfig"
VENV_DIR="${APP_DIR}/.venv"
CONF_DIR="/etc/musicfig"
UNIT_FILE="/etc/systemd/system/musicfig.service"

log()  { echo "[musicfig] $*"; }
die()  { echo "[musicfig] ERROR: $*" >&2; exit 1; }

# Re-exec as root so the rest of the script needs no sudo sprinkling.
if [[ "${EUID}" -ne 0 ]]; then
    command -v sudo >/dev/null 2>&1 || die "run as root or install sudo"
    exec sudo --preserve-env=MUSICFIG_REPO,MUSICFIG_BRANCH bash "$0" "$@"
fi

# Run a command as the service user with a sane HOME (runuser keeps root's).
as_svc() { runuser -u "${SVC_USER}" -- env HOME="${SVC_HOME}" "$@"; }

# First apt package name that exists in this suite (Trixie renamed the mpg123
# libs with a t64 suffix; Bookworm still uses the plain names).
pick_pkg() {
    local candidate
    for candidate in "$@"; do
        if apt-cache show "${candidate}" >/dev/null 2>&1; then
            echo "${candidate}"
            return 0
        fi
    done
    die "none of these packages exist in this apt suite: $*"
}

# ---------------------------------------------------------------- packages --
log "Installing apt packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
LIBMPG123="$(pick_pkg libmpg123-0t64 libmpg123-0)"
LIBOUT123="$(pick_pkg libout123-0t64 libout123-0)"
log "mpg123 libraries for this suite: ${LIBMPG123} ${LIBOUT123}"
# No PipeWire/PulseAudio on purpose: sound goes straight to ALSA over HDMI.
# build-essential + python3-dev: the pyatv extra (HomePod AirPlay, Apple TV)
# pulls in miniaudio, which builds from source on arm64.
apt-get install -y -qq --no-install-recommends \
    python3 python3-venv python3-pip python3-dev build-essential \
    "${LIBMPG123}" "${LIBOUT123}" mpg123 \
    libusb-1.0-0 git alsa-utils ffmpeg

# ------------------------------------------------------------- system user --
if ! id -u "${SVC_USER}" >/dev/null 2>&1; then
    log "Creating system user ${SVC_USER}..."
    getent group plugdev >/dev/null || groupadd --system plugdev
    useradd --system --home-dir "${SVC_HOME}" --create-home \
        --shell /usr/sbin/nologin --groups audio,plugdev "${SVC_USER}"
else
    usermod -aG audio,plugdev "${SVC_USER}"
fi
install -d -m 0750 -o "${SVC_USER}" -g "${SVC_USER}" "${SVC_HOME}"
install -d -m 0755 -o "${SVC_USER}" -g "${SVC_USER}" "${SVC_HOME}/music" "${SVC_HOME}/cache"

# -------------------------------------------------------------- code + venv --
if [[ -d "${APP_DIR}/.git" ]]; then
    log "Updating ${APP_DIR} (${MUSICFIG_BRANCH})..."
    chown -R "${SVC_USER}:${SVC_USER}" "${APP_DIR}"
    as_svc git -C "${APP_DIR}" fetch --quiet origin
    as_svc git -C "${APP_DIR}" checkout --quiet "${MUSICFIG_BRANCH}"
    as_svc git -C "${APP_DIR}" pull --ff-only --quiet
else
    log "Cloning ${MUSICFIG_REPO} (${MUSICFIG_BRANCH}) into ${APP_DIR}..."
    install -d -m 0755 -o "${SVC_USER}" -g "${SVC_USER}" "${APP_DIR}"
    as_svc git clone --quiet --branch "${MUSICFIG_BRANCH}" "${MUSICFIG_REPO}" "${APP_DIR}"
fi

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    log "Creating venv at ${VENV_DIR}..."
    as_svc python3 -m venv "${VENV_DIR}"
fi
log "Installing Python requirements (the mpg123 wheel needs ${LIBMPG123} present)..."
as_svc "${VENV_DIR}/bin/pip" install --quiet --upgrade pip wheel
as_svc "${VENV_DIR}/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
# Optional integrations (HomePod AirPlay, Apple TV, Hue sync box). Set
# MUSICFIG_EXTRAS=no to skip them on a Pi that will never talk to Apple gear.
if [[ "${MUSICFIG_EXTRAS:-yes}" == "yes" ]]; then
    log "Installing the optional integrations (pyatv builds miniaudio; a few minutes on a Pi 5)..."
    as_svc "${VENV_DIR}/bin/pip" install --quiet -r "${APP_DIR}/requirements-extras.txt"
fi

# -------------------------------------------------------------------- udev --
if ! cmp -s "${APP_DIR}/99-lego.rules" /etc/udev/rules.d/99-lego.rules; then
    log "Installing udev rule for the LEGO Dimensions pad..."
    install -m 0644 -o root -g root "${APP_DIR}/99-lego.rules" /etc/udev/rules.d/99-lego.rules
    udevadm control --reload-rules
    udevadm trigger
fi

# ------------------------------------------------------------------ config --
install -d -m 0750 -o root -g "${SVC_USER}" "${CONF_DIR}"
if [[ ! -f "${CONF_DIR}/config.py" ]]; then
    log "Creating ${CONF_DIR}/config.py from config.py-sample (add Spotify keys here later)..."
    install -m 0640 -o root -g "${SVC_USER}" "${APP_DIR}/config.py-sample" "${CONF_DIR}/config.py"
fi
if [[ ! -f "${CONF_DIR}/tags.yml" ]]; then
    log "Creating ${CONF_DIR}/tags.yml from tags.yml-sample..."
    install -m 0644 -o root -g "${SVC_USER}" "${APP_DIR}/tags.yml-sample" "${CONF_DIR}/tags.yml"
    # Point the sample's mp3 base folder at the service's music directory.
    sed -i "s|^mp3_dir: .*|mp3_dir: ${SVC_HOME}/music|" "${CONF_DIR}/tags.yml"
fi

# ----------------------------------------------------------- wake the kiosk --
# Let the service user wake the wall display (and nothing else) when a tag
# lands on the pad. No-op on a Pi without the kiosk: the unit's hook command
# fails quietly if gsd-screen or the kiosk user is missing.
if id -u kiosk >/dev/null 2>&1; then
    log "Allowing ${SVC_USER} to run gsd-screen as kiosk (wake-on-tag)..."
    echo "${SVC_USER} ALL=(kiosk) NOPASSWD: /usr/local/bin/gsd-screen" > /etc/sudoers.d/musicfig-gsd-screen
    chmod 0440 /etc/sudoers.d/musicfig-gsd-screen
    visudo -cf /etc/sudoers.d/musicfig-gsd-screen >/dev/null || die "sudoers drop-in failed validation"
fi

# Unit line for the wake-on-tag hook; empty on a Pi with no kiosk user. systemd
# splits an unquoted Environment= value on whitespace, so it must be quoted.
ON_TAG_ENV=""
if id -u kiosk >/dev/null 2>&1; then
    ON_TAG_ENV='Environment="MUSICFIG_ON_TAG_CMD=sudo -n -u kiosk /usr/local/bin/gsd-screen on"'
fi

# ----------------------------------------------------------------- systemd --
log "Writing ${UNIT_FILE}..."
cat > "${UNIT_FILE}" <<UNIT
[Unit]
Description=Musicfig - LEGO Dimensions NFC jukebox
Wants=network-online.target
After=network-online.target sound.target

[Service]
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${SVC_HOME}
RuntimeDirectory=musicfig
Environment=PYTHONPATH=${CONF_DIR}
Environment=MUSICFIG_TAGS_FILE=${CONF_DIR}/tags.yml
Environment=MUSICFIG_ROOM=$(hostname -s)
Environment=MUSICFIG_YOTO_TRACKS_URL=${MUSICFIG_YOTO_TRACKS_URL:-https://yoto-mcp-production.up.railway.app}
Environment=MUSICFIG_YOTO_TRACKS_KEY_FILE=${CONF_DIR}/yoto-tracks.key
Environment=MUSICFIG_LOG_FILE=${SVC_HOME}/musicfig.log
Environment=MUSICFIG_CACHE_DIR=${SVC_HOME}/cache
${ON_TAG_ENV}
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "${UNIT_FILE}"

# Pad report: the figure map goes to gsd-cloud (the Figures page) whenever
# tags.yml changes and once an hour. The service exits 0 quietly until
# ${CONF_DIR}/pad-report.key exists, so these units are harmless on a pad
# that is not reporting. Same environment as the pad so the room and the
# tags file are the same ones musicfig itself uses.
log "Writing musicfig-report units..."
cat > /etc/systemd/system/musicfig-report.service <<UNIT
[Unit]
Description=Musicfig - report this pad's figure map to gsd-cloud
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${SVC_HOME}
TimeoutStartSec=10min
Environment=MUSICFIG_TAGS_FILE=${CONF_DIR}/tags.yml
Environment=MUSICFIG_ROOM=$(hostname -s)
Environment=MUSICFIG_YOTO_TRACKS_URL=${MUSICFIG_YOTO_TRACKS_URL:-https://yoto-mcp-production.up.railway.app}
Environment=MUSICFIG_YOTO_TRACKS_KEY_FILE=${CONF_DIR}/yoto-tracks.key
Environment=MUSICFIG_REPORT_KEY_FILE=${CONF_DIR}/pad-report.key
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/scripts/report_pad.py
UNIT
cat > /etc/systemd/system/musicfig-report.path <<UNIT
[Unit]
Description=Musicfig - re-report the figure map when tags.yml changes

[Path]
PathChanged=${CONF_DIR}/tags.yml
Unit=musicfig-report.service

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/musicfig-report.timer <<UNIT
[Unit]
Description=Musicfig - hourly figure map report

[Timer]
OnBootSec=3min
OnUnitActiveSec=1h
RandomizedDelaySec=5min
Unit=musicfig-report.service

[Install]
WantedBy=timers.target
UNIT
chmod 0644 /etc/systemd/system/musicfig-report.{service,path,timer}

# Network watchdog: a wedged Wi-Fi chip leaves the Pi "online" on the router
# with nothing passing; scripts/pi/netwatch.sh bounces Wi-Fi, then reboots,
# when the gateway stays unreachable. It runs as root, so it is copied out of
# the ${SVC_USER}-owned checkout into a root-owned path first.
log "Writing musicfig-netwatch units..."
install -o root -g root -m 0755 "${APP_DIR}/scripts/pi/netwatch.sh" /usr/local/sbin/musicfig-netwatch
cat > /etc/systemd/system/musicfig-netwatch.service <<UNIT
[Unit]
Description=Musicfig - network watchdog (bounce Wi-Fi, then reboot, when the gateway is unreachable)

[Service]
Type=oneshot
TimeoutStartSec=90s
ExecStart=/usr/local/sbin/musicfig-netwatch
UNIT
cat > /etc/systemd/system/musicfig-netwatch.timer <<UNIT
[Unit]
Description=Musicfig - network watchdog every 2 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=2min
AccuracySec=30s
Unit=musicfig-netwatch.service

[Install]
WantedBy=timers.target
UNIT
chmod 0644 /etc/systemd/system/musicfig-netwatch.{service,timer}

# Network report: this pad's view of the house network (its Wi-Fi link, the
# watchdog's state, a ping sweep of its /24) goes to gsd-cloud (the Network
# page) every 5 minutes. Same key file as the figure report, so it is just
# as quiet on a pad that is not reporting. Runs as the service user, never
# root (it executes the ${SVC_USER}-owned venv, which is exactly why netwatch
# is copied out of the checkout): unprivileged ping works on Raspberry Pi OS
# (ping_group_range sysctl / file caps), `ip neigh` and `nmcli` are
# read-only, the watchdog's counter dir is 0755, and the systemd-journal
# group covers `journalctl -u musicfig-netwatch`.
log "Writing musicfig-network units..."
cat > /etc/systemd/system/musicfig-network.service <<UNIT
[Unit]
Description=Musicfig - report this pad's view of the network to gsd-cloud
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=${SVC_USER}
Group=${SVC_USER}
SupplementaryGroups=systemd-journal
WorkingDirectory=${SVC_HOME}
TimeoutStartSec=3min
Environment=MUSICFIG_ROOM=$(hostname -s)
Environment=MUSICFIG_REPORT_KEY_FILE=${CONF_DIR}/pad-report.key
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/scripts/report_network.py
UNIT
cat > /etc/systemd/system/musicfig-network.timer <<UNIT
[Unit]
Description=Musicfig - network report every 5 minutes

[Timer]
OnBootSec=4min
OnUnitActiveSec=5min
RandomizedDelaySec=30s
AccuracySec=30s
Unit=musicfig-network.service

[Install]
WantedBy=timers.target
UNIT
chmod 0644 /etc/systemd/system/musicfig-network.{service,timer}

systemctl daemon-reload
systemctl enable --quiet musicfig.service
systemctl enable --quiet --now musicfig-report.path musicfig-report.timer
systemctl enable --quiet --now musicfig-netwatch.timer
systemctl enable --quiet --now musicfig-network.timer
log "Starting musicfig.service..."
systemctl restart musicfig.service
sleep 2
systemctl --no-pager --lines=0 status musicfig.service || true

# ------------------------------------------------------------------- hints --
cat <<HINTS

[musicfig] Done. Next steps:

  Audio (Pi 5 has no headphone jack; sound goes out over HDMI):
    aplay -l                                          # list ALSA cards; HDMI is "vc4-hdmi-0"
    sudo -u ${SVC_USER} speaker-test -c2 -t wav -l1     # should say "Front Left / Front Right"
    If the wrong card is the default, create /etc/asound.conf with:
        defaults.pcm.card <N>
        defaults.ctl.card <N>
    (N = the vc4-hdmi card number from aplay -l), then: sudo systemctl restart musicfig

  Find a tag UID:
    journalctl -u musicfig -f                         # (or: tail -f ${SVC_HOME}/musicfig.log)
    put a tag on the pad -> copy the UID from the log
    sudo nano ${CONF_DIR}/tags.yml                     # add the UID under identifier:
    copy MP3s into ${SVC_HOME}/music/
    sudo systemctl restart musicfig

  Yoto (optional): put the Yoto MCP shared secret in ${CONF_DIR}/yoto-tracks.key
    (root:${SVC_USER} 0640; scp it, never paste it), then map a tag with
    yoto_airplay: <card id> (stream to the room's HomePod) or yoto: <card id>
    (play on the Yoto player itself; yoto_player: "@room" + room_yoto_players).
  Spotify (optional): add CLIENT_ID/CLIENT_SECRET to ${CONF_DIR}/config.py
    then follow scripts/pi/README.md for the SSH port-forward OAuth dance.
  Figures page (optional): put the gsd-cloud MUSICFIG_REPORT_KEY in
    ${CONF_DIR}/pad-report.key (root:${SVC_USER} 0640; scp it, never paste it).
    The map is sent on every tags.yml change and hourly; check with
      sudo -u ${SVC_USER} ${VENV_DIR}/bin/python ${APP_DIR}/scripts/report_pad.py --dry-run
      journalctl -u musicfig-report
  Network watchdog: every 2 min the Pi pings its gateway; 2 misses bounce
    Wi-Fi, 4 misses reboot (never within 20 min of boot, doubling per
    consecutive watchdog reboot up to 160 min; a good ping resets it). It
    only reboots once 2 bounces have left the Wi-Fi link down (a wedged
    chip); a link that is up with no gateway is a mesh/uplink fault, logged,
    not rebooted for. A bounce waits (up to 3 cycles) while a figure plays
    (musicfig keeps /run/musicfig/playing while an MP3 or HomePod stream runs).
      journalctl -u musicfig-netwatch -g 'netwatch:'    # every blip this boot and what was done about it
      sudo cat ${SVC_HOME}/netwatch.log                  # bounces/reboots/recoveries, survives the reboot
      ls /run/musicfig-netwatch/                         # failures / bounces / deferrals counters this boot
  Network page (optional): with the same ${CONF_DIR}/pad-report.key in place
    the pad also reports its Wi-Fi link, the watchdog's state and the devices
    on its /24 to gsd-cloud every 5 minutes; check with (sudo -u alone cannot
    add the systemd-journal group, so the netwatch part would read as zeros)
      sudo systemd-run -p User=${SVC_USER} -p Group=${SVC_USER} -p SupplementaryGroups=systemd-journal \\
        --wait --pipe ${VENV_DIR}/bin/python ${APP_DIR}/scripts/report_network.py --dry-run
      journalctl -u musicfig-network

HINTS
