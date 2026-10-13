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
#   failure 4+       reboot, but only once the Pi has been up MIN_UPTIME_S
#                    times a backoff multiplier, so a router that is down
#                    does not turn into a 2-minute boot loop, and only once
#                    two bounces have been tried without the Wi-Fi link
#                    coming back (a wedged chip; see below)
#
# The counter lives in STATE_DIR (tmpfs, so it resets on reboot) and goes
# back to zero on the first successful ping. Everything it does is logged to
# the journal under the unit name; `journalctl -u musicfig-netwatch` tells
# the story of every blip.
#
# Two refinements from life-ops #121 live in the same tmpfs dir:
#
#   bounces     how many bounces in a row NetworkManager has not reported the
#               link on IFACE back at "100 (connected)" afterwards. A reboot
#               wants REBOOT_BOUNCES of them: a chip that will not reassociate
#               is the wedge a reboot clears, while a link that is up with a
#               gateway that still does not answer looks like the mesh
#               extender or the router's uplink being down, which a reboot
#               cannot fix. Both only DEFER the reboot, to HARD_REBOOT_AT
#               misses: a wedged chip that still reports "connected" (the
#               router listed the Pi as online through the 2026-10-12 wedges)
#               must not hold the reboot back forever. The uptime backoff
#               still applies to the forced reboot.
#   deferrals   bounces put off because musicfig was mid-playback (a local
#               MP3 or a HomePod stream on the same extender plays fine
#               without the gateway; the bounce, and the reboot behind it,
#               would cut it). musicfig keeps PLAYING_FILE present while a
#               figure plays (app/lego.py); at most MAX_DEFERRALS cycles per
#               bounce, then it goes ahead anyway (and no bounce means no
#               reboot either, see above).
#
# Two things survive the reboot (life-ops #121), both under /var/lib/musicfig:
#
#   REBOOTS_FILE  how many watchdog reboots in a row. Each one doubles the
#                 uptime a further reboot needs (20/40/80/160 min, capped at
#                 8x), so a whole-house outage costs a handful of reboots,
#                 not one every ~22 min. Reset by the first successful ping.
#   LOG_FILE      every bounce / reboot / "would reboot" / recovery line,
#                 timestamped. The pads' journal is volatile, so this is the
#                 only record of what the watchdog did before a reboot. Kept
#                 to the last LOG_KEEP lines.
#
# Overridable for tests (tests/test_netwatch.py) and for a different box:
#   NETWATCH_STATE_DIR     where the failure counter lives (default /run/musicfig-netwatch)
#   NETWATCH_STATE_PERSIST the consecutive-reboot counter (default /var/lib/musicfig/netwatch-reboots)
#   NETWATCH_LOG           the persistent log (default /var/lib/musicfig/netwatch.log)
#   NETWATCH_UPTIME_FILE   /proc/uptime or a fake
#   NETWATCH_TARGET        host to ping instead of the default gateway
#   NETWATCH_IFACE         the Wi-Fi device whose link state nmcli reports (default wlan0)
#   NETWATCH_PLAYING_FILE  musicfig's "a figure is playing" marker (default /run/musicfig/playing)
#   NETWATCH_REBOOT_AT     consecutive failures before a reboot (default 4)
#   NETWATCH_BOUNCE_AT     consecutive failures before a Wi-Fi bounce (default 2)
#   NETWATCH_MIN_UPTIME    seconds of uptime required before the first reboot (default 1200)
#
set -euo pipefail

STATE_DIR="${NETWATCH_STATE_DIR:-/run/musicfig-netwatch}"
REBOOTS_FILE="${NETWATCH_STATE_PERSIST:-/var/lib/musicfig/netwatch-reboots}"
LOG_FILE="${NETWATCH_LOG:-/var/lib/musicfig/netwatch.log}"
UPTIME_FILE="${NETWATCH_UPTIME_FILE:-/proc/uptime}"
IFACE="${NETWATCH_IFACE:-wlan0}"
PLAYING_FILE="${NETWATCH_PLAYING_FILE:-/run/musicfig/playing}"
BOUNCE_AT="${NETWATCH_BOUNCE_AT:-2}"
REBOOT_AT="${NETWATCH_REBOOT_AT:-4}"
MIN_UPTIME_S="${NETWATCH_MIN_UPTIME:-1200}"
MAX_BACKOFF=8
REBOOT_BOUNCES=2
MAX_DEFERRALS=3
# 12 misses (24 min) by default: room for two bounces even when each one
# waits out its MAX_DEFERRALS cycles of playback first.
HARD_REBOOT_AT=$((REBOOT_AT * 3))
LOG_KEEP=500
LOG_TRIM_AT=600
PING_COUNT=3
PING_WAIT_S=2
BOUNCE_PAUSE_S=5

log() { echo "netwatch: $*"; }

# Journal line plus a timestamped copy in LOG_FILE. A log that cannot be
# written (read-only /var/lib, missing user) is skipped, never fatal; the
# trim keeps it small with portable tools (tail > tmp && mv).
record() {
    log "$*"
    mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null || return 0
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "${LOG_FILE}" 2>/dev/null || return 0
    local lines
    lines="$(wc -l < "${LOG_FILE}" 2>/dev/null || echo 0)"
    lines="${lines// /}"
    if [[ "${lines:-0}" -gt "${LOG_TRIM_AT}" ]]; then
        tail -n "${LOG_KEEP}" "${LOG_FILE}" > "${LOG_FILE}.tmp" 2>/dev/null \
            && mv "${LOG_FILE}.tmp" "${LOG_FILE}" 2>/dev/null \
            || rm -f "${LOG_FILE}.tmp" 2>/dev/null || true
    fi
}

# Every counter is a file holding one number; garbage counts as zero and a
# hand-edited "08" must not kill the run (hence base 10).
read_count() {
    local n=0
    if [[ -r "$1" ]]; then
        n="$(tr -dc '0-9' < "$1" || true)"
    fi
    echo $((10#${n:-0}))
}

# A state dir that cannot be written degrades to "log only, no memory"
# rather than killing the check (set -e would otherwise exit here).
mkdir -p "${STATE_DIR}" 2>/dev/null || log "cannot create ${STATE_DIR}"
FAIL_FILE="${STATE_DIR}/failures"
BOUNCES_FILE="${STATE_DIR}/bounces"
DEFERRALS_FILE="${STATE_DIR}/deferrals"
failures="$(read_count "${FAIL_FILE}")"
bounces="$(read_count "${BOUNCES_FILE}")"
deferrals="$(read_count "${DEFERRALS_FILE}")"
remember() { echo "$1" > "${FAIL_FILE}" 2>/dev/null || log "cannot write ${FAIL_FILE}"; }
remember_bounces() { echo "$1" > "${BOUNCES_FILE}" 2>/dev/null || log "cannot write ${BOUNCES_FILE}"; }
remember_deferrals() { echo "$1" > "${DEFERRALS_FILE}" 2>/dev/null || log "cannot write ${DEFERRALS_FILE}"; }

reboots="$(read_count "${REBOOTS_FILE}")"
remember_reboots() {
    mkdir -p "$(dirname "${REBOOTS_FILE}")" 2>/dev/null || true
    echo "$1" > "${REBOOTS_FILE}" 2>/dev/null || log "cannot write ${REBOOTS_FILE}"
}

# MIN_UPTIME_S * 2^reboots, capped at MAX_BACKOFF x (so the counter cannot
# shift the threshold out of reach, whatever is in the file).
reboot_threshold_s() {
    local mult=1 n=0
    while [[ "${n}" -lt "${reboots}" && "${mult}" -lt "${MAX_BACKOFF}" ]]; do
        mult=$((mult * 2))
        n=$((n + 1))
    done
    echo $((MIN_UPTIME_S * mult))
}

uptime_s() {
    local up
    up="$(cut -d' ' -f1 "${UPTIME_FILE}" 2>/dev/null || true)"
    up="${up%.*}"
    echo "${up:-0}"
}

# NetworkManager's view of the Wi-Fi link: `GENERAL.STATE:100 (connected)`
# once associated with an address. Exactly 100 is up - 110 is deactivating
# and 120 failed - and no answer at all (nmcli missing, device gone) is down.
link_up() {
    local state
    state="$(nmcli -t -f GENERAL.STATE dev show "${IFACE}" 2>/dev/null \
             | awk -F: 'NR == 1 {print $2 + 0}' || true)"
    [[ "${state:-0}" -eq 100 ]]
}

# musicfig keeps PLAYING_FILE present while a figure plays; a musicfig that
# is not running cannot be mid-playback whatever the file says (systemd
# removes its RuntimeDirectory with it anyway).
playing() {
    [[ -e "${PLAYING_FILE}" ]] && systemctl is-active --quiet musicfig >/dev/null 2>&1
}

# The gateway is the best thing to ping: it says "this Pi can talk to the
# house" without depending on the internet, and it is what the pad needs
# for AirPlay, Yoto and the Figures report alike. A failing `ip` (netlink
# upset while the Wi-Fi stack is wedged) must count as a miss, not abort.
target="${NETWATCH_TARGET:-}"
if [[ -z "${target}" ]]; then
    target="$(ip -4 route show default 2>/dev/null | awk '/^default/ {print $3; exit}' || true)"
fi

reachable=0
if [[ -z "${target}" ]]; then
    log "no default route (Wi-Fi not connected)"
elif ping -c "${PING_COUNT}" -W "${PING_WAIT_S}" "${target}" >/dev/null 2>&1; then
    reachable=1
fi

if [[ "${reachable}" -eq 1 ]]; then
    if [[ "${failures}" -gt 0 ]]; then
        record "gateway ${target} reachable again after ${failures} failed check(s)"
    fi
    # The first good ping after a watchdog reboot is the recovery that the
    # tmpfs counter cannot see (it came up at zero); it also ends the backoff.
    if [[ "${reboots}" -gt 0 ]]; then
        record "gateway ${target} reachable after ${reboots} watchdog reboot(s); backoff reset"
        remember_reboots 0
    fi
    remember 0
    if [[ "${bounces}" -gt 0 ]]; then remember_bounces 0; fi
    if [[ "${deferrals}" -gt 0 ]]; then remember_deferrals 0; fi
    exit 0
fi

failures=$((failures + 1))
remember "${failures}"
log "cannot reach ${target:-the gateway} (consecutive failures: ${failures})"

# A link that is back up after a bounce means the chip reassociated; the
# bounces that got it there no longer count towards a reboot. Recorded, so
# the log can tell a wedge that still reported "connected" from a mesh fault.
link=0
link_word=down
if link_up; then
    link=1
    link_word=up
    if [[ "${bounces}" -gt 0 ]]; then
        record "link on ${IFACE} is up again after ${bounces} bounce(s)"
        bounces=0
        remember_bounces 0
    fi
fi

if [[ "${failures}" -ge "${REBOOT_AT}" ]]; then
    up="$(uptime_s)"
    need="$(reboot_threshold_s)"
    # Past the hard cap neither the link state nor the bounce count holds
    # the reboot back any more; only the uptime backoff still does.
    forced=0
    if [[ "${failures}" -ge "${HARD_REBOOT_AT}" ]]; then forced=1; fi
    if [[ "${forced}" -eq 0 && "${link}" -eq 1 ]]; then
        record "link up, gateway unreachable - mesh/uplink fault; reboot deferred (${failures}/${HARD_REBOOT_AT} consecutive failures)"
    elif [[ "${forced}" -eq 0 && "${bounces}" -lt "${REBOOT_BOUNCES}" ]]; then
        record "only ${bounces} bounce(s) tried without the link returning (need ${REBOOT_BOUNCES}); reboot deferred (${failures}/${HARD_REBOOT_AT} consecutive failures)"
    elif [[ "${up}" -ge "${need}" ]]; then
        why="${bounces} bounces without the link returning"
        if [[ "${forced}" -eq 1 ]]; then
            why="hard cap reached, link ${link_word}, ${bounces} bounce(s) without the link returning"
        fi
        # Count it before ordering it, so the count survives the reboot.
        remember_reboots $((reboots + 1))
        record "rebooting: ${failures} consecutive failures, ${why}, up ${up}s (threshold ${need}s, watchdog reboot #$((reboots + 1)))"
        systemctl reboot
        exit 0
    else
        record "would reboot but only up ${up}s (< ${need}s after ${reboots} watchdog reboot(s)); skipping the reboot"
    fi
fi

if [[ "${failures}" -ge "${BOUNCE_AT}" ]]; then
    if [[ "${deferrals}" -lt "${MAX_DEFERRALS}" ]] && playing; then
        remember_deferrals $((deferrals + 1))
        record "playing; bounce deferred ($((deferrals + 1))/${MAX_DEFERRALS})"
        exit 0
    fi
    if [[ "${deferrals}" -gt 0 ]]; then remember_deferrals 0; fi
    remember_bounces $((bounces + 1))
    record "bouncing Wi-Fi"
    # NetworkManager persists the radio switch; if this run is killed during
    # the pause the Pi would boot with Wi-Fi off, so the trap turns it back
    # on no matter how the script ends.
    trap 'nmcli radio wifi on >/dev/null 2>&1 || true' EXIT
    nmcli radio wifi off || true
    sleep "${BOUNCE_PAUSE_S}"
    nmcli radio wifi on || true
    trap - EXIT
fi
exit 0
