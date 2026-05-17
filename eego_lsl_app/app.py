from __future__ import annotations

import math
import queue
import statistics
import sys
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from eego_sdk import EegoSdk
from layout import Electrode, auxiliary_contacts, impedance_contacts, load_layout, normalize_to_canvas, reference_electrodes
from workers import StreamWorker, WorkerConfig
from firewall import add_firewall_rules, ensure_firewall_rule_interactive

GREEN = "#35c46f"
YELLOW = "#f4c542"
RED = "#e85d5d"
GRAY = "#a8b3b8"
DARK = "#24323a"
CANVAS_BG = "#f7f9fb"
HEAD_OUTLINE = "#d8dee4"
ELECTRODE_OUTLINE = "#4f5b62"


class EegoLslApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("eego LSL: Impedance + EEG v20")
        self.geometry("1160x780")
        self.minsize(1020, 680)

        self.layout_path: Path | None = None
        self.electrodes: list[Electrode] = []
        self.electrode_items: dict[str, int] = {}
        self.electrode_text_items: dict[str, int] = {}
        # Separate canvas item IDs for auxiliary duplicate icons (EOG/REF/GND).
        # EOG is also Ref 32 in the main layout, so it needs its own aux item.
        self.aux_icon_items: dict[str, int] = {}
        self.aux_icon_text_items: dict[str, int] = {}
        # Auxiliary contacts shown in the upper-left of the topomap for 64-channel caps.
        # These are not part of the 64 EEG reference channels; they are display/status contacts.
        self.aux_channel_names = ["EOG", "REF", "GND"]
        self.last_values: dict[str, float] = {}
        # Topomap colours are based only on impedance values. EEG voltage is never used for colouring.
        self.last_impedance_values: dict[str, float] = {}
        self.signal_buffers: dict[str, deque[float]] = {}
        self.signal_buffer_maxlen = 1024
        self.signal_names: list[str] = []
        self.last_unit: str = "kΩ"
        self.filter_state: dict[tuple[str, str], tuple[float, float, float, float]] = {}
        self.filter_signature: tuple | None = None
        self.last_signal_redraw_time = 0.0
        self.signal_redraw_interval_s = 0.16
        self.amplifiers = []
        self.worker: StreamWorker | None = None
        self.queue: queue.Queue = queue.Queue()

        self._build_ui()
        self.after(250, self._ask_layout_on_start)
        self.after(700, self._ensure_firewall_on_startup)
        self.after(100, self._poll_worker_queue)

    def _ensure_firewall_on_startup(self):
        ensure_firewall_rule_interactive(self)

    def configure_firewall(self):
        ensure_firewall_rule_interactive(self, force_prompt=True)

    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(root)
        top.pack(fill=tk.X)

        ttk.Button(top, text="Load electrode layout", command=self.load_layout_dialog).pack(side=tk.LEFT)
        ttk.Button(top, text="Configure firewall", command=self.configure_firewall).pack(side=tk.LEFT, padx=(8, 0))
        self.layout_label = ttk.Label(top, text="No layout loaded")
        self.layout_label.pack(side=tk.LEFT, padx=8)

        ttk.Separator(root).pack(fill=tk.X, pady=8)

        controls = ttk.Frame(root)
        controls.pack(fill=tk.X)

        ttk.Button(controls, text="Detect amplifier", command=self.detect_amplifiers).grid(row=0, column=0, padx=4, pady=4)
        self.amp_combo = ttk.Combobox(controls, state="readonly", width=34)
        self.amp_combo.grid(row=0, column=1, padx=4, pady=4)

        ttk.Label(controls, text="Display / stream").grid(row=0, column=2, padx=(20, 4), sticky=tk.E)
        self.mode_var = tk.StringVar(value="Impedance (kΩ)")
        self.mode_combo = ttk.Combobox(
            controls,
            state="readonly",
            width=22,
            textvariable=self.mode_var,
            values=("Impedance (kΩ)", "EEG activity (µV)"),
        )
        self.mode_combo.grid(row=0, column=3, padx=4, pady=4, sticky=tk.W)
        self.mode_combo.bind("<<ComboboxSelected>>", lambda _event: self._mode_changed())

        ttk.Label(controls, text="Sampling rate").grid(row=0, column=4, padx=(20, 4), sticky=tk.E)
        self.sampling_rate_var = tk.StringVar(value="512")
        ttk.Entry(controls, textvariable=self.sampling_rate_var, width=8).grid(row=0, column=5, padx=4, sticky=tk.W)

        ttk.Label(controls, text="Green below kΩ").grid(row=1, column=0, padx=4, sticky=tk.E)
        self.good_ohm_var = tk.StringVar(value="10")
        self.good_ohm_entry = ttk.Entry(controls, textvariable=self.good_ohm_var, width=10)
        self.good_ohm_entry.grid(row=1, column=1, sticky=tk.W)
        ttk.Label(controls, text="Red above kΩ").grid(row=1, column=2, padx=4, sticky=tk.E)
        self.ok_ohm_var = tk.StringVar(value="20")
        self.ok_ohm_entry = ttk.Entry(controls, textvariable=self.ok_ohm_var, width=10)
        self.ok_ohm_entry.grid(row=1, column=3, sticky=tk.W)
        ttk.Button(controls, text="Apply thresholds", command=self.apply_impedance_thresholds).grid(row=1, column=4, padx=(16, 4), sticky=tk.W)
        self.threshold_note_var = tk.StringVar(value="Bands: <10 kΩ green, 10–20 kΩ yellow, >20 kΩ red")
        ttk.Label(controls, textvariable=self.threshold_note_var).grid(row=1, column=5, sticky=tk.W)
        self.good_ohm_entry.bind("<Return>", lambda _event: self.apply_impedance_thresholds())
        self.ok_ohm_entry.bind("<Return>", lambda _event: self.apply_impedance_thresholds())

        ttk.Label(controls, text="Electrode spacing").grid(row=2, column=0, padx=4, sticky=tk.E)
        self.layout_spacing_var = tk.DoubleVar(value=1.0)
        self.layout_spacing_slider = ttk.Scale(
            controls,
            from_=0.75,
            to=1.35,
            variable=self.layout_spacing_var,
            command=lambda _value: self.redraw_layout(),
        )
        self.layout_spacing_slider.grid(row=2, column=1, columnspan=2, sticky=tk.EW, padx=4)


        actions = ttk.Frame(root)
        actions.pack(fill=tk.X, pady=8)
        self.start_btn = ttk.Button(actions, text="Start LSL Stream", command=self.start_selected_stream)
        self.start_btn.pack(side=tk.LEFT, padx=4)
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop_stream, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=4)

        self.stream_note_var = tk.StringVar(value="Selected mode: impedance state in kΩ. Topomap colours are impedance-only.")
        ttk.Label(actions, textvariable=self.stream_note_var).pack(side=tk.LEFT, padx=14)

        self.status_var = tk.StringVar(value="Load a layout, then detect amplifier.")
        ttk.Label(root, textvariable=self.status_var).pack(fill=tk.X, pady=2)

        body = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=1)

        self.notebook = ttk.Notebook(left)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        topo_tab = ttk.Frame(self.notebook)
        signal_tab = ttk.Frame(self.notebook)
        self.notebook.add(topo_tab, text="Topomap")
        self.notebook.add(signal_tab, text="Signal viewer")

        self.canvas = tk.Canvas(topo_tab, bg=CANVAS_BG, highlightthickness=1, highlightbackground="#dfe5ea")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda event: self.redraw_layout())

        self._build_signal_viewer(signal_tab)

        ttk.Label(right, text="Live values").pack(anchor=tk.W)
        cols = ("electrode", "value", "status")
        self.table = ttk.Treeview(right, columns=cols, show="headings", height=16)
        for col in cols:
            self.table.heading(col, text=col.capitalize())
        self.table.column("electrode", width=90)
        self.table.column("value", width=120)
        self.table.column("status", width=90)
        self.table.pack(fill=tk.BOTH, expand=True)

        ttk.Label(right, text="Messages / errors").pack(anchor=tk.W, pady=(8, 0))
        self.log = tk.Text(right, height=14, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=False, pady=(2, 0))


    def _build_signal_viewer(self, parent):
        """Create a lightweight native-Tkinter EEG viewer.

        This viewer draws the same EEG data that is being sent to LSL. The
        optional filter is display-only: it does not change the LSL stream.
        """
        controls = ttk.Frame(parent, padding=(8, 8, 8, 4))
        controls.pack(fill=tk.X)

        ttk.Label(controls, text="Channels shown").grid(row=0, column=0, sticky=tk.W, padx=(0, 4))
        self.signal_count_var = tk.StringVar(value="64")
        ttk.Combobox(
            controls,
            state="readonly",
            width=6,
            textvariable=self.signal_count_var,
            values=("4", "8", "16", "32", "48", "64"),
        ).grid(row=0, column=1, sticky=tk.W, padx=(0, 12))

        self.signal_autoscroll_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="Auto-follow latest", variable=self.signal_autoscroll_var).grid(row=0, column=7, sticky=tk.W, padx=(12, 0))

        ttk.Label(controls, text="Scale µV/div").grid(row=0, column=2, sticky=tk.W, padx=(0, 4))
        self.signal_scale_var = tk.StringVar(value="100")
        ttk.Entry(controls, textvariable=self.signal_scale_var, width=8).grid(row=0, column=3, sticky=tk.W, padx=(0, 16))

        self.filter_enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Display filter", variable=self.filter_enabled_var).grid(row=0, column=4, sticky=tk.W, padx=(0, 8))

        ttk.Label(controls, text="HP Hz").grid(row=1, column=0, sticky=tk.W, padx=(0, 4), pady=(6, 0))
        self.hp_var = tk.StringVar(value="0.5")
        ttk.Entry(controls, textvariable=self.hp_var, width=8).grid(row=1, column=1, sticky=tk.W, padx=(0, 12), pady=(6, 0))

        ttk.Label(controls, text="LP Hz").grid(row=1, column=2, sticky=tk.W, padx=(0, 4), pady=(6, 0))
        self.lp_var = tk.StringVar(value="40")
        ttk.Entry(controls, textvariable=self.lp_var, width=8).grid(row=1, column=3, sticky=tk.W, padx=(0, 12), pady=(6, 0))

        ttk.Label(controls, text="Notch").grid(row=1, column=4, sticky=tk.W, padx=(0, 4), pady=(6, 0))
        self.notch_var = tk.StringVar(value="50 Hz")
        ttk.Combobox(
            controls,
            state="readonly",
            width=8,
            textvariable=self.notch_var,
            values=("Off", "50 Hz", "60 Hz"),
        ).grid(row=1, column=5, sticky=tk.W, padx=(0, 12), pady=(6, 0))

        ttk.Button(controls, text="Apply filter", command=self.reset_display_filter).grid(row=1, column=6, sticky=tk.W, pady=(6, 0))

        note = ttk.Label(
            parent,
            text="The filter is display-only. The outgoing LSL EEG stream remains raw microvolt data.",
        )
        note.pack(fill=tk.X, padx=8, pady=(0, 4))

        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.signal_canvas = tk.Canvas(frame, bg="#ffffff", highlightthickness=1, highlightbackground="#dfe5ea")
        yscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.signal_canvas.yview)
        self.signal_canvas.configure(yscrollcommand=yscroll.set)
        self.signal_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.signal_canvas.bind("<Configure>", lambda _event: self.redraw_signal())
        self.signal_canvas.bind("<MouseWheel>", self._on_signal_mousewheel)
        self.filter_signature = self._current_filter_signature()

    def _ask_layout_on_start(self):
        answer = messagebox.askyesno("Electrode layout", "Load an electrode layout before using the app?")
        if answer:
            self.load_layout_dialog()

    def _mode_changed(self):
        if self.selected_mode() == "impedance":
            self.stream_note_var.set("Selected mode: impedance state in kΩ.")
            self.last_unit = "kΩ"
        else:
            self.stream_note_var.set("Selected mode: EEG activity in microvolts. Topomap colours use last impedance values only.")
            self.last_unit = "µV"
        self.last_values.clear()
        self.clear_signal_buffers()
        self._refresh_table_names()
        self.redraw_layout()
        self.redraw_signal()

    def selected_mode(self) -> str:
        return "eeg" if "µV" in self.mode_var.get() else "impedance"

    def log_msg(self, msg: str):
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)

    def load_layout_dialog(self):
        path = filedialog.askopenfilename(
            title="Select electrode layout",
            filetypes=[("Layout files", "*.txt *.csv *.tsv *.json"), ("All files", "*.*")],
            initialdir=str(Path(__file__).resolve().parent / "capfiles"),
        )
        if not path:
            return
        try:
            self.electrodes = load_layout(path)
            self.layout_path = Path(path)
            self.last_values.clear()
            self.last_impedance_values.clear()
            self.clear_signal_buffers()
            n_ref = len(reference_electrodes(self.electrodes))
            n_aux = len(auxiliary_contacts(self.electrodes))
            self.layout_label.configure(text=f"{self.layout_path.name} ({n_ref} EEG + {n_aux} aux contacts)")
            self.status_var.set("Layout loaded. Detect amplifier next.")
            self.redraw_layout()
            self._refresh_table_names()
        except Exception as exc:
            messagebox.showerror("Layout error", str(exc))

    def detect_amplifiers(self):
        try:
            with EegoSdk() as sdk:
                self.amplifiers = sdk.amplifiers()
                version = sdk.version()
        except Exception as exc:
            messagebox.showerror("eego SDK error", str(exc))
            self.status_var.set("Could not load SDK or detect amplifier.")
            return
        if not self.amplifiers:
            self.amp_combo["values"] = []
            self.status_var.set("No amplifier detected.")
            return
        values = [f"{a.serial} | id={a.id}" for a in self.amplifiers]
        self.amp_combo["values"] = values
        self.amp_combo.current(0)
        self.status_var.set(f"Detected {len(self.amplifiers)} amplifier(s). SDK version: {version}")

    def selected_amp(self):
        idx = self.amp_combo.current()
        if idx < 0 or idx >= len(self.amplifiers):
            raise RuntimeError("No amplifier selected. Click 'Detect amplifier' first.")
        return self.amplifiers[idx]

    def make_config(self) -> WorkerConfig:
        amp = self.selected_amp()
        if not self.electrodes:
            raise RuntimeError("Load an electrode layout first.")
        refs = reference_electrodes(self.electrodes)
        if not refs:
            raise RuntimeError("The selected layout contains no regular EEG reference electrodes.")
        selected_refs = refs[:64]
        if len(refs) > 64:
            self.log_msg(f"Using first 64 regular EEG reference electrodes; layout contains {len(refs)} reference entries.")
        # Impedance/EEG mapping is based on the manual SDK reference order.
        # For the 64-channel waveguard layout, EOG is Ref 32 and is therefore
        # included in selected_refs. REF/GND are separate contacts.
        imp_names = [e.name for e in selected_refs]
        eog_contacts = [e for e in selected_refs if e.name.upper() == "EOG"]
        return WorkerConfig(
            amplifier_id=amp.id,
            serial=amp.serial or str(amp.id),
            channel_names=[e.name for e in selected_refs],
            sampling_rate=int(self.sampling_rate_var.get()),
            stream_lsl=True,
            selected_reference_count=len(selected_refs),
            eog_channel_name=eog_contacts[0].name if eog_contacts else None,
            impedance_channel_names=imp_names,
        )

    def start_selected_stream(self):
        self._start_mode(self.selected_mode())

    def _start_mode(self, mode: str):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Stream already active", "Stop the current stream before switching display/stream mode.")
            return
        try:
            config = self.make_config()
        except Exception as exc:
            messagebox.showerror("Cannot start", str(exc))
            return
        self.last_values.clear()
        if mode == "impedance":
            self.last_impedance_values.clear()
        self.clear_signal_buffers()
        self.last_unit = "kΩ" if mode == "impedance" else "µV"
        self._refresh_table_names()
        self.redraw_layout()
        self.worker = StreamWorker(mode, config, self.queue)
        self.worker.start()
        self.start_btn.configure(state=tk.DISABLED)
        self.mode_combo.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.status_var.set(f"Starting {self._mode_label(mode)} LSL stream...")

    def stop_stream(self):
        if not self.worker:
            return
        self.stop_btn.configure(state=tk.DISABLED)
        self.status_var.set("Stopping stream and closing SDK/LSL resources...")
        self.worker.stop()
        self.after(100, self._check_worker_stopped)

    def _check_worker_stopped(self):
        if self.worker and self.worker.is_alive():
            self.after(100, self._check_worker_stopped)
            return
        self.worker = None
        self.start_btn.configure(state=tk.NORMAL)
        self.mode_combo.configure(state="readonly")
        self.stop_btn.configure(state=tk.DISABLED)
        self.status_var.set("Stopped. Stream closed.")

    def _poll_worker_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                self._handle_worker_msg(msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_worker_queue)

    def _handle_worker_msg(self, msg: dict):
        typ = msg.get("type")
        if typ == "started":
            self.status_var.set(f"Started {self._mode_label(msg.get('mode'))} LSL stream with {len(msg.get('channels', []))} channels.")
            self.log_msg(self.status_var.get())
        elif typ == "impedance":
            self._update_values(msg["names"], msg["values"], unit="kΩ")
        elif typ == "eeg":
            self._update_values(msg["names"], msg["values"], unit="µV")
        elif typ == "eeg_block":
            self._update_signal_block(msg.get("names", []), msg.get("samples", []), unit="µV")
        elif typ == "info":
            self.log_msg("INFO: " + msg.get("message", ""))
            self.status_var.set(msg.get("message", ""))
        elif typ == "error":
            self.log_msg("ERROR: " + msg.get("message", "unknown error"))
            messagebox.showerror("Stream error", msg.get("message", "unknown error"))
        elif typ == "stopped":
            self.worker = None
            self.start_btn.configure(state=tk.NORMAL)
            self.mode_combo.configure(state="readonly")
            self.stop_btn.configure(state=tk.DISABLED)
            self.status_var.set("Stopped. Stream closed.")
            self.log_msg("Stream stopped and SDK stream closed.")

    def _mode_label(self, mode: str | None) -> str:
        return "impedance" if mode == "impedance" else "EEG / microvolt"

    def redraw_layout(self):
        self.canvas.delete("all")
        self.electrode_items.clear()
        self.electrode_text_items.clear()
        self.aux_icon_items.clear()
        self.aux_icon_text_items.clear()
        if not self.electrodes:
            self.canvas.create_text(24, 24, anchor=tk.NW, text="No layout loaded", fill=GRAY, font=("Segoe UI", 11))
            return

        w = max(self.canvas.winfo_width(), 520)
        h = max(self.canvas.winfo_height(), 440)
        cx, cy = w / 2, h / 2 + 10
        radius = min(w, h) * 0.42

        # Soft head outline, ears, and nose. This makes the front/up direction explicit.
        self.canvas.create_oval(cx - radius, cy - radius, cx + radius, cy + radius, outline=HEAD_OUTLINE, width=3)
        self.canvas.create_arc(cx - radius - 18, cy - 42, cx - radius + 18, cy + 42, start=80, extent=200, outline=HEAD_OUTLINE, width=3, style=tk.ARC)
        self.canvas.create_arc(cx + radius - 18, cy - 42, cx + radius + 18, cy + 42, start=-100, extent=200, outline=HEAD_OUTLINE, width=3, style=tk.ARC)
        self.canvas.create_polygon(cx - 18, cy - radius + 8, cx, cy - radius - 22, cx + 18, cy - radius + 8, outline=HEAD_OUTLINE, fill=CANVAS_BG, width=3)
        self.canvas.create_text(cx, cy - radius - 42, text="front", fill="#687680", font=("Segoe UI", 11))

        self._draw_accessory_icons(cx, cy, radius)

        display_electrodes = reference_electrodes(self.electrodes)
        coords = normalize_to_canvas(display_electrodes, w, h, margin=int(min(w, h) * 0.16), orientation="eego_topomap")
        spacing = float(getattr(self, "layout_spacing_var", tk.DoubleVar(value=1.0)).get())
        if abs(spacing - 1.0) > 1e-6:
            coords = {name: (cx + (x - cx) * spacing, cy + (y - cy) * spacing) for name, (x, y) in coords.items()}

        n = len(display_electrodes)
        r = 17 if n <= 70 else 13
        font_size = 8 if n > 70 else 9

        # Draw in two passes so labels sit cleanly above all electrode circles.
        # IMPORTANT: electrode colour is based only on the last impedance value.
        # EEG voltage values are never used for topomap colouring.
        for e in display_electrodes:
            x, y = coords[e.name]
            color = self._impedance_color_for_name(e.name)
            item = self.canvas.create_oval(
                x - r,
                y - r,
                x + r,
                y + r,
                fill=color,
                outline=ELECTRODE_OUTLINE,
                width=1,
            )
            self.electrode_items[e.name] = item

        for e in display_electrodes:
            x, y = coords[e.name]
            txt = self.canvas.create_text(x, y, text=e.name, fill=DARK, font=("Segoe UI", font_size))
            self.electrode_text_items[e.name] = txt


    def _layout_aux_names(self) -> list[str]:
        """Auxiliary cap contacts to draw for impedance status.

        EOG is Ref 32 in the EE-21x/EE-22x 64-channel Appendix-A order, so it is
        still part of the main EEG/impedance channel list. We also show a
        duplicate, data-aware EOG icon in the Aux box because it is physically
        an auxiliary contact on the cap cable and is useful during preparation.
        """
        aux = {e.name.upper(): e.name for e in auxiliary_contacts(self.electrodes)}
        refs = {e.name.upper(): e.name for e in reference_electrodes(self.electrodes)}
        names: list[str] = []

        # EOG may be listed as a reference contact rather than as an auxiliary
        # contact. Include it if it exists in either place or if impedance has
        # already returned an EOG value.
        for key in ("EOG", "REF", "GND"):
            if key in aux:
                names.append(aux[key])
            elif key in refs:
                names.append(refs[key])
            elif key in self.last_impedance_values:
                names.append(key)

        # De-duplicate while preserving order/capitalisation.
        seen = set()
        out = []
        for name in names:
            up = name.upper()
            if up not in seen:
                out.append(name)
                seen.add(up)
        return out

    def _draw_accessory_icons(self, cx: float, cy: float, radius: float):
        """Draw data-aware EOG/REF/GND markers in the upper-left of the topomap."""
        aux_names = self._layout_aux_names()
        if not aux_names:
            return

        panel_x = cx - radius * 0.92
        panel_y = cy - radius * 0.90
        panel_w = max(132, 50 + 58 * len(aux_names))
        panel_h = 74
        self.canvas.create_rectangle(
            panel_x - 18, panel_y - 22, panel_x - 18 + panel_w, panel_y - 22 + panel_h,
            fill="#eef3f7", outline="#d6dde3", width=1,
        )
        self.canvas.create_text(panel_x - 8, panel_y - 14, anchor=tk.NW, text="Aux", fill="#65727a", font=("Segoe UI", 8, "bold"))

        r = 15
        for i, name in enumerate(aux_names):
            x = panel_x + 24 + i * 58
            y = panel_y + 22
            color = self._impedance_color_for_name(name)
            item = self.canvas.create_oval(
                x - r, y - r, x + r, y + r,
                fill=color, outline=ELECTRODE_OUTLINE, width=1,
            )
            # Keep aux icons separate from the main topomap items. This is
            # essential for EOG, which is also a normal referential channel
            # in the Appendix-A order.
            self.aux_icon_items[name] = item
            self.aux_icon_text_items[name] = self.canvas.create_text(
                x, y, text=name, fill=DARK, font=("Segoe UI", 8, "bold"),
            )

    def _refresh_table_names(self):
        for item in self.table.get_children():
            self.table.delete(item)
        for e in reference_electrodes(self.electrodes)[:64]:
            self.table.insert("", tk.END, iid=e.name, values=(e.name, "—", "unknown"))
        for name in self._layout_aux_names():
            if not self.table.exists(name):
                self.table.insert("", tk.END, iid=name, values=(name, "—", "unknown"))

    def _update_values(self, names, values, unit: str):
        self.last_unit = unit
        known_names = {e.name for e in reference_electrodes(self.electrodes)} | set(self._layout_aux_names())
        if unit == "kΩ":
            # Impedance updates are the only source of electrode/topomap colour.
            for name, value in zip(names, values):
                if name not in self.electrode_items and name not in known_names:
                    continue
                v = float(value)
                self.last_values[name] = v
                self.last_impedance_values[name] = v
                if name in self.electrode_items:
                    self.canvas.itemconfigure(self.electrode_items[name], fill=self._impedance_color(v))
                if name in self.aux_icon_items:
                    self.canvas.itemconfigure(self.aux_icon_items[name], fill=self._impedance_color(v))
                status = self._impedance_status(v)
                if self.table.exists(name):
                    self.table.item(name, values=(name, f"{v:.1f} kΩ", status))
            if values:
                try:
                    med = statistics.median([abs(float(v)) for v in values])
                    self.status_var.set(f"Live impedance values. Median: {med:.1f} kΩ")
                except Exception:
                    pass
            return

        # EEG updates are displayed numerically only. They do not recolour the electrode diagram.
        for name, value in zip(names, values):
            if name not in self.electrode_items and name not in known_names:
                continue
            v = float(value)
            self.last_values[name] = v
            status = self._status(v, unit)
            if self.table.exists(name):
                self.table.item(name, values=(name, f"{v:.1f} {unit}", status))
        if values:
            try:
                med = statistics.median([abs(float(v)) for v in values])
                self.status_var.set(f"Live {unit} values. Median: {med:.1f} {unit}. Topomap colours remain impedance-only.")
            except Exception:
                pass

    def _thresholds(self):
        good, ok = self._validate_thresholds(show_error=False)
        return good, ok

    def _validate_thresholds(self, show_error: bool = True) -> tuple[float, float]:
        try:
            good = float(self.good_ohm_var.get().replace(",", "."))
            ok = float(self.ok_ohm_var.get().replace(",", "."))
        except Exception:
            if show_error:
                messagebox.showerror("Invalid thresholds", "Impedance thresholds must be numbers, e.g. 10 and 20.")
            return 10.0, 20.0
        if good < 0 or ok < 0 or good >= ok:
            if show_error:
                messagebox.showerror(
                    "Invalid thresholds",
                    "Use two positive values where the green threshold is lower than the red threshold. Example: 10 and 20.",
                )
            return 10.0, 20.0
        return good, ok

    def apply_impedance_thresholds(self):
        good, ok = self._validate_thresholds(show_error=True)
        self.good_ohm_var.set(f"{good:g}")
        self.ok_ohm_var.set(f"{ok:g}")
        self.threshold_note_var.set(f"Bands: <{good:g} kΩ green, {good:g}–{ok:g} kΩ yellow, >{ok:g} kΩ red")
        self.redraw_layout()
        if self.last_unit == "kΩ":
            for name, value in list(self.last_impedance_values.items()):
                if self.table.exists(name):
                    status = self._impedance_status(value)
                    self.table.item(name, values=(name, f"{value:.1f} kΩ", status))

    def _impedance_status(self, value: float) -> str:
        good, ok = self._thresholds()
        if value < good:
            return "good"
        if value <= ok:
            return "ok"
        return "bad"

    def _impedance_color(self, value: float | None) -> str:
        if value is None:
            return GRAY
        status = self._impedance_status(value)
        if status == "good":
            return GREEN
        if status == "ok":
            return YELLOW
        return RED

    def _impedance_color_for_name(self, name: str) -> str:
        return self._impedance_color(self.last_impedance_values.get(name))

    def _status(self, value: float, unit: str) -> str:
        if unit == "kΩ":
            good, ok = self._thresholds()
            if value < good:
                return "good"
            if value <= ok:
                return "ok"
            return "bad"
        # EEG mode: colour by current absolute amplitude, not impedance quality.
        av = abs(value)
        if av < 100:
            return "normal"
        if av < 250:
            return "large"
        return "very large"

    def _value_color(self, value: float | None, unit: str = "kΩ") -> str:
        # Kept for backward compatibility with older code paths. Topomap colouring
        # must remain impedance-only, so non-impedance values are shown as unknown.
        if unit != "kΩ":
            return GRAY
        return self._impedance_color(value)


    def clear_signal_buffers(self):
        self.signal_buffers.clear()
        self.signal_names = []
        self.filter_state.clear()
        if hasattr(self, "signal_canvas"):
            self.redraw_signal()

    def reset_display_filter(self):
        """Reset display-only filter state after changing filter settings."""
        self.filter_state.clear()
        self.filter_signature = self._current_filter_signature()
        self.log_msg("Display filter settings applied; signal viewer buffer cleared.")
        self.clear_signal_buffers()

    def _update_signal_block(self, names, samples, unit: str = "µV"):
        """Append a block of EEG samples to the lightweight Tkinter signal viewer.

        samples is expected to be a list of rows: samples x channels.
        """
        if unit != "µV":
            return
        if not samples:
            return
        self.signal_names = list(names)
        sig = self._current_filter_signature()
        if sig != self.filter_signature:
            self.filter_state.clear()
            self.signal_buffers.clear()
            self.filter_signature = sig

        for name in self.signal_names:
            if name not in self.signal_buffers:
                self.signal_buffers[name] = deque(maxlen=self.signal_buffer_maxlen)

        filtered_rows = self._filter_samples_for_display(self.signal_names, samples)
        for row in filtered_rows:
            for name, value in zip(self.signal_names, row):
                try:
                    self.signal_buffers[name].append(float(value))
                except Exception:
                    pass
        now = getattr(__import__("time"), "time")()
        if now - self.last_signal_redraw_time >= self.signal_redraw_interval_s:
            self.last_signal_redraw_time = now
            self.redraw_signal()

    def _current_filter_signature(self):
        if not hasattr(self, "filter_enabled_var") or not self.filter_enabled_var.get():
            return (False,)
        try:
            fs = float(self.sampling_rate_var.get())
        except Exception:
            fs = 512.0
        return (
            True,
            round(fs, 6),
            self.hp_var.get().strip(),
            self.lp_var.get().strip(),
            self.notch_var.get().strip(),
        )

    def _parse_filter_settings(self):
        """Return display-filter settings. Invalid/empty values disable that stage."""
        try:
            fs = float(self.sampling_rate_var.get())
        except Exception:
            fs = 512.0
        fs = max(1.0, fs)

        def parse_freq(text: str) -> float | None:
            text = text.strip().replace(",", ".")
            if not text or text.lower() in {"off", "none", "0"}:
                return None
            try:
                f = float(text)
            except Exception:
                return None
            if f <= 0 or f >= fs / 2:
                return None
            return f

        hp = parse_freq(self.hp_var.get())
        lp = parse_freq(self.lp_var.get())
        notch_text = self.notch_var.get().strip()
        notch = 50.0 if notch_text.startswith("50") else 60.0 if notch_text.startswith("60") else None
        if notch is not None and notch >= fs / 2:
            notch = None
        return fs, hp, lp, notch

    def _filter_samples_for_display(self, names, samples):
        """Apply simple causal IIR filters to the signal viewer only.

        The LSL outlet still receives unfiltered EEG data in workers.py. This is intentionally
        display-only so the recorded stream remains clean and analysis-ready.
        """
        if not hasattr(self, "filter_enabled_var") or not self.filter_enabled_var.get():
            return samples
        fs, hp, lp, notch = self._parse_filter_settings()
        stages = []
        if hp is not None:
            stages.append(("hp", self._biquad_highpass(fs, hp, q=0.707)))
        if lp is not None:
            stages.append(("lp", self._biquad_lowpass(fs, lp, q=0.707)))
        if notch is not None:
            stages.append((f"notch{int(notch)}", self._biquad_notch(fs, notch, q=30.0)))
        if not stages:
            return samples

        out_rows = []
        for row in samples:
            out = []
            for name, value in zip(names, row):
                y = float(value)
                for stage_name, coeffs in stages:
                    y = self._apply_biquad(name, stage_name, y, coeffs)
                out.append(y)
            out_rows.append(out)
        return out_rows

    def _apply_biquad(self, channel: str, stage: str, x: float, coeffs) -> float:
        b0, b1, b2, a1, a2 = coeffs
        key = (channel, stage)
        x1, x2, y1, y2 = self.filter_state.get(key, (0.0, 0.0, 0.0, 0.0))
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        self.filter_state[key] = (x, x1, y, y1)
        return y

    def _biquad_lowpass(self, fs: float, fc: float, q: float = 0.707):
        w0 = 2.0 * math.pi * fc / fs
        cos_w0 = math.cos(w0)
        alpha = math.sin(w0) / (2.0 * q)
        b0 = (1.0 - cos_w0) / 2.0
        b1 = 1.0 - cos_w0
        b2 = (1.0 - cos_w0) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha
        return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0

    def _biquad_highpass(self, fs: float, fc: float, q: float = 0.707):
        w0 = 2.0 * math.pi * fc / fs
        cos_w0 = math.cos(w0)
        alpha = math.sin(w0) / (2.0 * q)
        b0 = (1.0 + cos_w0) / 2.0
        b1 = -(1.0 + cos_w0)
        b2 = (1.0 + cos_w0) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha
        return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0

    def _biquad_notch(self, fs: float, fc: float, q: float = 30.0):
        w0 = 2.0 * math.pi * fc / fs
        cos_w0 = math.cos(w0)
        alpha = math.sin(w0) / (2.0 * q)
        b0 = 1.0
        b1 = -2.0 * cos_w0
        b2 = 1.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha
        return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0

    def _on_signal_mousewheel(self, event):
        if hasattr(self, "signal_canvas"):
            self.signal_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def redraw_signal(self):
        if not hasattr(self, "signal_canvas"):
            return
        c = self.signal_canvas
        try:
            previous_y = c.yview()[0]
        except Exception:
            previous_y = 0.0
        c.delete("all")
        c.configure(scrollregion=(0, 0, max(c.winfo_width(), 480), max(c.winfo_height(), 300)))
        w = max(c.winfo_width(), 480)
        view_h = max(c.winfo_height(), 300)
        h = view_h
        pad_l, pad_r, pad_t, pad_b = 72, 16, 22, 24
        plot_w = max(10, w - pad_l - pad_r)
        plot_h = max(10, h - pad_t - pad_b)

        filter_label = "filtered display" if hasattr(self, "filter_enabled_var") and self.filter_enabled_var.get() else "raw display"
        c.create_text(12, 10, anchor=tk.NW, text=f"Live EEG signal ({filter_label})", fill=DARK, font=("Segoe UI", 11, "bold"))
        if self.selected_mode() != "eeg":
            c.create_text(
                w / 2,
                h / 2,
                text="Select 'EEG activity (µV)' and press 'Start LSL Stream' to view live signals.",
                fill="#71808a",
                font=("Segoe UI", 11),
            )
            return
        if not self.signal_buffers:
            c.create_text(w / 2, h / 2, text="Waiting for EEG samples…", fill="#71808a", font=("Segoe UI", 11))
            return

        try:
            n_show = int(self.signal_count_var.get())
        except Exception:
            n_show = 8
        try:
            scale_uv = max(1.0, float(self.signal_scale_var.get()))
        except Exception:
            scale_uv = 100.0

        n_show = max(1, min(64, n_show))
        names = [n for n in self.signal_names if n in self.signal_buffers and len(self.signal_buffers[n]) > 1][:n_show]
        if not names:
            return

        # Keep traces readable when many channels are shown; the canvas becomes scrollable up to 64 channels.
        min_row_h = 22 if len(names) > 32 else 28
        row_h = max(min_row_h, plot_h / len(names))
        plot_h = row_h * len(names)
        h = int(pad_t + plot_h + pad_b)
        c.configure(scrollregion=(0, 0, w, h))
        amp_px = row_h * 0.35

        # Grid and zero-lines.
        for i, name in enumerate(names):
            y0 = pad_t + row_h * (i + 0.5)
            c.create_line(pad_l, y0, w - pad_r, y0, fill="#e6ebef")
            c.create_text(8, y0, anchor=tk.W, text=name, fill=DARK, font=("Segoe UI", 9))

            data = list(self.signal_buffers[name])[-self.signal_buffer_maxlen:]
            if len(data) < 2:
                continue
            step = plot_w / max(1, len(data) - 1)
            points = []
            for j, v in enumerate(data):
                x = pad_l + j * step
                # Clamp large artefacts so a single bad sample does not destroy the display.
                vv = max(-scale_uv * 3, min(scale_uv * 3, v))
                y = y0 - (vv / scale_uv) * amp_px
                points.extend((x, y))
            if len(points) >= 4:
                c.create_line(*points, fill="#2f5f8f", width=1.2, smooth=False)

        filter_footer = "viewer filter on" if hasattr(self, "filter_enabled_var") and self.filter_enabled_var.get() else "viewer filter off"
        c.create_text(
            w - pad_r,
            h - 8,
            anchor=tk.SE,
            text=f"Showing {len(names)} channel(s), ±{scale_uv:.0f} µV/div, {filter_footer}",
            fill="#71808a",
            font=("Segoe UI", 8),
        )
        # Redrawing at live-update speed can otherwise snap the scrollbar back
        # to the top. Preserve the user's current scroll position unless they
        # explicitly enable auto-follow.
        try:
            if hasattr(self, "signal_autoscroll_var") and self.signal_autoscroll_var.get():
                c.yview_moveto(1.0)
            else:
                c.yview_moveto(previous_y)
        except Exception:
            pass


def main():
    if "--add-firewall-rule" in sys.argv:
        # This path is used after the app relaunches itself with administrator
        # permission. No full GUI is needed; only a small hidden Tk root for
        # error/information dialogs.
        root = tk.Tk()
        root.withdraw()
        result = add_firewall_rules()
        if result.ok:
            messagebox.showinfo("Windows Firewall", result.message, parent=root)
        else:
            messagebox.showerror("Windows Firewall", result.message, parent=root)
        root.destroy()
        return

    app = EegoLslApp()
    app.mainloop()


if __name__ == "__main__":
    main()
