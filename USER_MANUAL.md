# BACnet MS/TP Explorer — User Manual

A step-by-step guide to using the **BACnet MS/TP Explorer** to discover devices
on an RS-485 (MS/TP) bus, browse their objects, and read or write property
values — including writing a value repeatedly on a timer.

> **In one sentence:** connect your RS-485 adapter, join the bus as a master,
> find your device, list its objects, then read or write any property.

---

## Contents

1. [Before you start](#1-before-you-start)
2. [Starting the program](#2-starting-the-program)
3. [The window at a glance](#3-the-window-at-a-glance)
4. [Step 1 — Connect to the bus](#4-step-1--connect-to-the-bus)
5. [Step 2 — Find your device (Who-Is)](#5-step-2--find-your-device-who-is)
6. [Step 3 — List a device's objects](#6-step-3--list-a-devices-objects)
7. [Step 4 — Read a property](#7-step-4--read-a-property)
8. [Step 5 — Write a value](#8-step-5--write-a-value)
9. [Step 6 — Write continuously (on a timer)](#9-step-6--write-continuously-on-a-timer)
10. [Understanding the target banner (colors)](#10-understanding-the-target-banner-colors)
11. [Reading the traffic log](#11-reading-the-traffic-log)
12. [Tips & good practice](#12-tips--good-practice)
13. [Troubleshooting](#13-troubleshooting)
14. [Quick reference](#14-quick-reference)

---

## 1. Before you start

You need:

- A **USB ↔ RS-485 adapter** plugged into your PC and wired to the MS/TP bus
  (A/B/− + common ground). Make sure the bus is terminated correctly.
- To know your bus's **baud rate** (one of `9600`, `19200`, `38400`, `76800`) —
  it must match, or you'll see nothing.
- A **free MS/TP MAC address** for this tool to use (0–127). It must not clash
  with any other device on the bus.

> ⚠️ **This tool actively transmits.** It joins the bus as a master and can
> **write** values to devices. Double-check the target device before writing —
> the colored banner (see §10) is there to help you avoid writing to the wrong
> one.

---

## 2. Starting the program

**If you have the standalone `.exe`:**
Double-click **`BACnet-MSTP-Explorer.exe`**. No Python needed. Each time you open
the app it writes a new log file named with the open time —
`capture_<timestamp>.log` (e.g. `capture_20260715_143022.log`) — next to the
`.exe`. It's a copy of everything the log shows, handy for sharing when
troubleshooting. The **newest 10** files are kept; older ones are deleted
automatically, so history builds up without filling the folder.

**If you're running from source:**
```
py -3.14 explorer.py
```

The main window opens. Nothing talks to the bus until you press **Connect**.

---

## 3. The window at a glance

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Port: [COM3 - USB Serial ▾] ↻  Baud: [38400 ▾]  Master MAC: [127]         │  ← Toolbar
│ [Connect]   ☐ Pause log   [Clear]                                          │
├───────────────┬───────────────────┬──────────────────────────────────────┤
│ Devices       │ Objects           │ Read / Write                          │
│ MAC Dev# ...  │ Type  Inst  Name  │  ▶ SENDING TO: device … · MAC …       │  ← banner
│  3  369003    │ analog-value 1 …  │  Property: [present-value ▾]          │
│               │ analog-value 2 …  │  Array idx: [   ]                     │
│ [Who-Is]      │ …                 │  [Read]                               │
│ [Read Object  │                   │  ─────────────────                    │
│  List] ☑names │ [green progress]  │  Value: [       ]                     │
│               │                   │  Type: [Real ▾]  Priority: [ ▾]       │
│               │                   │  [Write] [Write continuously…]        │
│               │                   │  Result: …                            │
├───────────────┴───────────────────┴──────────────────────────────────────┤
│ MS/TP traffic (live decoded log)                                          │  ← Log
├────────────────────────────────────────────────────────────────────────── ┤
│ Master on COM3 @ 38400, MAC 127            frames: 1234   errors: 0        │  ← Status bar
└──────────────────────────────────────────────────────────────────────────┘
```

- **Devices** (left) — every MS/TP station seen on the bus.
- **Objects** (middle) — the object list of the device you selected.
- **Read / Write** (right) — the target banner and all read/write controls.
- **MS/TP traffic** (bottom) — a live, color-coded decode of every frame.
- **Status bar** — current connection + running frame / error counts.

---

## 4. Step 1 — Connect to the bus

1. **Port** — pick your RS-485 adapter from the dropdown. Click **↻** to rescan
   if you just plugged it in.
2. **Baud** — choose the rate your bus uses (default `38400`).
3. **Master MAC** — this tool's own station address (default `127`). It must be
   **unused** on the bus and **≤ the peer device's `Max_Master`** setting, or the
   device will never hand this tool the token. If nothing happens after
   connecting, try lowering it to just above your device's own MAC.
4. Click **Connect**.

The button changes to **Disconnect**, the port/baud/MAC controls lock, and the
status bar shows e.g. `Master on COM3 @ 38400, MAC 127`. The traffic log should
start scrolling with token / poll frames.

To stop, click **Disconnect**.

---

## 5. Step 2 — Find your device (Who-Is)

1. Click **Who-Is**. This broadcasts a discovery request.
2. Devices reply with **I-Am** and appear in the **Devices** table, showing:
   - **MAC** — the station address
   - **Device #** — the device instance number (its BACnet identity)
   - **Vendor** — vendor id
   - **Frames** — how many frames we've seen from it

> Devices also appear as soon as they send *any* traffic, but the **Device #**
> and **Vendor** columns only fill in after an I-Am — so if you see `-` there,
> click **Who-Is**.

---

## 6. Step 3 — List a device's objects

1. Click a device row in the **Devices** table to select it.
2. Leave **☑ names** ticked if you want each object's name read too (a little
   slower, but much more readable). Untick it for a faster, numbers-only list.
3. Click **Read Object List**.

A **green progress bar** fills across the bottom of the Objects panel as it
works (`3 / 20`, `4 / 20`, …). When done, the **Objects** table lists every
object with its **Type**, **Instance**, and **Name**.

**Caching:** the object list is remembered for the session. Click away to
another device and back, and its objects reappear **instantly** — no re-read.
The status bar will say `showing N cached objects for MAC …`. To force a fresh
read (e.g. the device changed), just click **Read Object List** again.

> If you get *"Unknown device instance"*, the device hasn't announced itself
> yet — click **Who-Is** first, wait for its **Device #** to appear, then retry.

---

## 7. Step 4 — Read a property

1. Select a **device** (Devices table) **and** an **object** (Objects table).
2. In **Read / Write**, set **Property** — pick from the dropdown (e.g.
   `present-value`, `object-name`, `units`, `description`) or type any property
   name or numeric id.
3. **Array idx** *(optional)* — for array properties, the element to read. Leave
   blank for the whole property.
4. Click **Read**.

The decoded value appears in the **Result** box, and a `✓` line is added to the
log. Errors (e.g. *unknown-property*) are shown in the Result box and flagged in
the log.

---

## 8. Step 5 — Write a value

> First check the **banner** at the top of the Read/Write panel — it names
> exactly which device and object you're about to write to. See §10.

1. Select the **device** and **object**, and set the **Property** you want to
   write (e.g. `present-value`).
2. **Value** — type the value to write (e.g. `21.5`).
3. **Type** — choose how to encode it:
   `Real`, `Unsigned`, `Signed`, `Boolean`, `Enumerated`, `Null`, or
   `CharacterString`. This must match what the property expects (temperatures
   are usually `Real`; on/off is often `Enumerated` or `Boolean`).
4. **Priority** *(optional)* — `1`–`16` for commandable objects (e.g. writing at
   priority 8 for "manual operator"). Leave blank to write with no priority.
   *Tip:* to release a priority you previously set, write **Type = `Null`** at
   the same priority.
5. Click **Write**.

There is **no confirmation popup** — the write is sent immediately. The Result
box shows **"Write acknowledged (OK)"** on success, or the error on failure. A
matching line is written to the log.

---

## 9. Step 6 — Write continuously (on a timer)

Use this to send the **same value repeatedly** at a fixed interval — a
keep-alive, watchdog refresh, or soak test.

1. Set up the target exactly as for a normal write (device, object, property,
   value, type, priority).
2. Click **Write continuously…**. A small independent window opens.

The window shows:

- A **banner** naming the destination — `▶ SENDING TO: device 369003 · MAC 3`
  plus the object and property — so there's no doubt where it's going.
- **Value**, **Type**, **Priority**, and **Interval (s)** fields (interval
  defaults to `3` seconds).
- A **Start / Stop** button and a status line showing the sent count and the
  last result (`sent 12 ✓ (value=21.0, every 3s)`).

3. Set the **Interval** (seconds) and press **Start**. It writes immediately and
   then every interval after that. The banner turns **green** while running.

**While it runs:**

- **Value and Interval are live-editable** — change them and the next cycle uses
  the new figures. (**Type** and **Priority** are locked once started; Stop to
  change them.)
- **The main window stays fully usable.** You can select another object — or
  even another device — and do one-off Reads and Writes without disturbing the
  running loop. The loop keeps targeting whatever it was pointed at when you
  pressed Start.
- Each write still shows up in the main traffic log.

**Stopping:** press **Stop**, or just **close the window**. Closing the main app
stops all continuous writers automatically. If the connection drops, the loop
stops itself and the banner turns red.

**Limits & safeguards:**

- You can have at most **6** continuous-write windows open at once.
- You **cannot** open a second continuous-write window for a property that
  already has one running — the app warns you and brings the existing window to
  the front instead.

---

## 10. Understanding the target banner (colors)

The banner at the top of the **Read / Write** panel always tells you where a
command will go. Its color is a safety signal:

| Color | Meaning |
|-------|---------|
| 🔵 **Blue** | A device is selected, but no object is selected yet (or objects aren't loaded). Reads/writes need an object. |
| 🟢 **Green** | Device **and** object selected, and the listed objects belong to this device. **Safe to go.** |
| 🔴 **Red** | ⚠️ **Mismatch** — the objects currently shown belong to a *different* device than the one selected. **Re-read the object list for the selected device before writing**, or you may hit the wrong object. |
| ⚫ **Grey** | No device selected. |

This is the main defense against writing to the wrong device when you have
several identical units — always confirm the banner is **green** before you
write.

---

## 11. Reading the traffic log

Each line is `time  src -> dest  FrameType  |  decoded APDU`. For example:

```
[14:03:21]   5 -> broadcast  BACnet Data Not Expecting Reply  |  I-Am device,10 (vendor 260)
[14:03:21] 127 ->  5         BACnet Data Expecting Reply      |  readProperty [inv 3] analog-value,1 present-value
```

Colors:

- **grey** — token / poll housekeeping frames
- **blue** — management frames (your reads/writes and their replies)
- **green** — Who-Is / I-Am discovery
- **yellow** — this tool's own action notes (`→ Who-Is`, `✓ write acknowledged`)
- **red** — errors, and any frame that fails its CRC (flagged `[CRC ERROR]`)

Controls:

- **☐ Pause log** — freezes the scrolling text so you can read it; everything
  else keeps working (frames are still counted, devices still update). Note that
  while paused, frames are **not** written to the `capture_<timestamp>.log`
  file either — a paused stretch is a real gap in the saved log, not just hidden
  on screen.
- **Clear** — empties the log **and** the Devices/Objects tables, clears the
  object cache, and resets the counters. Use it for a clean slate.
- **Status bar counts** — `frames:` total frames seen, `errors:` CRC failures. A
  climbing error count points at wiring, termination, or baud-rate problems.

> **Note:** *Clear* only empties the on-screen log — it does **not** clear the
> `capture_<timestamp>.log` file, which keeps recording for the rest of the
> session.

---

## 12. Tips & good practice

- **Always glance at the banner before writing** — green means the object and
  device agree.
- **Let the object list cache work for you** — switching between devices you've
  already read is instant; only re-read when something actually changed.
- **Match the value Type to the property.** A temperature written as `Unsigned`
  won't behave like one written as `Real`.
- **Use Priority deliberately.** Writing at a priority "holds" the value there;
  write `Null` at that same priority to release it.
- **Untick "names"** when reading the object list of a device with many objects
  if you just need the list quickly.
- **`capture_<timestamp>.log`** (next to the app) is a full transcript of each
  session — a new file is created every launch and the newest 10 are kept, so
  you can go back to a recent run. Attach the relevant one when asking for help
  diagnosing bus issues.

---

## 13. Troubleshooting

**Nothing appears in the log after Connect.**
- Wrong **baud** — it must match the bus exactly. Try the others.
- Wrong **port** — pick the correct COM port; click **↻** to rescan.
- **Master MAC too high** — the device only polls up to its `Max_Master`. Lower
  the MAC (try just above the device's own MAC).
- Check RS-485 wiring/polarity and bus termination.

**"Connection failed / closed — see log; pick a port."**
- The port couldn't be opened (wrong port, already in use by another program, or
  the adapter was unplugged). The controls unlock automatically — just pick the
  right port and Connect again. *(You are no longer locked out after picking a
  bad port — that earlier issue is fixed.)*

**Device shows up but Device # / Vendor are `-`.**
- Click **Who-Is** and wait a moment for its I-Am.

**"Unknown device instance" when reading the object list.**
- Same fix: **Who-Is** first, then read once the Device # is filled in.

**Reads/writes time out.**
- USB-serial latency can miss MS/TP deadlines. For FTDI adapters: Device Manager
  → your COM port → **Port Settings → Advanced → Latency Timer → 1 ms**.
- Confirm from the log that tokens actually reach this station.

**Lots of `[CRC ERROR]` lines / rising error count.**
- Wiring, termination, ground, or a baud mismatch. Fix the physical bus.

**A write is rejected with an error.**
- The property may be read-only, need a **Priority**, or expect a different
  **Type**. Check the error text in the Result box and adjust.

**"Limit reached" / "Already running" when opening a continuous-write window.**
- You already have 6 open (close one), or one already targets that exact
  property (the existing window is brought to the front).

---

## 14. Quick reference

| I want to… | Do this |
|------------|---------|
| Connect | Pick Port + Baud + Master MAC → **Connect** |
| Find devices | **Who-Is** |
| List objects | Select a device → **Read Object List** |
| See objects again | Just click the device (cached, instant) |
| Read a value | Select device + object → set **Property** → **Read** |
| Write a value | Select device + object → **Property**, **Value**, **Type**, (Priority) → **Write** |
| Write on a timer | …then **Write continuously…** → set **Interval** → **Start** |
| Release a priority | Write **Type = Null** at that priority |
| Pause the scrolling | **☐ Pause log** |
| Start fresh | **Clear** |
| Confirm the target | Check the **banner** is 🟢 green |

---

*BACnet MS/TP Explorer — active MS/TP master. Client services: Who-Is,
ReadProperty, WriteProperty. See `README.md` for the technical overview and
build instructions.*
