#!/usr/bin/env python3
"""
Headless tests for the UI-agnostic Sequencer (safety state machine + ramps).

No GUI / Tk required. Drives the real control logic against the simulators using
a recording event sink, including a SPLIT channel (temperature and current on two
different physical devices) and a MULTI-controller run. Run with:
    python src/test_sequencer.py
or under pytest:
    pytest src/test_sequencer.py
"""

import time

from driver import StopToken
from system import ChannelBinding, Endpoint
from ldc3908 import LDC3908Driver
from wavelength_tc import WavelengthTCDriver
from wavelength_qcl import WavelengthQCLDriver
from sequencer import Sequencer, SequenceEvents, ChannelPlan, estimate_run_times


class RecordingEvents(SequenceEvents):
    def __init__(self):
        self.events = []

    def on_status(self, idx, text, color):
        self.events.append(("status", idx, text))

    def on_led(self, idx, color):
        self.events.append(("led", idx, color))

    def on_live_output(self, idx, kind, state):
        self.events.append(("out", idx, kind, state))

    def on_live_value(self, idx, kind, value):
        self.events.append(("val", idx, kind, value))

    def on_channel_halted(self, idx):
        self.events.append(("halted", idx))

    def on_channel_fault(self, idx, message):
        self.events.append(("fault", idx, message))

    # helpers
    def kinds(self, name):
        return [e for e in self.events if e[0] == name]

    def outputs(self):
        return [e for e in self.events if e[0] == "out"]


def _rig():
    """One combined LDC-3908 (simulated). The Sequencer shares the driver's stop
    token, so tests can set/read c.is_stop_requested."""
    c = LDC3908Driver()
    c.open_simulator()
    ev = RecordingEvents()
    seq = Sequencer(stop=c.stop, events=ev)
    return c, ev, seq


def _bind(driver, idx, ch_num):
    """A combined binding: temperature and current on the same device+channel."""
    return ChannelBinding(idx=idx, ch_num=ch_num, unit_id="u1",
                          temp=Endpoint(driver, ch_num), current=Endpoint(driver, ch_num))


def _plan(driver, idx, ch_num, tec, las, tt, ti, valid=True):
    return ChannelPlan(idx=idx, ch_num=ch_num, tec_cmd=tec, las_cmd=las,
                       t_target=tt, i_target=ti, targets_valid=valid,
                       binding=_bind(driver, idx, ch_num))


FAST_T = 50.0   # degC/s  — keep ramps to ~1 iteration in tests
FAST_I = 50.0   # mA/s


def test_tec_on_only_from_all_off():
    c, ev, seq = _rig()
    plan = _plan(c, 0, 1, "ON", "OFF", 23.0, 0.0)
    seq.run([plan], FAST_T, FAST_I, 22.0)
    assert c.sim_state['TEC_ON'][0] == 1
    assert c.sim_state['LAS_ON'][0] == 0
    assert abs(c.sim_state['T_actual'][0] - 23.0) < 0.1
    assert ("out", 0, "TEC", "ON") in ev.outputs()
    assert not ev.kinds("fault") and not ev.kinds("halted")
    assert not c.is_stop_requested


def test_tec_and_las_on_from_all_off():
    c, ev, seq = _rig()
    plan = _plan(c, 0, 1, "ON", "ON", 23.0, 8.0)
    seq.run([plan], FAST_T, FAST_I, 22.0)
    assert c.sim_state['TEC_ON'][0] == 1
    assert c.sim_state['LAS_ON'][0] == 1
    assert abs(c.sim_state['T_actual'][0] - 23.0) < 0.1
    assert abs(c.sim_state['I_actual'][0] - 8.0) < 0.1
    # TEC must come on before LAS in the event stream.
    outs = ev.outputs()
    assert outs.index(("out", 0, "TEC", "ON")) < outs.index(("out", 0, "LAS", "ON"))


def test_interlock_rejects_las_on_with_tec_off():
    c, ev, seq = _rig()
    plan = _plan(c, 0, 1, "OFF", "ON", 23.0, 8.0)
    seq.run([plan], FAST_T, FAST_I, 22.0)
    faults = ev.kinds("fault")
    assert faults and "TEC must be ON" in faults[0][2]
    assert c.sim_state['LAS_ON'][0] == 0   # laser never enabled
    assert c.is_stop_requested


def test_temperature_limit_rejected():
    c, ev, seq = _rig()
    plan = _plan(c, 0, 1, "ON", "OFF", 200.0, 0.0)  # sim THI=80
    seq.run([plan], FAST_T, FAST_I, 22.0)
    faults = ev.kinds("fault")
    assert faults and "exceeds limit" in faults[0][2]
    assert c.sim_state['TEC_ON'][0] == 0


def test_current_limit_rejected():
    c, ev, seq = _rig()
    plan = _plan(c, 0, 1, "ON", "ON", 23.0, 999.0)  # sim I lim=150
    seq.run([plan], FAST_T, FAST_I, 22.0)
    faults = ev.kinds("fault")
    assert faults and "exceeds limit" in faults[0][2]
    assert c.sim_state['LAS_ON'][0] == 0


def test_full_shutdown_from_tec_and_las_on():
    c, ev, seq = _rig()
    # Pre-set hardware: TEC + LAS both ON, warm and lasing.
    c.sim_state['TEC_ON'][0] = 1
    c.sim_state['LAS_ON'][0] = 1
    c.sim_state['T_actual'][0] = 30.0
    c.sim_state['I_actual'][0] = 8.0
    plan = _plan(c, 0, 1, "OFF", "OFF", 0.0, 0.0)
    seq.run([plan], FAST_T, FAST_I, 22.0)
    assert c.sim_state['LAS_ON'][0] == 0
    assert c.sim_state['TEC_ON'][0] == 0
    assert abs(c.sim_state['I_actual'][0] - 0.0) < 0.1
    assert abs(c.sim_state['T_actual'][0] - 22.0) < 0.1   # ramped to t_off_target
    # Current must reach zero before the laser output is switched off.
    outs = ev.outputs()
    assert ("out", 0, "LAS", "OFF") in outs


def test_critical_laser_on_without_tec_is_shut_down():
    c, ev, seq = _rig()
    # Dangerous pre-state: laser ON while TEC is OFF.
    c.sim_state['TEC_ON'][0] = 0
    c.sim_state['LAS_ON'][0] = 1
    c.sim_state['I_actual'][0] = 8.0
    plan = _plan(c, 0, 1, "ON", "ON", 23.0, 8.0)
    seq.run([plan], FAST_T, FAST_I, 22.0)
    faults = ev.kinds("fault")
    assert faults and "CRITICAL" in faults[0][2]
    assert c.sim_state['LAS_ON'][0] == 0            # laser was force-shut
    assert abs(c.sim_state['I_actual'][0] - 0.0) < 0.1  # ramped down first
    assert c.is_stop_requested


def test_stop_requested_before_run_processes_nothing():
    c, ev, seq = _rig()
    c.is_stop_requested = True
    plan = _plan(c, 0, 1, "ON", "OFF", 23.0, 0.0)
    seq.run([plan], FAST_T, FAST_I, 22.0)
    assert c.sim_state['TEC_ON'][0] == 0   # never touched
    assert ev.events == []


def test_invalid_targets_skip_channel_without_fault():
    c, ev, seq = _rig()
    bad = _plan(c, 0, 1, "ON", "OFF", 0.0, 0.0, valid=False)
    good = _plan(c, 4, 5, "ON", "OFF", 23.0, 0.0)
    seq.run([bad, good], FAST_T, FAST_I, 22.0)
    # Bad channel reported "Invalid targets" but did NOT abort the whole run.
    assert any(e[0] == "status" and e[1] == 0 and "Invalid targets" in e[2] for e in ev.events)
    assert not ev.kinds("fault") and not ev.kinds("halted")
    assert c.sim_state['TEC_ON'][4] == 1   # the good channel still ran
    assert not c.is_stop_requested


# ----------------------------------------------------------------------------
# Multi-channel modes: parallel + stage (the installed sim channels are 1,2,5).
# ----------------------------------------------------------------------------
_INSTALLED = [(0, 1), (1, 2), (4, 5)]


def _bring_up_plans(c):
    return [_plan(c, i, n, "ON", "ON", 23.0, 5.0) for i, n in _INSTALLED]


def test_parallel_bring_up_all_channels():
    c, ev, seq = _rig()
    seq.run(_bring_up_plans(c), FAST_T, FAST_I, 22.0, mode="parallel")
    for i, _ in _INSTALLED:
        assert c.sim_state['TEC_ON'][i] == 1 and c.sim_state['LAS_ON'][i] == 1, i
        assert abs(c.sim_state['T_actual'][i] - 23.0) < 0.1
        assert abs(c.sim_state['I_actual'][i] - 5.0) < 0.1
    assert not c.is_stop_requested


def test_stage_bring_up_all_channels():
    c, ev, seq = _rig()
    seq.run(_bring_up_plans(c), FAST_T, FAST_I, 22.0, mode="stage")
    for i, _ in _INSTALLED:
        assert c.sim_state['TEC_ON'][i] == 1 and c.sim_state['LAS_ON'][i] == 1, i
        assert abs(c.sim_state['T_actual'][i] - 23.0) < 0.1
        assert abs(c.sim_state['I_actual'][i] - 5.0) < 0.1
    assert not c.is_stop_requested


def test_stage_shutdown_all_channels():
    c, ev, seq = _rig()
    for i, _ in _INSTALLED:
        c.sim_state['TEC_ON'][i] = 1
        c.sim_state['LAS_ON'][i] = 1
        c.sim_state['T_actual'][i] = 30.0
        c.sim_state['I_actual'][i] = 8.0
    plans = [_plan(c, i, n, "OFF", "OFF", 0.0, 0.0) for i, n in _INSTALLED]
    seq.run(plans, FAST_T, FAST_I, 22.0, mode="stage")
    for i, _ in _INSTALLED:
        assert c.sim_state['LAS_ON'][i] == 0 and c.sim_state['TEC_ON'][i] == 0, i
        assert abs(c.sim_state['I_actual'][i] - 0.0) < 0.1
        assert abs(c.sim_state['T_actual'][i] - 22.0) < 0.1   # ramped to t_off


def test_parallel_fault_in_one_channel_aborts_run():
    c, ev, seq = _rig()
    plans = [
        _plan(c, 0, 1, "ON", "ON", 23.0, 5.0),
        _plan(c, 1, 2, "ON", "ON", 23.0, 999.0),  # over I limit
    ]
    seq.run(plans, FAST_T, FAST_I, 22.0, mode="parallel")
    assert c.is_stop_requested
    assert c.sim_state['LAS_ON'][1] == 0   # the over-limit channel never lased
    assert any(e[0] == "fault" for e in ev.events)


_DIFF_TARGETS = {0: (30.0, 32.5), 1: (31.5, 45.0), 4: (25.5, 12.0)}  # per-channel (T, I)


def _diff_plans(c):
    return [_plan(c, i, n, "ON", "ON", _DIFF_TARGETS[i][0], _DIFF_TARGETS[i][1])
            for i, n in _INSTALLED]


def test_parallel_reaches_distinct_setpoints():
    c, ev, seq = _rig()
    seq.run(_diff_plans(c), FAST_T, FAST_I, 22.0, mode="parallel")
    for i, _ in _INSTALLED:
        tt, ti = _DIFF_TARGETS[i]
        assert abs(c.sim_state['T_actual'][i] - tt) < 0.1, (i, c.sim_state['T_actual'][i])
        assert abs(c.sim_state['I_actual'][i] - ti) < 0.1, (i, c.sim_state['I_actual'][i])
    assert not c.is_stop_requested


def test_stage_reaches_distinct_setpoints():
    c, ev, seq = _rig()
    seq.run(_diff_plans(c), FAST_T, FAST_I, 22.0, mode="stage")
    for i, _ in _INSTALLED:
        tt, ti = _DIFF_TARGETS[i]
        assert abs(c.sim_state['T_actual'][i] - tt) < 0.1, (i, c.sim_state['T_actual'][i])
        assert abs(c.sim_state['I_actual'][i] - ti) < 0.1, (i, c.sim_state['I_actual'][i])
    assert not c.is_stop_requested


# ----------------------------------------------------------------------------
# SPLIT channel: temperature on a Wavelength TC, current on a Wavelength QCL —
# two physical devices, one logical laser line. The interlock must hold ACROSS
# the two devices.
# ----------------------------------------------------------------------------
def _split_rig():
    stop = StopToken()
    tc = WavelengthTCDriver(stop=stop)
    qcl = WavelengthQCLDriver(stop=stop)
    tc.open_simulator()
    qcl.open_simulator()
    ev = RecordingEvents()
    seq = Sequencer(stop=stop, events=ev)
    binding = ChannelBinding(idx=0, ch_num=1, unit_id="pair",
                             temp=Endpoint(tc, 1), current=Endpoint(qcl, 1))
    return tc, qcl, stop, ev, seq, binding


def test_split_channel_bring_up_tec_before_las():
    tc, qcl, stop, ev, seq, binding = _split_rig()
    plan = ChannelPlan(idx=0, ch_num=1, tec_cmd="ON", las_cmd="ON",
                       t_target=30.0, i_target=5.0, targets_valid=True, binding=binding)
    seq.run([plan], FAST_T, FAST_I, 22.0)
    assert tc.sim_state['TEC_ON'] == 1
    assert qcl.sim_state['LAS_ON'] == 1
    assert abs(tc.sim_state['T_actual'] - 30.0) < 0.1
    assert abs(qcl.sim_state['I_actual'] - 5.0) < 0.1
    # Interlock across two devices: TEC (on the TC) before LAS (on the QCL).
    outs = ev.outputs()
    assert outs.index(("out", 0, "TEC", "ON")) < outs.index(("out", 0, "LAS", "ON"))
    assert not ev.kinds("fault") and not stop.is_stop_requested


def test_split_channel_shutdown_las_before_tec():
    tc, qcl, stop, ev, seq, binding = _split_rig()
    tc.sim_state['TEC_ON'] = 1
    tc.sim_state['T_actual'] = 30.0
    qcl.sim_state['LAS_ON'] = 1
    qcl.sim_state['I_actual'] = 8.0
    plan = ChannelPlan(idx=0, ch_num=1, tec_cmd="OFF", las_cmd="OFF",
                       t_target=0.0, i_target=0.0, targets_valid=True, binding=binding)
    seq.run([plan], FAST_T, FAST_I, 22.0)
    assert qcl.sim_state['LAS_ON'] == 0 and tc.sim_state['TEC_ON'] == 0
    assert abs(qcl.sim_state['I_actual'] - 0.0) < 0.1
    assert abs(tc.sim_state['T_actual'] - 22.0) < 0.1   # ramped to t_off
    # LAS must go off before TEC in the event stream (across the two devices).
    outs = ev.outputs()
    assert outs.index(("out", 0, "LAS", "OFF")) < outs.index(("out", 0, "TEC", "OFF"))


def test_split_channel_interlock_rejects_las_without_tec():
    tc, qcl, stop, ev, seq, binding = _split_rig()
    plan = ChannelPlan(idx=0, ch_num=1, tec_cmd="OFF", las_cmd="ON",
                       t_target=30.0, i_target=5.0, targets_valid=True, binding=binding)
    seq.run([plan], FAST_T, FAST_I, 22.0)
    faults = ev.kinds("fault")
    assert faults and "TEC must be ON" in faults[0][2]
    assert qcl.sim_state['LAS_ON'] == 0   # the QCL never enabled current


# ----------------------------------------------------------------------------
# MULTI-controller: an ILX channel and a Wavelength pair run in ONE sequence,
# sharing one stop token; each reaches its own distinct setpoints.
# ----------------------------------------------------------------------------
def test_multi_controller_run_reaches_all_setpoints():
    stop = StopToken()
    ilx = LDC3908Driver(stop=stop)
    tc = WavelengthTCDriver(stop=stop)
    qcl = WavelengthQCLDriver(stop=stop)
    for d in (ilx, tc, qcl):
        d.open_simulator()
    ev = RecordingEvents()
    seq = Sequencer(stop=stop, events=ev)

    ilx_plan = ChannelPlan(idx=0, ch_num=1, tec_cmd="ON", las_cmd="ON",
                           t_target=28.0, i_target=6.0, targets_valid=True,
                           binding=_bind(ilx, 0, 1))
    pair_plan = ChannelPlan(idx=1, ch_num=1, tec_cmd="ON", las_cmd="ON",
                            t_target=35.0, i_target=12.0, targets_valid=True,
                            binding=ChannelBinding(idx=1, ch_num=1, unit_id="pair",
                                                   temp=Endpoint(tc, 1), current=Endpoint(qcl, 1)))
    seq.run([ilx_plan, pair_plan], FAST_T, FAST_I, 22.0)

    assert ilx.sim_state['TEC_ON'][0] == 1 and ilx.sim_state['LAS_ON'][0] == 1
    assert abs(ilx.sim_state['T_actual'][0] - 28.0) < 0.1
    assert abs(ilx.sim_state['I_actual'][0] - 6.0) < 0.1
    assert tc.sim_state['TEC_ON'] == 1 and qcl.sim_state['LAS_ON'] == 1
    assert abs(tc.sim_state['T_actual'] - 35.0) < 0.1
    assert abs(qcl.sim_state['I_actual'] - 12.0) < 0.1
    assert not stop.is_stop_requested and not ev.kinds("fault")


def test_estimate_parallel_is_faster_than_sequential():
    infos = [dict(curr_t=22.0, curr_i=0.0, t_target=40.0, i_target=60.0,
                  tec_cmd="ON", las_cmd="ON", live_tec="OFF", live_las="OFF")
             for _ in range(4)]
    est = estimate_run_times(infos, 0.1, 0.5, 22.0)
    assert est["sequential"] > 0
    assert est["stage"] == est["sequential"]          # same work, reordered
    assert est["parallel"] < est["sequential"]        # overlap saves time
    assert estimate_run_times([], 0.1, 0.5, 22.0)["sequential"] == 0.0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    start = time.time()
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed in {time.time() - start:.2f}s")
    raise SystemExit(1 if failed else 0)
