#!/usr/bin/env python3
"""
ILX Lightwave LDC-3908 laser diode controller driver.

Speaks the LDC-3908 SCPI command set over RS-232 and ships a built-in Demo
Simulator. Implements the device-agnostic LaserControllerDriver interface
(driver.py) so the control engine and GUI never see a raw SCPI command — adding
another controller is a matter of writing a sibling driver module.

Reference: ILX Lightwave LDC-3908 / LDC-3916370 series user manuals.
"""

import math
import re
import time

from driver import LaserControllerDriver, CAP_TEMPERATURE, CAP_CURRENT


class LDC3908Driver(LaserControllerDriver):
    num_channels = 8
    baudrate = 9600
    capabilities = frozenset({CAP_TEMPERATURE, CAP_CURRENT})  # combined T + I
    model = "ILX Lightwave LDC-3908"

    # ILX Lightwave module error codes returned by MODERR?.
    KNOWN_ERROR_CODES = ["501", "504", "503", "505", "508", "511", "404", "407"]

    def __init__(self, num_channels=8, stop=None):
        super().__init__(num_channels=num_channels, stop=stop)
        self._sim_reset()

    # ------------------------------------------------------------------
    # Simulator (mimics LDC-3908 SCPI responses)
    # ------------------------------------------------------------------
    def _sim_reset(self):
        n = self.num_channels
        # is_installed: 1=installed+laser, 2=installed/no-laser (T<0), 0=empty slot
        installed = [1, 1, 0, 2, 1, 0, 0, 0][:n] + [0] * max(0, n - 8)
        self.sim_state = {
            'curr_chan': 1,
            'T_actual': [22.0] * n,
            'I_actual': [0.0] * n,
            'TEC_ON': [0] * n,
            'LAS_ON': [0] * n,
            'LAS_MOD': [1] * n,
            'is_installed': installed,
        }
        self.sim_query_response = ""
        # Injectable MODERR? reply so the fault path is testable.
        self.sim_moderr = "0"

    def _sim_exec(self, cmd):
        parts = cmd.split()
        if not parts:
            return
        cmd_root = parts[0]
        st = self.sim_state

        if cmd == "CHAN?":
            self.sim_query_response = str(st['curr_chan'])
        elif cmd_root == "CHAN":
            ch = int(parts[-1])
            if st['is_installed'][ch - 1] == 0:
                self.sim_query_response = "ERROR"
            else:
                st['curr_chan'] = ch
        elif cmd_root in ["TEC:T?", "TEC:SYNCT?"]:
            ch = st['curr_chan']
            if st['is_installed'][ch - 1] == 2:
                self.sim_query_response = "-10.5"
            else:
                self.sim_query_response = f"{st['T_actual'][ch - 1]:.2f}"
        elif cmd_root == "TEC:T":
            if len(parts) > 1:
                try:
                    st['T_actual'][st['curr_chan'] - 1] = float(parts[-1])
                except Exception:
                    pass
        elif cmd_root in ["LAS:LDI?", "LAS:SYNCLDI?"]:
            ch = st['curr_chan']
            if st['is_installed'][ch - 1] == 2:
                self.sim_query_response = "NaN"
            else:
                self.sim_query_response = f"{st['I_actual'][ch - 1]:.2f}"
        elif cmd_root == "LAS:LDI":
            if len(parts) > 1:
                try:
                    st['I_actual'][st['curr_chan'] - 1] = float(parts[-1])
                except Exception:
                    pass
        elif cmd == "TEC:OUT?":
            self.sim_query_response = str(st['TEC_ON'][st['curr_chan'] - 1])
        elif cmd_root == "TEC:OUTPUT":
            if len(parts) > 1:
                try:
                    st['TEC_ON'][st['curr_chan'] - 1] = int(parts[-1])
                except Exception:
                    pass
        elif cmd == "LAS:OUT?":
            self.sim_query_response = str(st['LAS_ON'][st['curr_chan'] - 1])
        elif cmd_root == "LAS:OUTPUT":
            if len(parts) > 1:
                try:
                    st['LAS_ON'][st['curr_chan'] - 1] = int(parts[-1])
                except Exception:
                    pass
        elif cmd == "LAS:LIM:I?":
            self.sim_query_response = "150"
        elif cmd == "TEC:LIM:THI?":
            self.sim_query_response = "80"
        elif cmd == "LAS:MOD?":
            self.sim_query_response = str(st['LAS_MOD'][st['curr_chan'] - 1])
        elif cmd_root == "LAS:MOD":
            if len(parts) > 1:
                try:
                    st['LAS_MOD'][st['curr_chan'] - 1] = int(parts[-1])
                except Exception:
                    pass
        elif cmd == "MODERR?":
            self.sim_query_response = self.sim_moderr

    def _sim_reply(self):
        return self.sim_query_response

    # ------------------------------------------------------------------
    # Device operations (act on the currently selected channel)
    # ------------------------------------------------------------------
    def select_channel(self, ch):
        self._write(f"CHAN {ch}")

    def active_channel(self):
        return int(self._query("CHAN?"))

    def disable_modulation(self):
        self._write("LAS:MOD 0")

    def enable_modulation(self):
        self._write("LAS:MOD 1")

    def modulation(self):
        return int(float(self._query("LAS:MOD?")))

    def set_tec(self, on):
        self._write(f"TEC:OUTPUT {1 if on else 0}")

    def tec_output(self):
        return int(float(self._query("TEC:OUT?")))

    def set_laser(self, on):
        self._write(f"LAS:OUTPUT {1 if on else 0}")

    def laser_output(self):
        return int(float(self._query("LAS:OUT?")))

    def set_temp_setpoint(self, celsius):
        self._write(f"TEC:T {celsius:.2f}")

    def temp_setpoint(self):
        return float(self._query("TEC:T?"))

    def temperature(self):
        return float(self._query("TEC:SYNCT?"))

    def set_current_setpoint(self, milliamps):
        self._write(f"LAS:LDI {milliamps:.2f}")

    def current_setpoint(self):
        return float(self._query("LAS:LDI?"))

    def current(self):
        return float(self._query("LAS:SYNCLDI?"))

    def temp_limit(self):
        return float(self._query("TEC:LIM:THI?"))

    def current_limit(self):
        return float(self._query("LAS:LIM:I?"))

    def clear_module(self):
        self._write("*CLS")

    # --- errors ---
    @staticmethod
    def parse_moderr(err_str):
        """Classify a MODERR? response string into (has_error, message).

        Tokenize into exact integer codes rather than substring-matching (so
        "5040"/"1504" do not false-match "504", and "0,0" reads as no fault)."""
        err_str = (err_str or "").strip()
        codes = re.findall(r"\d+", err_str)
        if not codes or all(int(c) == 0 for c in codes):
            return False, ""
        if "501" in codes:
            return True, f"Interlock Error (E501): Key switch is off. [{err_str}]"
        elif "504" in codes:
            return True, f"Current Limit Reached (E504). [{err_str}]"
        elif "503" in codes:
            return True, f"Voltage Limit Reached / Open Circuit (E503). [{err_str}]"
        elif "505" in codes:
            return True, f"Voltage Limit Warning (E505). [{err_str}]"
        elif "508" in codes:
            return True, f"TEC Off Status Forced LAS Off (E508). [{err_str}]"
        elif "511" in codes:
            return True, f"Hardware Error (E511). [{err_str}]"
        elif "404" in codes or "407" in codes:
            return True, f"Temperature Limit Error. [{err_str}]"
        else:
            return True, f"Module Error Code: {err_str}"

    def read_errors(self):
        """Query MODERR? on the (already selected) channel and classify it."""
        err_str = ""
        codes = []
        for _ in range(3):
            if not self.is_simulated and self.transport is not None:
                self.transport.flush_input()
            self._write("MODERR?")
            time.sleep(0.15)
            err_str = self._read().strip()
            codes = re.findall(r"\d+", err_str)
            if not codes or all(int(c) == 0 for c in codes):
                return False, ""
            if any(c in codes for c in self.KNOWN_ERROR_CODES):
                break
            time.sleep(0.2)
        return self.parse_moderr(err_str)
