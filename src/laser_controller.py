#!/usr/bin/env python3
"""
UI-agnostic control core for the Newport LDC-3908 Modular Laser Diode Controller.

This module owns everything that talks to (or simulates) the hardware and contains
NO GUI code. It can be imported and exercised head-less — see test_laser_controller.py.

Responsibilities:
  * Serial connection lifecycle (open/close) and the built-in Demo Simulator.
  * Low-level SCPI command I/O wrappers (send/read/query/cmd_pause).
  * Safety helpers shared by the sequencer: safe_pause (cooperative HALT),
    verify_hw_state (read-back verification), and MODERR? fault parsing.
  * The shared control flags (is_stop_requested / is_emo_requested) that the
    higher-level sequencer sets and the ramp loops poll.

The GUI (main.py) composes an instance of LaserController and drives the
higher-level scan / telemetry / ramp / sequence orchestration on top of it.
"""

import math
import re
import time
import threading

import serial


class LaserController:
    # Recognized Newport module error codes returned by MODERR?.
    KNOWN_ERROR_CODES = ["501", "504", "503", "505", "508", "511", "404", "407"]

    def __init__(self, num_channels=8):
        self.ser = None
        self.serial_lock = threading.Lock()
        self.num_channels = num_channels
        self.is_simulated = False

        # Cooperative control flags. The sequencer sets these; ramp loops and
        # safe_pause() poll is_stop_requested to abort a ramp promptly.
        self.is_stop_requested = False
        self.is_emo_requested = False

        # --- Simulation Mock State ---
        # 1=Installed/Good, 2=Installed/NoLaser(T<0), 0=Empty Slot
        self.sim_state = {
            'curr_chan': 1,
            'T_actual': [22.0] * num_channels,
            'I_actual': [0.0] * num_channels,
            'TEC_ON': [0] * num_channels,
            'LAS_ON': [0] * num_channels,
            'LAS_MOD': [1] * num_channels,
            'is_installed': [1, 1, 0, 2, 1, 0, 0, 0]  # Exact match to MATLAB
        }
        self.sim_query_response = ""
        # Injectable MODERR? response so the fault path can be exercised in the
        # simulator / tests. Real hardware overrides this via its own responses.
        self.sim_moderr = "0"

    # ----------------------------------------------------
    # CONNECTION LIFECYCLE
    # ----------------------------------------------------
    def open(self, port, baudrate=9600, timeout=5.0):
        """Open a real serial connection. Raises on failure."""
        self.is_simulated = False
        self.ser = serial.Serial(port, baudrate=baudrate, timeout=timeout)
        self.ser.reset_input_buffer()

    def open_simulator(self):
        self.is_simulated = True
        self.ser = None

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

    # ----------------------------------------------------
    # LOW-LEVEL COMMAND I/O
    # ----------------------------------------------------
    def send_cmd(self, cmd):
        if self.is_simulated:
            self.process_sim_cmd(cmd)
        else:
            if self.ser:
                try:
                    self.ser.write(f"{cmd}\n".encode("ascii"))
                except serial.SerialException as e:
                    raise serial.SerialException(f"Hardware connection lost during write: {e}")

    def read_cmd(self):
        if self.is_simulated:
            return self.process_sim_query()
        else:
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

    def query_cmd(self, cmd):
        # Flush stale data, send query, wait, read response
        if not self.is_simulated and self.ser:
            self.ser.reset_input_buffer()
        self.send_cmd(cmd)
        time.sleep(0.15)
        return self.read_cmd()

    def cmd_pause(self, cmd):
        self.send_cmd(cmd)
        time.sleep(0.15)

    # ----------------------------------------------------
    # SIMULATION MOCK PROCESSOR
    # ----------------------------------------------------
    def process_sim_cmd(self, cmd):
        parts = cmd.split()
        if not parts:
            return

        cmd_root = parts[0]

        if cmd == "CHAN?":
            self.sim_query_response = str(self.sim_state['curr_chan'])

        elif cmd_root == "CHAN":
            ch = int(parts[-1])
            if self.sim_state['is_installed'][ch - 1] == 0:
                self.sim_query_response = "ERROR"
            else:
                self.sim_state['curr_chan'] = ch

        elif cmd_root in ["TEC:T?", "TEC:SYNCT?"]:
            ch = self.sim_state['curr_chan']
            if self.sim_state['is_installed'][ch - 1] == 2:
                self.sim_query_response = "-10.5"
            else:
                self.sim_query_response = f"{self.sim_state['T_actual'][ch - 1]:.2f}"

        elif cmd_root == "TEC:T":
            if len(parts) > 1:
                ch = self.sim_state['curr_chan']
                try:
                    self.sim_state['T_actual'][ch - 1] = float(parts[-1])
                except Exception:
                    pass

        elif cmd_root in ["LAS:LDI?", "LAS:SYNCLDI?"]:
            ch = self.sim_state['curr_chan']
            if self.sim_state['is_installed'][ch - 1] == 2:
                self.sim_query_response = "NaN"
            else:
                self.sim_query_response = f"{self.sim_state['I_actual'][ch - 1]:.2f}"

        elif cmd_root == "LAS:LDI":
            if len(parts) > 1:
                ch = self.sim_state['curr_chan']
                try:
                    self.sim_state['I_actual'][ch - 1] = float(parts[-1])
                except Exception:
                    pass

        elif cmd == "TEC:OUT?":
            ch = self.sim_state['curr_chan']
            self.sim_query_response = str(self.sim_state['TEC_ON'][ch - 1])

        elif cmd_root == "TEC:OUTPUT":
            if len(parts) > 1:
                ch = self.sim_state['curr_chan']
                try:
                    self.sim_state['TEC_ON'][ch - 1] = int(parts[-1])
                except Exception:
                    pass

        elif cmd == "LAS:OUT?":
            ch = self.sim_state['curr_chan']
            self.sim_query_response = str(self.sim_state['LAS_ON'][ch - 1])

        elif cmd_root == "LAS:OUTPUT":
            if len(parts) > 1:
                ch = self.sim_state['curr_chan']
                try:
                    self.sim_state['LAS_ON'][ch - 1] = int(parts[-1])
                except Exception:
                    pass

        elif cmd == "LAS:LIM:I?":
            self.sim_query_response = "150"
        elif cmd == "TEC:LIM:THI?":
            self.sim_query_response = "80"
        elif cmd == "LAS:MOD?":
            ch = self.sim_state['curr_chan']
            self.sim_query_response = str(self.sim_state['LAS_MOD'][ch - 1])
        elif cmd_root == "LAS:MOD":
            if len(parts) > 1:
                ch = self.sim_state['curr_chan']
                try:
                    self.sim_state['LAS_MOD'][ch - 1] = int(parts[-1])
                except Exception:
                    pass
        elif cmd == "MODERR?":
            self.sim_query_response = self.sim_moderr

    def process_sim_query(self):
        return self.sim_query_response

    # ----------------------------------------------------
    # SAFETY & VERIFICATION HELPERS
    # ----------------------------------------------------
    def safe_pause(self, t):
        """Sleep, then raise HALT if a stop was requested during the pause."""
        time.sleep(t)
        if self.is_stop_requested:
            raise RuntimeError("HALT")

    def verify_hw_state(self, cmd, expected_val, err_msg):
        """Query cmd and confirm it reads back expected_val; raise otherwise."""
        res = -1
        for retry in range(2):
            try:
                res_val = float(self.query_cmd(cmd))
                if not math.isnan(res_val) and int(res_val) == expected_val:
                    return
                res = res_val
            except Exception:
                pass
            self.safe_pause(0.15)
        raise RuntimeError(f"{err_msg} (Expected {expected_val}, Got {res})")

    @staticmethod
    def parse_moderr(err_str):
        """Classify a MODERR? response string into (has_error, message).

        MODERR? returns a list of numeric module/command error codes (e.g. "0",
        "504", or a comma/space separated list). We parse out the individual
        integer tokens and compare exactly, rather than doing substring matches on
        the raw string — a naive `"504" in err_str` test would false-match codes
        like "5040" or "1504" and misreport faults.
        """
        err_str = (err_str or "").strip()
        codes = re.findall(r"\d+", err_str)

        # No error: nothing returned, or every returned code is zero.
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

    def check_controller_errors(self, ch_num):
        """Select ch_num, query MODERR?, and classify the response.

        Retries a few times so a recognized fault code (rather than a transient
        empty/zero read) is what ultimately gets reported.
        """
        err_str = ""
        codes = []
        for retry in range(3):
            self.send_cmd(f"CHAN {ch_num}")
            time.sleep(0.1)

            if not self.is_simulated and self.ser:
                self.ser.reset_input_buffer()

            self.send_cmd("MODERR?")
            time.sleep(0.15)
            err_resp = self.read_cmd()
            err_str = err_resp.strip()
            codes = re.findall(r"\d+", err_str)

            # No error: nothing returned, or every returned code is zero.
            if not codes or all(int(c) == 0 for c in codes):
                return False, ""

            # Stop retrying once we see a recognized fault code.
            if any(c in codes for c in self.KNOWN_ERROR_CODES):
                break
            time.sleep(0.2)

        return self.parse_moderr(err_str)
