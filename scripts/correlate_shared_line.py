#!/usr/bin/env python3
"""Correlate both Alicats and transducers on a shared pressure line.

Only one Alicat controls at a time. The other controller is held closed so it
can measure the shared line without fighting or venting the active controller.
All pressure arguments and CSV pressure columns are absolute PSI.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_config, setup_logging
from app.hardware.port import Port, PortId, PortManager

logger = logging.getLogger(__name__)

TORR_PER_PSI = 51.714932572
DEFAULT_STATIC_POINTS = (0.5, 1.0, 2.0, 5.0, 7.5, 10.0, 14.7, 20.0, 25.0, 29.5)
CSV_FIELDS = (
    'host_timestamp_s',
    'elapsed_s',
    'driver',
    'phase',
    'target_psia',
    'commanded_rate_torr_s',
    'alicat_a_timestamp_s',
    'alicat_a_psia',
    'alicat_a_raw_psia',
    'alicat_a_setpoint_psia',
    'alicat_b_timestamp_s',
    'alicat_b_psia',
    'alicat_b_raw_psia',
    'alicat_b_setpoint_psia',
    'transducer_a_timestamp_s',
    'transducer_a_psia',
    'transducer_a_unfiltered_psia',
    'transducer_a_linear_psia',
    'transducer_a_voltage',
    'transducer_b_timestamp_s',
    'transducer_b_psia',
    'transducer_b_unfiltered_psia',
    'transducer_b_linear_psia',
    'transducer_b_voltage',
)


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _linear_pressure(port: Port, voltage: Optional[float]) -> Optional[float]:
    """Convert voltage to pre-offset, pre-error-model pressure."""
    value = _finite(voltage)
    if value is None:
        return None
    daq = port.daq
    voltage_span = float(daq.voltage_max) - float(daq.voltage_min)
    if voltage_span <= 0:
        return None
    pressure_span = float(daq.pressure_max) - float(daq.pressure_min)
    return (
        (value - float(daq.voltage_min)) / voltage_span * pressure_span
        + float(daq.pressure_min)
    )


def _expected_leg_seconds(start_psia: float, target_psia: float, rate_torr_s: float) -> float:
    """Return ideal ramp duration for a pressure move."""
    return abs(target_psia - start_psia) * TORR_PER_PSI / rate_torr_s


class SharedLineRunner:
    """Run and record a shared-line four-sensor comparison."""

    def __init__(
        self,
        manager: PortManager,
        output_dir: Path,
        *,
        sample_hz: float,
        low_psia: float,
        high_psia: float,
    ) -> None:
        self.manager = manager
        self.port_a = self._require_port(PortId.PORT_A)
        self.port_b = self._require_port(PortId.PORT_B)
        self.output_dir = output_dir
        self.sample_period_s = 1.0 / max(1.0, sample_hz)
        self.low_psia = low_psia
        self.high_psia = high_psia
        self.started_s = time.time()
        self.csv_path = output_dir / 'shared_line_samples.csv'
        self.events_path = output_dir / 'shared_line_events.jsonl'
        self._csv_stream: Any = None
        self._event_stream: Any = None
        self._writer: Optional[csv.DictWriter] = None
        self._driver = 'none'

    def _require_port(self, port_id: PortId) -> Port:
        port = self.manager.get_port(port_id)
        if port is None:
            raise RuntimeError(f'{port_id.value} is unavailable')
        return port

    def __enter__(self) -> 'SharedLineRunner':
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._csv_stream = self.csv_path.open('w', newline='', encoding='utf-8')
        self._event_stream = self.events_path.open('w', encoding='utf-8')
        self._writer = csv.DictWriter(self._csv_stream, fieldnames=CSV_FIELDS)
        self._writer.writeheader()
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._csv_stream is not None:
            self._csv_stream.close()
        if self._event_stream is not None:
            self._event_stream.close()

    def event(self, name: str, **payload: Any) -> None:
        record = {'timestamp_s': time.time(), 'event': name, **payload}
        assert self._event_stream is not None
        self._event_stream.write(json.dumps(record, sort_keys=True) + '\n')
        self._event_stream.flush()
        logger.info('EVENT %s %s', name, payload)

    def _sample(self, phase: str, target_psia: float, rate_torr_s: float) -> dict[str, Any]:
        # Read each transducer directly; Alicats are independently timestamped
        # by the high-rate shared-COM cache poller.
        trans_a = self.port_a.daq.read_transducer()
        trans_b = self.port_b.daq.read_transducer()
        alicat_a = self.port_a.get_cached_alicat()
        alicat_b = self.port_b.get_cached_alicat()
        now = time.time()
        row = {
            'host_timestamp_s': now,
            'elapsed_s': now - self.started_s,
            'driver': self._driver,
            'phase': phase,
            'target_psia': target_psia,
            'commanded_rate_torr_s': rate_torr_s,
            'alicat_a_timestamp_s': getattr(alicat_a, 'timestamp', None),
            'alicat_a_psia': getattr(alicat_a, 'pressure', None),
            'alicat_a_raw_psia': getattr(alicat_a, 'pressure_raw', None),
            'alicat_a_setpoint_psia': getattr(alicat_a, 'setpoint', None),
            'alicat_b_timestamp_s': getattr(alicat_b, 'timestamp', None),
            'alicat_b_psia': getattr(alicat_b, 'pressure', None),
            'alicat_b_raw_psia': getattr(alicat_b, 'pressure_raw', None),
            'alicat_b_setpoint_psia': getattr(alicat_b, 'setpoint', None),
            'transducer_a_timestamp_s': getattr(trans_a, 'timestamp', None),
            'transducer_a_psia': getattr(trans_a, 'pressure', None),
            'transducer_a_unfiltered_psia': getattr(trans_a, 'pressure_raw', None),
            'transducer_a_linear_psia': _linear_pressure(
                self.port_a, getattr(trans_a, 'voltage', None)
            ),
            'transducer_a_voltage': getattr(trans_a, 'voltage', None),
            'transducer_b_timestamp_s': getattr(trans_b, 'timestamp', None),
            'transducer_b_psia': getattr(trans_b, 'pressure', None),
            'transducer_b_unfiltered_psia': getattr(trans_b, 'pressure_raw', None),
            'transducer_b_linear_psia': _linear_pressure(
                self.port_b, getattr(trans_b, 'voltage', None)
            ),
            'transducer_b_voltage': getattr(trans_b, 'voltage', None),
        }
        assert self._writer is not None
        self._writer.writerow(row)
        return row

    @staticmethod
    def _pressures(row: dict[str, Any]) -> dict[str, float]:
        result: dict[str, float] = {}
        for key in ('alicat_a_psia', 'alicat_b_psia', 'transducer_a_psia', 'transducer_b_psia'):
            value = _finite(row.get(key))
            if value is not None:
                result[key] = value
        return result

    def _guard(self, row: dict[str, Any], *, require_all: bool = True) -> None:
        pressures = self._pressures(row)
        if require_all and len(pressures) != 4:
            raise RuntimeError(f'Missing shared-line channel: {pressures}')
        for channel, pressure in pressures.items():
            if not -0.75 <= pressure <= 30.75:
                raise RuntimeError(f'{channel} outside safe diagnostic range: {pressure:.3f} psia')

    def set_driver(self, driver: str) -> None:
        if driver not in {'A', 'B'}:
            raise ValueError(f'Unknown driver {driver!r}')
        active = self.port_a if driver == 'A' else self.port_b
        passive = self.port_b if driver == 'A' else self.port_a
        if not passive.alicat.hold_valve(closed=True):
            raise RuntimeError(f'Failed to hold passive Alicat {passive.alicat.address} closed')
        passive.refresh_alicat()
        if not passive._alicat_in_hold_mode():
            raise RuntimeError(f'Passive Alicat {passive.alicat.address} did not enter HLD')
        self._driver = driver
        self.event('driver_selected', driver=driver, passive=passive.alicat.address)
        # Synchronize the active controller's stored setpoint to the live line
        # at a fast rate. Otherwise C resumes a stale pre-test setpoint and the
        # internal ramp generator can initially move away from the requested
        # diagnostic target.
        active.alicat.hold_valve(closed=True)
        active_reading = active.get_cached_alicat()
        current = _finite(getattr(active_reading, 'pressure', None))
        if current is None:
            raise RuntimeError(f'Active Alicat {active.alicat.address} pressure unavailable')
        self.move_and_capture(
            target_psia=current,
            rate_torr_s=100.0,
            phase=f'driver_{driver.lower()}_setpoint_sync',
            settle_s=1.0,
            tolerance_torr=10.0,
        )

    def prepare_shared_route(self) -> None:
        for port in (self.port_a, self.port_b):
            if not port.alicat.hold_valve(closed=True):
                raise RuntimeError(f'Failed to close Alicat {port.alicat.address}')
        if not self.port_a.connect_test_route() or not self.port_b.connect_test_route():
            raise RuntimeError('Failed to connect both ports to the shared test route')
        self.event('shared_route_connected')

    def capture(self, phase: str, target_psia: float, rate_torr_s: float, duration_s: float) -> None:
        deadline = time.perf_counter() + duration_s
        next_sample = time.perf_counter()
        while time.perf_counter() < deadline:
            row = self._sample(phase, target_psia, rate_torr_s)
            self._guard(row)
            next_sample += self.sample_period_s
            delay = next_sample - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -self.sample_period_s:
                next_sample = time.perf_counter()
        self._csv_stream.flush()

    def move_and_capture(
        self,
        *,
        target_psia: float,
        rate_torr_s: float,
        phase: str,
        settle_s: float,
        tolerance_torr: float = 10.0,
    ) -> None:
        active = self.port_a if self._driver == 'A' else self.port_b
        current_reading = active.get_cached_alicat()
        current = _finite(getattr(current_reading, 'pressure', None))
        if current is None:
            raise RuntimeError(f'Driver {self._driver} pressure unavailable')
        rate_psi_s = rate_torr_s / TORR_PER_PSI
        if not active.set_ramp_rate(rate_psi_s):
            raise RuntimeError(f'Driver {self._driver} rejected ramp rate {rate_torr_s} Torr/s')
        if not active.set_pressure(target_psia):
            raise RuntimeError(f'Driver {self._driver} rejected target {target_psia} psia')

        expected_s = _expected_leg_seconds(current, target_psia, rate_torr_s)
        timeout_s = expected_s + max(60.0, expected_s * 0.15)
        tolerance_psi = tolerance_torr / TORR_PER_PSI
        deadline = time.perf_counter() + timeout_s
        stability_window_s = max(1.0, settle_s)
        stable_window: deque[tuple[float, float]] = deque()
        next_progress = time.perf_counter() + 30.0
        self.event(
            'move_start',
            driver=self._driver,
            phase=phase,
            start_psia=current,
            target_psia=target_psia,
            rate_torr_s=rate_torr_s,
            expected_s=expected_s,
        )
        while time.perf_counter() < deadline:
            row = self._sample(phase, target_psia, rate_torr_s)
            self._guard(row)
            driver_pressure = _finite(row[f'alicat_{self._driver.lower()}_psia'])
            driver_setpoint = _finite(row[f'alicat_{self._driver.lower()}_setpoint_psia'])
            now = time.perf_counter()
            if driver_pressure is not None:
                stable_window.append((now, driver_pressure))
                while stable_window and now - stable_window[0][0] > stability_window_s:
                    stable_window.popleft()
            setpoint_finished = (
                driver_setpoint is not None
                and abs(driver_setpoint - target_psia) * TORR_PER_PSI <= 1.0
            )
            pressure_near_target = (
                driver_pressure is not None
                and abs(driver_pressure - target_psia) <= tolerance_psi
            )
            pressure_stable = False
            if (
                len(stable_window) >= 4
                and now - stable_window[0][0] >= stability_window_s * 0.9
            ):
                midpoint = len(stable_window) // 2
                first = [value for _, value in list(stable_window)[:midpoint]]
                second = [value for _, value in list(stable_window)[midpoint:]]
                trend_torr_s = (
                    (sum(second) / len(second) - sum(first) / len(first))
                    * TORR_PER_PSI
                    * 2.0
                    / stability_window_s
                )
                ordered = sorted(value for _, value in stable_window)
                p10 = ordered[int((len(ordered) - 1) * 0.10)]
                p90 = ordered[int((len(ordered) - 1) * 0.90)]
                robust_span_torr = (p90 - p10) * TORR_PER_PSI
                pressure_stable = (
                    abs(trend_torr_s) <= 0.35 and robust_span_torr <= 3.0
                )
            if setpoint_finished and pressure_near_target and pressure_stable:
                self._csv_stream.flush()
                self.event(
                    'move_complete',
                    driver=self._driver,
                    phase=phase,
                    target_psia=target_psia,
                    measured_psia=driver_pressure,
                )
                return
            if now >= next_progress:
                logger.info(
                    '%s driver=%s target=%.3f psia current=%s elapsed remaining profile active',
                    phase,
                    self._driver,
                    target_psia,
                    f'{driver_pressure:.3f}' if driver_pressure is not None else 'missing',
                )
                next_progress += 30.0
            time.sleep(max(0.0, self.sample_period_s))
        raise RuntimeError(
            f'{phase}: driver {self._driver} did not settle at {target_psia:.3f} psia '
            f'within {timeout_s:.1f}s'
        )

    def preflight(self) -> None:
        self.set_driver('A')
        initial = self._sample('preflight_initial', float('nan'), 0.0)
        self._guard(initial)
        values = self._pressures(initial)
        spread_torr = (max(values.values()) - min(values.values())) * TORR_PER_PSI
        self.event('preflight_initial', pressures=values, spread_torr=spread_torr)
        if spread_torr > 75.0:
            raise RuntimeError(f'Initial four-channel spread is too large: {spread_torr:.2f} Torr')
        start = values['alicat_a_psia']
        target = min(self.high_psia, max(self.low_psia, start - 0.5))
        self.move_and_capture(
            target_psia=target,
            rate_torr_s=5.0,
            phase='preflight_move',
            settle_s=1.0,
            tolerance_torr=10.0,
        )
        final = self._sample('preflight_final', target, 0.0)
        self._guard(final)
        final_values = self._pressures(final)
        final_spread_torr = (max(final_values.values()) - min(final_values.values())) * TORR_PER_PSI
        moved = start - final_values['alicat_a_psia']
        for channel, pressure in final_values.items():
            if channel == 'alicat_a_psia':
                continue
            if values[channel] - pressure < 0.15:
                raise RuntimeError(f'{channel} did not follow the shared-line preflight move')
        if moved < 0.3 or final_spread_torr > 75.0:
            raise RuntimeError(
                f'Preflight failed: moved={moved:.3f} psi spread={final_spread_torr:.2f} Torr'
            )
        self.event('preflight_passed', pressures=final_values, spread_torr=final_spread_torr)

    def static_profile(self, points: tuple[float, ...], repeats: int, label: str) -> None:
        for repeat in range(1, repeats + 1):
            order = points + tuple(reversed(points))
            for index, target in enumerate(order, 1):
                self.move_and_capture(
                    target_psia=target,
                    rate_torr_s=100.0,
                    phase=f'{label}_r{repeat:02d}_p{index:02d}_move',
                    settle_s=1.0,
                    tolerance_torr=10.0,
                )
                self.capture(
                    f'{label}_r{repeat:02d}_p{index:02d}_hold', target, 0.0, 10.0
                )

    def dynamic_cycles(self, *, driver: str, rate_torr_s: float, cycles: int, label: str) -> None:
        if cycles <= 0:
            return
        self.set_driver(driver)
        self.move_and_capture(
            target_psia=self.high_psia,
            rate_torr_s=100.0,
            phase=f'{label}_position_high',
            settle_s=2.0,
            tolerance_torr=10.0,
        )
        for cycle in range(1, cycles + 1):
            self.capture(f'{label}_c{cycle:02d}_high_hold', self.high_psia, 0.0, 5.0)
            self.move_and_capture(
                target_psia=self.low_psia,
                rate_torr_s=rate_torr_s,
                phase=f'{label}_c{cycle:02d}_down',
                settle_s=0.5,
            )
            self.capture(f'{label}_c{cycle:02d}_low_hold', self.low_psia, 0.0, 5.0)
            self.move_and_capture(
                target_psia=self.high_psia,
                rate_torr_s=rate_torr_s,
                phase=f'{label}_c{cycle:02d}_up',
                settle_s=0.5,
            )


def _parse_static_points(value: str) -> tuple[float, ...]:
    points = tuple(float(item.strip()) for item in value.split(',') if item.strip())
    if not points:
        raise argparse.ArgumentTypeError('At least one static point is required')
    return points


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir')
    parser.add_argument('--low-psia', type=float, default=0.5)
    parser.add_argument('--high-psia', type=float, default=29.5)
    parser.add_argument('--sample-hz', type=float, default=100.0)
    parser.add_argument('--static-points', type=_parse_static_points, default=DEFAULT_STATIC_POINTS)
    parser.add_argument('--static-repeats', type=int, default=3)
    parser.add_argument('--left-1-cycles', type=int, default=1)
    parser.add_argument('--left-5-cycles', type=int, default=3)
    parser.add_argument('--right-1-cycles', type=int, default=1)
    parser.add_argument('--right-5-cycles', type=int, default=1)
    parser.add_argument('--preflight-only', action='store_true')
    args = parser.parse_args()

    if not 0.0 <= args.low_psia < args.high_psia <= 30.0:
        parser.error('Require 0 <= low < high <= 30 psia')
    if any(not args.low_psia <= point <= args.high_psia for point in args.static_points):
        parser.error('Every static point must be within the requested range')

    config = load_config()
    setup_logging(config)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir = Path(
        args.output_dir or PROJECT_ROOT / 'logs' / f'shared_line_correlation_{stamp}'
    )
    manager = PortManager(config)
    metadata = {
        'started_at': datetime.now().astimezone().isoformat(),
        'low_psia': args.low_psia,
        'high_psia': args.high_psia,
        'sample_hz': args.sample_hz,
        'static_points': args.static_points,
        'static_repeats': args.static_repeats,
        'left_1_cycles': args.left_1_cycles,
        'left_5_cycles': args.left_5_cycles,
        'right_1_cycles': args.right_1_cycles,
        'right_5_cycles': args.right_5_cycles,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / 'run_config.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')

    success = False
    try:
        manager.initialize_ports()
        if not manager.connect_all():
            raise RuntimeError('One or more ports failed to connect')
        if not manager.start_polling():
            raise RuntimeError('Failed to start high-rate Alicat polling')
        port_a = manager.get_port(PortId.PORT_A)
        port_b = manager.get_port(PortId.PORT_B)
        if port_a is None or port_b is None:
            raise RuntimeError('Both ports are required')
        for port in (port_a, port_b):
            if not port.alicat.configure_units_from_ptp('13'):
                raise RuntimeError(f'Failed to select Torr resolution on Alicat {port.alicat.address}')

        with SharedLineRunner(
            manager,
            output_dir,
            sample_hz=args.sample_hz,
            low_psia=args.low_psia,
            high_psia=args.high_psia,
        ) as runner:
            runner.prepare_shared_route()
            runner.preflight()
            if not args.preflight_only:
                runner.static_profile(args.static_points, args.static_repeats, 'left_static_before')
                runner.dynamic_cycles(
                    driver='A',
                    rate_torr_s=1.0,
                    cycles=args.left_1_cycles,
                    label='left_1torr_s',
                )
                runner.dynamic_cycles(
                    driver='A',
                    rate_torr_s=5.0,
                    cycles=args.left_5_cycles,
                    label='left_5torr_s',
                )
                if args.static_repeats > 0:
                    runner.static_profile(args.static_points, 1, 'left_static_after')
                runner.dynamic_cycles(
                    driver='B',
                    rate_torr_s=1.0,
                    cycles=args.right_1_cycles,
                    label='right_1torr_s',
                )
                runner.dynamic_cycles(
                    driver='B',
                    rate_torr_s=5.0,
                    cycles=args.right_5_cycles,
                    label='right_5torr_s',
                )
            runner.event('run_complete')
        success = True
    except KeyboardInterrupt:
        logger.warning('Shared-line correlation interrupted by operator')
    except Exception:
        logger.exception('Shared-line correlation failed')
    finally:
        manager.disconnect_all()

    print(f'Output: {output_dir}')
    print(f'Success: {success}')
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
