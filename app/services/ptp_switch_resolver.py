"""Resolve PTP switch terminals onto stand-specific LabJack wiring."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class PtpSwitchResolution:
    """Resolved switch wiring for one port and one PTP setup."""

    port_id: str
    common_terminal: Optional[int]
    normally_open_terminal: Optional[int]
    normally_closed_terminal: Optional[int]
    common_dio: Optional[int]
    drive_terminal: Optional[int]
    drive_dio: Optional[int]
    drive_role: str
    no_dio: Optional[int]
    nc_dio: Optional[int]
    sensed_db9_pins: tuple[int, ...]
    observed_terminals: tuple[str, ...]
    derivation_mode: str
    derive_nc_from_no: bool = False
    derive_no_from_nc: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_valid(self) -> bool:
        return (
            not self.errors
            and self.common_dio is not None
            and self.drive_dio is not None
            and self.no_dio is not None
            and self.nc_dio is not None
        )

    @property
    def summary(self) -> str:
        no_text = self._fmt(self.normally_open_terminal, self.no_dio)
        nc_text = self._fmt(self.normally_closed_terminal, self.nc_dio)
        if self.derive_nc_from_no:
            nc_text = f'{self._fmt_terminal(self.normally_closed_terminal)}->derived from NO DIO{self.no_dio}'
        elif self.derive_no_from_nc:
            no_text = f'{self._fmt_terminal(self.normally_open_terminal)}->derived from NC DIO{self.nc_dio}'
        if self.derivation_mode == 'drive_no_read_common':
            no_text = (
                f'{self._fmt_terminal(self.normally_open_terminal)}->via COM DIO{self.common_dio}'
            )
            nc_text = f'{self._fmt_terminal(self.normally_closed_terminal)}->derived from NO DIO{self.no_dio}'
        elif self.derivation_mode == 'drive_nc_read_common':
            nc_text = (
                f'{self._fmt_terminal(self.normally_closed_terminal)}->via COM DIO{self.common_dio}'
            )
            no_text = f'{self._fmt_terminal(self.normally_open_terminal)}->derived from NC DIO{self.nc_dio}'
        elif self.derivation_mode == 'drive_no_read_adapter_common':
            no_text = (
                f'{self._fmt_terminal(self.normally_open_terminal)}->via adapter common DIO{self.no_dio}'
            )
            nc_text = f'{self._fmt_terminal(self.normally_closed_terminal)}->derived from NO DIO{self.no_dio}'
        elif self.derivation_mode == 'drive_nc_read_adapter_common':
            nc_text = (
                f'{self._fmt_terminal(self.normally_closed_terminal)}->via adapter common DIO{self.nc_dio}'
            )
            no_text = f'{self._fmt_terminal(self.normally_open_terminal)}->derived from NC DIO{self.nc_dio}'
        return (
            f'COM={self._fmt(self.common_terminal, self.common_dio)}, '
            f'NO={no_text}, '
            f'NC={nc_text}, '
            f'drive={self.drive_role} {self._fmt(self.drive_terminal, self.drive_dio)}, '
            f'mode={self.derivation_mode}'
        )

    @staticmethod
    def _fmt(terminal: Optional[int], dio: Optional[int]) -> str:
        term = PtpSwitchResolution._fmt_terminal(terminal)
        return f'{term}->--' if dio is None else f'{term}->DIO{dio}'

    @staticmethod
    def _fmt_terminal(terminal: Optional[int]) -> str:
        return 'not connected' if terminal is None else str(terminal)


def resolve_ptp_switch_config(
    *,
    ptp_params: dict[str, Any],
    port_id: str,
    port_config: dict[str, Any],
) -> PtpSwitchResolution:
    """Resolve PTP DB9 terminals into the DIOs this stand should read.

    PTP is the source of logical switch identity. The port config only describes
    physical stand wiring, such as which DB9 pins are sensed.
    """

    normalized_port = str(port_id).strip().lower()
    warnings: list[str] = []
    errors: list[str] = []

    common_terminal = _terminal_from_ptp(ptp_params, 'CommonTerminal', errors)
    no_terminal = _terminal_from_ptp(
        ptp_params,
        'NormallyOpenTerminal',
        errors,
        warnings,
        allow_not_connected=True,
    )
    nc_terminal = _terminal_from_ptp(
        ptp_params,
        'NormallyClosedTerminal',
        errors,
        warnings,
        allow_not_connected=True,
    )

    if no_terminal is not None and nc_terminal is not None and no_terminal == nc_terminal:
        warnings.append(
            'NormallyOpenTerminal and NormallyClosedTerminal share DB9 pin '
            f'{no_terminal}; treating as single-throw PTP and deriving NC from NO'
        )
        nc_terminal = None
    if no_terminal is None and nc_terminal is None:
        errors.append('At least one of NormallyOpenTerminal or NormallyClosedTerminal must be connected')
    if common_terminal is not None and common_terminal in {no_terminal, nc_terminal}:
        errors.append('CommonTerminal must be different from NO and NC terminals')

    sensed_pins = _sensed_db9_pins(port_config, normalized_port, warnings)
    if not sensed_pins:
        errors.append(
            'No switch_sensed_db9_pins configured for this port; cannot observe PTP NO/NC terminals'
        )

    common_dio = _db9_pin_to_dio(normalized_port, common_terminal)
    no_ptp_dio = _db9_pin_to_dio(normalized_port, no_terminal)
    nc_ptp_dio = _db9_pin_to_dio(normalized_port, nc_terminal)

    no_sensed = no_terminal in sensed_pins if no_terminal is not None else False
    nc_sensed = nc_terminal in sensed_pins if nc_terminal is not None else False
    common_sensed = common_terminal in sensed_pins if common_terminal is not None else False

    no_dio: Optional[int] = None
    nc_dio: Optional[int] = None
    drive_terminal: Optional[int] = None
    drive_dio: Optional[int] = None
    drive_role = 'unresolved'
    observed: tuple[str, ...] = ()
    derivation_mode = 'unresolved'
    derive_nc_from_no = False
    derive_no_from_nc = False

    if not errors:
        if no_sensed and nc_sensed:
            drive_terminal = common_terminal
            drive_dio = common_dio
            drive_role = 'common'
            no_dio = no_ptp_dio
            nc_dio = nc_ptp_dio
            observed = ('normally_open', 'normally_closed')
            derivation_mode = 'direct'
        elif no_sensed:
            drive_terminal = common_terminal
            drive_dio = common_dio
            drive_role = 'common'
            no_dio = no_ptp_dio
            nc_dio = no_ptp_dio
            observed = ('normally_open',)
            derivation_mode = 'derive_nc_from_no'
            derive_nc_from_no = True
        elif nc_sensed:
            drive_terminal = common_terminal
            drive_dio = common_dio
            drive_role = 'common'
            no_dio = nc_ptp_dio
            nc_dio = nc_ptp_dio
            observed = ('normally_closed',)
            derivation_mode = 'derive_no_from_nc'
            derive_no_from_nc = True
        elif common_sensed and no_terminal is not None:
            drive_terminal = no_terminal
            drive_dio = no_ptp_dio
            drive_role = 'normally_open'
            no_dio = common_dio
            nc_dio = common_dio
            observed = ('common_as_normally_open',)
            derivation_mode = 'drive_no_read_common'
            derive_nc_from_no = True
        elif common_sensed and nc_terminal is not None:
            drive_terminal = nc_terminal
            drive_dio = nc_ptp_dio
            drive_role = 'normally_closed'
            no_dio = common_dio
            nc_dio = common_dio
            observed = ('common_as_normally_closed',)
            derivation_mode = 'drive_nc_read_common'
            derive_no_from_nc = True
        else:
            fallback = _resolve_com5_single_sense_fallback(
                normalized_port,
                common_terminal=common_terminal,
                common_dio=common_dio,
                no_terminal=no_terminal,
                nc_terminal=nc_terminal,
                sensed_pins=sensed_pins,
                warnings=warnings,
            )
            if fallback is None:
                fallback = _resolve_single_throw_ptp_fallback(
                    normalized_port,
                    common_terminal=common_terminal,
                    common_dio=common_dio,
                    no_terminal=no_terminal,
                    no_dio=no_ptp_dio,
                    nc_terminal=nc_terminal,
                    nc_dio=nc_ptp_dio,
                    sensed_pins=sensed_pins,
                    warnings=warnings,
                )
            if fallback is None:
                fallback = _resolve_single_throw_adapter_fallback(
                    normalized_port,
                    common_terminal=common_terminal,
                    no_terminal=no_terminal,
                    nc_terminal=nc_terminal,
                    sensed_pins=sensed_pins,
                    warnings=warnings,
                )
            if fallback is not None:
                (
                    drive_terminal,
                    drive_dio,
                    drive_role,
                    no_dio,
                    nc_dio,
                    observed,
                    derivation_mode,
                    derive_nc_from_no,
                    derive_no_from_nc,
                ) = fallback
            else:
                errors.append(
                    'PTP NO/NC terminals are not observable on this stand '
                    f'(COM={common_terminal}, NO={no_terminal}, NC={nc_terminal}, '
                    f'sensed={list(sensed_pins)})'
                )

    return PtpSwitchResolution(
        port_id=normalized_port,
        common_terminal=common_terminal,
        normally_open_terminal=no_terminal,
        normally_closed_terminal=nc_terminal,
        common_dio=common_dio,
        drive_terminal=drive_terminal,
        drive_dio=drive_dio,
        drive_role=drive_role,
        no_dio=no_dio,
        nc_dio=nc_dio,
        sensed_db9_pins=sensed_pins,
        observed_terminals=observed,
        derivation_mode=derivation_mode,
        derive_nc_from_no=derive_nc_from_no,
        derive_no_from_nc=derive_no_from_nc,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def _resolve_single_throw_ptp_fallback(
    port_id: str,
    *,
    common_terminal: Optional[int],
    common_dio: Optional[int],
    no_terminal: Optional[int],
    no_dio: Optional[int],
    nc_terminal: Optional[int],
    nc_dio: Optional[int],
    sensed_pins: tuple[int, ...],
    warnings: list[str],
) -> Optional[
    tuple[
        Optional[int],
        Optional[int],
        str,
        Optional[int],
        Optional[int],
        tuple[str, ...],
        str,
        bool,
        bool,
    ]
]:
    """Resolve SPST parts by reading the one PTP throw even if not predeclared sensed."""

    del port_id
    if common_dio is None or len(sensed_pins) != 1:
        return None
    if (no_terminal is None) == (nc_terminal is None):
        return None

    if no_terminal is not None:
        if no_dio is None:
            return None
        warnings.append(
            f'SPST PTP fallback: read NormallyOpenTerminal {no_terminal} '
            f'(DIO{no_dio}) even though configured sensed pins are {list(sensed_pins)}'
        )
        return (
            common_terminal,
            common_dio,
            'common',
            no_dio,
            no_dio,
            ('normally_open_single_throw',),
            'derive_nc_from_no',
            True,
            False,
        )

    if nc_dio is None:
        return None
    warnings.append(
        f'SPST PTP fallback: read NormallyClosedTerminal {nc_terminal} '
        f'(DIO{nc_dio}) even though configured sensed pins are {list(sensed_pins)}'
    )
    return (
        common_terminal,
        common_dio,
        'common',
        nc_dio,
        nc_dio,
        ('normally_closed_single_throw',),
        'derive_no_from_nc',
        False,
        True,
    )


def _resolve_single_throw_adapter_fallback(
    port_id: str,
    *,
    common_terminal: Optional[int],
    no_terminal: Optional[int],
    nc_terminal: Optional[int],
    sensed_pins: tuple[int, ...],
    warnings: list[str],
) -> Optional[
    tuple[
        Optional[int],
        Optional[int],
        str,
        Optional[int],
        Optional[int],
        tuple[str, ...],
        str,
        bool,
        bool,
    ]
]:
    """Resolve SPST parts whose adapter presents common on the stand sense pin.

    Several SPST PTP rows name product terminals such as COM=1/NO=2 while the
    stand fixture senses DB9 pin 3. For these one-throw switches, read the
    configured stand sense pin and drive the one connected throw. This still
    requires a real electrical transition; it only removes the overly strict
    requirement that the PTP product terminal number equal the stand sense pin.
    """

    if len(sensed_pins) != 1:
        return None
    if (no_terminal is None) == (nc_terminal is None):
        return None

    sensed_pin = sensed_pins[0]
    read_dio = _db9_pin_to_dio(port_id, sensed_pin)
    if read_dio is None:
        return None

    if no_terminal is not None:
        drive_dio = _db9_pin_to_dio(port_id, no_terminal)
        if drive_dio is None:
            return None
        warnings.append(
            f'SPST adapter fallback: read configured sensed pin {sensed_pin} '
            f'(DIO{read_dio}) as common for PTP COM={common_terminal}; '
            f'drive NormallyOpenTerminal {no_terminal}'
        )
        return (
            no_terminal,
            drive_dio,
            'normally_open',
            read_dio,
            read_dio,
            ('adapter_common_as_normally_open',),
            'drive_no_read_adapter_common',
            True,
            False,
        )

    drive_dio = _db9_pin_to_dio(port_id, nc_terminal)
    if drive_dio is None:
        return None
    warnings.append(
        f'SPST adapter fallback: read configured sensed pin {sensed_pin} '
        f'(DIO{read_dio}) as common for PTP COM={common_terminal}; '
        f'drive NormallyClosedTerminal {nc_terminal}'
    )
    return (
        nc_terminal,
        drive_dio,
        'normally_closed',
        read_dio,
        read_dio,
        ('adapter_common_as_normally_closed',),
        'drive_nc_read_adapter_common',
        False,
        True,
    )


def db9_pin_to_dio(port_id: str, pin: int) -> Optional[int]:
    """Public DB9 pin to DIO helper used by diagnostics."""
    return _db9_pin_to_dio(str(port_id).strip().lower(), pin)


def _resolve_com5_single_sense_fallback(
    port_id: str,
    *,
    common_terminal: Optional[int],
    common_dio: Optional[int],
    no_terminal: Optional[int],
    nc_terminal: Optional[int],
    sensed_pins: tuple[int, ...],
    warnings: list[str],
) -> Optional[
    tuple[
        Optional[int],
        Optional[int],
        str,
        Optional[int],
        Optional[int],
        tuple[str, ...],
        str,
        bool,
        bool,
    ]
]:
    """Map COM=5 switches whose PTP throw pins land off the configured sensed pin.

    PTP is correct (COM=5, NO/NC on 4/6). On this bench the red wire lands on
    DB9 pin 3, but deep diagnostic (2026-06-25, SPS02262-02 seq 600) showed
    switch transitions on PTP CommonTerminal pin 5 (DIO4), not pin 3 (DIO2).
    Read common on pin 5 and drive the active throw pin directly.
    """

    if common_terminal != 5 or len(sensed_pins) != 1 or common_dio is None:
        return None
    if no_terminal is None and nc_terminal is None:
        return None

    read_dio = common_dio
    no_ptp_dio = _db9_pin_to_dio(port_id, no_terminal)
    nc_ptp_dio = _db9_pin_to_dio(port_id, nc_terminal)

    observe_no = no_terminal is not None and nc_terminal is None
    if no_terminal is not None and nc_terminal is not None:
        active_throw = min(no_terminal, nc_terminal)
        observe_no = active_throw == no_terminal

    bench_note = (
        f'COM=5 bench: read PTP CommonTerminal pin {common_terminal} (DIO{read_dio}), '
        f'not configured sensed pin {sensed_pins[0]}'
    )

    if observe_no:
        if no_ptp_dio is None:
            return None
        warnings.append(
            f'{bench_note}; drive NormallyOpenTerminal {no_terminal}'
        )
        return (
            no_terminal,
            no_ptp_dio,
            'normally_open',
            read_dio,
            read_dio,
            ('common_as_normally_open',),
            'drive_no_read_common',
            True,
            False,
        )

    if nc_ptp_dio is None:
        return None
    warnings.append(
        f'{bench_note}; drive NormallyClosedTerminal {nc_terminal}'
    )
    return (
        nc_terminal,
        nc_ptp_dio,
        'normally_closed',
        read_dio,
        read_dio,
        ('common_as_normally_closed',),
        'drive_nc_read_common',
        False,
        True,
    )


def _terminal_from_ptp(
    params: dict[str, Any],
    key: str,
    errors: list[str],
    warnings: Optional[list[str]] = None,
    *,
    allow_not_connected: bool = False,
) -> Optional[int]:
    value = params.get(key)
    if value in (None, ''):
        errors.append(f'Missing {key}')
        return None
    try:
        terminal = int(float(str(value).strip()))
    except (TypeError, ValueError):
        errors.append(f'{key} must be a DB9 pin number')
        return None
    if terminal == 0 and allow_not_connected:
        if warnings is not None:
            warnings.append(f'{key}=0 interpreted as not connected')
        return None
    if terminal < 1 or terminal > 9:
        errors.append(f'{key} must be a DB9 pin from 1 to 9')
        return None
    return terminal


def _sensed_db9_pins(
    port_config: dict[str, Any],
    port_id: str,
    warnings: list[str],
) -> tuple[int, ...]:
    configured = _coerce_pin_tuple(port_config.get('switch_sensed_db9_pins'))
    if configured:
        return configured

    inferred = _infer_legacy_sensed_pins(port_config, port_id)
    if inferred:
        warnings.append(
            'switch_sensed_db9_pins missing; inferred physical sensed pins from legacy DIO config'
        )
    return inferred


def _coerce_pin_tuple(value: Any) -> tuple[int, ...]:
    if value in (None, ''):
        return ()
    raw_values: Iterable[Any]
    if isinstance(value, str):
        raw_values = value.replace(',', ' ').split()
    elif isinstance(value, Iterable):
        raw_values = value
    else:
        raw_values = (value,)

    pins: list[int] = []
    for raw in raw_values:
        try:
            pin = int(float(str(raw).strip()))
        except (TypeError, ValueError):
            continue
        if 1 <= pin <= 9 and pin not in pins:
            pins.append(pin)
    return tuple(sorted(pins))


def _infer_legacy_sensed_pins(port_config: dict[str, Any], port_id: str) -> tuple[int, ...]:
    pins: list[int] = []
    derived_from_no = bool(port_config.get('switch_nc_derived_from_no'))
    derived_from_nc = bool(port_config.get('switch_no_derived_from_nc'))

    if derived_from_no:
        _append_pin_for_dio(pins, port_id, port_config.get('switch_no_dio'))
    elif derived_from_nc:
        _append_pin_for_dio(pins, port_id, port_config.get('switch_nc_dio'))
    else:
        _append_pin_for_dio(pins, port_id, port_config.get('switch_no_dio'))
        _append_pin_for_dio(pins, port_id, port_config.get('switch_nc_dio'))
    return tuple(sorted(pins))


def _append_pin_for_dio(pins: list[int], port_id: str, value: Any) -> None:
    try:
        dio = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return
    pin = _dio_to_db9_pin(port_id, dio)
    if pin is not None and pin not in pins:
        pins.append(pin)


def _db9_pin_to_dio(port_id: str, pin: Optional[int]) -> Optional[int]:
    if pin is None or pin < 1 or pin > 9:
        return None
    if port_id == 'port_a':
        return pin - 1
    if port_id == 'port_b':
        return pin + 8
    return None


def _dio_to_db9_pin(port_id: str, dio: int) -> Optional[int]:
    if port_id == 'port_a':
        pin = dio + 1
    elif port_id == 'port_b':
        pin = dio - 8
    else:
        return None
    if 1 <= pin <= 9:
        return pin
    return None
