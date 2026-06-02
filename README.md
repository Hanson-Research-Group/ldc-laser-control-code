# Newport LDC-3908 Modular Laser Diode Controller Software

A production-grade, fully graphical desktop control suite for the **Newport LDC-3908 Modular Laser Diode Controller**. This software provides scientists and engineers with an intuitive, unified interface to configure, monitor, and safely ramp multi-laser setups.

Developed by Zev Granowitz.

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
├── docs/                      # Newport hardware manuals & references
│   ├── LDC-3908-User-Manual.pdf
│   ├── LDC-3908-User-Manual.txt
│   ├── LDC-3916370-User-Manual.pdf
│   └── LDC-3916370-User-Manual.txt
├── profiles/                  # Saved laser configuration profiles
│   ├── AirXenonIonizatioCampaign_LaserSetpoints1.txt
│   ├── AirXenonIonizatioCampaign_LaserSetpoints2.txt
│   ├── AirXenonIonizatioCampaign_LaserSetpoints3.txt
│   ├── AirXenonIonizatioCampaign_LaserSetpoints4.txt
│   └── template_profile.txt
├── src/                       # Main source code directory
│   └── LDC3908_ModularLaserDiodeControllerSoftware_v0_1_1.m  # GUI App entry point
├── .gitignore                 # MATLAB-specific git ignore file
└── README.md                  # Beautiful, highly professional user guide
```

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
2. Open MATLAB and run `LDC3908_ModularLaserDiodeControllerSoftware_v0_1_1`.
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
4. To run all enabled channels sequentially, click the green **▶ RUN ALL** button.
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

The software is available in two implementations sharing identical SCPI communication logic and safety algorithms:
*   **MATLAB Implementation** (`src/LDC3908_ModularLaserDiodeControllerSoftware_v0_1_1.m`): Utilizes MATLAB's App Designer `uifigure` and an async background `timer` loop.
*   **Python Port** (`src/main.py`): Built with `CustomTkinter` and features a fully thread-safe execution model. Telemetry queries and sequence ramps run on background worker threads, preventing UI lockups and connection timing jitters.

---

## 📦 Standalone Executable (Windows)

A standalone Windows executable is compiled and distributed for systems without MATLAB installed.

### Key Features of Python App:
*   **Modern Interface**: Premium dark-mode GUI using CustomTkinter.
*   **High-DPI Scaling**: Fits and resizes dynamically across diverse monitor sizes and screen resolutions.
*   **Zero Dependencies**: Run the `.exe` directly; no runtime or compiler libraries required.

### How to Run:
1. Go to the **Releases** section of this repository.
2. Download `LDC3908_ModularLaserDiodeControllerSoftware.exe` from the latest release.
3. Run the executable on any Windows 10/11 PC.

