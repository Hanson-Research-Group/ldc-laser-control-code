#!/usr/bin/env python3
"""
Newport LDC-3908 Laser Diode Controller — PySide6 / Qt front-end.

This is the Qt port of the GUI. It reuses the UI-agnostic control core verbatim:
  * laser_controller.LaserController — serial I/O, simulator, safety helpers
  * sequencer.Sequencer              — safety state machine + ramps

The only new piece is the Qt view + a QtBridge that turns the engine's
SequenceEvents callbacks (fired on worker threads) into Qt signals, which Qt
delivers to the GUI thread via queued connections — the idiomatic replacement
for the Tk after() marshalling used by the CustomTkinter app (main.py).

Run:  python src/main_qt.py
"""

import json
import os
import sys
import threading
import time

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtGui import QColor, QPalette, QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QComboBox, QLineEdit,
    QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView, QHBoxLayout, QVBoxLayout,
    QGridLayout, QFrame, QMessageBox, QFileDialog, QSizePolicy, QAbstractItemView,
)

import serial.tools.list_ports

from laser_controller import LaserController
from sequencer import Sequencer, SequenceEvents, ChannelPlan
import theme


# ----------------------------------------------------------------------------
# Theme helpers (Qt): map the engine's semantic status kinds to QColors, and
# provide light/dark Fusion palettes.
# ----------------------------------------------------------------------------
def status_qcolor(kind, dark):
    light_hex, dark_hex = theme.status(kind)
    return QColor(dark_hex if dark else light_hex)


def led_qcolor(kind):
    return QColor(theme.led(kind))


def make_palette(dark):
    pal = QPalette()
    if dark:
        bg, base, text, alt = QColor("#242424"), QColor("#2d2d2d"), QColor("#e6e6e6"), QColor("#323232")
    else:
        bg, base, text, alt = QColor("#f0f0f0"), QColor("#ffffff"), QColor("#1a1a1a"), QColor("#e8e8e8")
    pal.setColor(QPalette.Window, bg)
    pal.setColor(QPalette.Base, base)
    pal.setColor(QPalette.AlternateBase, alt)
    pal.setColor(QPalette.Text, text)
    pal.setColor(QPalette.WindowText, text)
    pal.setColor(QPalette.Button, alt)
    pal.setColor(QPalette.ButtonText, text)
    pal.setColor(QPalette.Highlight, QColor("#1976D2"))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    return pal


# ----------------------------------------------------------------------------
# Bridge: SequenceEvents (worker thread) -> Qt signals (GUI thread).
# ----------------------------------------------------------------------------
class QtBridge(QObject):
    status = Signal(int, str, str)       # idx, text, kind
    led = Signal(int, str)               # idx, kind
    liveOutput = Signal(int, str, str)   # idx, "TEC"/"LAS", "ON"/"OFF"
    liveValue = Signal(int, str, float)  # idx, "T"/"I", value
    tick = Signal()
    halted = Signal(int)
    fault = Signal(int, str)
    post = Signal(object)                # generic "run this callable on the GUI thread"


class QtSequenceEvents(SequenceEvents):
    def __init__(self, bridge):
        self.b = bridge

    def on_status(self, idx, text, kind):
        self.b.status.emit(idx, text, kind)

    def on_led(self, idx, kind):
        self.b.led.emit(idx, kind)

    def on_live_output(self, idx, kind, state):
        self.b.liveOutput.emit(idx, kind, state)

    def on_live_value(self, idx, kind, value):
        self.b.liveValue.emit(idx, kind, float(value))

    def on_tick(self):
        self.b.tick.emit()

    def on_channel_halted(self, idx):
        self.b.halted.emit(idx)

    def on_channel_fault(self, idx, message):
        self.b.fault.emit(idx, message)


# Column indices for the channel table.
COL_CH, COL_EN, COL_LABEL, COL_STATUS, COL_LTEC, COL_LLAS, COL_LT, COL_LI, \
    COL_TTEC, COL_TLAS, COL_TT, COL_MAXT, COL_TI, COL_MAXI, COL_RUN = range(15)

HEADERS = ["Ch", "On", "Label", "Status", "Live TEC", "Live LAS", "Live T °C",
           "Live I mA", "Tgt TEC", "Tgt LAS", "Tgt T", "Max T", "Tgt I", "Max I", ""]


class LDCMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.num_channels = 8
        self.ctl = LaserController(num_channels=self.num_channels)

        self.bridge = QtBridge()
        self.seq = Sequencer(self.ctl, events=QtSequenceEvents(self.bridge))

        # UI/exec state (mirrors the CTk app's flags).
        self.is_executing = False
        self.is_scanning = False
        self.is_closing = False
        self.has_scanned = False
        self.populated = [False] * self.num_channels
        self.total_estimated_time = 0.0
        self.sequence_start_time = None
        self.telemetry_active = False
        self.telemetry_thread = None
        self.telemetry_fail_count = 0
        self._emo_thread = None
        self.active_profile_path = ""
        self.dark = False

        self.setWindowTitle("LDC-3908 Modular Laser Diode Controller — Qt v0.4.0")
        self.resize(1500, 850)
        self.setMinimumSize(900, 600)

        self._build_ui()
        self._connect_bridge()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- Top connection bar ---
        top = QHBoxLayout()
        top.addWidget(QLabel("COM Port:"))
        self.com_combo = QComboBox()
        self.com_combo.setMinimumWidth(200)
        self._refresh_ports()
        top.addWidget(self.com_combo)
        self.btn_refresh = QPushButton("↻")
        self.btn_refresh.setFixedWidth(36)
        self.btn_refresh.clicked.connect(self._refresh_ports)
        top.addWidget(self.btn_refresh)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self.connect_serial)
        top.addWidget(self.btn_connect)
        self.btn_scan = QPushButton("Scan Channels")
        self.btn_scan.setEnabled(False)
        self.btn_scan.clicked.connect(self.start_scan)
        top.addWidget(self.btn_scan)
        self.btn_clear = QPushButton("Clear Faults")
        self.btn_clear.setEnabled(False)
        self.btn_clear.clicked.connect(self.clear_faults)
        top.addWidget(self.btn_clear)
        top.addStretch(1)
        self.chk_show_unused = QCheckBox("Show unused")
        self.chk_show_unused.stateChanged.connect(self._apply_row_visibility)
        top.addWidget(self.chk_show_unused)
        self.btn_theme = QPushButton("Dark")
        self.btn_theme.setCheckable(True)
        self.btn_theme.setFixedWidth(70)
        self.btn_theme.clicked.connect(self.toggle_theme)
        top.addWidget(self.btn_theme)
        self.status_label = QLabel("Status: Disconnected")
        self.status_label.setStyleSheet("font-weight: bold;")
        top.addWidget(self.status_label)
        root.addLayout(top)

        # --- Master override bar ---
        master = QHBoxLayout()
        master.addWidget(QLabel("Master:"))
        for text, fn in [("All ON", lambda: self.set_all_systems("ON")),
                         ("All OFF", lambda: self.set_all_systems("OFF")),
                         ("TEC On", lambda: self.set_all_dropdowns("TEC", "ON")),
                         ("TEC Off", lambda: self.set_all_dropdowns("TEC", "OFF")),
                         ("LAS On", lambda: self.set_all_dropdowns("LAS", "ON")),
                         ("LAS Off", lambda: self.set_all_dropdowns("LAS", "OFF"))]:
            b = QPushButton(text)
            b.setEnabled(False)
            b.clicked.connect(fn)
            master.addWidget(b)
            self._master_buttons = getattr(self, "_master_buttons", [])
            self._master_buttons.append(b)
        master.addStretch(1)
        root.addLayout(master)

        # --- Channel table ---
        self.table = QTableWidget(self.num_channels, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(COL_STATUS, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_LABEL, QHeaderView.Stretch)
        for c in range(len(HEADERS)):
            if c not in (COL_STATUS, COL_LABEL):
                hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.rows = []
        for i in range(self.num_channels):
            self.rows.append(self._build_row(i))
        self.table.resizeColumnsToContents()
        root.addWidget(self.table, 1)

        # --- Bottom panel: ramp params + profile + run controls ---
        bottom = QGridLayout()
        bottom.addWidget(QLabel("T Ramp (°C/s):"), 0, 0)
        self.t_ramp = QLineEdit("0.1"); self.t_ramp.setFixedWidth(80)
        bottom.addWidget(self.t_ramp, 0, 1)
        bottom.addWidget(QLabel("I Ramp (mA/s):"), 1, 0)
        self.i_ramp = QLineEdit("0.5"); self.i_ramp.setFixedWidth(80)
        bottom.addWidget(self.i_ramp, 1, 1)
        bottom.addWidget(QLabel("T OFF Target (°C):"), 0, 2)
        self.t_off = QLineEdit("22.0"); self.t_off.setFixedWidth(80)
        bottom.addWidget(self.t_off, 0, 3)

        self.btn_save = QPushButton("💾 Save Profile"); self.btn_save.clicked.connect(self.save_profile)
        bottom.addWidget(self.btn_save, 2, 0, 1, 2)
        self.btn_load = QPushButton("📂 Load Profile"); self.btn_load.clicked.connect(self.load_profile)
        bottom.addWidget(self.btn_load, 2, 2, 1, 2)
        self.lbl_profile = QLabel("Active Profile: [Unsaved]")
        bottom.addWidget(self.lbl_profile, 2, 4)

        bottom.setColumnStretch(4, 1)

        self.btn_run_all = QPushButton("▶ RUN ALL")
        self.btn_run_all.setEnabled(False)
        self.btn_run_all.setStyleSheet("background-color:#2e7d32; color:white; font-weight:bold; padding:10px;")
        self.btn_run_all.clicked.connect(self.execute_all)
        bottom.addWidget(self.btn_run_all, 0, 6, 2, 1)
        self.btn_stop = QPushButton("⏹ CANCEL RUN (Safe)")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet("background-color:#c62828; color:white; font-weight:bold;")
        self.btn_stop.clicked.connect(self.stop_execution)
        bottom.addWidget(self.btn_stop, 2, 6)
        self.btn_emo = QPushButton("⚠ EMO OFF")
        self.btn_emo.setEnabled(False)
        self.btn_emo.setStyleSheet("background-color:#c62828; color:white; font-weight:bold; padding:10px;")
        self.btn_emo.clicked.connect(self.emergency_las_off)
        bottom.addWidget(self.btn_emo, 0, 5, 3, 1)
        root.addLayout(bottom)

        self._apply_theme()

    def _build_row(self, i):
        ch_idx = i + 1
        t = self.table

        def item(text, editable=False):
            it = QTableWidgetItem(text)
            it.setTextAlignment(Qt.AlignCenter)
            return it

        t.setItem(i, COL_CH, item(str(ch_idx)))
        chk = QCheckBox()
        chk.setEnabled(False)
        chk.stateChanged.connect(self._apply_row_visibility)
        chk_wrap = QWidget(); lay = QHBoxLayout(chk_wrap); lay.addWidget(chk)
        lay.setAlignment(Qt.AlignCenter); lay.setContentsMargins(0, 0, 0, 0)
        t.setCellWidget(i, COL_EN, chk_wrap)

        label = QLineEdit(f"Laser {ch_idx}"); label.setEnabled(False)
        t.setCellWidget(i, COL_LABEL, label)

        t.setItem(i, COL_STATUS, item("Run Scan First"))
        t.setItem(i, COL_LTEC, item("OFF"))
        t.setItem(i, COL_LLAS, item("OFF"))
        t.setItem(i, COL_LT, item("0.0"))
        t.setItem(i, COL_LI, item("0.0"))

        tec = QComboBox(); tec.addItems(["ON", "OFF"]); tec.setCurrentText("OFF"); tec.setEnabled(False)
        t.setCellWidget(i, COL_TTEC, tec)
        las = QComboBox(); las.addItems(["ON", "OFF"]); las.setCurrentText("OFF"); las.setEnabled(False)
        t.setCellWidget(i, COL_TLAS, las)
        tt = QLineEdit("22.0"); tt.setEnabled(False); tt.setFixedWidth(64)
        t.setCellWidget(i, COL_TT, tt)
        t.setItem(i, COL_MAXT, item("-"))
        ti = QLineEdit("0.0"); ti.setEnabled(False); ti.setFixedWidth(64)
        t.setCellWidget(i, COL_TI, ti)
        t.setItem(i, COL_MAXI, item("-"))

        run = QPushButton("▶ Run"); run.setEnabled(False)
        run.clicked.connect(lambda _=False, ch=ch_idx: self.execute_channels([ch]))
        t.setCellWidget(i, COL_RUN, run)

        return {'enable': chk, 'label': label, 'tec_cmd': tec, 'las_cmd': las,
                't_target': tt, 'i_target': ti, 'run': run}

    def _connect_bridge(self):
        self.bridge.status.connect(self._on_status)
        self.bridge.led.connect(self._on_led)
        self.bridge.liveOutput.connect(self._on_live_output)
        self.bridge.liveValue.connect(self._on_live_value)
        self.bridge.tick.connect(self.update_eta)
        self.bridge.halted.connect(self._on_halted)
        self.bridge.fault.connect(self._on_fault)
        self.bridge.post.connect(lambda fn: fn())

    # ------------------------------------------------------------------
    # Bridge slots (run on the GUI thread)
    # ------------------------------------------------------------------
    def _set_item(self, row, col, text):
        it = self.table.item(row, col)
        if it is not None:
            it.setText(text)

    def _on_status(self, idx, text, kind):
        it = self.table.item(idx, COL_STATUS)
        if it is not None:
            it.setText(text)
            it.setForeground(status_qcolor(kind, self.dark))

    def _on_led(self, idx, kind):
        # Represent the LED as a colored dot in the Ch cell.
        it = self.table.item(idx, COL_CH)
        if it is not None:
            it.setForeground(led_qcolor(kind))
            it.setText(f"● {idx + 1}")

    def _on_live_output(self, idx, kind, state):
        col = COL_LTEC if kind == "TEC" else COL_LLAS
        self._set_item(idx, col, state)
        it = self.table.item(idx, col)
        if it is not None:
            it.setForeground(QColor("#2e7d32") if state == "ON" else status_qcolor("muted", self.dark))

    def _on_live_value(self, idx, kind, value):
        self._set_item(idx, COL_LT if kind == "T" else COL_LI, f"{value:.1f}")

    def _on_halted(self, idx):
        self._on_status(idx, f"HALTED at {self.table.item(idx, COL_LT).text()}°C, "
                             f"{self.table.item(idx, COL_LI).text()}mA", "fault")
        QApplication.beep()

    def _on_fault(self, idx, message):
        self._on_status(idx, message, "fault")
        self._on_led(idx, "fault")
        print(f"[Hardware Fault] Channel {idx + 1}: {message}")
        QApplication.beep()

    # ------------------------------------------------------------------
    # Connection / ports
    # ------------------------------------------------------------------
    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        ports.append("Demo Simulator")
        self.com_combo.clear()
        self.com_combo.addItems(ports)
        real = [p for p in ports if p != "Demo Simulator"]
        self.com_combo.setCurrentText(real[0] if real else "Demo Simulator")

    def _set_status(self, text):
        self.status_label.setText(text)

    def connect_serial(self):
        if self.is_executing:
            self._set_status("Status: PRESS STOP BEFORE DISCONNECTING!")
            return
        if self.btn_connect.text() == "Disconnect":
            self.disconnect_serial()
            return
        choice = self.com_combo.currentText()
        if choice == "Demo Simulator":
            self.ctl.open_simulator()
        else:
            try:
                self.ctl.open(choice, baudrate=9600, timeout=5.0)
            except Exception as e:
                QMessageBox.critical(self, "Connection Error", f"Failed to connect to {choice}:\n{e}")
                self._set_status("Status: Connection Failed")
                return
        self._set_status("Status: Connected (Ready)")
        self.btn_connect.setText("Disconnect")
        self.com_combo.setEnabled(False)
        self.btn_scan.setEnabled(True)
        self.btn_clear.setEnabled(True)

    def disconnect_serial(self):
        self.telemetry_active = False
        if self.telemetry_thread and self.telemetry_thread.is_alive():
            self.telemetry_thread.join(timeout=1.0)
        self.ctl.close()
        self._set_status("Status: Disconnected")
        self.btn_connect.setText("Connect")
        self.com_combo.setEnabled(True)
        self.btn_scan.setEnabled(False)
        self.btn_clear.setEnabled(False)
        self.btn_run_all.setEnabled(False)
        self._lock_controls(False)
        self.has_scanned = False
        for i in range(self.num_channels):
            self._mark_empty(i, "Disconnected")
        self._apply_row_visibility()

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------
    def start_scan(self):
        self.is_scanning = True
        self.btn_scan.setEnabled(False)
        self._set_status("Status: Scanning active hardware...")
        threading.Thread(target=self._run_scan, daemon=True).start()

    def _run_scan(self):
        cards = 0
        for k in range(self.num_channels):
            if self.is_closing or not self.ctl.is_connected:
                break
            ch_num = k + 1
            with self.ctl.serial_lock:
                try:
                    self.ctl.cmd_pause(f"CHAN {ch_num}")
                    time.sleep(0.1)
                    ans = self.ctl.query_cmd("CHAN?")
                    try:
                        ans_i = int(ans)
                    except Exception:
                        ans_i = -1
                    if not ans or ans_i != ch_num:
                        self._post(self._mark_empty, k, "Empty Slot")
                        continue
                    cards += 1
                    t_str = self.ctl.query_cmd("TEC:T?")
                    try:
                        t_val = float(t_str)
                    except Exception:
                        t_val = float('nan')
                    i_str = self.ctl.query_cmd("LAS:LDI?")
                    try:
                        i_val = float(i_str)
                    except Exception:
                        i_val = 0.0
                    try:
                        tec_out = int(float(self.ctl.query_cmd("TEC:OUT?")))
                        las_out = int(float(self.ctl.query_cmd("LAS:OUT?")))
                    except Exception:
                        tec_out = las_out = 0
                    import math
                    if not t_str or math.isnan(t_val) or t_val < 0:
                        # Sub-zero / floating thermistor -> no diode attached. Valid
                        # for this lab's above-0 °C use only (see main.py note).
                        self._post(self._mark_empty, k, "No Laser Attached")
                        continue
                    has_err, err = self.ctl.check_controller_errors(ch_num)
                    try:
                        max_t = float(self.ctl.query_cmd("TEC:LIM:THI?"))
                    except Exception:
                        max_t = 99.0
                    try:
                        max_i = float(self.ctl.query_cmd("LAS:LIM:I?"))
                    except Exception:
                        max_i = 500.0
                    self.ctl.cmd_pause("LAS:MOD 1")
                    self._post(self._update_after_scan, k, t_val, i_val, tec_out, las_out,
                               max_t, max_i, has_err, err)
                except Exception as e:
                    print(f"Scan error ch{ch_num}: {e}")
                    self._post(self._mark_empty, k, "Empty Slot")
        self._post(self._finish_scan, cards)

    def _post(self, fn, *args):
        """Marshal a call onto the GUI thread. Uses a Qt signal (thread-safe from
        worker threads) rather than QTimer.singleShot (GUI-thread only)."""
        self.bridge.post.emit(lambda: fn(*args))

    def _update_after_scan(self, i, t_val, i_val, tec_out, las_out, max_t, max_i, has_err, err):
        self.populated[i] = True
        r = self.rows[i]
        r['enable'].setEnabled(True); r['enable'].setChecked(True)
        r['label'].setEnabled(True)
        for w in ('tec_cmd', 'las_cmd', 't_target', 'i_target', 'run'):
            r[w].setEnabled(True)
        r['tec_cmd'].setCurrentText("ON" if tec_out == 1 else "OFF")
        r['las_cmd'].setCurrentText("ON" if las_out == 1 else "OFF")
        self._set_item(i, COL_LT, f"{t_val:.1f}")
        self._set_item(i, COL_LI, f"{i_val:.1f}")
        self._set_item(i, COL_MAXT, f"{max_t:.0f}")
        self._set_item(i, COL_MAXI, f"{max_i:.0f}")
        self._on_live_output(i, "TEC", "ON" if tec_out == 1 else "OFF")
        self._on_live_output(i, "LAS", "ON" if las_out == 1 else "OFF")
        if has_err:
            self._on_status(i, f"FAULT: {err}", "fault"); self._on_led(i, "fault")
        elif tec_out == 1 and las_out == 1:
            self._on_status(i, "TEC ON, LAS ON", "warn"); self._on_led(i, "ok")
        elif tec_out == 1:
            self._on_status(i, "TEC ON, LAS OFF", "warn"); self._on_led(i, "warn")
        elif las_out == 1:
            self._on_status(i, "WARNING: LAS ON, TEC OFF", "fault"); self._on_led(i, "fault")
        else:
            self._on_status(i, "Ready", "ok"); self._on_led(i, "ok")

    def _mark_empty(self, i, reason):
        self.populated[i] = False
        r = self.rows[i]
        r['enable'].setChecked(False); r['enable'].setEnabled(False)
        r['label'].setEnabled(False)
        for w in ('tec_cmd', 'las_cmd', 't_target', 'i_target', 'run'):
            r[w].setEnabled(False)
        self._set_item(i, COL_LTEC, "OFF"); self._set_item(i, COL_LLAS, "OFF")
        self._set_item(i, COL_LT, "0.0"); self._set_item(i, COL_LI, "0.0")
        self._on_status(i, reason, "muted"); self._on_led(i, "empty")

    def _finish_scan(self, cards):
        self.is_scanning = False
        if self.is_closing:
            self.close()
            return
        self._set_status("Status: Scan Complete & Matched" if cards else
                         "WARNING: 0 slots responded. Check connection & power.")
        self.has_scanned = True
        self._apply_row_visibility()
        self._lock_controls(True)
        self.btn_scan.setEnabled(True)
        self.btn_run_all.setEnabled(True)
        if not self.telemetry_active:
            self.telemetry_active = True
            self.telemetry_thread = threading.Thread(target=self._telemetry_loop, daemon=True)
            self.telemetry_thread.start()

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------
    def _telemetry_loop(self):
        while self.telemetry_active:
            if not self.is_executing and not self.is_scanning and not self.is_closing:
                self._telemetry_cycle()
            time.sleep(2.0)

    def _telemetry_cycle(self):
        with self.ctl.serial_lock:
            if not self.ctl.is_connected:
                return
            any_on = False
            for k in range(self.num_channels):
                if self.is_executing or self.is_scanning or self.is_closing:
                    break
                if not self.populated[k]:
                    continue
                ch_num = k + 1
                try:
                    self.ctl.cmd_pause(f"CHAN {ch_num}")
                    t_str = self.ctl.query_cmd("TEC:T?")
                    i_str = self.ctl.query_cmd("LAS:LDI?")
                    try:
                        tec = int(float(self.ctl.query_cmd("TEC:OUT?")))
                        las = int(float(self.ctl.query_cmd("LAS:OUT?")))
                    except Exception:
                        tec = las = None
                    self._post(self._telemetry_update, k, t_str, i_str, tec, las)
                    if las == 1:
                        any_on = True
                    self.telemetry_fail_count = 0
                except Exception as e:
                    print(f"Telemetry fail ch{ch_num}: {e}")
                    self.telemetry_fail_count += 1
            self._post(self.btn_emo.setEnabled, any_on)

    def _telemetry_update(self, i, t_str, i_str, tec, las):
        try:
            self._set_item(i, COL_LT, f"{float(t_str):.1f}")
        except Exception:
            pass
        try:
            self._set_item(i, COL_LI, f"{float(i_str):.1f}")
        except Exception:
            pass
        if tec is not None:
            self._on_live_output(i, "TEC", "ON" if tec == 1 else "OFF")
        if las is not None:
            self._on_live_output(i, "LAS", "ON" if las == 1 else "OFF")

    # ------------------------------------------------------------------
    # Master overrides
    # ------------------------------------------------------------------
    def set_all_dropdowns(self, kind, state):
        for r in self.rows:
            w = r['tec_cmd'] if kind == "TEC" else r['las_cmd']
            if w.isEnabled():
                w.setCurrentText(state)

    def set_all_systems(self, state):
        for r in self.rows:
            if r['tec_cmd'].isEnabled():
                r['tec_cmd'].setCurrentText(state)
            if r['las_cmd'].isEnabled():
                r['las_cmd'].setCurrentText(state)

    # ------------------------------------------------------------------
    # Sequence execution
    # ------------------------------------------------------------------
    def execute_all(self):
        chans = [i + 1 for i in range(self.num_channels)
                 if self.rows[i]['enable'].isChecked() and self.rows[i]['enable'].isEnabled()]
        if not chans:
            QMessageBox.warning(self, "Warning", 'No channels are marked "On" for the sequence!')
            return
        self.execute_channels(chans)

    def execute_channels(self, chans):
        if self.is_executing:
            return
        try:
            t_ramp = float(self.t_ramp.text()); i_ramp = float(self.i_ramp.text())
            t_off = float(self.t_off.text())
        except ValueError:
            QMessageBox.critical(self, "Invalid Configuration", "Ramp speeds and off target must be numbers.")
            return
        if t_ramp <= 0 or i_ramp <= 0:
            QMessageBox.critical(self, "Invalid Configuration", "Ramp speeds must be > 0.")
            return

        plans = []
        for ch_num in chans:
            r = self.rows[ch_num - 1]
            try:
                tt = float(r['t_target'].text()); ti = float(r['i_target'].text()); valid = True
            except ValueError:
                tt, ti, valid = 0.0, 0.0, False
            plans.append(ChannelPlan(idx=ch_num - 1, ch_num=ch_num,
                                     tec_cmd=r['tec_cmd'].currentText(), las_cmd=r['las_cmd'].currentText(),
                                     t_target=tt, i_target=ti, targets_valid=valid))

        self.total_estimated_time = self._estimate_time(plans, t_ramp, i_ramp, t_off)
        self.is_executing = True
        self.ctl.is_stop_requested = False
        self.ctl.is_emo_requested = False
        self._set_status("Status: Sequence Running...")
        self._lock_controls(False)
        self.btn_stop.setEnabled(True)
        self.btn_emo.setEnabled(True)
        threading.Thread(target=self._run_sequence, args=(plans, t_ramp, i_ramp, t_off), daemon=True).start()

    def _estimate_time(self, plans, t_ramp, i_ramp, t_off):
        TEC_ON, LAS_ON, LAS_OFF, TEC_OFF = 1.0, 4.0, 1.5, 1.0
        total = 0.0
        for p in plans:
            r = self.rows[p.idx]
            try:
                ct = float(self.table.item(p.idx, COL_LT).text())
                ci = float(self.table.item(p.idx, COL_LI).text())
            except Exception:
                ct, ci = 22.0, 0.0
            lt = self.table.item(p.idx, COL_LTEC).text()
            ll = self.table.item(p.idx, COL_LLAS).text()
            tc, lc = p.tec_cmd, p.las_cmd
            if lt == "OFF" and ll == "OFF":
                if tc == "ON" and lc == "OFF":
                    total += TEC_ON + abs(p.t_target - ct) / t_ramp
                elif tc == "ON" and lc == "ON":
                    total += TEC_ON + abs(p.t_target - ct) / t_ramp + LAS_ON + abs(p.i_target) / i_ramp
            elif lt == "ON" and ll == "OFF":
                if tc == "ON" and lc == "ON":
                    total += abs(p.t_target - ct) / t_ramp + LAS_ON + abs(p.i_target) / i_ramp
                elif tc == "OFF" and lc == "OFF":
                    total += abs(t_off - ct) / t_ramp + TEC_OFF
                elif tc == "ON" and lc == "OFF":
                    total += abs(p.t_target - ct) / t_ramp
            elif lt == "ON" and ll == "ON":
                if tc == "ON" and lc == "OFF":
                    total += abs(ci) / i_ramp + LAS_OFF + abs(p.t_target - ct) / t_ramp
                elif tc == "OFF" and lc == "OFF":
                    total += abs(ci) / i_ramp + LAS_OFF + abs(t_off - ct) / t_ramp + TEC_OFF
                elif tc == "ON" and lc == "ON":
                    total += abs(p.t_target - ct) / t_ramp + abs(p.i_target - ci) / i_ramp
        return total

    def _run_sequence(self, plans, t_ramp, i_ramp, t_off):
        self.sequence_start_time = time.time()
        self.seq.run(plans, t_ramp, i_ramp, t_off)
        self._post(self._finish_sequence, len(plans))

    def _finish_sequence(self, n):
        self.is_executing = False
        self._lock_controls(True)
        self.btn_stop.setEnabled(False)
        if self.ctl.is_emo_requested:
            self._perform_emergency_shutdown()
            self.ctl.is_emo_requested = False
        elif self.ctl.is_stop_requested:
            self._set_status("Status: Hardware Halted & Pinned.")
        else:
            self._set_status("Status: Sequence Complete & Settled.")
            if n > 1:
                QMessageBox.information(self, "Done", "Sequence completed across all selected channels.")
        threading.Thread(target=self._telemetry_cycle, daemon=True).start()

    def update_eta(self):
        if not self.sequence_start_time:
            return
        rem = max(0.0, self.total_estimated_time - (time.time() - self.sequence_start_time))
        if rem > 0:
            self._set_status(f"Status: Sequence Running... (Time Remaining: {int(rem // 60):02d}:{int(rem % 60):02d})")
        else:
            self._set_status("Status: Sequence Running... (Finishing up)")

    # ------------------------------------------------------------------
    # Safety shutoffs
    # ------------------------------------------------------------------
    def stop_execution(self):
        if self.is_executing:
            self.ctl.is_stop_requested = True
            self._set_status("Status: STOP COMMANDED. Halting safely...")
            QApplication.beep()

    def emergency_las_off(self):
        if QMessageBox.question(self, "Emergency LAS OFF",
                                "Immediately cutting current without ramping can damage the diode. Proceed?") \
                != QMessageBox.Yes:
            return
        self.ctl.is_stop_requested = True
        self.ctl.is_emo_requested = True
        if self.is_executing:
            self._set_status("Status: EMERGENCY OFF — cutting laser...")
            return
        self._emo_thread = threading.Thread(target=self._perform_emergency_shutdown, daemon=False)
        self._emo_thread.start()

    def _perform_emergency_shutdown(self):
        with self.ctl.serial_lock:
            for k in range(self.num_channels):
                if not self.populated[k]:
                    continue
                try:
                    self.ctl.send_cmd(f"CHAN {k + 1}")
                    time.sleep(0.15)
                    self.ctl.send_cmd("LAS:OUTPUT 0")
                    self._post(self._on_status, k, "EMERGENCY OFF: Current Cut", "fault")
                    self._post(self._on_led, k, "fault")
                except Exception:
                    pass
        self._post(self._set_status, "Status: EMERGENCY LASER SHUTDOWN TRIGGERED.")

    def clear_faults(self):
        self._set_status("Status: Clearing chassis faults...")

        def run():
            with self.ctl.serial_lock:
                for k in range(self.num_channels):
                    if not self.populated[k]:
                        continue
                    try:
                        self.ctl.send_cmd(f"CHAN {k + 1}"); time.sleep(0.1)
                        self.ctl.send_cmd("*CLS"); time.sleep(0.1)
                    except Exception:
                        pass
            self.is_scanning = True
            self._run_scan()
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------
    # Visibility, locking, theme, profiles
    # ------------------------------------------------------------------
    def _apply_row_visibility(self, *_):
        show_all = self.chk_show_unused.isChecked() or not self.has_scanned
        for i in range(self.num_channels):
            visible = show_all or (self.populated[i] and self.rows[i]['enable'].isChecked())
            self.table.setRowHidden(i, not visible)

    def _lock_controls(self, enabled):
        for i in range(self.num_channels):
            if not self.populated[i]:
                continue
            r = self.rows[i]
            for w in ('enable', 'label', 'tec_cmd', 'las_cmd', 't_target', 'i_target', 'run'):
                r[w].setEnabled(enabled)
        for b in getattr(self, "_master_buttons", []):
            b.setEnabled(enabled)
        self.btn_run_all.setEnabled(enabled)
        self.btn_scan.setEnabled(enabled)
        for w in (self.t_ramp, self.i_ramp, self.t_off, self.btn_save, self.btn_load):
            w.setEnabled(enabled)

    def toggle_theme(self):
        self.dark = self.btn_theme.isChecked()
        self.btn_theme.setText("Light" if self.dark else "Dark")
        self._apply_theme()

    def _apply_theme(self):
        QApplication.instance().setPalette(make_palette(self.dark))

    def save_profile(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Profile", "", "Text files (*.txt)")
        if not path:
            return
        data = {"COM_Port": self.com_combo.currentText(),
                "T_ramp": float(self.t_ramp.text()), "I_ramp": float(self.i_ramp.text()),
                "T_OFF_Target": float(self.t_off.text()), "channels": []}
        for r in self.rows:
            data["channels"].append({"T_Target": float(r['t_target'].text() or 0),
                                     "I_Target": float(r['i_target'].text() or 0),
                                     "Label": r['label'].text()})
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            self.active_profile_path = path
            self.lbl_profile.setText(f"Active Profile: {os.path.basename(path)}")
            QMessageBox.information(self, "Profile Saved", "Configuration profile saved.")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    def load_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Profile", "", "Text files (*.txt)")
        if not path:
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self.t_ramp.setText(str(data["T_ramp"]))
            self.i_ramp.setText(str(data["I_ramp"]))
            self.t_off.setText(str(data["T_OFF_Target"]))
            for k, cfg in enumerate(data["channels"][:self.num_channels]):
                self.rows[k]['t_target'].setText(str(cfg["T_Target"]))
                self.rows[k]['i_target'].setText(str(cfg["I_Target"]))
                self.rows[k]['label'].setText(cfg.get("Label", f"Laser {k + 1}"))
            self.active_profile_path = path
            self.lbl_profile.setText(f"Active Profile: {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "Load Error", f"Failed to load profile:\n{e}")

    def closeEvent(self, event):
        if self.is_executing:
            if QMessageBox.question(self, "Sequence Running",
                                    "A sequence is running. Stop and close?") == QMessageBox.Yes:
                self.stop_execution()
            event.ignore()
            return
        self.is_closing = True
        self.telemetry_active = False
        if self.telemetry_thread and self.telemetry_thread.is_alive():
            self.telemetry_thread.join(timeout=1.0)
        if self._emo_thread and self._emo_thread.is_alive():
            self._emo_thread.join(timeout=3.0)
        self.ctl.close()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = LDCMainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
