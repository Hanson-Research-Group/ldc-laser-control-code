#!/usr/bin/env python3
"""
Headless tests for the UI-agnostic LaserController core.

No GUI / Tk required. Run with:
    python src/test_laser_controller.py
or under pytest:
    pytest src/test_laser_controller.py
"""

import math
import time

from laser_controller import LaserController


# ----------------------------------------------------------------------------
# MODERR? parsing (pure, the code path that previously used fragile substring
# matching and mis-handled "0,0").
# ----------------------------------------------------------------------------
def test_parse_moderr_no_fault_cases():
    for s in ["", "0", "000", "0,0", "0 0", "  0  "]:
        has_err, msg = LaserController.parse_moderr(s)
        assert has_err is False, f"{s!r} should be no-fault"
        assert msg == ""


def test_parse_moderr_recognized_codes():
    assert LaserController.parse_moderr("501")[0] is True
    assert "E501" in LaserController.parse_moderr("501")[1]
    assert "E504" in LaserController.parse_moderr("504")[1]
    assert "E504" in LaserController.parse_moderr("504,0")[1]
    assert "Temperature" in LaserController.parse_moderr("404")[1]
    assert "Temperature" in LaserController.parse_moderr("407")[1]


def test_parse_moderr_no_false_substring_match():
    # These contain the digits of 504 but are NOT code 504.
    has_err, msg = LaserController.parse_moderr("5040")
    assert has_err is True and "E504" not in msg, "5040 must not match E504"
    has_err, msg = LaserController.parse_moderr("1504")
    assert has_err is True and "E504" not in msg, "1504 must not match E504"
    # A generic unknown, non-zero code is still a fault.
    assert LaserController.parse_moderr("23")[0] is True


def test_parse_moderr_first_known_code_wins():
    # 501 is checked before 504 in the classifier.
    assert "E501" in LaserController.parse_moderr("501 504")[1]


# ----------------------------------------------------------------------------
# Simulator command round-trips.
# ----------------------------------------------------------------------------
def _sim():
    c = LaserController()
    c.open_simulator()
    return c


def test_sim_channel_select_and_query():
    c = _sim()
    c.send_cmd("CHAN 1")
    assert c.query_cmd("CHAN?") == "1"
    c.send_cmd("CHAN 5")
    assert c.query_cmd("CHAN?") == "5"


def test_sim_empty_slot_rejects_select():
    c = _sim()
    # Channel 3 is an empty slot (is_installed == 0): current channel unchanged.
    c.send_cmd("CHAN 1")
    c.send_cmd("CHAN 3")
    assert c.query_cmd("CHAN?") == "1"


def test_sim_no_laser_card_signatures():
    c = _sim()
    # Channel 4 is installed-but-no-laser (is_installed == 2).
    c.send_cmd("CHAN 4")
    assert c.query_cmd("TEC:T?") == "-10.5"          # negative -> "no laser" sentinel
    assert float(c.query_cmd("TEC:T?")) < 0
    assert c.query_cmd("LAS:LDI?") == "NaN"           # current reads NaN
    assert math.isnan(float(c.query_cmd("LAS:LDI?")))


def test_sim_temp_setpoint_roundtrip():
    c = _sim()
    c.send_cmd("CHAN 1")
    c.send_cmd("TEC:T 25.5")
    assert float(c.query_cmd("TEC:T?")) == 25.5
    assert float(c.query_cmd("TEC:SYNCT?")) == 25.5


def test_sim_current_setpoint_roundtrip():
    c = _sim()
    c.send_cmd("CHAN 1")
    c.send_cmd("LAS:LDI 42.0")
    assert float(c.query_cmd("LAS:LDI?")) == 42.0
    assert float(c.query_cmd("LAS:SYNCLDI?")) == 42.0


def test_sim_output_toggles():
    c = _sim()
    c.send_cmd("CHAN 1")
    assert c.query_cmd("TEC:OUT?") == "0"
    c.send_cmd("TEC:OUTPUT 1")
    assert c.query_cmd("TEC:OUT?") == "1"
    c.send_cmd("LAS:OUTPUT 1")
    assert c.query_cmd("LAS:OUT?") == "1"
    c.send_cmd("LAS:OUTPUT 0")
    assert c.query_cmd("LAS:OUT?") == "0"


def test_sim_limits_and_modulation():
    c = _sim()
    c.send_cmd("CHAN 1")
    assert c.query_cmd("LAS:LIM:I?") == "150"
    assert c.query_cmd("TEC:LIM:THI?") == "80"
    c.send_cmd("LAS:MOD 0")
    assert c.query_cmd("LAS:MOD?") == "0"
    c.send_cmd("LAS:MOD 1")
    assert c.query_cmd("LAS:MOD?") == "1"


def test_sim_channels_are_independent():
    c = _sim()
    c.send_cmd("CHAN 1")
    c.send_cmd("TEC:T 30.0")
    c.send_cmd("CHAN 5")
    c.send_cmd("TEC:T 40.0")
    c.send_cmd("CHAN 1")
    assert float(c.query_cmd("TEC:T?")) == 30.0
    c.send_cmd("CHAN 5")
    assert float(c.query_cmd("TEC:T?")) == 40.0


# ----------------------------------------------------------------------------
# Safety helpers.
# ----------------------------------------------------------------------------
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


def test_verify_hw_state_pass():
    c = _sim()
    c.send_cmd("CHAN 1")
    c.send_cmd("TEC:OUTPUT 1")
    c.verify_hw_state("TEC:OUT?", 1, "TEC ON acknowledge failed.")  # should not raise


def test_verify_hw_state_mismatch_raises():
    c = _sim()
    c.send_cmd("CHAN 1")
    c.send_cmd("TEC:OUTPUT 0")
    raised = False
    try:
        c.verify_hw_state("TEC:OUT?", 1, "TEC ON acknowledge failed.")
    except RuntimeError as e:
        raised = "acknowledge failed" in str(e)
    assert raised


# ----------------------------------------------------------------------------
# check_controller_errors end-to-end through the simulator (via sim_moderr).
# ----------------------------------------------------------------------------
def test_check_controller_errors_clean():
    c = _sim()
    c.sim_moderr = "0"
    has_err, msg = c.check_controller_errors(1)
    assert has_err is False and msg == ""


def test_check_controller_errors_fault():
    c = _sim()
    c.sim_moderr = "504"
    has_err, msg = c.check_controller_errors(1)
    assert has_err is True and "E504" in msg


def test_check_controller_errors_false_substring():
    c = _sim()
    c.sim_moderr = "5040"
    has_err, msg = c.check_controller_errors(1)
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
