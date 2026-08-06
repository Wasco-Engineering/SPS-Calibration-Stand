# Stinger Bug List

Runtime screenshots and logs are saved locally under `logs/bug_reports/`.
This document tracks durable, cross-stand issues.

## BUG-001 — Derived contact can light on an empty port

- **Status:** Pinned for follow-up
- **Area:** LabJack switch presence / UI indicators
- **Severity:** Medium
- **Observed:** With a single sensed PTP throw, an unplugged sensed pin can read
  HIGH (correctly inactive), then `derive_nc_from_no` renders the complementary
  NC contact active.
- **Expected:** The UI must not represent a derived complement as proof that a
  DUT is connected.
- **Next investigation:** Decide between an idle-until-transition UI latch and
  a hardware topology that supports static detection.

## BUG-002 — Evaluate COM-HIGH topology with external pull-downs

- **Status:** Pinned hardware investigation
- **Area:** LabJack T7 switch sensing
- **Severity:** Low
- **Context:** T7 DIO has fixed pull-ups and no software pull-down. COM HIGH
  masks an open contact unless external pull-down resistors are added.
- **Next investigation:** Validate suitable pull-down value and harness changes
  on one stand before changing software configuration.

## BUG-003 — DOE2608-01 sequences 401–407 reported as mmHg

- **Status:** Open
- **Area:** PTP units / display / Alicat configuration
- **Severity:** High
- **Observed:** The PTP set was reported as mmHg.
- **Expected:** Confirm pressure reference, display unit, and Alicat DCU agree
  with the database PTP.
- **Next investigation:** Export the PTP rows; record `UnitsOfMeasure`,
  `PressureReference`, setpoints, and limits; then compare
  `ptp_service.py`, `pressure_domain.py`, and `configure_units_from_ptp`.

