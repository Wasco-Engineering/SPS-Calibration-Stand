"""
Resolve machine-local vs shared (release) paths for Stinger.

Shared builds (code, releases, docs) may live on Z: or git.
Per-computer YAML configs live in the repo under ``configs/<hostname>/`` and are
selected automatically from the machine hostname (see deploy/DEPLOYMENT_REGISTRY.yaml).

Environment variables (highest precedence first for config files):

  STINGER_CONFIG          Full path to stinger_config.yaml
  STINGER_QUALITY_CONFIG  Full path to quality_cal_config.yaml
  STINGER_CONFIG_DIR      Directory containing both YAML files (explicit override)
  STINGER_HOSTNAME        Override hostname used for configs/<hostname>/ lookup
  STINGER_STAND_ID        Stand label; maps via deployment registry when hostname
                          folder is missing
  STINGER_HOME            Machine-local root (legacy LOCALAPPDATA layout)
  STINGER_RELEASE_ROOT    Shared release/build root on Z: (documentation / deploy)

Production stands typically clone/run from C:\\Stinger with
``configs\\<hostname>\\stinger_config.yaml``. Logs stay under C:\\Stinger\\logs
(or ``STINGER_CONFIG_DIR``/logs when an explicit config dir override is set).
"""

from __future__ import annotations

import logging
import os
import socket
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Per-PC install root (executables + logs/; configs live under configs/<hostname>/)
DEFAULT_INSTALL_ROOT = Path(r'C:\Stinger')

# Shared engineering root (computer-agnostic artifacts, docs, release bundles)
DEFAULT_RELEASE_ROOT = Path(
    r'Z:\Engineering\Program Builds\Python Builds\Stinger',
)

CONFIGS_DIRNAME = 'configs'
REGISTRY_RELATIVE = Path('deploy') / 'DEPLOYMENT_REGISTRY.yaml'


def get_install_root() -> Path:
    """Return the standard on-disk install directory for this PC."""
    return DEFAULT_INSTALL_ROOT.resolve()


def get_release_root() -> Path:
    raw = os.environ.get('STINGER_RELEASE_ROOT', '').strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return DEFAULT_RELEASE_ROOT


def get_stinger_home() -> Path:
    raw = os.environ.get('STINGER_HOME', '').strip()
    if raw:
        return Path(raw).expanduser().resolve()
    local_app = os.environ.get('LOCALAPPDATA', '').strip()
    if local_app:
        return Path(local_app) / 'Stinger'
    return Path.home() / '.stinger'


def get_stand_id() -> str:
    return os.environ.get('STINGER_STAND_ID', 'default').strip() or 'default'


def get_hostname() -> str:
    """Hostname used to select ``configs/<hostname>/``."""
    override = os.environ.get('STINGER_HOSTNAME', '').strip()
    if override:
        return override
    return socket.gethostname().strip()


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _candidate_roots() -> list[Path]:
    """Roots that may contain ``configs/<hostname>/`` or deploy/registry."""
    roots: list[Path] = []
    for candidate in (_repo_root(), get_install_root()):
        resolved = candidate.resolve()
        if resolved not in roots:
            roots.append(resolved)
    if getattr(sys, 'frozen', False):
        exe_root = Path(sys.executable).resolve().parent
        if exe_root not in roots:
            roots.append(exe_root)
    return roots


@lru_cache(maxsize=1)
def _load_deployment_registry() -> dict[str, Any]:
    try:
        import yaml
    except ImportError:
        return {}

    for root in _candidate_roots():
        path = root / REGISTRY_RELATIVE
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        except Exception as exc:
            logger.warning('Failed to load deployment registry %s: %s', path, exc)
            return {}
        if isinstance(data, dict):
            return data
    return {}


def _registry_computers() -> list[dict[str, Any]]:
    computers = _load_deployment_registry().get('computers') or []
    if not isinstance(computers, list):
        return []
    return [c for c in computers if isinstance(c, dict)]


def lookup_computer_record(
    *,
    hostname: Optional[str] = None,
    stand_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Return the deployment-registry row for this host or stand id."""
    host = (hostname or '').strip().lower()
    stand = (stand_id or '').strip().lower()
    computers = _registry_computers()
    if host:
        for row in computers:
            if str(row.get('hostname') or '').strip().lower() == host:
                return row
    if stand:
        for row in computers:
            if str(row.get('stand_id') or '').strip().lower() == stand:
                return row
            if str(row.get('equipment_id') or '').strip().lower() == stand:
                return row
    return None


def _hostname_config_dir(hostname: str) -> Optional[Path]:
    name = hostname.strip()
    if not name:
        return None
    for root in _candidate_roots():
        candidate = root / CONFIGS_DIRNAME / name
        if (candidate / 'stinger_config.yaml').is_file():
            return candidate.resolve()
    return None


def get_host_config_dir() -> Optional[Path]:
    """
    Auto-selected per-computer config directory from hostname / stand id.

    Looks for ``configs/<hostname>/stinger_config.yaml`` under the repo or
    install root. If missing, maps ``STINGER_STAND_ID`` through the deployment
    registry to a hostname folder.
    """
    host_dir = _hostname_config_dir(get_hostname())
    if host_dir is not None:
        return host_dir

    record = lookup_computer_record(stand_id=get_stand_id())
    if record is not None:
        mapped_host = str(record.get('hostname') or '').strip()
        if mapped_host:
            mapped = _hostname_config_dir(mapped_host)
            if mapped is not None:
                return mapped
    return None


def get_config_dir() -> Path:
    """Directory containing stinger_config.yaml and quality_cal_config.yaml."""
    host_dir = get_host_config_dir()
    raw = os.environ.get('STINGER_CONFIG_DIR', '').strip()
    if raw:
        override = Path(raw).expanduser().resolve()
        # Legacy machine env pointed at C:\Stinger (install root). Prefer the
        # hostname folder when it exists so git-tracked per-PC configs win.
        if host_dir is not None and override == get_install_root():
            return host_dir
        return override

    if host_dir is not None:
        return host_dir

    install_root = get_install_root()
    if (install_root / 'stinger_config.yaml').is_file():
        return install_root

    legacy = get_stinger_home() / get_stand_id()
    if (legacy / 'stinger_config.yaml').is_file():
        return legacy

    # Prefer creating/using the hostname folder under the repo when known.
    record = lookup_computer_record(hostname=get_hostname(), stand_id=get_stand_id())
    host_name = str((record or {}).get('hostname') or get_hostname()).strip()
    if host_name:
        return (_repo_root() / CONFIGS_DIRNAME / host_name).resolve()

    return install_root


def _resolve_named_config(
    env_var: str,
    filename: str,
    *,
    frozen_basename: Optional[str] = None,
) -> Path:
    explicit = os.environ.get(env_var, '').strip()
    if explicit:
        return Path(explicit).expanduser().resolve()

    local_path = get_config_dir() / filename
    if local_path.is_file():
        return local_path

    if getattr(sys, 'frozen', False):
        frozen_name = frozen_basename or filename
        exe_dir = Path(sys.executable).resolve().parent / frozen_name
        if exe_dir.is_file():
            return exe_dir

    # Legacy repo-root fallback (template / transitional).
    repo_path = _repo_root() / filename
    if repo_path.is_file():
        return repo_path

    return local_path


def get_stinger_config_path() -> Path:
    return _resolve_named_config('STINGER_CONFIG', 'stinger_config.yaml')


def get_quality_cal_config_path() -> Path:
    return _resolve_named_config('STINGER_QUALITY_CONFIG', 'quality_cal_config.yaml')


def get_logs_dir() -> Path:
    """
    Log directory.

    Hostname configs keep logs at the install root (``C:\\Stinger\\logs``) so
    tracked YAML under ``configs/<hostname>/`` stays reviewable. Explicit
    ``STINGER_CONFIG_DIR`` overrides (except the legacy install-root value)
    keep logs beside that override for tests/dev sandboxes.
    """
    host_dir = get_host_config_dir()
    raw = os.environ.get('STINGER_CONFIG_DIR', '').strip()
    if raw:
        override = Path(raw).expanduser().resolve()
        if host_dir is not None and override == get_install_root():
            return get_install_root() / 'logs'
        return override / 'logs'

    if host_dir is not None:
        return get_install_root() / 'logs'

    return get_config_dir() / 'logs'


def ensure_config_dir() -> Path:
    path = get_config_dir()
    path.mkdir(parents=True, exist_ok=True)
    get_logs_dir().mkdir(parents=True, exist_ok=True)
    return path
