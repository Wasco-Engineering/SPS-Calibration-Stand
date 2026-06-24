"""HTML, PDF, and CSV report generation for quality calibration results."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QMarginsF
from PyQt6.QtGui import QPdfWriter
from PyQt6.QtGui import QTextDocument

from quality_cal.core.calibration_export import point_passes_raw
from quality_cal.config import QualitySettings
from quality_cal.session import CalibrationPointResult, PortCalibrationResult, QualityCalibrationSession


def build_report_html(session: QualityCalibrationSession, settings: QualitySettings) -> str:
    left_html = _build_port_section(session.left_port, settings)
    right_html = _build_port_section(session.right_port, settings)
    leak_html = ""
    if session.include_leak_check:
        leak_html = _build_leak_section(session)

    started = session.started_at.strftime("%Y-%m-%d %H:%M:%S") if session.started_at else "N/A"
    completed = session.completed_at.strftime("%Y-%m-%d %H:%M:%S") if session.completed_at else "N/A"

    return f"""
    <html>
      <head>
        <style>
          body {{ font-family: Segoe UI, Arial, sans-serif; color: #1f2937; margin: 24px; }}
          h1, h2, h3 {{ color: #111827; }}
          .meta {{ margin-bottom: 18px; }}
          .meta td {{ padding: 4px 10px 4px 0; }}
          table {{ width: 100%; border-collapse: collapse; margin-top: 10px; margin-bottom: 24px; }}
          th, td {{ border: 1px solid #cbd5e1; padding: 6px 8px; font-size: 10pt; }}
          th {{ background: #e2e8f0; text-align: left; }}
          .pass {{ color: #166534; font-weight: bold; }}
          .fail {{ color: #b91c1c; font-weight: bold; }}
          .note {{ color: #6b7280; font-size: 9pt; }}
        </style>
      </head>
      <body>
        <h1>Quality Calibration Certificate</h1>
        <table class="meta">
          <tr><td><b>Technician</b></td><td>{_escape(session.technician_name)}</td></tr>
          <tr><td><b>Asset ID</b></td><td>{_escape(session.asset_id)}</td></tr>
          <tr><td><b>Started</b></td><td>{started}</td></tr>
          <tr><td><b>Completed</b></td><td>{completed}</td></tr>
          <tr><td><b>Overall Result</b></td><td class="{'pass' if session.overall_passed else 'fail'}">{'PASS' if session.overall_passed else 'FAIL'}</td></tr>
        </table>
        <p class="note">Template reference: {_escape(str(settings.report_template_path))}</p>
        {leak_html}
        {left_html}
        {right_html}
      </body>
    </html>
    """


def export_report_pdf(
    session: QualityCalibrationSession,
    settings: QualitySettings,
    output_path: Path | None = None,
) -> Path:
    report_path = output_path or (settings.report_output_dir / default_report_filename(session, settings))
    report_path.parent.mkdir(parents=True, exist_ok=True)

    writer = QPdfWriter(str(report_path))
    layout = writer.pageLayout()
    layout.setMargins(QMarginsF(12, 12, 12, 12))
    writer.setPageLayout(layout)

    document = QTextDocument()
    document.setHtml(build_report_html(session, settings))
    document.print(writer)
    return report_path


def build_text_document(session: QualityCalibrationSession, settings: QualitySettings) -> QTextDocument:
    document = QTextDocument()
    document.setHtml(build_report_html(session, settings))
    return document


def default_report_filename(session: QualityCalibrationSession, settings: QualitySettings) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    asset = "".join(char for char in session.asset_id if char.isalnum() or char in {"-", "_"})
    return f"{settings.report_filename_prefix}_Asset{asset}_{timestamp}.pdf"


def default_csv_filename(session: QualityCalibrationSession, settings: QualitySettings) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    asset = "".join(char for char in session.asset_id if char.isalnum() or char in {"-", "_"})
    return f"{settings.report_filename_prefix}_Asset{asset}_{timestamp}.csv"


def build_report_csv(session: QualityCalibrationSession, settings: QualitySettings) -> str:
    """Build CSV content with session metadata and calibration data for quality use."""
    out = io.StringIO()
    w = csv.writer(out)
    started = (
        session.started_at.strftime("%Y-%m-%d %H:%M:%S")
        if session.started_at
        else ""
    )
    completed = (
        session.completed_at.strftime("%Y-%m-%d %H:%M:%S")
        if session.completed_at
        else ""
    )
    w.writerow(["Technician", session.technician_name or ""])
    w.writerow(["Asset ID", session.asset_id or ""])
    w.writerow(["Started", started])
    w.writerow(["Completed", completed])
    w.writerow(["Overall Result", "PASS" if session.overall_passed else "FAIL"])
    w.writerow([])
    w.writerow([
        "Port", "Point", "Target (psia)", "Mensor (psia)", "Alicat (psia)",
        "Transducer (psia)", "Raw Dev (psia)", "Corrected Dev (psia)",
        "Raw Result", "Final Result",
    ])
    for port in (session.left_port, session.right_port):
        for point in port.points:
            w.writerow(_point_row(port.port_label, point, settings))
    return out.getvalue()


def _point_row(
    port_label: str,
    point: CalibrationPointResult,
    settings: QualitySettings,
) -> list:
    raw_pass = point_passes_raw(point, pressure_tolerance_psia=settings.pressure_tolerance_psia)
    return [
        port_label,
        f"{point.point_index}/{point.point_total}",
        f"{point.target_psia:.4f}",
        _csv_fmt(point.mensor_psia),
        _csv_fmt(point.alicat_psia),
        _csv_fmt(point.transducer_psia),
        _csv_fmt(point.deviation_psia),
        _csv_fmt(point.corrected_deviation_psia),
        "PASS" if raw_pass else "FAIL",
        "PASS" if point.passed else "FAIL",
    ]


def _csv_fmt(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.4f}"


def export_report_csv(
    session: QualityCalibrationSession,
    settings: QualitySettings,
    output_path: Path,
) -> Path:
    """Write calibration data CSV to the given path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    content = build_report_csv(session, settings)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def _build_port_section(port_result: PortCalibrationResult, settings: QualitySettings) -> str:
    rows: list[str] = []
    for point in port_result.points:
        raw_pass = point_passes_raw(
            point,
            pressure_tolerance_psia=settings.pressure_tolerance_psia,
        )
        final_status = 'PASS' if point.passed else 'FAIL'
        raw_status = 'PASS' if raw_pass else 'FAIL'
        rows.append(
            f"""
            <tr>
              <td>{point.point_index}/{point.point_total}</td>
              <td>{point.target_psia:.2f}</td>
              <td>{_fmt(point.mensor_psia)}</td>
              <td>{_fmt(point.alicat_psia)}</td>
              <td>{_fmt(point.transducer_psia)}</td>
              <td>{_fmt(point.deviation_psia)}</td>
              <td>{_fmt(point.corrected_deviation_psia)}</td>
              <td class="{'pass' if raw_pass else 'fail'}">{raw_status}</td>
              <td class="{'pass' if point.passed else 'fail'}">{final_status}</td>
              <td>{point.route}</td>
            </tr>
            """
        )

    if not rows:
        rows.append(
            '<tr><td colspan="10" class="note">No calibration points recorded.</td></tr>'
        )

    fit_note = ''
    if port_result.fit_summary is not None and port_result.fit_summary.applied_to_stinger_config:
        fit_note = (
            '<p class="note">Correlation models were fitted from the sweep and applied '
            f'before final pass/fail (Alicat 0–{settings.alicat_fit_max_psia:.0f} PSIA; '
            f'transducer 0–{settings.fit_max_psia:.0f} PSIA when installed).</p>'
        )

    return f"""
    <h2>{_escape(port_result.port_label)}</h2>
    <p><b>Result:</b> <span class="{'pass' if port_result.overall_passed else 'fail'}">{'PASS' if port_result.overall_passed else 'FAIL'}</span>
    &nbsp;&nbsp; <b>Tolerance (final):</b> +/- {settings.pressure_tolerance_psia:.4f} psia</p>
    {fit_note}
    <table>
      <tr>
        <th>Point</th>
        <th>Target (psia)</th>
        <th>Mensor</th>
        <th>Alicat</th>
        <th>Transducer</th>
        <th>Raw Dev</th>
        <th>Corrected Dev</th>
        <th>Raw</th>
        <th>Final</th>
        <th>Route</th>
      </tr>
      {''.join(rows)}
    </table>
    """


def _build_leak_section(session: QualityCalibrationSession) -> str:
    rows: list[str] = []
    for leak in (session.left_port.leak_check, session.right_port.leak_check):
        if leak is None:
            continue
        status = "N/A" if leak.passed is None else ("PASS" if leak.passed else "FAIL")
        status_class = "" if leak.passed is None else ("pass" if leak.passed else "fail")
        rows.append(
            f"""
            <tr>
              <td>{_escape(leak.port_label)}</td>
              <td>{leak.target_psia:.2f}</td>
              <td>{leak.duration_s:.0f}</td>
              <td>{leak.initial_alicat_psia:.3f}</td>
              <td>{leak.final_alicat_psia:.3f}</td>
              <td>{leak.alicat_leak_rate_psi_per_min:.4f}</td>
              <td>{_fmt(leak.transducer_leak_rate_psi_per_min)}</td>
              <td class="{status_class}">{status}</td>
            </tr>
            """
        )
    if not rows:
        rows.append('<tr><td colspan="8" class="note">Leak check was enabled but no results were recorded.</td></tr>')

    return f"""
    <h2>Port Leak Check</h2>
    <table>
      <tr>
        <th>Port</th>
        <th>Target (psia)</th>
        <th>Duration (s)</th>
        <th>Initial Alicat</th>
        <th>Final Alicat</th>
        <th>Alicat Leak Rate (psi/min)</th>
        <th>Transducer Leak Rate (psi/min)</th>
        <th>Result</th>
      </tr>
      {''.join(rows)}
    </table>
    """


def _fmt(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.4f}"


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
