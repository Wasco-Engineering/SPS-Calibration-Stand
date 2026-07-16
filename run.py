#!/usr/bin/env python3
"""
Stinger - Scorpion Calibration Stand
Entry point for the application.
"""

import argparse
import logging
import os
import subprocess
import sys
import time
import threading
from pathlib import Path
from typing import Optional
from ctypes import Structure, byref, sizeof, windll
from ctypes.wintypes import DWORD, HANDLE, LONG, WCHAR

# Add app to path
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from app.core.config import load_config, setup_logging
from app.core.version import __version__, __app_name__

logger = logging.getLogger(__name__)


class _ProcessEntry32(Structure):
    _fields_ = [
        ('dwSize', DWORD),
        ('cntUsage', DWORD),
        ('th32ProcessID', DWORD),
        ('th32DefaultHeapID', HANDLE),
        ('th32ModuleID', DWORD),
        ('cntThreads', DWORD),
        ('th32ParentProcessID', DWORD),
        ('pcPriClassBase', LONG),
        ('dwFlags', DWORD),
        ('szExeFile', WCHAR * 260),
    ]


def _app_root() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _parent_process_id(pid: int) -> Optional[int]:
    if sys.platform != 'win32':
        return None
    snapshot = windll.kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == HANDLE(-1).value:
        return None
    try:
        entry = _ProcessEntry32()
        entry.dwSize = sizeof(_ProcessEntry32)
        if not windll.kernel32.Process32FirstW(snapshot, byref(entry)):
            return None
        while True:
            if int(entry.th32ProcessID) == int(pid):
                return int(entry.th32ParentProcessID)
            if not windll.kernel32.Process32NextW(snapshot, byref(entry)):
                return None
    finally:
        windll.kernel32.CloseHandle(snapshot)


def _find_previous_instance_pids(project_root: Path) -> list[int]:
    """Find older Stinger app processes that could own hardware ports."""
    if sys.platform != 'win32':
        return []

    current_pid = os.getpid()
    current_parent_pid = _parent_process_id(current_pid)
    try:
        result = subprocess.run(
            [
                'tasklist',
                '/FI',
                'IMAGENAME eq SPS Calibration Stand.exe',
                '/FO',
                'CSV',
                '/NH',
            ],
            capture_output=True,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            text=True,
            timeout=5,
        )
    except Exception as e:
        logger.warning('Unable to scan for previous app instances: %s', e)
        return []

    if result.returncode != 0:
        logger.warning('Unable to scan for previous app instances: %s', result.stderr.strip())
        return []

    pids: list[int] = []
    for line in result.stdout.splitlines():
        parts = [part.strip('" ') for part in line.split(',')]
        if len(parts) < 2 or parts[0].upper().startswith('INFO:'):
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if pid != current_pid and pid != current_parent_pid:
            pids.append(pid)
    return pids


def _close_previous_instances(project_root: Path) -> list[int]:
    """Close older instances so the newest app owns COM and LabJack resources."""
    pids = _find_previous_instance_pids(project_root)
    if not pids:
        return []

    closed: list[int] = []
    for pid in pids:
        try:
            result = subprocess.run(
                ['taskkill', '/PID', str(pid), '/F'],
                capture_output=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                text=True,
                timeout=5,
            )
        except Exception as e:
            logger.warning('Unable to close previous app instance %s: %s', pid, e)
            continue
        if result.returncode == 0:
            closed.append(pid)
        else:
            logger.warning(
                'Unable to close previous app instance %s: %s',
                pid,
                result.stderr.strip() or result.stdout.strip(),
            )

    if closed:
        time.sleep(0.5)
    return closed


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Stinger - Scorpion Calibration Stand')
    parser.add_argument(
        '--skip-login',
        action='store_true',
        help='Skip operator login and enter built-in test mode (dev/UI preview only)',
    )
    args = parser.parse_args()

    # Load configuration
    try:
        config = load_config()
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        print("Please ensure stinger_config.yaml exists in the project root.")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR loading config: {e}")
        sys.exit(1)
    
    # Setup logging
    setup_logging(config)
    
    logger.info(f"Starting {__app_name__} v{__version__}")
    closed_pids = _close_previous_instances(_app_root())
    if closed_pids:
        logger.warning('Closed previous app instance(s): %s', closed_pids)

    try:
        # Defer heavy imports so startup reaches UI sooner.
        from app.ui import MainWindow
        from app.services.ui_bridge import UIBridge
        from app.services.work_order_controller import WorkOrderController
        from app.database.session import initialize_database

        # Initialize database in the background so failed/slow SQL handshakes
        # do not block first paint. Controller status polling will pick this up.
        def _init_database_async() -> None:
            db_config = config.get('database', {})
            if not initialize_database(db_config):
                logger.warning("Database connection failed - running in offline mode")
            else:
                logger.info("Database initialization completed")

        threading.Thread(target=_init_database_async, daemon=True).start()
        
        # Create Qt application
        app = QApplication(sys.argv)
        app.setApplicationName(__app_name__)
        app.setApplicationVersion(__version__)
        
        # Enable high DPI scaling for touch screens (if supported)
        if hasattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"):
            app.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
        
        # Create UI bridge
        ui_bridge = UIBridge(config)
        
        # Create and show main window
        window = MainWindow(config, ui_bridge=ui_bridge, skip_login=args.skip_login)
        window.showMaximized()

        # Create work order controller (starts GUI-thread hardware polling timer)
        work_order_controller = WorkOrderController(ui_bridge, config)
        window.attach_work_order_controller(work_order_controller)
        app.aboutToQuit.connect(work_order_controller.cleanup)
        if args.skip_login:
            window.enter_dev_session()
    except Exception as e:
        logger.exception('Startup failed before event loop: %s', e)
        print(f'ERROR starting app: {e}')
        sys.exit(1)
    
    # Run event loop
    exit_code = app.exec()
    
    # Cleanup
    logger.info("Shutting down...")
    work_order_controller.cleanup()
    
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
