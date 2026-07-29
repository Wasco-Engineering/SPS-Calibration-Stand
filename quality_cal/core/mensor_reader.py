"""Minimal Mensor serial reader for the quality calibration workflow."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import serial

    SERIAL_AVAILABLE = True
except ImportError:  # pragma: no cover - hardware dependency
    serial = None
    SERIAL_AVAILABLE = False

# EMENSOR / CPG-style dual-channel instruments expose A?/B? (and Baro?).
_CHANNEL_QUERIES = {
    'A': 'A?',
    'B': 'B?',
    'C': 'C?',
    'D': 'D?',
    'BARO': 'Baro?',
    'BAROMETER': 'Baro?',
}


@dataclass(slots=True)
class MensorReading:
    pressure_psia: float
    timestamp: float
    channel: Optional[str] = None


class MensorReader:
    """Simple serial client for a Mensor pressure reference.

    Dual-channel EMENSOR 600 / CPG-class units: configure ``channel`` (``A`` / ``B``)
    or ``port_channels`` (``port_a`` / ``port_b`` → ``A`` / ``B``) so reads use ``A?`` /
    ``B?`` instead of active-channel ``?``.
    """

    # Keep last N raw responses for diagnostic logging (tail of readings).
    _RESPONSE_TAIL_SIZE = 20

    def __init__(self, config: dict[str, Any]):
        self._config = config
        self._port = str(config.get('port', 'COM10'))
        self._baudrate = int(config.get('baudrate', 57600))
        self._timeout_s = float(config.get('timeout_s', 1.0))
        self._default_channel = self._normalize_channel(config.get('channel'))
        raw_map = config.get('port_channels') or {}
        self._port_channels: dict[str, str] = {}
        if isinstance(raw_map, dict):
            for key, value in raw_map.items():
                normalized = self._normalize_channel(value)
                if normalized:
                    self._port_channels[str(key).strip().lower()] = normalized
        self._serial = None
        self._last_status = 'Not Connected'
        self._response_tail: list[str] = []

    @property
    def status(self) -> str:
        return self._last_status

    @property
    def default_channel(self) -> Optional[str]:
        return self._default_channel

    @property
    def port_channels(self) -> dict[str, str]:
        return dict(self._port_channels)

    @property
    def response_tail(self) -> list[str]:
        """Last N raw serial responses for diagnostic logging."""
        return list(self._response_tail)

    def connect(self) -> bool:
        if not SERIAL_AVAILABLE:
            self._last_status = 'Connected (simulated)'
            return True

        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=8,
                parity=serial.PARITY_NONE,
                stopbits=1,
                timeout=self._timeout_s,
            )
            time.sleep(0.3)
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
            for command in ('MODE MEASURE',):
                self._send(command)
            if self._default_channel in ('A', 'B', 'C', 'D'):
                # Set active channel for instruments that only honor ``?``.
                self._send(f'Chan {self._default_channel}')
            self._last_status = 'Connected'
            return True
        except Exception as exc:  # pragma: no cover - hardware dependency
            self._last_status = f'Error: {exc}'
            logger.error('Failed to connect Mensor: %s', exc)
            self.close()
            return False

    def close(self) -> None:
        try:
            if self._serial:
                self._serial.close()
        except Exception:
            pass
        self._serial = None
        if self._last_status != 'Connected (simulated)':
            self._last_status = 'Disconnected'

    def resolve_channel(
        self,
        channel: Optional[str] = None,
        *,
        port_id: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve which transducer channel to query (A/B/...) or None for ``?``."""
        explicit = self._normalize_channel(channel)
        if explicit:
            return explicit
        if port_id:
            mapped = self._port_channels.get(str(port_id).strip().lower())
            if mapped:
                return mapped
        return self._default_channel

    def read_pressure(
        self,
        channel: Optional[str] = None,
        *,
        port_id: Optional[str] = None,
    ) -> MensorReading:
        if not SERIAL_AVAILABLE:
            return MensorReading(
                pressure_psia=14.7,
                timestamp=time.time(),
                channel=self.resolve_channel(channel, port_id=port_id),
            )

        resolved = self.resolve_channel(channel, port_id=port_id)
        command = self._query_command(resolved)
        response = self._send(command)
        pressure = self._parse_pressure(response)
        if pressure is None:
            raise RuntimeError(
                f'Mensor read_pressure failed (channel={resolved or "active"}, cmd={command!r})'
            )
        if not (-1.0 <= pressure <= 300.0):
            logger.warning(
                'Mensor raw response out of range: pressure=%.3f psia, channel=%s, response=%r',
                pressure,
                resolved or 'active',
                response[:200] if response else None,
            )
        return MensorReading(
            pressure_psia=pressure,
            timestamp=time.time(),
            channel=resolved,
        )

    def read_channels(self, channels: tuple[str, ...] = ('A', 'B')) -> dict[str, MensorReading]:
        """Read multiple transducer channels (default A and B)."""
        out: dict[str, MensorReading] = {}
        for channel in channels:
            normalized = self._normalize_channel(channel)
            if not normalized:
                continue
            out[normalized] = self.read_pressure(normalized)
        return out

    def _query_command(self, channel: Optional[str]) -> str:
        if not channel:
            return '?'
        query = _CHANNEL_QUERIES.get(channel)
        if query is None:
            raise ValueError(f'Unsupported Mensor channel: {channel!r}')
        return query

    def _send(self, command: str) -> Optional[str]:
        if self._serial is None:
            return None
        try:
            self._serial.reset_input_buffer()
            self._serial.write(f'{command}\r'.encode())
            self._serial.flush()
            time.sleep(0.05)
            response = self._serial.read_all().decode(errors='ignore').strip()
            if response:
                self._response_tail.append(response)
                if len(self._response_tail) > self._RESPONSE_TAIL_SIZE:
                    self._response_tail.pop(0)
            return response or None
        except Exception as exc:  # pragma: no cover - hardware dependency
            logger.error('Mensor communication error: %s', exc)
            return None

    @staticmethod
    def _normalize_channel(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip().upper()
        if not text or text in ('ACTIVE', 'DEFAULT', '?', 'NONE'):
            return None
        if text in _CHANNEL_QUERIES:
            return 'BARO' if text == 'BAROMETER' else text
        if text.startswith('CH') and len(text) >= 3 and text[2:] in _CHANNEL_QUERIES:
            return text[2:]
        if text.startswith('CHANNEL') and text[7:].strip() in _CHANNEL_QUERIES:
            rest = text[7:].strip()
            return 'BARO' if rest == 'BAROMETER' else rest
        raise ValueError(f'Unsupported Mensor channel: {value!r}')

    @staticmethod
    def _coerce_float(field: str) -> Optional[float]:
        text = field.strip()
        # Mensor often prefixes a signed scientific value with a bare ``E``.
        if len(text) >= 2 and text[0] in 'Ee' and text[1] in '+-':
            text = text[1:]
        try:
            return float(text)
        except ValueError:
            match = re.search(r'[+-]?\d*\.?\d+(?:[Ee][+-]?\d+)?', text)
            if not match:
                return None
            try:
                return float(match.group())
            except ValueError:
                return None

    @staticmethod
    def _parse_pressure(response: Optional[str]) -> Optional[float]:
        if not response:
            return None
        fields = [f.strip() for f in response.split(',') if f.strip()]

        # Prefer scientific-notation fields (typical MEASURE / A? / B? response).
        for field in fields:
            if not re.search(r'[Ee][+-]?\d+', field):
                continue
            value = MensorReader._coerce_float(field)
            if value is not None and abs(value) <= 300.0:
                return value

        first_field = fields[0] if fields else ''
        value = MensorReader._coerce_float(first_field)
        if value is None:
            return None

        # Heuristic for non-scientific numeric fields only (Pa/mbar legacy paths).
        if 'e' not in first_field.lower():
            if value > 100.0:
                return value * 0.0001450377
            if value > 10.0:
                return value * 0.01450377
        return value

    @staticmethod
    def list_available_ports() -> list[str]:
        if not SERIAL_AVAILABLE or serial is None:
            return []
        tools_module = getattr(serial, 'tools', None)
        if tools_module is None:
            return []
        list_ports_module = getattr(tools_module, 'list_ports', None)
        if list_ports_module is None:
            return []
        try:
            return [port.device for port in list_ports_module.comports()]
        except Exception as exc:  # pragma: no cover - hardware dependency
            logger.error('Failed to enumerate Mensor serial ports: %s', exc)
            return []
