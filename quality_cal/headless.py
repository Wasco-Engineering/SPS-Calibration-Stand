"""Headless quality calibration sweep on real hardware (no main window)."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QCoreApplication

from app.hardware.port import Port
from quality_cal.config import (
    QualitySettings,
    get_default_config_path,
    load_config,
    parse_quality_settings,
    setup_logging,
)
from quality_cal.core.calibration_runner import CalibrationRunner
from quality_cal.core.hardware_helpers import ensure_port_at_atmosphere, safe_shutdown_port
from quality_cal.core.hardware_session import connect_hardware_session
from quality_cal.core.port_calibrator import (
    PortCalibrationFitResult,
    finalize_port_calibration,
    fit_port_from_sweep_csv,
    format_fit_dialog_text,
)
from quality_cal.core.qf87_certificate import (
    export_certificate_bundle,
    load_equipment_id_from_stinger_config,
)
from quality_cal.session import (
    CalibrationPointResult,
    QualityCalibrationSession,
)

logger = logging.getLogger(__name__)

VACUUM_TAIL_POINTS_PSIA = [10.0, 5.0, 1.0, 0.5, 0.2, 0.05]


def run_headless_sweep(
    *,
    settings: QualitySettings,
    port: Port,
    mensor,
    port_id: str = 'port_a',
    auto_ack_mensor_disconnect: bool = True,
) -> tuple[list[CalibrationPointResult], Optional[str], Optional[Path]]:
    """Run a full calibration sweep on the main thread (no QThread — avoids HW deadlocks)."""
    if QCoreApplication.instance() is None:
        QCoreApplication(sys.argv)

    results: list[CalibrationPointResult] = []
    error: Optional[str] = None
    sweep_path: Optional[Path] = None

    runner = CalibrationRunner(
        port_id=port_id,
        port=port,
        mensor=mensor,
        settings=settings,
        auto_ack_mensor_disconnect=auto_ack_mensor_disconnect,
    )
    runner.progressChanged.connect(
        lambda pct, msg: logger.info('Progress (%s%%): %s', pct, msg),
    )

    def _on_finished(points: object) -> None:
        nonlocal results
        results = list(points)  # type: ignore[arg-type]

    def _on_failed(message: str) -> None:
        nonlocal error
        error = message

    def _on_sweep_csv(path: str) -> None:
        nonlocal sweep_path
        sweep_path = Path(path)

    runner.finished.connect(_on_finished)
    runner.failed.connect(_on_failed)
    runner.sweepCsvReady.connect(_on_sweep_csv)
    runner.run()

    for handler in logging.root.handlers:
        handler.flush()

    return results, error, sweep_path


def _log_fit_summary(fit: PortCalibrationFitResult, settings: QualitySettings) -> None:
    logger.info(
        '--- Correction factor fit (Alicat 0–%.0f, transducer 0–%.0f psia vs Mensor) ---',
        settings.alicat_fit_max_psia,
        settings.fit_max_psia,
    )
    for line in format_fit_dialog_text(fit, settings).splitlines():
        if line.strip():
            logger.info('%s', line)


def _log_rescore_validation(
    points: list[CalibrationPointResult],
    settings: QualitySettings,
) -> None:
    logger.info('--- Point validation (raw / corrected vs Mensor) ---')
    torr_limit = settings.pass_threshold_torr
    for point in points:
        if not point.mensor_used:
            logger.info(
                'Pt %s target=%.1f psia (no Mensor — Alicat vs target): alicat=%.3f pass=%s',
                point.point_index,
                point.target_psia,
                point.alicat_psia or float('nan'),
                point.passed,
            )
            continue
        raw_psi = point.deviation_psia
        corr_psi = point.corrected_deviation_psia
        raw_torr = abs(raw_psi * 51.7149) if raw_psi is not None else None
        corr_torr = abs(corr_psi * 51.7149) if corr_psi is not None else None
        logger.info(
            'Pt %s target=%.1f ΔAlicat raw=%+.4f psi (%s Torr) corr=%+.4f psi (%s Torr) pass=%s',
            point.point_index,
            point.target_psia,
            raw_psi or 0.0,
            f'{raw_torr:.3f}' if raw_torr is not None else '--',
            corr_psi or 0.0,
            f'{corr_torr:.3f}' if corr_torr is not None else '--',
            point.passed,
        )
        if point.transducer_deviation_psia is not None:
            corr_t = point.corrected_transducer_deviation_psia
            logger.info(
                '         ΔTransducer raw=%+.4f psi corr=%+.4f psi',
                point.transducer_deviation_psia,
                corr_t if corr_t is not None else float('nan'),
            )
    logger.info(
        'Points passing quarterly criteria: %s/%s',
        sum(1 for p in points if p.passed),
        len(points),
    )


def _build_session(
    *,
    port_id: str,
    points: list[CalibrationPointResult],
    fit_summary,
    technician: str,
    asset_id: str,
    profile_id: str,
    profile_label: str,
) -> QualityCalibrationSession:
    session = QualityCalibrationSession(
        technician_name=technician,
        asset_id=asset_id,
        profile_id=profile_id,
        profile_label=profile_label,
    )
    session.begin()
    port_result = session.port_result(port_id)
    port_result.points = list(points)
    port_result.fit_summary = fit_summary
    session.complete()
    return session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Run quality calibration sweep on real hardware without UI.',
    )
    parser.add_argument(
        '--port',
        default='port_a',
        choices=('port_a', 'port_b'),
        help='Which test port to calibrate (default: port_a / left).',
    )
    parser.add_argument(
        '--profile',
        default='high_0_115',
        help='Calibration profile id (default: high_0_115).',
    )
    parser.add_argument(
        '--technician',
        default='Headless',
        help='Technician name for QF87 certificate.',
    )
    parser.add_argument(
        '--asset-id',
        default='222',
        help='Asset id for QF87 certificate.',
    )
    parser.add_argument(
        '--vacuum-tail',
        action='store_true',
        help='Only run low-pressure points: 10, 5, 1, 0.5, 0.2, 0.05 psia.',
    )
    parser.add_argument(
        '--points',
        nargs='+',
        type=float,
        metavar='PSIA',
        help='Override pressure points (e.g. --points 5 1 0.5).',
    )
    parser.add_argument(
        '--hardware-check-only',
        action='store_true',
        help='Connect and verify hardware, then exit without starting a sweep.',
    )
    parser.add_argument(
        '--wait-mensor-disconnect',
        action='store_true',
        help='Block at high-pressure points until Mensor disconnect is acknowledged.',
    )
    parser.add_argument(
        '--no-apply-models',
        action='store_true',
        help='Fit correction models but do not write them to stinger_config.yaml.',
    )
    parser.add_argument(
        '--no-export-qf87',
        action='store_true',
        help='Skip QF87 certificate export after sweep.',
    )
    args = parser.parse_args(argv)

    config_path = get_default_config_path()
    print(f'Config: {config_path}', flush=True)
    config = load_config()
    setup_logging(config)
    profile_id = args.profile or None
    settings = parse_quality_settings(config, profile_id=profile_id)

    if args.profile and profile_id != settings.profile_id:
        logger.warning(
            'Profile %r not in config; using %r',
            args.profile,
            settings.profile_id,
        )
    if args.vacuum_tail:
        settings = replace(settings, pressure_points_psia=list(VACUUM_TAIL_POINTS_PSIA))
    if args.points:
        settings = replace(settings, pressure_points_psia=list(args.points))

    session_hw = None
    try:
        logger.info('Connecting hardware (discovery + port/Mensor init)...')
        session_hw = connect_hardware_session(config)
        if not session_hw.overall_ok:
            logger.error('Hardware check failed — fix devices and retry.')
            return 1
        if args.hardware_check_only:
            logger.info('Hardware check passed.')
            return 0

        port = session_hw.get_port(args.port)
        if port is None:
            logger.error('Port %s is not configured.', args.port)
            return 1

        logger.info('Venting %s to atmosphere before sweep...', args.port)
        if not ensure_port_at_atmosphere(port):
            logger.error('Could not vent %s to atmosphere — aborting.', args.port)
            return 1
        safe_shutdown_port(port)

        logger.info(
            'Starting headless sweep on %s: %s points (%s)',
            args.port,
            len(settings.pressure_points_psia),
            settings.profile_label,
        )
        max_sweep_attempts = 3
        fit: Optional[PortCalibrationFitResult] = None
        sweep_path: Optional[Path] = None
        results: list[CalibrationPointResult] = []

        for attempt in range(1, max_sweep_attempts + 1):
            logger.info('=== Sweep attempt %s/%s ===', attempt, max_sweep_attempts)
            results, error, sweep_path = run_headless_sweep(
                settings=settings,
                port=port,
                mensor=session_hw.mensor_reader,
                port_id=args.port,
                auto_ack_mensor_disconnect=not args.wait_mensor_disconnect,
            )
            if error:
                logger.error('Sweep failed: %s', error)
                if attempt >= max_sweep_attempts:
                    return 1
                continue

            if sweep_path is None:
                logger.error('Sweep finished but no CSV path was recorded.')
                if attempt >= max_sweep_attempts:
                    return 1
                continue

            logger.info('Sweep CSV: %s', sweep_path)
            logger.info('Sweep completed: %s points', len(results))
            passed = sum(1 for p in results if p.passed)
            logger.info('Overall pass (sweep): %s/%s points', passed, len(results))

            fit = fit_port_from_sweep_csv(sweep_path, args.port, settings)
            _log_fit_summary(fit, settings)
            if fit.overall_passed:
                logger.info('Correction fit passed on attempt %s', attempt)
                break
            def _fit_status(sensor_fit) -> str:
                if sensor_fit is None:
                    return 'n/a'
                return 'PASS' if sensor_fit.passed else 'FAIL'

            logger.warning(
                'Correction fit did not pass on attempt %s (transducer=%s alicat=%s)%s',
                attempt,
                _fit_status(fit.transducer),
                _fit_status(fit.alicat),
                f' error={fit.error_message}' if fit.error_message else '',
            )

        if fit is None or sweep_path is None:
            return 1

        apply_models = not args.no_apply_models
        finalized = finalize_port_calibration(
            sweep_path,
            args.port,
            results,
            settings,
            port=port,
            apply_to_stinger=apply_models,
        )
        results = finalized.points
        fit_summary = finalized.fit_summary
        fit = finalized.fit
        logger.info(finalized.message)
        _log_rescore_validation(results, settings)

        from quality_cal.core.calibration_export import port_calibration_passed

        quarterly_pass = port_calibration_passed(results, fit_summary)
        logger.info(
            'Quarterly calibration result: %s (%s/%s points pass, fit applied=%s)',
            'PASS' if quarterly_pass else 'FAIL',
            sum(1 for p in results if p.passed),
            len(results),
            fit_summary.applied_to_stinger_config if fit_summary else False,
        )
        if not quarterly_pass:
            logger.error(
                'Calibration did not meet quarterly pass criteria '
                '(fit band p99 or severe point failure).',
            )
            return 1

        for point in results:
            logger.info(
                'Pt %s/%s target=%.3f route=%s mensor=%s alicat=%s xducer=%s pass=%s',
                point.point_index,
                point.point_total,
                point.target_psia,
                point.route,
                f'{point.mensor_psia:.3f}' if point.mensor_psia is not None else '--',
                f'{point.alicat_psia:.3f}' if point.alicat_psia is not None else '--',
                f'{point.transducer_psia:.3f}' if point.transducer_psia is not None else '--',
                point.passed,
            )

        if len(results) != len(settings.pressure_points_psia):
            logger.error(
                'Point count mismatch: expected %s, got %s',
                len(settings.pressure_points_psia),
                len(results),
            )
            return 1

        if not args.no_export_qf87:
            cal_session = _build_session(
                port_id=args.port,
                points=results,
                fit_summary=fit_summary,
                technician=args.technician,
                asset_id=args.asset_id,
                profile_id=settings.profile_id,
                profile_label=settings.profile_label,
            )
            equipment_id = load_equipment_id_from_stinger_config()
            try:
                paths = export_certificate_bundle(
                    cal_session,
                    settings,
                    equipment_id=equipment_id,
                )
                logger.info('QF87 PDF: %s', paths.get('pdf'))
                if paths.get('records_pdf'):
                    logger.info('QF87 records PDF: %s', paths['records_pdf'])
            except Exception as exc:
                logger.exception('QF87 export failed (sweep CSV and models are still saved): %s', exc)

        logger.info(
            'Headless pipeline finished at %s',
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )
        return 0
    finally:
        if session_hw is not None:
            logger.info('Venting all ports to atmosphere before disconnect...')
            for port_id in ('port_a', 'port_b'):
                other = session_hw.get_port(port_id)
                if other is not None:
                    safe_shutdown_port(other)
            logger.info('Disconnecting hardware...')
            session_hw.cleanup()


if __name__ == '__main__':
    raise SystemExit(main())
