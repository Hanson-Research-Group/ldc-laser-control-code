#!/usr/bin/env python3
"""
Device-agnostic laser-controller driver interface.

The control engine (sequencer.py) and the GUI (main.py) talk ONLY to this
interface — never to device-specific commands. To add support for a new
controller, subclass LaserControllerDriver, declare its `capabilities`, and
implement the operations it supports; nothing in the engine or the GUI needs to
change. Concrete drivers today: the ILX Lightwave LDC-3908 (ldc3908.py, full)
plus sim-backed stubs for Thorlabs ITC and Wavelength TC10 / QCL1000.

Capabilities
------------
A driver declares CAP_TEMPERATURE and/or CAP_CURRENT. A *combined* controller
(ILX, Thorlabs) declares both and drives temperature and current for a channel.
A *split* controller declares one: the Wavelength TC10 is temperature-only, the
QCL1000 is current-only. A logical laser channel (system.ChannelBinding) may
therefore be served by two different physical drivers — the engine routes
temperature operations to one and current operations to the other.

Conventions
-----------
* Channels are 1-based. Every channel operation acts on the *currently selected*
  channel; the caller (the engine) selects it and owns the driver's lock.
  Single-channel controllers ignore select_channel and report channel 1.
* Read operations return parsed values (int / float / bool) and raise on failure.
  Floats may be NaN; the engine handles that.
* Write operations just send the command; the engine inserts settle delays and
  read-back verification.

Transport is pluggable (transport.py): open_serial()/open_visa()/open_simulator().
A driver for another link can override the transport wiring.

Cooperative stop: all drivers in a run share one StopToken; the engine polls
`is_stop_requested`. `is_stop_requested`/`is_emo_requested` are proxies onto the
shared token so existing callers (and tests) can set them on the driver.
"""

import threading
import time


# --- Capability tags ----------------------------------------------------------
CAP_TEMPERATURE = "temperature"
CAP_CURRENT = "current"


class StopToken:
    """A cooperative abort flag shared by every driver + the sequencer in one
    run, so a single Cancel/EMO reaches all controllers at once."""

    def __init__(self):
        self.is_stop_requested = False
        self.is_emo_requested = False

    def reset(self):
        self.is_stop_requested = False
        self.is_emo_requested = False


class LaserControllerDriver:
    """Abstract base. Concrete drivers set `num_channels`/`baudrate`/`capabilities`
    and implement the device operations at the bottom of this class."""

    num_channels = 8
    baudrate = 9600
    capabilities = frozenset({CAP_TEMPERATURE, CAP_CURRENT})
    # Human-readable model, shown in the UI (e.g. a unit box title).
    model = "Laser Controller"

    def __init__(self, num_channels=None, stop=None):
        if num_channels is not None:
            self.num_channels = num_channels
        self.transport = None
        self.serial_lock = threading.Lock()
        self.is_simulated = False
        # Which channel is currently selected on the link (None = unknown). The
        # engine's bus only re-selects + settles when this needs to change, so a
        # single-channel device settles once and a multi-channel device avoids
        # redundant channel switches within a sequence.
        self._selected_channel = None
        # Shared cooperative-abort token (assigned a common one by the system).
        self.stop = stop or StopToken()

    # --- capability helpers ---
    @property
    def supports_temperature(self):
        return CAP_TEMPERATURE in self.capabilities

    @property
    def supports_current(self):
        return CAP_CURRENT in self.capabilities

    # --- shared stop flags proxy onto the token ---
    @property
    def is_stop_requested(self):
        return self.stop.is_stop_requested

    @is_stop_requested.setter
    def is_stop_requested(self, v):
        self.stop.is_stop_requested = v

    @property
    def is_emo_requested(self):
        return self.stop.is_emo_requested

    @is_emo_requested.setter
    def is_emo_requested(self, v):
        self.stop.is_emo_requested = v

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    def open_serial(self, port, timeout=5.0):
        """Open an RS-232 link. Raises on failure."""
        from transport import SerialTransport
        self.is_simulated = False
        self._selected_channel = None
        self.transport = SerialTransport(port, baudrate=self.baudrate, timeout=timeout)
        self.transport.open()

    def open_visa(self, resource, timeout=5.0, backend=None):
        """Open a USB/VISA link. Raises a clear error if no VISA backend."""
        from transport import VisaTransport
        self.is_simulated = False
        self._selected_channel = None
        self.transport = VisaTransport(resource, timeout=timeout, backend=backend)
        self.transport.open()

    # Back-compat alias: the GUI's serial connect path calls open().
    def open(self, port, timeout=5.0):
        self.open_serial(port, timeout=timeout)

    def open_simulator(self):
        """Switch to the built-in Demo Simulator (see _sim_* hooks)."""
        self.is_simulated = True
        self.transport = None
        self._selected_channel = None
        self._sim_reset()

    def close(self):
        with self.serial_lock:
            if self.transport:
                self.transport.close()
                self.transport = None
        self.is_simulated = False
        self._selected_channel = None

    @property
    def is_connected(self):
        return self.is_simulated or (self.transport is not None and self.transport.is_open)

    def safe_pause(self, t):
        """Sleep, then raise HALT if a stop was requested during the pause."""
        time.sleep(t)
        if self.stop.is_stop_requested:
            raise RuntimeError("HALT")

    # ------------------------------------------------------------------
    # Low-level transport used by concrete drivers to build their commands
    # ------------------------------------------------------------------
    def _write(self, cmd):
        if self.is_simulated:
            self._sim_exec(cmd)
        elif self.transport:
            self.transport.write(cmd)

    def _read(self):
        if self.is_simulated:
            return self._sim_reply()
        if self.transport:
            return self.transport.read()
        return ""

    def _query(self, cmd):
        if self.is_simulated:
            self._sim_exec(cmd)
            return self._sim_reply()
        if self.transport:
            return self.transport.query(cmd)
        return ""

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
    # Device operations — implement the ones a driver's capabilities cover.
    # All act on the currently selected channel (except select_channel).
    # ------------------------------------------------------------------
    # --- addressing (single-channel default: no-op select, fixed channel 1) ---
    def select_channel(self, ch):
        """Select the active channel. No-op on single-channel controllers."""

    def active_channel(self):
        """-> int: which channel is currently selected (1 on single-channel)."""
        return 1

    # --- temperature capability ---
    def set_tec(self, on):
        raise NotImplementedError

    def tec_output(self):
        """-> int (1/0): TEC output state."""
        raise NotImplementedError

    def set_temp_setpoint(self, celsius):
        raise NotImplementedError

    def temp_setpoint(self):
        """-> float: the TEC temperature setpoint (degC)."""
        raise NotImplementedError

    def temperature(self):
        """-> float: the measured temperature (degC); may be NaN."""
        raise NotImplementedError

    def temp_limit(self):
        """-> float: hardware high-temperature limit (degC); may be NaN."""
        raise NotImplementedError

    # --- current capability ---
    def set_laser(self, on):
        raise NotImplementedError

    def laser_output(self):
        """-> int (1/0): laser current-source output state."""
        raise NotImplementedError

    def set_current_setpoint(self, milliamps):
        raise NotImplementedError

    def current_setpoint(self):
        """-> float: the laser current setpoint (mA)."""
        raise NotImplementedError

    def current(self):
        """-> float: the measured laser current (mA); may be NaN."""
        raise NotImplementedError

    def current_limit(self):
        """-> float: hardware current limit (mA); may be NaN."""
        raise NotImplementedError

    def disable_modulation(self):
        """Disable external modulation (current-source concept). Default no-op
        for controllers without a modulation input."""

    def enable_modulation(self):
        """Enable external modulation. Default no-op."""

    def modulation(self):
        """-> int (1/0): external-modulation switch state. Default 0."""
        return 0

    # --- errors (default: no error source) ---
    def read_errors(self):
        """-> (has_error: bool, message: str) for the selected channel."""
        return False, ""

    def clear_module(self):
        """Clear latched module fault state on the selected channel. Default no-op."""
