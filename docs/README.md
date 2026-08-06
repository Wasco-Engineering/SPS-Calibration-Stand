# Stinger Documentation

## Start here

- **`SYSTEM_SPEC.md`**: what Stinger is + core constraints (authoritative)
- **`INITIAL_SETUP.md`**: new PC / new stand bring-up, hardware verification, measurement policy

## Core specs

- **`WORKFLOWS.md`**: operator workflows (QAL 15/16/17), per-port phases, button mapping
- **`UI_SPEC.md`**: UI layout + touch-first interaction rules
- **`HARDWARE_SPEC.md`**: hardware topology + control/measurement + channel assignments
- **`DATABASE_CONTRACT.md`**: DB read/write contract (PTP, work order, results, retest)
- **`STATE_MACHINE.md`**: per-port state machine definition, substates, transitions
- **`TESTING.md`**: how to run unit, coverage, and hardware integration tests
- **`COVERAGE_BASELINE.md`**: current module-level coverage baseline
- **`OPEN_QUESTIONS.md`**: remaining unknowns + how to verify them
- **`PRD.md`**: product requirements and operating decisions
- **`bugs/BUG_LIST.md`**: detailed cross-stand bug tracker

## Configuration

- **`../configs/<hostname>/stinger_config.yaml`**: hardware channels, timing, database, logging (authoritative per PC)
- **`../configs/<hostname>/quality_cal_config.yaml`**: Mensor / calibration profiles
- **`../configs/README.md`**: layout and hostname selection

## Quick reference

- **`KNOWLEDGE_CONSOLIDATION.md`**: summary of confirmed facts and decisions

## Reference (evidence / notes)

- `reference/DB_EXPLORATION.md`: exploration notes and observed patterns
- `reference/ptp_dumps/`: sample PTP JSON dumps
- `LABJACK_T7_PRO.md`: LabJack T7-Pro device notes

## Archive

- `_archive/`: older design docs and previous iterations (numbered design summaries, alternate state machine and QAL15 workflow docs; for reference only)