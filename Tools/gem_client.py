"""Gem socket bridge.

The gem-injected code running inside the game on the Switch is a TCP *client*
that dials *out* to a configured `server=<ip> port=<port>`. So we are the
server: we bind, listen, and the console connects to us (same model the
Rainbow reference caller uses). One console = one connection.

On that single socket, packets are interleaved. Wire format
(`gem/source/program/comms/protocol.hpp`):

    PacketHeader  : 4 bytes  -> type:u8, size:u24 little-endian
    <body>        : `size` bytes

    Heartbeat(0)                 keepalive ping/pong   -> outbound thread sends
                                                          Heartbeat(magic=N), gem
                                                          echoes back with magic=N+1.
                                                          Used purely to keep the
                                                          socket (and any NAT/firewall
                                                          state on the path) warm; a
                                                          missed echo tears the
                                                          connection down.
    ReplayReq(1)                 we send this         -> 17-byte body: 16-char
                                                         code + NUL
    ReplayResp(2)                success              -> body is raw replay bytes
    Err(3)                       failure              -> body is u32 ErrorType LE
                                                         (0 BadReplayCode,
                                                          1 ReplayDownloadFailure)
    UploadReplayNotification(4)  replay was uploaded   -> forward the code to
                                                          the on_replay_code
                                                          handler (off-thread)
    FriendPlayingNotification(5) friend playing event   -> forward the parsed
                                                          (timestamp, nsa, subtype,
                                                          match_mode, sender) to
                                                          on_friend_playing (off-thread)

Concurrency: gem refuses more than one replay at a time — sending a second
ReplayReq while it is still working *crashes the game*. submit() therefore
serializes on a single in-flight lock and only returns once the matching
ReplayResp / Err / timeout has been observed, so callers can safely enqueue
codes one at a time.
"""

import socket
import struct
import threading
import time


# --- Protocol constants ---

PKT_HEARTBEAT = 0
PKT_REPLAY_REQ = 1
PKT_REPLAY_RESP = 2
PKT_ERR = 3
PKT_UPLOAD_NOTIFICATION = 4
PKT_FRIEND_NOTIFICATION = 5

REPLAY_CODE_LENGTH = 16
REPLAY_REQ_BODY_SIZE = REPLAY_CODE_LENGTH + 1  # char m_Code[17]

# UploadReplayNotificationBody (protocol.hpp:57): u64 timestamp @0,
# u64 m_NsaId @8, char m_Sender[23] @16, char m_ReplayCode[17] @39, total
# 56 bytes. NSA ID and the NPLN ID (sender) are on the wire but Plannink
# doesn't forward them anywhere — only the code is consumed.
UPLOAD_NOTIFICATION_CODE_OFFSET = 39
UPLOAD_NOTIFICATION_BODY_SIZE = 56

# FriendPlayingNotificationBody (protocol.hpp:64): u64 timestamp @0,
# u64 m_NsaId @8, u8 m_Subtype @16, u32 m_MatchMode @20 (3B pad in between),
# char m_Sender[23] @24, total 48 bytes.
NPLN_ID_LENGTH = 22
FRIEND_NOTIFICATION_BODY_SIZE = 48
FRIEND_NOTIFICATION_TIMESTAMP_OFFSET = 0
FRIEND_NOTIFICATION_NSA_ID_OFFSET = 8
FRIEND_NOTIFICATION_SUBTYPE_OFFSET = 16
FRIEND_NOTIFICATION_MATCH_MODE_OFFSET = 20
FRIEND_NOTIFICATION_SENDER_OFFSET = 24

# FriendPlayingSubtype (protocol.hpp:28)
FRIEND_SUBTYPE_START_SOLO = 0
FRIEND_SUBTYPE_CREATE_ROOM = 1
FRIEND_SUBTYPE_JOIN_ROOM = 2

FRIEND_SUBTYPE_NAMES = {
    FRIEND_SUBTYPE_START_SOLO: 'StartSolo',
    FRIEND_SUBTYPE_CREATE_ROOM: 'CreateRoom',
    FRIEND_SUBTYPE_JOIN_ROOM: 'JoinRoom',
}

# Heartbeat keepalive: outbound thread per connection sends Heartbeat,
# waits for the echo, then sleeps. Gem's response is essentially instant
# *when its main loop is reading*, so a missed echo within
# HEARTBEAT_REPLY_TIMEOUT is treated as a dead link.
#
# Heartbeats are gated on `_pending is None` — i.e., we only fire when no
# ReplayReq is in flight. Gem's main loop is single-threaded, and while it
# is doing comms::SendBody for a large ReplayResp it isn't reading the
# socket, so a heartbeat sent during that window can time out spuriously
# and tear down a healthy connection mid-transfer. The active transfer is
# already keeping the link warm; heartbeats are only useful when idle.
HEARTBEAT_BODY_SIZE = 4
HEARTBEAT_INTERVAL = 10        # seconds to sleep between cycles, post-reply
HEARTBEAT_REPLY_TIMEOUT = 5    # seconds to wait for gem to echo before dropping
HEARTBEAT_BUSY_POLL = 1        # while a ReplayReq is in flight, re-check this often

# ErrorType (protocol.hpp:20)
ERR_BAD_REPLAY_CODE = 0          # code invalid / replay does not exist
ERR_REPLAY_DOWNLOAD_FAILURE = 1  # generic: meta weirdness, fetch fail, oversized

# A ReplayResp larger than this almost certainly means the stream desynced;
# treat it as a dead connection rather than trying to read gigabytes.
_MAX_REPLAY_BYTES = 64 * 1024 * 1024


# --- Exceptions ---

class GemError(Exception):
    """Base for all gem-side failures."""


class GemNotConnected(GemError):
    """No console is connected to the gem socket (or it dropped mid-request)."""


class GemTimeout(GemError):
    """The worker accepted the code but produced no response before the deadline."""


class GemReplayError(GemError):
    """The worker explicitly reported a failure. `error_type` is one of the
    ERR_* constants."""

    def __init__(self, error_type):
        self.error_type = error_type
        name = {
            ERR_BAD_REPLAY_CODE: 'BadReplayCode',
            ERR_REPLAY_DOWNLOAD_FAILURE: 'ReplayDownloadFailure',
        }.get(error_type, f'Unknown({error_type})')
        super().__init__(f'gem reported {name}')


def _recv_exact(sock, n):
    """Read exactly n bytes or raise ConnectionError on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError('gem connection closed')
        buf.extend(chunk)
    return bytes(buf)


class GemServer:
    """Owns the listening socket, the (single) console connection, and the
    request/response handshake."""

    def __init__(self, bind, port, log, on_replay_code=None, on_friend_playing=None):
        self._bind = bind
        self._port = port
        self._log = log
        # Called with the replay code (str) whenever the console pushes an
        # UploadReplayNotification. Invoked on a throwaway daemon thread so a
        # slow handler can never stall the socket reader / heartbeats.
        self._on_replay_code = on_replay_code
        # Called with (timestamp:int, nsa_id:int, subtype:int, match_mode:int,
        # sender:str) for every FriendPlayingNotification. Same off-thread
        # semantics as the replay callback.
        self._on_friend_playing = on_friend_playing

        self._conn = None            # active console socket, or None
        self._send_lock = threading.Lock()   # serialize writes to the socket
        self._inflight_lock = threading.Lock()  # at most one ReplayReq in flight

        # Single pending request slot, protected by _state_lock. The reader
        # thread fills `data` or `err` and sets `event`; submit() waits on it.
        self._state_lock = threading.Lock()
        self._pending = None

        # Bumped every time a connection is established *or* lost, so the
        # state machine can tell "the console dropped while I was parked"
        # (a strong game-crash signal) apart from "still the same link".
        self._generation = 0
        self._connected = False

        # Heartbeat ping/pong synchronization. The reader thread sets
        # `_heartbeat_reply` whenever an inbound Heartbeat arrives; the
        # per-connection heartbeat thread waits on it. `_heartbeat_seq`
        # is a wrap-around counter we put in m_Magic so logs can correlate
        # send/receive even though we don't validate the echo value.
        self._heartbeat_reply = threading.Event()
        self._heartbeat_seq = 0

    # ----- lifecycle -----

    def start(self):
        """Spawn the accept loop in a daemon thread."""
        t = threading.Thread(target=self._accept_loop, name='gem-accept', daemon=True)
        t.start()

    def _accept_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self._bind, self._port))
        srv.listen(1)
        self._log.info(f"Gem socket listening on {self._bind}:{self._port}")
        while True:
            try:
                conn, addr = srv.accept()
            except OSError as e:
                self._log.warning(f"Gem accept failed: {e}")
                continue
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._log.info(f"Gem console connected from {addr[0]}:{addr[1]}")
            # A fresh connection supersedes any stale one.
            self._drop_connection('superseded by new console connection')
            with self._state_lock:
                self._conn = conn
                self._connected = True
                self._generation += 1
            threading.Thread(
                target=self._heartbeat_loop, args=(conn,),
                name='gem-heartbeat', daemon=True,
            ).start()
            self._reader_loop(conn)

    def _drop_connection(self, reason):
        """Tear down the current connection and fail any in-flight request."""
        with self._state_lock:
            conn = self._conn
            was_connected = self._connected
            self._conn = None
            self._connected = False
            if was_connected:
                self._generation += 1
            pending = self._pending
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        if pending is not None and not pending['event'].is_set():
            pending['err'] = GemNotConnected(f'gem connection lost: {reason}')
            pending['event'].set()
        if was_connected:
            self._log.warning(f"Gem console disconnected ({reason})")

    # ----- reader -----

    def _reader_loop(self, conn):
        try:
            while True:
                header = _recv_exact(conn, 4)
                ptype = header[0]
                size = int.from_bytes(header[1:4], 'little')

                if ptype == PKT_REPLAY_RESP:
                    if size > _MAX_REPLAY_BYTES:
                        raise ConnectionError(f'absurd ReplayResp size {size}')
                    body = _recv_exact(conn, size)
                    self._deliver(data=body)
                    continue

                if ptype == PKT_ERR:
                    body = _recv_exact(conn, size)
                    error_type = int.from_bytes(body[:4], 'little') if len(body) >= 4 else -1
                    self._deliver(err=GemReplayError(error_type))
                    continue

                # Everything else: consume the body to keep the stream
                # aligned, then handle/ignore it.
                body = _recv_exact(conn, size) if size else b''
                if ptype == PKT_HEARTBEAT:
                    # Echo of a heartbeat we sent — wake the heartbeat thread.
                    # We don't validate body.m_Magic; gem always echoes
                    # (magic+1) and we only care that *something* came back.
                    self._heartbeat_reply.set()
                elif ptype == PKT_UPLOAD_NOTIFICATION:
                    self._handle_upload_notification(body)
                elif ptype == PKT_FRIEND_NOTIFICATION:
                    self._handle_friend_notification(body)
                else:
                    self._log.warning(f"Gem: ignoring unknown packet type {ptype} ({size}B)")
        except (ConnectionError, OSError) as e:
            self._drop_connection(str(e))

    def _handle_upload_notification(self, body):
        """Console pushed an UploadReplayNotification: pull the replay code
        out and forward it to the configured handler (off-thread)."""
        if self._on_replay_code is None:
            return
        if len(body) < UPLOAD_NOTIFICATION_BODY_SIZE:
            self._log.warning(
                f"Gem: upload notification body too short "
                f"({len(body)} < {UPLOAD_NOTIFICATION_BODY_SIZE}); ignoring")
            return
        raw = body[UPLOAD_NOTIFICATION_CODE_OFFSET:
                    UPLOAD_NOTIFICATION_CODE_OFFSET + REPLAY_REQ_BODY_SIZE]
        code = raw.split(b'\x00', 1)[0].decode('ascii', 'ignore').strip().upper()
        if len(code) != REPLAY_CODE_LENGTH:
            self._log.warning(f"Gem: upload notification with bad code {code!r}")
            return
        self._log.info(f"Gem: upload notification for {code}")
        threading.Thread(
            target=self._on_replay_code, args=(code,),
            name='gem-ingest', daemon=True,
        ).start()

    def _handle_friend_notification(self, body):
        """Console pushed a FriendPlayingNotification (a friend started a
        solo / created or joined a room). Decode the body and forward the
        structured event to the configured handler (off-thread)."""
        if self._on_friend_playing is None:
            return
        if len(body) < FRIEND_NOTIFICATION_BODY_SIZE:
            self._log.warning(
                f"Gem: friend notification body too short "
                f"({len(body)} < {FRIEND_NOTIFICATION_BODY_SIZE}); ignoring")
            return
        timestamp = int.from_bytes(
            body[FRIEND_NOTIFICATION_TIMESTAMP_OFFSET:
                 FRIEND_NOTIFICATION_TIMESTAMP_OFFSET + 8], 'little')
        nsa_id = int.from_bytes(
            body[FRIEND_NOTIFICATION_NSA_ID_OFFSET:
                 FRIEND_NOTIFICATION_NSA_ID_OFFSET + 8], 'little')
        subtype = body[FRIEND_NOTIFICATION_SUBTYPE_OFFSET]
        match_mode = int.from_bytes(
            body[FRIEND_NOTIFICATION_MATCH_MODE_OFFSET:
                 FRIEND_NOTIFICATION_MATCH_MODE_OFFSET + 4], 'little')
        sender_raw = body[FRIEND_NOTIFICATION_SENDER_OFFSET:
                          FRIEND_NOTIFICATION_SENDER_OFFSET + NPLN_ID_LENGTH + 1]
        sender = sender_raw.split(b'\x00', 1)[0].decode('ascii', 'ignore')
        subtype_name = FRIEND_SUBTYPE_NAMES.get(subtype, f'Unknown({subtype})')
        self._log.info(
            f"Gem: friend playing {subtype_name} nsa={nsa_id:016x} sender={sender}")
        threading.Thread(
            target=self._on_friend_playing,
            args=(timestamp, nsa_id, subtype, match_mode, sender),
            name='gem-playing', daemon=True,
        ).start()

    def _heartbeat_loop(self, conn):
        """Per-connection ping thread: send Heartbeat → wait for the echo →
        sleep → repeat. A missed echo means the link is dead (gem replies
        in microseconds when it's alive *and* its recv loop is running), so
        we tear the connection down and let the accept loop pick up the
        next one.

        Gated on `_pending is None`: while a ReplayReq is in flight, gem
        may be busy in SendBody for the (potentially large) ReplayResp and
        won't read incoming heartbeats — sending one would false-timeout
        and kill a healthy transfer. The active transfer keeps the socket
        warm, so suppressing heartbeats during it is safe."""
        while True:
            # Bail if a newer connection has superseded ours.
            if self._conn is not conn:
                return
            # If a replay request is in flight, skip this cycle — see the
            # module-level comment on HEARTBEAT_BUSY_POLL for why.
            with self._state_lock:
                busy = self._pending is not None
            if busy:
                time.sleep(HEARTBEAT_BUSY_POLL)
                continue
            self._heartbeat_reply.clear()
            magic = self._heartbeat_seq & 0xFFFFFFFF
            self._heartbeat_seq = (self._heartbeat_seq + 1) & 0xFFFFFFFF
            body = magic.to_bytes(4, 'little')
            header = bytes([PKT_HEARTBEAT]) + \
                HEARTBEAT_BODY_SIZE.to_bytes(3, 'little')
            try:
                with self._send_lock:
                    conn.sendall(header + body)
            except OSError as e:
                self._log.info(
                    f"Gem heartbeat: send failed ({e}); thread exiting")
                return
            if not self._heartbeat_reply.wait(timeout=HEARTBEAT_REPLY_TIMEOUT):
                self._log.warning(
                    f"Gem heartbeat: no echo within {HEARTBEAT_REPLY_TIMEOUT}s; "
                    f"dropping connection")
                self._drop_connection('heartbeat reply timeout')
                return
            time.sleep(HEARTBEAT_INTERVAL)

    def _deliver(self, data=None, err=None):
        """Hand a result to the waiting submit(), if any."""
        with self._state_lock:
            pending = self._pending
        if pending is None:
            self._log.warning("Gem: response with no pending request — dropping")
            return
        if not pending['event'].is_set():
            pending['data'] = data
            pending['err'] = err
            pending['event'].set()

    # ----- public API -----

    def is_connected(self):
        with self._state_lock:
            return self._connected

    def generation(self):
        """Monotonic counter; changes whenever the link comes up or drops."""
        with self._state_lock:
            return self._generation

    def submit(self, code, timeout):
        """Send `code` to the worker and block until it responds.

        Returns the raw replay bytes. Raises GemReplayError / GemNotConnected
        / GemTimeout. Strictly one request in flight at a time.
        """
        code = code.strip().upper()
        if len(code) != REPLAY_CODE_LENGTH:
            raise GemReplayError(ERR_BAD_REPLAY_CODE)

        with self._inflight_lock:
            with self._state_lock:
                conn = self._conn
                if conn is None:
                    raise GemNotConnected('no console connected to gem socket')
                pending = {'event': threading.Event(), 'data': None, 'err': None}
                self._pending = pending

            try:
                body = code.encode('ascii').ljust(REPLAY_REQ_BODY_SIZE, b'\x00')
                header = struct.pack('<B', PKT_REPLAY_REQ) + \
                    REPLAY_REQ_BODY_SIZE.to_bytes(3, 'little')
                with self._send_lock:
                    conn.sendall(header + body)
            except OSError as e:
                with self._state_lock:
                    self._pending = None
                self._drop_connection(f'send failed: {e}')
                raise GemNotConnected(f'failed to send replay request: {e}')

            try:
                if not pending['event'].wait(timeout=timeout):
                    raise GemTimeout(f'no gem response within {timeout}s')
                if pending['err'] is not None:
                    raise pending['err']
                return pending['data']
            finally:
                with self._state_lock:
                    if self._pending is pending:
                        self._pending = None
