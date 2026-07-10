#!/usr/bin/env python3
"""
Multi-controller system model — the seam that lets the engine and GUI treat many
controllers (and split temperature/current instruments) uniformly.

A physical controller is a `LaserControllerDriver` (driver.py). This module binds
those devices into the logical structure the app works with:

  * Endpoint       — a (driver, channel) address for one capability.
  * ChannelBinding — one logical "laser line": a temperature endpoint + a current
    endpoint. For a *combined* controller both endpoints are the same device+channel;
    for a Wavelength *pairing* the temperature endpoint is a TC10 and the current
    endpoint is a QCL1000 (two physical devices, one line).
  * Unit           — one box in the UI: a combined controller (its channels) or a
    T+I pairing (one channel). Units stack in the same table.
  * ControllerSystem — the whole hardware setup: the unique devices, the units, the
    flat ordered list of bindings (each with a global idx), and the shared StopToken.

The setup is described by a JSON-able `config` dict (stored inside a profile), so
loading a profile rebuilds the system. `build_system(config)` is the factory.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from driver import StopToken, CAP_TEMPERATURE, CAP_CURRENT


# --- unit kinds ---
KIND_COMBINED = "combined"   # one device serves both temperature and current
KIND_PAIRING = "pairing"     # a temperature device + a current device, one line


# ----------------------------------------------------------------------------
# Driver registry: config type key -> concrete driver class (imported lazily so
# this module loads even if an optional driver module is absent).
# ----------------------------------------------------------------------------
DRIVER_TYPES = [
    {"key": "ldc3908", "label": "ILX Lightwave LDC-3908 (combined, 8 ch)"},
    {"key": "thorlabs_itc", "label": "Thorlabs ITC (combined, 1 ch)"},
    {"key": "wavelength_tc", "label": "Wavelength TC (temperature only)"},
    {"key": "wavelength_qcl", "label": "Wavelength QCL (current only)"},
]


def driver_class(type_key):
    if type_key == "ldc3908":
        from ldc3908 import LDC3908Driver
        return LDC3908Driver
    if type_key == "thorlabs_itc":
        from thorlabs_itc import ThorlabsITCDriver
        return ThorlabsITCDriver
    if type_key == "wavelength_tc":
        from wavelength_tc import WavelengthTCDriver
        return WavelengthTCDriver
    if type_key == "wavelength_qcl":
        from wavelength_qcl import WavelengthQCLDriver
        return WavelengthQCLDriver
    raise ValueError(f"Unknown driver type: {type_key!r}")


# ----------------------------------------------------------------------------
# Data structures
# ----------------------------------------------------------------------------
@dataclass
class Endpoint:
    driver: object      # a LaserControllerDriver
    channel: int        # 1-based channel on that driver


@dataclass
class ChannelBinding:
    idx: int                        # global 0-based index (into system.channels / UI cards)
    ch_num: int                     # channel number within its unit (for display / messages)
    unit_id: str
    label: str = ""
    temp: Optional[Endpoint] = None
    current: Optional[Endpoint] = None

    @property
    def temp_driver(self):
        return self.temp.driver if self.temp else None

    @property
    def current_driver(self):
        return self.current.driver if self.current else None

    @property
    def is_split(self):
        """True when temperature and current live on different physical devices."""
        return (self.temp is not None and self.current is not None
                and self.temp.driver is not self.current.driver)


@dataclass
class Unit:
    id: str
    title: str
    kind: str                       # KIND_COMBINED | KIND_PAIRING
    devices: List[object] = field(default_factory=list)   # unique driver instances
    channels: List[ChannelBinding] = field(default_factory=list)


class ControllerSystem:
    """The full hardware setup. Owns the shared StopToken so one Cancel/EMO
    reaches every device."""

    def __init__(self, units, channels, devices, stop, config):
        self.units = units
        self.channels = channels          # flat, ordered by global idx
        self.devices = devices            # unique driver instances
        self.stop = stop
        self.config = config

    @property
    def num_channels(self):
        return len(self.channels)

    @property
    def is_connected(self):
        return bool(self.devices) and all(d.is_connected for d in self.devices)

    def connect_all(self):
        """Open every device per its stored transport spec. Raises on the first
        failure (after closing what was opened)."""
        opened = []
        try:
            for d in self.devices:
                _connect_driver(d, getattr(d, "transport_spec", {"type": "sim"}))
                opened.append(d)
        except Exception:
            for d in opened:
                try:
                    d.close()
                except Exception:
                    pass
            raise

    def close_all(self):
        for d in self.devices:
            try:
                d.close()
            except Exception:
                pass


# ----------------------------------------------------------------------------
# Transport wiring
# ----------------------------------------------------------------------------
def _connect_driver(driver, spec):
    t = (spec or {}).get("type", "sim")
    if t == "sim":
        driver.open_simulator()
    elif t == "serial":
        driver.open_serial(spec["port"], timeout=spec.get("timeout", 5.0))
    elif t == "visa":
        driver.open_visa(spec["resource"], timeout=spec.get("timeout", 5.0),
                         backend=spec.get("backend"))
    else:
        raise ValueError(f"Unknown transport type: {t!r}")


def _make_driver(type_key, spec, stop):
    cls = driver_class(type_key)
    d = cls(stop=stop)
    d.transport_spec = dict(spec or {"type": "sim"})
    d.type_key = type_key
    return d


# ----------------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------------
def build_system(config, stop=None):
    """Build a ControllerSystem from a config dict. Devices are created but NOT
    connected (call connect_all()). A shared StopToken is created if not given."""
    stop = stop or StopToken()
    units = []
    channels = []
    devices = []
    gidx = 0

    for u in config.get("units", []):
        kind = u.get("kind", KIND_COMBINED)
        uid = u.get("id") or f"u{len(units) + 1}"

        if kind == KIND_PAIRING:
            tcfg = u["temp"]
            ccfg = u["current"]
            td = _make_driver(tcfg["driver"], tcfg.get("transport"), stop)
            cd = _make_driver(ccfg["driver"], ccfg.get("transport"), stop)
            devices.extend([td, cd])
            title = u.get("title") or f"{td.model} + {cd.model}"
            unit = Unit(id=uid, title=title, kind=kind, devices=[td, cd])
            # A pairing is one logical channel (both sub-devices are single-channel).
            b = ChannelBinding(idx=gidx, ch_num=1, unit_id=uid,
                               label=u.get("label", "") or "Laser",
                               temp=Endpoint(td, 1), current=Endpoint(cd, 1))
            unit.channels.append(b)
            channels.append(b)
            gidx += 1
        else:  # KIND_COMBINED
            d = _make_driver(u["driver"], u.get("transport"), stop)
            devices.append(d)
            title = u.get("title") or d.model
            unit = Unit(id=uid, title=title, kind=KIND_COMBINED, devices=[d])
            labels = u.get("channel_labels", [])
            # Optional display order of hardware channels (a permutation of
            # 1..num_channels); defaults to natural order.
            order = u.get("channel_order") or list(range(1, d.num_channels + 1))
            seen = set()
            order = [ch for ch in order if 1 <= ch <= d.num_channels and ch not in seen
                     and not seen.add(ch)]
            for ch in range(1, d.num_channels + 1):
                if ch not in seen:
                    order.append(ch)
            for ch in order:
                lbl = labels[ch - 1] if ch - 1 < len(labels) else f"Laser {ch}"
                b = ChannelBinding(idx=gidx, ch_num=ch, unit_id=uid, label=lbl,
                                   temp=Endpoint(d, ch), current=Endpoint(d, ch))
                unit.channels.append(b)
                channels.append(b)
                gidx += 1

        units.append(unit)

    return ControllerSystem(units, channels, devices, stop, config)


# ----------------------------------------------------------------------------
# Convenience configs
# ----------------------------------------------------------------------------
def default_config():
    """A single ILX LDC-3908 unit — the app's default (matches legacy behavior)."""
    return {"units": [{"id": "u1", "kind": KIND_COMBINED, "driver": "ldc3908",
                       "title": "ILX Lightwave LDC-3908",
                       "transport": {"type": "sim"}}]}


def single_ldc3908_config(transport_spec, channel_labels=None):
    """Wrap one LDC-3908 on a given transport as a one-unit config (used to load
    legacy flat profiles)."""
    u = {"id": "u1", "kind": KIND_COMBINED, "driver": "ldc3908",
         "title": "ILX Lightwave LDC-3908", "transport": transport_spec}
    if channel_labels:
        u["channel_labels"] = channel_labels
    return {"units": [u]}
