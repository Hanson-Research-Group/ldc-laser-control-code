#!/usr/bin/env python3
"""
Device-agnostic laser ramp / sequence engine.

The safety-critical control logic — the per-channel safety state machine and the
temperature/current ramps. It is independent of any specific controller: all
hardware access goes through a LaserControllerDriver (driver.py), and all UI
feedback goes through a SequenceEvents sink. Swapping in a different controller
means writing a new driver; this engine does not change.

Three execution modes (all share the SAME per-channel safety sequence and ramp
primitives, so interlocks are identical in every mode):

  * "sequential" (default) — one channel completes its full temp->current
    sequence before the next starts.
  * "stage"                — all channels move through the temperature stage,
    then all through the current stage, auto-ordered
    (current-down -> temperature -> current-up) so the TEC-before-LAS interlock
    holds for both start-up and shut-down.
  * "parallel"             — every channel ramps at once, one worker thread each.

A logical channel is a `system.ChannelBinding`: a *temperature* endpoint and a
*current* endpoint, each addressing a (driver, channel). For a combined
controller both endpoints are the same device; for a Wavelength pairing the
temperature and current endpoints are two different physical devices. The
per-channel `_Bus` routes each operation to the correct endpoint. Every
operation is one transaction: it takes that endpoint's device lock, re-selects
its channel only if the device isn't already on it, then runs — so the
TEC-before-LAS interlock holds even when temperature and current live on
separate instruments, and parallel channels (or the two devices of one split
channel) proceed independently.

Cooperative abort is a shared StopToken polled by the engine and the bus, so one
Cancel/EMO reaches every controller in the run.

No GUI imports. Exercised head-less in test_sequencer.py against the simulator.
"""

import math
import threading
import time
from dataclasses import dataclass
from typing import List

from driver import StopToken


# --- Semantic status / LED kinds ----------------------------------------------
C_INIT = "init"
C_OK = "ok"
C_FAULT = "fault"
C_RAMP_T = "ramp_t"
C_RAMP_I = "ramp_i"
LED_RAMP = "ramp"
LED_OK = "ok"
LED_FAULT = "fault"

# Fixed per-transition overheads (seconds) used by the run-time estimator.
_TEC_ON_TIME = 1.0
_LAS_ON_TIME = 4.0
_LAS_OFF_TIME = 1.5
_TEC_OFF_TIME = 1.0
_PARALLEL_SERIAL_FRAC = 0.4


class SequenceEvents:
    """Sink for everything the sequencer wants the UI to reflect. Default no-ops;
    the GUI subclasses this (marshalling each call onto the GUI thread), and tests
    subclass it to record calls."""

    def on_status(self, idx, text, kind):
        """Channel idx's status line should read `text`, styled by semantic
        `kind` ("init"/"ok"/"warn"/"fault"/"ramp_t"/"ramp_i")."""

    def on_led(self, idx, kind):
        """Channel idx's LED should show semantic `kind` ("ramp"/"ok"/"fault")."""

    def on_live_output(self, idx, kind, state):
        """kind is 'TEC' or 'LAS'; state is 'ON' or 'OFF'."""

    def on_live_value(self, idx, kind, value):
        """kind is 'T' (degC) or 'I' (mA); value is a float live reading."""

    def on_tick(self):
        """Periodic hook during a ramp so the UI can refresh its ETA display."""

    def on_channel_halted(self, idx):
        """Channel idx aborted via a cooperative STOP/EMO (a 'HALT')."""

    def on_channel_fault(self, idx, message):
        """Channel idx aborted on a hardware/validation fault with `message`."""


@dataclass
class ChannelPlan:
    """A snapshot of one channel's requested action, taken from the UI before the
    run starts (the UI is locked during execution, so the snapshot stays valid)."""
    idx: int          # 0-based global index into the UI channel list
    ch_num: int       # channel number within its unit (for display / messages)
    tec_cmd: str      # "ON" / "OFF"
    las_cmd: str      # "ON" / "OFF"
    t_target: float   # target temperature (degC); meaningful only if targets_valid
    i_target: float   # target current (mA); meaningful only if targets_valid
    targets_valid: bool
    binding: object = None   # system.ChannelBinding — the hardware this plan drives


# ----------------------------------------------------------------------------
# Run-time estimate (used by the GUI to label the mode selector).
# ----------------------------------------------------------------------------
def _channel_cost(info, t_ramp, i_ramp, t_off):
    """Return (movement_seconds, overhead_seconds) for one channel. `info` is a
    dict with keys curr_t, curr_i, t_target, i_target, tec_cmd, las_cmd,
    live_tec, live_las."""
    ct, ci = info["curr_t"], info["curr_i"]
    tt, ti = info["t_target"], info["i_target"]
    tc, lc = info["tec_cmd"], info["las_cmd"]
    lt, ll = info["live_tec"], info["live_las"]
    move = 0.0
    ovh = 0.0
    if lt == "OFF" and ll == "OFF":
        if tc == "ON" and lc == "OFF":
            ovh += _TEC_ON_TIME; move += abs(tt - ct) / t_ramp
        elif tc == "ON" and lc == "ON":
            ovh += _TEC_ON_TIME; move += abs(tt - ct) / t_ramp
            ovh += _LAS_ON_TIME; move += abs(ti) / i_ramp
    elif lt == "ON" and ll == "OFF":
        if tc == "ON" and lc == "ON":
            move += abs(tt - ct) / t_ramp
            ovh += _LAS_ON_TIME; move += abs(ti) / i_ramp
        elif tc == "OFF" and lc == "OFF":
            move += abs(t_off - ct) / t_ramp; ovh += _TEC_OFF_TIME
        elif tc == "ON" and lc == "OFF":
            move += abs(tt - ct) / t_ramp
    elif lt == "ON" and ll == "ON":
        if tc == "ON" and lc == "OFF":
            move += abs(ci) / i_ramp; ovh += _LAS_OFF_TIME; move += abs(tt - ct) / t_ramp
        elif tc == "OFF" and lc == "OFF":
            move += abs(ci) / i_ramp; ovh += _LAS_OFF_TIME
            move += abs(t_off - ct) / t_ramp; ovh += _TEC_OFF_TIME
        elif tc == "ON" and lc == "ON":
            move += abs(tt - ct) / t_ramp; move += abs(ti - ci) / i_ramp
    return move, ovh


def estimate_run_times(infos, t_ramp, i_ramp, t_off):
    """Approximate wall-clock seconds for each mode. Sequential and stage do the
    same total work; parallel overlaps the settle pauses but is floored by the
    shared serial traffic and by the single longest channel. Best-effort."""
    if t_ramp <= 0 or i_ramp <= 0 or not infos:
        return {"sequential": 0.0, "stage": 0.0, "parallel": 0.0}
    costs = [_channel_cost(i, t_ramp, i_ramp, t_off) for i in infos]
    moves = [m for m, _ in costs]
    ovhs = [o for _, o in costs]
    total = sum(moves) + sum(ovhs)
    par_move = max(max(moves), _PARALLEL_SERIAL_FRAC * sum(moves)) if moves else 0.0
    par = min(par_move + (max(ovhs) if ovhs else 0.0), total)
    return {"sequential": total, "stage": total, "parallel": par}


# ----------------------------------------------------------------------------
# Per-channel bus. Routes each operation to the right endpoint (temperature ops
# to the temperature device, current ops to the current device — which may be
# the SAME device or two different ones). Each operation is a self-contained
# transaction: take the endpoint device's lock, re-select its channel only if it
# isn't already selected, then run.
# ----------------------------------------------------------------------------
class _Bus:
    SETTLE = 0.15  # channel-switch settle after an actual channel change

    def __init__(self, binding, stop):
        self.binding = binding
        self.stop = stop
        self.temp_ep = binding.temp          # Endpoint | None
        self.cur_ep = binding.current    # Endpoint | None
        self.idx = binding.idx
        self.ch_num = binding.ch_num

    def _txn(self, ep, fn):
        """One transaction against endpoint `ep`: hold its device lock, ensure its
        channel is selected (settle only when the selection actually changes),
        then run `fn`."""
        drv = ep.driver
        with drv.serial_lock:
            if drv._selected_channel != ep.channel:
                drv.select_channel(ep.channel)
                drv._selected_channel = ep.channel
                time.sleep(self.SETTLE)
            return fn()

    # cooperative pause (outside any lock so other channels/devices proceed)
    def pause(self, t):
        time.sleep(t)
        if self.stop.is_stop_requested:
            raise RuntimeError("HALT")

    # --- temperature reads (safe defaults if this line has no temp endpoint) ---
    def tec_output(self):
        return self._txn(self.temp_ep, self.temp_ep.driver.tec_output) if self.temp_ep else 0

    def temperature(self):
        return self._txn(self.temp_ep, self.temp_ep.driver.temperature) if self.temp_ep else math.nan

    def temp_setpoint(self):
        return self._txn(self.temp_ep, self.temp_ep.driver.temp_setpoint) if self.temp_ep else math.nan

    def temp_limit(self):
        return self._txn(self.temp_ep, self.temp_ep.driver.temp_limit) if self.temp_ep else math.nan

    # --- temperature writes ---
    def set_tec(self, on):
        if self.temp_ep:
            self._txn(self.temp_ep, lambda: self.temp_ep.driver.set_tec(on))

    def set_temp_setpoint(self, x):
        if self.temp_ep:
            self._txn(self.temp_ep, lambda: self.temp_ep.driver.set_temp_setpoint(x))

    # --- current reads (safe defaults if this line has no current endpoint) ---
    def laser_output(self):
        return self._txn(self.cur_ep, self.cur_ep.driver.laser_output) if self.cur_ep else 0

    def current(self):
        return self._txn(self.cur_ep, self.cur_ep.driver.current) if self.cur_ep else math.nan

    def current_limit(self):
        return self._txn(self.cur_ep, self.cur_ep.driver.current_limit) if self.cur_ep else math.nan

    def modulation(self):
        return self._txn(self.cur_ep, self.cur_ep.driver.modulation) if self.cur_ep else 0

    # --- current writes ---
    def set_laser(self, on):
        if self.cur_ep:
            self._txn(self.cur_ep, lambda: self.cur_ep.driver.set_laser(on))

    def set_current_setpoint(self, x):
        if self.cur_ep:
            self._txn(self.cur_ep, lambda: self.cur_ep.driver.set_current_setpoint(x))

    def disable_modulation(self):
        if self.cur_ep:
            self._txn(self.cur_ep, self.cur_ep.driver.disable_modulation)

    def enable_modulation(self):
        if self.cur_ep:
            self._txn(self.cur_ep, self.cur_ep.driver.enable_modulation)

    # --- errors: poll each unique device once, aggregate ---
    def check_errors(self):
        seen = set()
        for ep in (self.temp_ep, self.cur_ep):
            if ep is None or id(ep.driver) in seen:
                continue
            seen.add(id(ep.driver))
            has, msg = self._txn(ep, ep.driver.read_errors)
            if has:
                return True, msg
        return False, ""

    # --- alignment: ensure each unique device sits on its endpoint channel ---
    def align(self):
        seen = set()
        for ep in (self.temp_ep, self.cur_ep):
            if ep is None or id(ep.driver) in seen:
                continue
            seen.add(id(ep.driver))
            self._align_endpoint(ep)

    def _align_endpoint(self, ep):
        chan = -1
        for _ in range(3):
            try:
                chan = self._txn(ep, ep.driver.active_channel)
                if chan == ep.channel:
                    return
            except Exception:
                pass
            # selection didn't take — force a fresh select on the next transaction
            try:
                ep.driver._selected_channel = None
            except Exception:
                pass
            self.pause(0.3)
        raise RuntimeError(f"Ch. switch to {ep.channel} on {ep.driver.model} timed out or failed.")

    # --- read-back verification (retries, then raises) ---
    def _verify(self, ep, reader, expected, msg):
        res = -1
        for _ in range(2):
            try:
                val = self._txn(ep, reader)
                if not (isinstance(val, float) and math.isnan(val)) and int(val) == expected:
                    return
                res = val
            except Exception:
                pass
            self.pause(0.15)
        raise RuntimeError(f"{msg} (Expected {expected}, Got {res})")

    def verify_tec(self, expected, msg):
        if self.temp_ep:
            self._verify(self.temp_ep, self.temp_ep.driver.tec_output, expected, msg)

    def verify_laser(self, expected, msg):
        if self.cur_ep:
            self._verify(self.cur_ep, self.cur_ep.driver.laser_output, expected, msg)

    def verify_modulation_off(self, msg):
        if self.cur_ep:
            self._verify(self.cur_ep, self.cur_ep.driver.modulation, 0, msg)


class Sequencer:
    def __init__(self, stop=None, events=None):
        self.stop = stop or StopToken()
        self.events = events or SequenceEvents()

    # ----------------------------------------------------
    # TOP-LEVEL DISPATCH
    # ----------------------------------------------------
    def run(self, plans: List[ChannelPlan], t_ramp, i_ramp, t_off_target, mode="sequential"):
        if mode == "parallel":
            self._run_parallel(plans, t_ramp, i_ramp, t_off_target)
        elif mode == "stage":
            self._run_stage(plans, t_ramp, i_ramp, t_off_target)
        else:
            self._run_sequential(plans, t_ramp, i_ramp, t_off_target)

    def _handle_exception(self, plan, e):
        if "HALT" in str(e):
            self.events.on_channel_halted(plan.idx)
        else:
            self.events.on_channel_fault(plan.idx, str(e))

    def _report_invalid(self, plans):
        valid = []
        for p in plans:
            if p.targets_valid:
                valid.append(p)
            else:
                self.events.on_status(p.idx, "Invalid targets", C_FAULT)
        return valid

    # ----------------------------------------------------
    # MODE: SEQUENTIAL (per channel)
    # ----------------------------------------------------
    def _run_sequential(self, plans, t_ramp, i_ramp, t_off):
        for plan in plans:
            if self.stop.is_stop_requested:
                break
            if not plan.targets_valid:
                self.events.on_status(plan.idx, "Invalid targets", C_FAULT)
                continue
            try:
                bus = _Bus(plan.binding, self.stop)
                self._run_one(bus, plan, t_ramp, i_ramp, t_off)
            except Exception as e:
                self._handle_exception(plan, e)
                self.stop.is_stop_requested = True
                break

    # ----------------------------------------------------
    # MODE: PARALLEL (one worker thread per channel)
    # ----------------------------------------------------
    def _run_parallel(self, plans, t_ramp, i_ramp, t_off):
        valid = self._report_invalid(plans)

        def worker(plan):
            if self.stop.is_stop_requested:
                return
            bus = _Bus(plan.binding, self.stop)
            try:
                self._run_one(bus, plan, t_ramp, i_ramp, t_off)
            except Exception as e:
                self._handle_exception(plan, e)
                self.stop.is_stop_requested = True  # abort the other channels

        threads = [threading.Thread(target=worker, args=(p,), daemon=True) for p in valid]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # ----------------------------------------------------
    # MODE: STAGE (all channels per stage; auto-ordered for interlock safety)
    # ----------------------------------------------------
    def _run_stage(self, plans, t_ramp, i_ramp, t_off):
        valid = self._report_invalid(plans)
        for stage_fn in (self._stage_init, self._stage_current_down,
                         self._stage_temperature, self._stage_current_up,
                         self._stage_finalize):
            for plan in valid:
                if self.stop.is_stop_requested:
                    return
                try:
                    bus = _Bus(plan.binding, self.stop)
                    stage_fn(bus, plan, t_ramp, i_ramp, t_off)
                except Exception as e:
                    self._handle_exception(plan, e)
                    self.stop.is_stop_requested = True
                    return

    def _stage_init(self, bus, plan, t_ramp, i_ramp, t_off):
        self._validate_limits(bus, plan)
        self.events.on_status(bus.idx, "Initializing...", C_INIT)
        self.events.on_led(bus.idx, LED_RAMP)
        self._align_and_disable_mod(bus)
        tec, las = self._read_states(bus)
        if tec == 0 and las == 1:
            self._critical_laser_without_tec(bus, i_ramp)

    def _stage_current_down(self, bus, plan, t_ramp, i_ramp, t_off):
        if plan.las_cmd != "OFF":
            return
        try:
            las = bus.laser_output()
        except Exception:
            las = -1
        if las == 1:
            self.ramp_current(bus, 0.0, i_ramp)
            bus.pause(0.15)
            bus.set_laser(False)
            bus.pause(0.2)
            bus.verify_laser(0, "LAS OFF acknowledge failed.")
            self.events.on_live_output(bus.idx, "LAS", "OFF")
            bus.pause(0.5)

    def _stage_temperature(self, bus, plan, t_ramp, i_ramp, t_off):
        try:
            tec = bus.tec_output()
        except Exception:
            tec = -1
        if plan.tec_cmd == "ON":
            if tec == 0:
                self.tec_temp_tset_tcurr(bus)
                bus.pause(0.15)
                bus.set_tec(True)
                bus.pause(0.2)
                bus.verify_tec(1, "TEC ON acknowledge failed.")
                self.events.on_live_output(bus.idx, "TEC", "ON")
                bus.pause(0.15)
            self.ramp_temp(bus, plan.t_target, t_ramp)
        else:  # target TEC OFF (laser already off from the current-down stage)
            if tec == 1:
                self.ramp_temp(bus, t_off, t_ramp)
                bus.pause(0.15)
                bus.set_tec(False)
                bus.pause(0.2)
                bus.verify_tec(0, "TEC OFF acknowledge failed.")
                self.events.on_live_output(bus.idx, "TEC", "OFF")
                bus.pause(0.5)

    def _stage_current_up(self, bus, plan, t_ramp, i_ramp, t_off):
        if plan.las_cmd != "ON":
            return
        try:
            las = bus.laser_output()
        except Exception:
            las = -1
        if las == 0:
            bus.set_current_setpoint(0.0)
            bus.pause(0.15)
            bus.set_laser(True)
            bus.pause(0.2)
            bus.verify_laser(1, "LAS ON acknowledge failed.")
            self.events.on_live_output(bus.idx, "LAS", "ON")
            bus.pause(2.5)  # mandatory safety lock delay
            has_err, err = bus.check_errors()
            if has_err:
                raise RuntimeError(err)
        self.ramp_current(bus, plan.i_target, i_ramp)

    def _stage_finalize(self, bus, plan, t_ramp, i_ramp, t_off):
        if not self.stop.is_stop_requested:
            self.final_check(bus)

    # ----------------------------------------------------
    # ONE CHANNEL (sequential / parallel) — full state machine
    # ----------------------------------------------------
    def _run_one(self, bus, plan, t_ramp, i_ramp, t_off):
        self._validate_limits(bus, plan)
        self.run_control_core(bus, plan.tec_cmd, plan.t_target, t_off,
                              plan.las_cmd, plan.i_target, t_ramp, i_ramp)
        has_err, err_str = bus.check_errors()
        if has_err:
            raise RuntimeError(err_str)

    def _validate_limits(self, bus, plan):
        try:
            h_i_lim = bus.current_limit()
        except Exception:
            h_i_lim = 500.0
        try:
            h_t_lim = bus.temp_limit()
        except Exception:
            h_t_lim = 80.0
        if plan.tec_cmd == "ON" and not math.isnan(h_t_lim) and plan.t_target > h_t_lim:
            raise ValueError(f"Target T ({plan.t_target:.1f}°C) exceeds limit ({h_t_lim:.1f}°C)")
        if plan.las_cmd == "ON" and not math.isnan(h_i_lim) and plan.i_target > h_i_lim:
            raise ValueError(f"Target I ({plan.i_target:.1f}mA) exceeds limit ({h_i_lim:.1f}mA)")
        if plan.tec_cmd == "OFF" and plan.las_cmd == "ON":
            raise ValueError("TEC must be ON for LAS to be ON.")

    def _align_and_disable_mod(self, bus):
        bus.align()
        bus.disable_modulation()
        bus.pause(0.25)
        bus.verify_modulation_off("Hardware failed to disable external modulation.")

    def _read_states(self, bus):
        try:
            tec = bus.tec_output()
        except Exception:
            tec = -1
        try:
            las = bus.laser_output()
        except Exception:
            las = -1
        return tec, las

    def _critical_laser_without_tec(self, bus, i_ramp):
        self.events.on_status(bus.idx, "CRITICAL: Laser ON without TEC. Ramping down safely.", C_FAULT)
        self.ramp_current(bus, 0.0, i_ramp)
        bus.pause(0.15)
        bus.set_laser(False)
        bus.pause(0.2)
        bus.verify_laser(0, "LAS OFF acknowledge failed.")
        self.events.on_live_output(bus.idx, "LAS", "OFF")
        raise RuntimeError(f"CRITICAL FAULT CH {bus.ch_num}: Laser ON while TEC OFF. Ramped down laser safely.")

    def run_control_core(self, bus, tec_on_off, t_on_target, t_off_target,
                         las_on_off, i_on_target, t_ramp, i_ramp):
        idx = bus.idx
        ch_num = bus.ch_num

        self.events.on_status(idx, "Initializing...", C_INIT)
        self.events.on_led(idx, LED_RAMP)

        self._align_and_disable_mod(bus)
        tec_curr_status, las_curr_status = self._read_states(bus)

        if tec_curr_status == 0 and las_curr_status == 0:
            if tec_on_off == "ON" and las_on_off == "OFF":
                self.tec_temp_tset_tcurr(bus)
                bus.pause(0.15)
                bus.set_tec(True)
                bus.pause(0.2)
                bus.verify_tec(1, "TEC ON acknowledge failed.")
                self.events.on_live_output(idx, "TEC", "ON")
                bus.pause(0.15)
                self.ramp_temp(bus, t_on_target, t_ramp)

            elif tec_on_off == "ON" and las_on_off == "ON":
                self.tec_temp_tset_tcurr(bus)
                bus.pause(0.15)
                bus.set_tec(True)
                bus.pause(0.2)
                bus.verify_tec(1, "TEC ON acknowledge failed.")
                self.events.on_live_output(idx, "TEC", "ON")
                bus.pause(0.15)
                self.ramp_temp(bus, t_on_target, t_ramp)

                bus.pause(0.15)
                bus.set_current_setpoint(0.0)
                bus.pause(0.15)
                bus.set_laser(True)
                bus.pause(0.2)
                bus.verify_laser(1, "LAS ON acknowledge failed.")
                self.events.on_live_output(idx, "LAS", "ON")
                bus.pause(2.5)  # Mandatory safety lock delay

                has_hw_err, hw_err_str = bus.check_errors()
                if has_hw_err:
                    raise RuntimeError(hw_err_str)

                self.ramp_current(bus, i_on_target, i_ramp)

        elif tec_curr_status == 1 and las_curr_status == 0:
            if tec_on_off == "ON" and las_on_off == "ON":
                self.ramp_temp(bus, t_on_target, t_ramp)
                bus.pause(0.15)

                bus.set_current_setpoint(0.0)
                bus.pause(0.15)
                bus.set_laser(True)
                bus.pause(0.2)
                bus.verify_laser(1, "LAS ON acknowledge failed.")
                self.events.on_live_output(idx, "LAS", "ON")
                bus.pause(0.5)

                has_hw_err, hw_err_str = bus.check_errors()
                if has_hw_err:
                    raise RuntimeError(hw_err_str)

                self.ramp_current(bus, i_on_target, i_ramp)

            elif tec_on_off == "OFF" and las_on_off == "OFF":
                self.ramp_temp(bus, t_off_target, t_ramp)
                bus.pause(0.15)
                bus.set_tec(False)
                bus.pause(0.2)
                bus.verify_tec(0, "TEC OFF acknowledge failed.")
                self.events.on_live_output(idx, "TEC", "OFF")
                bus.pause(0.5)

            elif tec_on_off == "ON" and las_on_off == "OFF":
                self.ramp_temp(bus, t_on_target, t_ramp)

        elif tec_curr_status == 1 and las_curr_status == 1:
            if tec_on_off == "ON" and las_on_off == "OFF":
                self.ramp_current(bus, 0.0, i_ramp)
                bus.pause(0.15)
                bus.set_laser(False)
                bus.pause(0.2)
                bus.verify_laser(0, "LAS OFF acknowledge failed.")
                self.events.on_live_output(idx, "LAS", "OFF")
                bus.pause(0.5)
                self.ramp_temp(bus, t_on_target, t_ramp)

            elif tec_on_off == "OFF" and las_on_off == "OFF":
                self.ramp_current(bus, 0.0, i_ramp)
                bus.pause(0.15)
                bus.set_laser(False)
                bus.pause(0.2)
                bus.verify_laser(0, "LAS OFF acknowledge failed.")
                self.events.on_live_output(idx, "LAS", "OFF")
                bus.pause(1.0)
                self.ramp_temp(bus, t_off_target, t_ramp)
                bus.pause(0.15)
                bus.set_tec(False)
                bus.pause(0.2)
                bus.verify_tec(0, "TEC OFF acknowledge failed.")
                self.events.on_live_output(idx, "TEC", "OFF")
                bus.pause(0.5)

            elif tec_on_off == "ON" and las_on_off == "ON":
                self.ramp_temp(bus, t_on_target, t_ramp)
                bus.pause(0.15)
                self.ramp_current(bus, i_on_target, i_ramp)
        else:
            if tec_curr_status == 0 and las_curr_status == 1:
                self._critical_laser_without_tec(bus, i_ramp)
            else:
                # TEC/LAS output state could not be determined; fail loud rather
                # than proceeding with an unknown output state.
                raise RuntimeError(
                    f"Could not read TEC/LAS output state for Ch {ch_num} "
                    f"(TEC={tec_curr_status}, LAS={las_curr_status}). Aborting for safety.")

        if not self.stop.is_stop_requested:
            self.final_check(bus)

    def tec_temp_tset_tcurr(self, bus):
        try:
            t_curr = bus.temp_setpoint()
            if not math.isnan(t_curr):
                bus.set_temp_setpoint(t_curr)
                bus.pause(0.15)
        except Exception:
            pass

    # ----------------------------------------------------
    # RAMPS
    # ----------------------------------------------------
    def ramp_temp(self, bus, t_target, t_ramp):
        idx = bus.idx
        t_curr = None
        for _ in range(5):
            try:
                t_curr = bus.temperature()
                break
            except Exception:
                bus.pause(0.15)

        if t_curr is None or math.isnan(t_curr):
            raise RuntimeError("Telemetry lost during initial Thermal readout.")

        if abs(t_curr - t_target) < 0.05:
            self.events.on_status(idx, f"T at Target ({t_curr:.1f} °C)", C_OK)
            return

        t_set = t_curr
        t_start = t_curr
        # Time-based stepping: advance the setpoint by (rate * actual elapsed time)
        # each iteration so the physical ramp rate matches the requested °C/s.
        direction = 1 if t_target > t_curr else -1
        NOMINAL_PERIOD = 0.5
        last_tick = time.time() - NOMINAL_PERIOD

        t_fail_count = 0
        while abs(t_set - t_target) > 0.01:
            if self.stop.is_stop_requested:
                raise RuntimeError("HALT")

            now = time.time()
            dt = now - last_tick
            last_tick = now
            dt = max(0.0, min(dt, 1.0))  # clamp so a stalled read can't leap the setpoint

            t_set += direction * abs(t_ramp) * dt
            if (direction > 0 and t_set > t_target) or (direction < 0 and t_set < t_target):
                t_set = t_target

            bus.set_temp_setpoint(t_set)

            p_start = time.time()
            while time.time() - p_start < 0.5:
                self.events.on_tick()
                bus.pause(0.1)

            try:
                t_curr = bus.temperature()
                if math.isnan(t_curr):
                    raise ValueError("NaN")
                t_fail_count = 0
            except Exception:
                t_fail_count += 1
                if t_fail_count > 3:
                    raise RuntimeError("Hardware communication lost during ramp. Stopping execution.")
                t_curr = t_set

            self.events.on_live_value(idx, "T", t_curr)

            pct = min(1.0, max(0.0, abs(t_curr - t_start) / max(0.01, abs(t_target - t_start))))
            num_blocks = round(pct * 10)
            prog_bar = '█' * num_blocks + '░' * (10 - num_blocks)
            self.events.on_status(idx, f"[{prog_bar}] ({t_curr:.1f} °C)", C_RAMP_T)

    def ramp_current(self, bus, i_target, i_ramp):
        idx = bus.idx
        i_curr = None
        for _ in range(5):
            try:
                i_curr = bus.current()
                break
            except Exception:
                bus.pause(0.15)

        if i_curr is None or math.isnan(i_curr):
            raise RuntimeError("Telemetry lost during initial Laser readout.")

        if abs(i_curr - i_target) < 0.05:
            self.events.on_status(idx, f"I at Target ({i_curr:.1f} mA)", C_OK)
            return

        i_set = i_curr
        i_start = i_curr
        direction = 1 if i_target > i_curr else -1
        NOMINAL_PERIOD = 0.5
        last_tick = time.time() - NOMINAL_PERIOD

        i_fail_count = 0
        while abs(i_set - i_target) > 0.01:
            if self.stop.is_stop_requested:
                raise RuntimeError("HALT")

            now = time.time()
            dt = now - last_tick
            last_tick = now
            dt = max(0.0, min(dt, 1.0))

            i_set += direction * abs(i_ramp) * dt
            if (direction > 0 and i_set > i_target) or (direction < 0 and i_set < i_target):
                i_set = i_target

            bus.set_current_setpoint(i_set)

            p_start = time.time()
            while time.time() - p_start < 0.5:
                self.events.on_tick()
                bus.pause(0.1)

            try:
                i_curr = bus.current()
                if math.isnan(i_curr):
                    raise ValueError("NaN")
                i_fail_count = 0
            except Exception:
                i_fail_count += 1
                if i_fail_count > 3:
                    raise RuntimeError("Hardware communication lost during ramp. Stopping execution.")
                i_curr = i_set

            self.events.on_live_value(idx, "I", i_curr)

            pct = min(1.0, max(0.0, abs(i_curr - i_start) / max(0.01, abs(i_target - i_start))))
            num_blocks = round(pct * 10)
            prog_bar = '█' * num_blocks + '░' * (10 - num_blocks)
            self.events.on_status(idx, f"[{prog_bar}] ({i_curr:.1f} mA)", C_RAMP_I)

    def final_check(self, bus):
        idx = bus.idx
        try:
            tec_stat = bus.tec_output()
        except Exception:
            tec_stat = -1
        try:
            las_stat = bus.laser_output()
        except Exception:
            las_stat = -1

        status_str = "Final Set: "
        if tec_stat == 1:
            status_str += "TEC ON, "
            self.events.on_live_output(idx, "TEC", "ON")
        else:
            status_str += "TEC OFF, "
            self.events.on_live_output(idx, "TEC", "OFF")

        if las_stat == 1:
            status_str += "LAS ON"
            self.events.on_live_output(idx, "LAS", "ON")
        else:
            status_str += "LAS OFF"
            self.events.on_live_output(idx, "LAS", "OFF")

        self.events.on_status(idx, status_str, C_OK)
        self.events.on_led(idx, LED_OK)

        bus.enable_modulation()
        bus.pause(0.15)
