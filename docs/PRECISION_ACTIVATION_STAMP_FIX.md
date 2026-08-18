# Precision activation stamp-fix (right port)

Date: 2026-08-18  
Stand: Idaho ID-SPS-01 (`configs/ID-MAN-SPS-01/`)  
Part used for evidence: SPS01496-02 / sequence 300, mmHg @ 0 C, band 395–405

## Symptom

Right-port precision activation was bimodal: most runs clustered near the true trip (~400 mmHg), but a subset stamped **406–407 mmHg** (the close-limit **approach** pressure) with a matching low deactivation (~435 vs ~440). Left port on the same morning 10× was tight (399.40 ± 0.42 mmHg, 10/10 in band). Right was 402.06 ± 3.17 mmHg, **7/10 in band**.

This is not random noise. Address B on the shared Alicat COM overshoots a ~8 Torr pad during the 5 PSI/s cycling→precision handoff. The slow sweep then treated an already-tripped switch (or a trip within 3 Torr of the start sample) as the activation edge.

## What we changed

### 1. Do not stamp the approach sample as activation

`TestExecutor._sweep_to_edge` no longer returns the initial already-matching switch state as a precision **activation** edge. Deactivation may still accept a stable level after the return gate.

If the first activation transition is within 3 Torr of the sweep start **and** there was still ≥5 Torr of room to the out-target, that edge is also rejected (leftover approach overshoot, not a real slow trip).

### 2. Re-arm once, then slow-sweep again

When those guards fire, precision re-arms from the reset/deactivation side, settles at the slow edge rate, and retries the out-sweep **once**. That turns a silent 406 mmHg false pass into a real ~398 mmHg trip instead of `EDGE_NOT_FOUND`.

### 3. Preserve the slow ramp when commanding the out-sweep

Shared-line address B ACKs `SR` before the PCD generator is live. Sending `C` (resume control) after `SR` used to wipe the new rate so the out-sweep inherited 5 PSI/s.

- `Port.set_pressure(..., resume_control=False)` skips `C` after a just-applied `SR`.
- Precision settle / out / back sweeps use `_engage_ramp_rate` then `resume_control=False`.
- Approach settle is floored at **0.45 s** (Idaho YAML `precision_approach_settle_sec: 0.5`). The old 0.18 s “already near” cap was removed.

### 4. Rate knobs left alone

A 10× right-port matrix (same DUT) showed:

| Condition | In band | 406 cluster | Act. stdev |
|-----------|---------|-------------|------------|
| Baseline (5 PSI/s, 5 Torr/s, 0.5 s) | 7/10 | 3 | 3.17 |
| Slow precision 2 Torr/s | 5/10 | 5 | 4.66 |
| Long settle 1.5 s | 5/10 | 4 | 4.62 |
| 1 PSI/s cycling + approach | 9/10 | 1 | 3.61 |
| Stamp-fix + re-arm (existing rates) | **10/10** | **0** | **0.60** |

Slowing only the precision approach to 1 PSI/s while cycling stayed at 5 **locked onto** the 406 mode (10/10). Stamp-fix without re-arm turned one of those into a missing activation edge.

**Do not** set `fast_cycle_rate_psi_per_sec` to 1.0. **Do not** raise settle further. **Do not** slow the precision sweep to 2 Torr/s.

GUI live runs after the fix (same part, both ports) stayed in band (~398 / ~420 left, ~398 / ~443 right).

## Related hardware / idle (same drop)

- LabJack solenoid vacuum/atmosphere DIO levels are configurable (`solenoid_vacuum_state` / `solenoid_atmosphere_state`); a newly opened DIO is always driven to the atmosphere route.
- Removed unused `open_fitting` from Idaho YAML and the idle-atmosphere special case. Vent idle prefers EXH and recovers a below-atmosphere reading with a barometric setpoint.
- Quality Cal finalize writes a versioned offset archive under `logs/offset_history/` (see `QUALITY_CAL_MANUAL.md`).

## Operator note

Rebuild the frozen exe after this lands if operators are not running `python run.py`. The Python GUI already includes the stamp-fix.
