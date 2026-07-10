#!/usr/bin/env python3
"""
Device-agnostic laser-controller driver interface.

The control engine (sequencer.py) and the GUI (main.py) talk ONLY to this
interface — never to device-specific commands. To add support for a new
controller, subclass LaserControllerDriver and implement the operations below;
nothing in the engine or the GUI needs to change. The one concrete driver today
is the ILX Lightwave LDC-3908 (see ldc3908.py).

Conventions
-----------
* Channels are 1-based. Every channel operation acts on the *currently selected*
  channel; the caller (the engine) selects it and owns the serial lock.
* Read operations return parsed values (int / float / bool) and raise on failure
  (no response or an unparseable reply). Floats may be NaN; the engine handles
  that. The engine wraps reads in try/except and applies its fallbacks.
* Write operations just send the command; the engine inserts the settle/pause
  delays and the read-back verification.

A driver also carries the connection lifecycle and a couple of cooperative
execution flags (is_stop_requested / is_emo_requested) that the engine polls.
Transport here is generic RS-232; a driver for a non-serial device can override
open()/close()/_write()/_read()/_query().
"""

import threading
import time

import serial


class LaserControllerDriver:
    """Abstract base. Concrete drivers set `num_channels`/`baudrate` and implement
    the device operations at the bottom of this class."""

    num_channels = 8
    baudrate = 9600

    def __init__(self, num_channels=None):
        if num_channels is not None:
            self.num_channels = num_channels
        self.ser = None
        self.serial_lock = threading.Lock()
        self.is_simulated = False
        # Cooperative flags the engine sets; ramps poll is_stop_requested.
        self.is_stop_requested = False
        self.is_emo_requested = False

    # ------------------------------------------------------------------
    # Connection lifecycle (generic RS-232; override for other transports)
    # ------------------------------------------------------------------
    def open(self, port, timeout=5.0):
        """Open a real serial connection. Raises on failure."""
        self.is_simulated = False
        self.ser = serial.Serial(port, baudrate=self.baudrate, timeout=timeout)
        self.ser.reset_input_buffer()

    def open_simulator(self):
        """Switch to the built-in Demo Simulator (see _sim_* hooks)."""
        self.is_simulated = True
        self.ser = None
        self._sim_reset()

    def close(self):
        with self.serial_lock:
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None
        self.is_simulated = False

    @property
    def is_connected(self):
        return self.is_simulated or self.ser is not None

    def safe_pause(self, t):
        """Sleep, then raise HALT if a stop was requested during the pause."""
        time.sleep(t)
        if self.is_stop_requested:
            raise RuntimeError("HALT")

    # ------------------------------------------------------------------
    # Low-level transport used by concrete drivers to build their commands
    # ------------------------------------------------------------------
    def _write(self, cmd):
        if self.is_simulated:
            self._sim_exec(cmd)
        elif self.ser:
            try:
                self.ser.write(f"{cmd}\n".encode("ascii"))
            except serial.SerialException as e:
                raise serial.SerialException(f"Hardware connection lost during write: {e}")

    def _read(self):
        if self.is_simulated:
            return self._sim_reply()
        if self.ser:
            try:
                raw = self.ser.readline()
            except serial.SerialException as e:
                raise serial.SerialException(f"Hardware connection lost during read: {e}")
            try:
                return raw.decode("ascii").strip()
            except Exception:
                return ""
        return ""

    def _query(self, cmd):
        """Flush stale input, send, wait, read one line."""
        if not self.is_simulated and self.ser:
            self.ser.reset_input_buffer()
        self._write(cmd)
        time.sleep(0.15)
        return self._read()

    # ------------------------------------------------------------------
    # Simulator hooks (override in a driver that ships a simulator)
    # ------------------------------------------------------------------
    def _sim_reset(self):
        pass

    def _sim_exec(self, cmd):
        pass

    def _sim_reply(self):
        return ""

    # ------------------------------------------------------------------
    # Device operations — implement these in a concrete driver.
    # All act on the currently selected channel (except select_channel).
    # ------------------------------------------------------------------
    def select_channel(self, ch):
        raise NotImplementedError

    def active_channel(self):
        """-> int: which channel is currently selected."""
        raise NotImplementedError

    def disable_modulation(self):
        raise NotImplementedError

    def enable_modulation(self):
        raise NotImplementedError

    def modulation(self):
        """-> int (1/0): external-modulation switch state."""
        raise NotImplementedError

    def set_tec(self, on):
        raise NotImplementedError

    def tec_output(self):
        """-> int (1/0): TEC output state."""
        raise NotImplementedError

    def set_laser(self, on):
        raise NotImplementedError

    def laser_output(self):
        """-> int (1/0): laser current-source output state."""
        raise NotImplementedError

    def set_temp_setpoint(self, celsius):
        raise NotImplementedError

    def temp_setpoint(self):
        """-> float: the TEC temperature setpoint (degC)."""
        raise NotImplementedError

    def temperature(self):
        """-> float: the measured temperature (degC); may be NaN."""
        raise NotImplementedError

    def set_current_setpoint(self, milliamps):
        raise NotImplementedError

    def current_setpoint(self):
        """-> float: the laser current setpoint (mA)."""
        raise NotImplementedError

    def current(self):
        """-> float: the measured laser current (mA); may be NaN."""
        raise NotImplementedError

    def temp_limit(self):
        """-> float: hardware high-temperature limit (degC); may be NaN."""
        raise NotImplementedError

    def current_limit(self):
        """-> float: hardware current limit (mA); may be NaN."""
        raise NotImplementedError

    def read_errors(self):
        """-> (has_error: bool, message: str) for the selected channel."""
        raise NotImplementedError

    def clear_module(self):
        """Clear latched module fault state on the selected channel."""
        raise NotImplementedError
