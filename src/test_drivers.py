#!/usr/bin/env python3
"""
Headless tests for the transport seam and the sim-backed stub drivers
(Thorlabs ITC, Wavelength TC / QCL): capability declarations and simulator
round-trips. No hardware / VISA backend required.

Run with:  python src/test_drivers.py   (or under pytest)
"""

import math
import time

from driver import CAP_TEMPERATURE, CAP_CURRENT
from transport import VisaTransport
from thorlabs_itc import ThorlabsITCDriver
from wavelength_tc import WavelengthTCDriver
from wavelength_qcl import WavelengthQCLDriver


# ----------------------------------------------------------------------------
# Transport seam
# ----------------------------------------------------------------------------
def test_visa_transport_raises_without_backend():
    """Opening a VISA link must fail loudly (missing pyvisa/backend or bad
    resource) rather than silently — the app + sims run without VISA installed."""
    t = VisaTransport("USB0::0x1313::0x0000::NOSUCH::INSTR")
    raised = False
    try:
        t.open()
    except Exception:
        raised = True
    assert raised, "VisaTransport.open() should raise when no backend/resource"
    assert t.is_open is False


# ----------------------------------------------------------------------------
# Capability declarations
# ----------------------------------------------------------------------------
def test_capabilities():
    itc = ThorlabsITCDriver()
    assert itc.supports_temperature and itc.supports_current
    assert itc.num_channels == 1

    tc = WavelengthTCDriver()
    assert tc.supports_temperature and not tc.supports_current
    assert tc.num_channels == 1

    qcl = WavelengthQCLDriver()
    assert qcl.supports_current and not qcl.supports_temperature
    assert qcl.num_channels == 1


def test_single_channel_addressing_defaults():
    tc = WavelengthTCDriver()
    tc.open_simulator()
    tc.select_channel(1)         # no-op on single-channel
    assert tc.active_channel() == 1


# ----------------------------------------------------------------------------
# Simulator round-trips
# ----------------------------------------------------------------------------
def test_thorlabs_sim_roundtrip():
    d = ThorlabsITCDriver()
    d.open_simulator()
    assert d.tec_output() == 0 and d.laser_output() == 0
    d.set_temp_setpoint(31.5)
    d.set_tec(True)
    assert d.tec_output() == 1
    assert abs(d.temp_setpoint() - 31.5) < 0.001
    assert abs(d.temperature() - 31.5) < 0.001
    d.set_current_setpoint(42.0)
    d.set_laser(True)
    assert d.laser_output() == 1
    assert abs(d.current() - 42.0) < 0.001
    assert d.temp_limit() > 0 and d.current_limit() > 0
    assert d.read_errors() == (False, "")   # base default, no error source


def test_wavelength_tc_sim_roundtrip():
    d = WavelengthTCDriver()
    d.open_simulator()
    assert d.tec_output() == 0
    d.set_temp_setpoint(27.0)
    d.set_tec(True)
    assert d.tec_output() == 1
    assert abs(d.temp_setpoint() - 27.0) < 0.001
    assert abs(d.temperature() - 27.0) < 0.001
    assert d.temp_limit() > 0
    # modulation is a current-side concept: base default reads OFF.
    assert d.modulation() == 0


def test_wavelength_qcl_sim_roundtrip():
    d = WavelengthQCLDriver()
    d.open_simulator()
    assert d.laser_output() == 0
    d.set_current_setpoint(120.0)
    d.set_laser(True)
    assert d.laser_output() == 1
    assert abs(d.current_setpoint() - 120.0) < 0.001
    assert abs(d.current() - 120.0) < 0.001
    assert d.current_limit() > 0
    d.set_laser(False)
    assert d.laser_output() == 0


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
