#!/usr/bin/env python3
"""
Laser Controller Console — PySide6 / Qt front-end.

Control software for laser diode controllers. The GUI is device-agnostic: it
drives a controller through a driver (driver.LaserControllerDriver) and the
ramp/sequence engine (sequencer.Sequencer). The only controller supported today
is the ILX Lightwave LDC-3908 (ldc3908.LDC3908Driver); adding another is a matter
of writing a new driver.

A QtBridge turns the engine's SequenceEvents callbacks (fired on worker threads)
into Qt signals delivered to the GUI thread via queued connections.

Run:  python src/main.py
"""

import json
import math
import os
import sys
import threading
import time

from PySide6.QtCore import Qt, QObject, Signal, QTimer, QMimeData, QPoint
from PySide6.QtGui import (QColor, QPalette, QPainter, QBrush, QIcon, QDoubleValidator,
                           QAction, QDrag)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QComboBox, QLineEdit,
    QCheckBox, QFrame, QHBoxLayout, QVBoxLayout, QGridLayout, QMessageBox,
    QFileDialog, QScrollArea, QButtonGroup, QDialog, QDialogButtonBox, QFormLayout,
    QListWidget, QInputDialog, QMenu, QSizePolicy,
)

import serial.tools.list_ports

from sequencer import Sequencer, SequenceEvents, ChannelPlan, estimate_run_times
import system as sysmod
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
    text = "#e6e6e6" if dark else "#1a1a1a"
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
    QFrame#unitBox {{
        background: {header_bg};
        border: 1px solid {card_border};
        border-radius: 10px;
    }}
    QLabel#unitTitle {{ font-size: 13px; font-weight: 700; }}
    QLabel#unitMeta {{ color: {muted}; font-size: 11px; }}
    QLabel#chNum {{ font-size: 15px; font-weight: 600; }}
    QLabel#caption {{ color: {muted}; font-size: 11px; }}
    QLabel#hdr {{ font-weight: 600; }}
    QLabel#reading {{ font-size: 16px; }}
    QLineEdit, QComboBox {{
        background: {field_bg};
        color: {text};
        border: 1px solid {field_border};
        border-radius: 6px;
        padding: 3px 6px;
        min-height: 20px;
    }}
    QLineEdit:hover, QComboBox:hover {{ border-color: #1976D2; }}
    QLineEdit:focus, QComboBox:focus {{ border-color: #1976D2; }}
    QLineEdit:disabled, QComboBox:disabled {{ color: {muted}; }}
    QLineEdit[invalid="true"] {{ border: 1px solid #e53935; }}
    /* Dropdown popup: keep hovered/selected items readable (was white-on-white). */
    QComboBox QAbstractItemView {{
        background: {field_bg};
        color: {text};
        border: 1px solid {field_border};
        outline: none;
        selection-background-color: #1976D2;
        selection-color: #ffffff;
    }}
    QComboBox QAbstractItemView::item {{ min-height: 22px; padding: 2px 6px; color: {text}; }}
    QComboBox QAbstractItemView::item:hover {{ background: #1976D2; color: #ffffff; }}
    QComboBox QAbstractItemView::item:selected {{ background: #1976D2; color: #ffffff; }}
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
    def __init__(self, size=16, parent=None):
        super().__init__(parent)
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


# Table-mode column spec: (title, fixed width, header alignment). Column 4
# (Status) stretches; all other columns are pinned to these widths in BOTH the
# header and every row so the labels line up with the data, and the header text
# is aligned to match each column's content.
_AL = Qt.AlignLeft | Qt.AlignVCenter
_AC = Qt.AlignCenter
_AR = Qt.AlignRight | Qt.AlignVCenter
TABLE_COLS = [
    ("Ch", 46, _AL), ("On", 34, _AC), ("Label", 124, _AL), ("", 22, _AC), ("Status", 150, _AL),
    ("Live TEC", 64, _AC), ("Live LAS", 64, _AC), ("Live T", 62, _AR), ("Live I", 62, _AR),
    ("Tgt TEC", 74, _AL), ("Tgt LAS", 74, _AL), ("Tgt T", 62, _AL), ("Max T", 46, _AC),
    ("Tgt I", 62, _AL), ("Max I", 46, _AC), ("Run", 98, _AC),
]
STATUS_COL = 4


class ChannelCard(QFrame):
    """One channel's controls. The SAME widgets are re-laid out for Table view
    (a full-width horizontal row) and Cards view (a compact vertical block)."""

    def __init__(self, idx, win, binding=None):
        super().__init__()
        self.idx = idx
        self.win = win
        self.binding = binding
        self.setObjectName("card")
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(10, 8, 10, 8)
        self._grid.setHorizontalSpacing(6)
        self._grid.setVerticalSpacing(4)

        # NOTE: every widget is parented to `self` (the card) at creation. If a
        # child were left parentless, _clear()'s setVisible(True) would turn it
        # into a floating top-level window (the "tiny windows flashing" bug, seen
        # as QWindowsWindow::setGeometry warnings on *Window objects).
        ch = binding.ch_num if binding is not None else idx + 1
        default_label = (binding.label if binding is not None and binding.label
                         else f"Laser {ch}")
        self.num = QLabel(f"Ch {ch}", self); self.num.setObjectName("chNum")
        self.led = LedDot(parent=self)
        self.enable = QCheckBox(self); self.enable.setEnabled(False)
        self.enable.setToolTip("Include this channel when you press RUN ALL.")
        self.enable.stateChanged.connect(win._on_enable_changed)
        self.label = QLineEdit(default_label, self); self.label.setEnabled(False)
        self.label.setToolTip("Custom name for this laser (e.g. its wavelength).")
        self.label.textEdited.connect(win._mark_unsaved)
        self.status = QLabel("Run Scan First", self); self.status.setObjectName("status")
        self.status.setToolTip("Live status / ramp progress for this channel.")
        self.live_tec = QLabel("OFF", self); self.live_tec.setAlignment(Qt.AlignCenter)
        self.live_tec.setToolTip("Live TEC (temperature controller) output state.")
        self.live_las = QLabel("OFF", self); self.live_las.setAlignment(Qt.AlignCenter)
        self.live_las.setToolTip("Live laser current-source output state.")
        self.live_t = QLabel("0.0", self); self.live_t.setObjectName("reading"); self.live_t.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.live_t.setToolTip("Live measured temperature (°C).")
        self.live_i = QLabel("0.0", self); self.live_i.setObjectName("reading"); self.live_i.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.live_i.setToolTip("Live measured laser current (mA).")
        self.tec_cmd = QComboBox(self); self.tec_cmd.addItems(["ON", "OFF"]); self.tec_cmd.setCurrentText("OFF"); self.tec_cmd.setEnabled(False)
        self.tec_cmd.setToolTip("Target TEC state to apply when this channel runs.")
        self.tec_cmd.currentTextChanged.connect(win._mark_unsaved)
        self.las_cmd = QComboBox(self); self.las_cmd.addItems(["ON", "OFF"]); self.las_cmd.setCurrentText("OFF"); self.las_cmd.setEnabled(False)
        self.las_cmd.setToolTip("Target laser state to apply when this channel runs (needs TEC ON).")
        self.las_cmd.currentTextChanged.connect(win._mark_unsaved)
        self.t_target = QLineEdit("22.0", self); self.t_target.setEnabled(False); self.t_target.setValidator(QDoubleValidator())
        self.t_target.setToolTip("Target temperature to ramp to (°C).")
        self.t_target.textEdited.connect(win._mark_unsaved)
        self.i_target = QLineEdit("0.0", self); self.i_target.setEnabled(False); self.i_target.setValidator(QDoubleValidator())
        self.i_target.setToolTip("Target laser current to ramp to (mA).")
        self.i_target.textEdited.connect(win._mark_unsaved)
        self.max_t = QLabel("-", self); self.max_t.setObjectName("caption"); self.max_t.setAlignment(Qt.AlignCenter)
        self.max_t.setToolTip("Hardware high-temperature limit read from the card.")
        self.max_i = QLabel("-", self); self.max_i.setObjectName("caption"); self.max_i.setAlignment(Qt.AlignCenter)
        self.max_i.setToolTip("Hardware current limit read from the card.")
        self.run = QPushButton("▶ Run Ch.", self); self.run.setEnabled(False)
        self.run.setToolTip("Run just this one channel to its targets now.")
        self.run.clicked.connect(lambda: win.execute_channels([self.idx]))

        # Captions shown only in Cards view (Table view has a shared header).
        self._caps = [QLabel(t, self) for t in
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
        self._grid.setHorizontalSpacing(10)
        for c in self._caps:
            c.setVisible(False)
        g = self._grid
        # widget per column, in TABLE_COLS order
        order = [self.num, self.enable, self.label, self.led, self.status,
                 self.live_tec, self.live_las, self.live_t, self.live_i,
                 self.tec_cmd, self.las_cmd, self.t_target, self.max_t,
                 self.i_target, self.max_i, self.run]
        small = {self.enable, self.led}
        for c, (w, (_, width, _align)) in enumerate(zip(order, TABLE_COLS)):
            self._grid.setColumnMinimumWidth(c, width)
            if c == STATUS_COL or w in small:
                w.setMinimumWidth(0); w.setMaximumWidth(16777215)
            else:
                w.setFixedWidth(width)
            if w in small:
                g.addWidget(w, 0, c, alignment=Qt.AlignCenter)
            else:
                g.addWidget(w, 0, c)
        self._grid.setColumnStretch(STATUS_COL, 1)

    def set_card_mode(self):
        self._clear()
        self._grid.setHorizontalSpacing(6)
        # Restore natural sizing (table mode pins widths).
        for w in (self.num, self.label, self.live_tec, self.live_las, self.live_t,
                  self.live_i, self.tec_cmd, self.las_cmd, self.t_target,
                  self.i_target, self.max_t, self.max_i, self.run):
            w.setMinimumWidth(0); w.setMaximumWidth(16777215)
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

    # --- drag to reorder channels within a controller ---
    # (Child controls consume their own mouse events, so a drag only starts from
    # the card background / the "Ch N" label — editing fields still works.)
    def mousePressEvent(self, e):
        self._press = e.position().toPoint() if e.button() == Qt.LeftButton else None

    def mouseMoveEvent(self, e):
        if getattr(self, "_press", None) is None or not (e.buttons() & Qt.LeftButton):
            return
        if (e.position().toPoint() - self._press).manhattanLength() < 12:
            return
        drag = QDrag(self)
        mime = QMimeData(); mime.setText(f"chan:{self.idx}")
        drag.setMimeData(mime)
        drag.exec(Qt.MoveAction)
        self._press = None


class UnitBox(QFrame):
    """One controller (or T+I pairing) drawn as a titled box; its channel cards
    live inside. All boxes stack in the same scroll area, so multiple controllers
    read as one table split into boxes. The header can be renamed (double-click),
    dragged to reorder controllers, or right-clicked for rename/edit/delete/move.
    Channel cards dropped inside reorder the channels within this controller."""

    def __init__(self, unit, win):
        super().__init__()
        self.unit = unit
        self.win = win
        self.setObjectName("unitBox")
        self.setAcceptDrops(True)
        self._press = None
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 8)
        v.setSpacing(4)
        self.head = QWidget()
        head = QHBoxLayout(self.head); head.setContentsMargins(0, 0, 0, 0); head.setSpacing(8)
        self.handle = QLabel("⠿"); self.handle.setObjectName("unitMeta")
        head.addWidget(self.handle)
        self.title = QLabel(unit.title); self.title.setObjectName("unitTitle")
        self.title.setToolTip("Double-click to rename; right-click for options; drag to reorder.")
        head.addWidget(self.title)
        self.meta = QLabel(""); self.meta.setObjectName("unitMeta")
        head.addWidget(self.meta)
        head.addStretch(1)
        # Per-controller Scan (only for multi-channel controllers) + Connect.
        self.btn_scan = QPushButton("Scan"); self.btn_scan.setFixedWidth(72)
        self.btn_scan.setToolTip("Re-interrogate this controller's channels for attached lasers.")
        self.btn_scan.clicked.connect(lambda: self.win._rescan_unit(self.unit.id))
        self.btn_scan.setVisible(False)
        head.addWidget(self.btn_scan)
        self.btn_conn = QPushButton("Connect"); self.btn_conn.setFixedWidth(100)
        self.btn_conn.setToolTip("Connect or disconnect this controller.")
        self.btn_conn.clicked.connect(lambda: self.win._toggle_unit_connect(self.unit.id))
        head.addWidget(self.btn_conn)
        v.addWidget(self.head)
        # Body holds this unit's channel cards (placed by _relayout).
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(8)
        self.grid.setAlignment(Qt.AlignTop)
        v.addWidget(self.body)

    def set_meta(self, text):
        self.meta.setText(text)

    def enable_scan(self, on):
        self.btn_scan.setVisible(on)

    def set_connected(self, connected):
        self.btn_conn.setText("Disconnect" if connected else "Connect")
        self.btn_scan.setEnabled(connected)

    def _on_header(self, y):
        return y <= self.head.height() + 6

    # --- rename / context menu ---
    def mouseDoubleClickEvent(self, e):
        if self._on_header(e.position().y()):
            self.win._rename_unit(self.unit.id)

    def contextMenuEvent(self, e):
        m = QMenu(self)
        a_ren = m.addAction("Rename…")
        a_edit = m.addAction("Edit connection / settings…")
        m.addSeparator()
        a_up = m.addAction("Move up")
        a_down = m.addAction("Move down")
        m.addSeparator()
        a_del = m.addAction("Delete controller")
        act = m.exec(e.globalPos())
        if act == a_ren:
            self.win._rename_unit(self.unit.id)
        elif act == a_edit:
            self.win._edit_unit(self.unit.id)
        elif act == a_up:
            self.win._move_unit(self.unit.id, -1)
        elif act == a_down:
            self.win._move_unit(self.unit.id, +1)
        elif act == a_del:
            self.win._delete_unit(self.unit.id)

    # --- drag to reorder controllers (grab the header) ---
    def mousePressEvent(self, e):
        self._press = e.position().toPoint() if (
            e.button() == Qt.LeftButton and self._on_header(e.position().y())) else None

    def mouseMoveEvent(self, e):
        if self._press is None or not (e.buttons() & Qt.LeftButton):
            return
        if (e.position().toPoint() - self._press).manhattanLength() < 12:
            return
        drag = QDrag(self)
        mime = QMimeData(); mime.setText(f"unit:{self.unit.id}")
        drag.setMimeData(mime)
        drag.exec(Qt.MoveAction)
        self._press = None

    def dragEnterEvent(self, e):
        t = e.mimeData().text() if e.mimeData().hasText() else ""
        if t.startswith("unit:") or t.startswith("chan:"):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        self.dragEnterEvent(e)

    def dropEvent(self, e):
        t = e.mimeData().text() if e.mimeData().hasText() else ""
        if t.startswith("unit:"):
            before = e.position().y() < self.height() / 2
            self.win._reorder_units(t[5:], self.unit.id, before)
            e.acceptProposedAction()
        elif t.startswith("chan:"):
            self.win._drop_channel(int(t[5:]), self.unit.id,
                                   self.mapToGlobal(e.position().toPoint()))
            e.acceptProposedAction()


# Combined single-device controller types offered in the Hardware dialog.
_COMBINED_TYPES = [("ldc3908", "ILX Lightwave LDC-3908 (8 channels)"),
                   ("thorlabs_itc", "Thorlabs ITC (combined, 1 channel)")]


def pick_transport(parent, ports, title, current=None):
    """Modal picker for a device connection. Returns a transport spec dict
    ({"type": "sim"|"serial"|"visa", ...}) or None if cancelled. `current`
    pre-selects the existing connection when editing."""
    d = QDialog(parent); d.setWindowTitle(title)
    f = QFormLayout(d)
    kind = QComboBox(); kind.addItems(["Demo Simulator", "Serial port", "USB / VISA resource"])
    f.addRow("Connection:", kind)
    port = QComboBox(); port.addItems(list(ports))
    f.addRow("Serial port:", port)
    res = QLineEdit(); res.setPlaceholderText("e.g. USB0::0x1313::0x8080::M00...::INSTR")
    f.addRow("VISA resource:", res)
    cur = current or {"type": "sim"}
    ct = cur.get("type", "sim")
    if ct == "serial":
        kind.setCurrentIndex(1)
        p = cur.get("port", "")
        if p and p not in ports:
            port.addItem(p)
        port.setCurrentText(p)
    elif ct == "visa":
        kind.setCurrentIndex(2); res.setText(cur.get("resource", ""))
    else:
        kind.setCurrentIndex(0)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.accepted.connect(d.accept); bb.rejected.connect(d.reject)
    f.addRow(bb)

    def upd():
        port.setEnabled(kind.currentIndex() == 1)
        res.setEnabled(kind.currentIndex() == 2)
    kind.currentIndexChanged.connect(upd); upd()
    if d.exec() != QDialog.Accepted:
        return None
    i = kind.currentIndex()
    if i == 1:
        return {"type": "serial", "port": port.currentText()}
    if i == 2:
        return {"type": "visa", "resource": res.text().strip()}
    return {"type": "sim"}


class HardwareDialog(QDialog):
    """Compose the controller layout: add combined controllers or Wavelength
    temperature+current pairings, each with its own connection (Demo / serial /
    USB-VISA). Produces a config dict consumed by system.build_system()."""

    def __init__(self, win, config):
        super().__init__(win)
        self._win = win
        self.setWindowTitle("Hardware Configuration")
        self.setMinimumWidth(600)
        # Work on a copy so Cancel discards changes.
        self.units = [json.loads(json.dumps(u)) for u in (config or {}).get("units", [])]

        v = QVBoxLayout(self)
        v.addWidget(QLabel("Each controller (or T+I pairing) is one box in the table:"))
        self.listw = QListWidget()
        v.addWidget(self.listw, 1)
        row = QHBoxLayout()
        b_add = QPushButton("Add controller…"); b_add.clicked.connect(self._add_combined)
        b_pair = QPushButton("Add Wavelength T+I pair…"); b_pair.clicked.connect(self._add_pair)
        b_del = QPushButton("Remove selected"); b_del.clicked.connect(self._remove)
        row.addWidget(b_add); row.addWidget(b_pair); row.addWidget(b_del); row.addStretch(1)
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._refresh()

    # --- rendering ---
    def _refresh(self):
        self.listw.clear()
        for u in self.units:
            self.listw.addItem(self._describe(u))

    @staticmethod
    def _tl(spec):
        spec = spec or {"type": "sim"}
        t = spec.get("type")
        return {"sim": "Demo", "serial": spec.get("port", "serial"),
                "visa": spec.get("resource", "USB")}.get(t, t)

    def _describe(self, u):
        if u.get("kind") == "pairing":
            return (f"{u.get('title', 'Pair')}  —  TEC via {self._tl(u['temp'].get('transport'))}"
                    f",  current via {self._tl(u['current'].get('transport'))}")
        return f"{u.get('title', u.get('driver'))}  [{u.get('driver')}]  —  {self._tl(u.get('transport'))}"

    def _new_id(self):
        existing = {u.get("id") for u in self.units}
        n = 1
        while f"u{n}" in existing:
            n += 1
        return f"u{n}"

    # --- transport picker ---
    def _pick_transport(self, title, current=None):
        return pick_transport(self, self._win._available_ports(), title, current)

    # --- add / remove ---
    def _add_combined(self):
        d = QDialog(self); d.setWindowTitle("Add controller")
        f = QFormLayout(d)
        dtype = QComboBox()
        for key, label in _COMBINED_TYPES:
            dtype.addItem(label, key)
        f.addRow("Controller:", dtype)
        title = QLineEdit(); f.addRow("Name:", title)
        # Pre-fill the name with the controller's default model, and keep it in
        # sync with the selected type until the user edits it themselves.
        def default_name():
            return sysmod.driver_class(dtype.currentData()).model
        title.setText(default_name())
        title._edited = False
        title.textEdited.connect(lambda *_: setattr(title, "_edited", True))
        dtype.currentIndexChanged.connect(
            lambda *_: (None if title._edited else title.setText(default_name())))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(d.accept); bb.rejected.connect(d.reject)
        f.addRow(bb)
        if d.exec() != QDialog.Accepted:
            return
        spec = self._pick_transport(f"Connection for {dtype.currentText()}")
        if spec is None:
            return
        self.units.append({"id": self._new_id(), "kind": "combined",
                           "driver": dtype.currentData(),
                           "title": title.text().strip() or dtype.currentText(),
                           "transport": spec})
        self._refresh()

    def _add_pair(self):
        default = f"{sysmod.driver_class('wavelength_tc').model} + {sysmod.driver_class('wavelength_qcl').model}"
        title, ok = QInputDialog.getText(self, "Add Wavelength pair",
                                         "Name for this laser line:", text=default)
        if not ok:
            return
        tspec = self._pick_transport("Temperature (Wavelength TC) connection")
        if tspec is None:
            return
        cspec = self._pick_transport("Current (Wavelength QCL) connection")
        if cspec is None:
            return
        self.units.append({"id": self._new_id(), "kind": "pairing",
                           "title": title.strip() or "Wavelength Laser",
                           "temp": {"driver": "wavelength_tc", "transport": tspec},
                           "current": {"driver": "wavelength_qcl", "transport": cspec}})
        self._refresh()

    def _remove(self):
        r = self.listw.currentRow()
        if 0 <= r < len(self.units):
            del self.units[r]
            self._refresh()

    def result_config(self):
        return {"units": self.units}


class LDCMainWindow(QMainWindow):
    @property
    def num_channels(self):
        return self.system.num_channels

    @property
    def channels(self):
        return self.system.channels

    def __init__(self):
        super().__init__()
        self.bridge = QtBridge()
        # Hardware layout: which controllers, their transports, and any T+I
        # pairings. This config is the source of truth; build_system() turns it
        # into a ControllerSystem (devices + units + flat channel bindings).
        # Default: one ILX LDC-3908 in Demo mode, so the app starts usable offline.
        self.config = sysmod.default_config()
        self.system = sysmod.build_system(self.config)
        self.seq = Sequencer(stop=self.system.stop, events=QtSequenceEvents(self.bridge))

        self.is_executing = False
        self.is_scanning = False
        self.is_closing = False
        self.has_scanned = False
        self._unit_scanned = {}     # uid -> bool (scanned since last connect)
        self.populated = [False] * self.system.num_channels
        self.total_estimated_time = 0.0
        self.sequence_start_time = None
        self.telemetry_active = False
        self.telemetry_thread = None
        self.telemetry_fail_count = 0
        self._emo_thread = None
        self.active_profile_path = ""
        self._unsaved = False
        self._MODE_KEYS = ("sequential", "stage", "parallel")
        self._MODE_NAMES = ("One laser at a time",
                            "All channels, stage by stage",
                            "All lasers at once")
        # Follow the OS light/dark theme (Qt 6.5+), and track live changes.
        hints = QApplication.instance().styleHints()
        try:
            self.dark = hints.colorScheme() == Qt.ColorScheme.Dark
        except Exception:
            self.dark = False
        try:
            hints.colorSchemeChanged.connect(self._on_os_theme_changed)
        except Exception:
            pass
        self._table_mode = True
        self._reflow_cols = 0

        self.setWindowTitle("Laser Controller Console v0.6.0")
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
        self._update_hw_summary()
        self._refresh_conn_ui()
        self._set_view_mode(True)
        QTimer.singleShot(0, self._load_last_profile)
        QTimer.singleShot(0, self._update_mode_estimates)

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
        self.btn_hardware = QPushButton("⚙ Hardware…")
        self.btn_hardware.setToolTip("Add/remove controllers, choose each one's connection (serial / USB /\n"
                                     "Demo), and pair split temperature + current instruments into one laser.")
        self.btn_hardware.clicked.connect(self.open_hardware_dialog)
        top.addWidget(self.btn_hardware)
        self.lbl_hw = QLabel(""); self.lbl_hw.setObjectName("hdr")
        self.lbl_hw.setToolTip("The controllers currently configured.")
        top.addWidget(self.lbl_hw)
        top.addSpacing(12)
        self.btn_connect = QPushButton("Connect All"); self.btn_connect.clicked.connect(self.connect_serial)
        self.btn_connect.setToolTip("Connect (or disconnect) every configured controller at once.\n"
                                    "Each controller also has its own Connect button in its header.")
        top.addWidget(self.btn_connect)
        self.btn_clear = QPushButton("Clear Faults"); self.btn_clear.setEnabled(False)
        self.btn_clear.setToolTip("Clear latched fault codes on every connected controller, then rescan.")
        self.btn_clear.clicked.connect(self.clear_faults)
        top.addWidget(self.btn_clear)
        top.addStretch(1)
        self.status_label = QLabel("Status: Disconnected")
        self.status_label.setObjectName("hdr")
        top.addWidget(self.status_label)
        root.addLayout(top)

        # --- Controls bar: title, view toggle, show-unused ---
        bar = QHBoxLayout()
        title = QLabel("Channel Configuration & Live Telemetry"); title.setObjectName("hdr")
        title.setStyleSheet("font-size: 15px;")
        bar.addWidget(title)
        bar.addStretch(1)

        self.btn_view_table = QPushButton("Table"); self.btn_view_table.setObjectName("seg"); self.btn_view_table.setCheckable(True); self.btn_view_table.setChecked(True)
        self.btn_view_table.setToolTip("Table view: one aligned row per channel.")
        self.btn_view_cards = QPushButton("Cards"); self.btn_view_cards.setObjectName("seg"); self.btn_view_cards.setCheckable(True)
        self.btn_view_cards.setToolTip("Card view: a reflowing grid of one card per channel.")
        grp = QButtonGroup(self); grp.setExclusive(True); grp.addButton(self.btn_view_table); grp.addButton(self.btn_view_cards)
        self.btn_view_table.clicked.connect(lambda: (self._set_view_mode(True), self._mark_unsaved()))
        self.btn_view_cards.clicked.connect(lambda: (self._set_view_mode(False), self._mark_unsaved()))
        seg = QHBoxLayout(); seg.setSpacing(0); seg.addWidget(self.btn_view_table); seg.addWidget(self.btn_view_cards)
        bar.addLayout(seg)
        bar.addSpacing(14)

        self.chk_show_unused = QCheckBox("Show unused")
        self.chk_show_unused.setToolTip("Also show empty slots, no-laser cards, and disabled channels.")
        self.chk_show_unused.stateChanged.connect(self._apply_visibility)
        bar.addWidget(self.chk_show_unused)
        root.addLayout(bar)

        # --- Scroll area: a vertical stack of per-unit boxes (each box groups one
        #     controller's channel cards), with a shared table header on top. ---
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.stack = QVBoxLayout(self.container)
        self.stack.setContentsMargins(4, 4, 4, 4)
        self.stack.setSpacing(10)
        self.stack.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.container)
        root.addWidget(self.scroll, 1)

        self.table_header = self._build_table_header()
        self.table_header.setParent(self.container); self.table_header.hide()
        self.cards = []
        self.unit_boxes = []
        self._build_channel_widgets()

        # --- Bottom panel: [ params + profile stacked ] ... [ presets | EMO | Run ] ---
        bottom = QHBoxLayout(); bottom.setSpacing(16)

        # Left column: params row, then the profile row directly beneath it. A
        # stretch above and below centers the pair vertically against the taller
        # presets/Run block on the right (no lopsided gap under the profile row).
        left = QVBoxLayout(); left.setSpacing(8)
        left.addStretch(1)
        params = QHBoxLayout(); params.setSpacing(8)

        def add_param(label, attr, default, tip):
            L = QLabel(label); L.setObjectName("hdr"); L.setToolTip(tip); params.addWidget(L)
            e = QLineEdit(default); e.setFixedWidth(72); e.setValidator(QDoubleValidator())
            e.setToolTip(tip)
            e.textEdited.connect(self._mark_unsaved)
            setattr(self, attr, e); params.addWidget(e)
        add_param("T Ramp (°C/s):", "t_ramp", "0.1",
                  "How fast the temperature setpoint is ramped, in °C per second (slower is gentler on the diode).")
        add_param("I Ramp (mA/s):", "i_ramp", "0.5",
                  "How fast the laser current is ramped, in mA per second.")
        add_param("T OFF Target (°C):", "t_off", "22.0",
                  "Temperature the TEC is ramped to before it is switched OFF.")
        params.addStretch(1)
        left.addLayout(params)

        profile = QHBoxLayout(); profile.setSpacing(8)
        self.lbl_profile = QLabel("Active Profile: [Unsaved]"); self.lbl_profile.setObjectName("hdr")
        profile.addWidget(self.lbl_profile)
        self.btn_save = QPushButton("💾 Save Profile"); self.btn_save.clicked.connect(self.save_profile)
        self.btn_save.setToolTip("Save all targets, ramp rates, ramp mode, and view to a profile file.")
        profile.addWidget(self.btn_save)
        self.btn_load = QPushButton("📂 Load Profile"); self.btn_load.clicked.connect(self.load_profile)
        self.btn_load.setToolTip("Load targets, ramp rates, ramp mode, and view from a profile file.")
        profile.addWidget(self.btn_load)
        self.btn_clear_prof = QPushButton("❌ Clear Profile"); self.btn_clear_prof.clicked.connect(self.clear_profile)
        self.btn_clear_prof.setToolTip("Reset all targets and settings to defaults.")
        profile.addWidget(self.btn_clear_prof)
        profile.addStretch(1)
        left.addLayout(profile)
        left.addStretch(1)
        bottom.addLayout(left)

        bottom.addStretch(1)

        # Bulk target presets: these only SET each channel's Target TEC/LAS; they
        # do not actuate hardware — the run happens when you press Run All. The
        # framed group + caption + placement next to Run All make that explicit.
        preset = QFrame(); preset.setObjectName("card")
        pv = QVBoxLayout(preset); pv.setContentsMargins(10, 6, 10, 8); pv.setSpacing(3)
        ph = QLabel("Bulk-set Target TEC / LAS"); ph.setObjectName("hdr")
        pv.addWidget(ph)
        pc = QLabel("Sets targets only — press ▶ RUN ALL to apply"); pc.setObjectName("caption")
        pv.addWidget(pc)
        mgrid = QGridLayout(); mgrid.setSpacing(4)
        self._master_buttons = []

        def mkmaster(text, fn, kind, r, c, tip):
            b = QPushButton(text); b.setEnabled(False); b.setFixedWidth(82); b.clicked.connect(fn)
            b.setToolTip(tip)
            if kind == "green":
                b.setStyleSheet("background:#2e7d32; color:white; font-weight:600;")
            elif kind == "red":
                b.setStyleSheet("background:#c62828; color:white; font-weight:600;")
            mgrid.addWidget(b, r, c)
            self._master_buttons.append(b)

        mkmaster("All On", lambda: self.set_all_systems("ON"), "green", 0, 0,
                 "Set every channel's Target TEC and Target LAS to ON.")
        mkmaster("TEC On", lambda: self.set_all_dropdowns("TEC", "ON"), "", 0, 1,
                 "Set every channel's Target TEC to ON.")
        mkmaster("LAS On", lambda: self.set_all_dropdowns("LAS", "ON"), "", 0, 2,
                 "Set every channel's Target LAS to ON.")
        mkmaster("All Off", lambda: self.set_all_systems("OFF"), "red", 1, 0,
                 "Set every channel's Target TEC and Target LAS to OFF.")
        mkmaster("TEC Off", lambda: self.set_all_dropdowns("TEC", "OFF"), "", 1, 1,
                 "Set every channel's Target TEC to OFF.")
        mkmaster("LAS Off", lambda: self.set_all_dropdowns("LAS", "OFF"), "", 1, 2,
                 "Set every channel's Target LAS to OFF.")
        pv.addLayout(mgrid)
        bottom.addWidget(preset)

        # Emergency button: a red block whose content is stacked labels (warning
        # icon on its own line, the bold action phrase, then a lighter warning) so
        # we can mix weights. The labels are click-transparent so the whole block
        # still fires the button. It stretches to match the Run-All/Cancel stack.
        self.btn_emo = QPushButton()
        self.btn_emo.setEnabled(False)
        self.btn_emo.setFixedWidth(150)
        self.btn_emo.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.btn_emo.setStyleSheet(
            "QPushButton { background:#b71c1c; border-radius:8px; }"
            "QPushButton:disabled { background:#6d3636; }")
        emo_lay = QVBoxLayout(self.btn_emo)
        emo_lay.setContentsMargins(8, 8, 8, 8); emo_lay.setSpacing(2)
        emo_lay.addStretch(1)
        emo_icon = QLabel("⚠"); emo_icon.setAlignment(Qt.AlignCenter)
        emo_icon.setStyleSheet("color:white; font-size:22px; font-weight:bold; background:transparent;")
        emo_title = QLabel("EMERGENCY LASER CURRENT OFF")
        emo_title.setAlignment(Qt.AlignCenter); emo_title.setWordWrap(True)
        emo_title.setStyleSheet("color:white; font-size:13px; font-weight:bold; background:transparent;")
        emo_warn = QLabel("MAY DAMAGE LASER")
        emo_warn.setAlignment(Qt.AlignCenter); emo_warn.setWordWrap(True)
        emo_warn.setStyleSheet("color:white; font-size:11px; font-weight:normal; background:transparent;")
        for _l in (emo_icon, emo_title, emo_warn):
            _l.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        emo_lay.addWidget(emo_icon)
        emo_lay.addWidget(emo_title)
        emo_lay.addSpacing(6)
        emo_lay.addWidget(emo_warn)
        emo_lay.addStretch(1)
        self.btn_emo.setToolTip("EMERGENCY: immediately cut laser current on every channel, bypassing the\n"
                                "ramp-down. Use only in a genuine emergency — for normal stops use Cancel Run.")
        self.btn_emo.clicked.connect(self.emergency_las_off)
        bottom.addWidget(self.btn_emo)

        run_col = QVBoxLayout(); run_col.setSpacing(6)
        # Ramp-mode selector on its own full-width line (label above) with the
        # per-mode time estimate baked into each item's text.
        mlbl = QLabel("Ramp mode:"); mlbl.setObjectName("caption")
        run_col.addWidget(mlbl)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(list(self._MODE_NAMES))  # order matches _MODE_KEYS
        self.mode_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.mode_combo.setMinimumWidth(280)
        self.mode_combo.setToolTip(
            "One laser at a time: finish each laser's temp→current before the next.\n"
            "All channels, stage by stage: moves every channel through one stage "
            "before the next, auto-ordered so the TEC-before-laser interlock holds "
            "both ways (temperatures come up before currents on start-up; currents "
            "come down before temperatures on shutdown).\n"
            "All lasers at once: ramp every channel simultaneously (fastest).")
        self.mode_combo.currentIndexChanged.connect(self._mark_unsaved)
        run_col.addWidget(self.mode_combo)
        self.btn_run_all = QPushButton("▶ RUN ALL"); self.btn_run_all.setEnabled(False)
        self.btn_run_all.setMinimumHeight(48); self.btn_run_all.setMinimumWidth(280)
        self.btn_run_all.setStyleSheet("background:#2e7d32; color:white; font-size:16px; font-weight:bold; border-radius:8px;")
        self.btn_run_all.setToolTip("Run every enabled channel to its targets using the selected ramp mode.")
        self.btn_run_all.clicked.connect(self.execute_all)
        run_col.addWidget(self.btn_run_all)
        self.btn_stop = QPushButton("⏹ CANCEL RUN (Safe)"); self.btn_stop.setEnabled(False)
        self.btn_stop.setMinimumHeight(30)
        self.btn_stop.setStyleSheet("background:#c62828; color:white; font-weight:bold; border-radius:8px;")
        self.btn_stop.setToolTip("Safely halt the run: stops ramping and holds each channel at its last setpoint.")
        self.btn_stop.clicked.connect(self.stop_execution)
        run_col.addWidget(self.btn_stop)
        bottom.addLayout(run_col)

        root.addLayout(bottom)

    def _build_table_header(self):
        h = QFrame(); h.setObjectName("tableHeader")
        # Margins + spacing must match ChannelCard's table-mode grid so the header
        # columns line up exactly with the row columns.
        g = QGridLayout(h); g.setContentsMargins(10, 4, 10, 4); g.setHorizontalSpacing(10)
        for c, (title, w, align) in enumerate(TABLE_COLS):
            g.setColumnMinimumWidth(c, w)
            lab = QLabel(title); lab.setObjectName("caption")
            lab.setAlignment(align)
            if c != STATUS_COL:
                lab.setFixedWidth(w)   # match the row widgets so labels line up
            g.addWidget(lab, 0, c)
        g.setColumnStretch(STATUS_COL, 1)
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
    # System / channel widgets
    # ------------------------------------------------------------------
    def _unit_meta(self, unit):
        def tlabel(dev):
            spec = getattr(dev, "transport_spec", {}) or {}
            t = spec.get("type", "sim")
            if t == "sim":
                return "Demo"
            if t == "serial":
                return spec.get("port", "serial")
            if t == "visa":
                return spec.get("resource", "USB")
            return t
        parts = [f"{d.model} ({tlabel(d)})" for d in unit.devices]
        return "  +  ".join(parts)

    def _build_channel_widgets(self):
        """(Re)create one UnitBox per unit and one ChannelCard per binding, from
        the current system. Cards are parented to their unit box and start hidden
        so _relayout only ever adds/removes and toggles visibility."""
        for box in getattr(self, "unit_boxes", []):
            box.setParent(None)
            box.deleteLater()
        self.unit_boxes = []
        cards_by_idx = [None] * self.system.num_channels
        for unit in self.system.units:
            box = UnitBox(unit, self)
            box.set_meta(self._unit_meta(unit))
            for b in unit.channels:
                card = ChannelCard(b.idx, self, binding=b)
                card.setParent(box.body)
                card.hide()
                cards_by_idx[b.idx] = card
            box.setParent(self.container)
            box.hide()
            self.unit_boxes.append(box)
        self.cards = cards_by_idx
        self.populated = [False] * len(self.cards)
        self._relayout_key = None

    def _snapshot_channel_state(self):
        """Capture the user-entered per-channel state keyed by (unit_id, ch_num),
        which is stable across reorder/delete, so a rebuild doesn't wipe it."""
        snap = {}
        for b in self.system.channels:
            c = self.cards[b.idx]
            snap[(b.unit_id, b.ch_num)] = {
                "label": c.label.text(), "t": c.t_target.text(), "i": c.i_target.text(),
                "tec": c.tec_cmd.currentText(), "las": c.las_cmd.currentText(),
            }
        return snap

    def _restore_channel_state(self, snap):
        for b in self.system.channels:
            s = snap.get((b.unit_id, b.ch_num))
            if not s:
                continue
            c = self.cards[b.idx]
            c.label.setText(s["label"])
            c.t_target.setText(s["t"]); c.i_target.setText(s["i"])
            c.tec_cmd.setCurrentText(s["tec"]); c.las_cmd.setCurrentText(s["las"])

    def _rebuild_system(self, preserve=True):
        """Rebuild the ControllerSystem from self.config and recreate the channel
        widgets. Closes any existing connection first. Called after the hardware
        config changes (Hardware dialog / reorder / profile load). `preserve`
        carries user-entered targets/labels across the rebuild."""
        snap = self._snapshot_channel_state() if (preserve and getattr(self, "cards", None)) else {}
        try:
            self.system.close_all()
        except Exception:
            pass
        self.telemetry_active = False
        self.system = sysmod.build_system(self.config)
        self.seq = Sequencer(stop=self.system.stop, events=QtSequenceEvents(self.bridge))
        self._build_channel_widgets()
        for card in self.cards:
            card.set_table_mode() if self._table_mode else card.set_card_mode()
        if snap:
            self._restore_channel_state(snap)
        self.has_scanned = False
        self._unit_scanned = {}
        self.btn_run_all.setEnabled(False)
        self._lock_controls(False)
        self._update_hw_summary()
        self._refresh_conn_ui()
        self._relayout(force=True)

    def _update_hw_summary(self):
        units = self.system.units
        names = ", ".join(u.title for u in units)
        self.lbl_hw.setText(f"{len(units)} controller{'s' if len(units) != 1 else ''}: {names}")

    def open_hardware_dialog(self):
        if self.is_executing:
            QMessageBox.warning(self, "Busy", "Stop the running sequence first.")
            return
        if self._any_connected():
            QMessageBox.information(self, "Disconnect first",
                                    "Disconnect all controllers before changing the hardware configuration.")
            return
        dlg = HardwareDialog(self, self.config)
        if dlg.exec() == QDialog.Accepted:
            cfg = dlg.result_config()
            if not cfg.get("units"):
                QMessageBox.warning(self, "No controllers", "Add at least one controller.")
                return
            self.config = cfg
            self._rebuild_system()
            self._mark_unsaved()

    # --- per-controller edit / reorder (from the box header + context menu) ---
    def _find_unit_cfg(self, uid):
        for u in self.config.get("units", []):
            if u.get("id") == uid:
                return u
        return None

    def _require_disconnected(self, what):
        if self.is_executing:
            QMessageBox.warning(self, "Busy", "Stop the running sequence first.")
            return False
        if self._any_connected():
            QMessageBox.information(self, "Disconnect first", f"Disconnect all controllers before you {what}.")
            return False
        return True

    def _rename_unit(self, uid):
        u = self._find_unit_cfg(uid)
        if u is None:
            return
        cur = u.get("title", "")
        new, ok = QInputDialog.getText(self, "Rename controller", "Name:", text=cur)
        if not ok:
            return
        new = new.strip() or cur
        u["title"] = new
        for un in self.system.units:
            if un.id == uid:
                un.title = new
        for box in self.unit_boxes:
            if box.unit.id == uid:
                box.title.setText(new)
        self._mark_unsaved()

    def _delete_unit(self, uid):
        if not self._require_disconnected("delete a controller"):
            return
        u = self._find_unit_cfg(uid)
        if u is None:
            return
        if QMessageBox.question(self, "Delete controller",
                                f"Remove “{u.get('title')}” from the configuration?") != QMessageBox.Yes:
            return
        self.config["units"] = [x for x in self.config["units"] if x.get("id") != uid]
        self._rebuild_system()
        self._mark_unsaved()

    def _move_unit(self, uid, delta):
        if not self._require_disconnected("reorder controllers"):
            return
        units = self.config["units"]
        i = next((k for k, u in enumerate(units) if u.get("id") == uid), None)
        if i is None:
            return
        j = i + delta
        if 0 <= j < len(units):
            units[i], units[j] = units[j], units[i]
            self._rebuild_system()
            self._mark_unsaved()

    def _reorder_units(self, src_uid, target_uid, before):
        if src_uid == target_uid or not self._require_disconnected("reorder controllers"):
            return
        units = self.config["units"]
        src = next((u for u in units if u.get("id") == src_uid), None)
        if src is None:
            return
        units.remove(src)
        tpos = next((k for k, u in enumerate(units) if u.get("id") == target_uid), len(units))
        if not before:
            tpos += 1
        units.insert(tpos, src)
        self._rebuild_system()
        self._mark_unsaved()

    def _edit_unit(self, uid):
        if not self._require_disconnected("edit a controller"):
            return
        u = self._find_unit_cfg(uid)
        if u is None:
            return
        ports = self._available_ports()
        if u.get("kind") == "pairing":
            title, ok = QInputDialog.getText(self, "Edit pairing", "Name:", text=u.get("title", ""))
            if not ok:
                return
            tspec = pick_transport(self, ports, "Temperature (Wavelength TC) connection",
                                   u["temp"].get("transport"))
            if tspec is None:
                return
            cspec = pick_transport(self, ports, "Current (Wavelength QCL) connection",
                                   u["current"].get("transport"))
            if cspec is None:
                return
            u["title"] = title.strip() or u.get("title")
            u["temp"]["transport"] = tspec
            u["current"]["transport"] = cspec
        else:
            d = QDialog(self); d.setWindowTitle("Edit controller")
            f = QFormLayout(d)
            dtype = QComboBox()
            for key, label in _COMBINED_TYPES:
                dtype.addItem(label, key)
            dtype.setCurrentIndex(next((k for k, (key, _) in enumerate(_COMBINED_TYPES)
                                        if key == u.get("driver")), 0))
            f.addRow("Controller:", dtype)
            name = QLineEdit(u.get("title", "")); f.addRow("Name:", name)
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.accepted.connect(d.accept); bb.rejected.connect(d.reject)
            f.addRow(bb)
            if d.exec() != QDialog.Accepted:
                return
            spec = pick_transport(self, ports, f"Connection for {dtype.currentText()}",
                                  u.get("transport"))
            if spec is None:
                return
            u["driver"] = dtype.currentData()
            u["title"] = name.text().strip() or dtype.currentText()
            u["transport"] = spec
        self._rebuild_system()
        self._mark_unsaved()

    def _drop_channel(self, src_idx, unit_id, global_pos):
        """Reorder a channel within its controller (drag-and-drop). Only combined
        multi-channel controllers reorder; channels can't move between controllers."""
        if not (0 <= src_idx < len(self.system.channels)):
            return
        src_b = self.system.channels[src_idx]
        if src_b.unit_id != unit_id:
            return
        ucfg = self._find_unit_cfg(unit_id)
        unit = next((u for u in self.system.units if u.id == unit_id), None)
        if ucfg is None or unit is None or ucfg.get("kind") != "combined" or len(unit.channels) < 2:
            return
        if not self._require_disconnected("reorder channels"):
            return
        order = [b.ch_num for b in unit.channels]
        target_ch, before = None, True
        for b in unit.channels:
            card = self.cards[b.idx]
            if not card.isVisible():
                continue
            top = card.mapToGlobal(QPoint(0, 0)).y()
            bottom = top + card.height()
            if top <= global_pos.y() <= bottom:
                target_ch = b.ch_num
                before = global_pos.y() < (top + bottom) / 2
                break
        if target_ch is None or target_ch == src_b.ch_num:
            return
        order.remove(src_b.ch_num)
        tpos = order.index(target_ch)
        if not before:
            tpos += 1
        order.insert(tpos, src_b.ch_num)
        ucfg["channel_order"] = order
        self._rebuild_system()
        self._mark_unsaved()

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
        if self.chk_show_unused.isChecked():
            return list(range(self.num_channels))
        out = []
        for b in self.system.channels:
            if not self._unit_scanned.get(b.unit_id, False):
                out.append(b.idx)          # not yet scanned: show so the layout is visible
            elif self.populated[b.idx] and self.cards[b.idx].enable.isChecked():
                out.append(b.idx)
        return out

    def _relayout(self, force=False):
        if not getattr(self, "cards", None):
            return
        shown = self._shown()
        shown_set = set(shown)
        if self._table_mode:
            cols = 1
        else:
            vw = self.scroll.viewport().width()
            cols = max(1, min(vw // CARD_W, max(1, len(shown))))
        key = (self._table_mode, cols, tuple(shown), len(self.unit_boxes))
        if not force and key == getattr(self, "_relayout_key", None):
            return
        self._relayout_key = key

        # Detach the stack items and each box's grid items (widgets stay parented
        # to the container / their box, so nothing becomes a top-level window).
        while self.stack.count():
            self.stack.takeAt(0)
        for box in self.unit_boxes:
            while box.grid.count():
                box.grid.takeAt(0)
            for c in range(max(4, box.grid.columnCount())):
                box.grid.setColumnStretch(c, 0)

        # Shared column header on top (table view only).
        if self._table_mode:
            self.stack.addWidget(self.table_header)
            self.table_header.setVisible(True)
        else:
            self.table_header.setVisible(False)

        for box in self.unit_boxes:
            vis = [self.cards[b.idx] for b in box.unit.channels if b.idx in shown_set]
            if self._table_mode:
                box.grid.setColumnStretch(0, 1)
                for pos, card in enumerate(vis):
                    box.grid.addWidget(card, pos, 0)
            else:
                for c in range(cols):
                    box.grid.setColumnStretch(c, 1)
                for pos, card in enumerate(vis):
                    r, c = divmod(pos, cols)
                    box.grid.addWidget(card, r, c)
            box.setVisible(bool(vis))
            if vis:
                self.stack.addWidget(box)

        for i, card in enumerate(self.cards):
            card.setVisible(i in shown_set)

    def _apply_visibility(self, *_):
        self._relayout(force=True)

    def _on_enable_changed(self, *_):
        self._relayout(force=True)
        self._update_mode_estimates()

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
    # Connection
    # ------------------------------------------------------------------
    @staticmethod
    def _available_ports():
        return [p.device for p in serial.tools.list_ports.comports()]

    def _set_status(self, text):
        self.status_label.setText(text)

    # --- per-controller state ---
    def _unit(self, uid):
        return next((u for u in self.system.units if u.id == uid), None)

    def _box(self, uid):
        return next((b for b in self.unit_boxes if b.unit.id == uid), None)

    def _unit_connected(self, unit):
        return bool(unit.devices) and all(d.is_connected for d in unit.devices)

    def _unit_needs_scan(self, unit):
        """Only multi-channel combined controllers (e.g. ILX) discover which
        channels have lasers; single-channel instruments are always active."""
        return (unit.kind == sysmod.KIND_COMBINED and unit.devices
                and unit.devices[0].num_channels > 1)

    def _any_connected(self):
        return any(self._unit_connected(u) for u in self.system.units)

    # --- global connect / disconnect (top-bar button) ---
    def connect_serial(self):
        if self.is_executing:
            self._set_status("Status: PRESS STOP BEFORE DISCONNECTING!")
            return
        if all(self._unit_connected(u) for u in self.system.units) and self.system.units:
            for u in list(self.system.units):
                self._disconnect_unit(u.id, relayout=False)
        else:
            for u in list(self.system.units):
                if not self._unit_connected(u):
                    self._connect_unit(u.id)
        self._relayout(force=True)
        self._refresh_conn_ui()

    # --- per-controller connect / disconnect (header button) ---
    def _toggle_unit_connect(self, uid):
        if self.is_executing:
            self._set_status("Status: PRESS STOP BEFORE DISCONNECTING!")
            return
        unit = self._unit(uid)
        if unit is None:
            return
        if self._unit_connected(unit):
            self._disconnect_unit(uid)
        else:
            self._connect_unit(uid)
            self._relayout(force=True)
        self._refresh_conn_ui()

    def _connect_unit(self, uid):
        unit = self._unit(uid)
        if unit is None or self._unit_connected(unit):
            return
        opened = []
        try:
            for d in unit.devices:
                sysmod._connect_driver(d, getattr(d, "transport_spec", {"type": "sim"}))
                opened.append(d)
        except Exception as e:
            for d in opened:
                try:
                    d.close()
                except Exception:
                    pass
            QMessageBox.critical(self, "Connection Error", f"Failed to connect “{unit.title}”:\n{e}")
            return
        self._set_status(f"Status: {unit.title} connected — scanning…")
        # Auto-scan on connect: ILX interrogates its slots; others just mark active.
        self.is_scanning = True
        threading.Thread(target=self._scan_unit, args=(uid,), daemon=True).start()

    def _disconnect_unit(self, uid, relayout=True):
        unit = self._unit(uid)
        if unit is None:
            return
        for d in unit.devices:
            try:
                d.close()
            except Exception:
                pass
        self._unit_scanned[uid] = False
        for b in unit.channels:
            self._mark_empty(b.idx, "Disconnected")
        if not self._any_connected():
            self.telemetry_active = False
        self._set_status(f"Status: {unit.title} disconnected")
        if relayout:
            self._relayout(force=True)
        self._refresh_conn_ui()

    def _rescan_unit(self, uid):
        unit = self._unit(uid)
        if unit is None or not self._unit_connected(unit) or self.is_executing:
            return
        self.is_scanning = True
        self._set_status(f"Status: Rescanning {unit.title}…")
        threading.Thread(target=self._scan_unit, args=(uid,), daemon=True).start()

    def _refresh_conn_ui(self):
        units = self.system.units
        all_conn = bool(units) and all(self._unit_connected(u) for u in units)
        any_conn = self._any_connected()
        self.btn_connect.setText("Disconnect All" if all_conn else "Connect All")
        self.btn_clear.setEnabled(any_conn)
        self.btn_run_all.setEnabled(any_conn and any(self._unit_scanned.get(u.id) for u in units))
        # Hardware layout may only change while fully disconnected.
        self.btn_hardware.setEnabled(not any_conn)
        for box in self.unit_boxes:
            u = self._unit(box.unit.id)
            box.enable_scan(self._unit_needs_scan(u))
            box.set_connected(self._unit_connected(u))

    @staticmethod
    def _dev_read(drv, ch, fn, default):
        """Read one value from a device under its lock, selecting its channel if
        needed. Returns `default` on any error / missing device."""
        if drv is None:
            return default
        try:
            with drv.serial_lock:
                if drv._selected_channel != ch:
                    drv.select_channel(ch)
                    drv._selected_channel = ch
                    time.sleep(0.1)
                return fn()
        except Exception:
            return default

    def _scan_unit(self, uid):
        unit = self._unit(uid)
        if unit is None:
            self._post(self._finish_unit_scan, uid, 0)
            return
        cards = 0
        for b in unit.channels:
            if self.is_closing or not self._unit_connected(unit):
                break
            if self._scan_binding(b):
                cards += 1
        self._post(self._finish_unit_scan, uid, cards)

    def _scan_binding(self, b):
        k = b.idx
        td, cd = b.temp_driver, b.current_driver
        tch = b.temp.channel if b.temp else 1
        cch = b.current.channel if b.current else 1
        rd = self._dev_read
        # Multi-channel combined controllers (e.g. ILX) have empty slots / no-laser
        # modules; single-channel instruments are simply present when connected.
        multi_combined = td is not None and td is cd and td.num_channels > 1

        if multi_combined:
            if rd(td, tch, td.active_channel, -1) != tch:
                self._post(self._mark_empty, k, "Empty Slot")
                return False

        t_val = rd(td, tch, td.temp_setpoint, float('nan'))
        i_val = rd(cd, cch, cd.current_setpoint, 0.0)
        tec_out = rd(td, tch, td.tec_output, 0)
        las_out = rd(cd, cch, cd.laser_output, 0)

        if multi_combined:
            # A negative/NaN setpoint means a module with no laser attached. A real
            # laser's setpoint is never below 0 °C in our use case.
            if (isinstance(t_val, float) and math.isnan(t_val)) or t_val < 0:
                self._post(self._mark_empty, k, "No Laser Attached")
                return False

        max_t = rd(td, tch, td.temp_limit, 99.0)
        max_i = rd(cd, cch, cd.current_limit, 500.0)

        has_err, err = False, ""
        for drv, ch in ((td, tch), (cd, cch)):
            if drv is None:
                continue
            he, e = rd(drv, ch, drv.read_errors, (False, ""))
            if he:
                has_err, err = True, e
                break
        rd(cd, cch, cd.enable_modulation, None)  # normalize modulation state

        if isinstance(t_val, float) and math.isnan(t_val):
            t_val = 0.0
        self._post(self._update_after_scan, k, t_val, i_val, tec_out, las_out,
                   max_t, max_i, has_err, err)
        return True

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

    def _finish_unit_scan(self, uid, cards):
        self.is_scanning = False
        if self.is_closing:
            self.close(); return
        self._unit_scanned[uid] = True
        self.has_scanned = True
        unit = self._unit(uid)
        title = unit.title if unit else ""
        demo = unit and all((getattr(d, "transport_spec", {}) or {}).get("type", "sim") == "sim"
                            for d in unit.devices)
        self._set_status(f"Status: {title} ready — {cards} channel{'s' if cards != 1 else ''}"
                         + (" (Demo)" if demo else ""))
        self._relayout(force=True)
        self._lock_controls(True)
        self._refresh_conn_ui()
        self._update_mode_estimates()
        if not self.telemetry_active and self._any_connected():
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
        if not self._any_connected():
            return
        any_on = False
        rd = self._dev_read
        for b in self.system.channels:
            if self.is_executing or self.is_scanning or self.is_closing:
                break
            k = b.idx
            # Only populated channels (of connected, scanned units) are polled.
            if not self.populated[k]:
                continue
            td, cd = b.temp_driver, b.current_driver
            tch = b.temp.channel if b.temp else 1
            cch = b.current.channel if b.current else 1
            try:
                t_val = rd(td, tch, td.temp_setpoint, None) if td else None
                i_val = rd(cd, cch, cd.current_setpoint, None) if cd else None
                tec = rd(td, tch, td.tec_output, None) if td else None
                las = rd(cd, cch, cd.laser_output, None) if cd else None
                self._post(self._telemetry_update, k, t_val, i_val, tec, las)
                if las == 1:
                    any_on = True
                self.telemetry_fail_count = 0
            except Exception as e:
                print(f"Telemetry fail ch{k + 1}: {e}")
                self.telemetry_fail_count += 1
                if self.telemetry_fail_count > 3:
                    self.telemetry_active = False
                    self._post(self._handle_connection_loss)
                    break
        self._post(self.btn_emo.setEnabled, any_on)

    def _telemetry_update(self, i, t_val, i_val, tec, las):
        try:
            self.cards[i].live_t.setText(f"{float(t_val):.1f}")
        except Exception:
            pass
        try:
            self.cards[i].live_i.setText(f"{float(i_val):.1f}")
        except Exception:
            pass
        if tec is not None:
            self._on_live_output(i, "TEC", "ON" if tec == 1 else "OFF")
        if las is not None:
            self._on_live_output(i, "LAS", "ON" if las == 1 else "OFF")
        # Live readings feed the ETA (ΔT/ΔI from the current values).
        if not self.is_executing:
            self._update_mode_estimates()

    def _handle_connection_loss(self):
        self._set_status("Status: Connection Lost")
        self._lock_controls(False)
        self.btn_connect.setText("Connect")
        self.btn_hardware.setEnabled(True)
        self.system.stop.is_stop_requested = True
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
        idxs = [i for i in range(self.num_channels)
                if self.cards[i].enable.isChecked() and self.cards[i].enable.isEnabled()]
        if not idxs:
            QMessageBox.warning(self, "Warning", 'No channels are marked "On" for the sequence!')
            return
        self.execute_channels(idxs)

    def execute_channels(self, idxs):
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
        for i in idxs:
            c = self.cards[i]
            b = self.system.channels[i]
            try:
                tt = float(c.t_target.text()); ti = float(c.i_target.text()); valid = True
            except ValueError:
                tt, ti, valid = 0.0, 0.0, False
            plans.append(ChannelPlan(idx=i, ch_num=b.ch_num,
                                     tec_cmd=c.tec_cmd.currentText(), las_cmd=c.las_cmd.currentText(),
                                     t_target=tt, i_target=ti, targets_valid=valid, binding=b))

        mode = self._selected_mode()
        infos = self._infos_from_cards([p.idx for p in plans if p.targets_valid])
        self.total_estimated_time = estimate_run_times(infos, t_ramp, i_ramp, t_off).get(mode, 0.0)
        self.is_executing = True
        self.system.stop.reset()
        self._set_status("Status: Sequence Running...")
        self._lock_controls(False)
        self.btn_stop.setEnabled(True)
        self.btn_emo.setEnabled(True)
        threading.Thread(target=self._run_sequence, args=(plans, t_ramp, i_ramp, t_off, mode), daemon=True).start()

    # --- Ramp-mode selection + live time estimates ---
    def _selected_mode(self):
        return self._MODE_KEYS[self.mode_combo.currentIndex()]

    def _active_channels(self):
        return [i for i in range(self.num_channels)
                if self.populated[i] and self.cards[i].enable.isChecked()]

    def _infos_from_cards(self, idxs):
        infos = []
        for i in idxs:
            c = self.cards[i]
            try:
                tt = float(c.t_target.text()); ti = float(c.i_target.text())
                ct = float(c.live_t.text()); ci = float(c.live_i.text())
            except ValueError:
                continue
            infos.append(dict(curr_t=ct, curr_i=ci, t_target=tt, i_target=ti,
                              tec_cmd=c.tec_cmd.currentText(), las_cmd=c.las_cmd.currentText(),
                              live_tec=c.live_tec.text(), live_las=c.live_las.text()))
        return infos

    def _update_mode_estimates(self):
        if not hasattr(self, "mode_combo"):
            return
        names = self._MODE_NAMES
        try:
            tr = float(self.t_ramp.text()); ir = float(self.i_ramp.text()); to = float(self.t_off.text())
        except (ValueError, AttributeError):
            tr = ir = to = None
        infos = self._infos_from_cards(self._active_channels()) if hasattr(self, "cards") else []
        est = None
        if tr and ir and tr > 0 and ir > 0 and infos:
            est = estimate_run_times(infos, tr, ir, to)
        for idx, key in enumerate(self._MODE_KEYS):
            if est:
                secs = est[key]
                self.mode_combo.setItemText(idx, f"{names[idx]}  (~{int(secs // 60)}:{int(secs % 60):02d})")
            else:
                self.mode_combo.setItemText(idx, names[idx])

    def _run_sequence(self, plans, t_ramp, i_ramp, t_off, mode):
        self.sequence_start_time = time.time()
        self.seq.run(plans, t_ramp, i_ramp, t_off, mode=mode)
        self._post(self._finish_sequence, len(plans))

    def _finish_sequence(self, n):
        self.is_executing = False
        self._lock_controls(True)
        self.btn_stop.setEnabled(False)
        if self.system.stop.is_emo_requested:
            self._perform_emergency_shutdown()
            self.system.stop.is_emo_requested = False
        elif self.system.stop.is_stop_requested:
            self._set_status("Status: Hardware Halted & Pinned.")
        else:
            self._set_status("Status: Sequence Complete & Settled.")
            if n > 1:
                QMessageBox.information(self, "Done", "Sequence completed across all selected channels.")
        self._refresh_conn_ui()
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
            self.system.stop.is_stop_requested = True
            self._set_status("Status: STOP COMMANDED. Halting safely...")
            self._triple_bell()

    def emergency_las_off(self):
        if QMessageBox.question(self, "Emergency LAS OFF",
                                "Immediately cutting current without ramping can damage the diode. Proceed?") \
                != QMessageBox.Yes:
            return
        self.system.stop.is_stop_requested = True
        self.system.stop.is_emo_requested = True
        if self.is_executing:
            self._set_status("Status: EMERGENCY OFF — cutting laser...")
            return
        self._emo_thread = threading.Thread(target=self._perform_emergency_shutdown, daemon=False)
        self._emo_thread.start()

    def _perform_emergency_shutdown(self):
        # Cut laser current on every populated channel, on whatever device owns
        # its current output (the QCL for a Wavelength pairing).
        for b in self.system.channels:
            if not self.populated[b.idx] or b.current is None:
                continue
            cd, cch = b.current.driver, b.current.channel
            try:
                with cd.serial_lock:
                    cd.select_channel(cch); cd._selected_channel = cch
                    time.sleep(0.15)
                    cd.set_laser(False)
                self._post(self._on_status, b.idx, "EMERGENCY OFF: Current Cut", "fault")
                self._post(self._on_led, b.idx, "fault")
            except Exception:
                pass
        self._post(self._set_status, "Status: EMERGENCY LASER SHUTDOWN TRIGGERED.")

    def clear_faults(self):
        self._set_status("Status: Clearing faults...")

        def run():
            for d in self.system.devices:
                if not d.is_connected:
                    continue
                try:
                    with d.serial_lock:
                        for ch in range(1, d.num_channels + 1):
                            d.select_channel(ch); d._selected_channel = ch
                            time.sleep(0.1)
                            d.clear_module(); time.sleep(0.1)
                except Exception:
                    pass
            # Rescan every connected controller.
            self.is_scanning = True
            for u in self.system.units:
                if self._unit_connected(u):
                    self._scan_unit(u.id)
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
        self.mode_combo.setEnabled(enabled)
        for w in (self.t_ramp, self.i_ramp, self.t_off, self.btn_save, self.btn_load, self.btn_clear_prof):
            w.setEnabled(enabled)

    def _on_os_theme_changed(self, scheme):
        self.dark = (scheme == Qt.ColorScheme.Dark)
        self._apply_theme()

    def _apply_theme(self):
        app = QApplication.instance()
        app.setPalette(make_palette(self.dark))
        app.setStyleSheet(build_stylesheet(self.dark))
        self._style_combo_popups()
        # Re-apply dynamic per-widget colors that the stylesheet doesn't cover.
        for i in range(self.num_channels):
            self._on_live_output(i, "TEC", self.cards[i].live_tec.text())
            self._on_live_output(i, "LAS", self.cards[i].live_las.text())

    def _style_combo_popups(self):
        """Style each combo's popup list view directly. A global QSS descendant
        selector does not reliably reach the popup (a separate top-level), which
        left hovered items as unreadable white-on-white; setting the view's own
        stylesheet fixes it."""
        field_bg = "#33373c" if self.dark else "#ffffff"
        text = "#e6e6e6" if self.dark else "#1a1a1a"
        qss = (f"QAbstractItemView {{ background: {field_bg}; color: {text}; outline: 0; }}"
               f"QAbstractItemView::item {{ color: {text}; padding: 3px 6px; min-height: 20px; }}"
               f"QAbstractItemView::item:hover {{ background: #1976D2; color: #ffffff; }}"
               f"QAbstractItemView::item:selected {{ background: #1976D2; color: #ffffff; }}")
        # Also fix the palette so the highlight is blue-on-white even if the style
        # draws the hovered item via palette roles rather than the stylesheet.
        pal = QPalette()
        pal.setColor(QPalette.Base, QColor(field_bg))
        pal.setColor(QPalette.Text, QColor(text))
        pal.setColor(QPalette.Highlight, QColor("#1976D2"))
        pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
        combos = []
        if hasattr(self, "mode_combo"):
            combos.append(self.mode_combo)
        for c in getattr(self, "cards", []):
            combos += [c.tec_cmd, c.las_cmd]
        for combo in combos:
            view = combo.view()
            view.setStyleSheet(qss)
            view.setPalette(pal)

    def _mark_unsaved(self, *_):
        # Any edit that could change the run also changes the time estimates.
        self._update_mode_estimates()
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
            data = {"T_ramp": float(self.t_ramp.text()), "I_ramp": float(self.i_ramp.text()),
                    "T_OFF_Target": float(self.t_off.text()),
                    "Ramp_Mode": self._selected_mode(),
                    "View_Mode": "table" if self._table_mode else "cards",
                    "hardware": self.config,   # full controller layout (units + transports + pairings)
                    "channels": []}
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
        if self.is_executing:
            QMessageBox.warning(self, "Busy", "Stop the running sequence before loading a profile.")
            return
        try:
            with open(path) as f:
                data = json.load(f)
            # Restore the hardware layout first, then re-apply per-channel state.
            hw = data.get("hardware")
            if hw and hw.get("units"):
                self.config = hw
            else:
                # Legacy flat profile: one ILX LDC-3908 on its saved COM port.
                port = data.get("COM_Port", "")
                spec = ({"type": "serial", "port": port}
                        if port and port != "Demo Simulator" else {"type": "sim"})
                self.config = sysmod.single_ldc3908_config(spec)
            self._rebuild_system()

            self.t_ramp.setText(str(data["T_ramp"]))
            self.i_ramp.setText(str(data["I_ramp"]))
            self.t_off.setText(str(data["T_OFF_Target"]))
            mode = data.get("Ramp_Mode", "sequential")
            if mode in self._MODE_KEYS:
                self.mode_combo.blockSignals(True)
                self.mode_combo.setCurrentIndex(self._MODE_KEYS.index(mode))
                self.mode_combo.blockSignals(False)
            table = data.get("View_Mode", "table") != "cards"
            self.btn_view_table.setChecked(table)
            self.btn_view_cards.setChecked(not table)
            self._set_view_mode(table)
            for k, cfg in enumerate(data.get("channels", [])[:self.num_channels]):
                self.cards[k].t_target.setText(str(cfg.get("T_Target", 22.0)))
                self.cards[k].i_target.setText(str(cfg.get("I_Target", 0.0)))
                if cfg.get("Label"):
                    self.cards[k].label.setText(cfg["Label"])
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
        self.config = sysmod.default_config()
        self._rebuild_system()
        self.t_ramp.setText("0.1"); self.i_ramp.setText("0.5"); self.t_off.setText("22.0")
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(0)  # back to "One laser at a time"
        self.mode_combo.blockSignals(False)
        self.btn_view_table.setChecked(True)
        self.btn_view_cards.setChecked(False)
        self._set_view_mode(True)
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
        self.system.close_all()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = LDCMainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
