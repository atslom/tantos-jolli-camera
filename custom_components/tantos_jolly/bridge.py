"""Background bridge: GLNK UDP session -> local HLS via ffmpeg + http.server."""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import shutil
import tempfile
import threading
import time

from homeassistant.core import HomeAssistant

from .const import (
    CAMERA_GIVE_UP_SECONDS,
    CHANNEL_MAX_SESSION_SECONDS,
    HANDSHAKE_RETRY_INTERVAL_SECONDS,
    HANDSHAKE_RETRY_WINDOW_SECONDS,
    HLS_PLAYLIST_NAME,
    HLS_PLAYLIST_SIZE,
    HLS_SEGMENT_PATTERN,
    HLS_SEGMENT_SECONDS,
    HUB_LOCKOUT_COOLDOWN_SECONDS,
    LOGIN_MAX_TRANSIENT_FAILS,
    LOGIN_TRANSIENT_COOLDOWN_SECONDS,
    PANEL_GIVE_UP_SECONDS,
    SESSION_OPEN_WAIT_SECONDS,
)
from .glnk_client import GlnkSession, StreamDemuxer

_LOGGER = logging.getLogger(__name__)

# Cap raw H.264 debug dump (bytes).  Set to 0 to disable entirely.
_RAW_DUMP_MAX_BYTES = 50 * 1024 * 1024

# ---------------------------------------------------------------------------
# Per-hub lockout cooldown, shared across ALL channel bridges for one host.
#
# The hub locks the `admin` account for 300 s after 5 failed logins (Sofia
# CUserManager, N=5/T=300 -- see const.py and the tantos-jolly-glnk-re
# memory). Because every channel logs in as the same `admin`, that lock is
# hub-wide in practice. When any channel's open fails (or the hub answers
# with a reject), we record a cooldown here and every channel of that hub
# refuses to send another packet until it passes -- so retoggling switches
# during the lock can't pile up more strikes and extend it.
# ---------------------------------------------------------------------------
_HUB_BLOCKED_UNTIL: dict[str, float] = {}
_HUB_BLOCKED_LOCK = threading.Lock()
# Consecutive transient (no-response) login failures per host, guarded by the
# same lock; reset on any successful login. Used to escalate a run of blips
# into the full lockout cooldown.
_HUB_LOGIN_FAILS: dict[str, int] = {}


def _bump_login_fails(host: str) -> int:
    with _HUB_BLOCKED_LOCK:
        n = _HUB_LOGIN_FAILS.get(host, 0) + 1
        _HUB_LOGIN_FAILS[host] = n
        return n


def _reset_login_fails(host: str) -> None:
    with _HUB_BLOCKED_LOCK:
        _HUB_LOGIN_FAILS.pop(host, None)


# Registry of all bridges per host, so an opening channel can tell whether
# another channel on the same hub is currently capturing (the hub serves one
# AV stream at a time -- see SESSION_OPEN_WAIT_SECONDS / the open gate).
_HOST_BRIDGES: dict[str, set] = {}


def _hub_blocked_remaining(host: str) -> float:
    with _HUB_BLOCKED_LOCK:
        until = _HUB_BLOCKED_UNTIL.get(host, 0.0)
    return max(0.0, until - time.monotonic())


def _set_hub_blocked(host: str, seconds: float, reason: str) -> None:
    with _HUB_BLOCKED_LOCK:
        until = time.monotonic() + seconds
        if until > _HUB_BLOCKED_UNTIL.get(host, 0.0):
            _HUB_BLOCKED_UNTIL[host] = until
    _LOGGER.warning(
        "Tantos Jolly %s: backing off all channels for %.0fs (%s)",
        host, seconds, reason,
    )


class GlnkCameraBridge:
    def __init__(
        self, hass: HomeAssistant, host: str, username: str, password: str,
        channel: int, rtsp_port: int,
    ) -> None:
        self.hass = hass
        self.host = host
        self.username = username
        self.password = password
        self.channel = channel
        self.hls_port = rtsp_port
        # Register for per-host session serialization (see the open gate).
        _HOST_BRIDGES.setdefault(host, set()).add(self)
        self._hls_dir = os.path.join(tempfile.gettempdir(), f"tantos_jolly_hls_ch{channel:x}")
        self.hls_url = f"http://127.0.0.1:{self.hls_port}/{HLS_PLAYLIST_NAME}"

        self._http_proc: asyncio.subprocess.Process | None = None
        self.connected = False
        self.capturing = False
        self.last_error: str | None = None
        self._video_ready_event = threading.Event()

        # Learned hub service port. Persisted across reconnect attempts within
        # this bridge instance so re-handshake doesn't have to re-scan.
        self._hub_port: int | None = None

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._sess: GlnkSession | None = None
        self._byte_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=512)

        self._raw_dump_path = f"/config/tantos_debug_raw_ch{channel:x}.h264"
        self._raw_dump_file = None
        self._raw_dump_bytes = 0

        self._ffmpeg_proc: asyncio.subprocess.Process | None = None
        self._ffmpeg_task: asyncio.Task | None = None
        # Hard per-channel session cap (see CHANNEL_MAX_SESSION_SECONDS): a
        # task that stops the capture after the cap so a forgotten-open
        # channel can't hold a hub slot forever.
        self._timeout_task: asyncio.Task | None = None
        self._lifecycle_lock = asyncio.Lock()

    # ------------------------------------------------------------------- lifecycle

    async def async_setup(self) -> None:
        return

    @staticmethod
    def _write_text(path: str, text: str) -> None:
        with open(path, "w") as fh:
            fh.write(text)

    @staticmethod
    def _append_endlist(path: str) -> None:
        """Append #EXT-X-ENDLIST to a live playlist so an HLS reader sees a
        clean end-of-stream and stops, rather than treating the source as
        still-live."""
        with open(path, "r+") as fh:
            data = fh.read()
            if "#EXT-X-ENDLIST" not in data:
                if data and not data.endswith("\n"):
                    fh.write("\n")
                fh.write("#EXT-X-ENDLIST\n")

    async def _wait_http_ready(self, timeout: float) -> None:
        """Block until the local HLS http server accepts a TCP connection, or
        `timeout` seconds elapse. Keeps HA's first playlist fetch from racing
        the server's bind and getting Connection-refused."""
        deadline = self.hass.loop.time() + timeout
        while self.hass.loop.time() < deadline:
            try:
                _reader, writer = await asyncio.open_connection(
                    "127.0.0.1", self.hls_port)
            except OSError:
                await asyncio.sleep(0.05)
                continue
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return

    async def _session_timeout(self) -> None:
        """Stop the capture once the hard per-channel session cap elapses, so
        a forgotten-open channel releases its hub slot. The 'Stream capture'
        switch polls bridge.capturing (every 2s) and flips off on its own."""
        try:
            await asyncio.sleep(CHANNEL_MAX_SESSION_SECONDS)
        except asyncio.CancelledError:
            return
        _LOGGER.info(
            "Tantos Jolly %s channel %#x: %.0f min session cap reached, "
            "stopping capture", self.host, self.channel,
            CHANNEL_MAX_SESSION_SECONDS / 60.0)
        await self.stop_capture()

    async def start_capture(self) -> None:
        # Radio-button behaviour: only ONE channel per hub streams at a time
        # (the hub serves a single AV stream; concurrent channels just starve
        # each other). Turning this channel on stops every other channel on
        # the same host. Done BEFORE taking our own lifecycle lock so two
        # near-simultaneous turn-ons can't deadlock each waiting on the other's
        # lock; stop_capture is idempotent (no-ops if already stopped). It also
        # fully closes the sibling's session, freeing the hub slot before we
        # log in (the open gate below then passes immediately).
        for other in list(_HOST_BRIDGES.get(self.host, ())):
            if other is not self and other.capturing:
                try:
                    await other.stop_capture()
                except Exception:
                    _LOGGER.exception(
                        "Tantos Jolly %s: failed to stop sibling channel %#x",
                        self.host, getattr(other, "channel", 0))

        async with self._lifecycle_lock:
            if self.capturing:
                return
            self.capturing = True
            self._stop_event.clear()
            self._video_ready_event.clear()
            self.connected = False
            self.last_error = None

            try:
                self._raw_dump_file = await self.hass.async_add_executor_job(
                    open, self._raw_dump_path, "wb"
                )
                self._raw_dump_bytes = 0
            except OSError as err:
                _LOGGER.warning("could not open raw dump: %s", err)
                self._raw_dump_file = None

            try:
                if os.path.isdir(self._hls_dir):
                    await self.hass.async_add_executor_job(shutil.rmtree, self._hls_dir)
                await self.hass.async_add_executor_job(os.makedirs, self._hls_dir, 0o755, True)
                # Pre-create a valid, segment-less live HLS playlist BEFORE the
                # http server comes up, so HA's stream component gets a 200 on
                # its first fetch and then polls for segments like any live
                # stream -- instead of hitting 404/Connection-refused during the
                # ~1-3s login+first-keyframe warm-up and backing off (10->60s
                # exponential) so far that video only appears a minute later.
                # ffmpeg overwrites this file once it has real segments.
                await self.hass.async_add_executor_job(
                    self._write_text,
                    os.path.join(self._hls_dir, HLS_PLAYLIST_NAME),
                    "#EXTM3U\n"
                    "#EXT-X-VERSION:3\n"
                    f"#EXT-X-TARGETDURATION:{HLS_SEGMENT_SECONDS}\n"
                    "#EXT-X-MEDIA-SEQUENCE:0\n",
                )
            except OSError as err:
                _LOGGER.warning("could not prepare HLS dir: %s", err)

            try:
                self._http_proc = await asyncio.create_subprocess_exec(
                    "python3", "-m", "http.server", str(self.hls_port),
                    "--bind", "127.0.0.1", "--directory", self._hls_dir,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                # Make sure the server is actually accepting connections before
                # we return, so HA's first fetch can't race it and get
                # Connection-refused (which also triggers the backoff above).
                await self._wait_http_ready(2.0)

                # Per-hub session serialization: wait (bounded) for any OTHER
                # channel on this host to stop capturing before we log in, so
                # we don't overlap the hub's single AV stream -- an overlap
                # logs in OK but never streams, then hits the no-video give-up
                # (the "hang" when switching channels fast). A normal switch
                # frees the previous session within a few seconds; the cap
                # keeps an other-left-on case from blocking forever.
                gate_deadline = self.hass.loop.time() + SESSION_OPEN_WAIT_SECONDS
                # "Busy" = another channel is still capturing OR its reader
                # thread hasn't fully exited yet (its GLNK socket/logout isn't
                # done, so the hub hasn't freed the slot). capturing alone
                # flips False at the very start of stop_capture, before
                # teardown, so we also check the thread is gone.
                def _host_busy() -> bool:
                    for b in list(_HOST_BRIDGES.get(self.host, ())):
                        if b is self:
                            continue
                        if b.capturing or (b._thread is not None
                                           and b._thread.is_alive()):
                            return True
                    return False

                while (not self._stop_event.is_set()
                       and self.hass.loop.time() < gate_deadline):
                    if not _host_busy():
                        break
                    await asyncio.sleep(0.25)

                self._thread = threading.Thread(target=self._reader_thread, daemon=True)
                self._thread.start()
                self._ffmpeg_task = asyncio.create_task(self._ffmpeg_feeder())
                self._timeout_task = asyncio.create_task(self._session_timeout())
                _LOGGER.info("Tantos Jolly %s channel %#x: capture started",
                             self.host, self.channel)
            except Exception:
                self.capturing = False
                _LOGGER.exception("start_capture() failed")
                raise

    async def stop_capture(self) -> None:
        async with self._lifecycle_lock:
            if not self.capturing:
                return
            self.capturing = False
            self._stop_event.set()
            self._video_ready_event.clear()

            # Cancel the session-cap timer on a manual/early stop. If stop was
            # triggered BY that timer, it's the current task -- don't cancel
            # ourselves, just drop the reference.
            if (self._timeout_task is not None
                    and self._timeout_task is not asyncio.current_task()):
                self._timeout_task.cancel()
            self._timeout_task = None

            if self._raw_dump_file is not None:
                try:
                    await self.hass.async_add_executor_job(self._raw_dump_file.close)
                except OSError:
                    pass
                self._raw_dump_file = None

            # Best-effort graceful logout.  We do NOT close the socket from
            # here -- the reader thread owns it and will close it in its
            # finally block.  This avoids a race on the UDP socket.
            if self._sess is not None:
                try:
                    self._sess.send_logout()
                except Exception:
                    pass
                # Short non-blocking delay to let the hub process the logout.
                await asyncio.sleep(0.5)

            if self._ffmpeg_task:
                self._ffmpeg_task.cancel()
                try:
                    await self._ffmpeg_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._ffmpeg_task = None

            if self._ffmpeg_proc and self._ffmpeg_proc.returncode is None:
                self._ffmpeg_proc.terminate()
                try:
                    await asyncio.wait_for(self._ffmpeg_proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    self._ffmpeg_proc.kill()
                    await self._ffmpeg_proc.wait()

            # Signal a clean end-of-stream so HA's stream_worker terminates,
            # instead of hammering the (about-to-vanish) port with
            # Connection-refused for its whole ~5-min idle keep-alive. ffmpeg
            # is already stopped, so the playlist is static now; we append
            # #EXT-X-ENDLIST and keep the http server up briefly for HA to
            # read it. capturing is already False, so any stream restart gets
            # a None stream_source and never re-attempts the dead port.
            try:
                playlist = os.path.join(self._hls_dir, HLS_PLAYLIST_NAME)
                if os.path.isfile(playlist):
                    await self.hass.async_add_executor_job(
                        self._append_endlist, playlist)
                    await asyncio.sleep(2.0)
            except OSError:
                pass

            if self._http_proc and self._http_proc.returncode is None:
                self._http_proc.terminate()
                try:
                    await asyncio.wait_for(self._http_proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    self._http_proc.kill()
                    await self._http_proc.wait()

            # Wait for reader thread to exit; it closes the socket in its finally.
            if self._thread:
                await self.hass.async_add_executor_job(self._thread.join, 5.0)
                self._thread = None

            try:
                if os.path.isdir(self._hls_dir):
                    await self.hass.async_add_executor_job(shutil.rmtree, self._hls_dir)
            except OSError:
                pass
            self._sess = None
            self.connected = False
            _LOGGER.info("Tantos Jolly %s channel %#x: capture stopped",
                         self.host, self.channel)

    async def async_stop(self) -> None:
        await self.stop_capture()
        # Deregister from the per-host registry so a removed channel leaves no
        # stale reference (the registry is what the radio-exclusion / open gate
        # iterate, and it grows/shrinks with the configured channels).
        siblings = _HOST_BRIDGES.get(self.host)
        if siblings is not None:
            siblings.discard(self)
            if not siblings:
                _HOST_BRIDGES.pop(self.host, None)

    # ------------------------------------------------------------------- reader

    def _reader_thread(self) -> None:
        sess: GlnkSession | None = None
        handshake_failures = 0

        is_panel = int(self.channel) <= 0x08

        # Respect a hub-wide cooldown from a recent failure on ANY channel:
        # if the hub's 300 s admin-lockout window might still be running, do
        # not send it a single packet -- just report and let the switch fall
        # back to off. The user re-toggles once the cooldown has elapsed.
        remaining = _hub_blocked_remaining(self.host)
        if remaining > 0:
            self.connected = False
            self.last_error = (
                f"hub in lockout cooldown, wait {remaining:.0f}s before retrying"
            )
            _LOGGER.warning(
                "Tantos Jolly %s channel %#x: skipping open, hub cooldown %.0fs left",
                self.host, self.channel, remaining,
            )
            self._stop_event.set()
            self.capturing = False
            return
        give_up_seconds = PANEL_GIVE_UP_SECONDS if is_panel else CAMERA_GIVE_UP_SECONDS
        # 2026-10-18: cameras used to retry indefinitely (999999) with
        # escalating backoff, unlike anything the real app does -- a real
        # capture shows the app making exactly ONE connection attempt per
        # "channel opened" event (the one extra handshake seen in a
        # capture was the app's own abandoned preliminary probe within the
        # SAME open, finished in ~1.7s, not a background retry loop). The
        # constant reconnect churn is suspected of contributing to the
        # hub's anti-abuse lockdowns we've hit repeatedly tonight. Both
        # channel types now get ONE try per switch-on, matching the real
        # app's actual behavior -- the user re-toggles the switch to retry,
        # same as closing and reopening the channel in the app.
        max_attempts = 1

        try:
            attempt = 0
            while not self._stop_event.is_set() and attempt < max_attempts:
                attempt += 1
                try:
                    if sess is None:
                        sess = GlnkSession(self.host, port=self._hub_port)
                        sess.channel = int(self.channel)
                        self._sess = sess

                    if sess.token_c is None:
                        # 2026-10-19: handshake() itself probes every
                        # candidate port, but a single probe cycle
                        # (~3-4s) is not enough -- real captures show the
                        # hub often needs tens of seconds of repeated
                        # HELLO1 attempts before it responds (the real
                        # vhOme app gets through by retrying every ~0.4s
                        # for up to ~20-40s, never by trying once). Retry
                        # the whole handshake here for a bounded window
                        # before falling through to the existing
                        # give-up/backoff logic below, which is unchanged.
                        handshake_deadline = (
                            time.monotonic() + HANDSHAKE_RETRY_WINDOW_SECONDS
                        )
                        handshake_ok = sess.handshake()
                        while (
                            not handshake_ok
                            and not self._stop_event.is_set()
                            and time.monotonic() < handshake_deadline
                        ):
                            self._stop_event.wait(HANDSHAKE_RETRY_INTERVAL_SECONDS)
                            if self._stop_event.is_set():
                                break
                            handshake_ok = sess.handshake()

                        if not handshake_ok:
                            self.connected = False
                            self.last_error = "handshake failed"
                            handshake_failures += 1

                            if handshake_failures == 1: delay = 30.0
                            elif handshake_failures == 2: delay = 60.0
                            elif handshake_failures == 3: delay = 300.0
                            elif handshake_failures == 4: delay = 900.0
                            else: delay = 1800.0

                            _LOGGER.warning(
                                "Tantos Jolly %s: handshake failed (%d) on all "
                                "candidate ports, waiting %.0fs",
                                self.host, handshake_failures, delay,
                            )
                            try:
                                sess.close()
                            except Exception:
                                pass
                            sess = None
                            self._sess = None

                            if is_panel:
                                # Give up cleanly so HA sees the switch as off.
                                self.capturing = False
                                self.last_error = (
                                    "handshake failed on panel; reset hub via app "
                                    "if it does not respond to any known port"
                                )
                                self._stop_event.set()
                                return

                            self._stop_event.wait(delay)
                            continue

                        # Remember the working port for fast reconnect.
                        self._hub_port = sess.port
                        handshake_failures = 0

                    # 2026-10-20: ONE login attempt per open, like the real
                    # app (panel-3 capture: a single LOGIN send got a single
                    # response). The old 3x loop x the client's old 4x UDT
                    # resend meant up to 12 login packets per open -- each a
                    # potential strike toward the hub's 5-fails-in-300s
                    # account lockout. If this one attempt fails, we do NOT
                    # retry here: we back the whole hub off for a full lockout
                    # window so we never accumulate strikes, and let the user
                    # re-toggle the switch once it clears.
                    _LOGGER.info("login starting (port %d)", sess.port)
                    resp = sess.login(self.username, self.password,
                                      channel=int(self.channel))
                    _LOGGER.info("login done, resp=%s reject_reason=%s",
                                 bool(resp), sess.reject_reason)

                    if not resp:
                        self.connected = False
                        if sess.reject_reason in (2, 4):
                            # 2 = auth/dispatch reject (the real brute-force
                            # path), 4 = source IP blocked. Both are genuine
                            # hub refusals -> full lockout cooldown.
                            reason_txt = {2: "auth/dispatch reject",
                                          4: "source IP blocked"}[sess.reject_reason]
                            self.last_error = f"hub rejected login: {reason_txt}"
                            _set_hub_blocked(
                                self.host, HUB_LOCKOUT_COOLDOWN_SECONDS,
                                f"login reject reason={sess.reject_reason}",
                            )
                        elif sess.reject_reason == 3:
                            # Too many connections: a transient slot-limit hit
                            # (the previous channel's session hadn't freed yet
                            # during a fast switch). Slots free within seconds
                            # -- short backoff, not the 330 s lockout.
                            self.last_error = "hub busy: too many connections"
                            _set_hub_blocked(
                                self.host, LOGIN_TRANSIENT_COOLDOWN_SECONDS,
                                "too many connections (slot limit)",
                            )
                        else:
                            # No response at all: a transient timeout, usually
                            # a brief session overlap during fast switching --
                            # NOT a brute-force strike (the hub never processed
                            # it as a failed auth). Short backoff; escalate to
                            # the full lockout only if it keeps happening.
                            n = _bump_login_fails(self.host)
                            if n >= LOGIN_MAX_TRANSIENT_FAILS:
                                self.last_error = "login failing repeatedly"
                                _set_hub_blocked(
                                    self.host, HUB_LOCKOUT_COOLDOWN_SECONDS,
                                    f"login no-response x{n}",
                                )
                            else:
                                self.last_error = "login failed (transient)"
                                _set_hub_blocked(
                                    self.host, LOGIN_TRANSIENT_COOLDOWN_SECONDS,
                                    f"login no-response x{n}",
                                )
                        try:
                            sess.send_logout()
                        except Exception:
                            pass
                        try:
                            sess.close()
                        except Exception:
                            pass
                        sess = None
                        self._sess = None
                        self.capturing = False
                        self._stop_event.set()
                        return

                    self.last_error = None
                    # Login succeeded -- clear the transient-failure streak for
                    # this hub so an earlier blip doesn't count toward escalation.
                    _reset_login_fails(self.host)
                    # NOTE: no explicit start-preview packet is sent. The real
                    # app streams from login + the proactive ACK flood alone
                    # (video starts ~2s after login, before the app ever sends
                    # a 2nd data packet). An early app->hub data packet here
                    # just desyncs the hub's UDT receiver. See 2026-10-20
                    # capture diff. send_start_av() is kept in glnk_client for
                    # reference but intentionally unused.
                    demux = StreamDemuxer(sess.stream_token or sess.token_c)
                    video_seen = False
                    session_start_time = time.monotonic()
                    last_video_time = None

                    while not self._stop_event.is_set():
                        if is_panel and not video_seen:
                            if time.monotonic() - session_start_time > give_up_seconds:
                                _LOGGER.warning(
                                    "Tantos Jolly %s channel %#x: no video for %.0fs, "
                                    "stopping capture to protect the hub",
                                    self.host, self.channel, give_up_seconds,
                                )
                                self.connected = False
                                self.last_error = "no video from panel"
                                # 2026-10-20 re-fix (lost again in the NOK
                                # rollback -- third time this exact bug has
                                # had to be re-applied this project): this
                                # used to `return` straight out, abandoning
                                # the UDT session without ever sending the
                                # 3x "80 05" logout the hub expects. A
                                # fresh MITM capture of a real failed-then-
                                # locked sequence showed the hub going
                                # completely silent (not even answering
                                # HELLO1) for the NEXT client immediately
                                # after exactly this kind of abandoned
                                # give-up -- send logout before leaving.
                                try:
                                    sess.send_logout()
                                except Exception:
                                    pass
                                try:
                                    sess.close()
                                except Exception:
                                    pass
                                self._sess = None
                                self._stop_event.set()
                                self.capturing = False
                                return

                        if (video_seen and last_video_time is not None
                                and time.monotonic() - last_video_time > give_up_seconds):
                            _LOGGER.info(
                                "Tantos Jolly %s: no video for %.1fs, reconnecting",
                                self.host, time.monotonic() - last_video_time,
                            )
                            break

                        # 2026-10-18: bound the pre-first-video wait for
                        # camera too (previously only panels had this --
                        # a camera that never got any data here would spin
                        # in iter_raw_packets() forever without ever
                        # reconnecting or logging anything).
                        #
                        # REVERTED same day: this block used to also send a
                        # periodic "proactive ACK" (an ACK-shaped packet
                        # with a plausible-but-unverified ack-sequence
                        # value) every ~100ms while waiting, on the theory
                        # that the hub's UDT sender needed to see us
                        # actively acking before it would start a video
                        # window. Live testing showed this is suspected of
                        # putting the hub into the same hub-wide anti-abuse
                        # lockdown seen before (symptom: real app can't
                        # open ANY channel, needs a physical power-cycle) --
                        # pulled out until the real trigger mechanism is
                        # understood with more confidence. Do not
                        # reintroduce without a validated hypothesis.
                        if not video_seen:
                            if time.monotonic() - session_start_time > give_up_seconds:
                                _LOGGER.info(
                                    "Tantos Jolly %s channel %#x: no video for %.0fs "
                                    "after login, reconnecting",
                                    self.host, self.channel, give_up_seconds,
                                )
                                break

                        iterator = sess.iter_raw_packets(
                            idle_timeout=0.05,
                            stop_check=self._stop_event.is_set,
                        )
                        try:
                            for raw in iterator:
                                if self._stop_event.is_set() or sess.device_closed_session:
                                    break

                                if sess.stream_token and sess.stream_token != demux.session_token:
                                    demux.session_token = sess.stream_token
                                demux.feed(raw)
                                video = demux.pop_video()

                                if video:
                                    last_video_time = time.monotonic()
                                    if not video_seen:
                                        _LOGGER.info("first video chunk, %d bytes", len(video))
                                        self._video_ready_event.set()
                                        self.connected = True
                                    video_seen = True

                                    if (self._raw_dump_file is not None
                                            and _RAW_DUMP_MAX_BYTES > 0
                                            and self._raw_dump_bytes < _RAW_DUMP_MAX_BYTES):
                                        try:
                                            self._raw_dump_file.write(video)
                                            self._raw_dump_bytes += len(video)
                                        except OSError:
                                            pass

                                    try:
                                        self._byte_queue.put(video, timeout=1.0)
                                    except queue.Full:
                                        pass
                        finally:
                            iterator.close()

                        if sess.device_closed_session:
                            break

                    self.connected = False
                    was_device_closed = sess.device_closed_session

                    if not was_device_closed:
                        # 2026-10-18 finding (re-applied -- lost in a
                        # rewrite): sending our own logout AFTER the
                        # device already sent its own 80 05 and closed
                        # the session is an extra packet referencing a
                        # session the hub already considers dead. Live
                        # testing showed this is what pushes the hub into
                        # a full anti-abuse lockdown (symptom: physical
                        # power-cycle required, breaks the real app too).
                        # Only send our own logout when WE are the one
                        # ending the session voluntarily.
                        try:
                            sess.send_logout()
                        except Exception:
                            pass
                    try:
                        sess.close()
                    except Exception:
                        pass
                    sess = None
                    self._sess = None

                    if was_device_closed and not video_seen:
                        _LOGGER.warning(
                            "Tantos Jolly %s channel %#x: hub closed session before video, "
                            "stopping capture. Please reset hub via app if needed.",
                            self.host, self.channel,
                        )
                        self.connected = False
                        self.last_error = "hub closed session (needs app reset)"
                        self._stop_event.set()
                        self.capturing = False
                        return

                    if not self._stop_event.is_set():
                        delay = 8.0 if was_device_closed else 2.0
                        self._stop_event.wait(delay)

                except Exception as err:
                    self.connected = False
                    if sess is not None:
                        try:
                            sess.close()
                        except Exception:
                            pass
                    sess = None
                    self._sess = None
                    if self._stop_event.is_set():
                        break
                    self.last_error = str(err)
                    _LOGGER.exception("reader thread error")
                    self._stop_event.wait(5.0)

            # 2026-10-18: with max_attempts=1 (now true for both channel
            # types), the outer while loop above exits on its own once the
            # single attempt is used up -- nothing was resetting
            # self.capturing, so the switch stayed stuck showing "on"
            # forever even though this thread had already finished. Only
            # applies when we're exiting because the attempt ran out, not
            # because the user asked to stop (that already reflects
            # correctly via stop_capture()).
            if not self._stop_event.is_set():
                self.capturing = False
        finally:
            if sess is not None:
                try:
                    sess.close()
                except Exception:
                    pass
            self._sess = None

    # ------------------------------------------------------------------- ffmpeg

    async def _log_ffmpeg_stderr(self, proc: asyncio.subprocess.Process) -> None:
        logged = 0
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                if logged < 40:
                    _LOGGER.warning("ffmpeg stderr: %s",
                                    line.decode(errors="replace").rstrip())
                    logged += 1
        except Exception:
            pass

    async def _ffmpeg_feeder(self) -> None:
        loop = asyncio.get_event_loop()
        playlist = os.path.join(self._hls_dir, HLS_PLAYLIST_NAME)
        seg_template = os.path.join(self._hls_dir, HLS_SEGMENT_PATTERN)
        try:
            while True:
                stderr_task = None
                try:
                    while True:
                        self._byte_queue.get_nowait()
                except queue.Empty:
                    pass

                try:
                    self._ffmpeg_proc = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-loglevel", "warning", "-err_detect", "ignore_err",
                        "-fflags", "+genpts+igndts+discardcorrupt",
                        "-use_wallclock_as_timestamps", "1",
                        "-f", "h264", "-probesize", "500000", "-analyzeduration", "1000000",
                        "-i", "pipe:0", "-c:v", "libx264", "-preset", "ultrafast",
                        "-tune", "zerolatency", "-bf", "0",
                        "-x264-params", "bframes=0:sync-lookahead=0:rc-lookahead=0",
                        "-pix_fmt", "yuv420p", "-g", "25", "-keyint_min", "25",
                        "-fps_mode", "cfr", "-r", "25",
                        "-f", "hls", "-hls_time", str(HLS_SEGMENT_SECONDS),
                        "-hls_list_size", str(HLS_PLAYLIST_SIZE),
                        "-hls_flags", "delete_segments+omit_endlist",
                        "-hls_segment_type", "mpegts",
                        "-hls_segment_filename", seg_template, playlist,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stderr_task = asyncio.create_task(self._log_ffmpeg_stderr(self._ffmpeg_proc))
                    while self._ffmpeg_proc.returncode is None:
                        try:
                            chunk = await loop.run_in_executor(
                                None, self._byte_queue.get, True, 1.0,
                            )
                        except queue.Empty:
                            continue
                        try:
                            self._ffmpeg_proc.stdin.write(chunk)
                            await self._ffmpeg_proc.stdin.drain()
                        except (BrokenPipeError, ConnectionResetError):
                            break
                except FileNotFoundError:
                    _LOGGER.error("ffmpeg not found")
                    return
                except Exception:
                    _LOGGER.exception("ffmpeg feeder error")

                if self._ffmpeg_proc and self._ffmpeg_proc.returncode is None:
                    self._ffmpeg_proc.terminate()
                if stderr_task:
                    await asyncio.sleep(0.2)
                    stderr_task.cancel()
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            if self._ffmpeg_proc and self._ffmpeg_proc.returncode is None:
                self._ffmpeg_proc.terminate()
            raise