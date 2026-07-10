#!/usr/bin/env python3
"""
Thorlabs ITC-series combined laser diode + TEC controller — driver STUB.

The ITC5225 / ITC4005 / CLD1010 are single-channel *combined* controllers (USB,
SCPI over USBTMC). One device serves both temperature and current for one laser,
so it maps to a KIND_COMBINED unit with a single channel.

STATUS: sim-backed stub. The simulator is fully functional so the architecture
runs headless. The SCPI strings below follow the Thorlabs LD/TEC controller SCPI
style but are marked "TODO: validate on hardware" — confirm against the ITC
programming reference (and note current is in Amps on the real instrument, while
this engine works in mA) before connecting hardware.
"""

import math

from driver import LaserControllerDriver, CAP_TEMPERATURE, CAP_CURRENT


class ThorlabsITCDriver(LaserControllerDriver):
    num_channels = 1
    capabilities = frozenset({CAP_TEMPERATURE, CAP_CURRENT})  # combined T + I
    model = "Thorlabs ITC"

    def __init__(self, num_channels=1, stop=None):
        super().__init__(num_channels=1, stop=stop)
        self._sim_reset()

    # ------------------------------------------------------------------
    # Simulator
    # ------------------------------------------------------------------
    def _sim_reset(self):
        self.sim_state = {"T_actual": 22.0, "T_set": 22.0, "TEC_ON": 0, "T_lim": 60.0,
                          "I_actual": 0.0, "I_set": 0.0, "LAS_ON": 0, "I_lim": 1000.0}
        self.sim_query_response = ""

    def _sim_exec(self, cmd):
        st = self.sim_state
        parts = cmd.split()
        root = parts[0] if parts else ""
        # --- temperature (SOURce2 / OUTPut2) ---
        if cmd == "OUTP2:STAT?":
            self.sim_query_response = str(st["TEC_ON"])
        elif root == "OUTP2:STAT" and len(parts) > 1:
            try:
                st["TEC_ON"] = int(parts[-1])
            except Exception:
                pass
        elif cmd == "SOUR2:TEMP?":
            self.sim_query_response = f"{st['T_set']:.2f}"
        elif root == "SOUR2:TEMP" and len(parts) > 1:
            try:
                st["T_set"] = float(parts[-1])
                st["T_actual"] = st["T_set"]
            except Exception:
                pass
        elif cmd == "MEAS:TEMP?":
            self.sim_query_response = f"{st['T_actual']:.2f}"
        elif cmd == "SOUR2:TEMP:LIM:HIGH?":
            self.sim_query_response = f"{st['T_lim']:.2f}"
        # --- current (SOURce1 / OUTPut1) ---
        elif cmd == "OUTP1:STAT?":
            self.sim_query_response = str(st["LAS_ON"])
        elif root == "OUTP1:STAT" and len(parts) > 1:
            try:
                st["LAS_ON"] = int(parts[-1])
            except Exception:
                pass
        elif cmd == "SOUR1:CURR?":
            self.sim_query_response = f"{st['I_set']:.2f}"
        elif root == "SOUR1:CURR" and len(parts) > 1:
            try:
                st["I_set"] = float(parts[-1])
                st["I_actual"] = st["I_set"]
            except Exception:
                pass
        elif cmd == "MEAS:CURR?":
            self.sim_query_response = f"{st['I_actual']:.2f}"
        elif cmd == "SOUR1:CURR:LIM?":
            self.sim_query_response = f"{st['I_lim']:.2f}"

    def _sim_reply(self):
        return self.sim_query_response

    # ------------------------------------------------------------------
    # Temperature operations  (TODO: validate SCPI on hardware)
    # ------------------------------------------------------------------
    def set_tec(self, on):
        self._write(f"OUTP2:STAT {1 if on else 0}")

    def tec_output(self):
        return int(float(self._query("OUTP2:STAT?")))

    def set_temp_setpoint(self, celsius):
        self._write(f"SOUR2:TEMP {celsius:.2f}")

    def temp_setpoint(self):
        return float(self._query("SOUR2:TEMP?"))

    def temperature(self):
        try:
            return float(self._query("MEAS:TEMP?"))
        except (TypeError, ValueError):
            return math.nan

    def temp_limit(self):
        return float(self._query("SOUR2:TEMP:LIM:HIGH?"))

    # ------------------------------------------------------------------
    # Current operations  (TODO: validate SCPI on hardware; real unit is Amps)
    # ------------------------------------------------------------------
    def set_laser(self, on):
        self._write(f"OUTP1:STAT {1 if on else 0}")

    def laser_output(self):
        return int(float(self._query("OUTP1:STAT?")))

    def set_current_setpoint(self, milliamps):
        self._write(f"SOUR1:CURR {milliamps:.2f}")

    def current_setpoint(self):
        return float(self._query("SOUR1:CURR?"))

    def current(self):
        try:
            return float(self._query("MEAS:CURR?"))
        except (TypeError, ValueError):
            return math.nan

    def current_limit(self):
        return float(self._query("SOUR1:CURR:LIM?"))
