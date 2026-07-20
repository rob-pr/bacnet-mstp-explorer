"""BACnet MS/TP Explorer — active master GUI.

Select a serial (COM) port, a standard MS/TP baud rate, and this tool's master
MAC address, then connect. The app joins the bus as a master and lets you:

  * broadcast Who-Is and watch devices announce themselves (I-Am),
  * read a device's object-list and browse its objects,
  * read any property of a selected object, and
  * write a value to a selected object's property.

All bus traffic is decoded live in the log at the bottom. Serial I/O and every
transaction run off the GUI thread; results are marshalled back through a
queue so the interface stays responsive.

Run:  py -3.14 explorer.py
"""

from __future__ import annotations

import glob
import itertools
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import serial
import serial.tools.list_ports

import bacnet
import mstp
from mstp_master import BROADCAST, MSTPMaster

APP_VERSION = "1.00.02"

BAUD_RATES = [9600, 19200, 38400, 76800]
DEFAULT_BAUD = 38400
DEFAULT_MASTER_MAC = 127

# Properties offered in the read/write dropdown (editable — any name/number ok).
COMMON_PROPERTIES = [
    "present-value", "object-name", "description", "object-identifier",
    "object-type", "status-flags", "event-state", "out-of-service", "units",
    "reliability", "priority-array", "relinquish-default", "min-pres-value",
    "max-pres-value", "number-of-states", "state-text", "active-text",
    "inactive-text", "polarity", "cov-increment", "vendor-name", "model-name",
    "firmware-revision", "object-list",
]


def resolve_property(text: str) -> int:
    text = text.strip()
    if text in bacnet.PROPERTY_IDS_BY_NAME:
        return bacnet.PROPERTY_IDS_BY_NAME[text]
    return int(text, 0)  # accept a raw numeric id


class DeviceTable:
    """What we know about each MS/TP MAC seen on the bus."""

    def __init__(self):
        self.rows: dict[int, dict] = {}

    def observe(self, frame: "mstp.Frame", decoded) -> None:
        row = self.rows.setdefault(
            frame.source,
            {"frames": 0, "device_instance": None, "vendor_id": None})
        row["frames"] += 1
        if decoded is not None:
            for detail in decoded.details:
                if detail.startswith("__device_instance="):
                    row["device_instance"] = int(detail.split("=", 1)[1])
                elif detail.startswith("__vendor_id="):
                    row["vendor_id"] = int(detail.split("=", 1)[1])


class ExplorerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"BACnet MS/TP Explorer  v{APP_VERSION}")
        root.geometry("1180x760")
        root.minsize(960, 600)

        self.queue: "queue.Queue" = queue.Queue()
        self.master: MSTPMaster | None = None
        self.devices = DeviceTable()
        self.objects_by_iid: dict[str, tuple[int, int, str]] = {}
        self._objects_device: tuple[int, int] | None = None  # (mac, instance)
        # mac -> (instance, [ (otype, inst, name), ... ] ) read this session.
        self.object_cache: dict[int, tuple] = {}
        self.frame_count = 0
        self.error_count = 0
        self.paused = tk.BooleanVar(value=False)
        self.read_names = tk.BooleanVar(value=True)
        self._invoke = itertools.count(1)
        self._busy = False
        self._connected = False
        self.cont_windows: list = []  # open ContinuousWriteWindow instances
        self._progress_frac = 0.0
        self._progress_text = ""

        # Tee all log output to a file so a capture can be shared for diagnosis.
        # When frozen (PyInstaller .exe) write next to the executable, not the
        # temporary extraction dir that __file__ points at.
        if getattr(sys, "frozen", False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        # One log file per launch, named with the open time, keeping only the
        # newest few so the folder doesn't grow without bound.
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.capture_path = os.path.join(base_dir, f"capture_{stamp}.log")
        try:
            self._capture = open(self.capture_path, "w", encoding="utf-8")
        except OSError:
            self._capture = None
        self._prune_captures(base_dir, keep=10)

        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        self.refresh_ports()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(60, self._poll_queue)

    def _next_invoke(self) -> int:
        return next(self._invoke) & 0xFF

    @staticmethod
    def _prune_captures(base_dir: str, keep: int = 10) -> None:
        """Keep only the newest `keep` capture_*.log files in base_dir."""
        files = glob.glob(os.path.join(base_dir, "capture_*.log"))
        files.sort(key=os.path.getmtime)  # oldest first
        for old in files[:-keep]:         # delete all but the newest `keep`
            try:
                os.remove(old)
            except OSError:
                pass                      # locked / already gone — skip quietly

    # ------------------------------------------------------------- build ---
    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(bar, text="Port:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(bar, textvariable=self.port_var, width=26,
                                        state="readonly")
        self.port_combo.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Button(bar, text="↻", width=3, command=self.refresh_ports).pack(
            side=tk.LEFT, padx=(0, 12))

        ttk.Label(bar, text="Baud:").pack(side=tk.LEFT)
        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUD))
        self.baud_combo = ttk.Combobox(bar, textvariable=self.baud_var, width=8,
                                       state="readonly",
                                       values=[str(b) for b in BAUD_RATES])
        self.baud_combo.pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(bar, text="Master MAC:").pack(side=tk.LEFT)
        self.mac_var = tk.StringVar(value=str(DEFAULT_MASTER_MAC))
        self.mac_spin = ttk.Spinbox(bar, from_=0, to=127, width=5,
                                    textvariable=self.mac_var)
        self.mac_spin.pack(side=tk.LEFT, padx=(4, 12))

        self.connect_btn = ttk.Button(bar, text="Connect", command=self.toggle_connect)
        self.connect_btn.pack(side=tk.LEFT)

        ttk.Checkbutton(bar, text="Pause log", variable=self.paused).pack(
            side=tk.LEFT, padx=(12, 0))
        ttk.Button(bar, text="Clear", command=self.clear).pack(side=tk.LEFT, padx=(6, 0))

    def _build_body(self) -> None:
        outer = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        outer.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        top = ttk.PanedWindow(outer, orient=tk.HORIZONTAL)
        outer.add(top, weight=3)

        self._build_devices(top)
        self._build_objects(top)
        self._build_rw(top)
        self._build_log(outer)

    def _build_devices(self, parent) -> None:
        frame = ttk.Labelframe(parent, text="Devices", padding=4)
        cols = ("mac", "device", "vendor", "frames")
        self.dev_tree = ttk.Treeview(frame, columns=cols, show="headings", height=8)
        for col, text, width in (("mac", "MAC", 50), ("device", "Device #", 80),
                                  ("vendor", "Vendor", 60), ("frames", "Frames", 60)):
            self.dev_tree.heading(col, text=text)
            self.dev_tree.column(col, width=width, anchor=tk.CENTER)
        self.dev_tree.pack(fill=tk.BOTH, expand=True)
        self.dev_tree.bind("<<TreeviewSelect>>", self._on_device_select)
        btns = ttk.Frame(frame)
        btns.pack(fill=tk.X, pady=(4, 0))
        self.whois_btn = ttk.Button(btns, text="Who-Is", command=self.do_who_is,
                                    state=tk.DISABLED)
        self.whois_btn.pack(side=tk.LEFT)
        self.objlist_btn = ttk.Button(btns, text="Read Object List",
                                      command=self.do_read_object_list, state=tk.DISABLED)
        self.objlist_btn.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Checkbutton(btns, text="names", variable=self.read_names).pack(side=tk.LEFT)
        parent.add(frame, weight=1)

    def _build_objects(self, parent) -> None:
        frame = ttk.Labelframe(parent, text="Objects", padding=4)
        self.objects_frame = frame
        cols = ("type", "instance", "name")
        self.obj_tree = ttk.Treeview(frame, columns=cols, show="headings", height=8)
        for col, text, width in (("type", "Type", 130), ("instance", "Inst", 50),
                                  ("name", "Name", 130)):
            self.obj_tree.heading(col, text=text)
            self.obj_tree.column(col, width=width,
                                 anchor=tk.W if col != "instance" else tk.CENTER)
        yscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.obj_tree.yview)
        self.obj_tree.configure(yscrollcommand=yscroll.set)
        # Green fill progress bar (canvas-drawn so the colour is guaranteed
        # regardless of the active ttk theme). Fills as objects are read.
        self.progress_canvas = tk.Canvas(frame, height=16, highlightthickness=0,
                                         bg="#2e3440")
        self.progress_canvas.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
        self.progress_canvas.bind("<Configure>", lambda _e: self._progress_draw())
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.obj_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.obj_tree.bind("<<TreeviewSelect>>", self._on_object_select)
        parent.add(frame, weight=1)

    def _build_rw(self, parent) -> None:
        frame = ttk.Labelframe(parent, text="Read / Write", padding=6)

        # Loud, color-coded banner naming the device a command will go to.
        # Green = device matches the listed objects; red = mismatch (danger).
        self.target_lbl = tk.Label(frame, text="▶  No device selected",
                                   font=("", 10, "bold"), justify=tk.LEFT,
                                   anchor=tk.W, bg="#4c566a", fg="#eceff4",
                                   relief=tk.RIDGE, bd=2, padx=8, pady=6)
        self.target_lbl.grid(row=0, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))

        ttk.Label(frame, text="Property:").grid(row=1, column=0, sticky=tk.W)
        self.prop_var = tk.StringVar(value="present-value")
        self.prop_combo = ttk.Combobox(frame, textvariable=self.prop_var,
                                       values=COMMON_PROPERTIES, width=22)
        self.prop_combo.grid(row=1, column=1, sticky=tk.W, pady=2)

        ttk.Label(frame, text="Array idx:").grid(row=2, column=0, sticky=tk.W)
        self.index_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.index_var, width=8).grid(
            row=2, column=1, sticky=tk.W, pady=2)
        ttk.Label(frame, text="(optional)").grid(row=2, column=2, sticky=tk.W)

        self.read_btn = ttk.Button(frame, text="Read", command=self.do_read,
                                   state=tk.DISABLED)
        self.read_btn.grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(4, 8))

        ttk.Separator(frame, orient=tk.HORIZONTAL).grid(
            row=4, column=0, columnspan=3, sticky=tk.EW, pady=4)

        ttk.Label(frame, text="Value:").grid(row=5, column=0, sticky=tk.W)
        self.value_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.value_var, width=22).grid(
            row=5, column=1, sticky=tk.W, pady=2)

        ttk.Label(frame, text="Type:").grid(row=6, column=0, sticky=tk.W)
        self.vtype_var = tk.StringVar(value="Real")
        ttk.Combobox(frame, textvariable=self.vtype_var, values=bacnet.WRITE_TYPES,
                     width=16, state="readonly").grid(row=6, column=1, sticky=tk.W, pady=2)

        ttk.Label(frame, text="Priority:").grid(row=7, column=0, sticky=tk.W)
        self.prio_var = tk.StringVar(value="")
        ttk.Combobox(frame, textvariable=self.prio_var,
                     values=[""] + [str(i) for i in range(1, 17)],
                     width=6, state="readonly").grid(row=7, column=1, sticky=tk.W, pady=2)

        wbtns = ttk.Frame(frame)
        wbtns.grid(row=8, column=0, columnspan=3, sticky=tk.W, pady=(4, 8))
        self.write_btn = ttk.Button(wbtns, text="Write", command=self.do_write,
                                    state=tk.DISABLED)
        self.write_btn.pack(side=tk.LEFT)
        self.contwrite_btn = ttk.Button(wbtns, text="Write continuously…",
                                        command=self.do_write_continuous,
                                        state=tk.DISABLED)
        self.contwrite_btn.pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(frame, text="Result:").grid(row=9, column=0, sticky=tk.NW)
        self.result = tk.Text(frame, height=5, width=32, wrap=tk.WORD,
                              state=tk.DISABLED, font=("Consolas", 9))
        self.result.grid(row=9, column=1, columnspan=2, sticky=tk.NSEW, pady=2)
        frame.rowconfigure(9, weight=1)
        frame.columnconfigure(2, weight=1)
        parent.add(frame, weight=1)

    def _build_log(self, parent) -> None:
        frame = ttk.Labelframe(parent, text="MS/TP traffic", padding=4)
        self.log = tk.Text(frame, wrap=tk.NONE, height=10, font=("Consolas", 9),
                           background="#101418", foreground="#d8dee9")
        yscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.log.yview)
        self.log.configure(yscrollcommand=yscroll.set)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for tag, color in (("token", "#616e88"), ("data", "#a3be8c"),
                           ("mgmt", "#88c0d0"), ("error", "#bf616a"),
                           ("info", "#ebcb8b")):
            self.log.tag_configure(tag, foreground=color)
        self.log.configure(state=tk.DISABLED)
        parent.add(frame, weight=2)

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 4))
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(bar, textvariable=self.status_var).pack(side=tk.LEFT)
        self.counts_var = tk.StringVar(value="frames: 0   errors: 0")
        ttk.Label(bar, textvariable=self.counts_var).pack(side=tk.RIGHT)

    # ------------------------------------------------------- connection ---
    def refresh_ports(self) -> None:
        ports = serial.tools.list_ports.comports()
        self._port_devices = [p.device for p in ports]
        self.port_combo["values"] = [f"{p.device} - {p.description}" for p in ports]
        if self._port_devices and not self.port_var.get():
            self.port_combo.current(0)

    def _selected_port(self) -> str | None:
        idx = self.port_combo.current()
        return self._port_devices[idx] if 0 <= idx < len(self._port_devices) else None

    def toggle_connect(self) -> None:
        if self.master and self.master.is_alive():
            self.disconnect()
        else:
            self.connect()

    def connect(self) -> None:
        port = self._selected_port()
        if not port:
            messagebox.showwarning("No port", "Select a serial port first.")
            return
        try:
            baud = int(self.baud_var.get())
            mac = int(self.mac_var.get())
        except ValueError:
            messagebox.showwarning("Bad settings", "Check baud and MAC values.")
            return
        self.master = MSTPMaster(
            port, baud, mac,
            frame_callback=lambda f: self.queue.put(("frame", f)),
            log_callback=lambda m: self.queue.put(("log", m)))
        self.master.start()
        self._connected = True
        self.connect_btn.config(text="Disconnect")
        self._set_conn_controls(False)
        self._enable_ops(True)
        self.status_var.set(f"Master on {port} @ {baud}, MAC {mac}")

    def disconnect(self) -> None:
        self._connected = False
        if self.master:
            self.master.stop()
            self.master = None
        self._reset_conn_ui()
        self.status_var.set("Disconnected")

    def _reset_conn_ui(self) -> None:
        self.connect_btn.config(text="Connect")
        self._set_conn_controls(True)
        self._enable_ops(False)

    def _check_connection_alive(self) -> None:
        """If the master thread died on its own (e.g. the port failed to open
        or was pulled out), return the UI to a selectable state so another
        port can be chosen."""
        if self._connected and self.master is not None and not self.master.is_alive():
            self._connected = False
            self.master = None
            self._reset_conn_ui()
            self.status_var.set("Connection failed / closed — see log; pick a port")

    def _set_conn_controls(self, enabled: bool) -> None:
        state = "readonly" if enabled else tk.DISABLED
        self.port_combo.config(state=state)
        self.baud_combo.config(state=state)
        self.mac_spin.config(state=tk.NORMAL if enabled else tk.DISABLED)

    def _enable_ops(self, enabled: bool) -> None:
        s = tk.NORMAL if enabled else tk.DISABLED
        for w in (self.whois_btn, self.objlist_btn, self.read_btn, self.write_btn,
                  self.contwrite_btn):
            w.config(state=s)

    def clear(self) -> None:
        self.log.config(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.config(state=tk.DISABLED)
        for tree in (self.dev_tree, self.obj_tree):
            tree.delete(*tree.get_children())
        self.devices = DeviceTable()
        self.object_cache.clear()
        self._clear_objects_view()
        self._update_target_banner()
        self.frame_count = self.error_count = 0
        self._update_counts()

    # ----------------------------------------------------- async runner ---
    def _run_async(self, work, on_done) -> None:
        if self._busy:
            self._log("busy — wait for the current operation to finish\n", "info")
            return
        self._busy = True

        def runner():
            try:
                result, err = work(), None
            except Exception as exc:  # surfaced to the user, not swallowed
                result, err = None, exc
            self.queue.put(("call", lambda: self._finish_async(on_done, result, err)))

        threading.Thread(target=runner, daemon=True).start()

    def _finish_async(self, on_done, result, err) -> None:
        self._busy = False
        on_done(result, err)

    # ------------------------------------------------- transaction utils --
    def _transact_read(self, mac, otype, instance, prop_id, array_index=None):
        invoke = self._next_invoke()
        req = bacnet.build_read_property(invoke, otype, instance, prop_id, array_index)
        txn = self.master.transact(mac, req)
        if txn.error:
            raise RuntimeError(txn.error)
        res = bacnet.parse_response(txn.response, want_invoke=invoke)
        if not res.ok:
            raise RuntimeError(res.error)
        return res

    def _transact_write(self, mac, otype, instance, prop_id, value_bytes,
                        array_index=None, priority=None):
        invoke = self._next_invoke()
        req = bacnet.build_write_property(invoke, otype, instance, prop_id,
                                          value_bytes, array_index, priority)
        txn = self.master.transact(mac, req)
        if txn.error:
            raise RuntimeError(txn.error)
        res = bacnet.parse_response(txn.response, want_invoke=invoke)
        if not res.ok:
            raise RuntimeError(res.error)
        return res

    # --------------------------------------------------------- selection --
    def _selected_device(self):
        sel = self.dev_tree.selection()
        if not sel:
            return None
        mac = int(sel[0][3:])  # iid = "macN"
        return mac, self.devices.rows.get(mac, {})

    def _on_object_select(self, _event=None) -> None:
        self._update_target_banner()

    def _on_device_select(self, _event=None) -> None:
        # Selecting a device shows its cached object list (if we've read it
        # before), so returning to a device brings its objects right back.
        dev = self._selected_device()
        if dev:
            mac, _ = dev
            if mac in self.object_cache:
                inst, objs = self.object_cache[mac]
                self._populate_objects(mac, inst, objs)
                self.status_var.set(f"showing {len(objs)} cached objects for MAC {mac}")
                return
            self._clear_objects_view()
        self._update_target_banner()

    def _update_target_banner(self) -> None:
        """Refresh the big 'SENDING TO' banner to reflect the selected device
        (and object), flagging any mismatch with the listed object-list."""
        dev = self._selected_device()
        if not dev:
            self.target_lbl.config(text="▶  No device selected",
                                   bg="#4c566a", fg="#eceff4")
            return
        mac, row = dev
        inst = row.get("device_instance")
        dev_str = f"device {inst}" if inst is not None else "device (unknown #)"

        obj_txt = ""
        sel = self.obj_tree.selection()
        if sel and sel[0] in self.objects_by_iid:
            ot, i, nm = self.objects_by_iid[sel[0]]
            obj_txt = f"{bacnet.object_type_name(ot)},{i}" + (f'  “{nm}”' if nm else "")

        text = f"▶  SENDING TO:  {dev_str}   ·   MAC {mac}"
        if obj_txt:
            text += f"\n        {obj_txt}"

        mismatch = (self._objects_device is not None
                    and self._objects_device[0] != mac)
        if mismatch:
            od_mac, od_inst = self._objects_device
            text += (f"\n⚠  objects listed are from device {od_inst} (MAC {od_mac}) — "
                     f"re-read the object list for this device")
            self.target_lbl.config(text=text, bg="#bf616a", fg="#ffffff")
        elif obj_txt:
            self.target_lbl.config(text=text, bg="#2e7d32", fg="#ffffff")
        else:
            self.target_lbl.config(text=text, bg="#5e81ac", fg="#ffffff")

    def _current_index(self):
        text = self.index_var.get().strip()
        return int(text, 0) if text else None

    # --------------------------------------------------------- operations -
    def do_who_is(self) -> None:
        if not self.master:
            return
        self._log("→ Who-Is (broadcast)\n", "info")
        self.master.send_unconfirmed(BROADCAST, bacnet.build_who_is())

    def do_read_object_list(self) -> None:
        dev = self._selected_device()
        if not dev:
            messagebox.showinfo("Select device", "Select a device row first.")
            return
        mac, row = dev
        instance = row.get("device_instance")
        if instance is None:
            messagebox.showinfo(
                "Unknown device instance",
                "This device hasn't announced its instance yet.\n"
                "Click Who-Is first, then retry once it appears with a Device #.")
            return
        with_names = self.read_names.get()
        dev_type = bacnet.OBJECT_TYPES_BY_NAME["device"]
        objlist = bacnet.PROPERTY_IDS_BY_NAME["object-list"]
        name_prop = bacnet.PROPERTY_IDS_BY_NAME["object-name"]

        def work():
            count = self._transact_read(mac, dev_type, instance, objlist,
                                        array_index=0).values[0]
            self.queue.put(("log", f"object-list length = {count}"))
            objects = []
            for i in range(1, count + 1):
                res = self._transact_read(mac, dev_type, instance, objlist,
                                          array_index=i)
                otype, inst = res.values[0]
                name = ""
                if with_names:
                    try:
                        name = self._transact_read(mac, otype, inst, name_prop).display
                    except Exception:
                        name = ""
                objects.append((otype, inst, name))
                self.queue.put(("progress", (i, count)))
            return objects

        self._log(f"→ reading object-list of device,{instance} (MAC {mac})\n", "info")
        self._progress_set(0.0, "reading object list…")
        self._run_async(
            work, lambda objs, err: self._on_objects_read(objs, err, mac, instance))

    def _on_objects_read(self, objects, err, mac, instance) -> None:
        if err:
            self._log(f"! object-list failed: {err}\n", "error")
            self.status_var.set(f"Object list failed: {err}")
            self._progress_set(0.0, "failed")
            return
        self.object_cache[mac] = (instance, objects)  # remember for this device
        self._populate_objects(mac, instance, objects)
        self._log(f"✓ {len(objects)} objects\n", "info")
        self.status_var.set(f"{len(objects)} objects read")

    def _populate_objects(self, mac, instance, objects) -> None:
        self.obj_tree.delete(*self.obj_tree.get_children())
        self.objects_by_iid.clear()
        for idx, (otype, inst, name) in enumerate(objects):
            iid = f"obj{idx}"
            self.obj_tree.insert("", tk.END, iid=iid,
                                 values=(bacnet.object_type_name(otype), inst, name))
            self.objects_by_iid[iid] = (otype, inst, name)
        self._objects_device = (mac, instance)
        title = (f"Objects — device {instance} (MAC {mac})" if instance is not None
                 else f"Objects — MAC {mac}")
        self.objects_frame.config(text=title)
        self._progress_set(1.0, f"{len(objects)} objects")
        self._update_target_banner()

    def _clear_objects_view(self) -> None:
        self.obj_tree.delete(*self.obj_tree.get_children())
        self.objects_by_iid.clear()
        self._objects_device = None
        self.objects_frame.config(text="Objects")
        self._progress_set(0.0, "")

    def do_read(self) -> None:
        target = self._require_target()
        if not target:
            return
        mac, otype, inst = target
        try:
            prop_id = resolve_property(self.prop_var.get())
        except ValueError:
            messagebox.showwarning("Property", "Enter a known property name or a numeric id.")
            return
        index = self._current_index()

        def work():
            return self._transact_read(mac, otype, inst, prop_id, index)

        self._show_result(f"Reading {self.prop_var.get()}…")
        self._run_async(work, lambda res, err: self._on_read_done(res, err, prop_id))

    def _on_read_done(self, res, err, prop_id) -> None:
        name = bacnet.property_name(prop_id)
        if err:
            self._show_result(f"{name}\nERROR: {err}")
            self._log(f"! read {name} failed: {err}\n", "error")
        else:
            self._show_result(f"{name} =\n{res.display}")
            self._log(f"✓ {name} = {res.display}\n", "info")

    def do_write(self) -> None:
        target = self._require_target()
        if not target:
            return
        mac, otype, inst = target
        try:
            prop_id = resolve_property(self.prop_var.get())
            value_bytes = bacnet.encode_application_value(
                self.vtype_var.get(), self.value_var.get())
        except ValueError as exc:
            messagebox.showwarning("Write", f"Bad value/property: {exc}")
            return
        index = self._current_index()
        prio = self.prio_var.get().strip()
        priority = int(prio) if prio else None
        dev_inst = self.devices.rows.get(mac, {}).get("device_instance")
        dev_str = f"device {dev_inst} (MAC {mac})" if dev_inst is not None else f"MAC {mac}"
        self._log(
            f"→ write {self.vtype_var.get()} '{self.value_var.get()}' to {dev_str} "
            f"{bacnet.object_type_name(otype)},{inst} {self.prop_var.get()}"
            + (f" [priority {priority}]" if priority else "") + "\n", "info")

        def work():
            return self._transact_write(mac, otype, inst, prop_id, value_bytes,
                                        index, priority)

        self._show_result("Writing…")
        self._run_async(work, lambda res, err: self._on_write_done(res, err))

    def _on_write_done(self, res, err) -> None:
        if err:
            self._show_result(f"WRITE FAILED:\n{err}")
            self._log(f"! write failed: {err}\n", "error")
        else:
            self._show_result("Write acknowledged (OK)")
            self._log("✓ write acknowledged\n", "info")

    MAX_CONT_WINDOWS = 6

    def do_write_continuous(self) -> None:
        """Open an independent window that writes the chosen value to the
        selected object property repeatedly at a fixed interval."""
        target = self._require_target()
        if not target:
            return
        mac, otype, inst = target
        try:
            prop_id = resolve_property(self.prop_var.get())
        except ValueError:
            messagebox.showwarning("Property", "Enter a known property name or a numeric id.")
            return
        index = self._current_index()

        # Don't allow a second continuous-write on a property that's already
        # being written — focus the existing window instead.
        key = (mac, otype, inst, prop_id, index)
        for w in self.cont_windows:
            if (w.mac, w.otype, w.inst, w.prop_id, w.array_index) == key:
                messagebox.showwarning(
                    "Already running",
                    f"A continuous-write window for MAC {mac} "
                    f"{bacnet.object_type_name(otype)},{inst} "
                    f"{self.prop_var.get()} is already open.")
                try:
                    w.win.deiconify()
                    w.win.lift()
                    w.win.focus_force()
                except tk.TclError:
                    pass
                return

        if len(self.cont_windows) >= self.MAX_CONT_WINDOWS:
            messagebox.showinfo(
                "Limit reached",
                f"You can have at most {self.MAX_CONT_WINDOWS} continuous-write "
                f"windows open at once.\nClose one before opening another.")
            return

        dev_inst = self.devices.rows.get(mac, {}).get("device_instance")
        prio = self.prio_var.get().strip()
        ContinuousWriteWindow(
            self, mac=mac, dev_inst=dev_inst, otype=otype, inst=inst,
            prop_id=prop_id, prop_label=self.prop_var.get(),
            obj_label=f"{bacnet.object_type_name(otype)},{inst}",
            value_type=self.vtype_var.get(),
            priority=int(prio) if prio else None,
            array_index=index,
            init_value=self.value_var.get())

    def _require_target(self):
        dev = self._selected_device()
        sel = self.obj_tree.selection()
        if not dev:
            messagebox.showinfo("Select device", "Select a device row first.")
            return None
        if not sel or sel[0] not in self.objects_by_iid:
            messagebox.showinfo("Select object", "Select an object first.")
            return None
        otype, inst, _ = self.objects_by_iid[sel[0]]
        return dev[0], otype, inst

    # --------------------------------------------------------- queue loop -
    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "frame":
                    self._handle_frame(payload)
                elif kind == "log":
                    tag = "error" if payload.lstrip().startswith("!") else "info"
                    self._log(payload + "\n", tag)
                elif kind == "call":
                    payload()
                elif kind == "progress":
                    done, total = payload
                    self.status_var.set(f"reading objects {done}/{total}…")
                    self._progress_set(done / total if total else 0.0,
                                       f"{done} / {total}")
        except queue.Empty:
            pass
        self._check_connection_alive()
        self.root.after(60, self._poll_queue)

    def _handle_frame(self, frame: "mstp.Frame") -> None:
        self.frame_count += 1
        decoded = None
        line = (f"[{time.strftime('%H:%M:%S')}] {frame.source:>3} -> "
                f"{frame.dest_str():<9} {frame.type_name}")
        tag = "token"
        if frame.is_data and frame.data:
            decoded = bacnet.decode_npdu(frame.data)
            line += f"  |  {decoded.summary}"
            tag = "data" if ("I-Am" in decoded.summary or "Who-Is" in decoded.summary) else "mgmt"
        if not frame.crc_ok:
            line += "  [CRC ERROR]"
            tag = "error"
            self.error_count += 1
        self.devices.observe(frame, decoded)
        self._refresh_device_row(frame.source)
        if not self.paused.get():
            self._append_log(line + "\n", tag)
        self._update_counts()

    def _refresh_device_row(self, mac: int) -> None:
        row = self.devices.rows.get(mac)
        if not row:
            return
        iid = f"mac{mac}"
        values = (mac,
                  row["device_instance"] if row["device_instance"] is not None else "-",
                  row["vendor_id"] if row["vendor_id"] is not None else "-",
                  row["frames"])
        if self.dev_tree.exists(iid):
            self.dev_tree.item(iid, values=values)
        else:
            self.dev_tree.insert("", tk.END, iid=iid, values=values)

    # --------------------------------------------------------- log utils --
    def _log(self, text: str, tag: str) -> None:
        self._append_log(text, tag)

    def _append_log(self, text: str, tag: str) -> None:
        if self._capture:
            try:
                self._capture.write(text)
                self._capture.flush()
            except OSError:
                pass
        self.log.config(state=tk.NORMAL)
        at_bottom = self.log.yview()[1] > 0.999
        self.log.insert(tk.END, text, tag)
        if int(self.log.index("end-1c").split(".")[0]) > 5000:
            self.log.delete("1.0", "1000.0")
        if at_bottom:
            self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _show_result(self, text: str) -> None:
        self.result.config(state=tk.NORMAL)
        self.result.delete("1.0", tk.END)
        self.result.insert(tk.END, text)
        self.result.config(state=tk.DISABLED)

    # -------------------------------------------------- read progress bar --
    def _progress_draw(self) -> None:
        c = self.progress_canvas
        w, h = c.winfo_width(), c.winfo_height()
        c.delete("all")
        if w <= 1:
            return
        fill = max(0, min(w, int(w * self._progress_frac)))
        if fill > 0:
            c.create_rectangle(0, 0, fill, h, fill="#2e7d32", outline="")  # green
        if self._progress_text:
            c.create_text(w // 2, h // 2, text=self._progress_text,
                          fill="#eceff4", font=("", 8))

    def _progress_set(self, frac: float, text: str) -> None:
        self._progress_frac = frac
        self._progress_text = text
        self._progress_draw()

    def _update_counts(self) -> None:
        self.counts_var.set(f"frames: {self.frame_count}   errors: {self.error_count}")

    def _on_close(self) -> None:
        for w in list(self.cont_windows):
            w.stop()
        self.disconnect()
        if self._capture:
            try:
                self._capture.close()
            except OSError:
                pass
        self.root.after(150, self.root.destroy)


class ContinuousWriteWindow:
    """A standalone window that writes a value to one object property on a
    fixed interval, in its own worker thread, until stopped or closed.

    It snapshots its target on creation, so the main window can move on to other
    objects/devices without affecting a running loop. Value and interval are
    live-editable; type and priority are fixed once Start is pressed.
    """

    BG_RUN = "#2e7d32"
    BG_ERR = "#bf616a"
    BG_IDLE = "#5e81ac"

    def __init__(self, app, mac, dev_inst, otype, inst, prop_id, prop_label,
                 obj_label, value_type, priority, array_index, init_value):
        self.app = app
        self.mac = mac
        self.otype = otype
        self.inst = inst
        self.prop_id = prop_id
        self.array_index = array_index
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sent = 0
        self._acked = 0  # writes the device acknowledged (Simple-ACK)
        # Plain attributes read by the worker (updated live from Tk on GUI thread).
        self._live_value = init_value
        self._live_interval = 3.0
        self._vtype = value_type
        self._priority = priority

        win = tk.Toplevel(app.root)
        self.win = win
        win.title(f"Continuous Write — MAC {mac}")
        win.geometry("380x290")
        win.transient(app.root)

        dev_str = f"device {dev_inst}" if dev_inst is not None else "device (unknown #)"
        self.banner = tk.Label(
            win, text=f"▶  SENDING TO:  {dev_str}   ·   MAC {mac}\n        {obj_label}  {prop_label}",
            font=("", 10, "bold"), justify=tk.LEFT, anchor=tk.W,
            bg=self.BG_IDLE, fg="#ffffff", relief=tk.RIDGE, bd=2, padx=8, pady=6)
        self.banner.pack(fill=tk.X, padx=8, pady=8)

        body = ttk.Frame(win, padding=8)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text="Value:").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.value_var = tk.StringVar(value=str(init_value))
        ttk.Entry(body, textvariable=self.value_var, width=20).grid(
            row=0, column=1, sticky=tk.W)

        ttk.Label(body, text="Type:").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.type_var = tk.StringVar(value=value_type)
        self.type_combo = ttk.Combobox(body, textvariable=self.type_var,
                                       values=bacnet.WRITE_TYPES, width=16,
                                       state="readonly")
        self.type_combo.grid(row=1, column=1, sticky=tk.W)

        ttk.Label(body, text="Priority:").grid(row=2, column=0, sticky=tk.W, pady=3)
        self.prio_var = tk.StringVar(value=str(priority) if priority else "")
        self.prio_combo = ttk.Combobox(body, textvariable=self.prio_var,
                                       values=[""] + [str(i) for i in range(1, 17)],
                                       width=6, state="readonly")
        self.prio_combo.grid(row=2, column=1, sticky=tk.W)

        ttk.Label(body, text="Interval (s):").grid(row=3, column=0, sticky=tk.W, pady=3)
        self.interval_var = tk.StringVar(value="3")
        ttk.Entry(body, textvariable=self.interval_var, width=8).grid(
            row=3, column=1, sticky=tk.W)

        self.start_btn = ttk.Button(body, text="Start", command=self.toggle)
        self.start_btn.grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(10, 6))

        self.status_var = tk.StringVar(value="idle — press Start")
        ttk.Label(body, textvariable=self.status_var, wraplength=340,
                  justify=tk.LEFT).grid(row=5, column=0, columnspan=2, sticky=tk.W)

        self.value_var.trace_add("write", self._on_value_change)
        self.interval_var.trace_add("write", self._on_interval_change)

        win.protocol("WM_DELETE_WINDOW", self.close)
        app.cont_windows.append(self)

    # ------------------------------------------------------ live edits ---
    def _on_value_change(self, *_):
        self._live_value = self.value_var.get()

    def _on_interval_change(self, *_):
        try:
            v = float(self.interval_var.get())
            if v > 0:
                self._live_interval = v
        except ValueError:
            pass  # keep the last valid interval

    # -------------------------------------------------------- controls ---
    def toggle(self):
        if self._thread and self._thread.is_alive():
            self.stop()
        else:
            self.start()

    def start(self):
        if not self.app._connected or self.app.master is None:
            self._set_status("not connected — connect in the main window first",
                             self.BG_ERR)
            return
        self._vtype = self.type_var.get()
        prio = self.prio_var.get().strip()
        self._priority = int(prio) if prio else None
        self._on_value_change()
        self._on_interval_change()
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        self.start_btn.config(text="Stop")
        self.type_combo.config(state=tk.DISABLED)
        self.prio_combo.config(state=tk.DISABLED)

    def stop(self):
        self._stop.set()
        try:
            self.start_btn.config(text="Start")
            self.type_combo.config(state="readonly")
            self.prio_combo.config(state="readonly")
        except tk.TclError:
            pass

    def close(self):
        self.stop()
        if self in self.app.cont_windows:
            self.app.cont_windows.remove(self)
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    # ---------------------------------------------------------- worker ---
    def _worker(self):
        while not self._stop.is_set():
            if self.app.master is None or not self.app._connected:
                self._post("stopped — connection lost", self.BG_ERR)
                self.app.queue.put(("call", self.stop))
                break
            try:
                value_bytes = bacnet.encode_application_value(
                    self._vtype, self._live_value)
                self.app._transact_write(self.mac, self.otype, self.inst,
                                         self.prop_id, value_bytes,
                                         self.array_index, self._priority)
                self._sent += 1
                self._acked += 1
                self._post(f"sent {self._sent} · resp {self._acked}  ✓   "
                           f"(value={self._live_value}, "
                           f"every {self._live_interval:g}s)", self.BG_RUN)
            except Exception as exc:  # bad value / timeout / error PDU
                self._sent += 1
                self._post(f"sent {self._sent} · resp {self._acked}  ✗   {exc}",
                           self.BG_ERR)
            self._stop.wait(self._live_interval)

    def _post(self, msg, bg):
        # Marshal the status update onto the GUI thread via the app's queue.
        self.app.queue.put(("call", lambda: self._set_status(msg, bg)))

    def _set_status(self, msg, bg):
        try:
            if self.win.winfo_exists():
                self.status_var.set(msg)
                self.banner.config(bg=bg)
        except tk.TclError:
            pass


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    ExplorerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
