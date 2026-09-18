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
#   /var/lib/musicfig        service home: music/, cache/, musicfig.log, Yoto tokens
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
apt-get install -y -qq --no-install-recommends \
    python3 python3-venv python3-pip \
    "${LIBMPG123}" "${LIBOUT123}" mpg123 \
    libusb-1.0-0 git alsa-utils

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
Environment=PYTHONPATH=${CONF_DIR}
Environment=MUSICFIG_TAGS_FILE=${CONF_DIR}/tags.yml
Environment=MUSICFIG_LOG_FILE=${SVC_HOME}/musicfig.log
Environment=MUSICFIG_CACHE_DIR=${SVC_HOME}/cache
Environment=MUSICFIG_YOTO_TOKEN_FILE=${SVC_HOME}/yoto-tokens.json
Environment=MUSICFIG_YOTO_CONFIG_FILE=${SVC_HOME}/yoto-config.json
${ON_TAG_ENV}
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "${UNIT_FILE}"
systemctl daemon-reload
systemctl enable --quiet musicfig.service
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

  Spotify (optional): add CLIENT_ID/CLIENT_SECRET to ${CONF_DIR}/config.py
    then follow scripts/pi/README.md for the SSH port-forward OAuth dance.

HINTS
