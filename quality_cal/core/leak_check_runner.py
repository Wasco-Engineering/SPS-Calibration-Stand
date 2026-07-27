"""Optional leak-check runner for the standalone quality calibration app."""

from __future__ import annotations

import threading
import time
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from app.hardware.port import Port
from quality_cal.config import QualitySettings
from quality_cal.core.hardware_helpers import (
    alicat_abs_psia,
    command_target_pressure,
    port_configured_barometric_psia,
    prepare_port_for_target,
    safe_shutdown_port,
    transducer_abs_psia,
    wait_until_near_target,
)
from quality_cal.session import LeakCheckResult


def compute_leak_rate_psi_per_min(samples: list[tuple[float, float]]) -> Optional[float]:
    """Return positive pressure decay rate in psi/min from time/value samples."""
    if len(samples) < 2:
        return None
    xs = [sample[0] for sample in samples]
    ys = [sample[1] for sample in samples]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    if ss_xx <= 0.0:
        return 0.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / ss_xx
    # Leak is reported as positive pressure loss per minute.
    return max(0.0, -slope * 60.0)


class LeakCheckRunner(QObject):
    progressChanged = pyqtSignal(int, str)
    sampleData = pyqtSignal(float, float, float)  # elapsed_s, alicat_psia, transducer_psia
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, *, port_id: str, port: Port, settings: QualitySettings) -> None:
        super().__init__()
        self._port_id = port_id
        self._port = port
        self._settings = settings
        self._cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self._cancel_event.set()

    @pyqtSlot()
    def run(self) -> None:
        last_barometric = port_configured_barometric_psia(self._port)
        try:
            self.progressChanged.emit(5, "Preparing leak-check route")
            route_ok, _route, last_barometric = prepare_port_for_target(
                self._port,
                self._settings.leak_check_target_psia,
                last_barometric,
                self._cancel_event,
            )
            if not route_ok:
                raise RuntimeError("Failed to route port for leak check")

            command_target_pressure(
                self._port,
                target_psia=self._settings.leak_check_target_psia,
                ramp_rate_psi_per_s=self._settings.leak_check_ramp_rate_psi_per_s,
            )
            stabilized = wait_until_near_target(
                port=self._port,
                target_psia=self._settings.leak_check_target_psia,
                tolerance_psia=self._settings.settle_tolerance_psia,
                hold_s=self._settings.settle_hold_s,
                timeout_s=self._settings.settle_timeout_s,
                sample_hz=self._settings.leak_check_sample_hz,
                cancel_event=self._cancel_event,
                progress_callback=lambda msg, _a, _t: self.progressChanged.emit(20, msg),
            )
            last_barometric = stabilized.barometric_psia
            self._port.alicat.hold_valve()

            self.progressChanged.emit(25, "Leak check running in hold mode")
            start = time.perf_counter()
            sample_period_s = max(0.05, 1.0 / max(self._settings.leak_check_sample_hz, 0.1))
            alicat_samples: list[tuple[float, float]] = []
            transducer_samples: list[tuple[float, float]] = []

            while time.perf_counter() - start < self._settings.leak_check_duration_s:
                if self._cancel_event.is_set():
                    self.cancelled.emit()
                    return

                elapsed = time.perf_counter() - start
                reading = self._port.read_all()
                alicat_value = alicat_abs_psia(reading, last_barometric)
                transducer_value = transducer_abs_psia(reading, last_barometric)
                if alicat_value is not None:
                    alicat_samples.append((elapsed, alicat_value))
                if transducer_value is not None:
                    transducer_samples.append((elapsed, transducer_value))
                self.sampleData.emit(
                    elapsed,
                    alicat_value if alicat_value is not None else 0.0,
                    transducer_value if transducer_value is not None else 0.0,
                )

                percent = 25 + int((elapsed / max(self._settings.leak_check_duration_s, 1.0)) * 70)
                remaining = max(0.0, self._settings.leak_check_duration_s - elapsed)
                self.progressChanged.emit(
                    min(percent, 95),
                    f"Measuring leak rate... {remaining:.0f}s remaining",
                )
                time.sleep(sample_period_s)

            if not alicat_samples:
                raise RuntimeError("No Alicat samples collected during leak check")

            alicat_rate = compute_leak_rate_psi_per_min(alicat_samples)
            transducer_rate = compute_leak_rate_psi_per_min(transducer_samples)
            passed = None
            limit = self._settings.leak_check_max_rate_psi_per_min
            if limit is not None and alicat_rate is not None:
                passed = alicat_rate <= limit

            result = LeakCheckResult(
                port_id=self._port_id,
                target_psia=self._settings.leak_check_target_psia,
                duration_s=self._settings.leak_check_duration_s,
                initial_alicat_psia=alicat_samples[0][1],
                final_alicat_psia=alicat_samples[-1][1],
                initial_transducer_psia=transducer_samples[0][1] if transducer_samples else None,
                final_transducer_psia=transducer_samples[-1][1] if transducer_samples else None,
                alicat_leak_rate_psi_per_min=alicat_rate or 0.0,
                transducer_leak_rate_psi_per_min=transducer_rate,
                passed=passed,
            )
            safe_shutdown_port(self._port)
            self.progressChanged.emit(100, "Leak check complete")
            self.finished.emit(result)
        except Exception as exc:
            safe_shutdown_port(self._port)
            if str(exc) == "Cancelled":
                self.cancelled.emit()
                return
            self.failed.emit(str(exc))
