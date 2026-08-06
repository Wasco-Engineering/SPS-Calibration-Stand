# Stinger Product Requirements Document

## Purpose

Stinger calibrates pressure and vacuum switches on shared two-port stands. It
controls pressure through Alicat controllers, measures results with calibrated
transducers, reads switch contacts through LabJack T7 DIO, and records results
against work orders and Product Test Parameters (PTP).

## Core requirements

- Use the transducer as measurement authority for recorded calibration points.
- Keep each Alicat in **TorrA** (DCU 13) for finer absolute-pressure setpoint
  and readback resolution than PSIA. Explicit gauge-mmHg PTPs may select mmHg.
- Capture an atmosphere reference once at boot (P0), then keep that session
  reference stable rather than continuously inferring weather from process
  pressure.
- Park Alicat in EXH idle with the exhaust solenoid routed to atmosphere.
- Treat the PCD-115PSIA short response frame as absolute pressure plus
  setpoint; it has no IB gauge or barometric fields.
- Identify this stand in the database as `CA-SPS-02`.
- Persist detail results with a port-qualified Alicat identity, for example
  `CA-SPS-02-L-601126` or `CA-SPS-02-R-601127`. Stinger reads the manufacturing
  serial at connect; it blocks a production result write when the serial is
  unavailable or would exceed the 20-character database `EquipmentID` limit.

## Operator bug reports

The Main-tab **Report Bug** button saves a timestamped report under
`logs/bug_reports/`. Each report contains the operator description, optional
reproduction steps, current UI screenshot, current session/rotating logs, and
available work-order/status context. Runtime reports remain local and are not
tracked in Git.

## Known limitations

Some PTPs use one sensed switch throw and derive the opposite logical contact.
An open, unplugged sensed line can therefore be displayed as an active derived
NO/NC contact. Static DIO state cannot prove DUT presence with that topology;
this is tracked in `docs/bugs/BUG_LIST.md`.

