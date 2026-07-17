from datetime import datetime
from pathlib import Path

from quality_cal.config import QualitySettings
from quality_cal.core.report_generator import build_report_html
from quality_cal.session import (
    CalibrationPointResult,
    LeakCheckResult,
    QualityCalibrationSession,
)


def _settings() -> QualitySettings:
    return QualitySettings(
        profile_id='mensor_0_30',
        profile_label='Test profile',
        pressure_points_psia=[1.0, 2.0],
        pressure_tolerance_psia=0.5,
        settle_tolerance_psia=0.4,
        settle_hold_s=5.0,
        settle_timeout_s=60.0,
        static_hold_s=8.0,
        settle_hold_at_or_below_5_psia_s=2.0,
        settle_hold_above_5_psia_s=1.5,
        settle_hold_above_30_psia_s=1.5,
        static_hold_at_or_below_5_psia_s=5.0,
        static_hold_above_5_psia_s=3.0,
        static_hold_above_30_psia_s=3.0,
        static_discard_s=1.0,
        sample_hz=4.0,
        mensor_max_psia=30.0,
        fit_max_psia=20.0,
        alicat_fit_max_psia=20.0,
        require_mensor=True,
        prompt_disconnect_mensor_above_psi=None,
        capture_raw_during_sweep=True,
        pass_threshold_torr=1.0,
        leak_check_target_psia=100.0,
        leak_check_duration_s=90.0,
        leak_check_sample_hz=4.0,
        leak_check_max_rate_psi_per_min=0.2,
        leak_check_ramp_rate_psi_per_s=8.0,
        report_output_dir=Path("reports"),
        report_template_path=Path("template.docx"),
        report_filename_prefix="QualityCalibration",
        desktop_output_dir=Path("desktop_reports"),
        also_write_records_path=False,
    )


def test_build_report_html_includes_leak_section_when_enabled():
    session = QualityCalibrationSession(
        technician_name="Quality Tech",
        asset_id="222",
        include_leak_check=True,
        started_at=datetime.now(),
        completed_at=datetime.now(),
    )
    session.left_port.points = [
        CalibrationPointResult(
            port_id="port_a",
            point_index=1,
            point_total=1,
            target_psia=10.0,
            route="pressure",
            mensor_psia=10.1,
            alicat_psia=10.0,
            transducer_psia=10.2,
            deviation_psia=0.1,
            passed=True,
            settle_duration_s=4.0,
            hold_duration_s=8.0,
            sample_count=5,
        )
    ]
    session.right_port.points = list(session.left_port.points)
    session.left_port.leak_check = LeakCheckResult(
        port_id="port_a",
        target_psia=100.0,
        duration_s=90.0,
        initial_alicat_psia=100.0,
        final_alicat_psia=99.8,
        initial_transducer_psia=100.0,
        final_transducer_psia=99.9,
        alicat_leak_rate_psi_per_min=0.1333,
        transducer_leak_rate_psi_per_min=0.0666,
        passed=True,
    )

    html = build_report_html(session, _settings())

    assert "Port Leak Check" in html
    assert "Left Port" in html
    assert "Alicat Leak Rate" in html


def test_build_report_html_omits_leak_section_when_disabled():
    session = QualityCalibrationSession(
        technician_name="Quality Tech",
        asset_id="222",
        include_leak_check=False,
        started_at=datetime.now(),
        completed_at=datetime.now(),
    )

    html = build_report_html(session, _settings())

    assert "Port Leak Check" not in html
