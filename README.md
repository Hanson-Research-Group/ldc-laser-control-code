# Laser Controller Console

A graphical desktop **control console for laser diode controllers** — the instruments that drive and temperature-stabilize laser diodes. It gives scientists and engineers a unified interface to configure, monitor, and safely ramp multi-laser setups: laser sources, laser-amplifier pump diodes, BBO/crystal heaters, and any other load managed through a supported controller's TEC and current channels.

**Multiple controllers at once.** You can drive several controllers simultaneously — over different connections (RS-232 serial, USB) and with different channel counts — and they all appear as boxed groups of channel rows stacked in one table, so the software behaves almost controller-agnostically across them.

**Split temperature/current instruments.** Some controllers separate the temperature and current stages into two physical boxes (e.g. Wavelength Electronics: a TC temperature controller + a QCL current driver). You can pair a temperature instrument with a current instrument so the two act as **one laser line**, and configure multiple such pairs. The TEC-before-laser interlock is enforced across the pair.

> **Supported hardware:**
> - **ILX Lightwave LDC-3908** (RS-232, 8 channels) — fully supported.
> - **Thorlabs ITC** series and **Wavelength Electronics TC / QCL** LAB instruments — included as **sim-backed driver stubs** (their command strings and USB/VISA backend are stubbed and marked for on-hardware validation). The architecture and UI support them today via the built-in simulator; finish and verify the command sets on the bench before connecting real hardware.
>
> The code is organized behind a device-agnostic driver + transport, so support for other controllers can be added without touching the GUI or the safety engine — see [Adding a controller](#-adding-another-controller).

---

## 🚀 Key Features

*   **Multi-Channel Grid System**: Direct configuration and monitoring for up to **8 independent controller channels** simultaneously.
*   **Real-Time Telemetry Monitor**: A high-efficiency background timer thread continuously queries active slots every 2.0 seconds to stream real-time Temperature (°C), Current (mA), and output statuses without locking the UI.
*   **Automated Safety Ramping**: Implements precise, linear, and gradual software ramping algorithms for temperature ($\Delta T/\text{s}$) and current ($\Delta I/\text{s}$) to safeguard delicate laser diodes against thermal shock and current spikes.
*   **Automated Chassis Interrogation**: Instantly interrogates the controller slot-by-slot to discover active channels, read hardware-level limits (max current, max temperature), and match UI controls accordingly.
*   **Master Controls**: Global overrides to toggle all TEC or Laser channels ON/OFF in a single click, alongside master ramping triggers.
*   **Flexible Profile Management**: Save and load complete multi-laser experimental profiles as pretty-printed, human-editable JSON-encoded configuration files. Includes an automatic unsaved change indicator (`*`) and auto-loads the last active profile on startup.
*   **Built-in Hardware Simulator**: Includes a headless, software-defined **Demo Simulator** to mimic active cards, telemetry, and ramping responses, enabling offline dry-runs and validation without physical hardware.
*   **Double-Safeguarded Shutoffs**:
    *   *Safe Abort*: Gracefully stops active ramping sequences and stabilizes current/temperature at safe parameters.
    *   *Emergency Laser Off (EMO)*: An instant-cut mechanism to bypass ramps and drop laser output currents immediately in hazardous situations.

---

## ⚠️ Critical Safety Protocols

> [!WARNING]
> **THERMAL INTERLOCK REQUIREMENT**
> To prevent irreversible active layer damage (catastrophic optical damage, COD), the laser diode current source must **strictly** remain disabled until the Thermoelectric Cooler (TEC) thermopile circuit is enabled and the temperature has stabilized. The software will automatically reject commands that attempt to turn on a laser while its associated TEC is offline.

> [!CAUTION]
> **EMERGENCY CURRENT SHUTDOWN RISK**
> Cutting current immediately using the Emergency Laser Off button without ramping down can stress laser diode junctions due to inductive kickbacks. Use the Emergency button **only** in genuine emergency conditions (e.g., active fiber combustion, thermal runaway). For standard shutdowns, use the **CANCEL RUN** button.

---

## 📂 Repository Directory Layout

```
laser-controller-console/
├── profiles/                  # Laser configuration profiles (JSON)
│   └── template_profile.txt       # Example profile to copy and edit
├── src/                       # Source code
│   ├── main.py                    # PySide6/Qt GUI entry point (device-agnostic)
│   ├── driver.py                  # Abstract controller-driver interface + capabilities
│   ├── transport.py               # Link layer: SerialTransport, VisaTransport (USB)
│   ├── system.py                  # Multi-controller model: units, channel bindings, pairings
│   ├── ldc3908.py                 # ILX Lightwave LDC-3908 driver + Demo Simulator
│   ├── thorlabs_itc.py            # Thorlabs ITC driver STUB + simulator
│   ├── wavelength_tc.py           # Wavelength TC (temperature) driver STUB + simulator
│   ├── wavelength_qcl.py          # Wavelength QCL (current) driver STUB + simulator
│   ├── sequencer.py               # Safety state machine + ramp engine
│   ├── theme.py                   # Light/dark theme tokens
│   ├── test_ldc3908.py            # Head-less ILX driver tests
│   ├── test_drivers.py            # Head-less transport + stub-driver tests
│   ├── test_sequencer.py          # Head-less sequencer tests (incl. split-channel)
│   ├── test_gui.py                # Head-less GUI smoke test (offscreen, multi-unit)
│   ├── laser_controller_icon.ico  # Application icon
│   └── laser_controller_icon.png
├── build_script.py            # PyInstaller build script
├── .gitignore
├── LICENSE
└── README.md
```

> **Hardware manuals:** the ILX Lightwave **LDC-3908** / **LDC-3916370** user manuals are
> not distributed here — download them from the manufacturer for the full SCPI command
> reference and chassis configuration details.

---

## 🔌 Hardware Setup & Interfacing

### Controller Serial Settings
The ILX Lightwave LDC-3908 communicates over a standard RS-232 serial interface. Verify the following parameters on the controller's front panel (**Config → Comm Menu**):
*   **Baud Rate**: `9600`
*   **Data Bits**: `8`
*   **Parity**: `None`
*   **Stop Bits**: `1`
*   **Terminator**: Line Feed (`LF` or `\n`)

### Connecting to the Host
1. Connect each controller to the host PC — RS-232 (null-modem cable or USB-to-RS232 adapter) for the ILX, USB directly for the Thorlabs/Wavelength instruments.
2. Run the application by executing `python src/main.py` from the project root.
3. Click **⚙ Hardware…** to configure your controllers: add each controller (or a Wavelength temperature+current pairing) — you can add as many of any type as you like — and choose each one's connection (a serial port, a USB/VISA resource, or **Demo Simulator** to explore the app offline). The default configuration is a single ILX LDC-3908 in Demo mode.
4. Connect controllers using the **Connect** button in each controller's header, or **Connect All** in the top bar to open them all at once. (Connection is explicit — nothing talks to hardware until you press Connect.)

---

## 💻 Operating Instructions

### 1. Connecting & scanning
Connecting a controller **automatically scans it**:
- A multi-channel controller (the ILX) is interrogated slot-by-slot: it finds installed cards, detects no-laser modules / empty slots (floating/negative temperature is identified as "No Laser Attached"), and reads each channel's hardware limits. Its header has a **Scan** button to re-interrogate at any time.
- A single-channel instrument (Thorlabs ITC, Wavelength TC/QCL) is simply marked active whenever it connects — including at negative temperatures, which are treated as valid.

Each controller (or T+I pairing) appears as its own **box** of channel rows, all stacked in one table. Controls stay locked until a controller is connected and scanned.

### Managing controllers
While disconnected, you can rearrange and edit the configuration directly from the table:
- **Rename** a controller by double-clicking its header, or right-click → **Rename**. (The setup dialog pre-fills each controller's default model name, which you can change.)
- **Right-click** a controller header for **Rename**, **Edit connection / settings**, **Move up/down**, and **Delete**.
- **Drag** a controller's header to reorder the controllers; **drag** a channel row to reorder channels within a multi-channel controller. Your targets and labels are preserved across a rearrange.

### 2. Setting Up Ramping Parameters
Before initiating a run, define global ramping parameters in the **Bottom Panel**:
*   **T Ramp (°C/s)**: Controls the speed of temperature transitions (Default: `0.1 °C/s`).
*   **I Ramp (mA/s)**: Controls the speed of current sweeps (Default: `0.5 mA/s`).
*   **T OFF Target (°C)**: The safe target temperature when turning the TEC system off (Default: `22.0 °C`).

### 3. Creating & Running Sequences
For each enabled channel, you can configure target parameters:
1. Select the desired **Target TEC** status (`ON`/`OFF`) and **Target LAS** status (`ON`/`OFF`).
2. Input the **Target T (°C)** and **Target I (mA)**.
3. To run a single channel, click the **▶ Run Ch.** button next to that channel.
4. To run all enabled channels, pick a **Ramp mode** (each option shows a live time estimate) and click the green **▶ RUN ALL** button:
   * **One laser at a time** — finish each laser's temperature→current sequence before starting the next. *(Default.)*
   * **All channels, stage by stage** — move every channel through one stage before the next, rather than finishing one laser at a time. Auto-ordered (current-down → temperature → current-up) so the TEC-before-LAS interlock holds for **both** directions: temperatures come up before currents on start-up, and currents come down before temperatures on shutdown. Useful for synchronizing a multi-laser experiment.
   * **All lasers at once** — ramp every channel simultaneously (fastest). Each channel still runs the full per-channel safety sequence in its own thread. *Validate on your hardware before relying on it.*
5. If you need to stop, click the red **⏹ CANCEL RUN (Safe)** button. The software will immediately halt sweeps and hold current values stable at their last safe increments.

---

## 💾 Profile Configurations

Experimental configurations are serialized into standard JSON text files, making them easily versionable and shareable.

### JSON Profile Schema Example
A profile carries the ramp settings, the view/mode, the **hardware layout** (`hardware`: which controllers, their connections, and any T+I pairings), and the per-channel targets. Loading a profile rebuilds the hardware configuration.

```json
{
  "T_ramp": 0.1,
  "I_ramp": 0.5,
  "T_OFF_Target": 22.0,
  "Ramp_Mode": "sequential",
  "View_Mode": "table",
  "hardware": {
    "units": [
      { "id": "u1", "kind": "combined", "driver": "ldc3908",
        "title": "ILX LDC-3908", "transport": { "type": "serial", "port": "COM3" } },
      { "id": "u2", "kind": "pairing", "title": "Wavelength Laser A",
        "temp":    { "driver": "wavelength_tc",  "transport": { "type": "visa", "resource": "USB0::...::INSTR" } },
        "current": { "driver": "wavelength_qcl", "transport": { "type": "visa", "resource": "USB0::...::INSTR" } } }
    ]
  },
  "channels": [
    { "T_Target": 35.0, "I_Target": 40.0, "Label": "Pump diode 1" },
    { "T_Target": 33.0, "I_Target": 30.0, "Label": "Pump diode 2" },
    { "T_Target": 22.0, "I_Target": 0.0,  "Label": "Laser 3" }
  ]
}
```
The `channels` array is ordered by the global channel index (all of unit 1's channels, then unit 2's, …). Legacy profiles without a `hardware` block load as a single ILX LDC-3908 on their saved `COM_Port`.

*   **Saving Profiles**: Modify parameters in the GUI and click **💾 Save Profile**. Unsaved changes will prepend a warning asterisk (`*`) to the active profile name. The ramp mode, Table/Cards view, and full hardware layout are saved with the profile.
*   **Loading Profiles**: Click **📂 Load Profile** and select your configuration; the controllers are reconfigured and the targets restored.

---

## 🛠️ Software Architecture

The control logic is split into UI-agnostic, device-agnostic layers so the GUI, the safety engine, the hardware protocol, and the physical link are all independent:

*   `driver.py` — the abstract `LaserControllerDriver` interface: a small set of *semantic* operations (`select_channel`, `set_tec`, `set_temp_setpoint`, `read_errors`, …) and a `capabilities` set (`temperature` and/or `current`). The engine and GUI talk only to this interface and never see a raw device command. A `StopToken` gives one Cancel/EMO reach across every controller in a run.
*   `transport.py` — the link layer: `SerialTransport` (RS-232) and `VisaTransport` (USB / USBTMC). A driver builds commands; the transport moves them, so the same driver logic works over serial or USB.
*   `ldc3908.py`, `thorlabs_itc.py`, `wavelength_tc.py`, `wavelength_qcl.py` — concrete drivers. Each maps the semantic operations onto its device's command set and ships a built-in simulator. The ILX is a *combined* controller (temperature + current); the Wavelength TC is *temperature-only* and the QCL is *current-only*.
*   `system.py` — the multi-controller model. An `Endpoint` is a (driver, channel); a `ChannelBinding` is one logical laser line = a temperature endpoint + a current endpoint (the same device for a combined controller, two devices for a pairing); a `Unit` is one box in the table; a `ControllerSystem` holds all devices, units, and the flat channel list. Built from the profile's `hardware` config by `build_system()`.
*   `sequencer.py` — the per-channel safety state machine and the temperature/current ramps (sequential / all-temps-then-currents / parallel). Its per-channel bus routes temperature operations to the temperature endpoint and current operations to the current endpoint, so the **TEC-before-laser interlock holds even when they are two separate instruments**. It reports back through a `SequenceEvents` sink instead of touching any widgets.

This core is exercised by head-less unit tests (`test_ldc3908.py`, `test_drivers.py`, `test_sequencer.py` — including a split-channel and a multi-controller run) that need no display.

The GUI (`main.py`) is built with **PySide6 / Qt** (`pip install PySide6`): each controller/pairing is a titled box of channel rows stacked in one scroll area, with a Table/Cards view, auto-hide of unused channels, OS-following light/dark theme, a Hardware Setup dialog, and profile management. Worker-thread updates reach the GUI through Qt signals, so telemetry and ramps run on background threads without locking the UI. Smoke-tested head-less by `test_gui.py`.

### ➕ Adding another controller

Support for a different controller is a matter of writing one new driver — no changes to the engine or the GUI:

1. Subclass `LaserControllerDriver` (in `driver.py`) in a new module, declare its `capabilities` (`{"temperature"}`, `{"current"}`, or both) and `num_channels`, and implement the semantic device operations it supports plus, if you want offline testing, the `_sim_*` simulator hooks. `ldc3908.py` is a complete worked example; the Wavelength stubs show single-capability drivers.
2. Register the type key in `system.DRIVER_TYPES` / `system.driver_class()` (and, for a combined type selectable in the Hardware dialog, in `main._COMBINED_TYPES`).

The transport is chosen per device in the config (`serial` / `visa` / `sim`); a driver for a non-serial link can also override the transport wiring in `driver.py`. **Note:** the Thorlabs and Wavelength drivers are structured stubs — their command strings and the concrete VISA backend are marked `TODO: validate on hardware` and must be confirmed on the bench before real use.

---

## 📦 Standalone Executable (Windows)

A standalone Windows executable is compiled and distributed using PyInstaller for systems without Python installed, or for convenient deployment.

### Key Features:
*   **Modern Interface**: PySide6 / Qt GUI that follows the OS light/dark theme.
*   **High-DPI Scaling**: Fits and resizes dynamically across diverse monitor sizes and screen resolutions.
*   **Zero Dependencies**: Run the `.exe` directly; no runtime or compiler libraries required.

### How to Run:
1. Go to the **Releases** section of this repository.
2. Download `LaserControllerConsole.exe` from the latest release.
3. Run the executable on any Windows 10/11 PC.

---

## ⚠️ Disclaimer

This software controls real laser hardware. It is provided **"as is", without warranty of
any kind** (see [LICENSE](LICENSE)). The authors accept no liability for equipment damage or
injury. You are responsible for the safe operation of your lasers: verify limits, keep the key
interlock and protective eyewear in place, and **validate the software against your own controller**
(especially the parallel/stage ramp modes) before relying on it. Not affiliated with or endorsed
by any hardware manufacturer.

---

## 📄 License

Released under the [MIT License](LICENSE). Contributions and use by other labs are welcome.
