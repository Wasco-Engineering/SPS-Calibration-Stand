"""
Configuration loader for Stinger.

Loads and validates the stinger_config.yaml file.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from app.core.paths import get_stinger_config_path

from app.core.logging_config import setup_logging as _setup_logging
from app.services.control_config import ControlConfigError, parse_control_config
from app.services.noise_estimator import (
    DEFAULT_MAX_HOLDOFF_MS,
    DEFAULT_MIN_SAMPLES,
    DEFAULT_TRANSITION_SIGMA_FACTOR,
    DEFAULT_TREND_ALPHA,
    DEFAULT_WINDOW_SAMPLES,
)

logger = logging.getLogger(__name__)

def get_default_config_path() -> Path:
    """Resolve stinger_config.yaml (local stand dir, then repo, then frozen exe dir)."""
    return get_stinger_config_path()


DEFAULT_CONFIG_PATH = get_default_config_path()
MEASUREMENT_SOURCE_TRANSDUCER = 'transducer'
MEASUREMENT_SOURCE_ALICAT = 'alicat'
MEASUREMENT_SOURCE_AUTO = 'auto'
VALID_MEASUREMENT_SOURCES = {
    MEASUREMENT_SOURCE_TRANSDUCER,
    MEASUREMENT_SOURCE_ALICAT,
    MEASUREMENT_SOURCE_AUTO,
}


def normalize_measurement_source(value: Any) -> str:
    """Normalize configured pressure source value to a supported token."""
    normalized = str(value or MEASUREMENT_SOURCE_AUTO).strip().lower()
    if normalized not in VALID_MEASUREMENT_SOURCES:
        logger.warning(
            'Invalid hardware.measurement.preferred_source=%r; defaulting to %s',
            value,
            MEASUREMENT_SOURCE_AUTO,
        )
        return MEASUREMENT_SOURCE_AUTO
    return normalized


def apply_measurement_defaults(config: Dict[str, Any]) -> None:
    """Ensure measurement-source settings exist and are normalized."""
    hardware_cfg = config.setdefault('hardware', {})
    if not isinstance(hardware_cfg, dict):
        raise ValueError('Config section "hardware" must be a mapping')

    measurement_cfg = hardware_cfg.setdefault('measurement', {})
    if not isinstance(measurement_cfg, dict):
        raise ValueError('Config section "hardware.measurement" must be a mapping')

    measurement_cfg['preferred_source'] = normalize_measurement_source(
        measurement_cfg.get('preferred_source', MEASUREMENT_SOURCE_AUTO),
    )
    measurement_cfg['fallback_on_unavailable'] = bool(
        measurement_cfg.get('fallback_on_unavailable', True),
    )

    def _measurement_float(key: str, default: float) -> float:
        try:
            return float(measurement_cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    transducer_only_below = _measurement_float('transducer_only_below_psi', 10.0)
    alicat_only_above = _measurement_float('alicat_only_above_psi', 31.0)
    if alicat_only_above < transducer_only_below:
        alicat_only_above = transducer_only_below + 2.0
    measurement_cfg['transducer_only_below_psi'] = transducer_only_below
    measurement_cfg['alicat_only_above_psi'] = alicat_only_above
    measurement_cfg['switch_pivot_min_psi'] = _measurement_float('switch_pivot_min_psi', 8.0)
    measurement_cfg['sensor_disagreement_fallback_enabled'] = bool(
        measurement_cfg.get('sensor_disagreement_fallback_enabled', True),
    )
    measurement_cfg['sensor_disagreement_max_psi'] = max(
        0.0,
        _measurement_float('sensor_disagreement_max_psi', 0.1),
    )


def apply_debug_noise_defaults(config: Dict[str, Any]) -> None:
    """Ensure debug noise settings exist and are normalized."""
    ui_cfg = config.setdefault('ui', {})
    if not isinstance(ui_cfg, dict):
        raise ValueError('Config section "ui" must be a mapping')

    debug_noise_cfg = ui_cfg.setdefault('debug_noise', {})
    if not isinstance(debug_noise_cfg, dict):
        raise ValueError('Config section "ui.debug_noise" must be a mapping')

    window_samples = debug_noise_cfg.get('window_samples', DEFAULT_WINDOW_SAMPLES)
    try:
        window_samples = max(10, int(window_samples))
    except (TypeError, ValueError):
        window_samples = DEFAULT_WINDOW_SAMPLES
    debug_noise_cfg['window_samples'] = window_samples

    min_samples = debug_noise_cfg.get('min_samples', DEFAULT_MIN_SAMPLES)
    try:
        min_samples = max(5, int(min_samples))
    except (TypeError, ValueError):
        min_samples = DEFAULT_MIN_SAMPLES
    debug_noise_cfg['min_samples'] = min(min_samples, window_samples)

    trend_alpha = debug_noise_cfg.get('trend_alpha', DEFAULT_TREND_ALPHA)
    try:
        trend_alpha = float(trend_alpha)
    except (TypeError, ValueError):
        trend_alpha = DEFAULT_TREND_ALPHA
    if trend_alpha <= 0.0 or trend_alpha >= 1.0:
        trend_alpha = DEFAULT_TREND_ALPHA
    debug_noise_cfg['trend_alpha'] = trend_alpha

    sigma_factor = debug_noise_cfg.get(
        'transition_sigma_factor',
        DEFAULT_TRANSITION_SIGMA_FACTOR,
    )
    try:
        sigma_factor = float(sigma_factor)
    except (TypeError, ValueError):
        sigma_factor = DEFAULT_TRANSITION_SIGMA_FACTOR
    debug_noise_cfg['transition_sigma_factor'] = max(1.0, sigma_factor)

    max_holdoff_ms = debug_noise_cfg.get('max_holdoff_ms', DEFAULT_MAX_HOLDOFF_MS)
    try:
        max_holdoff_ms = int(max_holdoff_ms)
    except (TypeError, ValueError):
        max_holdoff_ms = DEFAULT_MAX_HOLDOFF_MS
    debug_noise_cfg['max_holdoff_ms'] = max(0, max_holdoff_ms)


def apply_database_defaults(config: Dict[str, Any]) -> None:
    """Ensure database-related optional settings exist."""
    database_cfg = config.setdefault('database', {})
    if not isinstance(database_cfg, dict):
        raise ValueError('Config section "database" must be a mapping')

    local_cache = database_cfg.setdefault('local_cache', {})
    if not isinstance(local_cache, dict):
        raise ValueError('Config section "database.local_cache" must be a mapping')

    local_cache['enabled'] = bool(local_cache.get('enabled', True))
    local_cache['path'] = str(local_cache.get('path') or 'stinger_local.sqlite3')
    try:
        interval = int(float(local_cache.get('sync_interval_sec', 60)))
    except (TypeError, ValueError):
        interval = 60
    local_cache['sync_interval_sec'] = max(10, interval)


def _validate_required_sections(config: Dict[str, Any]) -> None:
    """Validate required top-level config sections."""
    required_sections = ['app', 'hardware', 'control', 'timing', 'database', 'ui']
    for section in required_sections:
        if section not in config:
            raise ValueError(f"Missing required config section: {section}")

    if 'labjack' not in config['hardware']:
        raise ValueError("Missing required hardware section: labjack")


def _normalize_and_validate_config(config: Dict[str, Any]) -> None:
    """Run shared normalization and validation for load/save paths."""
    _validate_required_sections(config)
    apply_database_defaults(config)
    apply_measurement_defaults(config)
    apply_debug_noise_defaults(config)
    try:
        parse_control_config(config)
    except ControlConfigError as exc:
        raise ValueError(f'Invalid control configuration: {exc}') from exc


def load_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load configuration from YAML file."""
    path = config_path or get_default_config_path()
    
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    
    logger.info(f"Loading configuration from {path}")
    
    with open(path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError('Configuration root must be a mapping')
    _normalize_and_validate_config(config)
    
    logger.info(f"Configuration loaded: {config['app']['name']} v{config['app']['version']}")
    return config


def save_config(config: Dict[str, Any], config_path: Optional[Path] = None) -> Path:
    """Persist configuration to YAML file after normalization/validation."""
    if not isinstance(config, dict):
        raise ValueError('Configuration root must be a mapping')
    _normalize_and_validate_config(config)

    path = config_path or get_default_config_path()
    logger.info('Saving configuration to %s', path)
    with open(path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(config, f, sort_keys=False)
    return path


def is_port_installed(config: Dict[str, Any], port_id: str) -> bool:
    """True when this port has hardware on the stand (left=port_a, right=port_b)."""
    port_cfg = config.get('hardware', {}).get('labjack', {}).get(port_id, {})
    if not isinstance(port_cfg, dict):
        return False
    if 'port_installed' in port_cfg:
        return bool(port_cfg['port_installed'])
    return bool(port_cfg.get('transducer_installed', True))


def is_transducer_installed(config: Dict[str, Any], port_id: str) -> bool:
    """True when a physical port transducer is installed and should be validated."""
    port_cfg = config.get('hardware', {}).get('labjack', {}).get(port_id, {})
    if not isinstance(port_cfg, dict):
        return True
    return bool(port_cfg.get('transducer_installed', True))


def get_port_config(config: Dict[str, Any], port_id: str) -> Dict[str, Any]:
    """Get configuration for a specific port (port_a or port_b)."""
    labjack_config = config['hardware']['labjack'].get(port_id, {})
    alicat_config = config['hardware']['alicat'].get(port_id, {})

    
    # Merge with common Alicat settings
    alicat_common = {
        'com_port': config['hardware']['alicat'].get('com_port'),
        'baudrate': config['hardware']['alicat'].get('baudrate'),
        'timeout_s': config['hardware']['alicat'].get('timeout_s'),
    }
    alicat_config = {**alicat_common, **alicat_config}

    return {
        'labjack': labjack_config,
        'alicat': alicat_config,
        'solenoid': config['hardware'].get('solenoid', {}),
    }


def setup_logging(config: Dict[str, Any]) -> None:
    """Compatibility wrapper around dedicated logging config module."""
    from app.core.paths import get_config_dir

    _setup_logging(config, project_root=get_config_dir())
