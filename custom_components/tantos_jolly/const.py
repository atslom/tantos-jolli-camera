"""Constants for the Tantos Jolly integration."""

DOMAIN = "tantos_jolly"

CONF_CHANNEL = "channel"
CONF_RTSP_PORT = "rtsp_port"

CHANNEL_PANEL_1 = 0x01
CHANNEL_PANEL_2 = 0x02
CHANNEL_PANEL_3 = 0x04
CHANNEL_PANEL_4 = 0x08
CHANNEL_CAMERA_1 = 0x10
CHANNEL_CAMERA_2 = 0x20
CHANNEL_CAMERA_3 = 0x40
CHANNEL_CAMERA_4 = 0x80

CHANNEL_CHOICES = {
    CHANNEL_PANEL_1: "Вызывная панель 1",
    CHANNEL_PANEL_2: "Вызывная панель 2",
    CHANNEL_PANEL_3: "Вызывная панель 3",
    CHANNEL_PANEL_4: "Вызывная панель 4",
    CHANNEL_CAMERA_1: "Камера 1",
    CHANNEL_CAMERA_2: "Камера 2",
    CHANNEL_CAMERA_3: "Камера 3",
    CHANNEL_CAMERA_4: "Камера 4",
}

# Kept for backward compatibility with existing config entries -- the value
# in this slot is reused as the HLS HTTP port for the local python3
# http.server that serves the playlist to HA.
DEFAULT_RTSP_BASE_PORT = 18554

HLS_PLAYLIST_NAME = "playlist.m3u8"
HLS_SEGMENT_PATTERN = "seg_%05d.ts"
HLS_SEGMENT_SECONDS = 1
HLS_PLAYLIST_SIZE = 4

# GLNK session retry policy (see glnk_client.py and bridge.py docstrings).
RECONNECT_DELAY_SECONDS = 5.0
HANDSHAKE_RETRY_DELAY_SECONDS = 180.0
START_STREAM_RETRY_SECONDS = 5.0
MAX_START_STREAM_RETRIES = 6
FAST_RETRY_SECONDS = 0.5
FAST_RETRY_ATTEMPTS = 3
ATTEMPT_CACHED_TOKEN_REUSE = False

# ---------------------------------------------------------------------------
# Hub service ports.
#
# The hub does NOT listen on a fixed port.  We have seen it use 22087 and
# 56492 on the same device across different sessions (see real network
# captures in the issue tracker).  Those two are tried first; the legacy
# 44418 (previously hardcoded) and 37020 (used by several GLNK installations)
# are appended as fall-backs.  The handshake routine probes every candidate
# with a HELLO1 packet and uses the first one that answers.
#
# 44418 is kept for users whose hubs happen to use it (or whose config still
# pins it explicitly via the port hint).
# ---------------------------------------------------------------------------
HUB_SERVICE_PORTS: tuple[int, ...] = (22087, 56492, 37020, 44418)

# Best-effort DISCOVER probes (not critical; currently only used for logging).
HUB_DISCOVERY_PORTS: tuple[int, ...] = (1500, 6800)

# 2026-10-17: how long to wait for the first video chunk before giving up
# and stopping the capture. For panels this is an ABSOLUTE limit -- once
# we hit it, we stop trying (the panel never gets a second relogin attempt
# in the same capture session, because doing so makes the whole hub go
# into anti-abuse lockdown and kills the camera too).
PANEL_GIVE_UP_SECONDS = 30.0
CAMERA_GIVE_UP_SECONDS = 10.0

# 2026-10-19: real-world captures (tcpdump on the HA host, cross-correlated
# with PCAPdroid on the phone running the real vhOme app) showed the hub
# routinely needs MULTIPLE handshake attempts spread over tens of seconds
# before it responds -- the real app gets through every time by retrying
# every ~0.4s for up to ~20-40s, never by giving up after one try. Our own
# single-shot handshake (previously: one probe cycle ~3-4s, then permanent
# give-up until the user manually retoggles) was the actual, confirmed
# cause of panels/cameras appearing to "hang" -- not a hub-side lockdown
# (that theory, based on PCAPdroid showing zero responses across several
# minutes, was retracted the same session: PCAPdroid was independently
# confirmed to silently drop the high-rate successful video exchange on
# this phone while still logging the low-rate failed attempts, so its
# "zero response" reading never actually proved the hub was unresponsive).
# These two constants bound a retry loop around `GlnkSession.handshake()`
# in `bridge.py` -- applies identically to panels and cameras, and only
# changes how long we're willing to keep retrying the handshake stage
# itself; everything that happens after a successful handshake, and the
# final give-up behavior if the whole window is exhausted, is unchanged.
HANDSHAKE_RETRY_WINDOW_SECONDS = 45.0
HANDSHAKE_RETRY_INTERVAL_SECONDS = 1.5

# ---------------------------------------------------------------------------
# Hub-side brute-force lockout (reverse-engineered from the `Sofia` daemon,
# 2026-10-20 -- see the `tantos-jolly-glnk-re` memory for the full trace).
#
# `CUserManager::init` (FUN_000745e0) hardcodes:
#     *(this+0x10) = 300   -> lockout window T  = 300 s
#     *(this+0x14) = 5     -> max failed logins N = 5
# i.e. 5 failed password attempts lock the account for 300 s, then the lock
# auto-clears. The lock is keyed PER USERNAME, so because both HA and the
# real iOS app log in as `admin`, tripping it blocks the app too -- which is
# exactly the "iOS app over WiFi also can't open, recovers after a few
# minutes" symptom we chased for days.
#
# The counter only advances on a *failed* auth and resets on success, so a
# correct login never trips it on its own -- but RESENDING a login that got
# no response (the old code sent up to 3 login() calls x 4 UDT resends = 12
# packets per open), or churning on->off->on, can pile up strikes fast if
# any of those attempts is seen as a failure. The defenses below enforce the
# rule "never let 5 attempts accumulate inside one 5-minute window" on our
# side: one login attempt per open, and a cooldown that refuses to touch the
# hub again until its lock would have expired.
# ---------------------------------------------------------------------------
HUB_LOCKOUT_MAX_FAILS = 5
HUB_LOCKOUT_WINDOW_SECONDS = 300.0
# A hair over the hub's 300 s window, so when we back off we come back only
# after the hub has definitely cleared the lock (and not one attempt before).
HUB_LOCKOUT_COOLDOWN_SECONDS = 330.0

# 2026-10-21: hard per-channel session cap. A capture left on (switch on /
# card forgotten open) would otherwise hold a hub connection slot forever,
# which can block the iOS app and the other channels. After this many
# seconds the bridge stops the capture itself and the "Stream capture"
# switch flips off (it polls bridge.capturing every 2 s); the user re-enables
# to watch again. Keep comfortably above a normal viewing session.
CHANNEL_MAX_SESSION_SECONDS = 600.0

# 2026-10-21: graduated login-failure backoff. A login that gets NO hub
# response, or a "too many connections" (slot-limit) reject, is a TRANSIENT
# failure -- it happens when the previous channel's session briefly overlaps
# a new one during fast channel switching, and it is NOT a brute-force strike
# (the hub never processed it as a failed auth, and slots free within
# seconds). Backing the whole hub off for the full 330 s lockout window on a
# single such blip blocked EVERY channel for 5.5 min after one fast switch.
# Instead we back off only a few seconds for a transient failure, and escalate
# to HUB_LOCKOUT_COOLDOWN_SECONDS only after several consecutive ones (nearing
# the hub's real N=5). A real auth reject (reason 2) or IP block (reason 4)
# still goes straight to the full cooldown.
LOGIN_TRANSIENT_COOLDOWN_SECONDS = 8.0
LOGIN_MAX_TRANSIENT_FAILS = 3

# 2026-10-21: per-hub session serialization. The hub serves ONE AV stream at
# a time: if a new channel logs in while another channel's session is still
# active on the same hub, the new one logs in OK but the hub never starts its
# video (and doesn't start it retroactively once the first frees), so it sits
# until the no-video give-up -- the "hang" seen when switching channels fast.
# Before logging in, a channel waits (bounded by this many seconds) for any
# other channel on the same host to stop capturing. A normal switch frees the
# old session within a few seconds; the cap keeps a stuck/other-left-on case
# from blocking forever (it then proceeds best-effort).
SESSION_OPEN_WAIT_SECONDS = 12.0