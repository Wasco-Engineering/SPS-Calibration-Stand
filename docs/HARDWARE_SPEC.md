# Hardware Spec (Stinger)

This document describes the physical + control-relevant hardware Stinger interacts with.

## Operating environment

- **Location**: cleanroom
- **Gas**: high purity N₂
- **Range**: parts may be calibrated/tested in vacuum units (e.g., Torr) or pressure units (e.g., PSI); system safety envelope is **0–115 PSI**

## Per-port hardware (Port A / Port B)

Each port is independent and includes:

### 1) Pressure control: Alicat (control authority)

- Alicat is used for **closed-loop control** (setpoint/ramping), but is **not accurate enough** for the most demanding low-pressure measurements.
- **Plumbing/control nuance** (important):
  - Alicat pressure port is always connected to a high-pressure source.
  - Alicat exhaust port is routed through a **solenoid** that switches the exhaust path between:
    - **Vacuum** (pull down)
    - **Atmosphere** (exhaust back up / "come up" when controlling near vacuum)
  - When controlling in vacuum and needing to rise, the system may need to switch exhaust routing to atmosphere to allow the controller to "exhaust up".

#### Control rates (confirmed)

| Rate Type | Value | Method |
|-----------|-------|--------|
| Precision sweep | 5 Torr/sec | Set Alicat ramp rate |
| Fast ramp | Maximum | Set Alicat setpoint to 0 (or max); controller slews naturally |

### 2) Pressure measurement: transducer (measurement authority)

- A dedicated analog transducer is used as the **authoritative pressure measurement** for:
  - recording activation/deactivation points
  - pass/fail evaluation
- **Transducer specification (confirmed)**:
  - 0.5–4.5V ratiometric
  - Range: 0–115 PSI
- DAQ: LabJack T7-Pro (production target)

#### Scaling formula

```
pressure_psi = (voltage - 0.5) * (115.0 / 4.0)
             = (voltage - 0.5) * 28.75
```

### 3) Switching / routing: exhaust solenoid output

- There is **one shared vacuum pump** feeding **two solenoids** (one per port).
- Each port uses its solenoid to route exhaust between vacuum and atmosphere.
- **Solenoid truth table (confirmed)**:
  - `DO = 1` → **Vacuum**
  - `DO = 0` → **Atmosphere**
  - **Default / safe state**: **Atmosphere** (`DO = 0`)
- **Pump protection policy**:
  - Do not switch a port to **Vacuum** while it is at high positive pressure.
  - The control algorithm should ensure the port is reduced to a safe pressure (near atmosphere or below a configured threshold) before routing to vacuum, to avoid "blasting the pump" with high-pressure air.
  - TODO: Confirm safe vacuum switch threshold (suggested: 5 PSI)

### 4) Digital inputs: switch state

- Switch state is read via digital inputs (DB9 provides up to 9 DI lines per port).
- **Pin mapping**: Pin 1 = `port0/line0`, Pin 2 = `port0/line1`, … Pin 9 = `port1/line0`
- **Pin assignments are loaded from PTP** (not hardcoded):
  - `CommonTerminal` — which pin is the common terminal
  - `NormallyOpenTerminal` — which pin is the NO terminal
  - `NormallyClosedTerminal` — which pin is the NC terminal
- PTP uses `0` on `NormallyOpenTerminal` or `NormallyClosedTerminal` to mean that throw
  terminal is not connected. Stinger treats that as a valid single-throw switch when the
  remaining throw or the common terminal can be observed by the stand.
- If the stand senses the PTP common terminal instead of the connected throw, Stinger drives
  the connected throw and reads common, then derives the missing logical side.
- There is **no watchdog input** on Stinger (that's a Functional Stand feature).
- **Fault rule (policy)**:
  - If the DI state is "impossible" (e.g., both NO/NC indicate active simultaneously, or both inactive when that should be impossible for the wiring) for **more than 5 consecutive checks**, treat as a wiring/harness fault and abort that port's run.

## Hardware Connection Summary (confirmed)

### DAQ Devices

| Port | DAQ Name | Purpose |
|------|----------|---------|
| Port A (Left) | `LeftDAQ` | AI, DI, DO for left port |
| Port B (Right) | `RightDAQ` | AI, DI, DO for right port |

### Alicat Controllers

| Port | COM Port | Address |
|------|----------|---------|
| Port A (Left) | COM3 | B |
| Port B (Right) | COM3 | A |

Both Alicats share the same COM port with different device addresses.

### Channel Assignments

| Signal | Port A (Left) | Port B (Right) | Notes |
|--------|---------------|----------------|-------|
| **Transducer AI** | `LeftDAQ/ai1:ai2` | `RightDAQ/ai1:ai2` | Ratiometric differential pair |
| **Solenoid DO** | `LeftDAQ/port1/line3` | `RightDAQ/port1/line3` | Confirmed |
| **Switch DI** | `LeftDAQ/port0/line*` | `RightDAQ/port0/line*` | Pin from PTP |

**Note**: NO/NC terminal pin assignments are read from PTP parameters (`CommonTerminal`, `NormallyOpenTerminal`, `NormallyClosedTerminal`) and converted to DAQ channel addresses at runtime.

See `stinger_config.yaml` for the authoritative channel configuration.

### LabJack T7 (current software config)

The current application is configured to use a single LabJack T7 (shared across both
ports) via the LabJack LJM library. This is the authoritative mapping used by the
software today.

#### Connection (from `stinger_config.yaml`)

- `device_type`: `T7`
- `connection_type`: `USB`
- `identifier`: `ANY` (LJM will connect to the first matching device)

#### Channel assignments (from `stinger_config.yaml`)

| Signal | Port A | Port B | Notes |
|--------|--------|--------|-------|
| **Transducer AI** | `AIN0/AIN1` (differential) | `AIN2/AIN3` (differential) | 0.5–4.5 V ratiometric (0–115 PSI), differential mode |
| **Switch COM** | PTP `CommonTerminal` -> DB9 pin -> DIO | PTP `CommonTerminal` -> DB9 pin -> DIO | Output; STINGER_03 drives LOW (`switch_com_state: 0`). Closed contact reads low; open floats high (clear edge). COM HIGH can mask open contacts on this bench. |
| **Switch sensed DI** | DB9 pin 3 (`DIO2`) | DB9 pin 3 (`DIO11`) | Input; PTP decides whether the sensed terminal is NO or NC for the active part/sequence |
| **Derived contact** | Complement of sensed NO/NC when only one terminal is wired | Complement of sensed NO/NC when only one terminal is wired | Used only after PTP resolution identifies which terminal is physically sensed |
| **Solenoid DO** | `DIO19` | `DIO18` | DO=1 vacuum, DO=0 atmosphere |

**Confirmed switch actuation points (2026-02-05):**
- Port A: actuates at ~22.9 PSI (rising), deactivates at ~21.4 PSI (falling)
- Port B: actuates at ~8.2 PSI (falling toward vacuum), deactivates at ~8.8 PSI (returning to atmosphere)

#### Transducer wiring (differential mode)

Each transducer has 4 wires: 5V, GND, A+ (positive), A- (negative).

**Port A (Left) Transducer:**
- **5V** → Pin 27 (VS) on DB37
- **GND** → Pin 1, 8, 10, 19, or 30 (any GND pin) on DB37
- **A+** → Pin 37 (AIN0) on DB37
- **A-** → Pin 18 (AIN1) on DB37

**Port B (Right) Transducer:**
- **5V** → Pin 27 (VS) on DB37 (shared with Port A)
- **GND** → Pin 1, 8, 10, 19, or 30 (any GND pin) on DB37 (shared with Port A)
- **A+** → Pin 36 (AIN2) on DB37
- **A-** → Pin 17 (AIN3) on DB37

**Note:** Both transducers can share the same 5V and GND connections. The differential configuration is set automatically during LabJack initialization via the `AIN#_NEGATIVE_CH` register.

#### Polling rate (software)

- `hardware_poll_interval_ms`: `10` (configured in `stinger_config.yaml`)
- Effective target: ~100 Hz for AIN + DIO reads (command-response, not stream)

#### DB9 inputs (switches)

Switch inputs are expected to land on LabJack DIO lines. The DB9 pin-to-DIO mapping is
based on the standard Stinger harness wiring below. PTP terminal assignments select
which DB9 pins represent NO/NC/COM, while this mapping defines how DB9 pins route to DIO.

LabJack T7 DIO numbering to connector labels (for wiring):

| DIO Range | Label | Connector |
|-----------|-------|-----------|
| `DIO0–DIO7` | `FIO0–FIO7` | DB37 |
| `DIO8–DIO15` | `EIO0–EIO7` | DB15 |
| `DIO16–DIO19` | `CIO0–CIO3` | DB15 |
| `DIO20–DIO22` | `MIO0–MIO2` | DB37 |

| DB9 Pin | Port A DIO | Port B DIO |
|---------|------------|------------|
| 1 | `DIO0` | `DIO9` |
| 2 | `DIO1` | `DIO10` |
| 3 | `DIO2` | `DIO11` |
| 4 | `DIO3` | `DIO12` |
| 5 | `DIO4` | `DIO13` |
| 6 | `DIO5` | `DIO14` |
| 7 | `DIO6` | `DIO15` |
| 8 | `DIO7` | `DIO16` |
| 9 | `DIO8` | `DIO17` |

DB9 cable color mapping (standard harness):

| DB9 Pin | Color | Port A LabJack Terminal | Port B LabJack Terminal |
|---------|-------|--------------------------|--------------------------|
| 1 | Black | `FIO0` (DB37/CB37) | `EIO1` (DB15/CB15) |
| 2 | Brown | `FIO1` (DB37/CB37) | `EIO2` (DB15/CB15) |
| 3 | Red | `FIO2` (DB37/CB37) | `EIO3` (DB15/CB15) |
| 4 | Orange | `FIO3` (DB37/CB37) | `EIO4` (DB15/CB15) |
| 5 | Yellow | `FIO4` (DB37/CB37) | `EIO5` (DB15/CB15) |
| 6 | Green | `FIO5` (DB37/CB37) | `EIO6` (DB15/CB15) |
| 7 | Blue | `FIO6` (DB37/CB37) | `EIO7` (DB15/CB15) |
| 8 | Violet | `FIO7` (DB37/CB37) | `CIO0` (DB15/CB15) |
| 9 | Grey | `EIO0` (DB15/CB15) | `CIO1` (DB15/CB15) |

Relays use dedicated DIO lines:
- Port A relay: `DIO18`
- Port B relay: `DIO19`

## Debounce behavior

Debounce / chatter occurs on switch edges; Stinger should:

- detect edges robustly
- capture the switching pressure at the **first transition** (while still logging/visualizing any subsequent bounce)
- require N stable samples to confirm edge (suggested: 3 samples)
- log raw edge events (timestamp + pressure at the moment)
- optionally visualize bounce/chatter during Debug (so it's measurable, not "magic")

### Suggested debounce parameters (tune on real hardware)

| Parameter | Suggested Value |
|-----------|-----------------|
| Stable sample count | 3 |
| Min edge interval | 50 ms |
| Log raw edges | Yes |

## Edge detection failure behavior

If no edge is detected within the expected range:

1. Continue ramping up to **10% past the limit**
2. If still no edge, return to atmosphere
3. Transition to ERROR state with "Edge not found" message

## Polling rates

| System | Target Rate | Notes |
|--------|-------------|-------|
| DAQ (AI + DI) | As fast as possible (~100 Hz+) | Poll continuously on hardware thread |
| UI refresh | 60 fps (~16 ms) | Smooth pressure display |
