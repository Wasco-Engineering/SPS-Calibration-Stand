"""Tests for QF87 certificate context and placeholder fill."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from quality_cal.config import QualitySettings
from quality_cal.core.qf87_certificate import (
    build_certificate_context,
    fill_qf87_docx,
)
from quality_cal.core.report_generator import export_report_pdf
from quality_cal.session import CalibrationPointResult, PortFitSummary, QualityCalibrationSession


def _settings(tmp_path: Path) -> QualitySettings:
    return QualitySettings(
        profile_id='high_0_115',
        profile_label='High 0–115 PSIA',
        pressure_points_psia=[0.0, 15.0],
        pressure_tolerance_psia=0.0193,
        settle_tolerance_psia=0.05,
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
        alicat_fit_max_psia=115.0,
        require_mensor=False,
        prompt_disconnect_mensor_above_psi=30.0,
        capture_raw_during_sweep=True,
        pass_threshold_torr=1.0,
        leak_check_target_psia=100.0,
        leak_check_duration_s=90.0,
        leak_check_sample_hz=4.0,
        leak_check_max_rate_psi_per_min=0.2,
        leak_check_ramp_rate_psi_per_s=8.0,
        report_output_dir=tmp_path / 'records',
        report_template_path=tmp_path / 'template.docx',
        report_filename_prefix='QualityCalibration',
        desktop_output_dir=tmp_path / 'desktop',
        also_write_records_path=False,
    )


def test_build_certificate_context_includes_technician(tmp_path: Path) -> None:
    session = QualityCalibrationSession(
        technician_name='NB',
        asset_id='222',
        started_at=datetime(2026, 6, 2, 12, 0, 0),
        completed_at=datetime(2026, 6, 2, 13, 0, 0),
    )
    session.left_port.points = [
        CalibrationPointResult(
            port_id='port_a',
            point_index=1,
            point_total=1,
            target_psia=14.0,
            route='atmosphere',
            mensor_psia=14.0,
            alicat_psia=14.1,
            transducer_psia=14.0,
            deviation_psia=0.1,
            passed=True,
            settle_duration_s=1.0,
            hold_duration_s=8.0,
            sample_count=4,
        ),
    ]
    ctx = build_certificate_context(session, _settings(tmp_path), equipment_id='CA-SPS-01')
    assert ctx['TECHNICIAN_ID'] == 'NB'
    assert ctx['OVERALL_RESULT'] == 'PASS'
    assert 'CA-SPS-01' in ctx['EQUIPMENT_ID']


def test_fill_qf87_docx_replaces_placeholders(tmp_path: Path) -> None:
    docx = pytest.importorskip('docx')
    template = tmp_path / 't.docx'
    out = tmp_path / 'out.docx'
    document = docx.Document()
    document.add_paragraph('Tech {{TECHNICIAN_ID}} result {{OVERALL_RESULT}}')
    document.save(str(template))

    fill_qf87_docx(template, {'TECHNICIAN_ID': 'NB', 'OVERALL_RESULT': 'PASS'}, out)
    saved = docx.Document(str(out))
    assert 'Tech NB result PASS' in saved.paragraphs[0].text


def test_export_report_pdf_writes_file(tmp_path: Path, qapp) -> None:
    session = QualityCalibrationSession(
        technician_name='NB',
        asset_id='222',
        started_at=datetime(2026, 6, 2, 12, 0, 0),
        completed_at=datetime(2026, 6, 2, 13, 0, 0),
    )
    session.left_port.points = [
        CalibrationPointResult(
            port_id='port_a',
            point_index=1,
            point_total=1,
            target_psia=14.0,
            route='atmosphere',
            mensor_psia=14.0,
            alicat_psia=14.1,
            transducer_psia=14.0,
            deviation_psia=0.1,
            passed=True,
            settle_duration_s=1.0,
            hold_duration_s=8.0,
            sample_count=4,
        ),
    ]
    settings = _settings(tmp_path)
    pdf_path = tmp_path / 'report.pdf'
    written = export_report_pdf(session, settings, pdf_path)
    assert written == pdf_path
    assert pdf_path.is_file()
    assert pdf_path.stat().st_size > 500
