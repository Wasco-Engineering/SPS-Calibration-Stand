"""Per-port runtime state containers for orchestration services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class PortRuntimeState:
    """Mutable per-port runtime state previously spread across controller fields."""

    last_barometric_psi: Dict[str, float] = field(default_factory=dict)
    barometric_warning_issued: Dict[str, bool] = field(default_factory=dict)
    cycle_estimates_abs_psi: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    current_measured_values: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)
    switch_presence: Dict[str, bool] = field(default_factory=dict)
    manual_switch_latched: Dict[str, bool] = field(default_factory=dict)
    debug_solenoid_mode: Dict[str, str] = field(default_factory=dict)
    debug_alicat_mode: Dict[str, str] = field(default_factory=dict)
    debug_solenoid_last_route: Dict[str, Optional[bool]] = field(default_factory=dict)

    @classmethod
    def with_defaults(cls) -> 'PortRuntimeState':
        ports = ('port_a', 'port_b')
        return cls(
            # Do not seed 14.7 as a "measured" baro — that blocks real site
            # samples (and P−G inference) under the jump filter.
            last_barometric_psi={},
            barometric_warning_issued={pid: False for pid in ports},
            cycle_estimates_abs_psi={
                pid: {'activation': None, 'deactivation': None, 'count': 0}
                for pid in ports
            },
            current_measured_values={
                pid: {'activation': None, 'deactivation': None}
                for pid in ports
            },
            switch_presence={pid: False for pid in ports},
            manual_switch_latched={pid: False for pid in ports},
            debug_solenoid_mode={pid: 'atmosphere' for pid in ports},
            debug_alicat_mode={pid: 'pressurize' for pid in ports},
            debug_solenoid_last_route={pid: None for pid in ports},
        )
