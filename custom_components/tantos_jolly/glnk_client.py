"""
Working Python client for the Tantos Jolly / Goolink UDP protocol.

Protocol notes (derived from real vhOme app captures on the wire):

  * The hub does NOT listen on a fixed UDP port.  We have observed it using
    22087 and 56492 on the same physical unit across different sessions.
    The actual service port is *learned by probing*: the `handshake()` method
    sends HELLO1 to every candidate port and uses the first one that answers.

  * Panel channels (0x01/0x02/0x04/0x08) require an extra 64-byte packet
    between HELLO1 and HELLO2, with a ~400 ms pause after it.

  * Camera channels (0x10/0x20/0x40/0x80) skip that packet -- HELLO2 is sent
    immediately (~1 ms) after HELLO1 response.
"""
from __future__ import annotations

import logging
import os
import random
import socket
import time

from .const import (
    HUB_DISCOVERY_PORTS,
    HUB_SERVICE_PORTS,
)

_LOGGER = logging.getLogger(__name__)


def h2b(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split())


_HELLO1_TMPL = h2b(
    "80 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 04 00 00 00 01 "
    "39 bf 03 a3 00 00 05 6a 00 00 03 e8 00 00 00 01 37 bf 08 a0 00 00 00 00 "
    "1b 00 a8 c0 00 00 00 00 00 00 00 00 00 00 00 00"
)
_HELLO2_TMPL = h2b(
    "80 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 04 00 00 00 01 "
    "39 bf 03 a3 00 00 05 6a 00 00 03 e8 ff ff ff ff 37 bf 08 a0 92 90 b1 7a "
    "1b 00 a8 c0 00 00 00 00 00 00 00 00 00 00 00 00"
)
_LOGIN_ENVELOPE_PREFIX = h2b("c0 00 00 01 00 02 c0 8b")
_LOGIN_TLV_HEADER = h2b("00 00 00 54 00 00 00 00 28 00 04 00 05 00 00 00")
# 2026-10-18 finding: Wireshark's own UDT dissector identifies this whole
# protocol as genuine UDT (UDP-based Data Transfer, a real documented
# library), not a bespoke "GLNK" scheme -- confirmed byte-for-byte against a
# real vhOme capture (tshark -V field decode). What we'd been calling
# "trigger"/"ack" packets are literally UDT control types: 0x80 0x02 = ACK
# (type 2), 0x80 0x06 = ACK2 (type 6), 0x80 0x05 = Shutdown (type 5),
# 0x80 0x01 = Keep-alive (type 1), 0x80 0x00 = Handshake (type 0).
#
# A real ACK (type 2) is 40 bytes: 16-byte control header (type word +
# "Ack Number" (this ACK message's own id, for ACK2 pairing) + timestamp +
# destination socket id) followed by a 24-byte body: cumulative
# ack-sequence-number (next expected data seq), RTT, RTT variance, receive
# buffer available (packets), receive rate (packets/sec), estimated link
# capacity (packets/sec). The previous _ACK_DATA_TMPL here had the type
# marker in the WRONG 4-byte word (word1 instead of word0) and was only
# 20 bytes total (missing RTT/RTTVar/BufferAvailable/Rate/LinkCapacity) --
# send_ack_data() now builds the real 40-byte shape.
_ACK_DATA_TMPL = h2b(
    "80 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 "
    "00 00 00 00 00 00 00 00 00 00 00 00 00 00 03 e8 "
    "00 00 00 01 00 00 03 e8"
)
# Real ACK2 (type 6) is 20 bytes: 16-byte header whose "Ack Number" field
# (offset 4:8) must ECHO the Ack Number of the ACK it's confirming -- NOT a
# constant. send_ack2()/​_maybe_ack_device_ping() now copy it from the
# incoming packet instead of hardcoding "00 00 00 01".
_ACK_CTRL_TMPL = h2b("80 06 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00")
# Real NAK (type 3) is 20 bytes for a single missing sequence number: header
# + the missing seq itself. Same word-alignment fix as the ACK template.
_NAK_TMPL = h2b("80 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00")
_LOGOUT_TMPL   = h2b("80 05 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00")

_DISCOVER = h2b("07 a0 20 00" + " 00" * 32)


def _pad(b: bytes, n: int) -> bytes:
    b = b[:n]
    return b + b"\x00" * (n - len(b))


def _find_nal_type(data: bytes, nal_type: int) -> int:
    i = 0
    n = len(data)
    while True:
        idx4 = data.find(b"\x00\x00\x00\x01", i)
        idx3 = data.find(b"\x00\x00\x01", i)
        if idx4 == -1 and idx3 == -1:
            return -1
        if idx4 != -1 and (idx3 == -1 or idx4 <= idx3):
            idx, hdr_pos = idx4, idx4 + 4
        else:
            idx, hdr_pos = idx3, idx3 + 3
        if hdr_pos < n and (data[hdr_pos] & 0x1F) == nal_type:
            return idx
        i = idx + 3


CHANNEL_PANEL_1 = 0x01
CHANNEL_PANEL_2 = 0x02
CHANNEL_PANEL_3 = 0x04
CHANNEL_PANEL_4 = 0x08
CHANNEL_CAMERA_1 = 0x10
CHANNEL_CAMERA_2 = 0x20
CHANNEL_CAMERA_3 = 0x40
CHANNEL_CAMERA_4 = 0x80

_LOGIN_TAIL_CANDIDATES = [h2b("ff 7f"), h2b("ff ff"), h2b("00 00")]


def build_login_packet(username: str, password: str, channel: int = CHANNEL_CAMERA_3) -> bytes:
    buf = bytearray(72)
    buf[0:4] = h2b("29 00 44 00")
    buf[5:37] = _pad(username.encode(), 32)
    buf[37:53] = _pad(password.encode(), 16)
    buf[66] = channel & 0xFF
    buf[67] = 0x00
    buf[68] = 0x01
    buf[69] = 0x02
    tail = _LOGIN_TAIL_CANDIDATES[random.randrange(len(_LOGIN_TAIL_CANDIDATES))]
    buf[70:72] = tail
    return bytes(buf)


class GlnkSession:
    """One UDP session against a Tantos Jolly hub.

    `port` is an OPTIONAL hint.  If `None` (the default) the session will
    probe every port in `const.HUB_SERVICE_PORTS` until the hub answers.
    If you have previously discovered the hub's port (e.g. on a previous
    capture session in the same HA run), pass it as `port` to skip the scan.
    """

    def __init__(self, host: str, port: int | None = None, timeout: float = 3.0):
        self.host = host
        self.port: int = port if port is not None else HUB_SERVICE_PORTS[0]
        self._hint_port: int | None = port

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 * 1024 * 1024)
        except OSError:
            pass

        self.sock.settimeout(timeout)
        self.sock.bind(("0.0.0.0", 0))

        self.session_key: bytes | None = None
        self.token_a: bytes | None = None
        self.token_b: bytes | None = None
        self.token_c: bytes | None = None
        self.temp_token_c: bytes | None = None
        self.last_recv_seq: bytes | None = None
        self.stream_token: bytes | None = None
        self.device_closed_session = False
        # Set by login() when the hub answers with a type-0x64 reject packet
        # (reverse-engineered from Sofia's FUN_00050370): 2 = auth/dispatch
        # reject, 3 = too many connections (>11 slots), 4 = source IP blocked.
        # None means "no reject seen". Best-effort -- see _scan_for_reject().
        self.reject_reason: int | None = None
        self.channel: int = CHANNEL_CAMERA_3
        self._expected_seq = 0
        # Our own outgoing-ACK message counter (UDT "Ack Number", used by the
        # peer to pair a later ACK2 back to this specific ACK).
        self._ack_counter = 0
        # Highest raw DATA sequence number seen so far (data[0:4] of any
        # packet whose top bit is 0) -- used to report the cumulative
        # "next expected sequence number" in our own ACKs.
        self._max_data_seq: int | None = None

    # ------------------------------------------------------------------ low level

    def send_ack_data(self, next_expected_seq: bytes) -> None:
        """Send a real 40-byte UDT ACK acknowledging data up to
        `next_expected_seq` (the cumulative "I have everything before
        this" pointer, i.e. highest-seen data seq + 1)."""
        self._ack_counter += 1
        pkt = bytearray(_ACK_DATA_TMPL)
        pkt[4:8] = self._ack_counter.to_bytes(4, "big")
        if self.token_c is not None:
            pkt[12:16] = self.token_c
        pkt[16:20] = next_expected_seq
        try:
            self.sock.sendto(bytes(pkt), (self.host, self.port))
        except OSError:
            pass

    # 2026-10-18: a send_proactive_ack() experiment (periodic ACK-shaped
    # packet with a plausible-but-unverified ack-sequence value, sent every
    # ~100ms before any real video arrived, on the theory that the hub's
    # UDT sender needed to see us actively acking first) was tried and
    # REVERTED the same day -- live testing showed it's suspected of
    # pushing the hub into its hub-wide anti-abuse lockdown (symptom: the
    # real app can't open ANY channel afterward, needs a physical
    # power-cycle). Do not reintroduce without a validated hypothesis for
    # what the hub actually needs to see before it starts streaming.

    def send_nak_data(self, seqno: bytes) -> None:
        pkt = bytearray(_NAK_TMPL)
        if self.token_c is not None:
            pkt[12:16] = self.token_c
        pkt[16:20] = seqno
        try:
            self.sock.sendto(bytes(pkt), (self.host, self.port))
        except OSError:
            pass

    def _maybe_ack_device_ping(self, data: bytes) -> None:
        if len(data) >= 2 and data[0] == 0x80 and data[1] == 0x05:
            self.device_closed_session = True

        if (len(data) >= 20 and data[0] == 0x80 and data[1] == 0x02
            and self.token_c is not None and data[12:16] != self.token_c):
            self.stream_token = bytes(data[12:16])
            ack = bytearray(_ACK_CTRL_TMPL)
            # Real UDT ACK2: "Ack Number" (offset 4:8) must echo the
            # incoming ACK's own Ack Number so the peer can pair this
            # reply to that specific ACK -- confirmed via tshark field
            # decode (frame53 Ack Number=1 -> frame55 ACK2 Ack Number=1).
            ack[4:8] = data[4:8]
            ack[12:16] = self.token_c
            try:
                self.sock.sendto(bytes(ack), (self.host, self.port))
            except OSError:
                pass

    def _send_recv(self, pkt: bytes, dest=None, retries=4, validate=None, timeout=None):
        dest = dest or (self.host, self.port)
        original_timeout = self.sock.gettimeout()
        slot_budget = timeout if timeout is not None else (original_timeout or 3.0)
        try:
            for _ in range(retries):
                self.sock.sendto(pkt, dest)
                deadline = time.monotonic() + slot_budget
                for _ in range(8):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.sock.settimeout(remaining)
                    try:
                        data, _addr = self.sock.recvfrom(8192)
                    except (socket.timeout, ConnectionResetError):
                        break
                    if len(data) >= 4:
                        self.last_recv_seq = data[:4]
                    self._maybe_ack_device_ping(data)
                    if self.device_closed_session:
                        return None
                    if validate is None or validate(data):
                        return data
            return None
        finally:
            self.sock.settimeout(original_timeout)

    # -------------------------------------------------------------- discovery

    def _probe_port(self, port: int, timeout: float = 0.6) -> bool:
        """Send HELLO1 to `port` and see if the hub answers.

        On success stores token_a, token_b and session_key so the caller
        can proceed directly with the rest of the handshake.
        """
        # token_a is the UDT initial sequence number: it becomes word0 of our
        # LOGIN (and every app->hub data packet), and in UDT word0's top bit
        # marks control(1) vs data(0). It MUST be < 0x80000000 or the hub
        # parses our LOGIN as a control packet, never ACKs it, and never
        # starts the stream (verified 2026-10-20: a tcpdump with token_a=
        # 0xc080f3aa got only keep-alives + no video; every real-app token_a
        # is < 0x80). os.urandom(4) alone set the top bit ~50% of the time --
        # the real root cause of the camera/panel "sometimes no video".
        self.token_a = bytes([os.urandom(1)[0] & 0x7F]) + os.urandom(3)
        self.token_b = os.urandom(4)
        self.token_c = None
        self.session_key = None
        self.temp_token_c = None
        self.port = port

        hello1 = bytearray(_HELLO1_TMPL)
        hello1[24:28] = self.token_a
        hello1[40:44] = self.token_b

        try:
            resp = self._send_recv(
                bytes(hello1),
                dest=(self.host, port),
                retries=1,
                timeout=timeout,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s:%d probe exception: %s", self.host, port, err)
            return False

        if not resp or len(resp) < 48:
            _LOGGER.debug("%s:%d probe: no valid HELLO1 response", self.host, port)
            return False

        self.session_key = resp[44:48]
        # Hub echoes token_b at resp1[40:44] -- needed for the panel packet.
        self.temp_token_c = resp[40:44]
        return True

    def _probe_via_discover_broadcast(self) -> bool:
        """Fallback used when none of the hardcoded candidate ports answer:
        query the hub's DISCOVER responder (port 1500/6800) for its CURRENT
        service port and probe that. The hub re-randomizes this port on
        every reboot, so the hardcoded candidate list alone goes stale the
        first time the hub is power-cycled -- this is the only way to find
        it again without a fresh capture."""
        for dport in HUB_DISCOVERY_PORTS:
            try:
                resp = self._send_recv(
                    _DISCOVER, dest=(self.host, dport), retries=1, timeout=0.3,
                )
            except Exception:  # noqa: BLE001
                resp = None
            if resp and len(resp) >= 38:
                # 2026-10-18 fix: this is 4 bytes too early -- confirmed
                # against a real broadcast capture that the LE uint16
                # service port sits right before the ASCII IP string, at
                # offset 36:38 (e.g. "e1 76" = 30433), not 32:34 (which is
                # just zero padding in every sample seen).
                advertised = int.from_bytes(resp[36:38], "little")
                if 1024 < advertised < 65535:
                    _LOGGER.info(
                        "%s: DISCOVER on %d advertised service port %d",
                        self.host, dport, advertised,
                    )
                    if self._probe_port(advertised):
                        return True
        return False

    # -------------------------------------------------------------- handshake

    def _build_panel_packet(self) -> bytes:
        """Build the 64-byte packet sent between HELLO1 and HELLO2 for panels."""
        token_a = self.token_a or b"\x00\x00\x00\x00"
        temp_token_c = self.temp_token_c or b"\x00\x00\x00\x00"
        session_key = self.session_key or b"\x00\x00\x00\x00"

        buf = bytearray(64)
        buf[0:4] = h2b("00 00 00 80")
        buf[4:12] = h2b("00 00 00 00 00 00 00 00")
        buf[12:16] = h2b("00 00 00 04")
        buf[16:20] = h2b("00 00 00 01")
        buf[20:23] = b"\x00\x00\x00"
        buf[23:27] = token_a[::-1]
        buf[27:29] = h2b("6a 05")
        buf[29:31] = b"\x00\x00"
        buf[31:35] = h2b("e8 03 00 00")
        buf[35:39] = h2b("ff ff ff ff")
        buf[39:43] = temp_token_c[::-1]
        buf[43:47] = session_key[::-1]
        buf[47:51] = socket.inet_aton(self.host)
        buf[51:64] = b"\x00" * 13
        return bytes(buf)

    def _finish_handshake(self) -> bool:
        """Called after a successful probe: send panel packet (if applicable)
        and HELLO2.  Populates self.token_c on success."""
        if self.token_a is None or self.token_b is None or self.session_key is None:
            _LOGGER.error("%s: _finish_handshake called without probe state", self.host)
            return False

        # Panels need the 64-byte packet + ~400 ms wait.
        if int(getattr(self, "channel", CHANNEL_CAMERA_3)) <= 0x08:
            try:
                self.sock.sendto(self._build_panel_packet(), (self.host, self.port))
            except OSError as err:
                _LOGGER.warning("%s: panel packet send failed: %s", self.host, err)
                return False
            time.sleep(0.4)

        hello2 = bytearray(_HELLO2_TMPL)
        hello2[24:28] = self.token_a
        hello2[40:44] = self.token_b
        hello2[44:48] = self.session_key

        try:
            resp2 = self._send_recv(
                bytes(hello2), retries=2, timeout=1.5,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("%s: HELLO2 exception on port %d: %s",
                            self.host, self.port, err)
            return False

        if not resp2 or len(resp2) < 44:
            _LOGGER.warning(
                "%s: HELLO2 no response on port %d (len=%d)",
                self.host, self.port, len(resp2 or b""),
            )
            return False

        self.token_c = resp2[40:44]
        return True

    def handshake(self) -> bool:
        """Find the hub's current service port and complete the handshake.

        2026-10-19: reordered after a full day of real captures showed the
        hardcoded `HUB_SERVICE_PORTS` list (22087/56492/37020/44418) has
        NEVER once answered -- only the port DISCOVER (1500/6800) actually
        advertises ever responds. The old code scanned all 5 hardcoded
        ports FIRST on every single call (~3.6s, 5 wasted UDP packets to
        the hub) before ever trying DISCOVER, and this method gets called
        repeatedly by `bridge.py`'s retry loop -- multiplying that waste
        into real, needless hammering of the hub on every reconnect
        attempt. Now: try the previously-learned port (if any, 1 cheap
        packet), then DISCOVER (the thing that actually works), and only
        fall back to scanning the dead hardcoded list as a last resort.
        """
        if self.token_c is not None:
            return True

        if self._hint_port:
            _LOGGER.debug("%s: trying handshake on hinted port %d", self.host, self._hint_port)
            if self._probe_port(self._hint_port) and self._finish_handshake():
                _LOGGER.info("%s: handshake OK on hinted port %d", self.host, self._hint_port)
                return True

        if self._probe_via_discover_broadcast():
            if self._finish_handshake():
                _LOGGER.info("%s: handshake OK via DISCOVER on port %d",
                             self.host, self.port)
                return True

        for port in HUB_SERVICE_PORTS:
            if port == self._hint_port:
                continue
            _LOGGER.debug("%s: trying handshake on fallback port %d", self.host, port)
            if not self._probe_port(port):
                continue
            if self._finish_handshake():
                _LOGGER.info("%s: handshake OK on fallback port %d", self.host, port)
                return True

        _LOGGER.warning(
            "%s: handshake failed via hint/DISCOVER and all fallback ports %r",
            self.host, HUB_SERVICE_PORTS,
        )
        return False

    # ------------------------------------------------------------------- login

    @staticmethod
    def _scan_for_reject(data: bytes, token_a: bytes) -> int | None:
        """LOG-ONLY probe for a possible type-0x64 reject from the hub.

        Sofia builds rejects in FUN_00050370 as an app-layer message whose
        first byte is 0x64 (100) with a reason byte further in (2/3/4). We do
        NOT yet have a captured reject to pin the exact wire offset, and a
        naive "short packet containing 0x64" test false-positives badly: the
        hub's normal 60-byte device-info reply carries the device UID
        (e.g. cgXXXXXXXX) whose bytes happen to include 0x64. So this NEVER
        changes control flow -- it only logs a candidate at DEBUG for offline
        analysis. It returns None always; wire up a real reason only once a
        genuine reject has been captured and the exact layout confirmed.
        """
        if not data or len(data) < 4 or data[:4] != token_a or len(data) >= 96:
            return None
        idx = data.find(0x64, 4)
        if idx != -1:
            _LOGGER.debug(
                "login: short hub reply with 0x64@%d (len=%d) -- NOT treated "
                "as a reject (needs a real captured reject to confirm): %s",
                idx, len(data), data.hex(),
            )
        return None

    def login(self, username: str, password: str, channel: int = CHANNEL_CAMERA_3) -> bytes | None:
        if self.token_c is None:
            raise RuntimeError("call handshake() first")
        self.channel = channel
        self.reject_reason = None
        login_struct = build_login_packet(username, password, channel)
        envelope = (bytearray(self.token_a or b"\x00\x00\x00\x00")
                    + bytearray(_LOGIN_ENVELOPE_PREFIX)
                    + bytearray(self.token_c)
                    + bytearray(_LOGIN_TLV_HEADER)
                    + bytearray(login_struct))
        token_a = bytes(self.token_a or b"\x00\x00\x00\x00")

        def _is_login_response(data: bytes) -> bool:
            return len(data) >= 4 and data[:4] == token_a

        # 2026-10-20: send the LOGIN exactly ONCE, like the real app does
        # (panel-3 capture frame 1681 -> response frame 1829, a single send).
        # The hub locks an account for 300 s after 5 failed auths (Sofia
        # CUserManager, N=5/T=300 -- see const.py), so the previous
        # retries=4 resend-storm was actively dangerous: a single unanswered
        # open could fire up to 4 login packets, and the bridge wrapped that
        # in a 3x loop (12 total). One send, then let the caller back off.
        resp = self._send_recv(bytes(envelope), retries=1,
                               validate=_is_login_response)

        # A hub reject comes back on the same socket and still starts with
        # token_a, so it would pass _is_login_response and masquerade as a
        # successful login. Screen it out and surface the reason instead.
        if resp is not None:
            reason = self._scan_for_reject(resp, token_a)
            if reason is not None:
                self.reject_reason = reason
                return None
        return resp

    # ------------------------------------------------------------------- stream

    @staticmethod
    def _is_udt_control(data: bytes) -> bool:
        """True for a UDT control packet (ACK/ACK2/NAK/Shutdown/Keep-alive/
        Handshake). These start with 0x80 and a small type byte; everything
        else on this socket is an application data packet whose first 4 bytes
        are a sequence number anchored at token_a (which may itself have its
        top bit set, so `data[0] < 0x80` is NOT a reliable data test)."""
        return (len(data) >= 4 and data[0] == 0x80
                and data[1] in (0x00, 0x01, 0x02, 0x03, 0x05, 0x06)
                and data[2] == 0x00 and data[3] == 0x00)

    @staticmethod
    def _seq_after(a: int, b: int) -> bool:
        """True if 31-bit UDT sequence `a` is strictly after `b`, using
        serial-number arithmetic so the 0x7fffffff -> 0 wrap is handled."""
        return 0 < ((a - b) & 0x7FFFFFFF) < 0x40000000

    def _drain_pending(self, pending: dict[int, bytes]):
        """Yield buffered packets that are now contiguous with
        `_expected_seq`, advancing `_expected_seq` past each one."""
        while True:
            nxt = pending.pop(self._expected_seq, None)
            if nxt is None:
                return
            yield nxt
            self._expected_seq = (self._expected_seq + 1) & 0x7FFFFFFF

    def iter_raw_packets(self, idle_timeout: float = 10.0, stop_check=None,
                         ack_interval: float = 0.1):
        original_timeout = self.sock.gettimeout()
        # Poll at the ACK cadence so we can drive the UDT receiver even while
        # the hub is silent.
        self.sock.settimeout(ack_interval)

        # PROACTIVE cumulative ACK. The real vhOme app, for BOTH camera and
        # panel, floods 80 02 ACKs every ~100 ms starting the instant login
        # completes; the hub will not begin streaming until it sees the
        # receiver acking. The cumulative "next expected sequence number"
        # starts at token_a + 1 and grows to (highest data seq seen + 1) once
        # video arrives. Our old code only acked in REACTION to a packet with
        # `data[0] < 0x80`, so whenever token_a's top bit was set (a coin
        # flip) we never acked and got no video -- the real cause of the
        # camera/panel flakiness, verified by a tcpdump diff vs the real app
        # (2026-10-20). We now ack proactively, independent of token_a.
        base_seq = (((int.from_bytes(self.token_a, "big") + 1) & 0xFFFFFFFF)
                    if self.token_a else 1)
        last_ack = 0.0
        last_data = time.monotonic()

        # ---- UDT in-order resequencing --------------------------------
        # The hub splits large video frames (keyframes) across several UDT
        # DATA packets (First/middle/Last flags) and RETRANSMITS lost ones.
        # The StreamDemuxer can only rebuild a frame if it sees those packets
        # strictly in sequence order; on a UDP socket they can arrive
        # reordered, and a retransmit arrives late. Feeding them to the
        # demuxer in ARRIVAL order (the old behaviour) splices bytes mid-NAL
        # and corrupts the frame -- the "static picture / corrupt frame"
        # symptom on a busy LAN (confirmed by capture 2026-10-20: ~1%
        # reorder/dupes was enough to wreck every multi-packet keyframe,
        # while short static-scene sessions with single-packet frames looked
        # fine). So we resequence here: deliver data packets in ascending
        # seq, drop duplicates/late ones, NAK a gap once so the hub
        # fast-retransmits, and skip past a gap left unfilled for
        # `gap_timeout` so a genuine (rare) loss can't stall forever.
        # Control packets bypass all of this and pass through immediately.
        pending: dict[int, bytes] = {}
        have_expected = False
        gap_since: float | None = None
        gap_timeout = 0.25
        naked_gap: int | None = None
        try:
            while True:
                if stop_check is not None and stop_check():
                    return

                now = time.monotonic()
                if now - last_ack >= ack_interval:
                    # Correct cumulative UDT ACK: the next seq we still need
                    # IN ORDER -- not max-seen+1, which would falsely tell the
                    # hub a gap was received and defeat retransmission. When
                    # the link is clean the two are identical, so this is a
                    # no-op vs the old behaviour except across a real gap.
                    nxt = (self._expected_seq & 0xFFFFFFFF
                           if have_expected else base_seq)
                    self.send_ack_data(nxt.to_bytes(4, "big"))
                    last_ack = now

                # Give up on a gap the hub never refilled: resume from the
                # earliest buffered packet so the stream keeps moving.
                if (pending and gap_since is not None
                        and now - gap_since >= gap_timeout):
                    self._expected_seq = min(
                        pending, key=lambda s: (s - self._expected_seq) & 0x7FFFFFFF)
                    gap_since = None
                    naked_gap = None
                    for pkt in self._drain_pending(pending):
                        yield pkt

                try:
                    data, _addr = self.sock.recvfrom(16384)
                except socket.timeout:
                    if time.monotonic() - last_data >= idle_timeout:
                        return
                    continue
                except ConnectionResetError:
                    continue
                except OSError:
                    return

                last_data = time.monotonic()
                if len(data) >= 4:
                    self.last_recv_seq = data[:4]

                # Control packets are not part of the data sequence space --
                # handle their side effects and pass them straight through.
                if self._is_udt_control(data) or len(data) < 4:
                    self._maybe_ack_device_ping(data)
                    yield data
                    continue

                seq = int.from_bytes(data[0:4], "big") & 0x7FFFFFFF
                if self._max_data_seq is None or seq > self._max_data_seq:
                    self._max_data_seq = seq
                self._maybe_ack_device_ping(data)

                if not have_expected:
                    self._expected_seq = seq
                    have_expected = True

                if seq == self._expected_seq:
                    yield data
                    self._expected_seq = (self._expected_seq + 1) & 0x7FFFFFFF
                    gap_since = None
                    naked_gap = None
                    for pkt in self._drain_pending(pending):
                        yield pkt
                elif self._seq_after(seq, self._expected_seq):
                    # A gap is open ahead of us -- buffer this packet and NAK
                    # the first missing seq once so the hub retransmits it.
                    pending[seq] = data
                    if gap_since is None:
                        gap_since = now
                    if naked_gap != self._expected_seq:
                        self.send_nak_data(self._expected_seq.to_bytes(4, "big"))
                        naked_gap = self._expected_seq
                    if len(pending) > 512:
                        # Runaway gap: skip it rather than buffer unbounded.
                        self._expected_seq = min(
                            pending,
                            key=lambda s: (s - self._expected_seq) & 0x7FFFFFFF)
                        gap_since = None
                        naked_gap = None
                        for pkt in self._drain_pending(pending):
                            yield pkt
                # else seq < expected: duplicate / late retransmit of data we
                # already delivered -- drop it silently.
        finally:
            self.sock.settimeout(original_timeout)

    def send_start_av(self) -> None:
        """Send the panel 'start video preview' request -- the 2nd app-data
        packet after login (mirrors the real app's frame 1817). The hub
        streams PANEL video only after receiving this; the camera starts from
        the proactive ACK flood alone and needs no such packet. Bytes verified
        against real_app_capture_panel3_readable.txt frame 1817:
          seq=token_a+1 | c0 00 00 02 | msg-seq | token_c | len=0x0c | payload
        """
        if self.token_a is None or self.token_c is None:
            return
        seq = (int.from_bytes(self.token_a, "big") + 1) & 0xFFFFFFFF
        pkt = bytearray()
        pkt += seq.to_bytes(4, "big")
        pkt += h2b("c0 00 00 02")
        pkt += h2b("00 02 c0 8c")   # msg-seq/ts slot (login used 00 02 c0 8b)
        pkt += self.token_c
        pkt += h2b("00 00 00 0c")   # payload length 12
        pkt += h2b("00 00 00 00 31 00 04 00 12 00 00 00")
        try:
            self.sock.sendto(bytes(pkt), (self.host, self.port))
        except OSError:
            pass

    def send_logout(self) -> None:
        if self.token_c is None:
            return
        pkt = bytearray(_LOGOUT_TMPL)
        pkt[12:16] = self.token_c
        for _ in range(3):
            try:
                self.sock.sendto(bytes(pkt), (self.host, self.port))
            except OSError:
                break

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class StreamDemuxer:
    """Reassembles GLNK audio/video messages out of UDT DATA packets.

    2026-10-18 rewrite: confirmed via tshark field decode (real capture)
    that every packet carries a genuine 16-byte UDT DATA header (seq +
    msgnum/flags + timestamp + dest socket id) BEFORE the GLNK payload --
    the previous version read `data[8]`/`data[24:]` directly off the raw
    packet, ignoring that header entirely, so it was reading garbage
    (part of the UDT timestamp field) as the GLNK type marker and feeding
    the wrong bytes to ffmpeg. The UDT header's "First"/"Last" flags (top
    2 bits of word1) mark which packets start a new logical GLNK message
    -- a single audio/video unit can span several UDT packets, and only
    the FIRST one carries the GLNK-level sub-header (8-byte counter/
    reserved pair + 4-byte type marker + per-type fields); continuation
    packets are pure raw bitstream, appended with no sub-header at all.
    Confirmed by direct byte comparison against a real capture:
      - audio (type 0x61): 24-byte sub-header, then raw G.711 samples.
      - video (type 0x63): 32-byte sub-header (which embeds its own
        Annex-B start code in the last 4 bytes), then the raw NAL body
        starting at the NAL header byte -- we prepend a fresh start code
        ourselves rather than reusing the hub's.
    The previous version also had the 0x61 (audio) branch appending into
    video_buf instead of audio_buf, corrupting the H.264 stream with raw
    PCM whenever audio was present.
    """

    def __init__(self, session_token: bytes):
        self.session_token = session_token
        self.video_buf = bytearray()
        self.audio_buf = bytearray()
        self._synced = False
        # Which buffer continuation fragments (First flag not set) belong
        # to for the message currently being reassembled -- "video",
        # "audio", or None to drop (e.g. a video GOP we're not synced to
        # yet, or an unrecognized message type).
        self._current_target: str | None = None

    def feed(self, data: bytes) -> None:
        # 16-byte UDT DATA header. Control packets (0x80 0x00-0x07) never
        # carry stream payload and are handled elsewhere (acks/pings).
        if len(data) < 16 or data[0] >= 0x80:
            return

        is_first = (data[4] & 0x80) != 0
        payload = data[16:]

        if not is_first:
            # Continuation of whatever message we're already in the
            # middle of -- no sub-header here, just append raw.
            if self._current_target == "video":
                self.video_buf += payload
            elif self._current_target == "audio":
                self.audio_buf += payload
            return

        if len(payload) < 12:
            self._current_target = None
            return

        msg_type = payload[8]

        if msg_type == 0x61:
            # Audio (G.711): 24-byte sub-header, then raw samples.
            self._current_target = "audio"
            self.audio_buf += payload[24:]

        elif msg_type == 0x63:
            # Video: 32-byte sub-header (embeds its own start code in the
            # last 4 bytes), then the raw NAL body from its header byte.
            nal_body = payload[32:]
            if len(nal_body) == 0:
                self._current_target = None
                return
            nal_header = nal_body[0]
            if nal_header in (0x65, 0x67, 0x68, 0x64, 0x61):
                if not self._synced:
                    if nal_header in (0x67, 0x65):
                        self._synced = True
                    else:
                        self._current_target = None
                        return
                self._current_target = "video"
                self.video_buf += b"\x00\x00\x00\x01" + nal_body
            else:
                self._current_target = None

        else:
            self._current_target = None

    def pop_video(self) -> bytes:
        b = bytes(self.video_buf)
        self.video_buf.clear()
        return b

    def pop_audio(self) -> bytes:
        b = bytes(self.audio_buf)
        self.audio_buf.clear()
        return b