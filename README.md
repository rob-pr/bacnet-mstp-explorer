# BACnet MS/TP Explorer

A GUI for exploring a **BACnet MS/TP** (RS-485 serial) network. Pick a serial
(COM) port, a baud rate, and a master MAC address, connect, and:

- watch decoded MS/TP traffic live,
- broadcast **Who-Is** and see devices announce themselves (**I-Am**),
- read a device's **object-list** and browse its objects,
- **read** any property of a selected object, and
- **write** a value to a selected object's property.

The app joins the bus as an active **MS/TP master** — it participates in token
passing so it can transmit confirmed requests (ReadProperty / WriteProperty)
and unconfirmed broadcasts (Who-Is).

## Requirements

- Python 3.11+ (tested on 3.14) with `tkinter` (bundled with the standard
  Windows/macOS installers)
- [`pyserial`](https://pyserial.readthedocs.io/)
- An RS-485 ↔ USB adapter connected to the MS/TP bus

```
pip install -r requirements.txt
```

## Run

```
python explorer.py
```

On this machine, use the launcher for the interpreter that has `pyserial`:

```
py -3.14 explorer.py
```

## Standalone .exe (no Python needed)

A single-file Windows executable can be built with PyInstaller:

```
py -3.14 -m pip install pyserial pyinstaller
build_exe.bat
```

This produces **`dist\BACnet-MSTP-Explorer.exe`** — copy that one file anywhere
and double-click to run; no Python install required on the target machine.
A timestamped log file `capture_<timestamp>.log` (e.g.
`capture_20260715_143022.log`) is written next to the .exe on each launch; the
newest 10 are kept and older ones are deleted automatically. (`build_exe.bat`
just runs `pyinstaller --onefile --windowed --name BACnet-MSTP-Explorer
explorer.py`.)

## Using it

1. **Port** — choose your RS-485 adapter's COM port. Click **↻** to rescan.
2. **Baud** — one of the four standard BACnet MS/TP rates: `9600`, `19200`,
   `38400`, `76800`. Must match the bus (default `38400`).
3. **Master MAC** — this tool's own MS/TP station address (0–127, default
   `127`). It must be **unused** on the bus and **≤ the peer's `Max_Master`**,
   or the peer will never poll us and we never get the token. If nothing
   happens after connecting, lower this (try a value just above the device's
   own MAC — the log shows both).
4. **Connect** — join the bus as a master. **Disconnect** stops.
5. **Who-Is** — broadcast a discovery request; devices reply with I-Am and
   populate the **Devices** table with their **Device #** and vendor.
6. Select a device, click **Read Object List** — its objects appear in the
   **Objects** table (with names, if *names* is ticked).
7. Select an object and use the **Read / Write** panel:
   - pick a **Property** (or type any name / numeric id), optional **array
     index**, then **Read**;
   - to write, enter a **Value**, choose its **Type** (Real / Unsigned /
     Boolean / Enumerated / …), optional **Priority** (1–16 for commandable
     objects), then **Write**.
   - **Write continuously…** opens a small independent window that writes the
     value to that object property on a fixed **interval** (seconds) until you
     press **Stop** or close the window. It shows the destination address, and
     **Value / Interval are editable live** while it runs. Multiple such windows
     can run at once (same or different devices), and the main window stays
     usable for other reads/writes meanwhile.

The **Devices** panel caches each device's object list for the session — click
a device you've already read and its objects reappear instantly.

- *Pause log* freezes the scrolling log while everything else keeps working.
- *Clear* resets the log, tables, and counters.

### What the log shows

Each line is `src -> dest  FrameType  |  decoded APDU`, for example:

```
[14:03:21]   5 -> broadcast  BACnet Data Not Expecting Reply  |  I-Am device,10 (vendor 260)
[14:03:21] 127 ->  5         BACnet Data Expecting Reply      |  readProperty [inv 3] analog-value,1 present-value
```

Frames that fail their header or data CRC are flagged `[CRC ERROR]` in red and
counted in the status bar — useful for spotting wiring / termination problems.

## Files

| File             | Purpose                                                           |
|------------------|-------------------------------------------------------------------|
| `explorer.py`    | tkinter GUI: connection, discovery, object browser, read/write    |
| `mstp_master.py` | MS/TP master state machine (token passing) + transaction API      |
| `mstp.py`        | MS/TP framing: preamble sync, header CRC-8 / data CRC-16, parser   |
| `bacnet.py`      | NPDU / APDU codec (Who-Is, I-Am, ReadProperty, WriteProperty)     |

`mstp.py` and `bacnet.py` each have a `_self_test()` runnable directly:

```
py -3.14 mstp.py
py -3.14 bacnet.py
```

## How the master works

- **Token following** — if the peer is an active master passing the token, the
  tool answers its *Poll For Master* (joining the ring), accepts the token when
  handed to it, sends its queued request, then passes the token back.
- **Sole master** — if the bus is silent (a slave-only peer, or nothing else is
  talking), the tool assumes ownership after `T_no_token` (0.5 s) and sends
  requests directly.

Reads avoid segmentation by fetching the object-list one element at a time
(index 0 = count, then 1…N), so it works even on devices that don't support
segmented responses.

## Scope & limits

- Client services implemented: Who-Is, ReadProperty, WriteProperty. Responses
  decoded: ComplexACK, SimpleACK, Error, Reject, Abort.
- Value types for writing: Real, Unsigned, Signed, Boolean, Enumerated, Null,
  CharacterString.
- **Segmented responses are not reassembled** — flagged rather than parsed. The
  object-list read sidesteps this; a few very large properties may not.
- **Timing** — the MS/TP master is timing-sensitive and USB-serial latency can
  cause missed token/poll deadlines. If transactions time out on real hardware,
  lower your adapter's latency timer (FTDI: Device Manager → COM port →
  Advanced → *Latency Timer* → 1 ms) and watch the log to see whether tokens
  reach this station. The timing constants (`T_no_token`, `T_reply_timeout`,
  `T_usage_timeout`) live at the top of `MSTPMaster.__init__` and can be tuned.
