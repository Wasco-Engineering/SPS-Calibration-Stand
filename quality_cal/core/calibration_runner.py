"""Calibration runner for the standalone quality calibration app."""

from __future__ import annotations

import csv
import logging
import statistics
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from app.hardware.port import Port
from quality_cal.config import (
    QualitySettings,
    point_timing_for_target,
)
from quality_cal.core.hardware_helpers import (
    alicat_abs_psia,
    command_target_pressure,
    infer_barometric_psia,
    prepare_port_for_target,
    safe_shutdown_port,
    settle_timeout_for_target,
    settle_tolerance_for_target,
    transducer_abs_psia,
    wait_until_near_target,
)
from quality_cal.core.mensor_reader import MensorReader
from quality_cal.core.raw_reference_store import (
    get_quality_cal_logs_dir,
    publish_latest_raw_reference,
)
from quality_cal.session import CalibrationPointResult

logger = logging.getLogger(__name__)

_OUTLIER_TOLERANCE_PSI = 2.0

SWEEP_CSV_COLUMNS = [
    'timestamp',
    'port_id',
    'phase',
    'target_abs_psi',
    'transducer_abs_psi',
    'transducer_raw_abs_psi',
    'alicat_abs_psi',
    'mensor_abs_psia',
]


class _SweepCsvWriter:
    def __init__(self, path: Path, port_id: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._port_id = port_id
        self._handle = path.open('w', newline='', encoding='utf-8')
        self._writer = csv.DictWriter(self._handle, fieldnames=SWEEP_CSV_COLUMNS)
        self._writer.writeheader()
        self._handle.flush()

    @property
    def path(self) -> Path:
        return self._path

    def write_row(
        self,
        *,
        phase: str,
        target_abs_psi: float,
        transducer_abs_psi: Optional[float],
        transducer_raw_abs_psi: Optional[float] = None,
        alicat_abs_psi: Optional[float],
        mensor_abs_psia: Optional[float],
    ) -> None:
        raw = (
            transducer_raw_abs_psi
            if transducer_raw_abs_psi is not None
            else transducer_abs_psi
        )
        self._writer.writerow(
            {
                'timestamp': f'{time.time():.3f}',
                'port_id': self._port_id,
                'phase': phase,
                'target_abs_psi': f'{target_abs_psi:.4f}',
                'transducer_abs_psi': '' if transducer_abs_psi is None else f'{transducer_abs_psi:.4f}',
                'transducer_raw_abs_psi': '' if raw is None else f'{raw:.4f}',
                'alicat_abs_psi': '' if alicat_abs_psi is None else f'{alicat_abs_psi:.4f}',
                'mensor_abs_psia': '' if mensor_abs_psia is None else f'{mensor_abs_psia:.4f}',
            }
        )
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def _average(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return statistics.fmean(values)


def _robust_average(values: list[float], expected_near: Optional[float] = None) -> Optional[float]:
    if not values:
        return None
    if len(values) <= 2:
        return statistics.fmean(values)
    if expected_near is not None:
        inlier = [v for v in values if abs(v - expected_near) <= _OUTLIER_TOLERANCE_PSI]
        if inlier:
            return statistics.fmean(inlier)
    median = statistics.median(values)
    inlier = [v for v in values if abs(v - median) <= _OUTLIER_TOLERANCE_PSI]
    if not inlier:
        return statistics.fmean(values)
    return statistics.fmean(inlier)


def _enable_raw_capture(port: Port) -> None:
    """Disable correction models for calibration; keep EMA filter for stable live reads."""
    port.daq.pressure_offset = 0.0
    port.daq._error_model = None
    port.alicat._error_model = None


def _sweep_csv_path(port_id: str) -> Path:
    log_dir = get_quality_cal_logs_dir()
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    return log_dir / f'quality_cal_sweep_{port_id}_{stamp}.csv'


def _point_passed(
    *,
    settings: QualitySettings,
    target_psia: float,
    avg_mensor: Optional[float],
    avg_alicat: Optional[float],
    avg_transducer: Optional[float],
) -> tuple[bool, Optional[float], Optional[float], bool]:
    """Return raw deviation fields; pass/fail is decided after correlation fit."""
    mensor_used = settings.require_mensor and target_psia <= settings.mensor_max_psia + 1e-6
    if mensor_used:
        if avg_mensor is None or avg_alicat is None:
            return False, None, None, True
        alicat_dev = avg_mensor - avg_alicat
        transducer_dev = None
        if avg_transducer is not None:
            transducer_dev = avg_mensor - avg_transducer
        return False, alicat_dev, transducer_dev, True
    if avg_alicat is None:
        return False, None, None, False
    alicat_err = avg_alicat - target_psia
    passed = abs(alicat_err) <= settings.pressure_tolerance_psia
    return passed, alicat_err, None, False


class CalibrationRunner(QObject):
    """Run the static pressure-point calibration workflow for a single port."""

    progressChanged = pyqtSignal(int, str)
    liveReadingsUpdated = pyqtSignal(object, object, object, object)
    pointMeasured = pyqtSignal(object)
    singlePointDone = pyqtSignal(object)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()
    sweepCsvReady = pyqtSignal(str)
    mensorDisconnectRequired = pyqtSignal(float)

    def __init__(
        self,
        *,
        port_id: str,
        port: Port,
        mensor: MensorReader,
        settings: QualitySettings,
        auto_ack_mensor_disconnect: bool = False,
    ) -> None:
        super().__init__()
        self._port_id = port_id
        self._port = port
        self._mensor = mensor
        self._settings = settings
        self._auto_ack_mensor_disconnect = auto_ack_mensor_disconnect
        self._cancel_event = threading.Event()
        self._mensor_disconnect_ack = threading.Event()
        self._mensor_disconnect_prompted = False

    def request_cancel(self) -> None:
        self._cancel_event.set()
        self._mensor_disconnect_ack.set()

    @pyqtSlot()
    def acknowledge_mensor_disconnect(self) -> None:
        self._mensor_disconnect_ack.set()

    def _maybe_wait_mensor_disconnect(self, target_psia: float) -> None:
        threshold = self._settings.prompt_disconnect_mensor_above_psi
        if threshold is None or target_psia <= threshold + 1e-6:
            return
        if self._mensor_disconnect_prompted:
            return
        self._mensor_disconnect_prompted = True
        if self._auto_ack_mensor_disconnect:
            logger.info(
                '%s: Headless auto-ack — Mensor should be disconnected above %.1f psia '
                '(target %.1f psia)',
                self._port_id,
                threshold,
                target_psia,
            )
            return
        self._mensor_disconnect_ack.clear()
        self.mensorDisconnectRequired.emit(float(target_psia))
        if not self._mensor_disconnect_ack.wait(timeout=600.0):
            raise RuntimeError('Timed out waiting for Mensor disconnect confirmation')

    def _read_mensor(self, target_psia: float) -> Optional[float]:
        if target_psia > self._settings.mensor_max_psia + 1e-6:
            return None
        try:
            return self._mensor.read_pressure(port_id=self._port_id).pressure_psia
        except Exception:
            return None

    def _hold_and_sample(
        self,
        *,
        index: int,
        target_psia: float,
        route: str,
        last_barometric: float,
        sweep: Optional[_SweepCsvWriter],
    ) -> tuple[CalibrationPointResult, float]:
        phase = f'static_{int(round(target_psia))}'
        mensor_values: list[float] = []
        alicat_values: list[float] = []
        transducer_values: list[float] = []
        hold_start = time.perf_counter()
        sample_period_s = max(0.05, 1.0 / max(self._settings.sample_hz, 0.1))

        if self._port.daq.get_status().get('simulated'):
            self._port.daq.sim_set_pressure(target_psia)

        _, static_hold_s = point_timing_for_target(target_psia, self._settings)
        discard_s = max(0.0, min(self._settings.static_discard_s, static_hold_s * 0.5))

        while time.perf_counter() - hold_start < static_hold_s:
            if self._cancel_event.is_set():
                raise RuntimeError('Cancelled')

            reading = self._port.read_all()
            alicat_value = alicat_abs_psia(reading, last_barometric)
            transducer_raw = transducer_abs_psia(reading, last_barometric, use_raw=True)
            transducer_display = transducer_abs_psia(reading, last_barometric)
            include_for_pass = (time.perf_counter() - hold_start) >= discard_s
            if include_for_pass:
                if alicat_value is not None:
                    alicat_values.append(alicat_value)
                if transducer_raw is not None:
                    transducer_values.append(transducer_raw)

            mensor_value = self._read_mensor(target_psia)
            if include_for_pass and mensor_value is not None:
                mensor_values.append(mensor_value)

            if sweep is not None:
                sweep.write_row(
                    phase=phase,
                    target_abs_psi=target_psia,
                    transducer_abs_psi=transducer_display,
                    transducer_raw_abs_psi=transducer_raw,
                    alicat_abs_psi=alicat_value,
                    mensor_abs_psia=mensor_value,
                )

            self.liveReadingsUpdated.emit(
                mensor_value,
                alicat_value,
                transducer_display,
                target_psia,
            )
            time.sleep(sample_period_s)

        avg_alicat = _average(alicat_values)
        avg_mensor = _robust_average(mensor_values, expected_near=target_psia)
        avg_transducer = _average(transducer_values)
        passed, deviation, transducer_deviation, mensor_used = _point_passed(
            settings=self._settings,
            target_psia=target_psia,
            avg_mensor=avg_mensor,
            avg_alicat=avg_alicat,
            avg_transducer=avg_transducer,
        )
        points = self._settings.pressure_points_psia
        return (
            CalibrationPointResult(
                port_id=self._port_id,
                point_index=index,
                point_total=len(points),
                target_psia=target_psia,
                route=route,
                mensor_psia=avg_mensor,
                alicat_psia=avg_alicat,
                transducer_psia=avg_transducer,
                deviation_psia=deviation,
                transducer_deviation_psia=transducer_deviation,
                passed=passed,
                settle_duration_s=0.0,
                hold_duration_s=static_hold_s,
                sample_count=max(len(alicat_values), len(mensor_values), len(transducer_values)),
                mensor_used=mensor_used,
            ),
            last_barometric,
        )

    @pyqtSlot()
    def run(self) -> None:
        results: list[CalibrationPointResult] = []
        points = self._settings.pressure_points_psia
        last_barometric = 14.7
        sweep: Optional[_SweepCsvWriter] = None
        sweep_path: Optional[Path] = None

        try:
            if self._settings.capture_raw_during_sweep:
                _enable_raw_capture(self._port)

            sweep_path = _sweep_csv_path(self._port_id)
            sweep = _SweepCsvWriter(sweep_path, self._port_id)
            last_route: Optional[str] = None
            units_configured = False

            for index, target_psia in enumerate(points, start=1):
                if self._cancel_event.is_set():
                    self.cancelled.emit()
                    return

                self._maybe_wait_mensor_disconnect(target_psia)

                percent = int(((index - 1) / max(len(points), 1)) * 100)
                self.progressChanged.emit(percent, f'Preparing point {index}/{len(points)}')
                route_ok, route, last_barometric = prepare_port_for_target(
                    self._port,
                    target_psia,
                    last_barometric,
                    self._cancel_event,
                    previous_route=last_route,
                )
                if not route_ok:
                    raise RuntimeError(
                        f'Failed to route {self._port_id} for {target_psia:.1f} psia',
                    )
                last_route = route

                logger.info(
                    '%s: Point %s/%s target=%.3f psia (route prep done, route=%s)',
                    self._port_id,
                    index,
                    len(points),
                    target_psia,
                    route,
                )
                command_target_pressure(
                    self._port,
                    target_psia=target_psia,
                    ramp_rate_psi_per_s=8.0,
                    configure_units=not units_configured,
                )
                units_configured = True

                def _settle_progress(
                    msg: str,
                    alicat_psia: Optional[float],
                    transducer_psia: Optional[float],
                ) -> None:
                    self.progressChanged.emit(percent, msg)
                    mensor_psia = self._read_mensor(target_psia)
                    self.liveReadingsUpdated.emit(
                        mensor_psia,
                        alicat_psia,
                        transducer_psia,
                        target_psia,
                    )

                settle_hold_s, _ = point_timing_for_target(target_psia, self._settings)
                settle_tol = settle_tolerance_for_target(
                    target_psia,
                    self._settings.settle_tolerance_psia,
                )
                settle_timeout_s = settle_timeout_for_target(
                    target_psia,
                    self._settings.settle_timeout_s,
                )
                stabilized = wait_until_near_target(
                    port=self._port,
                    target_psia=target_psia,
                    tolerance_psia=settle_tol,
                    hold_s=settle_hold_s,
                    timeout_s=settle_timeout_s,
                    sample_hz=self._settings.sample_hz,
                    cancel_event=self._cancel_event,
                    progress_callback=_settle_progress,
                    route=route,
                )
                last_barometric = stabilized.barometric_psia

                self.progressChanged.emit(
                    percent,
                    f'Holding point {index}/{len(points)} at {target_psia:.1f} psia',
                )

                point_result, last_barometric = self._hold_and_sample(
                    index=index,
                    target_psia=target_psia,
                    route=route,
                    last_barometric=last_barometric,
                    sweep=sweep,
                )
                point_result = CalibrationPointResult(
                    port_id=point_result.port_id,
                    point_index=point_result.point_index,
                    point_total=point_result.point_total,
                    target_psia=point_result.target_psia,
                    route=point_result.route,
                    mensor_psia=point_result.mensor_psia,
                    alicat_psia=point_result.alicat_psia,
                    transducer_psia=point_result.transducer_psia,
                    deviation_psia=point_result.deviation_psia,
                    transducer_deviation_psia=point_result.transducer_deviation_psia,
                    passed=point_result.passed,
                    settle_duration_s=stabilized.elapsed_s,
                    hold_duration_s=point_result.hold_duration_s,
                    sample_count=point_result.sample_count,
                    mensor_used=point_result.mensor_used,
                )
                results.append(point_result)
                logger.info(
                    '%s: Completed point %s/%s target=%.3f pass=%s xducer=%s',
                    self._port_id,
                    index,
                    len(points),
                    target_psia,
                    point_result.passed,
                    f'{point_result.transducer_psia:.3f}'
                    if point_result.transducer_psia is not None
                    else 'n/a',
                )
                self.pointMeasured.emit(point_result)
                self.progressChanged.emit(
                    int((index / max(len(points), 1)) * 100),
                    f'Completed point {index}/{len(points)}',
                )

            safe_shutdown_port(self._port)
            if sweep is not None:
                sweep.close()
                sweep = None
            if sweep_path is not None:
                try:
                    publish_latest_raw_reference(
                        sweep_path,
                        port_id=self._port_id,
                        profile_id=getattr(self._settings, 'profile_id', None),
                    )
                except Exception as exc:
                    logger.warning(
                        '%s: failed to publish raw reference from %s: %s',
                        self._port_id,
                        sweep_path,
                        exc,
                    )
                self.sweepCsvReady.emit(str(sweep_path))
            self.finished.emit(results)
        except Exception as exc:
            safe_shutdown_port(self._port)
            if str(exc) == 'Cancelled':
                self.cancelled.emit()
                return
            self.failed.emit(str(exc))
        finally:
            if sweep is not None:
                sweep.close()

    @pyqtSlot(int)
    def run_single_point(self, point_index: int) -> None:
        points = self._settings.pressure_points_psia
        if point_index < 1 or point_index > len(points):
            self.failed.emit(f'Point index {point_index} out of range 1..{len(points)}')
            return
        target_psia = points[point_index - 1]
        last_barometric = infer_barometric_psia(self._port.read_all()) or 14.7
        sweep: Optional[_SweepCsvWriter] = None
        try:
            self.progressChanged.emit(0, f'Retesting point {point_index}/{len(points)} at {target_psia:.1f} psia')
            self._maybe_wait_mensor_disconnect(target_psia)
            route_ok, route, last_barometric = prepare_port_for_target(
                self._port,
                target_psia,
                last_barometric,
                self._cancel_event,
                previous_route=None,
            )
            if not route_ok:
                self.failed.emit(f'Failed to route for {target_psia:.1f} psia')
                return

            command_target_pressure(self._port, target_psia=target_psia, ramp_rate_psi_per_s=8.0)

            def _retest_settle_progress(
                msg: str,
                alicat_psia: Optional[float],
                transducer_psia: Optional[float],
            ) -> None:
                self.progressChanged.emit(0, msg)
                self.liveReadingsUpdated.emit(
                    self._read_mensor(target_psia),
                    alicat_psia,
                    transducer_psia,
                    target_psia,
                )

            retest_settle_hold_s, _ = point_timing_for_target(target_psia, self._settings)
            retest_tol = settle_tolerance_for_target(
                target_psia,
                self._settings.settle_tolerance_psia,
            )
            retest_timeout_s = settle_timeout_for_target(
                target_psia,
                self._settings.settle_timeout_s,
            )
            stabilized = wait_until_near_target(
                port=self._port,
                target_psia=target_psia,
                tolerance_psia=retest_tol,
                hold_s=retest_settle_hold_s,
                timeout_s=retest_timeout_s,
                sample_hz=self._settings.sample_hz,
                cancel_event=self._cancel_event,
                progress_callback=_retest_settle_progress,
                route=route,
            )
            last_barometric = stabilized.barometric_psia

            point_result, _ = self._hold_and_sample(
                index=point_index,
                target_psia=target_psia,
                route=route,
                last_barometric=last_barometric,
                sweep=None,
            )
            result = CalibrationPointResult(
                port_id=point_result.port_id,
                point_index=point_result.point_index,
                point_total=point_result.point_total,
                target_psia=point_result.target_psia,
                route=point_result.route,
                mensor_psia=point_result.mensor_psia,
                alicat_psia=point_result.alicat_psia,
                transducer_psia=point_result.transducer_psia,
                deviation_psia=point_result.deviation_psia,
                transducer_deviation_psia=point_result.transducer_deviation_psia,
                passed=point_result.passed,
                settle_duration_s=stabilized.elapsed_s,
                hold_duration_s=point_result.hold_duration_s,
                sample_count=point_result.sample_count,
                mensor_used=point_result.mensor_used,
            )
            self.pointMeasured.emit(result)
            self.singlePointDone.emit(result)
        except Exception as exc:
            safe_shutdown_port(self._port)
            if str(exc) == 'Cancelled':
                self.cancelled.emit()
                return
            self.failed.emit(str(exc))
        finally:
            if sweep is not None:
                sweep.close()
