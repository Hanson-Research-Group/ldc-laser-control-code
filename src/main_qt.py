#!/usr/bin/env python3
"""
Newport LDC-3908 Laser Diode Controller — PySide6 / Qt front-end.

Qt GUI on top of the UI-agnostic control core:
  * laser_controller.LaserController — serial I/O, simulator, safety helpers
  * sequencer.Sequencer              — safety state machine + ramps

A QtBridge turns the engine's SequenceEvents callbacks (fired on worker threads)
into Qt signals delivered to the GUI thread via queued connections — the
idiomatic replacement for Tk's after().

Run:  python src/main_qt.py
"""

import json
import math
import os
import sys
import threading
import time

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtGui import QColor, QPalette, QPainter, QBrush, QIcon, QDoubleValidator
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QComboBox, QLineEdit,
    QCheckBox, QFrame, QHBoxLayout, QVBoxLayout, QGridLayout, QMessageBox,
    QFileDialog, QScrollArea, QButtonGroup,
)

import serial.tools.list_ports

from laser_controller import LaserController
from sequencer import Sequencer, SequenceEvents, ChannelPlan
import theme

PREF_FILE = os.path.join(os.path.expanduser("~"), ".ldc_laser_control_prefs.json")
CARD_W = 300  # logical px; approx one compact card width incl. margins


def resolve_path(rel):
    try:
        base = sys._MEIPASS
    except AttributeError:
        base = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(base, rel)


# ----------------------------------------------------------------------------
# Theme (Qt): map semantic status kinds to QColors, build a stylesheet + palette.
# ----------------------------------------------------------------------------
def status_hex(kind, dark):
    light_hex, dark_hex = theme.status(kind)
    return dark_hex if dark else light_hex


def make_palette(dark):
    pal = QPalette()
    if dark:
        window, base, text, alt = "#1f2124", "#2a2d31", "#e6e6e6", "#33373c"
    else:
        window, base, text, alt = "#f2f3f5", "#ffffff", "#1a1a1a", "#e9ebee"
    pal.setColor(QPalette.Window, QColor(window))
    pal.setColor(QPalette.Base, QColor(base))
    pal.setColor(QPalette.AlternateBase, QColor(alt))
    pal.setColor(QPalette.Text, QColor(text))
    pal.setColor(QPalette.WindowText, QColor(text))
    pal.setColor(QPalette.Button, QColor(alt))
    pal.setColor(QPalette.ButtonText, QColor(text))
    pal.setColor(QPalette.Highlight, QColor("#1976D2"))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.PlaceholderText, QColor("#888888"))
    return pal


def build_stylesheet(dark):
    card_bg = "#2a2d31" if dark else "#ffffff"
    card_border = "#3c4046" if dark else "#d6d9dd"
    field_bg = "#33373c" if dark else "#ffffff"
    field_border = "#464b52" if dark else "#c4c8cd"
    header_bg = "#26292d" if dark else "#eef0f2"
    muted = "#9aa0a6" if dark else "#6b7075"
    return f"""
    * {{ font-family: 'Segoe UI', 'Helvetica'; font-size: 13px; }}
    QFrame#card {{
        background: {card_bg};
        border: 1px solid {card_border};
        border-radius: 10px;
    }}
    QFrame#tableHeader {{
        background: {header_bg};
        border: 1px solid {card_border};
        border-radius: 8px;
    }}
    QLabel#chNum {{ font-size: 15px; font-weight: 600; }}
    QLabel#caption {{ color: {muted}; font-size: 11px; }}
    QLabel#hdr {{ font-weight: 600; }}
    QLabel#reading {{ font-size: 16px; }}
    QLineEdit, QComboBox {{
        background: {field_bg};
        border: 1px solid {field_border};
        border-radius: 6px;
        padding: 3px 6px;
        min-height: 20px;
    }}
    QLineEdit:disabled, QComboBox:disabled {{ color: {muted}; }}
    QLineEdit[invalid="true"] {{ border: 1px solid #e53935; }}
    QPushButton {{
        background: {field_bg};
        border: 1px solid {field_border};
        border-radius: 7px;
        padding: 6px 12px;
    }}
    QPushButton:hover:enabled {{ border-color: #1976D2; }}
    QPushButton:disabled {{ color: {muted}; }}
    QPushButton#seg {{ border-radius: 0px; padding: 5px 14px; }}
    QPushButton#seg:checked {{ background: #1976D2; color: white; border-color: #1976D2; }}
    QScrollArea {{ border: none; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; }}
    """


# ----------------------------------------------------------------------------
# LED indicator: a small painted dot (CTk had a canvas version).
# ----------------------------------------------------------------------------
class LedDot(QWidget):
    def __init__(self, size=16):
        super().__init__()
        self._size = size
        self._color = QColor(theme.led("idle"))
        self.setFixedSize(size, size)

    def set_kind(self, kind):
        self._color = QColor(theme.led(kind))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(self._color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(1, 1, self._size - 2, self._size - 2)


# ----------------------------------------------------------------------------
# Bridge: SequenceEvents (worker thread) -> Qt signals (GUI thread).
# ----------------------------------------------------------------------------
class QtBridge(QObject):
    status = Signal(int, str, str)
    led = Signal(int, str)
    liveOutput = Signal(int, str, str)
    liveValue = Signal(int, str, float)
    tick = Signal()
    halted = Signal(int)
    fault = Signal(int, str)
    post = Signal(object)


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


# Table-mode column spec: (title, min width). Status/Label stretch.
TABLE_COLS = [
    ("Ch", 40), ("On", 30), ("Label", 120), ("", 22), ("Status", 150),
    ("Live TEC", 64), ("Live LAS", 64), ("Live T", 60), ("Live I", 60),
    ("Tgt TEC", 68), ("Tgt LAS", 68), ("Tgt T", 60), ("Max", 42),
    ("Tgt I", 60), ("Max", 42), ("", 86),
]


class ChannelCard(QFrame):
    """One channel's controls. The SAME widgets are re-laid out for Table view
    (a full-width horizontal row) and Cards view (a compact vertical block)."""

    def __init__(self, idx, win):
        super().__init__()
        self.idx = idx
        self.win = win
        self.setObjectName("card")
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(10, 8, 10, 8)
        self._grid.setHorizontalSpacing(6)
        self._grid.setVerticalSpacing(4)

        ch = idx + 1
        self.num = QLabel(f"Ch {ch}"); self.num.setObjectName("chNum")
        self.led = LedDot()
        self.enable = QCheckBox(); self.enable.setEnabled(False)
        self.enable.stateChanged.connect(win._on_enable_changed)
        self.label = QLineEdit(f"Laser {ch}"); self.label.setEnabled(False)
        self.label.textEdited.connect(win._mark_unsaved)
        self.status = QLabel("Run Scan First"); self.status.setObjectName("status")
        self.live_tec = QLabel("OFF"); self.live_tec.setAlignment(Qt.AlignCenter)
        self.live_las = QLabel("OFF"); self.live_las.setAlignment(Qt.AlignCenter)
        self.live_t = QLabel("0.0"); self.live_t.setObjectName("reading"); self.live_t.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.live_i = QLabel("0.0"); self.live_i.setObjectName("reading"); self.live_i.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.tec_cmd = QComboBox(); self.tec_cmd.addItems(["ON", "OFF"]); self.tec_cmd.setCurrentText("OFF"); self.tec_cmd.setEnabled(False)
        self.tec_cmd.currentTextChanged.connect(win._mark_unsaved)
        self.las_cmd = QComboBox(); self.las_cmd.addItems(["ON", "OFF"]); self.las_cmd.setCurrentText("OFF"); self.las_cmd.setEnabled(False)
        self.las_cmd.currentTextChanged.connect(win._mark_unsaved)
        self.t_target = QLineEdit("22.0"); self.t_target.setEnabled(False); self.t_target.setValidator(QDoubleValidator())
        self.t_target.textEdited.connect(win._mark_unsaved)
        self.i_target = QLineEdit("0.0"); self.i_target.setEnabled(False); self.i_target.setValidator(QDoubleValidator())
        self.i_target.textEdited.connect(win._mark_unsaved)
        self.max_t = QLabel("-"); self.max_t.setObjectName("caption"); self.max_t.setAlignment(Qt.AlignCenter)
        self.max_i = QLabel("-"); self.max_i.setObjectName("caption"); self.max_i.setAlignment(Qt.AlignCenter)
        self.run = QPushButton("▶ Run Ch."); self.run.setEnabled(False)
        self.run.clicked.connect(lambda: win.execute_channels([ch]))

        # Captions shown only in Cards view (Table view has a shared header).
        self._caps = [QLabel(t) for t in
                      ("Live TEC", "Live LAS", "Live T °C", "Live I mA", "TEC", "LAS", "Target T", "Target I", "Max limit")]
        for c in self._caps:
            c.setObjectName("caption")
            c.setAlignment(Qt.AlignCenter)

        self._all = [self.num, self.led, self.enable, self.label, self.status,
                     self.live_tec, self.live_las, self.live_t, self.live_i,
                     self.tec_cmd, self.las_cmd, self.t_target, self.i_target,
                     self.max_t, self.max_i, self.run] + self._caps

    def _clear(self):
        for w in self._all:
            self._grid.removeWidget(w)
            w.setVisible(True)
        for c in range(max(16, self._grid.columnCount())):
            self._grid.setColumnStretch(c, 0)
            self._grid.setColumnMinimumWidth(c, 0)

    def set_table_mode(self):
        self._clear()
        for c in self._caps:
            c.setVisible(False)
        for c, (_, w) in enumerate(TABLE_COLS):
            self._grid.setColumnMinimumWidth(c, w)
        self._grid.setColumnStretch(4, 1)   # Status stretches
        g = self._grid
        g.addWidget(self.num, 0, 0)
        g.addWidget(self.enable, 0, 1, alignment=Qt.AlignCenter)
        g.addWidget(self.label, 0, 2)
        g.addWidget(self.led, 0, 3, alignment=Qt.AlignCenter)
        g.addWidget(self.status, 0, 4)
        g.addWidget(self.live_tec, 0, 5)
        g.addWidget(self.live_las, 0, 6)
        g.addWidget(self.live_t, 0, 7)
        g.addWidget(self.live_i, 0, 8)
        g.addWidget(self.tec_cmd, 0, 9)
        g.addWidget(self.las_cmd, 0, 10)
        g.addWidget(self.t_target, 0, 11)
        g.addWidget(self.max_t, 0, 12)
        g.addWidget(self.i_target, 0, 13)
        g.addWidget(self.max_i, 0, 14)
        g.addWidget(self.run, 0, 15)

    def set_card_mode(self):
        self._clear()
        for c in self._caps:
            c.setVisible(True)
        for c in range(4):
            self._grid.setColumnStretch(c, 1)
        g = self._grid
        (cap_ltec, cap_llas, cap_lt, cap_li, cap_tec, cap_las, cap_tt, cap_ti, cap_max) = self._caps
        g.addWidget(self.num, 0, 0)
        g.addWidget(self.label, 0, 1, 1, 2)
        g.addWidget(self.enable, 0, 3, alignment=Qt.AlignRight)
        g.addWidget(self.led, 1, 0, alignment=Qt.AlignCenter)
        g.addWidget(self.status, 1, 1, 1, 3)
        g.addWidget(cap_ltec, 2, 0); g.addWidget(cap_llas, 2, 1); g.addWidget(cap_lt, 2, 2); g.addWidget(cap_li, 2, 3)
        g.addWidget(self.live_tec, 3, 0); g.addWidget(self.live_las, 3, 1)
        g.addWidget(self.live_t, 3, 2); g.addWidget(self.live_i, 3, 3)
        g.addWidget(cap_tec, 4, 0); g.addWidget(cap_las, 4, 1); g.addWidget(cap_tt, 4, 2); g.addWidget(cap_ti, 4, 3)
        g.addWidget(self.tec_cmd, 5, 0); g.addWidget(self.las_cmd, 5, 1)
        g.addWidget(self.t_target, 5, 2); g.addWidget(self.i_target, 5, 3)
        g.addWidget(cap_max, 6, 1, alignment=Qt.AlignRight)
        g.addWidget(self.max_t, 6, 2); g.addWidget(self.max_i, 6, 3)
        g.addWidget(self.run, 7, 0, 1, 4)


class LDCMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.num_channels = 8
        self.ctl = LaserController(num_channels=self.num_channels)
        self.bridge = QtBridge()
        self.seq = Sequencer(self.ctl, events=QtSequenceEvents(self.bridge))

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
        self._unsaved = False
        self._table_mode = True
        self._reflow_cols = 0

        self.setWindowTitle("LDC-3908 Modular Laser Diode Controller — v0.4.0 (Qt)")
        self.resize(1500, 860)
        self.setMinimumSize(900, 620)
        for ext in ("src/laser_controller_icon.ico", "src/laser_controller_icon.png"):
            p = resolve_path(ext)
            if os.path.exists(p):
                self.setWindowIcon(QIcon(p))
                break

        self._build_ui()
        self._connect_bridge()
        self._apply_theme()
        self._set_view_mode(True)
        QTimer.singleShot(0, self._load_last_profile)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # --- Top connection bar ---
        top = QHBoxLayout()
        lbl = QLabel("COM Port:"); lbl.setObjectName("hdr")
        top.addWidget(lbl)
        self.com_combo = QComboBox(); self.com_combo.setMinimumWidth(190)
        self._refresh_ports()
        top.addWidget(self.com_combo)
        self.btn_refresh = QPushButton("↻"); self.btn_refresh.setFixedWidth(34)
        self.btn_refresh.clicked.connect(self._refresh_ports)
        top.addWidget(self.btn_refresh)
        self.btn_connect = QPushButton("Connect"); self.btn_connect.clicked.connect(self.connect_serial)
        top.addWidget(self.btn_connect)
        self.btn_scan = QPushButton("Scan Channels"); self.btn_scan.setEnabled(False)
        self.btn_scan.clicked.connect(self.start_scan)
        top.addWidget(self.btn_scan)
        self.btn_clear = QPushButton("Clear Faults"); self.btn_clear.setEnabled(False)
        self.btn_clear.clicked.connect(self.clear_faults)
        top.addWidget(self.btn_clear)
        top.addStretch(1)
        self.btn_theme = QPushButton("Dark"); self.btn_theme.setCheckable(True); self.btn_theme.setFixedWidth(64)
        self.btn_theme.clicked.connect(self.toggle_theme)
        top.addWidget(self.btn_theme)
        self.status_label = QLabel("Status: Disconnected")
        self.status_label.setObjectName("hdr")
        top.addWidget(self.status_label)
        root.addLayout(top)

        # --- Controls bar: title, view toggle, show-unused, master overrides ---
        bar = QHBoxLayout()
        title = QLabel("Channel Configuration & Live Telemetry"); title.setObjectName("hdr")
        title.setStyleSheet("font-size: 15px;")
        bar.addWidget(title)
        bar.addStretch(1)

        self.btn_view_table = QPushButton("Table"); self.btn_view_table.setObjectName("seg"); self.btn_view_table.setCheckable(True); self.btn_view_table.setChecked(True)
        self.btn_view_cards = QPushButton("Cards"); self.btn_view_cards.setObjectName("seg"); self.btn_view_cards.setCheckable(True)
        grp = QButtonGroup(self); grp.setExclusive(True); grp.addButton(self.btn_view_table); grp.addButton(self.btn_view_cards)
        self.btn_view_table.clicked.connect(lambda: self._set_view_mode(True))
        self.btn_view_cards.clicked.connect(lambda: self._set_view_mode(False))
        seg = QHBoxLayout(); seg.setSpacing(0); seg.addWidget(self.btn_view_table); seg.addWidget(self.btn_view_cards)
        bar.addLayout(seg)
        bar.addSpacing(14)

        self.chk_show_unused = QCheckBox("Show unused")
        self.chk_show_unused.stateChanged.connect(self._apply_visibility)
        bar.addWidget(self.chk_show_unused)
        bar.addSpacing(14)

        self._master_buttons = []
        for text, fn, kind in [("All ON", lambda: self.set_all_systems("ON"), "green"),
                               ("All OFF", lambda: self.set_all_systems("OFF"), "red"),
                               ("TEC On", lambda: self.set_all_dropdowns("TEC", "ON"), ""),
                               ("TEC Off", lambda: self.set_all_dropdowns("TEC", "OFF"), ""),
                               ("LAS On", lambda: self.set_all_dropdowns("LAS", "ON"), ""),
                               ("LAS Off", lambda: self.set_all_dropdowns("LAS", "OFF"), "")]:
            b = QPushButton(text); b.setEnabled(False); b.clicked.connect(fn)
            if kind == "green":
                b.setStyleSheet("background:#2e7d32; color:white; font-weight:600;")
            elif kind == "red":
                b.setStyleSheet("background:#c62828; color:white; font-weight:600;")
            bar.addWidget(b)
            self._master_buttons.append(b)
        root.addLayout(bar)

        # --- Scroll area with the reflowing card container + table header ---
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.cards_grid = QGridLayout(self.container)
        self.cards_grid.setContentsMargins(4, 4, 4, 4)
        self.cards_grid.setHorizontalSpacing(8)
        self.cards_grid.setVerticalSpacing(8)
        self.cards_grid.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.container)
        root.addWidget(self.scroll, 1)

        self.table_header = self._build_table_header()
        self.cards = [ChannelCard(i, self) for i in range(self.num_channels)]

        # --- Bottom panel: ramp params, profile, run controls ---
        bottom = QGridLayout()
        bottom.setHorizontalSpacing(8)
        for r, (lab, attr, default) in enumerate([("T Ramp (°C/s):", "t_ramp", "0.1"),
                                                  ("I Ramp (mA/s):", "i_ramp", "0.5")]):
            L = QLabel(lab); L.setObjectName("hdr"); bottom.addWidget(L, r, 0)
            e = QLineEdit(default); e.setFixedWidth(90); e.setValidator(QDoubleValidator())
            e.textEdited.connect(self._mark_unsaved)
            setattr(self, attr, e); bottom.addWidget(e, r, 1)
        Lo = QLabel("T OFF Target (°C):"); Lo.setObjectName("hdr"); bottom.addWidget(Lo, 0, 2)
        self.t_off = QLineEdit("22.0"); self.t_off.setFixedWidth(90); self.t_off.setValidator(QDoubleValidator())
        self.t_off.textEdited.connect(self._mark_unsaved)
        bottom.addWidget(self.t_off, 0, 3)

        self.btn_save = QPushButton("💾 Save Profile"); self.btn_save.clicked.connect(self.save_profile)
        bottom.addWidget(self.btn_save, 2, 0, 1, 2)
        self.btn_load = QPushButton("📂 Load Profile"); self.btn_load.clicked.connect(self.load_profile)
        bottom.addWidget(self.btn_load, 2, 2)
        self.btn_clear_prof = QPushButton("❌ Clear Profile"); self.btn_clear_prof.clicked.connect(self.clear_profile)
        bottom.addWidget(self.btn_clear_prof, 2, 3)
        self.lbl_profile = QLabel("Active Profile: [Unsaved]"); self.lbl_profile.setObjectName("caption")
        bottom.addWidget(self.lbl_profile, 2, 4)
        bottom.setColumnStretch(4, 1)

        self.btn_run_all = QPushButton("▶ RUN ALL"); self.btn_run_all.setEnabled(False)
        self.btn_run_all.setMinimumHeight(54)
        self.btn_run_all.setStyleSheet("background:#2e7d32; color:white; font-size:16px; font-weight:bold; border-radius:8px;")
        self.btn_run_all.clicked.connect(self.execute_all)
        bottom.addWidget(self.btn_run_all, 0, 6, 2, 1)
        self.btn_stop = QPushButton("⏹ CANCEL RUN (Safe)"); self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet("background:#c62828; color:white; font-weight:bold; border-radius:8px;")
        self.btn_stop.clicked.connect(self.stop_execution)
        bottom.addWidget(self.btn_stop, 2, 6)
        self.btn_emo = QPushButton("⚠\nEMO\nOFF"); self.btn_emo.setEnabled(False)
        self.btn_emo.setFixedWidth(84)
        self.btn_emo.setStyleSheet("background:#c62828; color:white; font-weight:bold; border-radius:8px;")
        self.btn_emo.clicked.connect(self.emergency_las_off)
        bottom.addWidget(self.btn_emo, 0, 5, 3, 1)
        root.addLayout(bottom)

    def _build_table_header(self):
        h = QFrame(); h.setObjectName("tableHeader")
        g = QGridLayout(h); g.setContentsMargins(10, 4, 10, 4); g.setHorizontalSpacing(6)
        for c, (title, w) in enumerate(TABLE_COLS):
            g.setColumnMinimumWidth(c, w)
            lab = QLabel(title); lab.setObjectName("caption")
            g.addWidget(lab, 0, c)
        g.setColumnStretch(4, 1)
        return h

    def _connect_bridge(self):
        self.bridge.status.connect(self._on_status)
        self.bridge.led.connect(self._on_led)
        self.bridge.liveOutput.connect(self._on_live_output)
        self.bridge.liveValue.connect(self._on_live_value)
        self.bridge.tick.connect(self.update_eta)
        self.bridge.halted.connect(self._on_halted)
        self.bridge.fault.connect(self._on_fault)
        self.bridge.post.connect(lambda fn: fn())

    def _post(self, fn, *args):
        self.bridge.post.emit(lambda: fn(*args))

    # ------------------------------------------------------------------
    # View mode + reflow + visibility
    # ------------------------------------------------------------------
    def _set_view_mode(self, table):
        self._table_mode = table
        for card in self.cards:
            card.set_table_mode() if table else card.set_card_mode()
        self._relayout(force=True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    def _shown(self):
        if self.chk_show_unused.isChecked() or not self.has_scanned:
            return list(range(self.num_channels))
        return [i for i in range(self.num_channels)
                if self.populated[i] and self.cards[i].enable.isChecked()]

    def _relayout(self, force=False):
        if not getattr(self, "cards", None):
            return
        shown = self._shown()
        if self._table_mode:
            cols = 1
        else:
            vw = self.scroll.viewport().width()
            cols = max(1, min(vw // CARD_W, max(1, len(shown))))
        key = (self._table_mode, cols, tuple(shown))
        if not force and key == getattr(self, "_relayout_key", None):
            return
        self._relayout_key = key

        # detach everything
        self.table_header.setParent(None)
        for card in self.cards:
            card.setParent(None)
        while self.cards_grid.count():
            self.cards_grid.takeAt(0)
        for c in range(max(4, self.cards_grid.columnCount())):
            self.cards_grid.setColumnStretch(c, 0)

        if self._table_mode:
            self.cards_grid.setColumnStretch(0, 1)
            self.cards_grid.addWidget(self.table_header, 0, 0)
            self.table_header.setParent(self.container); self.table_header.show()
            for pos, i in enumerate(shown):
                self.cards_grid.addWidget(self.cards[i], pos + 1, 0)
                self.cards[i].setParent(self.container); self.cards[i].show()
        else:
            self.table_header.hide()
            for c in range(cols):
                self.cards_grid.setColumnStretch(c, 1)
            for pos, i in enumerate(shown):
                r, c = divmod(pos, cols)
                self.cards_grid.addWidget(self.cards[i], r, c)
                self.cards[i].setParent(self.container); self.cards[i].show()

    def _apply_visibility(self, *_):
        self._relayout(force=True)

    def _on_enable_changed(self, *_):
        self._relayout(force=True)

    # ------------------------------------------------------------------
    # Bridge slots
    # ------------------------------------------------------------------
    def _on_status(self, idx, text, kind):
        s = self.cards[idx].status
        s.setText(text)
        s.setStyleSheet(f"color: {status_hex(kind, self.dark)};")

    def _on_led(self, idx, kind):
        self.cards[idx].led.set_kind(kind)

    def _on_live_output(self, idx, kind, state):
        w = self.cards[idx].live_tec if kind == "TEC" else self.cards[idx].live_las
        w.setText(state)
        if state == "ON":
            w.setStyleSheet("background:#2e7d32; color:white; border-radius:6px; padding:2px;")
        else:
            muted_bg = "#33373c" if self.dark else "#e6e6e6"
            muted_fg = "#9aa0a6" if self.dark else "#606060"
            w.setStyleSheet(f"background:{muted_bg}; color:{muted_fg}; border-radius:6px; padding:2px;")

    def _on_live_value(self, idx, kind, value):
        (self.cards[idx].live_t if kind == "T" else self.cards[idx].live_i).setText(f"{value:.1f}")

    def _on_halted(self, idx):
        c = self.cards[idx]
        self._on_status(idx, f"HALTED at {c.live_t.text()}°C, {c.live_i.text()}mA", "fault")
        self._triple_bell()

    def _on_fault(self, idx, message):
        self._on_status(idx, message, "fault")
        self._on_led(idx, "fault")
        print(f"[Hardware Fault] Channel {idx + 1}: {message}")
        self._triple_bell()

    def _triple_bell(self):
        QApplication.beep()
        QTimer.singleShot(150, QApplication.beep)
        QTimer.singleShot(300, QApplication.beep)

    # ------------------------------------------------------------------
    # Connection / ports
    # ------------------------------------------------------------------
    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        ports.append("Demo Simulator")
        self.com_combo.clear(); self.com_combo.addItems(ports)
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
            self._set_status("Status: Demo Mode Active")
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
        self._relayout(force=True)

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
                    if not t_str or math.isnan(t_val) or t_val < 0:
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

    def _update_after_scan(self, i, t_val, i_val, tec_out, las_out, max_t, max_i, has_err, err):
        self.populated[i] = True
        c = self.cards[i]
        c.enable.setEnabled(True); c.enable.setChecked(True)
        c.label.setEnabled(True)
        for w in (c.tec_cmd, c.las_cmd, c.t_target, c.i_target, c.run):
            w.setEnabled(True)
        c.tec_cmd.setCurrentText("ON" if tec_out == 1 else "OFF")
        c.las_cmd.setCurrentText("ON" if las_out == 1 else "OFF")
        c.live_t.setText(f"{t_val:.1f}"); c.live_i.setText(f"{i_val:.1f}")
        c.max_t.setText(f"{max_t:.0f}"); c.max_i.setText(f"{max_i:.0f}")
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
        c = self.cards[i]
        c.enable.setChecked(False); c.enable.setEnabled(False)
        c.label.setEnabled(False)
        for w in (c.tec_cmd, c.las_cmd, c.t_target, c.i_target, c.run):
            w.setEnabled(False)
        self._on_live_output(i, "TEC", "OFF"); self._on_live_output(i, "LAS", "OFF")
        c.live_t.setText("0.0"); c.live_i.setText("0.0")
        self._on_status(i, reason, "muted"); self._on_led(i, "empty")

    def _finish_scan(self, cards):
        self.is_scanning = False
        if self.is_closing:
            self.close(); return
        self._set_status("Status: Scan Complete & Matched" if cards else
                         "WARNING: 0 slots responded. Check connection & power.")
        self.has_scanned = True
        self._relayout(force=True)
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
                    if self.telemetry_fail_count > 3:
                        self.telemetry_active = False
                        self._post(self._handle_connection_loss)
                        break
            self._post(self.btn_emo.setEnabled, any_on)

    def _telemetry_update(self, i, t_str, i_str, tec, las):
        try:
            self.cards[i].live_t.setText(f"{float(t_str):.1f}")
        except Exception:
            pass
        try:
            self.cards[i].live_i.setText(f"{float(i_str):.1f}")
        except Exception:
            pass
        if tec is not None:
            self._on_live_output(i, "TEC", "ON" if tec == 1 else "OFF")
        if las is not None:
            self._on_live_output(i, "LAS", "ON" if las == 1 else "OFF")

    def _handle_connection_loss(self):
        self._set_status("Status: Connection Lost")
        self._lock_controls(False)
        self.btn_connect.setText("Connect")
        self.ctl.ser = None
        self.ctl.is_stop_requested = True
        QMessageBox.critical(self, "Connection Lost",
                             "Hardware communication lost. Execution halted. Lasers not explicitly shut off.")

    # ------------------------------------------------------------------
    # Master overrides
    # ------------------------------------------------------------------
    def set_all_dropdowns(self, kind, state):
        for c in self.cards:
            w = c.tec_cmd if kind == "TEC" else c.las_cmd
            if w.isEnabled():
                w.setCurrentText(state)

    def set_all_systems(self, state):
        for c in self.cards:
            if c.tec_cmd.isEnabled():
                c.tec_cmd.setCurrentText(state)
            if c.las_cmd.isEnabled():
                c.las_cmd.setCurrentText(state)

    # ------------------------------------------------------------------
    # Sequence execution
    # ------------------------------------------------------------------
    def execute_all(self):
        chans = [i + 1 for i in range(self.num_channels)
                 if self.cards[i].enable.isChecked() and self.cards[i].enable.isEnabled()]
        if not chans:
            QMessageBox.warning(self, "Warning", 'No channels are marked "On" for the sequence!')
            return
        self.execute_channels(chans)

    def execute_channels(self, chans):
        if self.is_executing:
            return
        try:
            t_ramp = float(self.t_ramp.text()); i_ramp = float(self.i_ramp.text()); t_off = float(self.t_off.text())
        except ValueError:
            QMessageBox.critical(self, "Invalid Configuration", "Ramp speeds and off target must be numbers.")
            return
        if t_ramp <= 0 or i_ramp <= 0:
            QMessageBox.critical(self, "Invalid Configuration", "Ramp speeds must be > 0.")
            return

        plans = []
        for ch_num in chans:
            c = self.cards[ch_num - 1]
            try:
                tt = float(c.t_target.text()); ti = float(c.i_target.text()); valid = True
            except ValueError:
                tt, ti, valid = 0.0, 0.0, False
            plans.append(ChannelPlan(idx=ch_num - 1, ch_num=ch_num,
                                     tec_cmd=c.tec_cmd.currentText(), las_cmd=c.las_cmd.currentText(),
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
            c = self.cards[p.idx]
            try:
                ct = float(c.live_t.text()); ci = float(c.live_i.text())
            except Exception:
                ct, ci = 22.0, 0.0
            lt, ll = c.live_tec.text(), c.live_las.text()
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
            self._triple_bell()

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
    # Locking, theme, profiles
    # ------------------------------------------------------------------
    def _lock_controls(self, enabled):
        for i in range(self.num_channels):
            if not self.populated[i]:
                continue
            c = self.cards[i]
            for w in (c.enable, c.label, c.tec_cmd, c.las_cmd, c.t_target, c.i_target, c.run):
                w.setEnabled(enabled)
        for b in self._master_buttons:
            b.setEnabled(enabled)
        self.btn_run_all.setEnabled(enabled)
        self.btn_scan.setEnabled(enabled)
        for w in (self.t_ramp, self.i_ramp, self.t_off, self.btn_save, self.btn_load, self.btn_clear_prof):
            w.setEnabled(enabled)

    def toggle_theme(self):
        self.dark = self.btn_theme.isChecked()
        self.btn_theme.setText("Light" if self.dark else "Dark")
        self._apply_theme()

    def _apply_theme(self):
        app = QApplication.instance()
        app.setPalette(make_palette(self.dark))
        app.setStyleSheet(build_stylesheet(self.dark))
        # Re-apply dynamic per-widget colors that the stylesheet doesn't cover.
        for i in range(self.num_channels):
            self._on_live_output(i, "TEC", self.cards[i].live_tec.text())
            self._on_live_output(i, "LAS", self.cards[i].live_las.text())

    def _mark_unsaved(self, *_):
        if not self._unsaved:
            self._unsaved = True
            t = self.lbl_profile.text()
            if not t.startswith("* "):
                self.lbl_profile.setText("* " + t)
                self.lbl_profile.setStyleSheet("color:#f57c00;")

    def _set_profile_label(self, text):
        self._unsaved = False
        self.lbl_profile.setText(text)
        self.lbl_profile.setStyleSheet("")

    def save_profile(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Profile", "", "Text files (*.txt)")
        if not path:
            return
        try:
            data = {"COM_Port": self.com_combo.currentText(),
                    "T_ramp": float(self.t_ramp.text()), "I_ramp": float(self.i_ramp.text()),
                    "T_OFF_Target": float(self.t_off.text()), "channels": []}
            for c in self.cards:
                data["channels"].append({"T_Target": float(c.t_target.text() or 0),
                                         "I_Target": float(c.i_target.text() or 0),
                                         "Label": c.label.text()})
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            self.active_profile_path = path
            self._save_pref(path)
            self._set_profile_label(f"Active Profile: {os.path.basename(path)}")
            QMessageBox.information(self, "Profile Saved", "Configuration profile saved.")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"Failed to save profile:\n{e}")

    def load_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Profile", "", "Text files (*.txt)")
        if path:
            self._load_profile_file(path, announce=True)

    def _load_profile_file(self, path, announce=False):
        try:
            with open(path) as f:
                data = json.load(f)
            self.t_ramp.setText(str(data["T_ramp"]))
            self.i_ramp.setText(str(data["I_ramp"]))
            self.t_off.setText(str(data["T_OFF_Target"]))
            for k, cfg in enumerate(data["channels"][:self.num_channels]):
                self.cards[k].t_target.setText(str(cfg["T_Target"]))
                self.cards[k].i_target.setText(str(cfg["I_Target"]))
                self.cards[k].label.setText(cfg.get("Label", f"Laser {k + 1}"))
            self.active_profile_path = path
            self._save_pref(path)
            self._set_profile_label(f"Active Profile: {os.path.basename(path)}")
            if announce:
                QMessageBox.information(self, "Profile Loaded",
                                       f'Profile "{os.path.basename(path)}" loaded.')
        except Exception as e:
            QMessageBox.critical(self, "Load Error", f"Failed to load profile:\n{e}")

    def clear_profile(self):
        if QMessageBox.question(self, "Confirm Clear",
                                "Clear the active profile and reset all settings to defaults?") != QMessageBox.Yes:
            return
        self.t_ramp.setText("0.1"); self.i_ramp.setText("0.5"); self.t_off.setText("22.0")
        for k, c in enumerate(self.cards):
            c.t_target.setText("22.0"); c.i_target.setText("0.0"); c.label.setText(f"Laser {k + 1}")
        try:
            if os.path.exists(PREF_FILE):
                os.remove(PREF_FILE)
        except Exception:
            pass
        self.active_profile_path = ""
        self._set_profile_label("Active Profile: [Default/Cleared]")

    def _save_pref(self, path):
        try:
            with open(PREF_FILE, "w") as f:
                json.dump({"LastProfilePath": path}, f)
        except Exception as e:
            print(f"Error saving preferences: {e}")

    def _load_last_profile(self):
        if os.path.exists(PREF_FILE):
            try:
                with open(PREF_FILE) as f:
                    last = json.load(f).get("LastProfilePath", "")
                if last and os.path.isfile(last):
                    self._load_profile_file(last)
            except Exception as e:
                print(f"Error loading preferences: {e}")

    # ------------------------------------------------------------------
    def closeEvent(self, event):
        if self.is_executing:
            if QMessageBox.question(self, "Sequence Running",
                                    "A sequence is running. Stop it now?") == QMessageBox.Yes:
                self.stop_execution()
            event.ignore()
            return
        if self._unsaved:
            resp = QMessageBox.question(self, "Unsaved Changes",
                                        "You have unsaved profile changes. Save before exiting?",
                                        QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if resp == QMessageBox.Cancel:
                event.ignore(); return
            if resp == QMessageBox.Yes:
                self.save_profile()
                if self._unsaved:  # save cancelled
                    event.ignore(); return
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
