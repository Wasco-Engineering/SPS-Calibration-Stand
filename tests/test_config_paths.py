"""Tests for machine-local and hostname-based config path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.core import paths


@pytest.fixture(autouse=True)
def _clear_registry_cache() -> None:
    paths._load_deployment_registry.cache_clear()
    yield
    paths._load_deployment_registry.cache_clear()


def test_explicit_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / 'cfg'
    cfg.mkdir()
    monkeypatch.setenv('STINGER_CONFIG_DIR', str(cfg))
    assert paths.get_config_dir() == cfg.resolve()


def test_hostname_config_dir_selected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / 'repo'
    host = 'TEST-HOST-01'
    cfg_dir = repo / 'configs' / host
    cfg_dir.mkdir(parents=True)
    (cfg_dir / 'stinger_config.yaml').write_text('app:\n  name: host\n', encoding='utf-8')
    (cfg_dir / 'quality_cal_config.yaml').write_text('app:\n  name: q\n', encoding='utf-8')

    monkeypatch.setattr(paths, '_repo_root', lambda: repo)
    monkeypatch.setattr(paths, 'get_install_root', lambda: tmp_path / 'install')
    monkeypatch.setenv('STINGER_HOSTNAME', host)
    monkeypatch.delenv('STINGER_CONFIG_DIR', raising=False)
    monkeypatch.delenv('STINGER_CONFIG', raising=False)

    assert paths.get_config_dir() == cfg_dir.resolve()
    assert paths.get_stinger_config_path() == (cfg_dir / 'stinger_config.yaml').resolve()
    assert paths.get_logs_dir() == (tmp_path / 'install' / 'logs').resolve()


def test_stand_id_maps_through_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / 'repo'
    host = 'ID-FAKE-01'
    cfg_dir = repo / 'configs' / host
    cfg_dir.mkdir(parents=True)
    (cfg_dir / 'stinger_config.yaml').write_text('app:\n  name: mapped\n', encoding='utf-8')
    registry_dir = repo / 'deploy'
    registry_dir.mkdir(parents=True)
    (registry_dir / 'DEPLOYMENT_REGISTRY.yaml').write_text(
        yaml.safe_dump(
            {
                'computers': [
                    {
                        'hostname': host,
                        'stand_id': 'ID-FAKE',
                        'equipment_id': 'ID-FAKE',
                    }
                ]
            }
        ),
        encoding='utf-8',
    )

    monkeypatch.setattr(paths, '_repo_root', lambda: repo)
    monkeypatch.setattr(paths, 'get_install_root', lambda: tmp_path / 'install')
    monkeypatch.setenv('STINGER_STAND_ID', 'ID-FAKE')
    monkeypatch.setenv('STINGER_HOSTNAME', 'not-a-real-host')
    monkeypatch.delenv('STINGER_CONFIG_DIR', raising=False)
    paths._load_deployment_registry.cache_clear()

    assert paths.get_config_dir() == cfg_dir.resolve()


def test_config_dir_uses_legacy_stand_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / 'StingerHome'
    legacy = home / 'STINGER_99'
    legacy.mkdir(parents=True)
    (legacy / 'stinger_config.yaml').write_text('app:\n  name: legacy\n', encoding='utf-8')
    empty_install = tmp_path / 'no_install'
    empty_install.mkdir()
    monkeypatch.setattr(paths, 'get_install_root', lambda: empty_install)
    monkeypatch.setattr(paths, 'get_host_config_dir', lambda: None)
    monkeypatch.setenv('STINGER_HOME', str(home))
    monkeypatch.setenv('STINGER_STAND_ID', 'STINGER_99')
    monkeypatch.delenv('STINGER_CONFIG_DIR', raising=False)
    assert paths.get_config_dir() == legacy.resolve()


def test_stinger_config_prefers_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / 'local'
    cfg.mkdir()
    local_file = cfg / 'stinger_config.yaml'
    local_file.write_text('app:\n  name: local\n', encoding='utf-8')
    monkeypatch.setenv('STINGER_CONFIG_DIR', str(cfg))
    monkeypatch.delenv('STINGER_CONFIG', raising=False)
    assert paths.get_stinger_config_path() == local_file.resolve()
