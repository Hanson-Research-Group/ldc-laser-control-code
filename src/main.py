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
    """A custom Tkinter widget to draw a beautiful, smooth status LED."""
    def __init__(self, parent, size=18, color="#cccccc", **kwargs):
        # Dynamically resolve background color based on parent CustomTkinter frame color
        bg_color = "#ebebeb"
        try:
            parent_fg = parent.cget("fg_color")
            if isinstance(parent_fg, (list, tuple)):
                mode = ctk.get_appearance_mode().lower()
                bg_color = parent_fg[0] if mode == "light" else parent_fg[1]
            elif parent_fg and parent_fg != "transparent":
                bg_color = parent_fg
        except:
            pass
        super().__init__(parent, width=size, height=size, bg=bg_color, highlightthickness=0, **kwargs)
        self.size = size
        self.color = color
        self.draw_circle()

    def draw_circle(self):
        self.delete("all")
        self.create_oval(2, 2, self.size - 2, self.size - 2, 
                         fill=self.color, outline=self.color, width=1.0)

    def set_color(self, color):
        self.color = color
        self.draw_circle()


class LDCControllerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # --- Configure Main Window ---
        self.title("LDC-3908 Modular Laser Diode Controller Software v0.3.1")
        self.geometry("1850x950")
        self.minsize(1650, 800)
        
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

        # --- Shared Application State Variables ---
        self.ser = None
        self.serial_lock = threading.Lock()
        self.num_channels = 8
        self.is_executing = False
        self.is_scanning = False
        self.is_stop_requested = False
        self.is_simulated = False
        self.active_profile_path = ""
        self.total_estimated_time = 0.0
        self.sequence_start_time = None
        self.is_closing = False
        
        # Telemetry control
        self.telemetry_thread = None
        self.telemetry_active = False
        self.telemetry_fail_count = 0

        # --- Simulation Mock State ---
        # 1=Installed/Good, 2=Installed/NoLaser(T<0), 0=Empty Slot
        self.sim_state = {
            'curr_chan': 1,
            'T_actual': [22.0] * 8,
            'I_actual': [0.0] * 8,
            'TEC_ON': [0] * 8,
            'LAS_ON': [0] * 8,
            'LAS_MOD': [1] * 8,
            'is_installed': [1, 1, 0, 2, 1, 0, 0, 0]  # Exact match to MATLAB
        }
        self.sim_query_response = ""

        # --- Build GUI Layout ---
        self.setup_layout()

        # --- Load Last Profile Preference ---
        self.load_last_profile_preference()

        # --- Handle Application Closing Gracefully ---
        self.protocol("WM_DELETE_WINDOW", self.close_app)

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

        self.btn_clear_faults = ctk.CTkButton(top_frame, text="Clear Faults", width=135, fg_color="transparent", text_color="#c62828", 
                                               hover_color="#ffebee", state="disabled", command=self.clear_faults)
        self.btn_clear_faults.grid(row=0, column=5, padx=5, pady=10)

        self.status_label = ctk.CTkLabel(top_frame, text="Status: Disconnected", text_color="#c62828", font=("Segoe UI", 16, "bold"))
        self.status_label.grid(row=0, column=7, padx=(10, 20), pady=10, sticky="e")

        # ----------------------------------------------------
        # 2. MIDDLE PANEL: CHANNELS GRID
        # ----------------------------------------------------
        chan_panel = ctk.CTkFrame(self)
        chan_panel.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        
        # Configure layout grids inside channel panel
        chan_panel.grid_rowconfigure(1, weight=1)
        chan_panel.grid_columnconfigure(0, weight=1)

        # Title Label
        ctk.CTkLabel(chan_panel, text="Individual Channel Configuration & Live Telemetry", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, padx=10, pady=5, sticky="w")

        # Dynamic Grid Container
        self.grid_container = ctk.CTkFrame(chan_panel, fg_color="transparent")
        self.grid_container.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        
        # Headers definitions
        headers = [
            ("Ch.", 48), ("Enable", 68), ("Label", 162), ("LED", 54), ("Status", 203),
            ("Live TEC", 102), ("Live LAS", 102), ("Live T (°C)", 115), ("Live I (mA)", 115),
            ("Target TEC", 115), ("Target LAS", 115), ("Target T (°C)", 115), ("Max T", 61),
            ("Target I (mA)", 115), ("Max I", 61), ("Action", 122)
        ]
        
        for col_idx, (header_text, min_width) in enumerate(headers):
            self.grid_container.grid_columnconfigure(col_idx, weight=1, minsize=min_width)
            lbl = ctk.CTkLabel(self.grid_container, text=header_text, font=("Segoe UI", 14, "bold"))
            # Custom styling color for live readouts
            if "Live" in header_text:
                lbl.configure(text_color="#2e7d32")
            lbl.grid(row=0, column=col_idx, padx=3, pady=5)

        # Store UI elements per channel
        self.ch_ui = []
        
        for i in range(self.num_channels):
            ch_idx = i + 1
            row = ch_idx

            self.grid_container.grid_rowconfigure(row, weight=1)

            # Ch. number
            lbl_ch = ctk.CTkLabel(self.grid_container, text=str(ch_idx), font=("Segoe UI", 14, "bold"))
            lbl_ch.grid(row=row, column=0, padx=3, pady=3)

            # Enable checkbox
            var_enable = tk.BooleanVar(value=False)
            chk_enable = ctk.CTkCheckBox(self.grid_container, text="", variable=var_enable, width=27, state="disabled")
            chk_enable.grid(row=row, column=1, padx=3, pady=3)

            # Label field
            ent_label = ctk.CTkEntry(self.grid_container, placeholder_text=f"Laser {ch_idx}", width=149, state="disabled")
            ent_label.insert(0, f"Laser {ch_idx}")
            ent_label.grid(row=row, column=2, padx=3, pady=3, sticky="ew")
            ent_label.bind("<KeyRelease>", self.mark_profile_unsaved)

            # LED Indicator Canvas
            led = LEDIndicator(self.grid_container, size=20, color="#b0bec5")
            led.grid(row=row, column=3, padx=3, pady=3)

            # Status Label
            lbl_status = ctk.CTkLabel(self.grid_container, text="Run Scan First", text_color="#888888", font=("Segoe UI", 19))
            lbl_status.grid(row=row, column=4, padx=3, pady=3, sticky="w")

            # Live TEC state field (read-only entry)
            ent_live_tec = ctk.CTkEntry(self.grid_container, width=88, state="disabled", font=("Segoe UI", 14, "bold"), justify="center",
                                       fg_color=("#e0e0e0", "#3a3a3a"), text_color=("black", "#888888"))
            set_entry_val(ent_live_tec, "OFF")
            ent_live_tec.grid(row=row, column=5, padx=3, pady=3)

            # Live LAS state field (read-only entry)
            ent_live_las = ctk.CTkEntry(self.grid_container, width=88, state="disabled", font=("Segoe UI", 14, "bold"), justify="center",
                                       fg_color=("#e0e0e0", "#3a3a3a"), text_color=("black", "#888888"))
            set_entry_val(ent_live_las, "OFF")
            ent_live_las.grid(row=row, column=6, padx=3, pady=3)

            # Live Temp Readout
            ent_live_t = ctk.CTkEntry(self.grid_container, width=102, state="disabled", font=("Segoe UI", 19), justify="right")
            set_entry_val(ent_live_t, "0.0")
            ent_live_t.grid(row=row, column=7, padx=3, pady=3)

            # Live Current Readout
            ent_live_i = ctk.CTkEntry(self.grid_container, width=102, state="disabled", font=("Segoe UI", 19), justify="right")
            set_entry_val(ent_live_i, "0.0")
            ent_live_i.grid(row=row, column=8, padx=3, pady=3)

            # Target TEC Dropdown
            opt_tec = ctk.CTkOptionMenu(self.grid_container, values=["ON", "OFF"], width=102, state="disabled", command=self.mark_profile_unsaved)
            opt_tec.set("OFF")
            opt_tec.grid(row=row, column=9, padx=3, pady=3)

            # Target LAS Dropdown
            opt_las = ctk.CTkOptionMenu(self.grid_container, values=["ON", "OFF"], width=102, state="disabled", command=self.mark_profile_unsaved)
            opt_las.set("OFF")
            opt_las.grid(row=row, column=10, padx=3, pady=3)

            # Target Temp Entry
            ent_target_t = ctk.CTkEntry(self.grid_container, width=102, state="disabled", justify="right")
            ent_target_t.insert(0, "22.0")
            ent_target_t.grid(row=row, column=11, padx=3, pady=3)
            ent_target_t.bind("<KeyRelease>", self.mark_profile_unsaved)
            ent_target_t.bind("<FocusOut>", lambda e, w=ent_target_t: self._validate_numeric_entry(w))

            # Max Temp Limit Label
            lbl_max_t = ctk.CTkLabel(self.grid_container, text="-", text_color="#666666", font=("Segoe UI", 19))
            lbl_max_t.grid(row=row, column=12, padx=3, pady=3)

            # Target Current Entry
            ent_target_i = ctk.CTkEntry(self.grid_container, width=102, state="disabled", justify="right")
            ent_target_i.insert(0, "0.0")
            ent_target_i.grid(row=row, column=13, padx=3, pady=3)
            ent_target_i.bind("<KeyRelease>", self.mark_profile_unsaved)
            ent_target_i.bind("<FocusOut>", lambda e, w=ent_target_i: self._validate_numeric_entry(w))

            # Max Current Limit Label
            lbl_max_i = ctk.CTkLabel(self.grid_container, text="-", text_color="#666666", font=("Segoe UI", 19))
            lbl_max_i.grid(row=row, column=14, padx=3, pady=3)

            # Run Channel Button
            btn_run_ch = ctk.CTkButton(self.grid_container, text="▶ Run Ch.", width=108, state="disabled", 
                                       command=lambda ch=ch_idx: self.execute_single_channel(ch))
            btn_run_ch.grid(row=row, column=15, padx=3, pady=3)

            # Store mapping reference
            self.ch_ui.append({
                'label_num': lbl_ch,
                'enable_var': var_enable,
                'enable_chk': chk_enable,
                'laser_label': ent_label,
                'led': led,
                'status': lbl_status,
                'live_tec': ent_live_tec,
                'live_las': ent_live_las,
                'cur_t': ent_live_t,
                'cur_i': ent_live_i,
                'tec_cmd': opt_tec,
                'las_cmd': opt_las,
                't_target': ent_target_t,
                't_lim': lbl_max_t,
                'i_target': ent_target_i,
                'i_lim': lbl_max_i,
                'btn_exec': btn_run_ch
            })

        # Global overrides row layout under grid
        master_row_1 = self.num_channels + 1
        master_row_2 = self.num_channels + 2
        self.grid_container.grid_rowconfigure(master_row_1, weight=1)
        self.grid_container.grid_rowconfigure(master_row_2, weight=1)

        # Master All ON/OFF aligned with live readouts
        self.btn_master_on = ctk.CTkButton(self.grid_container, text="MASTER All ON", font=("Segoe UI", 14, "bold"),
                                            fg_color="#2e7d32", hover_color="#1b5e20", text_color="white",
                                            state="disabled", command=lambda: self.set_all_systems("ON"))
        self.btn_master_on.grid(row=master_row_1, column=7, columnspan=2, padx=3, pady=3, sticky="ew")

        self.btn_master_off = ctk.CTkButton(self.grid_container, text="MASTER All OFF", font=("Segoe UI", 14, "bold"),
                                             fg_color="#c62828", hover_color="#b71c1c", text_color="white",
                                             state="disabled", command=lambda: self.set_all_systems("OFF"))
        self.btn_master_off.grid(row=master_row_2, column=7, columnspan=2, padx=3, pady=3, sticky="ew")

        # TEC All ON/OFF aligned under Target TEC
        self.btn_tec_on = ctk.CTkButton(self.grid_container, text="TEC All ON", font=("Segoe UI", 13, "bold"),
                                         fg_color="#2e7d32", hover_color="#1b5e20", text_color="white",
                                         state="disabled", command=lambda: self.set_all_dropdowns("TEC", "ON"))
        self.btn_tec_on.grid(row=master_row_1, column=9, padx=3, pady=3, sticky="ew")

        self.btn_tec_off = ctk.CTkButton(self.grid_container, text="TEC All OFF", font=("Segoe UI", 13, "bold"),
                                          fg_color="#c62828", hover_color="#b71c1c", text_color="white",
                                          state="disabled", command=lambda: self.set_all_dropdowns("TEC", "OFF"))
        self.btn_tec_off.grid(row=master_row_2, column=9, padx=3, pady=3, sticky="ew")

        # LAS All ON/OFF aligned under Target LAS
        self.btn_las_on = ctk.CTkButton(self.grid_container, text="LAS All ON", font=("Segoe UI", 13, "bold"),
                                         fg_color="#2e7d32", hover_color="#1b5e20", text_color="white",
                                         state="disabled", command=lambda: self.set_all_dropdowns("LAS", "ON"))
        self.btn_las_on.grid(row=master_row_1, column=10, padx=3, pady=3, sticky="ew")

        self.btn_las_off = ctk.CTkButton(self.grid_container, text="LAS All OFF", font=("Segoe UI", 13, "bold"),
                                          fg_color="#c62828", hover_color="#b71c1c", text_color="white",
                                          state="disabled", command=lambda: self.set_all_dropdowns("LAS", "OFF"))
        self.btn_las_off.grid(row=master_row_2, column=10, padx=3, pady=3, sticky="ew")


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
        self.is_simulated = (port_choice == "Demo Simulator")

        if self.is_simulated:
            self.status_label.configure(text="Status: Demo Mode Active", text_color="#7b1fa2")
            self.btn_connect.configure(text="Disconnect", fg_color="#c62828", hover_color="#b71c1c")
            self.com_dropdown.configure(state="disabled")
            self.btn_scan.configure(state="normal")
            self.btn_clear_faults.configure(state="normal")
            return

        try:
            self.ser = serial.Serial(port_choice, baudrate=9600, timeout=5.0)
            self.ser.reset_input_buffer()
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
        self.btn_connect.configure(text="Connect", fg_color=None)
        self.com_dropdown.configure(state="normal")
        self.btn_scan.configure(state="disabled")
        self.btn_clear_faults.configure(state="disabled")
        self.btn_exec_all.configure(state="disabled")
        self.lock_ui("disabled")

        # Reset all rows to disconnected look
        for idx in range(self.num_channels):
            self.mark_empty(idx, "Disconnected")

    # --- Write and Read Command Wrappers ---
    def send_cmd(self, cmd):
        if self.is_simulated:
            self.process_sim_cmd(cmd)
        else:
            if self.ser:
                try:
                    self.ser.write(f"{cmd}\n".encode("ascii"))
                except serial.SerialException as e:
                    raise serial.SerialException(f"Hardware connection lost during write: {e}")

    def read_cmd(self):
        if self.is_simulated:
            return self.process_sim_query()
        else:
            if self.ser:
                try:
                    raw = self.ser.readline()
                except serial.SerialException as e:
                    raise serial.SerialException(f"Hardware connection lost during read: {e}")
                try:
                    return raw.decode("ascii").strip()
                except:
                    return ""
            return ""

    def query_cmd(self, cmd):
        # Flush stale data, send query, wait, read response
        if not self.is_simulated and self.ser:
            self.ser.reset_input_buffer()
        self.send_cmd(cmd)
        time.sleep(0.15)
        return self.read_cmd()

    def cmd_pause(self, cmd):
        self.send_cmd(cmd)
        time.sleep(0.15)

    # ----------------------------------------------------
    # SIMULATION MOCK PROCESSOR
    # ----------------------------------------------------
    def process_sim_cmd(self, cmd):
        parts = cmd.split()
        if not parts:
            return

        cmd_root = parts[0]
        
        if cmd == "CHAN?":
            self.sim_query_response = str(self.sim_state['curr_chan'])

        elif cmd_root == "CHAN":
            ch = int(parts[-1])
            if self.sim_state['is_installed'][ch - 1] == 0:
                self.sim_query_response = "ERROR"
            else:
                self.sim_state['curr_chan'] = ch

        elif cmd_root in ["TEC:T?", "TEC:SYNCT?"]:
            ch = self.sim_state['curr_chan']
            if self.sim_state['is_installed'][ch - 1] == 2:
                self.sim_query_response = "-10.5"
            else:
                self.sim_query_response = f"{self.sim_state['T_actual'][ch - 1]:.2f}"

        elif cmd_root == "TEC:T":
            if len(parts) > 1:
                ch = self.sim_state['curr_chan']
                try:
                    self.sim_state['T_actual'][ch - 1] = float(parts[-1])
                except:
                    pass

        elif cmd_root in ["LAS:LDI?", "LAS:SYNCLDI?"]:
            ch = self.sim_state['curr_chan']
            if self.sim_state['is_installed'][ch - 1] == 2:
                self.sim_query_response = "NaN"
            else:
                self.sim_query_response = f"{self.sim_state['I_actual'][ch - 1]:.2f}"

        elif cmd_root == "LAS:LDI":
            if len(parts) > 1:
                ch = self.sim_state['curr_chan']
                try:
                    self.sim_state['I_actual'][ch - 1] = float(parts[-1])
                except:
                    pass

        elif cmd == "TEC:OUT?":
            ch = self.sim_state['curr_chan']
            self.sim_query_response = str(self.sim_state['TEC_ON'][ch - 1])

        elif cmd_root == "TEC:OUTPUT":
            if len(parts) > 1:
                ch = self.sim_state['curr_chan']
                try:
                    self.sim_state['TEC_ON'][ch - 1] = int(parts[-1])
                except:
                    pass

        elif cmd == "LAS:OUT?":
            ch = self.sim_state['curr_chan']
            self.sim_query_response = str(self.sim_state['LAS_ON'][ch - 1])

        elif cmd_root == "LAS:OUTPUT":
            if len(parts) > 1:
                ch = self.sim_state['curr_chan']
                try:
                    self.sim_state['LAS_ON'][ch - 1] = int(parts[-1])
                except:
                    pass

        elif cmd == "LAS:LIM:I?":
            self.sim_query_response = "150"
        elif cmd == "TEC:LIM:THI?":
            self.sim_query_response = "80"
        elif cmd == "LAS:MOD?":
            ch = self.sim_state['curr_chan']
            self.sim_query_response = str(self.sim_state['LAS_MOD'][ch - 1])
        elif cmd_root == "LAS:MOD":
            if len(parts) > 1:
                ch = self.sim_state['curr_chan']
                try:
                    self.sim_state['LAS_MOD'][ch - 1] = int(parts[-1])
                except:
                    pass
        elif cmd == "MODERR?":
            self.sim_query_response = "0"

    def process_sim_query(self):
        return self.sim_query_response

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
                        # Card exists but diode floating/negative voltage = no laser attached
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
            ch["led"].set_color("#c62828")
        elif tec_out == 1 and las_out == 1:
            ch["status"].configure(text="TEC ON, LAS ON", text_color="#f57c00")
            ch["led"].set_color("#2e7d32")
        elif tec_out == 1:
            ch["status"].configure(text="TEC ON, LAS OFF", text_color="#f57c00")
            ch["led"].set_color("#f57c00")
        elif las_out == 1:
            ch["status"].configure(text="WARNING: LAS ON, TEC OFF", text_color="#c62828")
            ch["led"].set_color("#c62828")
        else:
            ch["status"].configure(text="Ready", text_color="#2e7d32")
            ch["led"].set_color("#2e7d32")

    def finish_channel_scan(self, cards_found):
        self.is_scanning = False
        if self.is_closing:
            self.do_cleanup_and_close()
            return

        if cards_found == 0:
            self.status_label.configure(text="WARNING: 0 slots responded. Check connection & power.", text_color="#f57c00")
        else:
            self.status_label.configure(text="Status: Scan Complete & Matched", text_color="#2e7d32")

        self.lock_ui("normal")
        self.btn_connect.configure(state="normal")

        # Start Telemetry thread if not active
        if not self.telemetry_active:
            self.telemetry_active = True
            self.telemetry_thread = threading.Thread(target=self.telemetry_loop, daemon=True)
            self.telemetry_thread.start()

    def mark_empty(self, idx, reason):
        ch = self.ch_ui[idx]
        ch["enable_chk"].configure(state="disabled")
        ch["enable_var"].set(False)
        ch["laser_label"].configure(state="disabled")
        
        ch["live_tec"].configure(fg_color=("#e0e0e0", "#3a3a3a"), text_color=("black", "#888888"))
        set_entry_val(ch["live_tec"], "OFF")
        ch["live_las"].configure(fg_color=("#e0e0e0", "#3a3a3a"), text_color=("black", "#888888"))
        set_entry_val(ch["live_las"], "OFF")

        ch["status"].configure(text=reason, text_color="#78909c")
        ch["led"].set_color("#444444")
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

        # P1 #6: EMO thread must NOT be a daemon — laser-off commands must complete
        # even if the main thread is exiting. Track it for join-on-close.
        self._emo_thread = threading.Thread(target=self.run_emergency_las_off_thread, daemon=False)
        self._emo_thread.start()

    def run_emergency_las_off_thread(self):
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
        ch["live_las"].configure(fg_color="#3a3a3a", text_color="#888888")
        set_entry_val(ch["live_las"], "OFF")
        ch["status"].configure(text="EMERGENCY OFF: Current Cut", text_color="#c62828")
        ch["led"].set_color("#c62828")

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

        # Prepare state variables
        self.is_executing = True
        self.is_stop_requested = False
        self.status_label.configure(text="Status: Sequence Running...", text_color="#2e7d32")

        self.lock_ui("disabled")
        self.btn_stop.configure(state="normal")
        self.btn_emerg.configure(state="normal")

        # Start sequence thread
        threading.Thread(target=self.run_sequence_thread, 
                         args=(channels_to_run, t_ramp, i_ramp, t_off_target), 
                         daemon=True).start()

    def run_sequence_thread(self, channels_to_run, t_ramp, i_ramp, t_off_target):
        # 1. Pre-calculate global estimated time of arrival (ETA) matching MATLAB
        self.total_estimated_time = 0.0
        
        TEC_ON_TIME = 1.0
        LAS_ON_TIME = 4.0
        LAS_OFF_TIME = 1.5
        TEC_OFF_TIME = 1.0

        for ch_num in channels_to_run:
            ch = self.ch_ui[ch_num - 1]
            try:
                curr_t = float(ch["cur_t"].get())
                curr_i = float(ch["cur_i"].get())
                t_targ = float(ch["t_target"].get())
                i_targ = float(ch["i_target"].get())
            except:
                curr_t, curr_i, t_targ, i_targ = 22.0, 0.0, 22.0, 0.0

            t_cmd = ch["tec_cmd"].get()
            l_cmd = ch["las_cmd"].get()
            live_t = ch["live_tec"].get()
            live_l = ch["live_las"].get()

            if live_t == "OFF" and live_l == "OFF":
                if t_cmd == "ON" and l_cmd == "OFF":
                    self.total_estimated_time += TEC_ON_TIME + abs(t_targ - curr_t) / t_ramp
                elif t_cmd == "ON" and l_cmd == "ON":
                    self.total_estimated_time += TEC_ON_TIME + abs(t_targ - curr_t) / t_ramp
                    self.total_estimated_time += LAS_ON_TIME + abs(i_targ - 0.0) / i_ramp
            elif live_t == "ON" and live_l == "OFF":
                if t_cmd == "ON" and l_cmd == "ON":
                    self.total_estimated_time += abs(t_targ - curr_t) / t_ramp
                    self.total_estimated_time += LAS_ON_TIME + abs(i_targ - 0.0) / i_ramp
                elif t_cmd == "OFF" and l_cmd == "OFF":
                    self.total_estimated_time += abs(t_off_target - curr_t) / t_ramp + TEC_OFF_TIME
                elif t_cmd == "ON" and l_cmd == "OFF":
                    self.total_estimated_time += abs(t_targ - curr_t) / t_ramp
            elif live_t == "ON" and live_l == "ON":
                if t_cmd == "ON" and l_cmd == "OFF":
                    self.total_estimated_time += abs(0.0 - curr_i) / i_ramp + LAS_OFF_TIME
                    self.total_estimated_time += abs(t_targ - curr_t) / t_ramp
                elif t_cmd == "OFF" and l_cmd == "OFF":
                    self.total_estimated_time += abs(0.0 - curr_i) / i_ramp + LAS_OFF_TIME
                    self.total_estimated_time += abs(t_off_target - curr_t) / t_ramp + TEC_OFF_TIME
                elif t_cmd == "ON" and l_cmd == "ON":
                    self.total_estimated_time += abs(t_targ - curr_t) / t_ramp
                    self.total_estimated_time += abs(i_targ - curr_i) / i_ramp

        self.sequence_start_time = time.time()

        # 2. Iterate channels and run ramps
        for ch_num in channels_to_run:
            if self.is_stop_requested:
                break

            ch = self.ch_ui[ch_num - 1]
            tec_on_off = ch["tec_cmd"].get()
            las_on_off = ch["las_cmd"].get()

            try:
                t_on_target = float(ch["t_target"].get())
                i_on_target = float(ch["i_target"].get())
            except ValueError:
                self.after(0, self.update_channel_status, ch_num - 1, "Invalid targets", "#c62828")
                continue

            try:
                # Lock serial during execution steps
                with self.serial_lock:
                    self.send_cmd(f"CHAN {ch_num}")
                    time.sleep(0.1)
                    
                    # Read hardware limits from controller
                    h_i_lim_str = self.query_cmd("LAS:LIM:I?")
                    try:
                        h_i_lim = float(h_i_lim_str)
                    except:
                        h_i_lim = 500.0

                    h_t_lim_str = self.query_cmd("TEC:LIM:THI?")
                    try:
                        h_t_lim = float(h_t_lim_str)
                    except:
                        h_t_lim = 80.0

                    # Safety Validation Checks
                    if tec_on_off == "ON" and not math.isnan(h_t_lim) and t_on_target > h_t_lim:
                        raise ValueError(f"Target T ({t_on_target:.1f}°C) exceeds limit ({h_t_lim:.1f}°C)")
                    
                    if las_on_off == "ON" and not math.isnan(h_i_lim) and i_on_target > h_i_lim:
                        raise ValueError(f"Target I ({i_on_target:.1f}mA) exceeds limit ({h_i_lim:.1f}mA)")

                    if tec_on_off == "OFF" and las_on_off == "ON":
                        raise ValueError("TEC must be ON for LAS to be ON.")

                    # Core execution logic
                    self.run_control_core(ch_num, tec_on_off, t_on_target, t_off_target, las_on_off, i_on_target, t_ramp, i_ramp)

                    # Final check for silent hardware error
                    has_err, err_str = self.check_controller_errors_threadsafe(ch_num)
                    if has_err:
                        raise RuntimeError(err_str)

            except Exception as e:
                # Handle sequence halt/error
                err_msg = str(e)
                if "HALT" in err_msg:
                    status_text = ch["status"].cget("text")
                    if status_text == "Initializing...":
                        self.after(0, self.update_channel_status, ch_num - 1, "HALTED (Before Ramp)", "#c62828")
                    else:
                        self.after(0, self.update_channel_status, ch_num - 1,
                                   f"HALTED at {ch['cur_t'].get()}°C, {ch['cur_i'].get()}mA", "#c62828")
                else:
                    # P2 #10: Show full error message — truncation hid critical fault info
                    self.after(0, self.update_channel_status, ch_num - 1, err_msg, "#c62828")
                    self.after(0, ch["led"].set_color, "#ff0000")
                    print(f"[Hardware Fault] Channel {ch_num}: {err_msg}")

                self.is_stop_requested = True
                # P3 #9: Triple bell matches MATLAB triple-beep for hardware fault alert
                self.bell()
                self.after(150, self.bell)
                self.after(300, self.bell)
                break

        # Finished Phase Cleanup
        self.is_executing = False
        self.after(0, self.lock_ui, "normal")
        self.after(0, lambda: self.btn_stop.configure(state="disabled"))

        if self.is_stop_requested:
            self.after(0, lambda: self.status_label.configure(text="Status: Hardware Halted & Pinned.", text_color="#c62828"))
        else:
            self.after(0, lambda: self.status_label.configure(text="Status: Sequence Complete & Settled.", text_color="#2e7d32"))
            if len(channels_to_run) > 1:
                self.bell()
                self.after(0, lambda: messagebox.showinfo("Done", "Sequence completed across all selected channels."))

        # Trigger telemetry immediately to synchronize button state
        self.run_telemetry_cycle()

    def run_control_core(self, ch_num, tec_on_off, t_on_target, t_off_target, las_on_off, i_on_target, t_ramp, i_ramp):
        idx = ch_num - 1
        ch = self.ch_ui[idx]
        
        self.after(0, self.update_channel_status, idx, "Initializing...", "#222222")
        self.after(0, ch["led"].set_color, "#ffdd00")

        # 1. Command Verification Alignment
        chan_curr = -1
        for retry in range(3):
            try:
                chan_curr = int(self.query_cmd("CHAN?"))
                if chan_curr == ch_num:
                    break
            except:
                pass
            self.safe_pause(0.15)
            self.cmd_pause(f"CHAN {ch_num}")

        if chan_curr != ch_num:
            raise RuntimeError(f"Ch. switch to {ch_num} timed out or failed.")

        self.cmd_pause("LAS:MOD 0")
        self.safe_pause(0.1)
        self.verify_hw_state("LAS:MOD?", 0, "Hardware failed to disable external modulation.")

        # 2. Read Current Status
        try:
            tec_curr_status = int(float(self.query_cmd("TEC:OUT?")))
        except:
            tec_curr_status = -1

        try:
            las_curr_status = int(float(self.query_cmd("LAS:OUT?")))
        except:
            las_curr_status = -1

        # Safety Routing State Machine Matching MATLAB
        if tec_curr_status == 0 and las_curr_status == 0:
            if tec_on_off == "ON" and las_on_off == "OFF":
                self.tec_temp_tset_tcurr()
                self.safe_pause(0.15)
                self.send_cmd("TEC:OUTPUT 1")
                self.safe_pause(0.2)
                self.verify_hw_state("TEC:OUT?", 1, "TEC ON acknowledge failed.")
                self.after(0, self.update_live_out_ui, idx, "TEC", "ON")
                self.safe_pause(0.15)
                self.ramp_temp(t_on_target, t_ramp, idx)

            elif tec_on_off == "ON" and las_on_off == "ON":
                self.tec_temp_tset_tcurr()
                self.safe_pause(0.15)
                self.send_cmd("TEC:OUTPUT 1")
                self.safe_pause(0.2)
                self.verify_hw_state("TEC:OUT?", 1, "TEC ON acknowledge failed.")
                self.after(0, self.update_live_out_ui, idx, "TEC", "ON")
                self.safe_pause(0.15)
                self.ramp_temp(t_on_target, t_ramp, idx)

                self.safe_pause(0.15)
                self.send_cmd("LAS:LDI 0.0")
                self.safe_pause(0.15)
                self.send_cmd("LAS:OUTPUT 1")
                self.safe_pause(0.2)
                self.verify_hw_state("LAS:OUT?", 1, "LAS ON acknowledge failed.")
                self.after(0, self.update_live_out_ui, idx, "LAS", "ON")
                self.safe_pause(2.5)  # Mandatory safety lock delay
                
                has_hw_err, hw_err_str = self.check_controller_errors_threadsafe(ch_num)
                if has_hw_err:
                    raise RuntimeError(hw_err_str)

                self.ramp_current(i_on_target, i_ramp, idx)

        elif tec_curr_status == 1 and las_curr_status == 0:
            if tec_on_off == "ON" and las_on_off == "ON":
                self.ramp_temp(t_on_target, t_ramp, idx)
                self.safe_pause(0.15)

                self.send_cmd("LAS:LDI 0.0")
                self.safe_pause(0.15)
                self.send_cmd("LAS:OUTPUT 1")
                self.safe_pause(0.2)
                self.verify_hw_state("LAS:OUT?", 1, "LAS ON acknowledge failed.")
                self.after(0, self.update_live_out_ui, idx, "LAS", "ON")
                self.safe_pause(0.5)
                
                has_hw_err, hw_err_str = self.check_controller_errors_threadsafe(ch_num)
                if has_hw_err:
                    raise RuntimeError(hw_err_str)

                self.ramp_current(i_on_target, i_ramp, idx)

            elif tec_on_off == "OFF" and las_on_off == "OFF":
                self.ramp_temp(t_off_target, t_ramp, idx)
                self.safe_pause(0.15)
                self.send_cmd("TEC:OUTPUT 0")
                self.safe_pause(0.2)
                self.verify_hw_state("TEC:OUT?", 0, "TEC OFF acknowledge failed.")
                self.after(0, self.update_live_out_ui, idx, "TEC", "OFF")
                self.safe_pause(0.5)

            elif tec_on_off == "ON" and las_on_off == "OFF":
                self.ramp_temp(t_on_target, t_ramp, idx)

        elif tec_curr_status == 1 and las_curr_status == 1:
            if tec_on_off == "ON" and las_on_off == "OFF":
                self.ramp_current(0.0, i_ramp, idx)
                self.safe_pause(0.15)
                self.send_cmd("LAS:OUTPUT 0")
                self.safe_pause(0.2)
                self.verify_hw_state("LAS:OUT?", 0, "LAS OFF acknowledge failed.")
                self.after(0, self.update_live_out_ui, idx, "LAS", "OFF")
                self.safe_pause(0.5)
                self.ramp_temp(t_on_target, t_ramp, idx)

            elif tec_on_off == "OFF" and las_on_off == "OFF":
                self.ramp_current(0.0, i_ramp, idx)
                self.safe_pause(0.15)
                self.send_cmd("LAS:OUTPUT 0")
                self.safe_pause(0.2)
                self.verify_hw_state("LAS:OUT?", 0, "LAS OFF acknowledge failed.")
                self.after(0, self.update_live_out_ui, idx, "LAS", "OFF")
                self.safe_pause(1.0)
                self.ramp_temp(t_off_target, t_ramp, idx)
                self.safe_pause(0.15)
                self.send_cmd("TEC:OUTPUT 0")
                self.safe_pause(0.2)
                self.verify_hw_state("TEC:OUT?", 0, "TEC OFF acknowledge failed.")
                self.after(0, self.update_live_out_ui, idx, "TEC", "OFF")
                self.safe_pause(0.5)

            elif tec_on_off == "ON" and las_on_off == "ON":
                self.ramp_temp(t_on_target, t_ramp, idx)
                self.safe_pause(0.15)
                self.ramp_current(i_on_target, i_ramp, idx)
        else:
            if tec_curr_status == 0 and las_curr_status == 1:
                self.after(0, self.update_channel_status, idx, "CRITICAL: Laser ON without TEC. Ramping down safely.", "#c62828")
                self.ramp_current(0.0, i_ramp, idx)
                self.safe_pause(0.15)
                self.send_cmd("LAS:OUTPUT 0")
                self.safe_pause(0.2)
                self.verify_hw_state("LAS:OUT?", 0, "LAS OFF acknowledge failed.")
                self.after(0, self.update_live_out_ui, idx, "LAS", "OFF")
                raise RuntimeError(f"CRITICAL FAULT CH {ch_num}: Laser ON while TEC OFF. Ramped down laser safely.")
            else:
                self.after(0, self.update_channel_status, idx, "Warning Status Collision. Skipping.", "#c62828")
                return

        if not self.is_stop_requested:
            self.final_check(ch_num)

    def tec_temp_tset_tcurr(self):
        t_curr_str = self.query_cmd("TEC:T?")
        try:
            t_curr = float(t_curr_str)
            if not math.isnan(t_curr):
                self.cmd_pause(f"TEC:T {t_curr:.2f}")
        except:
            pass

    def ramp_temp(self, t_target, t_ramp, idx):
        ch = self.ch_ui[idx]
        t_curr = None
        for retry in range(5):
            t_curr_str = self.query_cmd("TEC:SYNCT?")
            try:
                t_curr = float(t_curr_str)
                break
            except:
                self.safe_pause(0.15)

        if t_curr is None or math.isnan(t_curr):
            raise RuntimeError("Telemetry lost during initial Thermal readout.")

        if abs(t_curr - t_target) < 0.05:
            self.after(0, self.update_channel_status, idx, f"T at Target ({t_curr:.1f} °C)", "#2e7d32")
            return

        t_set = t_curr
        t_start = t_curr
        step = ((1 if t_target > t_curr else -1) * abs(t_ramp) * 0.5)

        t_fail_count = 0
        while abs(t_set - t_target) > 0.01:
            if self.is_stop_requested:
                raise RuntimeError("HALT")

            t_set += step
            if (step > 0 and t_set > t_target) or (step < 0 and t_set < t_target):
                t_set = t_target

            self.send_cmd(f"TEC:T {t_set:.2f}")

            # Safe high-responsiveness loop pause (total 0.5s)
            p_start = time.time()
            while time.time() - p_start < 0.5:
                self.update_eta()
                self.safe_pause(0.1)

            t_curr_str = self.query_cmd("TEC:SYNCT?")
            try:
                t_curr = float(t_curr_str)
                if math.isnan(t_curr):
                    raise ValueError("NaN")
                t_fail_count = 0
            except:
                t_fail_count += 1
                if t_fail_count > 3:
                    raise RuntimeError("Hardware communication lost during ramp. Stopping execution.")
                t_curr = t_set

            # Update entry box readout
            self.after(0, set_entry_val, ch["cur_t"], f"{t_curr:.1f}")

            # Draw progress bar text in status field
            pct = min(1.0, max(0.0, abs(t_curr - t_start) / max(0.01, abs(t_target - t_start))))
            num_blocks = round(pct * 10)
            prog_bar = '█' * num_blocks + '░' * (10 - num_blocks)
            
            self.after(0, self.update_channel_status, idx, f"[{prog_bar}] ({t_curr:.1f} °C)", "#1565c0")

    def ramp_current(self, i_target, i_ramp, idx):
        ch = self.ch_ui[idx]
        i_curr = None
        for retry in range(5):
            i_curr_str = self.query_cmd("LAS:SYNCLDI?")
            try:
                i_curr = float(i_curr_str)
                break
            except:
                self.safe_pause(0.15)

        if i_curr is None or math.isnan(i_curr):
            raise RuntimeError("Telemetry lost during initial Laser readout.")

        if abs(i_curr - i_target) < 0.05:
            self.after(0, self.update_channel_status, idx, f"I at Target ({i_curr:.1f} mA)", "#2e7d32")
            return

        i_set = i_curr
        i_start = i_curr
        step = ((1 if i_target > i_curr else -1) * abs(i_ramp) * 0.5)

        i_fail_count = 0
        while abs(i_set - i_target) > 0.01:
            if self.is_stop_requested:
                raise RuntimeError("HALT")

            i_set += step
            if (step > 0 and i_set > i_target) or (step < 0 and i_set < i_target):
                i_set = i_target

            self.send_cmd(f"LAS:LDI {i_set:.2f}")

            p_start = time.time()
            while time.time() - p_start < 0.5:
                self.update_eta()
                self.safe_pause(0.1)

            i_curr_str = self.query_cmd("LAS:SYNCLDI?")
            try:
                i_curr = float(i_curr_str)
                if math.isnan(i_curr):
                    raise ValueError("NaN")
                i_fail_count = 0
            except:
                i_fail_count += 1
                if i_fail_count > 3:
                    raise RuntimeError("Hardware communication lost during ramp. Stopping execution.")
                i_curr = i_set

            self.after(0, set_entry_val, ch["cur_i"], f"{i_curr:.1f}")

            # Draw progress bar text in status field
            pct = min(1.0, max(0.0, abs(i_curr - i_start) / max(0.01, abs(i_target - i_start))))
            num_blocks = round(pct * 10)
            prog_bar = '█' * num_blocks + '░' * (10 - num_blocks)
            
            self.after(0, self.update_channel_status, idx, f"[{prog_bar}] ({i_curr:.1f} mA)", "#7b1fa2")

    def final_check(self, ch_num):
        idx = ch_num - 1
        ch = self.ch_ui[idx]
        
        try:
            tec_stat = int(float(self.query_cmd("TEC:OUT?")))
        except:
            tec_stat = -1

        try:
            las_stat = int(float(self.query_cmd("LAS:OUT?")))
        except:
            las_stat = -1

        status_str = "Final Set: "
        if tec_stat == 1:
            status_str += "TEC ON, "
            self.after(0, self.update_live_out_ui, idx, "TEC", "ON")
        else:
            status_str += "TEC OFF, "
            self.after(0, self.update_live_out_ui, idx, "TEC", "OFF")

        if las_stat == 1:
            status_str += "LAS ON"
            self.after(0, self.update_live_out_ui, idx, "LAS", "ON")
        else:
            status_str += "LAS OFF"
            self.after(0, self.update_live_out_ui, idx, "LAS", "OFF")

        self.after(0, self.update_channel_status, idx, status_str, "#2e7d32")
        self.after(0, ch["led"].set_color, "#2e7d32")

        self.cmd_pause("LAS:MOD 1")

    # --- Verification & Error Helpers (Thread-safe) ---
    def safe_pause(self, t):
        time.sleep(t)
        if self.is_stop_requested:
            raise RuntimeError("HALT")

    def verify_hw_state(self, cmd, expected_val, err_msg):
        res = -1
        for retry in range(2):
            try:
                res_val = float(self.query_cmd(cmd))
                if not math.isnan(res_val) and int(res_val) == expected_val:
                    return
                res = res_val
            except:
                pass
            self.safe_pause(0.15)
        raise RuntimeError(f"{err_msg} (Expected {expected_val}, Got {res})")

    def check_controller_errors_threadsafe(self, ch_num):
        err_str = ""
        for retry in range(3):
            self.send_cmd(f"CHAN {ch_num}")
            time.sleep(0.1)
            
            if not self.is_simulated and self.ser:
                self.ser.reset_input_buffer()
            
            self.send_cmd("MODERR?")
            time.sleep(0.15)
            err_resp = self.read_cmd()
            err_str = err_resp.strip()

            if not err_str or err_str in ["0", "000", "00"]:
                return False, ""

            # Check for recognized hardware error strings
            if any(code in err_str for code in ["501", "504", "503", "505", "508", "511", "404", "407"]):
                break
            time.sleep(0.2)

        if not err_str or err_str in ["0", "000", "00"]:
            return False, ""

        if "501" in err_str:
            return True, f"Interlock Error (E501): Key switch is off. [{err_str}]"
        elif "504" in err_str:
            return True, f"Current Limit Reached (E504). [{err_str}]"
        elif "503" in err_str:
            return True, f"Voltage Limit Reached / Open Circuit (E503). [{err_str}]"
        elif "505" in err_str:
            return True, f"Voltage Limit Warning (E505). [{err_str}]"
        elif "508" in err_str:
            return True, f"TEC Off Status Forced LAS Off (E508). [{err_str}]"
        elif "511" in err_str:
            return True, f"Hardware Error (E511). [{err_str}]"
        elif "404" in err_str or "407" in err_str:
            return True, f"Temperature Limit Error. [{err_str}]"
        else:
            return True, f"Module Error Code: {err_str}"

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
