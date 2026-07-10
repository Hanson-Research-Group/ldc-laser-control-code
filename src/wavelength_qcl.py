#!/usr/bin/env python3
"""
Wavelength Electronics QCL LAB-series current driver — driver STUB.

The QCL1000 LAB is a single-channel *current-only* instrument (USB / Ethernet,
Test-&-Measurement class command set, up to 1 A). It provides the current half
of a laser line; pair it with a WavelengthTCDriver (temperature) to form one
logical laser channel (see system.KIND_PAIRING).

STATUS: sim-backed stub. The simulator is fully functional so the split-channel
architecture runs headless. The real command strings are placeholders marked
"TODO: validate on hardware" — confirm against the Wavelength LAB Series Command
Set before connecting hardware.
"""

import math

from driver import LaserControllerDriver, CAP_CURRENT


class WavelengthQCLDriver(LaserControllerDriver):
    num_channels = 1
    capabilities = frozenset({CAP_CURRENT})  # current only
    model = "Wavelength Electronics QCL LAB"

    def __init__(self, num_channels=1, stop=None):
        super().__init__(num_channels=1, stop=stop)
        self._sim_reset()

    # ------------------------------------------------------------------
    # Simulator
    # ------------------------------------------------------------------
    def _sim_reset(self):
        self.sim_state = {"I_actual": 0.0, "I_set": 0.0, "LAS_ON": 0, "I_lim": 1000.0}
        self.sim_query_response = ""

    def _sim_exec(self, cmd):
        st = self.sim_state
        parts = cmd.split()
        root = parts[0] if parts else ""
        if cmd == "LAS:OUT?":
            self.sim_query_response = str(st["LAS_ON"])
        elif root == "LAS:OUT" and len(parts) > 1:
            try:
                st["LAS_ON"] = int(parts[-1])
            except Exception:
                pass
        elif cmd == "LAS:SET?":
            self.sim_query_response = f"{st['I_set']:.2f}"
        elif root == "LAS:SET" and len(parts) > 1:
            try:
                st["I_set"] = float(parts[-1])
                st["I_actual"] = st["I_set"]   # sim tracks setpoint instantly
            except Exception:
                pass
        elif cmd == "LAS:ACT?":
            self.sim_query_response = f"{st['I_actual']:.2f}"
        elif cmd == "LAS:LIM?":
            self.sim_query_response = f"{st['I_lim']:.2f}"

    def _sim_reply(self):
        return self.sim_query_response

    # ------------------------------------------------------------------
    # Current operations  (TODO: validate command strings on hardware)
    # ------------------------------------------------------------------
    def set_laser(self, on):
        self._write(f"LAS:OUT {1 if on else 0}")

    def laser_output(self):
        return int(float(self._query("LAS:OUT?")))

    def set_current_setpoint(self, milliamps):
        self._write(f"LAS:SET {milliamps:.2f}")

    def current_setpoint(self):
        return float(self._query("LAS:SET?"))

    def current(self):
        try:
            return float(self._query("LAS:ACT?"))
        except (TypeError, ValueError):
            return math.nan

    def current_limit(self):
        return float(self._query("LAS:LIM?"))
