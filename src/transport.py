#!/usr/bin/env python3
"""
Transport seam — *how* we talk to a controller, separate from *what* we say.

A driver (driver.py) builds device commands and hands them to a transport; the
transport owns the physical link. This lets the same driver logic run over
different links (RS-232 serial, USB/VISA) and lets the engine stay
controller-agnostic.

Transports here assume the caller serializes access (the driver holds its own
lock around each transaction), so transports themselves are not internally
locked.

  * SerialTransport — RS-232 via pyserial (the ILX LDC-3908 link).
  * VisaTransport   — USBTMC / Test-&-Measurement class via pyvisa. The concrete
    VISA backend (pyvisa-py or NI-VISA) is chosen at deployment; until one is
    installed/configured, open() raises a clear, actionable error.
"""

import time

import serial


class SerialTransport:
    """RS-232 line. Mirrors the original driver serial behavior: newline-framed
    ASCII, flush-before-query, a fixed post-write settle, then one readline."""

    QUERY_SETTLE = 0.15

    def __init__(self, port, baudrate=9600, timeout=5.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None

    @property
    def is_open(self):
        return self.ser is not None

    def open(self):
        self.ser = serial.Serial(self.port, baudrate=self.baudrate, timeout=self.timeout)
        self.ser.reset_input_buffer()

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def write(self, cmd):
        try:
            self.ser.write(f"{cmd}\n".encode("ascii"))
        except serial.SerialException as e:
            raise serial.SerialException(f"Hardware connection lost during write: {e}")

    def read(self):
        try:
            raw = self.ser.readline()
        except serial.SerialException as e:
            raise serial.SerialException(f"Hardware connection lost during read: {e}")
        try:
            return raw.decode("ascii").strip()
        except Exception:
            return ""

    def query(self, cmd):
        self.ser.reset_input_buffer()
        self.write(cmd)
        time.sleep(self.QUERY_SETTLE)
        return self.read()

    def flush_input(self):
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass


class VisaTransport:
    """USBTMC / VISA link (Thorlabs ITC, Wavelength TC10 / QCL1000, ...).

    The VISA backend is deliberately not pinned here — a lab installs either the
    pure-Python pyvisa-py backend or NI-VISA and passes its identifier as
    `backend` (empty string lets pyvisa pick its default). open() surfaces a
    clear instruction if pyvisa/backend is unavailable, so the rest of the app
    (and the simulators) run without VISA installed.
    """

    def __init__(self, resource, timeout=5.0, backend=None):
        self.resource = resource
        self.timeout = timeout
        self.backend = backend
        self._inst = None

    @property
    def is_open(self):
        return self._inst is not None

    def open(self):
        try:
            import pyvisa
        except ImportError as e:
            raise RuntimeError(
                "USB/VISA controllers require the 'pyvisa' package and a VISA backend "
                "(install 'pyvisa-py' for a pure-Python backend, or NI-VISA). "
                "Install one, then set the backend in the hardware configuration."
            ) from e
        try:
            rm = pyvisa.ResourceManager(self.backend or "")
            self._inst = rm.open_resource(self.resource)
            self._inst.timeout = int(self.timeout * 1000)
        except Exception as e:
            raise RuntimeError(f"Failed to open VISA resource {self.resource!r}: {e}") from e

    def close(self):
        if self._inst is not None:
            try:
                self._inst.close()
            except Exception:
                pass
            self._inst = None

    def write(self, cmd):
        self._inst.write(cmd)

    def read(self):
        return self._inst.read().strip()

    def query(self, cmd):
        return self._inst.query(cmd).strip()

    def flush_input(self):
        try:
            self._inst.clear()
        except Exception:
            pass
