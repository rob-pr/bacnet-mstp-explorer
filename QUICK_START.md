# BACnet MS/TP Explorer — Quick Start (for the Tester)

A short, do-this-then-that guide. For the full details see **`USER_MANUAL.md`**.

> ⚠️ This tool **transmits** on the bus and can **write** to devices. Before any
> write, check the banner on the right is **🟢 green** — that means the device and
> object agree.

---

## 1. Open the app

Double-click **`BACnet-MSTP-Explorer.exe`**. The window opens with four areas:

```
┌────────────── Toolbar: Port · Baud · Master MAC · Connect ──────────────┐
├─────────────┬──────────────┬────────────────────────────────────────────┤
│  Devices    │   Objects    │  Read / Write  (banner + controls)          │
├─────────────┴──────────────┴────────────────────────────────────────────┤
│  MS/TP traffic (live log)                                                 │
└───────────────────────────────────────────────────────────────────────────┘
```

- **Devices** (left) — devices found on the bus
- **Objects** (middle) — objects in the selected device
- **Read / Write** (right) — read/write a property
- **Traffic log** (bottom) — live decoded bus traffic

---

## 2. Select COM port and baud rate → Connect

1. **Port** — pick your USB↔RS-485 adapter (click **↻** to rescan).
2. **Baud** — match your bus (`9600`, `19200`, `38400`, `76800`). Default `38400`.
3. **Master MAC** — leave `127` unless it clashes with a device; if nothing shows
   up, lower it to just above your device's MAC.
4. Click **Connect**.

The log should start scrolling and the status bar shows `Master on COM… @ …, MAC …`.

---

## 3. Send Who-Is

Click **Who-Is**. Devices reply and appear in the **Devices** table with their
**MAC** and **Device #**.

> If **Device #** shows `-`, click **Who-Is** again and wait a moment.

---

## 4. Select the device

Click the device's row in the **Devices** table.

---

## 5. List the objects

Click **Read Object List**. A green progress bar fills; when done, the
**Objects** table lists every object (Type, Instance, Name).

> Leave **☑ names** ticked for readable names, or untick it for a faster list.
> Already-read devices reappear instantly (cached) when you click them again.

---

## 6. Select one object

Click an object's row in the **Objects** table. The right-side banner turns
**🟢 green** — you're ready.

---

## 7. Read it

1. Set **Property** (e.g. `present-value`) — pick from the list or type one.
2. Click **Read**. The decoded value shows in the **Result** box.

---

## 8. Write to the object property

1. Confirm the banner is **🟢 green**.
2. **Value** — type the value (e.g. `21.5`).
3. **Type** — how to encode it: `Real` (temperatures), `Boolean`/`Enumerated`
   (on/off), `Unsigned`, `Signed`, `Null`, `CharacterString`.
4. **Priority** *(optional)* — `1`–`16` for commandable objects.
   *To release a priority: write **Type = Null** at that same priority.*
5. Click **Write**. No popup — it sends immediately. Result box shows
   **"Write acknowledged (OK)"** or the error.

---

## 9. (Optional) Write periodically (on a timer)

To send the **same value repeatedly** (keep-alive / soak test):

1. Set up the write as above (device, object, property, value, type, priority).
2. Click **Write continuously…** — a small window opens.
3. Set **Interval (s)** (default `3`) and press **Start**. It writes immediately,
   then every interval. Banner turns green while running.

**While running:**
- **Value** and **Interval** are live-editable; **Type** and **Priority** are locked
  (press Stop to change them).
- The main window stays usable — you can read/write other objects meanwhile.
- **Stop** or close the window to end it.

**Limits:**
- Max **6** continuous-write windows open at once.
- You **can't** open a second one for a property that already has a running writer —
  the app just brings the existing window to the front.

---

## Quick reference

| I want to… | Do this |
|------------|---------|
| Connect | Port + Baud + Master MAC → **Connect** |
| Find devices | **Who-Is** |
| List objects | Select device → **Read Object List** |
| Read a value | Select object → set **Property** → **Read** |
| Write a value | **Value** + **Type** (+ Priority) → **Write** (banner 🟢) |
| Write on a timer | **Write continuously…** → set **Interval** → **Start** |
| Release a priority | Write **Type = Null** at that priority |

*Trouble? Wrong baud/port or a too-high Master MAC is the usual cause of an empty
log. See `USER_MANUAL.md` §13 for full troubleshooting.*
