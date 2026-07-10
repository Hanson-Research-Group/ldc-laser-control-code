#!/usr/bin/env python3
"""
Newport LDC-3908 Modular Laser Diode Controller Software
A production-grade graphical desktop control suite ported from MATLAB.
Developed by Zev Granowitz in collaboration with .
"""

import sys
import os
import json
import math
import time
import threading
import serial
import serial.tools.list_ports
import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox

from laser_controller import LaserController
from sequencer import Sequencer, SequenceEvents, ChannelPlan
import theme

import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# Set appearance mode and color theme
ctk.set_appearance_mode("Light")
ctk.set_default_color_theme("blue")

def resolve_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller."""
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        # main.py is in src/, so parent directory of os.path.dirname(__file__) is root
        base_path = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(base_path, relative_path)

class LEDIndicator(tk.Canvas):
    """A custom Tkinter widget to draw a beautiful, smooth status LED.

    The LED lives on a bare tk.Canvas, which CustomTkinter does not theme, so its
    background is resolved from the parent frame (or the theme's frame color when
    the parent is transparent) for the active appearance mode — and can be
    refreshed on a light/dark toggle via refresh_theme()."""
    def __init__(self, parent, size=18, color="#cccccc", **kwargs):
        super().__init__(parent, width=size, height=size,
                         bg=self._resolve_bg(parent), highlightthickness=0, **kwargs)
        self.size = size
        self.color = color
        self.draw_circle()

    @staticmethod
    def _resolve_bg(parent):
        mode_idx = 0 if ctk.get_appearance_mode().lower() == "light" else 1
        try:
            parent_fg = parent.cget("fg_color")
            if isinstance(parent_fg, (list, tuple)):
                return parent_fg[mode_idx]
            if parent_fg and parent_fg != "transparent":
                return parent_fg
        except Exception:
            pass
        # Transparent/unknown parent: fall back to the theme's frame color.
        return theme.frame_bg()

    def draw_circle(self):
        self.delete("all")
        self.create_oval(2, 2, self.size - 2, self.size - 2,
                         fill=self.color, outline=self.color, width=1.0)

    def set_color(self, color):
        self.color = color
        self.draw_circle()

    def refresh_theme(self):
        """Re-resolve the canvas background for the current appearance mode."""
        try:
            self.configure(bg=self._resolve_bg(self.master))
            self.draw_circle()
        except Exception:
            pass


class _TkSequenceEvents(SequenceEvents):
    """Bridges the UI-agnostic Sequencer to the Tk GUI. The sequencer runs on a
    worker thread, so every callback is marshalled onto the Tk main thread via
    app.after(...) — the same pattern the ramp code used inline before extraction."""

    def __init__(self, app):
        self.app = app

    def on_status(self, idx, text, kind):
        # Map the engine's semantic status kind to a themed (light, dark) color.
        self.app.after(0, self.app.update_channel_status, idx, text, theme.status(kind))

    def on_led(self, idx, kind):
        self.app.after(0, self.app.ch_ui[idx]["led"].set_color, theme.led(kind))

    def on_live_output(self, idx, kind, state):
        self.app.after(0, self.app.update_live_out_ui, idx, kind, state)

    def on_live_value(self, idx, kind, value):
        widget = self.app.ch_ui[idx]["cur_t" if kind == "T" else "cur_i"]
        self.app.after(0, set_entry_val, widget, f"{value:.1f}")

    def on_tick(self):
        # update_eta() itself schedules the label update via after(), so it is
        # safe to call directly from the worker thread.
        self.app.update_eta()

    def on_channel_halted(self, idx):
        self.app.after(0, self.app._handle_channel_halted, idx)

    def on_channel_fault(self, idx, message):
        self.app.after(0, self.app._handle_channel_fault, idx, message)


class LDCControllerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # --- Configure Main Window ---
        self.title("LDC-3908 Modular Laser Diode Controller Software v0.3.1")
        self.geometry("1850x950")
        # Low minimum so the app is usable on laptops and at half/quarter of a
        # large display; the channel cards reflow to fewer columns as it shrinks.
        self.minsize(900, 650)
        
        # Set window icon
        try:
            icon_ico = resolve_path("src/laser_controller_icon.ico")
            icon_png = resolve_path("src/laser_controller_icon.png")
            if os.path.exists(icon_ico):
                self.iconbitmap(icon_ico)
            elif os.path.exists(icon_png):
                self.iconphoto(False, tk.PhotoImage(file=icon_png))
        except:
            pass

        self.num_channels = 8

        # --- Control Core ---
        # All hardware I/O, the Demo Simulator, and the shared control flags live
        # in the UI-agnostic LaserController (laser_controller.py). The GUI accesses
        # them through the proxy properties / delegator methods defined below.
        self.ctl = LaserController(num_channels=self.num_channels)

        # Ramp / sequence engine (also UI-agnostic). It drives self.ctl and reports
        # back through the Tk event adapter, which marshals every update onto the
        # main thread — replacing the old direct self.after(...) calls scattered
        # through the ramp code.
        self.seq = Sequencer(self.ctl, events=_TkSequenceEvents(self))

        # --- Application (UI) State Variables ---
        self.is_executing = False
        self.is_scanning = False
        self.active_profile_path = ""
        self.total_estimated_time = 0.0
        self.sequence_start_time = None
        self.is_closing = False

        # Telemetry control
        self.telemetry_thread = None
        self.telemetry_active = False
        self.telemetry_fail_count = 0

        # --- Build GUI Layout ---
        self.setup_layout()

        # --- Load Last Profile Preference ---
        self.load_last_profile_preference()

        # --- Handle Application Closing Gracefully ---
        self.protocol("WM_DELETE_WINDOW", self.close_app)

    # ----------------------------------------------------
    # CONTROL-CORE PROXIES
    # ----------------------------------------------------
    # Compatibility shims so the GUI can keep referring to self.ser /
    # self.is_stop_requested / self.send_cmd(...) etc. while the hardware state and
    # protocol I/O actually live in the extracted LaserController (self.ctl). As
    # the scan / telemetry / ramp / sequence logic migrates into the core in later
    # Phase-0 sub-steps, these proxies and delegators will be removed.
    @property
    def ser(self):
        return self.ctl.ser

    @ser.setter
    def ser(self, value):
        self.ctl.ser = value

    @property
    def serial_lock(self):
        return self.ctl.serial_lock

    @property
    def is_simulated(self):
        return self.ctl.is_simulated

    @is_simulated.setter
    def is_simulated(self, value):
        self.ctl.is_simulated = value

    @property
    def is_stop_requested(self):
        return self.ctl.is_stop_requested

    @is_stop_requested.setter
    def is_stop_requested(self, value):
        self.ctl.is_stop_requested = value

    @property
    def is_emo_requested(self):
        return self.ctl.is_emo_requested

    @is_emo_requested.setter
    def is_emo_requested(self, value):
        self.ctl.is_emo_requested = value

    def send_cmd(self, cmd):
        self.ctl.send_cmd(cmd)

    def read_cmd(self):
        return self.ctl.read_cmd()

    def query_cmd(self, cmd):
        return self.ctl.query_cmd(cmd)

    def cmd_pause(self, cmd):
        self.ctl.cmd_pause(cmd)

    def safe_pause(self, t):
        self.ctl.safe_pause(t)

    def verify_hw_state(self, cmd, expected_val, err_msg):
        self.ctl.verify_hw_state(cmd, expected_val, err_msg)

    def check_controller_errors_threadsafe(self, ch_num):
        return self.ctl.check_controller_errors(ch_num)

    def setup_layout(self):
        # Configure root grid weights for responsiveness
        self.grid_rowconfigure(0, weight=0)  # Top panel
        self.grid_rowconfigure(1, weight=1)  # Channels panel
        self.grid_rowconfigure(2, weight=0)  # Bottom panel
        self.grid_columnconfigure(0, weight=1)

        # ----------------------------------------------------
        # 1. TOP PANEL: CONNECTION
        # ----------------------------------------------------
        top_frame = ctk.CTkFrame(self, height=60)
        top_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=5)
        top_frame.grid_columnconfigure(5, weight=1)  # Spacer column

        ctk.CTkLabel(top_frame, text="COM Port:", font=("Segoe UI", 15, "bold")).grid(row=0, column=0, padx=(10, 5), pady=10, sticky="w")

        # Scan system COM ports
        self.avail_ports = [port.device for port in serial.tools.list_ports.comports()]
        self.avail_ports.append("Demo Simulator")
        
        self.com_dropdown = ctk.CTkOptionMenu(top_frame, values=self.avail_ports, width=203)
        self.com_dropdown.grid(row=0, column=1, padx=(5, 0), pady=10)
        
        self.btn_refresh = ctk.CTkButton(top_frame, text="↻", width=41, command=self.refresh_ports)
        self.btn_refresh.grid(row=0, column=2, padx=(2, 5), pady=10)

        # P1 #5: Prefer real hardware port first (matches MATLAB behaviour);
        # only fall back to Demo Simulator when no physical ports exist.
        real_ports = [p for p in self.avail_ports if p != "Demo Simulator"]
        if real_ports:
            self.com_dropdown.set(real_ports[0])
        else:
            self.com_dropdown.set("Demo Simulator")

        self.btn_connect = ctk.CTkButton(top_frame, text="Connect", width=135, command=self.connect_serial)
        self.btn_connect.grid(row=0, column=3, padx=5, pady=10)

        self.btn_scan = ctk.CTkButton(top_frame, text="Scan Channels", width=162, state="disabled", command=self.start_channel_scan)
        self.btn_scan.grid(row=0, column=4, padx=5, pady=10)

        self.btn_clear_faults = ctk.CTkButton(top_frame, text="Clear Faults", width=135, fg_color="transparent", text_color=theme.status("fault"),
                                               hover_color="#ffebee", state="disabled", command=self.clear_faults)
        self.btn_clear_faults.grid(row=0, column=5, padx=5, pady=10)

        # Appearance (light/dark) toggle — user-selectable at runtime.
        self.appearance_toggle = ctk.CTkSegmentedButton(
            top_frame, values=["Light", "Dark"], width=140, command=self.set_appearance)
        self.appearance_toggle.set(ctk.get_appearance_mode())
        self.appearance_toggle.grid(row=0, column=6, padx=(10, 5), pady=10, sticky="e")

        self.status_label = ctk.CTkLabel(top_frame, text="Status: Disconnected", text_color=theme.status("fault"), font=("Segoe UI", 16, "bold"))
        self.status_label.grid(row=0, column=7, padx=(10, 20), pady=10, sticky="e")

        # ----------------------------------------------------
        # 2. MIDDLE PANEL: CHANNEL CARDS (responsive, reflowing)
        # ----------------------------------------------------
        chan_panel = ctk.CTkFrame(self)
        chan_panel.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        chan_panel.grid_rowconfigure(1, weight=1)
        chan_panel.grid_columnconfigure(0, weight=1)

        # --- Controls bar: title, show-unused switch, master overrides ---
        bar = ctk.CTkFrame(chan_panel, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))
        bar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(bar, text="Channel Configuration & Live Telemetry",
                     font=("Segoe UI", 16, "bold")).grid(row=0, column=0, sticky="w")

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.grid(row=0, column=2, sticky="e")

        self.view_toggle = ctk.CTkSegmentedButton(right, values=["Table", "Cards"],
                                                  width=140, command=self._set_view_mode)
        self.view_toggle.set("Table")
        self.view_toggle.pack(side="left", padx=(0, 14))

        self.show_unused_var = tk.BooleanVar(value=False)
        self.chk_show_unused = ctk.CTkSwitch(right, text="Show unused", variable=self.show_unused_var,
                                             command=lambda: self._reflow_cards(force=True))
        self.chk_show_unused.pack(side="left", padx=(0, 14))

        self.btn_master_on = ctk.CTkButton(right, text="All ON", width=78, font=("Segoe UI", 13, "bold"),
                                           fg_color=theme.GREEN, hover_color=theme.GREEN_HOVER, text_color="white",
                                           state="disabled", command=lambda: self.set_all_systems("ON"))
        self.btn_master_on.pack(side="left", padx=3)
        self.btn_master_off = ctk.CTkButton(right, text="All OFF", width=78, font=("Segoe UI", 13, "bold"),
                                            fg_color=theme.RED, hover_color=theme.RED_HOVER, text_color="white",
                                            state="disabled", command=lambda: self.set_all_systems("OFF"))
        self.btn_master_off.pack(side="left", padx=3)
        self.btn_tec_on = ctk.CTkButton(right, text="TEC On", width=72, font=("Segoe UI", 13),
                                        state="disabled", command=lambda: self.set_all_dropdowns("TEC", "ON"))
        self.btn_tec_on.pack(side="left", padx=3)
        self.btn_tec_off = ctk.CTkButton(right, text="TEC Off", width=72, font=("Segoe UI", 13),
                                         state="disabled", command=lambda: self.set_all_dropdowns("TEC", "OFF"))
        self.btn_tec_off.pack(side="left", padx=3)
        self.btn_las_on = ctk.CTkButton(right, text="LAS On", width=72, font=("Segoe UI", 13),
                                        state="disabled", command=lambda: self.set_all_dropdowns("LAS", "ON"))
        self.btn_las_on.pack(side="left", padx=3)
        self.btn_las_off = ctk.CTkButton(right, text="LAS Off", width=72, font=("Segoe UI", 13),
                                         state="disabled", command=lambda: self.set_all_dropdowns("LAS", "OFF"))
        self.btn_las_off.pack(side="left", padx=3)

        # --- Scrollable, reflowing card container ---
        self.cards_container = ctk.CTkScrollableFrame(chan_panel, fg_color="transparent")
        self.cards_container.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)

        # Per-channel state used by the visibility filter.
        self._has_scanned = False
        self.ch_populated = [False] * self.num_channels
        self._reflow_key = None

        self.ch_ui = []
        for i in range(self.num_channels):
            self.ch_ui.append(self._build_channel_card(i))

        # Default to Table view (full-width stacked rows under a shared header).
        self._build_table_header()
        self._table_mode = True
        for ch in self.ch_ui:
            self._layout_table_row(ch)

        # Reflow on window resize; prime once geometry settles.
        self.bind("<Configure>", self._reflow_cards)
        self.after(60, lambda: self._reflow_cards(force=True))

        # ----------------------------------------------------
        # 3. BOTTOM PANEL: PARAMETERS & UTILITIES
        # ----------------------------------------------------
        bot_panel = ctk.CTkFrame(self, height=150)
        bot_panel.grid(row=2, column=0, sticky="ew", padx=10, pady=5)
        
        # Configure layout grids inside bottom panel
        bot_panel.grid_columnconfigure(4, weight=1)  # Spacer column
        bot_panel.grid_rowconfigure(0, weight=1)
        bot_panel.grid_rowconfigure(1, weight=1)
        bot_panel.grid_rowconfigure(2, weight=1)

        # Row 1: Global Sequence parameters labels & entry fields
        ctk.CTkLabel(bot_panel, text="T Ramp (°C/s):", font=("Segoe UI", 14, "bold")).grid(row=0, column=0, padx=(15, 5), pady=5, sticky="w")
        self.t_ramp_edit = ctk.CTkEntry(bot_panel, width=122)
        self.t_ramp_edit.insert(0, "0.1")
        self.t_ramp_edit.grid(row=0, column=1, padx=5, pady=5, sticky="w")
        self.t_ramp_edit.bind("<KeyRelease>", self.mark_profile_unsaved)
        self.t_ramp_edit.bind("<FocusOut>", lambda e: self._validate_numeric_entry(self.t_ramp_edit, min_val=0.0001))

        ctk.CTkLabel(bot_panel, text="I Ramp (mA/s):", font=("Segoe UI", 14, "bold")).grid(row=1, column=0, padx=(15, 5), pady=5, sticky="w")
        self.i_ramp_edit = ctk.CTkEntry(bot_panel, width=122)
        self.i_ramp_edit.insert(0, "0.5")
        self.i_ramp_edit.grid(row=1, column=1, padx=5, pady=5, sticky="w")
        self.i_ramp_edit.bind("<KeyRelease>", self.mark_profile_unsaved)
        self.i_ramp_edit.bind("<FocusOut>", lambda e: self._validate_numeric_entry(self.i_ramp_edit, min_val=0.0001))

        ctk.CTkLabel(bot_panel, text="T OFF Target (°C):", font=("Segoe UI", 14, "bold")).grid(row=0, column=2, padx=(20, 5), pady=5, sticky="w")
        self.t_off_edit = ctk.CTkEntry(bot_panel, width=122)
        self.t_off_edit.insert(0, "22.0")
        self.t_off_edit.grid(row=0, column=3, padx=5, pady=5, sticky="w")
        self.t_off_edit.bind("<KeyRelease>", self.mark_profile_unsaved)
        self.t_off_edit.bind("<FocusOut>", lambda e: self._validate_numeric_entry(self.t_off_edit))

        # Profile configurations actions
        self.btn_save = ctk.CTkButton(bot_panel, text="💾 Save Profile", width=162, command=self.save_config)
        self.btn_save.grid(row=2, column=0, columnspan=2, padx=(15, 5), pady=10, sticky="w")

        self.btn_load = ctk.CTkButton(bot_panel, text="📂 Load Profile", width=162, command=self.load_config)
        self.btn_load.grid(row=2, column=2, columnspan=2, padx=5, pady=10, sticky="w")

        self.btn_clear_profile = ctk.CTkButton(bot_panel, text="❌ Clear Profile", width=162, command=self.clear_profile)
        self.btn_clear_profile.grid(row=2, column=3, padx=5, pady=10, sticky="w")

        self.lbl_profile = ctk.CTkLabel(bot_panel, text="Active Profile: [Unsaved]", font=("Segoe UI", 15, "bold"), text_color="#888888")
        self.lbl_profile.grid(row=2, column=4, padx=15, pady=10, sticky="w")

        # Execution actions on the right side
        self.btn_exec_all = ctk.CTkButton(bot_panel, text="▶ RUN ALL", font=("Segoe UI", 16, "bold"), fg_color="#2e7d32", hover_color="#1b5e20",
                                           text_color="white", state="disabled", width=230, height=60, command=self.execute_all)
        self.btn_exec_all.grid(row=0, column=6, rowspan=2, padx=10, pady=10, sticky="nsew")

        self.btn_stop = ctk.CTkButton(bot_panel, text="⏹ CANCEL RUN (Safe)", font=("Segoe UI", 15, "bold"), fg_color="#c62828", hover_color="#b71c1c",
                                       text_color="white", state="disabled", width=230, command=self.stop_execution)
        self.btn_stop.grid(row=2, column=6, padx=10, pady=10, sticky="nsew")

        # Emergency Laser Off
        self.btn_emerg = ctk.CTkButton(bot_panel, text="⚠\nEMO\nOFF", font=("Segoe UI", 13, "bold"), fg_color="#c62828", hover_color="#b71c1c",
                                        text_color="white", state="disabled", width=81, command=self.emergency_las_off)
        self.btn_emerg.grid(row=0, column=5, rowspan=3, padx=10, pady=10, sticky="nsew")

    # ----------------------------------------------------
    # CHANNEL CARD LAYOUT (responsive reflow)
    # ----------------------------------------------------
    # Column spec for Table view (and its header): (title, min logical width, weight).
    # Index == grid column. The Status column stretches to absorb extra width.
    _TABLE_COLS = [
        ("Ch", 44, 0), ("En", 30, 0), ("Label", 120, 0), ("", 24, 0), ("Status", 150, 1),
        ("Live TEC", 62, 0), ("Live LAS", 62, 0), ("Live T", 62, 0), ("Live I", 62, 0),
        ("TEC", 62, 0), ("LAS", 62, 0), ("Tgt T", 58, 0), ("Max", 40, 0),
        ("Tgt I", 58, 0), ("Max", 40, 0), ("Action", 92, 0),
    ]

    def _build_channel_card(self, i):
        """Create one channel's widgets inside a card frame. Returns the ch_ui dict
        with the SAME keys the scan/telemetry/sequence code expects, plus 'card' (the
        frame) and '_caps' (compact-view caption labels). The widgets are NOT gridded
        here — _layout_compact()/_layout_table_row() arrange them per the view mode."""
        ch_idx = i + 1
        card = ctk.CTkFrame(self.cards_container, corner_radius=8, border_width=1,
                            border_color=("#d0d0d0", "#3f3f3f"))
        muted = theme.status("muted")

        caps = []  # (widget, compact_col, compact_row, sticky) — shown only in card view

        def cap(text, col, row, sticky="s"):
            lbl = ctk.CTkLabel(card, text=text, font=("Segoe UI", 11), text_color=muted)
            caps.append((lbl, col, row, sticky))
            return lbl

        lbl_ch = ctk.CTkLabel(card, text=f"Ch {ch_idx}", font=("Segoe UI", 15, "bold"))
        ent_label = ctk.CTkEntry(card, placeholder_text=f"Laser {ch_idx}", state="disabled")
        ent_label.insert(0, f"Laser {ch_idx}")
        ent_label.bind("<KeyRelease>", self.mark_profile_unsaved)
        var_enable = tk.BooleanVar(value=False)
        chk_enable = ctk.CTkCheckBox(card, text="", width=24, variable=var_enable,
                                     state="disabled", command=self._on_enable_toggle)
        led = LEDIndicator(card, size=18, color=theme.led("idle"))
        lbl_status = ctk.CTkLabel(card, text="Run Scan First", text_color=muted,
                                  font=("Segoe UI", 14), anchor="w")

        cap("Live TEC", 0, 2); cap("Live LAS", 1, 2); cap("Live T °C", 2, 2); cap("Live I mA", 3, 2)
        ent_live_tec = ctk.CTkEntry(card, width=72, state="disabled", font=("Segoe UI", 13, "bold"),
                                    justify="center", fg_color=theme.LIVE_IDLE_BG, text_color=theme.LIVE_IDLE_TEXT)
        set_entry_val(ent_live_tec, "OFF")
        ent_live_las = ctk.CTkEntry(card, width=72, state="disabled", font=("Segoe UI", 13, "bold"),
                                    justify="center", fg_color=theme.LIVE_IDLE_BG, text_color=theme.LIVE_IDLE_TEXT)
        set_entry_val(ent_live_las, "OFF")
        ent_live_t = ctk.CTkEntry(card, width=72, state="disabled", font=("Segoe UI", 16), justify="right")
        set_entry_val(ent_live_t, "0.0")
        ent_live_i = ctk.CTkEntry(card, width=72, state="disabled", font=("Segoe UI", 16), justify="right")
        set_entry_val(ent_live_i, "0.0")

        cap("TEC", 0, 4); cap("LAS", 1, 4); cap("Target T", 2, 4); cap("Target I", 3, 4)
        opt_tec = ctk.CTkOptionMenu(card, values=["ON", "OFF"], width=72, state="disabled",
                                    command=self.mark_profile_unsaved)
        opt_tec.set("OFF")
        opt_las = ctk.CTkOptionMenu(card, values=["ON", "OFF"], width=72, state="disabled",
                                    command=self.mark_profile_unsaved)
        opt_las.set("OFF")
        ent_target_t = ctk.CTkEntry(card, width=72, state="disabled", justify="right")
        ent_target_t.insert(0, "22.0")
        ent_target_t.bind("<KeyRelease>", self.mark_profile_unsaved)
        ent_target_t.bind("<FocusOut>", lambda e, w=ent_target_t: self._validate_numeric_entry(w))
        ent_target_i = ctk.CTkEntry(card, width=72, state="disabled", justify="right")
        ent_target_i.insert(0, "0.0")
        ent_target_i.bind("<KeyRelease>", self.mark_profile_unsaved)
        ent_target_i.bind("<FocusOut>", lambda e, w=ent_target_i: self._validate_numeric_entry(w))

        cap("Max limit:", 1, 6, sticky="e")
        lbl_max_t = ctk.CTkLabel(card, text="-", font=("Segoe UI", 13), text_color=muted)
        lbl_max_i = ctk.CTkLabel(card, text="-", font=("Segoe UI", 13), text_color=muted)

        btn_run_ch = ctk.CTkButton(card, text="▶ Run Ch.", width=92, state="disabled",
                                   command=lambda ch=ch_idx: self.execute_single_channel(ch))

        return {
            'card': card, '_caps': caps,
            'label_num': lbl_ch, 'enable_var': var_enable, 'enable_chk': chk_enable,
            'laser_label': ent_label, 'led': led, 'status': lbl_status,
            'live_tec': ent_live_tec, 'live_las': ent_live_las,
            'cur_t': ent_live_t, 'cur_i': ent_live_i,
            'tec_cmd': opt_tec, 'las_cmd': opt_las,
            't_target': ent_target_t, 't_lim': lbl_max_t,
            'i_target': ent_target_i, 'i_lim': lbl_max_i, 'btn_exec': btn_run_ch,
        }

    def _clear_card_grid(self, card):
        for w in card.grid_slaves():
            w.grid_forget()

    def _layout_compact(self, ch):
        """Card view: a compact block, 4 internal columns."""
        card = ch['card']
        self._clear_card_grid(card)
        for c in range(len(self._TABLE_COLS)):
            card.grid_columnconfigure(c, weight=0, minsize=0)
        for c in range(4):
            card.grid_columnconfigure(c, weight=1, minsize=0)

        ch['label_num'].grid(row=0, column=0, padx=(10, 4), pady=(8, 2), sticky="w")
        ch['laser_label'].grid(row=0, column=1, columnspan=2, padx=4, pady=(8, 2), sticky="ew")
        ch['enable_chk'].grid(row=0, column=3, padx=(4, 10), pady=(8, 2), sticky="e")
        ch['led'].grid(row=1, column=0, padx=(10, 4), pady=2, sticky="w")
        ch['status'].grid(row=1, column=1, columnspan=3, padx=4, pady=2, sticky="ew")
        for lbl, col, row, sticky in ch['_caps']:
            lbl.grid(row=row, column=col, padx=2, pady=(6, 0), sticky=sticky)
        ch['live_tec'].grid(row=3, column=0, padx=2, pady=2, sticky="ew")
        ch['live_las'].grid(row=3, column=1, padx=2, pady=2, sticky="ew")
        ch['cur_t'].grid(row=3, column=2, padx=2, pady=2, sticky="ew")
        ch['cur_i'].grid(row=3, column=3, padx=2, pady=2, sticky="ew")
        ch['tec_cmd'].grid(row=5, column=0, padx=2, pady=2, sticky="ew")
        ch['las_cmd'].grid(row=5, column=1, padx=2, pady=2, sticky="ew")
        ch['t_target'].grid(row=5, column=2, padx=2, pady=2, sticky="ew")
        ch['i_target'].grid(row=5, column=3, padx=2, pady=2, sticky="ew")
        ch['t_lim'].grid(row=6, column=2, padx=2, pady=(0, 2))
        ch['i_lim'].grid(row=6, column=3, padx=2, pady=(0, 2))
        ch['btn_exec'].grid(row=7, column=0, columnspan=4, padx=10, pady=(6, 10), sticky="ew")

    def _layout_table_row(self, ch):
        """Table view: one full-width horizontal row, columns aligned with the header."""
        card = ch['card']
        self._clear_card_grid(card)
        for c, (_, minsize, weight) in enumerate(self._TABLE_COLS):
            card.grid_columnconfigure(c, minsize=minsize, weight=weight)

        order = ['label_num', 'enable_chk', 'laser_label', 'led', 'status',
                 'live_tec', 'live_las', 'cur_t', 'cur_i', 'tec_cmd', 'las_cmd',
                 't_target', 't_lim', 'i_target', 'i_lim', 'btn_exec']
        for col, key in enumerate(order):
            sticky = "w" if key in ('label_num', 'status') else "ew"
            ch[key].grid(row=0, column=col, padx=2, pady=4, sticky=sticky)

    def _build_table_header(self):
        """A single header row shown above the stacked channel rows in Table view."""
        self.table_header = ctk.CTkFrame(self.cards_container, fg_color="transparent")
        for c, (title, minsize, weight) in enumerate(self._TABLE_COLS):
            self.table_header.grid_columnconfigure(c, minsize=minsize, weight=weight)
            ctk.CTkLabel(self.table_header, text=title, font=("Segoe UI", 12, "bold"),
                         anchor="w").grid(row=0, column=c, padx=2, pady=(2, 2), sticky="w")

    def _set_view_mode(self, mode):
        """Switch between 'Table' (full-width stacked rows) and 'Cards' (compact grid)."""
        self._table_mode = (mode == "Table")
        for ch in self.ch_ui:
            (self._layout_table_row if self._table_mode else self._layout_compact)(ch)
        self._reflow_cards(force=True)

    def _shown_indices(self):
        """Which channel cards should be visible right now. Before the first scan,
        or when 'Show unused' is on, show all. Otherwise show only populated +
        enabled channels (hiding empty slots, no-laser cards, and disabled ones)."""
        if self.show_unused_var.get() or not self._has_scanned:
            return list(range(self.num_channels))
        return [i for i in range(self.num_channels)
                if self.ch_populated[i] and self.ch_ui[i]['enable_var'].get()]

    def _reflow_cards(self, event=None, force=False):
        """Place the visible channel cards. In Table view they stack full-width under
        a shared header; in Cards view they reflow into as many columns as fit the
        current width (down to one on a laptop / quarter-screen window)."""
        if not getattr(self, "ch_ui", None):
            return
        shown = self._shown_indices()

        if getattr(self, "_table_mode", True):
            key = ("table", tuple(shown))
            if not force and key == self._reflow_key:
                return
            self._reflow_key = key
            for c in range(len(self._TABLE_COLS)):
                self.cards_container.grid_columnconfigure(c, weight=(1 if c == 0 else 0), minsize=0)
            for i in range(self.num_channels):
                self.ch_ui[i]['card'].grid_forget()
            self.table_header.grid(row=0, column=0, sticky="ew", padx=8, pady=(2, 0))
            for pos, i in enumerate(shown):
                self.ch_ui[i]['card'].grid(row=pos + 1, column=0, padx=8, pady=3, sticky="ew")
            return

        # Cards view: compute columns from the viewport width (physical px -> logical
        # via the HiDPI widget-scaling factor).
        self.table_header.grid_forget()
        canvas = getattr(self.cards_container, "_parent_canvas", None)
        px_width = canvas.winfo_width() if canvas is not None else self.cards_container.winfo_width()
        if px_width <= 1:
            self.after(100, lambda: self._reflow_cards(force=True))
            return
        try:
            scaling = ctk.ScalingTracker.get_widget_scaling(self.cards_container)
        except Exception:
            scaling = 1.0
        width = px_width / max(scaling, 0.1)

        CARD_W = 360
        cols = max(1, min(int(width // CARD_W), max(1, len(shown))))
        key = ("cards", cols, tuple(shown))
        if not force and key == self._reflow_key:
            return
        self._reflow_key = key

        for c in range(max(cols, len(self._TABLE_COLS))):
            self.cards_container.grid_columnconfigure(c, weight=(1 if c < cols else 0), minsize=0)
        for i in range(self.num_channels):
            self.ch_ui[i]['card'].grid_forget()
        for pos, i in enumerate(shown):
            r, c = divmod(pos, cols)
            self.ch_ui[i]['card'].grid(row=r, column=c, padx=6, pady=6, sticky="nsew")

    def _on_enable_toggle(self):
        """User toggled a channel's Enable box: re-evaluate card visibility."""
        self._reflow_cards(force=True)

    # ----------------------------------------------------
    # INPUT VALIDATION HELPERS
    # ----------------------------------------------------
    def _validate_numeric_entry(self, widget, min_val=None, max_val=None):
        """Visual validation for numeric CTkEntry fields. Highlights red on invalid input."""
        raw = widget.get().strip()
        valid = True
        try:
            val = float(raw)
            if min_val is not None and val <= min_val - 1e-9:
                valid = False
            if max_val is not None and val > max_val:
                valid = False
        except ValueError:
            valid = False

        if valid:
            widget.configure(border_color=("#979da2", "#565b5e"))  # default
        else:
            widget.configure(border_color="#c62828")  # red highlight
        return valid

    # ----------------------------------------------------
    # PROFILE SETTING HELPERS
    # ----------------------------------------------------
    def mark_profile_unsaved(self, *args):
        text = self.lbl_profile.cget("text")
        if not text.startswith("* "):
            self.lbl_profile.configure(text=f"* {text}", text_color="#f57c00")

    def refresh_ports(self):
        self.avail_ports = [port.device for port in serial.tools.list_ports.comports()]
        self.avail_ports.append("Demo Simulator")
        self.com_dropdown.configure(values=self.avail_ports)
        real_ports = [p for p in self.avail_ports if p != "Demo Simulator"]
        if real_ports:
            self.com_dropdown.set(real_ports[0])
        else:
            self.com_dropdown.set("Demo Simulator")

    def set_appearance(self, mode):
        """Switch the light/dark theme at runtime. CustomTkinter widgets defined
        with (light, dark) colors auto-switch; the custom LED canvases, which CTk
        does not theme, are refreshed manually."""
        ctk.set_appearance_mode(mode)
        for ch in self.ch_ui:
            ch["led"].refresh_theme()

    def load_last_profile_preference(self):
        pref_file = os.path.join(os.path.expanduser("~"), ".ldc_laser_control_prefs.json")
        if os.path.exists(pref_file):
            try:
                with open(pref_file, "r") as f:
                    prefs = json.load(f)
                    last_path = prefs.get("LastProfilePath", "")
                    if last_path and os.path.isfile(last_path):
                        self.load_profile_from_file(last_path)
            except Exception as e:
                print(f"Error loading preferences: {e}")

    def save_last_profile_preference(self, path):
        pref_file = os.path.join(os.path.expanduser("~"), ".ldc_laser_control_prefs.json")
        try:
            with open(pref_file, "w") as f:
                json.dump({"LastProfilePath": path}, f)
        except Exception as e:
            print(f"Error saving preferences: {e}")

    def save_config(self):
        file_path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text files", "*.txt")])
        if not file_path:
            return
        
        try:
            config_data = {
                "COM_Port": self.com_dropdown.get(),
                "T_ramp": float(self.t_ramp_edit.get()),
                "I_ramp": float(self.i_ramp_edit.get()),
                "T_OFF_Target": float(self.t_off_edit.get()),
                "channels": []
            }
            for ch in self.ch_ui:
                config_data["channels"].append({
                    "T_Target": float(ch["t_target"].get()),
                    "I_Target": float(ch["i_target"].get()),
                    "Label": ch["laser_label"].get()
                })

            with open(file_path, "w") as f:
                json.dump(config_data, f, indent=2)

            self.active_profile_path = file_path
            self.save_last_profile_preference(file_path)
            name = os.path.basename(file_path)
            self.lbl_profile.configure(text=f"Active Profile: {name}", text_color="#888888")
            
            messagebox.showinfo("Profile Saved", "Hardware configuration profile has been saved successfully.")
        except Exception as e:
            messagebox.showerror("Save Error", f"Failed to save profile:\n{e}")

    def load_config(self):
        file_path = filedialog.askopenfilename(filetypes=[("Text files", "*.txt")])
        if not file_path:
            return
        self.load_profile_from_file(file_path)

    def load_profile_from_file(self, file_path):
        try:
            with open(file_path, "r") as f:
                config_data = json.load(f)

            saved_channels = len(config_data["channels"])
            if saved_channels != self.num_channels:
                msg = f"Profile Mismatch: The profile contains settings for {saved_channels} channels, but the GUI is configured for {self.num_channels} channels.\n\nApply compatible settings anyway?"
                if not messagebox.askyesno("Channel Count Mismatch", msg):
                    return

            if "COM_Port" in config_data:
                com_port = config_data["COM_Port"]
                if com_port not in self.avail_ports:
                    # Update available ports to include the one from the profile if missing
                    self.avail_ports.append(com_port)
                    self.com_dropdown.configure(values=self.avail_ports)
                self.com_dropdown.set(com_port)

            self.t_ramp_edit.delete(0, tk.END)
            self.t_ramp_edit.insert(0, str(config_data["T_ramp"]))
            self.i_ramp_edit.delete(0, tk.END)
            self.i_ramp_edit.insert(0, str(config_data["I_ramp"]))
            self.t_off_edit.delete(0, tk.END)
            self.t_off_edit.insert(0, str(config_data["T_OFF_Target"]))

            apply_count = min(saved_channels, self.num_channels)
            for k in range(apply_count):
                ch = self.ch_ui[k]
                ch_cfg = config_data["channels"][k]
                
                # Update target entries safely
                set_entry_val(ch["t_target"], str(ch_cfg["T_Target"]))
                set_entry_val(ch["i_target"], str(ch_cfg["I_Target"]))
                set_entry_val(ch["laser_label"], ch_cfg.get("Label", f"Laser {k+1}"))

            self.active_profile_path = file_path
            self.save_last_profile_preference(file_path)
            name = os.path.basename(file_path)
            self.lbl_profile.configure(text=f"Active Profile: {name}", text_color="#888888")

            # If disconnected, the user shouldn't edit live channel configs until they connect
            if self.btn_connect.cget("text") == "Disconnect" and not self.is_scanning:
                self.lock_ui("normal")
            else:
                self.lock_ui("disabled")

            # P3 #12: Show success popup to match MATLAB behaviour
            messagebox.showinfo("Profile Loaded", f'Hardware configuration profile "{name}" has been loaded.')
        except Exception as e:
            messagebox.showerror("Load Error", f"Failed to load config file. It might be corrupted or outdated:\n{e}")

    def clear_profile(self):
        if not messagebox.askyesno("Confirm Clear", "Are you sure you want to clear the active profile and reset all settings to defaults?"):
            return
        
        self.t_ramp_edit.delete(0, tk.END)
        self.t_ramp_edit.insert(0, "0.1")
        self.i_ramp_edit.delete(0, tk.END)
        self.i_ramp_edit.insert(0, "0.5")
        self.t_off_edit.delete(0, tk.END)
        self.t_off_edit.insert(0, "22.0")

        for k, ch in enumerate(self.ch_ui):
            set_entry_val(ch["t_target"], "22.0")
            set_entry_val(ch["i_target"], "0.0")
            set_entry_val(ch["laser_label"], f"Laser {k+1}")

        pref_file = os.path.join(os.path.expanduser("~"), ".ldc_laser_control_prefs.json")
        if os.path.exists(pref_file):
            try:
                os.remove(pref_file)
            except:
                pass
        self.active_profile_path = ""
        self.lbl_profile.configure(text="Active Profile: [Default/Cleared]", text_color="#888888")

    # ----------------------------------------------------
    # SERIAL COMMUNICATION INTERFACE
    # ----------------------------------------------------
    def connect_serial(self):
        # Don't permit disconnect during sequence execution
        if self.is_executing:
            if self.is_stop_requested:
                self.status_label.configure(text="Status: WAITING for halt...", text_color="#f57c00")
            else:
                self.status_label.configure(text="Status: PRESS STOP BEFORE DISCONNECTING!", text_color="#c62828")
            return

        # Handle Disconnect
        if self.btn_connect.cget("text") == "Disconnect":
            self.disconnect_serial()
            return

        # Handle Connect
        port_choice = self.com_dropdown.get()

        if port_choice == "Demo Simulator":
            self.ctl.open_simulator()
            self.status_label.configure(text="Status: Demo Mode Active", text_color="#7b1fa2")
            self.btn_connect.configure(text="Disconnect", fg_color="#c62828", hover_color="#b71c1c")
            self.com_dropdown.configure(state="disabled")
            self.btn_scan.configure(state="normal")
            self.btn_clear_faults.configure(state="normal")
            return

        try:
            self.ctl.open(port_choice, baudrate=9600, timeout=5.0)
            self.status_label.configure(text="Status: Connected (Ready)", text_color="#2e7d32")
            self.btn_connect.configure(text="Disconnect", fg_color="#c62828", hover_color="#b71c1c")
            self.com_dropdown.configure(state="disabled")
            self.btn_scan.configure(state="normal")
            self.btn_clear_faults.configure(state="normal")
        except Exception as e:
            self.status_label.configure(text="Status: Connection Failed", text_color="#c62828")
            messagebox.showerror("Connection Error", f"Failed to connect to {port_choice}:\n{e}")

    def disconnect_serial(self):
        self.telemetry_active = False
        if self.telemetry_thread and self.telemetry_thread.is_alive():
            self.telemetry_thread.join(timeout=1.0)
        
        with self.serial_lock:
            if self.ser:
                try:
                    self.ser.close()
                except:
                    pass
                self.ser = None

        self.is_simulated = False
        self.status_label.configure(text="Status: Disconnected", text_color="#c62828")
        self.btn_connect.configure(text="Connect", fg_color="#1976D2")
        self.com_dropdown.configure(state="normal")
        self.btn_scan.configure(state="disabled")
        self.btn_clear_faults.configure(state="disabled")
        self.btn_exec_all.configure(state="disabled")
        self.lock_ui("disabled")

        # Reset all rows to disconnected look
        for idx in range(self.num_channels):
            self.mark_empty(idx, "Disconnected")

        # Nothing is known to be populated anymore; show every card again.
        self._has_scanned = False
        self._reflow_cards(force=True)


    # ----------------------------------------------------
    # CHASSIS INTERROGATOR (SCAN CHANNELS)
    # ----------------------------------------------------
    def start_channel_scan(self):
        self.is_scanning = True
        self.btn_scan.configure(state="disabled")
        # Keep connect button enabled so user can disconnect and abort if hung
        self.btn_exec_all.configure(state="disabled")
        self.lock_ui("disabled")
        self.status_label.configure(text="Status: Scanning active hardware...", text_color="#f57c00")
        
        # Run scan loop in a background thread to prevent UI freezing
        threading.Thread(target=self.run_channel_scan, daemon=True).start()

    def run_channel_scan(self):
        cards_found = 0
        
        for k in range(self.num_channels):
            if self.is_closing or (not self.ser and not self.is_simulated):
                break
            ch_num = k + 1
            
            with self.serial_lock:
                try:
                    # Flush buffer
                    if not self.is_simulated and self.ser:
                        self.ser.reset_input_buffer()
                    
                    # P0 #2: Use cmd_pause (send + 0.15s wait) to match MATLAB cmdPause,
                    # then an extra 0.1s settling before querying — total 0.25s as in MATLAB.
                    self.cmd_pause(f"CHAN {ch_num}")
                    time.sleep(0.1)
                    if self.is_closing:
                        break

                    if not self.is_simulated and self.ser:
                        self.ser.timeout = 0.5
                    ans_chan_str = self.query_cmd("CHAN?")
                    if not self.is_simulated and self.ser:
                        self.ser.timeout = 1.0
                    
                    try:
                        ans_chan = int(ans_chan_str)
                    except:
                        ans_chan = -1

                    if not ans_chan_str or ans_chan != ch_num:
                        # Empty slot
                        self.after(0, self.mark_empty, k, "Empty Slot")
                        continue

                    cards_found += 1

                    # Query Thermocouple signature to detect attached diode
                    t_val_str = self.query_cmd("TEC:T?")
                    try:
                        t_val = float(t_val_str)
                    except:
                        t_val = float('nan')

                    # Telemetry Snapshot
                    i_val_str = self.query_cmd("LAS:LDI?")
                    try:
                        i_val = float(i_val_str)
                    except:
                        i_val = 0.0

                    tec_out_status = 0
                    las_out_status = 0
                    try:
                        tec_out_status = int(float(self.query_cmd("TEC:OUT?")))
                        las_out_status = int(float(self.query_cmd("LAS:OUT?")))
                    except:
                        pass

                    if not t_val_str or math.isnan(t_val) or t_val < 0:
                        # Card exists but diode floating/negative voltage = no laser attached.
                        # NOTE for future maintainers: this treats ANY sub-zero thermistor
                        # reading as "no diode present". That is valid for this lab's use case
                        # (these lasers always operate above 0 °C, so a negative reading only
                        # ever means a floating/open input), but it is NOT safe for cryogenic
                        # or sub-zero setups — a genuinely cold diode would be misclassified as
                        # absent and locked out. Revisit this sentinel if that ever changes.
                        self.after(0, self.mark_empty, k, "No Laser Attached")
                    else:
                        # Valid card found! Query errors
                        has_hw_err, hw_err_str = self.check_controller_errors_threadsafe(ch_num)
                        
                        # Fetch Limits
                        max_t_str = self.query_cmd("TEC:LIM:THI?")
                        try:
                            max_t = float(max_t_str)
                            if math.isnan(max_t):
                                max_t = 99.0
                        except:
                            max_t = 99.0

                        max_i_str = self.query_cmd("LAS:LIM:I?")
                        try:
                            max_i = float(max_i_str)
                        except:
                            max_i = float('nan')

                        if math.isnan(max_i):
                            max_i_str = self.query_cmd("LAS:LIM:LDI?")
                            try:
                                max_i = float(max_i_str)
                            except:
                                max_i = 500.0

                        if math.isnan(max_i):
                            max_i = 500.0

                        # Restore modulation with a safety settling delay
                        self.cmd_pause("LAS:MOD 1")

                        # Dispatch updates to UI thread safely
                        self.after(0, self.update_channel_after_scan, k, t_val, i_val, tec_out_status, las_out_status, max_t, max_i, has_hw_err, hw_err_str)
                except Exception as e:
                    print(f"Error scanning channel {ch_num}: {e}")
                    self.after(0, self.mark_empty, k, "Empty Slot")

        # Restore default timeout
        if not self.is_simulated and self.ser:
            self.ser.timeout = 5.0

        # Run completion callbacks
        self.after(0, self.finish_channel_scan, cards_found)

    def update_channel_after_scan(self, idx, t_val, i_val, tec_out, las_out, max_t, max_i, has_hw_err, hw_err_str):
        ch = self.ch_ui[idx]
        self.ch_populated[idx] = True   # a laser card responded here
        ch["enable_chk"].configure(state="normal")
        ch["enable_var"].set(True)
        ch["laser_label"].configure(state="normal")

        # Update Live boxes
        set_entry_val(ch["cur_t"], f"{t_val:.1f}")
        set_entry_val(ch["cur_i"], f"{i_val:.1f}")
        
        ch["t_lim"].configure(text=f"{max_t:.0f}")
        ch["i_lim"].configure(text=f"{max_i:.0f}")

        # Update Command State drop downs to match hardware
        ch["tec_cmd"].configure(state="normal")
        ch["las_cmd"].configure(state="normal")
        ch["tec_cmd"].set("ON" if tec_out == 1 else "OFF")
        ch["las_cmd"].set("ON" if las_out == 1 else "OFF")

        ch["t_target"].configure(state="normal")
        ch["i_target"].configure(state="normal")
        ch["btn_exec"].configure(state="normal")

        # Set live output status boxes visually
        if tec_out == 1:
            ch["live_tec"].configure(fg_color="#2e7d32", text_color="white")
            set_entry_val(ch["live_tec"], "ON")
        else:
            ch["live_tec"].configure(fg_color=("#e0e0e0", "#3a3a3a"), text_color=("black", "#888888"))
            set_entry_val(ch["live_tec"], "OFF")

        if las_out == 1:
            ch["live_las"].configure(fg_color="#2e7d32", text_color="white")
            set_entry_val(ch["live_las"], "ON")
        else:
            ch["live_las"].configure(fg_color=("#e0e0e0", "#3a3a3a"), text_color=("black", "#888888"))
            set_entry_val(ch["live_las"], "OFF")

        # Update LED colors and status text
        if has_hw_err:
            ch["status"].configure(text=f"FAULT: {hw_err_str}", text_color="#c62828")
            ch["led"].set_color(theme.led("fault"))
        elif tec_out == 1 and las_out == 1:
            ch["status"].configure(text="TEC ON, LAS ON", text_color="#f57c00")
            ch["led"].set_color(theme.led("ok"))
        elif tec_out == 1:
            ch["status"].configure(text="TEC ON, LAS OFF", text_color="#f57c00")
            ch["led"].set_color(theme.led("warn"))
        elif las_out == 1:
            ch["status"].configure(text="WARNING: LAS ON, TEC OFF", text_color="#c62828")
            ch["led"].set_color(theme.led("fault"))
        else:
            ch["status"].configure(text="Ready", text_color="#2e7d32")
            ch["led"].set_color(theme.led("ok"))

    def finish_channel_scan(self, cards_found):
        self.is_scanning = False
        if self.is_closing:
            self.do_cleanup_and_close()
            return

        if cards_found == 0:
            self.status_label.configure(text="WARNING: 0 slots responded. Check connection & power.", text_color="#f57c00")
        else:
            self.status_label.configure(text="Status: Scan Complete & Matched", text_color="#2e7d32")

        # We now know which slots are populated, so the "unused" filter can apply.
        self._has_scanned = True
        self._reflow_cards(force=True)

        self.lock_ui("normal")
        self.btn_connect.configure(state="normal")

        # Start Telemetry thread if not active
        if not self.telemetry_active:
            self.telemetry_active = True
            self.telemetry_thread = threading.Thread(target=self.telemetry_loop, daemon=True)
            self.telemetry_thread.start()

    def mark_empty(self, idx, reason):
        ch = self.ch_ui[idx]
        self.ch_populated[idx] = False   # empty slot / no laser / disconnected
        ch["enable_chk"].configure(state="disabled")
        ch["enable_var"].set(False)
        ch["laser_label"].configure(state="disabled")
        
        ch["live_tec"].configure(fg_color=("#e0e0e0", "#3a3a3a"), text_color=("black", "#888888"))
        set_entry_val(ch["live_tec"], "OFF")
        ch["live_las"].configure(fg_color=("#e0e0e0", "#3a3a3a"), text_color=("black", "#888888"))
        set_entry_val(ch["live_las"], "OFF")

        ch["status"].configure(text=reason, text_color="#78909c")
        ch["led"].set_color(theme.led("empty"))
        set_entry_val(ch["cur_t"], "0.0")
        set_entry_val(ch["cur_i"], "0.0")

        ch["tec_cmd"].configure(state="disabled")
        ch["las_cmd"].configure(state="disabled")
        ch["t_target"].configure(state="disabled")
        ch["i_target"].configure(state="disabled")
        ch["btn_exec"].configure(state="disabled")

    def lock_ui(self, state):
        # State: normal or disabled
        for ch in self.ch_ui:
            # Skip locked empty slots
            status_text = ch["status"].cget("text")
            if status_text in ["Empty Slot", "No Laser Attached", "Disconnected"]:
                continue
            ch["btn_exec"].configure(state=state)
            ch["enable_chk"].configure(state=state)
            ch["tec_cmd"].configure(state=state)
            ch["las_cmd"].configure(state=state)
            ch["t_target"].configure(state=state)
            ch["i_target"].configure(state=state)
            ch["laser_label"].configure(state=state)

        self.btn_exec_all.configure(state=state)
        self.btn_scan.configure(state=state)
        self.btn_clear_faults.configure(state=state)
        self.btn_load.configure(state=state)
        self.btn_save.configure(state=state)
        self.btn_clear_profile.configure(state=state)
        self.t_ramp_edit.configure(state=state)
        self.i_ramp_edit.configure(state=state)
        self.t_off_edit.configure(state=state)

        # Master buttons
        self.btn_master_on.configure(state=state)
        self.btn_master_off.configure(state=state)
        self.btn_tec_on.configure(state=state)
        self.btn_tec_off.configure(state=state)
        self.btn_las_on.configure(state=state)
        self.btn_las_off.configure(state=state)

    # ----------------------------------------------------
    # TELEMETRY LOOP BACKGROUND SERVICE
    # ----------------------------------------------------
    def telemetry_loop(self):
        while self.telemetry_active:
            if not self.is_executing and not self.is_scanning and not self.is_closing:
                self.run_telemetry_cycle()
            time.sleep(2.0)

    def run_telemetry_cycle(self):
        with self.serial_lock:
            if not self.ser and not self.is_simulated:
                return

            old_timeout = 5.0
            if not self.is_simulated and self.ser:
                old_timeout = self.ser.timeout
                self.ser.timeout = 0.2

            any_laser_on = False

            for k in range(self.num_channels):
                # Break immediately if state changes during sweep
                if self.is_executing or self.is_scanning or self.is_closing:
                    break

                ch = self.ch_ui[k]
                # Only telemetry valid active channels
                if ch["enable_chk"].cget("state") == "normal":
                    ch_num = k + 1
                    try:
                        # P0 #4: cmd_pause includes the 0.15s settling delay after CHAN
                        # switch that MATLAB's cmdPause() provided. Without it the next
                        # query may return data from the previously selected channel.
                        self.cmd_pause(f"CHAN {ch_num}")

                        t_val_str = self.query_cmd("TEC:T?")
                        try:
                            t_val = float(t_val_str)
                            if math.isnan(t_val):
                                t_val = None
                        except:
                            t_val = None

                        i_val_str = self.query_cmd("LAS:LDI?")
                        try:
                            i_val = float(i_val_str)
                            if math.isnan(i_val):
                                i_val = None
                        except:
                            i_val = None

                        tec_stat = None
                        las_stat = None
                        try:
                            tec_stat = int(float(self.query_cmd("TEC:OUT?")))
                            las_stat = int(float(self.query_cmd("LAS:OUT?")))
                        except:
                            pass

                        # Dispatch updates safely
                        self.after(0, self.update_telemetry_widgets, k, t_val, i_val, tec_stat, las_stat)
                        
                        if las_stat == 1:
                            any_laser_on = True

                        self.telemetry_fail_count = 0  # Reset on success

                    except Exception as e:
                        print(f"Background telemetry failed on channel {ch_num}: {e}")
                        self.telemetry_fail_count += 1
                        if self.telemetry_fail_count > 3:
                            self.telemetry_active = False
                            self.after(0, self.handle_telemetry_connection_loss)
                            break

            # Update EMO button status
            if any_laser_on:
                self.after(0, lambda: self.btn_emerg.configure(state="normal"))
            else:
                self.after(0, lambda: self.btn_emerg.configure(state="disabled"))

            if not self.is_simulated and self.ser:
                self.ser.timeout = old_timeout

    def update_telemetry_widgets(self, idx, t_val, i_val, tec_stat, las_stat):
        try:
            if self.is_closing:
                return
            ch = self.ch_ui[idx]
            if t_val is not None:
                set_entry_val(ch["cur_t"], f"{t_val:.1f}")
            if i_val is not None:
                set_entry_val(ch["cur_i"], f"{i_val:.1f}")

            if tec_stat is not None:
                if tec_stat == 1:
                    ch["live_tec"].configure(fg_color="#2e7d32", text_color="white")
                    set_entry_val(ch["live_tec"], "ON")
                else:
                    ch["live_tec"].configure(fg_color=("#e0e0e0", "#3a3a3a"), text_color=("black", "#888888"))
                    set_entry_val(ch["live_tec"], "OFF")

            if las_stat is not None:
                if las_stat == 1:
                    ch["live_las"].configure(fg_color="#2e7d32", text_color="white")
                    set_entry_val(ch["live_las"], "ON")
                else:
                    ch["live_las"].configure(fg_color=("#e0e0e0", "#3a3a3a"), text_color=("black", "#888888"))
                    set_entry_val(ch["live_las"], "OFF")
        except tk.TclError:
            pass

    def handle_telemetry_connection_loss(self):
        self.status_label.configure(text="Status: Connection Lost", text_color="#c62828")
        self.lock_ui("disabled")
        self.btn_connect.configure(text="Connect", fg_color="#1976D2")
        self.ser = None
        messagebox.showerror("Connection Lost", "Hardware communication lost. Execution halted. Lasers not explicitly shut off.")
        self.is_stop_requested = True

    # ----------------------------------------------------
    # MASTER DROPDOWN / OVERRIDE FUNCTIONS
    # ----------------------------------------------------
    def set_all_dropdowns(self, type_str, state_str):
        for ch in self.ch_ui:
            if type_str == "TEC" and ch["tec_cmd"].cget("state") == "normal":
                ch["tec_cmd"].set(state_str)
            elif type_str == "LAS" and ch["las_cmd"].cget("state") == "normal":
                ch["las_cmd"].set(state_str)
        self.mark_profile_unsaved()

    def set_all_systems(self, state_str):
        for ch in self.ch_ui:
            if ch["tec_cmd"].cget("state") == "normal":
                ch["tec_cmd"].set(state_str)
            if ch["las_cmd"].cget("state") == "normal":
                ch["las_cmd"].set(state_str)
        self.mark_profile_unsaved()

    # ----------------------------------------------------
    # SAFETY SHUTOFFS (EMO & STOP BUTTONS)
    # ----------------------------------------------------
    def stop_execution(self):
        if self.is_executing:
            self.is_stop_requested = True
            self.status_label.configure(text="Status: STOP COMMANDED. Halting safely...", text_color="#c62828")
            # P3 #9: Triple bell matches MATLAB triple-beep auditory alert in lab environments
            self.bell()
            self.after(150, self.bell)
            self.after(300, self.bell)

    def emergency_las_off(self):
        self.btn_emerg.configure(state="disabled")
        msg = "WARNING: Immediately cutting current without ramping down can damage the laser diode. Are you sure you want to proceed?"
        if not messagebox.askyesno("Emergency LAS OFF", msg):
            self.btn_emerg.configure(state="normal")
            return

        # Break any in-progress ramp immediately so the laser stops advancing.
        self.is_stop_requested = True
        self.is_emo_requested = True

        if self.is_executing:
            # A sequence is running and owns the serial lock for the entire duration
            # of each channel's ramp. Spawning a competing EMO thread here would just
            # block on that lock until the whole ramp finished (the previous bug: EMO
            # could not fire during the most dangerous moment of a run). Instead we
            # flag it: setting is_stop_requested aborts the ramp within a fraction of
            # a second (via safe_pause), and run_sequence_thread's cleanup performs
            # the laser cutoff itself once the serial lock is free.
            self.status_label.configure(text="Status: EMERGENCY OFF — cutting laser...", text_color="#ff0000")
            return

        # Idle (laser holding but not ramping): the serial lock is free, so cut now.
        # P1 #6: EMO thread must NOT be a daemon — laser-off commands must complete
        # even if the main thread is exiting. Track it for join-on-close.
        self._emo_thread = threading.Thread(target=self._perform_emergency_shutdown, daemon=False)
        self._emo_thread.start()

    def _perform_emergency_shutdown(self):
        """Cut LAS output on every active channel. Caller must ensure the serial
        lock is free (either idle, or the sequence thread has released it)."""
        with self.serial_lock:
            for k in range(self.num_channels):
                # P2 #7: Abort gracefully if app is closing (Removed: EMO must complete)
                ch = self.ch_ui[k]
                if ch["enable_chk"].cget("state") == "normal":
                    ch_num = k + 1
                    try:
                        self.send_cmd(f"CHAN {ch_num}")
                        time.sleep(0.15)
                        self.send_cmd("LAS:OUTPUT 0")

                        # Update UI
                        self.after(0, self.update_channel_emergency_off, k)
                    except:
                        pass

            self.after(0, self.finish_emergency_las_off)

    def update_channel_emergency_off(self, idx):
        ch = self.ch_ui[idx]
        ch["live_las"].configure(fg_color=("#e0e0e0", "#3a3a3a"), text_color=("black", "#888888"))
        set_entry_val(ch["live_las"], "OFF")
        ch["status"].configure(text="EMERGENCY OFF: Current Cut", text_color="#c62828")
        ch["led"].set_color(theme.led("fault"))

    def finish_emergency_las_off(self):
        self.status_label.configure(text="Status: EMERGENCY LASER SHUTDOWN TRIGGERED.", text_color="#ff0000")
        self.is_stop_requested = True
        self.btn_emerg.configure(state="normal")

    def clear_faults(self):
        self.lock_ui("disabled")
        self.status_label.configure(text="Status: Clearing chassis faults...", text_color="#f57c00")
        threading.Thread(target=self.run_clear_faults, daemon=True).start()

    def run_clear_faults(self):
        with self.serial_lock:
            for k in range(self.num_channels):
                if self.is_closing:
                    break
                ch = self.ch_ui[k]
                if ch["enable_chk"].cget("state") == "normal":
                    ch_num = k + 1
                    try:
                        self.send_cmd(f"CHAN {ch_num}")
                        time.sleep(0.1)
                        self.send_cmd("*CLS")
                        time.sleep(0.1)
                    except:
                        pass

        # P1 #8: Set is_scanning flag so the telemetry loop won't collide
        # with the subsequent re-scan on the serial port.
        self.is_scanning = True
        # Trigger a scan again to refresh hardware status
        self.run_channel_scan()

    # ----------------------------------------------------
    # PROCEDURE EXECUTION LOOP (RUN ALL & RUN CH)
    # ----------------------------------------------------
    def execute_single_channel(self, ch_num):
        self.execute_channels([ch_num])

    def execute_all(self):
        active_channels = []
        for idx, ch in enumerate(self.ch_ui):
            if ch["enable_chk"].cget("state") == "normal" and ch["enable_var"].get():
                active_channels.append(idx + 1)

        if not active_channels:
            messagebox.showwarning("Warning", "No channels are marked \"Enable\" for the sequence!")
            return
        
        self.execute_channels(active_channels)

    def execute_channels(self, channels_to_run):
        if self.is_executing:
            return

        # Read global ramp configuration inputs
        try:
            t_ramp = float(self.t_ramp_edit.get())
            i_ramp = float(self.i_ramp_edit.get())
            t_off_target = float(self.t_off_edit.get())
        except ValueError:
            messagebox.showerror("Invalid Configuration", "Ramping speeds and off target must be valid numbers.")
            return

        if t_ramp <= 0.0 or i_ramp <= 0.0:
            messagebox.showerror("Invalid Configuration", "Ramp speeds must be strictly greater than 0 to prevent hardware damage and infinite loops.")
            return

        # Build the channel plans and ETA on the MAIN thread. All widget reads must
        # happen here — the worker thread (and the sequence engine) must never touch
        # Tk widgets. The engine reports back only through the _TkSequenceEvents
        # adapter, which marshals every update via after().
        plans, total_est = self._build_sequence(channels_to_run, t_ramp, i_ramp, t_off_target)

        # Prepare state variables
        self.is_executing = True
        self.is_stop_requested = False
        self.is_emo_requested = False
        self.status_label.configure(text="Status: Sequence Running...", text_color="#2e7d32")

        self.lock_ui("disabled")
        self.btn_stop.configure(state="normal")
        self.btn_emerg.configure(state="normal")

        # Start sequence thread
        threading.Thread(target=self.run_sequence_thread,
                         args=(plans, t_ramp, i_ramp, t_off_target, total_est),
                         daemon=True).start()

    def _build_sequence(self, channels_to_run, t_ramp, i_ramp, t_off_target):
        """Read the per-channel widgets (MAIN THREAD ONLY) and produce the channel
        plans plus the estimated total run time (ETA). Kept off the worker thread so
        the sequence engine never touches Tk widgets."""
        TEC_ON_TIME = 1.0
        LAS_ON_TIME = 4.0
        LAS_OFF_TIME = 1.5
        TEC_OFF_TIME = 1.0

        total_est = 0.0
        plans = []
        for ch_num in channels_to_run:
            ch = self.ch_ui[ch_num - 1]

            t_cmd = ch["tec_cmd"].get()
            l_cmd = ch["las_cmd"].get()
            live_t = ch["live_tec"].get()
            live_l = ch["live_las"].get()

            # Targets are user input and decide whether the channel can run.
            try:
                t_targ = float(ch["t_target"].get())
                i_targ = float(ch["i_target"].get())
                targets_valid = True
            except ValueError:
                t_targ, i_targ, targets_valid = 0.0, 0.0, False

            # ETA snapshot (matches MATLAB: any unparseable field -> all defaults).
            try:
                curr_t = float(ch["cur_t"].get())
                curr_i = float(ch["cur_i"].get())
                eta_t = float(ch["t_target"].get())
                eta_i = float(ch["i_target"].get())
            except ValueError:
                curr_t, curr_i, eta_t, eta_i = 22.0, 0.0, 22.0, 0.0

            if live_t == "OFF" and live_l == "OFF":
                if t_cmd == "ON" and l_cmd == "OFF":
                    total_est += TEC_ON_TIME + abs(eta_t - curr_t) / t_ramp
                elif t_cmd == "ON" and l_cmd == "ON":
                    total_est += TEC_ON_TIME + abs(eta_t - curr_t) / t_ramp
                    total_est += LAS_ON_TIME + abs(eta_i - 0.0) / i_ramp
            elif live_t == "ON" and live_l == "OFF":
                if t_cmd == "ON" and l_cmd == "ON":
                    total_est += abs(eta_t - curr_t) / t_ramp
                    total_est += LAS_ON_TIME + abs(eta_i - 0.0) / i_ramp
                elif t_cmd == "OFF" and l_cmd == "OFF":
                    total_est += abs(t_off_target - curr_t) / t_ramp + TEC_OFF_TIME
                elif t_cmd == "ON" and l_cmd == "OFF":
                    total_est += abs(eta_t - curr_t) / t_ramp
            elif live_t == "ON" and live_l == "ON":
                if t_cmd == "ON" and l_cmd == "OFF":
                    total_est += abs(0.0 - curr_i) / i_ramp + LAS_OFF_TIME
                    total_est += abs(eta_t - curr_t) / t_ramp
                elif t_cmd == "OFF" and l_cmd == "OFF":
                    total_est += abs(0.0 - curr_i) / i_ramp + LAS_OFF_TIME
                    total_est += abs(t_off_target - curr_t) / t_ramp + TEC_OFF_TIME
                elif t_cmd == "ON" and l_cmd == "ON":
                    total_est += abs(eta_t - curr_t) / t_ramp
                    total_est += abs(eta_i - curr_i) / i_ramp

            plans.append(ChannelPlan(
                idx=ch_num - 1, ch_num=ch_num,
                tec_cmd=t_cmd, las_cmd=l_cmd,
                t_target=t_targ, i_target=i_targ,
                targets_valid=targets_valid))

        return plans, total_est

    def run_sequence_thread(self, plans, t_ramp, i_ramp, t_off_target, total_estimated_time):
        # Plans and ETA were prepared on the main thread (see _build_sequence).
        # This worker only drives the UI-agnostic sequence engine, which does all
        # hardware I/O, the safety state machine, and the ramps.
        self.total_estimated_time = total_estimated_time
        self.sequence_start_time = time.time()

        self.seq.run(plans, t_ramp, i_ramp, t_off_target)

        # Finished Phase Cleanup
        self.is_executing = False
        self.after(0, self.lock_ui, "normal")
        self.after(0, lambda: self.btn_stop.configure(state="disabled"))

        if self.is_emo_requested:
            # EMO was requested mid-run: the ramp has aborted and we've now left the
            # serial-lock block, so the lock is free. Cut every active laser here in
            # the sequence thread (finish_emergency_las_off sets the final status).
            self._perform_emergency_shutdown()
            self.is_emo_requested = False
        elif self.is_stop_requested:
            self.after(0, lambda: self.status_label.configure(text="Status: Hardware Halted & Pinned.", text_color="#c62828"))
        else:
            self.after(0, lambda: self.status_label.configure(text="Status: Sequence Complete & Settled.", text_color="#2e7d32"))
            if len(plans) > 1:
                self.bell()
                self.after(0, lambda: messagebox.showinfo("Done", "Sequence completed across all selected channels."))

        # Trigger telemetry immediately to synchronize button state
        self.run_telemetry_cycle()

    # --- Sequence event handlers (invoked on the Tk thread via _TkSequenceEvents) ---
    def _handle_channel_halted(self, idx):
        """UI response to a cooperative STOP / EMO halt on channel idx."""
        ch = self.ch_ui[idx]
        if ch["status"].cget("text") == "Initializing...":
            self.update_channel_status(idx, "HALTED (Before Ramp)", theme.status("fault"))
        else:
            self.update_channel_status(
                idx, f"HALTED at {ch['cur_t'].get()}°C, {ch['cur_i'].get()}mA", theme.status("fault"))
        self._triple_bell()

    def _handle_channel_fault(self, idx, message):
        """UI response to a hardware / validation fault on channel idx."""
        self.update_channel_status(idx, message, theme.status("fault"))
        self.ch_ui[idx]["led"].set_color(theme.led("fault"))
        print(f"[Hardware Fault] Channel {idx + 1}: {message}")
        self._triple_bell()

    def _triple_bell(self):
        # P3 #9: Triple bell matches MATLAB triple-beep for fault/halt alerts.
        self.bell()
        self.after(150, self.bell)
        self.after(300, self.bell)

    # --- UI Dispatch Update Helpers ---
    def update_channel_status(self, idx, text, color):
        self.ch_ui[idx]["status"].configure(text=text, text_color=color)

    def update_live_out_ui(self, idx, type_str, state_str):
        ch = self.ch_ui[idx]
        if type_str == "TEC":
            if state_str == "ON":
                ch["live_tec"].configure(fg_color="#2e7d32", text_color="white")
                set_entry_val(ch["live_tec"], "ON")
            else:
                ch["live_tec"].configure(fg_color=("#e0e0e0", "#3a3a3a"), text_color=("black", "#888888"))
                set_entry_val(ch["live_tec"], "OFF")
        elif type_str == "LAS":
            if state_str == "ON":
                ch["live_las"].configure(fg_color="#2e7d32", text_color="white")
                set_entry_val(ch["live_las"], "ON")
            else:
                ch["live_las"].configure(fg_color=("#e0e0e0", "#3a3a3a"), text_color=("black", "#888888"))
                set_entry_val(ch["live_las"], "OFF")

    def update_eta(self):
        if not self.sequence_start_time:
            return
        elapsed = time.time() - self.sequence_start_time
        rem = max(0.0, self.total_estimated_time - elapsed)
        
        mins = int(rem // 60)
        secs = int(rem % 60)

        if rem > 0.0:
            self.after(0, lambda: self.status_label.configure(
                text=f"Status: Sequence Running... (Time Remaining: {mins:02d}:{secs:02d})", text_color="#2e7d32"))
        else:
            self.after(0, lambda: self.status_label.configure(
                text="Status: Sequence Running... (Finishing up)", text_color="#2e7d32"))

    # ----------------------------------------------------
    # APPLICATION CLEANUP & SHUTDOWN
    # ----------------------------------------------------
    def close_app(self):
        if self.is_executing:
            selection = messagebox.askyesnocancel("Action Required", 
                "A hardware sequence is currently running. You must safely halt the hardware before closing.\n\nWould you like to STOP execution now?")
            if selection:  # Yes -> Stop and delay closing
                self.stop_execution()
            return

        if self.is_scanning:
            self.is_closing = True
            self.status_label.configure(text="Status: Aborting scan to close...", text_color="#f57c00")
            return

        # Check unsaved profile
        text = self.lbl_profile.cget("text")
        if text.startswith("* "):
            selection = messagebox.askyesnocancel("Unsaved Changes", 
                "You have unsaved changes to the active profile. Do you want to save them before exiting?")
            if selection is True:  # Save
                self.save_config()
                # Verify if save was completed or cancelled
                if self.lbl_profile.cget("text").startswith("* "):
                    return
            elif selection is None:  # Cancel close
                return

        self.do_cleanup_and_close()

    def do_cleanup_and_close(self):
        self.is_closing = True
        self.telemetry_active = False

        # Join telemetry thread
        if self.telemetry_thread and self.telemetry_thread.is_alive():
            self.telemetry_thread.join(timeout=1.0)

        # P1 #6: Join EMO thread — it's non-daemon so laser-off commands must complete
        emo_thread = getattr(self, "_emo_thread", None)
        if emo_thread and emo_thread.is_alive():
            emo_thread.join(timeout=3.0)

        # Close serial port
        with self.serial_lock:
            if self.ser:
                try:
                    self.ser.close()
                except:
                    pass
                self.ser = None

        self.destroy()
        sys.exit(0)


# --- Generic Helper for CTkEntry Value Manipulation ---
def set_entry_val(entry, value):
    state = entry.cget("state")
    entry.configure(state="normal")
    entry.delete(0, tk.END)
    entry.insert(0, str(value))
    entry.configure(state=state)


if __name__ == "__main__":
    app = LDCControllerApp()
    app.mainloop()
