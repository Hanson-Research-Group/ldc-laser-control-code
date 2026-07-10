#!/usr/bin/env python3
"""
Headless tests for the ILX Lightwave LDC-3908 driver (ldc3908.LDC3908Driver).

Exercises the device-agnostic driver API against the built-in Demo Simulator,
plus the pure MODERR? classifier and the read_errors() round-trip. No GUI /
hardware required. Run with:
    python src/test_ldc3908.py
or under pytest:
    pytest src/test_ldc3908.py
"""

import math
import time

from ldc3908 import LDC3908Driver


# ----------------------------------------------------------------------------
# MODERR? parsing (pure; previously used fragile substring matching and
# mis-handled "0,0").
# ----------------------------------------------------------------------------
def test_parse_moderr_no_fault_cases():
    for s in ["", "0", "000", "0,0", "0 0", "  0  "]:
        has_err, msg = LDC3908Driver.parse_moderr(s)
        assert has_err is False, f"{s!r} should be no-fault"
        assert msg == ""


def test_parse_moderr_recognized_codes():
    assert LDC3908Driver.parse_moderr("501")[0] is True
    assert "E501" in LDC3908Driver.parse_moderr("501")[1]
    assert "E504" in LDC3908Driver.parse_moderr("504")[1]
    assert "E504" in LDC3908Driver.parse_moderr("504,0")[1]
    assert "Temperature" in LDC3908Driver.parse_moderr("404")[1]
    assert "Temperature" in LDC3908Driver.parse_moderr("407")[1]


def test_parse_moderr_no_false_substring_match():
    # These contain the digits of 504 but are NOT code 504.
    has_err, msg = LDC3908Driver.parse_moderr("5040")
    assert has_err is True and "E504" not in msg, "5040 must not match E504"
    has_err, msg = LDC3908Driver.parse_moderr("1504")
    assert has_err is True and "E504" not in msg, "1504 must not match E504"
    # A generic unknown, non-zero code is still a fault.
    assert LDC3908Driver.parse_moderr("23")[0] is True


def test_parse_moderr_first_known_code_wins():
    # 501 is checked before 504 in the classifier.
    assert "E501" in LDC3908Driver.parse_moderr("501 504")[1]


# ----------------------------------------------------------------------------
# Semantic driver API round-trips against the simulator.
# ----------------------------------------------------------------------------
def _sim():
    c = LDC3908Driver()
    c.open_simulator()
    return c


def test_num_channels_and_baudrate():
    c = LDC3908Driver()
    assert c.num_channels == 8
    assert c.baudrate == 9600


def test_channel_select_and_query():
    c = _sim()
    c.select_channel(1)
    assert c.active_channel() == 1
    c.select_channel(5)
    assert c.active_channel() == 5


def test_empty_slot_rejects_select():
    c = _sim()
    # Channel 3 is an empty slot (is_installed == 0): current channel unchanged.
    c.select_channel(1)
    c.select_channel(3)
    assert c.active_channel() == 1


def test_no_laser_card_signatures():
    c = _sim()
    # Channel 4 is installed-but-no-laser (is_installed == 2).
    c.select_channel(4)
    assert c.temp_setpoint() < 0            # negative -> "no laser" sentinel
    assert math.isnan(c.current_setpoint())  # current reads NaN


def test_temp_setpoint_roundtrip():
    c = _sim()
    c.select_channel(1)
    c.set_temp_setpoint(25.5)
    assert c.temp_setpoint() == 25.5
    assert c.temperature() == 25.5


def test_current_setpoint_roundtrip():
    c = _sim()
    c.select_channel(1)
    c.set_current_setpoint(42.0)
    assert c.current_setpoint() == 42.0
    assert c.current() == 42.0


def test_output_toggles():
    c = _sim()
    c.select_channel(1)
    assert c.tec_output() == 0
    c.set_tec(True)
    assert c.tec_output() == 1
    c.set_laser(True)
    assert c.laser_output() == 1
    c.set_laser(False)
    assert c.laser_output() == 0
    c.set_tec(False)
    assert c.tec_output() == 0


def test_limits_and_modulation():
    c = _sim()
    c.select_channel(1)
    assert c.current_limit() == 150
    assert c.temp_limit() == 80
    c.disable_modulation()
    assert c.modulation() == 0
    c.enable_modulation()
    assert c.modulation() == 1


def test_channels_are_independent():
    c = _sim()
    c.select_channel(1)
    c.set_temp_setpoint(30.0)
    c.select_channel(5)
    c.set_temp_setpoint(40.0)
    c.select_channel(1)
    assert c.temp_setpoint() == 30.0
    c.select_channel(5)
    assert c.temp_setpoint() == 40.0


def test_clear_module_does_not_raise():
    c = _sim()
    c.select_channel(1)
    c.clear_module()  # *CLS — no observable state in the sim, just must not raise


# ----------------------------------------------------------------------------
# Connection lifecycle / cooperative flags.
# ----------------------------------------------------------------------------
def test_is_connected_lifecycle():
    c = LDC3908Driver()
    assert c.is_connected is False
    c.open_simulator()
    assert c.is_connected is True
    c.close()
    assert c.is_connected is False


def test_safe_pause_normal():
    c = _sim()
    c.is_stop_requested = False
    c.safe_pause(0.01)  # should not raise


def test_safe_pause_halts_on_stop():
    c = _sim()
    c.is_stop_requested = True
    raised = False
    try:
        c.safe_pause(0.01)
    except RuntimeError as e:
        raised = "HALT" in str(e)
    assert raised, "safe_pause must raise HALT when is_stop_requested is set"


# ----------------------------------------------------------------------------
# read_errors() end-to-end through the simulator (via sim_moderr).
# ----------------------------------------------------------------------------
def test_read_errors_clean():
    c = _sim()
    c.sim_moderr = "0"
    c.select_channel(1)
    has_err, msg = c.read_errors()
    assert has_err is False and msg == ""


def test_read_errors_fault():
    c = _sim()
    c.sim_moderr = "504"
    c.select_channel(1)
    has_err, msg = c.read_errors()
    assert has_err is True and "E504" in msg


def test_read_errors_false_substring():
    c = _sim()
    c.sim_moderr = "5040"
    c.select_channel(1)
    has_err, msg = c.read_errors()
    assert has_err is True and "E504" not in msg


# ----------------------------------------------------------------------------
# Minimal runner so the file works without pytest installed.
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    start = time.time()
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    dur = time.time() - start
    print(f"\n{passed} passed, {failed} failed in {dur:.2f}s")
    raise SystemExit(1 if failed else 0)
