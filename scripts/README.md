## Scripts Layout

The scripts folder contains diagnostics and calibration helpers.

### Preferred Entry Points

**Hardware Diagnostics:**
- `python scripts/hardware.py discover` - LabJack discovery and basic read checks
- `python scripts/hardware.py switch` - Pressure switch NO/NC/COM test
- `python scripts/hardware.py switch-sweep` - Pressure switch sweep test
- `python scripts/hardware.py solenoids` - Solenoid toggle test
- `python scripts/hardware.py alicat-dual` - Dual Alicat test on shared COM

**Test Suites:**
- `python scripts/suite.py` - Multi-step pressure validation suites (static, resolution, ramps, filtering, plotting)
- `python scripts/calibrate.py` - Calibration workflow (collect correlation data and/or analyze CSVs)
- `python scripts/certify_ptp_applications.py --all-sps --output logs/application_certification_all_sps.csv` - Software-certify all SPS PTP applications
- `python scripts/verify_ptp_fixture_mapping.py --all-sps --all-top-level` - Required live pre-release check that resolves both fixture ports for all supported SPS and 17xxx final-test PTP records

**Specialized Diagnostics:**
- `python scripts/edge_replay.py <csv>` - Deterministic edge-detector replay diagnostics
- `python scripts/diagnose_ptp_switch.py --part SPS01496-02 --sequence 300 --port port_a` - Resolve PTP switch terminals and poll the configured switch state

### Active Test & Analysis Scripts

- `analyze_correlation.py` - Analyze correlation data
- `comprehensive_correlation_test.py` - Comprehensive correlation test
- `plot_test_results.py` - Plot test results
- `quick_static_test.py` - Quick static pressure test
- `ramp_test.py` - Pressure ramp test
- `torr_resolution_test.py` - Torr resolution test
- `vacuum_correlation_test.py` - Vacuum correlation test
- `pressure_alignment_scan.py` - Pressure alignment scan

### Specialized Utilities

- `dio_switch_diagnostic.py` - Specialized DIO switch diagnostic (bypasses application layer)
- `certify_ptp_applications.py` - Validate PTP completeness, switch resolution, pressure display, and cycle targets
- `verify_ptp_fixture_mapping.py` - Live PTP-to-fixture mapping gate; reports resolvable wiring but does not replace a hardware switch-transition check
- `generate_application_verification_matrix.py` - Generate the PTP application verification matrix
- `hardware_test.py` - General hardware test (LabJack, Alicat)
- `db_ptp_smoke_test.py` - Database/PTP smoke test helper
- `verify_switch_config.py` - Verify switch configuration

### Archived Scripts

Deprecated scripts and one-off utilities have been moved to `scripts/archive/`. See `scripts/archive/README.md` for details on what was archived and their replacements.
