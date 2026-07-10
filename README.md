# Newport LDC-3908 Modular Laser Diode Controller Software

A graphical desktop control suite for the **Newport LDC-3908 Modular Laser Diode Controller**. This software provides scientists and engineers with an intuitive, unified interface to configure, monitor, and safely ramp multi-laser setups.

---

## 🚀 Key Features

*   **Multi-Channel Grid System**: Direct configuration and monitoring for up to **8 independent laser controller cards** simultaneously.
*   **Real-Time Telemetry Monitor**: A high-efficiency background timer thread continuously queries active slots every 2.0 seconds to stream real-time Temperature (°C), Current (mA), and output statuses without locking the UI.
*   **Automated Safety Ramping**: Implements precise, linear, and gradual software ramping algorithms for temperature ($\Delta T/\text{s}$) and current ($\Delta I/\text{s}$) to safeguard delicate laser diodes against thermal shock and current spikes.
*   **Automated Chassis Interrogation**: Instantly interrogates the Newport mainframe slot-by-slot to discover active laser cards, read hardware-level limits (max current, max temperature), and match UI controls accordingly.
*   **Master Controls**: Global overrides to toggle all TEC or Laser modules ON/OFF in a single click, alongside master ramping triggers.
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
ldc-laser-control-code/
├── profiles/                  # Laser configuration profiles (JSON)
│   └── template_profile.txt       # Example profile to copy and edit
├── src/                       # Source code
│   ├── main.py                    # PySide6/Qt GUI entry point
│   ├── laser_controller.py        # UI-agnostic hardware/protocol core + simulator
│   ├── sequencer.py               # Safety state machine + ramp engine
│   ├── theme.py                   # Light/dark theme tokens
│   ├── test_laser_controller.py   # Head-less core tests
│   ├── test_sequencer.py          # Head-less sequencer tests
│   ├── test_gui.py                # Head-less GUI smoke test (offscreen)
│   ├── laser_controller_icon.ico  # Application icon
│   └── laser_controller_icon.png
├── build_script.py            # PyInstaller build script
├── .gitignore
├── LICENSE
└── README.md
```

> **Hardware manuals:** the Newport **LDC-3908** and **LDC-3916370** user manuals are
> not distributed here — download them from [Newport](https://www.newport.com/) for the
> full SCPI command reference and chassis configuration details.

---

## 🔌 Hardware Setup & Interfacing

### Mainframe Serial Settings
The mainframe communicates over a standard RS-232 serial interface. Verify the following parameters on the Newport LDC-3908 physical chassis (**Config -> Comm Menu**):
*   **Baud Rate**: `9600`
*   **Data Bits**: `8`
*   **Parity**: `None`
*   **Stop Bits**: `1`
*   **Terminator**: Line Feed (`LF` or `\n`)

### Connecting to the Host
1. Connect the Newport LDC-3908 RS-232 port to the host PC using a null-modem cable or USB-to-RS232 adapter.
2. Run the application by executing `python src/main.py` from the project root.
3. In the **COM Port** dropdown, select the corresponding serial port detected on your Windows USB stack (e.g., `COM3`).
4. Click **Connect**. If the physical hardware is unavailable, select **Demo Simulator** to explore the software's capabilities safely in virtual space.

---

## 💻 Operating Instructions

### 1. Channel Scanning
Upon connecting, the UI controls remain locked to protect hardware. Click **Scan Channels**. The software will:
- Check slot allocations 1 through 8.
- Verify if a physical card is present.
- Measure thermocouple feedback to detect if a laser diode is actively attached (floating/negative voltage signals are safely identified as "No Laser Attached").
- Synchronize targets, limits, and enable status.

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
   * **All temps, then currents** — ramp every channel's temperature first, then every channel's current. Auto-ordered (current-down → temperature → current-up) so the TEC-before-LAS interlock holds for both start-up and shutdown; useful for synchronizing a multi-laser experiment.
   * **All lasers at once** — ramp every channel simultaneously (fastest). Each channel still runs the full per-channel safety sequence in its own thread. *Validate on your hardware before relying on it.*
5. If you need to stop, click the red **⏹ CANCEL RUN (Safe)** button. The software will immediately halt sweeps and hold current values stable at their last safe increments.

---

## 💾 Profile Configurations

Experimental configurations are serialized into standard JSON text files, making them easily versionable and shareable.

### JSON Profile Schema Example
```json
{
  "T_ramp": 0.1,
  "I_ramp": 0.5,
  "T_OFF_Target": 22.0,
  "channels": [
    { "T_Target": 35.0, "I_Target": 40.0, "Label": "D1-Xenon Pump" },
    { "T_Target": 33.0, "I_Target": 30.0, "Label": "D2-Xenon Probe" },
    { "T_Target": 22.0, "I_Target": 0.0,  "Label": "Laser 3" }
  ]
}
```

*   **Saving Profiles**: Modify parameters in the GUI and click **💾 Save Profile**. Unsaved changes will prepend a warning asterisk (`*`) to the active profile name.
*   **Loading Profiles**: Click **📂 Load Profile** and select your configuration. The GUI will perform integrity checks to ensure channel alignment.

---

## 🛠️ Software Architecture

The hardware/protocol and safety-critical control logic live in a UI-agnostic core, independent of any GUI toolkit:

*   `laser_controller.py` — serial I/O, the Demo Simulator, and safety helpers (read-back verification, cooperative halt, `MODERR?` parsing).
*   `sequencer.py` — the per-channel safety state machine and the temperature/current ramps. It drives the controller and reports back through a `SequenceEvents` sink instead of touching any widgets.

This core is exercised by head-less unit tests (`test_laser_controller.py`, `test_sequencer.py`) that run with no display.

The GUI (`main.py`) is built with **PySide6 / Qt** (`pip install PySide6`): a responsive Table/Cards channel view with auto-hide of unused channels, OS-following light/dark theme, and profile management. Worker-thread updates reach the GUI through Qt signals, so telemetry queries and sequence ramps run on background threads without locking the UI. It is smoke-tested head-less by `test_gui.py`.

---

## 📦 Standalone Executable (Windows)

A standalone Windows executable is compiled and distributed using PyInstaller for systems without Python installed, or for convenient deployment.

### Key Features:
*   **Modern Interface**: PySide6 / Qt GUI that follows the OS light/dark theme.
*   **High-DPI Scaling**: Fits and resizes dynamically across diverse monitor sizes and screen resolutions.
*   **Zero Dependencies**: Run the `.exe` directly; no runtime or compiler libraries required.

### How to Run:
1. Go to the **Releases** section of this repository.
2. Download `LDC3908_ModularLaserDiodeControllerSoftware.exe` from the latest release.
3. Run the executable on any Windows 10/11 PC.

---

## ⚠️ Disclaimer

This software controls real laser hardware. It is provided **"as is", without warranty of
any kind** (see [LICENSE](LICENSE)). The authors accept no liability for equipment damage or
injury. You are responsible for the safe operation of your lasers: verify limits, keep the key
interlock and protective eyewear in place, and **validate the software against your own chassis**
(especially the parallel/stage ramp modes) before relying on it. Not affiliated with or endorsed
by Newport Corporation.

---

## 📄 License

Released under the [MIT License](LICENSE). Contributions and use by other labs are welcome.

