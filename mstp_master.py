"""A pragmatic BACnet MS/TP master node.

Implements enough of the MS/TP master state machine (ASHRAE 135 clause 9.5.6)
to become a participating node on the bus and run confirmed request/response
transactions (ReadProperty / WriteProperty) plus fire-and-forget broadcasts
(Who-Is). It runs its own thread that owns the serial port.

Two operating modes are handled automatically:

* **Token following** — if another master is actively passing the token, we
  answer its Poll For Master (joining the ring), accept the token when it is
  handed to us, transmit our queued frame(s), then pass the token back.

* **Sole master** — if the bus is silent (the peer is a slave that only
  replies, or we are alone), we assume ownership after ``T_no_token`` and send
  our requests directly.

The state machine is timing-sensitive; USB-serial latency can cause missed
token/poll deadlines. ``log_callback`` narrates transitions so behaviour can be
observed and the timing constants tuned against real hardware.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import serial

import mstp

# Frame types we send / recognise.
FT_TOKEN = 0x00
FT_POLL_FOR_MASTER = 0x01
FT_REPLY_TO_POLL = 0x02
FT_TEST_REQUEST = 0x03
FT_TEST_RESPONSE = 0x04
FT_DATA_EXPECTING_REPLY = 0x05
FT_DATA_NOT_EXPECTING_REPLY = 0x06
FT_REPLY_POSTPONED = 0x07

# States.
S_IDLE = "IDLE"
S_ACTIVE = "USE_TOKEN"
S_WAIT_REPLY = "WAIT_FOR_REPLY"

BROADCAST = 0xFF
N_MAX_MASTER = 127


@dataclass
class Transaction:
    dest: int
    npdu: bytes
    expect_reply: bool = True
    _event: threading.Event = field(default_factory=threading.Event)
    response: bytes | None = None
    error: str | None = None

    def complete(self, response: bytes) -> None:
        self.response = response
        self._event.set()

    def fail(self, error: str) -> None:
        self.error = error
        self._event.set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


class MSTPMaster(threading.Thread):
    def __init__(self, port: str, baud: int, this_station: int,
                 frame_callback=None, log_callback=None):
        super().__init__(daemon=True)
        self.port_name = port
        self.baud = baud
        self.ts = this_station & 0x7F
        self.frame_callback = frame_callback or (lambda f: None)
        self.log = log_callback or (lambda m: None)

        # Tunable timing (seconds). Defaults sit at the tolerant end of the spec.
        self.T_no_token = 0.5
        self.T_reply_timeout = 0.30
        self.T_usage_timeout = 0.10
        self.read_granularity = 0.005
        self.transact_timeout = 5.0  # overall wait incl. time to obtain token

        self._stop = threading.Event()
        self._serial: serial.Serial | None = None
        self._tx_lock = threading.Lock()
        self._tx_queue: list[Transaction] = []

        # FSM state.
        self.state = S_IDLE
        self.sole_master = False
        self.known_masters: set[int] = set()
        self._current: Transaction | None = None
        self._reply_deadline = 0.0
        self._last_activity = 0.0
        self._token_source = self.ts
        self._poll_seen = False
        self._poll_to_us_seen = False
        self._warned_max_master = False
        self._max_poll_target = -1

    # ------------------------------------------------------- public API ---
    def stop(self) -> None:
        self._stop.set()

    def transact(self, dest: int, npdu: bytes, expect_reply: bool = True,
                 timeout: float | None = None) -> Transaction:
        """Queue a frame and block until reply / timeout. Returns the
        Transaction (inspect ``.response`` / ``.error``)."""
        txn = Transaction(dest, npdu, expect_reply)
        with self._tx_lock:
            self._tx_queue.append(txn)
        if not expect_reply:
            # Give the FSM a chance to transmit, but don't block the caller long.
            txn.wait(1.0)
            return txn
        if not txn.wait(timeout or self.transact_timeout):
            txn.error = txn.error or "timeout: no token / no response"
        return txn

    def send_unconfirmed(self, dest: int, npdu: bytes) -> None:
        """Fire-and-forget (e.g. Who-Is broadcast)."""
        self.transact(dest, npdu, expect_reply=False)

    # ------------------------------------------------------------- loop ---
    def run(self) -> None:
        try:
            self._serial = serial.Serial(self.port_name, self.baud,
                                         timeout=self.read_granularity)
        except serial.SerialException as exc:
            self.log(f"! serial error: {exc}")
            self._fail_all(str(exc))
            return
        self.log(f"MS/TP master on {self.port_name} @ {self.baud} baud, "
                 f"station {self.ts}")
        parser = mstp.FrameParser()
        self._last_activity = time.perf_counter()
        try:
            while not self._stop.is_set():
                try:
                    data = self._serial.read(64)
                except serial.SerialException as exc:
                    self.log(f"! serial error: {exc}")
                    self._fail_all(str(exc))
                    break
                now = time.perf_counter()
                if data:
                    self._last_activity = now
                    for frame in parser.feed(data):
                        self._handle_rx(frame, now)
                self._tick(now)
        finally:
            if self._serial and self._serial.is_open:
                self._serial.close()
            self._fail_all("disconnected")
            self.log("master stopped")

    # ---------------------------------------------------------- helpers ---
    def _send_frame(self, frame_type: int, dest: int, data: bytes = b"") -> None:
        if not (self._serial and self._serial.is_open):
            return
        self._serial.write(mstp.build_frame(frame_type, dest, self.ts, data))

    def _pending(self) -> Transaction | None:
        with self._tx_lock:
            return self._tx_queue[0] if self._tx_queue else None

    def _pop_pending(self) -> Transaction | None:
        with self._tx_lock:
            return self._tx_queue.pop(0) if self._tx_queue else None

    def _fail_all(self, reason: str) -> None:
        with self._tx_lock:
            pending = list(self._tx_queue)
            self._tx_queue.clear()
        for txn in pending:
            txn.fail(reason)
        if self._current:
            self._current.fail(reason)
            self._current = None

    # --------------------------------------------------------- RX events --
    def _handle_rx(self, frame: "mstp.Frame", now: float) -> None:
        self.frame_callback(frame)
        if not frame.crc_ok:
            return

        src, dest, ftype = frame.source, frame.destination, frame.frame_type
        if src != self.ts and ftype in (FT_TOKEN, FT_POLL_FOR_MASTER, FT_REPLY_TO_POLL):
            self.known_masters.add(src)

        # A reply we were waiting for.
        if (self.state == S_WAIT_REPLY and self._current is not None
                and dest == self.ts and ftype in
                (FT_DATA_NOT_EXPECTING_REPLY, FT_DATA_EXPECTING_REPLY,
                 FT_TEST_RESPONSE, FT_REPLY_POSTPONED)):
            if ftype == FT_REPLY_POSTPONED:
                self._current.fail("reply postponed by device")
            else:
                self._current.complete(frame.data)
            self._current = None
            self._after_transaction(now)
            return

        if ftype == FT_POLL_FOR_MASTER:
            self._poll_seen = True
            if dest > self._max_poll_target:
                self._max_poll_target = dest
                self.log(f"peer {src} polling up to station {dest}")
            elif (dest < self._max_poll_target and not self._poll_to_us_seen
                    and not self._warned_max_master):
                # The poll sweep wrapped back down without ever reaching us:
                # our address is above the peer's Max_Master.
                self._warned_max_master = True
                self.log(f"! peer's poll sweep reached station {self._max_poll_target} "
                         f"then wrapped without polling station {self.ts}; "
                         f"its Max_Master is below {self.ts} — set a lower Master MAC "
                         f"(try just above {src}).")
            if dest == self.ts:
                if not self._poll_to_us_seen:
                    self.log(f"polled by {src} at station {self.ts} — replying")
                self._poll_to_us_seen = True
                self._send_frame(FT_REPLY_TO_POLL, src)  # "I exist, include me"
            return

        if ftype == FT_TOKEN and dest == self.ts:
            self.sole_master = False
            self._token_source = src
            self.log(f"received token from {src}")
            self._use_token(now)
            return

    # --------------------------------------------------------- timeouts --
    def _tick(self, now: float) -> None:
        if self.state == S_WAIT_REPLY and now >= self._reply_deadline:
            if self._current:
                self._current.fail("timeout: no response from device")
                self._current = None
            self._after_transaction(now)
            return

        if self.state == S_IDLE:
            if self.sole_master and self._pending() is not None:
                self._use_token(now)  # keep the floor; no need to re-establish
                return
            silence = now - self._last_activity
            if silence >= self.T_no_token:
                if not self.sole_master:
                    self.sole_master = True
                    self.log("bus silent — assuming sole master")
                if self._pending() is not None:
                    self._use_token(now)

    # ----------------------------------------------------- token usage ---
    def _use_token(self, now: float) -> None:
        self.state = S_ACTIVE
        txn = self._pop_pending()
        if txn is None:
            self._done_with_token(now)
            return
        ftype = FT_DATA_EXPECTING_REPLY if txn.expect_reply else FT_DATA_NOT_EXPECTING_REPLY
        self._send_frame(ftype, txn.dest, txn.npdu)
        if txn.expect_reply:
            self._current = txn
            self.state = S_WAIT_REPLY
            self._reply_deadline = now + self.T_reply_timeout
        else:
            txn.complete(b"")
            # More to send this turn?
            if self._pending() is not None:
                self._use_token(time.perf_counter())
            else:
                self._done_with_token(now)

    def _after_transaction(self, now: float) -> None:
        # Just finished a reply/timeout; send the next queued frame or release.
        if self._pending() is not None:
            self._use_token(now)
        else:
            self._done_with_token(now)

    def _done_with_token(self, now: float) -> None:
        self.state = S_IDLE
        if self.sole_master:
            return  # keep the floor; we'll send again as work arrives
        ns = self._next_station()
        self._send_frame(FT_TOKEN, ns)

    def _next_station(self) -> int:
        masters = sorted(m for m in self.known_masters
                         if 0 <= m <= N_MAX_MASTER and m != self.ts)
        if not masters:
            return self._token_source
        above = [m for m in masters if m > self.ts]
        return above[0] if above else masters[0]
