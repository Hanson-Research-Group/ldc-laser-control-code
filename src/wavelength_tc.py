#!/usr/bin/env python3
"""
Wavelength Electronics TC LAB-series temperature controller — driver STUB.

The TC10 LAB is a single-channel *temperature-only* instrument (USB / Ethernet,
Test-&-Measurement class command set). It provides the temperature half of a
laser line; pair it with a WavelengthQCLDriver (current) to form one logical
laser channel (see system.KIND_PAIRING).

STATUS: sim-backed stub. The built-in simulator is fully functional so the
multi-controller / split-channel architecture runs headless. The real command
strings below are placeholders marked "TODO: validate on hardware" — confirm
them against the Wavelength LAB Series Command Set before connecting hardware.
"""

import math

from driver import LaserControllerDriver, CAP_TEMPERATURE


class WavelengthTCDriver(LaserControllerDriver):
    num_channels = 1
    capabilities = frozenset({CAP_TEMPERATURE})  # temperature only
    model = "Wavelength Electronics TC LAB"

    def __init__(self, num_channels=1, stop=None):
        super().__init__(num_channels=1, stop=stop)
        self._sim_reset()

    # ------------------------------------------------------------------
    # Simulator
    # ------------------------------------------------------------------
    def _sim_reset(self):
        self.sim_state = {"T_actual": 22.0, "T_set": 22.0, "TEC_ON": 0, "T_lim": 60.0}
        self.sim_query_response = ""

    def _sim_exec(self, cmd):
        st = self.sim_state
        parts = cmd.split()
        root = parts[0] if parts else ""
        if cmd == "TEC:OUT?":
            self.sim_query_response = str(st["TEC_ON"])
        elif root == "TEC:OUT" and len(parts) > 1:
            try:
                st["TEC_ON"] = int(parts[-1])
            except Exception:
                pass
        elif cmd in ("TEC:SET?",):
            self.sim_query_response = f"{st['T_set']:.2f}"
        elif root == "TEC:SET" and len(parts) > 1:
            try:
                st["T_set"] = float(parts[-1])
                st["T_actual"] = st["T_set"]   # sim tracks setpoint instantly
            except Exception:
                pass
        elif cmd == "TEC:ACT?":
            self.sim_query_response = f"{st['T_actual']:.2f}"
        elif cmd == "TEC:LIM?":
            self.sim_query_response = f"{st['T_lim']:.2f}"

    def _sim_reply(self):
        return self.sim_query_response

    # ------------------------------------------------------------------
    # Temperature operations  (TODO: validate command strings on hardware)
    # ------------------------------------------------------------------
    def set_tec(self, on):
        self._write(f"TEC:OUT {1 if on else 0}")

    def tec_output(self):
        return int(float(self._query("TEC:OUT?")))

    def set_temp_setpoint(self, celsius):
        self._write(f"TEC:SET {celsius:.2f}")

    def temp_setpoint(self):
        return float(self._query("TEC:SET?"))

    def temperature(self):
        try:
            return float(self._query("TEC:ACT?"))
        except (TypeError, ValueError):
            return math.nan

    def temp_limit(self):
        return float(self._query("TEC:LIM?"))
