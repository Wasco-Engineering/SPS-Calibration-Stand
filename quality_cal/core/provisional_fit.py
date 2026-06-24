"""Lightweight in-run error models for live calibration charts."""

from __future__ import annotations

from typing import Any, Optional

from app.services.pressure_calibration import (
    REFERENCE_MENSOR,
    SENSOR_ALICAT,
    SENSOR_TRANSDUCER,
    CalibrationSample,
    filter_samples_pressure_band,
    fit_quadratic_error_model,
)
from quality_cal.session import CalibrationPointResult


def _points_to_samples(points: list[CalibrationPointResult]) -> list[CalibrationSample]:
    samples: list[CalibrationSample] = []
    for idx, point in enumerate(points):
        if point.mensor_psia is None:
            continue
        samples.append(
            CalibrationSample(
                index=idx,
                timestamp=float(idx),
                port_id=point.port_id,
                phase=f'static_{int(round(point.target_psia))}',
                target_abs_psi=point.target_psia,
                transducer_abs_psi=point.transducer_psia,
                alicat_abs_psi=point.alicat_psia,
                mensor_abs_psia=point.mensor_psia,
            )
        )
    return samples


def provisional_error_models(
    points: list[CalibrationPointResult],
    *,
    fit_max_psia: float = 30.0,
    alicat_fit_max_psia: float | None = None,
    min_points: int = 3,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Fit quadratic error models from completed points for live UI (not final cert fit)."""
    samples = _points_to_samples(points)
    alicat_max = alicat_fit_max_psia if alicat_fit_max_psia is not None else fit_max_psia
    transducer_banded = filter_samples_pressure_band(
        samples,
        min_psi=0.0,
        max_psi=fit_max_psia,
        reference=REFERENCE_MENSOR,
    )
    alicat_banded = filter_samples_pressure_band(
        samples,
        min_psi=0.0,
        max_psi=alicat_max,
        reference=REFERENCE_MENSOR,
    )
    if len(transducer_banded) < min_points and len(alicat_banded) < min_points:
        return None, None

    transducer_model: Optional[dict[str, Any]] = None
    alicat_model: Optional[dict[str, Any]] = None
    if len(transducer_banded) >= min_points:
        try:
            transducer_model = fit_quadratic_error_model(
                transducer_banded,
                sensor=SENSOR_TRANSDUCER,
                reference=REFERENCE_MENSOR,
            )
        except ValueError:
            transducer_model = None
    if len(alicat_banded) >= min_points:
        try:
            alicat_model = fit_quadratic_error_model(
                alicat_banded,
                sensor=SENSOR_ALICAT,
                reference=REFERENCE_MENSOR,
            )
        except ValueError:
            alicat_model = None
    return transducer_model, alicat_model


def apply_provisional_corrections(
    points: list[CalibrationPointResult],
    *,
    fit_max_psia: float = 30.0,
    alicat_fit_max_psia: float | None = None,
) -> tuple[list[CalibrationPointResult], Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Rescore completed points with in-run quadratic models (for table + chart)."""
    from quality_cal.core.port_calibrator import rescore_points_with_models

    transducer_model, alicat_model = provisional_error_models(
        points,
        fit_max_psia=fit_max_psia,
        alicat_fit_max_psia=alicat_fit_max_psia,
    )
    rescored = rescore_points_with_models(
        points,
        alicat_model=alicat_model,
        transducer_model=transducer_model,
    )
    return rescored, transducer_model, alicat_model
