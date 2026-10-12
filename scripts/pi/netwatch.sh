#!/usr/bin/env bash
#
# musicfig network watchdog (life-ops #118).
#
# A Pi 5's onboard Wi-Fi occasionally wedges: the router still lists the Pi
# as online, the kiosk keeps cycling the photos it already has, but no packet
# moves and nobody can ssh in. Only a reboot clears it, and until someone
# notices, that room's pad is dead. The hardware watchdog (RuntimeWatchdogSec
# in the kiosk recipe) only catches a hung kernel, not a hung Wi-Fi chip.
#
# This runs from musicfig-netwatch.timer every CHECK_INTERVAL and escalates
# on consecutive failures to reach the default gateway:
#
#   failure 1        log it
#   failure 2        bounce Wi-Fi (nmcli radio wifi off/on; autoconnect rejoins)
#   failure 4+       reboot, but only once the Pi has been up MIN_UPTIME_S, so
#                    a router that is down does not turn into a 2-minute boot
#                    loop (it becomes one reboot per ~MIN_UPTIME_S instead)
#
# The counter lives in STATE_DIR (tmpfs, so it resets on reboot) and goes
# back to zero on the first successful ping. Everything it does is logged to
# the journal under the unit name; `journalctl -u musicfig-netwatch` tells
# the story of every blip.
#
# Overridable for tests (tests/test_netwatch.py) and for a different box:
#   NETWATCH_STATE_DIR   where the failure counter lives (default /run/musicfig-netwatch)
#   NETWATCH_UPTIME_FILE /proc/uptime or a fake
#   NETWATCH_TARGET      host to ping instead of the default gateway
#   NETWATCH_REBOOT_AT   consecutive failures before a reboot (default 4)
#   NETWATCH_BOUNCE_AT   consecutive failures before a Wi-Fi bounce (default 2)
#   NETWATCH_MIN_UPTIME  seconds of uptime required before a reboot (default 1200)
#
set -euo pipefail

STATE_DIR="${NETWATCH_STATE_DIR:-/run/musicfig-netwatch}"
UPTIME_FILE="${NETWATCH_UPTIME_FILE:-/proc/uptime}"
BOUNCE_AT="${NETWATCH_BOUNCE_AT:-2}"
REBOOT_AT="${NETWATCH_REBOOT_AT:-4}"
MIN_UPTIME_S="${NETWATCH_MIN_UPTIME:-1200}"
PING_COUNT=3
PING_WAIT_S=2
BOUNCE_PAUSE_S=5

log() { echo "netwatch: $*"; }

mkdir -p "${STATE_DIR}"
FAIL_FILE="${STATE_DIR}/failures"
failures=0
if [[ -r "${FAIL_FILE}" ]]; then
    failures="$(tr -dc '0-9' < "${FAIL_FILE}")"
    failures="${failures:-0}"
fi

uptime_s() {
    local up
    up="$(cut -d' ' -f1 "${UPTIME_FILE}" 2>/dev/null || echo 0)"
    echo "${up%.*}"
}

# The gateway is the best thing to ping: it says "this Pi can talk to the
# house" without depending on the internet, and it is what the pad needs
# for AirPlay, Yoto and the Figures report alike.
target="${NETWATCH_TARGET:-}"
if [[ -z "${target}" ]]; then
    target="$(ip -4 route show default 2>/dev/null | awk '/^default/ {print $3; exit}')"
fi

reachable=0
if [[ -z "${target}" ]]; then
    log "no default route (Wi-Fi not connected)"
elif ping -c "${PING_COUNT}" -W "${PING_WAIT_S}" "${target}" >/dev/null 2>&1; then
    reachable=1
fi

if [[ "${reachable}" -eq 1 ]]; then
    if [[ "${failures}" -gt 0 ]]; then
        log "gateway ${target} reachable again after ${failures} failed check(s)"
    fi
    echo 0 > "${FAIL_FILE}"
    exit 0
fi

failures=$((failures + 1))
echo "${failures}" > "${FAIL_FILE}"
log "cannot reach ${target:-the gateway} (consecutive failures: ${failures})"

if [[ "${failures}" -ge "${REBOOT_AT}" ]]; then
    up="$(uptime_s)"
    if [[ "${up}" -ge "${MIN_UPTIME_S}" ]]; then
        log "rebooting: ${failures} consecutive failures, up ${up}s"
        systemctl reboot
        exit 0
    fi
    log "would reboot but only up ${up}s (< ${MIN_UPTIME_S}s); skipping the reboot"
fi

if [[ "${failures}" -ge "${BOUNCE_AT}" ]]; then
    log "bouncing Wi-Fi"
    nmcli radio wifi off || true
    sleep "${BOUNCE_PAUSE_S}"
    nmcli radio wifi on || true
fi
exit 0
